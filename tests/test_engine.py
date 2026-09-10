"""Locating an installed SonarQube for IDE extension and a usable JRE.

Fake extension trees under tmp_path, with Path.home() redirected and the java version
probe stubbed, so nothing here requires a real install and no JVM is ever started.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from pysonarlint import engine as eng
from pysonarlint.engine import (
    MIN_JAVA,
    Engine,
    EngineNotFound,
    _bundled_java,
    _candidate_javas,
    _engine_version,
    _extension_roots,
    _java_major,
    _version_key,
    discover,
    find_java,
)


@pytest.fixture(autouse=True)
def _no_real_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A real extension on the developer's machine must not leak into any assertion."""
    for var in ("PYSONARLINT_HOME", "PYSONARLINT_JAVA", "JAVA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "nowhere"))
    monkeypatch.setattr(eng.shutil, "which", lambda _name: None)


# -- fake extension trees --------------------------------------------------


def _touch(path: Path, content: bytes = b"stub") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_extension(
    root: Path,
    *,
    server_jar: bool = True,
    analyzers: tuple[str, ...] = ("sonarpython", "sonarjs"),
    jre: bool = True,
) -> Path:
    """Build the on-disk shape the extension actually has."""
    if server_jar:
        _touch(root / "server" / "sonarlint-ls.jar")
    for name in analyzers:
        _touch(root / "analyzers" / f"{name}.jar")
    if jre:
        exe = "java.exe" if os.name == "nt" else "java"
        # The bundled JRE's directory name confusingly ends in ".tar".
        _touch(root / "jre" / "17.0.11-win32-x64.tar" / "bin" / exe)
    return root


def _extension_dir(home: Path, editor: str, version: str, *, suffix: str = "-win32-x64") -> Path:
    """The real installed name carries a platform suffix, e.g. ...-5.9.1-win32-x64."""
    return home / editor / "extensions" / f"sonarsource.sonarlint-vscode-{version}{suffix}"


@pytest.fixture
def good_java(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every candidate java reports a supported version, without being executed."""
    monkeypatch.setattr(eng, "_java_major", lambda _path: 17)


# -- _version_key ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The platform-suffixed form is what actually lands on disk, e.g.
        # sonarsource.sonarlint-vscode-5.9.1-win32-x64. The version must be read out of
        # the middle of the name, not just off the end.
        ("sonarsource.sonarlint-vscode-5.9.1-win32-x64", (5, 9, 1)),
        ("sonarsource.sonarlint-vscode-5.10.0-linux-x64", (5, 10, 0)),
        ("sonarsource.sonarlint-vscode-4.30.0", (4, 30, 0)),
        ("sonarsource.sonarlint-vscode-4.9.1", (4, 9, 1)),
        ("sonarsource.sonarlint-vscode-4", (4,)),
        # Anything without a version immediately after the prefix sorts last.
        ("sonarsource.sonarlint-vscode-universal", (0,)),
        ("something-else", (0,)),
    ],
)
def test_version_key(name: str, expected: tuple[int, ...]) -> None:
    assert _version_key(Path(name)) == expected


@pytest.mark.parametrize("suffix", ["", "-win32-x64"])
def test_version_key_orders_numerically_not_lexically(suffix: str) -> None:
    """5.10 must sort above 5.9; a string compare would get this backwards."""
    names = [
        Path(f"sonarsource.sonarlint-vscode-5.9.0{suffix}"),
        Path(f"sonarsource.sonarlint-vscode-5.30.0{suffix}"),
        Path(f"sonarsource.sonarlint-vscode-5.10.2{suffix}"),
    ]
    newest_first = sorted(names, key=_version_key, reverse=True)
    assert [p.name for p in newest_first] == [
        f"sonarsource.sonarlint-vscode-5.30.0{suffix}",
        f"sonarsource.sonarlint-vscode-5.10.2{suffix}",
        f"sonarsource.sonarlint-vscode-5.9.0{suffix}",
    ]


# -- _extension_roots ----------------------------------------------------


def test_extension_roots_is_empty_on_a_clean_machine() -> None:
    assert _extension_roots() == []


def test_extension_roots_finds_every_editor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    for editor in (".vscode", ".cursor", ".kiro", ".windsurf", ".vscodium", ".vscode-insiders"):
        _extension_dir(home, editor, "4.30.0").mkdir(parents=True)
    found = _extension_roots()
    assert len(found) == 6
    assert {p.parent.parent.name for p in found} == {
        ".vscode",
        ".cursor",
        ".kiro",
        ".windsurf",
        ".vscodium",
        ".vscode-insiders",
    }


def test_extension_roots_returns_newest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    for version in ("4.9.0", "4.30.0", "4.10.1"):
        _extension_dir(home, ".vscode", version).mkdir(parents=True)
    assert [_version_key(p) for p in _extension_roots()] == [(4, 30, 0), (4, 10, 1), (4, 9, 0)]


def test_extension_roots_ignores_files_and_unrelated_extensions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    ext = home / ".vscode" / "extensions"
    ext.mkdir(parents=True)
    (ext / "ms-python.python-2024.1").mkdir()
    _touch(ext / "sonarsource.sonarlint-vscode-4.30.0")  # a file, not a directory
    assert _extension_roots() == []


def test_extension_roots_skips_an_editor_with_no_extensions_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    (home / ".vscode").mkdir(parents=True)  # exists, but has no extensions/
    assert _extension_roots() == []


# -- _bundled_java -------------------------------------------------------


def test_bundled_java_is_found_under_the_tar_named_directory(tmp_path: Path) -> None:
    root = make_extension(tmp_path / "ext")
    found = _bundled_java(root)
    assert found is not None
    assert found.parent.parent.name.endswith(".tar")


def test_bundled_java_is_none_without_a_jre(tmp_path: Path) -> None:
    root = make_extension(tmp_path / "ext", jre=False)
    assert _bundled_java(root) is None


def test_bundled_java_ignores_a_directory_named_like_the_binary(tmp_path: Path) -> None:
    root = tmp_path / "ext"
    exe = "java.exe" if os.name == "nt" else "java"
    (root / "jre" / "17.tar" / "bin" / exe).mkdir(parents=True)
    assert _bundled_java(root) is None


# -- _java_major ---------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ('openjdk version "17.0.11" 2024-04-16', 17),
        ('java version "21.0.3" 2024-04-16 LTS', 21),
        ('openjdk version "1.8.0_402"', 8),  # legacy numbering
        ('openjdk version "11.0.22" 2024-01-16', 11),
        ("no version anywhere", None),
        ("", None),
    ],
)
def test_java_major_parses_the_banner(
    stderr: str, expected: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Java prints its version to stderr, which is easy to get wrong."""
    monkeypatch.setattr(
        eng.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="", stderr=stderr),
    )
    assert _java_major(Path("java")) == expected


def test_java_major_also_reads_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        eng.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, stdout='openjdk version "17.0.1"', stderr=""
        ),
    )
    assert _java_major(Path("java")) == 17


@pytest.mark.parametrize(
    "error",
    [
        OSError("not executable"),
        subprocess.TimeoutExpired("java", 30),
    ],
)
def test_java_major_is_none_when_the_binary_will_not_run(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stub JDK directory (common on corporate images) has the layout but no binary."""

    def boom(*_a: object, **_k: object) -> None:
        raise error

    monkeypatch.setattr(eng.subprocess, "run", boom)
    assert _java_major(Path("java")) is None


# -- _candidate_javas ----------------------------------------------------


def test_candidate_order_puts_the_override_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = make_extension(tmp_path / "ext")
    monkeypatch.setenv("PYSONARLINT_JAVA", str(tmp_path / "mine" / "java"))
    monkeypatch.setenv("JAVA_HOME", str(tmp_path / "jhome"))
    monkeypatch.setattr(eng.shutil, "which", lambda _n: str(tmp_path / "path" / "java"))
    candidates = _candidate_javas(root)
    assert candidates[0] == tmp_path / "mine" / "java"
    assert candidates[1].parent.parent.parent.name == "jre"  # the bundled JRE next
    assert candidates[2].parent.name == "bin"  # then JAVA_HOME/bin
    assert candidates[3] == tmp_path / "path" / "java"  # then PATH


def test_candidates_is_empty_with_nothing_configured() -> None:
    assert _candidate_javas(None) == []


def test_candidates_without_a_root_skips_the_bundled_jre(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = "java.exe" if os.name == "nt" else "java"
    monkeypatch.setenv("JAVA_HOME", str(tmp_path / "jhome"))
    assert _candidate_javas(None) == [tmp_path / "jhome" / "bin" / exe]


# -- find_java -----------------------------------------------------------


def test_find_java_returns_the_bundled_jre(tmp_path: Path, good_java: None) -> None:
    root = make_extension(tmp_path / "ext")
    assert find_java(root) == _bundled_java(root)


def test_find_java_prefers_the_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_extension(tmp_path / "ext")
    mine = _touch(tmp_path / "mine" / "java")
    monkeypatch.setenv("PYSONARLINT_JAVA", str(mine))
    monkeypatch.setattr(eng, "_java_major", lambda _p: 21)
    assert find_java(root) == mine


def test_find_java_rejects_a_stub_binary_and_moves_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason the version is *executed* rather than inferred from the path."""
    root = make_extension(tmp_path / "ext")
    stub = _touch(tmp_path / "stub" / "java")
    monkeypatch.setenv("PYSONARLINT_JAVA", str(stub))
    monkeypatch.setattr(eng, "_java_major", lambda path: None if path == stub else 17)
    assert find_java(root) == _bundled_java(root)


def test_find_java_names_the_stub_in_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _touch(tmp_path / "stub" / "java")
    monkeypatch.setenv("PYSONARLINT_JAVA", str(stub))
    monkeypatch.setattr(eng, "_java_major", lambda _p: None)
    with pytest.raises(EngineNotFound) as exc:
        find_java(None)
    assert "did not run" in str(exc.value)
    assert str(stub) in str(exc.value)


def test_find_java_rejects_a_too_old_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _touch(tmp_path / "old" / "java")
    monkeypatch.setenv("PYSONARLINT_JAVA", str(old))
    monkeypatch.setattr(eng, "_java_major", lambda _p: 11)
    with pytest.raises(EngineNotFound) as exc:
        find_java(None)
    assert f"Java 11, need {MIN_JAVA}+" in str(exc.value)


def test_find_java_skips_a_candidate_that_is_not_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYSONARLINT_JAVA", str(tmp_path / "does" / "not" / "exist"))
    with pytest.raises(EngineNotFound, match="none found"):
        find_java(None)


def test_find_java_failure_names_the_remedies(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(EngineNotFound) as exc:
        find_java(None)
    message = str(exc.value)
    assert "SonarQube for IDE extension" in message
    assert "PYSONARLINT_JAVA" in message


# -- _engine_version -----------------------------------------------------


def test_engine_version_comes_from_the_directory_name(tmp_path: Path) -> None:
    root = tmp_path / "sonarsource.sonarlint-vscode-4.30.0"
    root.mkdir()
    assert _engine_version(root, root / "server" / "sonarlint-ls.jar") == "4.30.0"


def test_engine_version_falls_back_to_the_jar_manifest(tmp_path: Path) -> None:
    """A --sonarlint-home directory has no version in its name."""
    root = tmp_path / "my-unpacked-extension"
    jar = root / "server" / "sonarlint-ls.jar"
    jar.parent.mkdir(parents=True)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\r\nImplementation-Version: 4.29.1\r\nBuild-Jdk: 17\r\n",
        )
    assert _engine_version(root, jar) == "4.29.1"


def test_engine_version_is_unknown_when_the_manifest_has_no_version(tmp_path: Path) -> None:
    root = tmp_path / "unnamed"
    jar = root / "server" / "sonarlint-ls.jar"
    jar.parent.mkdir(parents=True)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n")
    assert _engine_version(root, jar) == "unknown"


def test_engine_version_is_unknown_when_the_jar_has_no_manifest(tmp_path: Path) -> None:
    root = tmp_path / "unnamed"
    jar = root / "server" / "sonarlint-ls.jar"
    jar.parent.mkdir(parents=True)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("other.txt", "x")
    assert _engine_version(root, jar) == "unknown"


def test_engine_version_is_unknown_for_a_jar_that_is_not_a_zip(tmp_path: Path) -> None:
    root = tmp_path / "unnamed"
    jar = _touch(root / "server" / "sonarlint-ls.jar", b"not a zip at all")
    assert _engine_version(root, jar) == "unknown"


def test_engine_version_is_unknown_for_a_missing_jar(tmp_path: Path) -> None:
    root = tmp_path / "unnamed"
    root.mkdir()
    assert _engine_version(root, root / "server" / "sonarlint-ls.jar") == "unknown"


# -- discover ------------------------------------------------------------


def test_discover_from_an_explicit_root(tmp_path: Path, good_java: None) -> None:
    root = make_extension(tmp_path / "sonarsource.sonarlint-vscode-4.30.0")
    found = discover(root)
    assert found.root == root
    assert found.version == "4.30.0"
    assert found.server_jar == root / "server" / "sonarlint-ls.jar"
    assert [a.stem for a in found.analyzers] == ["sonarjs", "sonarpython"]  # sorted
    assert found.java == _bundled_java(root)


def test_discover_from_an_installed_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, good_java: None
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    make_extension(_extension_dir(home, ".vscode", "4.30.0"))
    assert discover().version == "4.30.0"


def test_discover_prefers_the_newest_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, good_java: None
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    make_extension(_extension_dir(home, ".vscode", "4.9.0"))
    make_extension(_extension_dir(home, ".cursor", "4.30.0"))
    assert discover().version == "4.30.0"


def test_the_environment_override_beats_an_explicit_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, good_java: None
) -> None:
    from_env = make_extension(tmp_path / "from-env")
    explicit = make_extension(tmp_path / "sonarsource.sonarlint-vscode-4.30.0")
    monkeypatch.setenv("PYSONARLINT_HOME", str(from_env))
    assert discover(explicit).root == from_env


def test_discover_skips_a_broken_install_and_uses_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, good_java: None
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    make_extension(_extension_dir(home, ".vscode", "4.30.0"), server_jar=False)
    make_extension(_extension_dir(home, ".cursor", "4.9.0"))
    assert discover().version == "4.9.0"


def test_discover_reports_a_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(EngineNotFound) as exc:
        discover(missing)
    assert f"{missing}: not a directory" in str(exc.value)


def test_discover_reports_a_missing_server_jar(tmp_path: Path) -> None:
    root = make_extension(tmp_path / "ext", server_jar=False)
    with pytest.raises(EngineNotFound) as exc:
        discover(root)
    assert f"{root}: no server/sonarlint-ls.jar" in str(exc.value)


def test_discover_reports_missing_analyzers(tmp_path: Path) -> None:
    root = make_extension(tmp_path / "ext", analyzers=())
    with pytest.raises(EngineNotFound) as exc:
        discover(root)
    assert f"{root}: no analyzers/*.jar" in str(exc.value)


def test_discover_on_a_clean_machine_says_nothing_exists() -> None:
    with pytest.raises(EngineNotFound) as exc:
        discover()
    assert "no extension directories exist" in str(exc.value)


def test_discover_failure_explains_how_to_fix_it(tmp_path: Path) -> None:
    with pytest.raises(EngineNotFound) as exc:
        discover(tmp_path / "nope")
    message = str(exc.value)
    assert "Install the 'SonarQube for IDE' extension" in message
    assert "PYSONARLINT_HOME" in message


def test_discover_propagates_a_java_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A found extension with no runnable JRE must fail loudly, not silently degrade."""
    root = make_extension(tmp_path / "ext")
    monkeypatch.setattr(eng, "_java_major", lambda _p: None)
    with pytest.raises(EngineNotFound, match="No usable Java"):
        discover(root)


# -- Engine --------------------------------------------------------------


def test_engine_analyzer_lookup_by_stem(tmp_path: Path, good_java: None) -> None:
    root = make_extension(tmp_path / "ext", analyzers=("sonarpython", "sonarjs"))
    found = discover(root)
    assert found.analyzer("sonarpython") == root / "analyzers" / "sonarpython.jar"
    assert found.analyzer("sonarjava") is None


def test_engine_is_frozen() -> None:
    found = Engine(
        java=Path("java"),
        server_jar=Path("ls.jar"),
        analyzers=(),
        root=Path("."),
        version="1.0",
    )
    with pytest.raises(AttributeError):
        found.version = "2.0"  # type: ignore[misc]
