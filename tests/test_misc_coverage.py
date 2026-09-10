"""Remaining edge paths: version detection, colour support, glob corners, unreadable files.

Small in isolation, but each one is a place where a failure would either crash a run or
silently reduce the file set, which reads as "clean".
"""

from __future__ import annotations

# Imported here, at module scope, rather than inside the tests that use it. `ctypes`
# reads `os.name` at import time and pulls in Windows-only symbols when it is "nt", so a
# first import performed while the fake `os` below is installed would fail off Windows.
# Importing it now caches it in sys.modules, and the later `import ctypes` inside a test
# is served from that cache without re-executing the module.
import ctypes
import os
import sys
from pathlib import Path

import pytest

from pysonarlint import collect as col
from pysonarlint import report as rep
from pysonarlint.collect import (
    Matcher,
    _classify,
    _iter_files,
    _to_regex,
    read_text,
)
from pysonarlint.config import Binding
from pysonarlint.report import _rel, _rule_url, _supports_color


class _FakeOsName:
    """`os` with a different `name`; see the note in tests/test_lsp_transport.py."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str) -> object:
        return getattr(os, attr)


# -- __init__ ------------------------------------------------------------


def test_version_falls_back_when_the_package_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running from a source checkout must not raise on import."""
    import importlib.metadata

    from pysonarlint import _detect_version

    def boom(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("pysonarlint")

    monkeypatch.setattr(importlib.metadata, "version", boom)
    assert _detect_version() == "0.0.0+dev"


def test_version_is_read_from_the_installed_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single source of truth: pyproject.toml, never a hardcoded copy."""
    import importlib.metadata

    from pysonarlint import _detect_version

    monkeypatch.setattr(importlib.metadata, "version", lambda _n: "9.9.9")
    assert _detect_version() == "9.9.9"


def test_the_console_entry_point_delegates_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import pysonarlint
    import pysonarlint.cli

    monkeypatch.setattr(pysonarlint.cli, "main", lambda: 7)
    assert pysonarlint.main() == 7


def test_the_declared_public_surface() -> None:
    import pysonarlint

    assert pysonarlint.__all__ == ["__version__", "main"]


# -- report._supports_color ----------------------------------------------


class _Tty:
    def __init__(self, *, tty: bool = True) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_no_color_on_a_non_terminal() -> None:
    assert _supports_color(_Tty(tty=False)) is False


def test_no_color_on_a_stream_without_isatty() -> None:
    assert _supports_color(object()) is False


def _fake_os_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """`_supports_color` does `import os` inside the function, so it resolves through
    sys.modules rather than a module attribute. The stand-in proxies everything but
    `name`, which keeps pathlib (and pytest's own reporting) working."""
    monkeypatch.setitem(sys.modules, "os", _FakeOsName(name))


def test_no_color_when_no_color_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    _fake_os_module(monkeypatch, "posix")
    assert _supports_color(_Tty()) is False


def test_color_on_a_posix_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    _fake_os_module(monkeypatch, "posix")
    assert _supports_color(_Tty()) is True


def test_color_on_windows_enables_vt_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows Terminal and modern conhost need the mode set explicitly."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    _fake_os_module(monkeypatch, "nt")
    calls: list[str] = []

    class _Kernel32:
        def GetStdHandle(self, _n: int) -> int:  # noqa: N802 - a Win32 API name
            calls.append("GetStdHandle")
            return 1

        def SetConsoleMode(self, _h: int, _mode: int) -> int:  # noqa: N802 - a Win32 API name
            calls.append("SetConsoleMode")
            return 1

    # raising=False: `windll` only exists on Windows, so off Windows this adds it.
    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": _Kernel32()})(), raising=False)
    assert _supports_color(_Tty()) is True
    assert calls == ["GetStdHandle", "SetConsoleMode"]


def test_no_color_when_the_windows_console_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy console that cannot do VT gets plain text rather than escape garbage."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    _fake_os_module(monkeypatch, "nt")

    class _Broken:
        @property
        def kernel32(self) -> object:
            raise OSError("no console")

    monkeypatch.setattr(ctypes, "windll", _Broken(), raising=False)
    assert _supports_color(_Tty()) is False


def test_render_text_uses_colour_when_the_stream_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pysonarlint.analyze import Result
    from pysonarlint.lsp import Diagnostic

    monkeypatch.setattr(rep, "_supports_color", lambda _s: True)
    result = Result(
        issues=[
            Diagnostic(
                path=Path("/repo/a.py"),
                line=1,
                column=1,
                end_line=1,
                end_column=2,
                rule="python:S100",
                message="m",
                severity=1,
            )
        ],
        analyzed=[Path("/repo/a.py")],
    )
    out = rep.render_text(result, Path("/repo"), stream=_Tty())
    assert "\033[" in out


def test_render_text_is_plain_when_the_stream_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    from pysonarlint.analyze import Result

    monkeypatch.setattr(rep, "_supports_color", lambda _s: False)
    out = rep.render_text(Result(analyzed=[Path("/repo/a.py")]), Path("/repo"), stream=_Tty())
    assert "\033[" not in out


def test_render_text_defaults_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    from pysonarlint.analyze import Result

    seen: list[object] = []
    monkeypatch.setattr(rep, "_supports_color", lambda s: bool(seen.append(s)) and False)
    rep.render_text(Result(), Path("/repo"))
    assert seen[0] is sys.stdout


# -- report._rule_url ----------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("python:S1481", "https://rules.sonarsource.com/python/RSPEC-1481/"),
        ("javascript:S3923", "https://rules.sonarsource.com/javascript/RSPEC-3923/"),
        ("no-colon", None),
        ("python:NotAnS", None),  # hotspots and other keys have no RSPEC page
        ("", None),
    ],
)
def test_rule_url(rule: str, expected: str | None) -> None:
    assert _rule_url(rule) == expected


def test_rel_falls_back_to_an_absolute_path_outside_the_root() -> None:
    """A file outside the root must still be named, not crash the render."""
    assert _rel(Path("/elsewhere/a.py"), Path("/repo")) == "/elsewhere/a.py"


# -- collect._to_regex ---------------------------------------------------


def test_a_leading_slash_is_stripped_from_a_pattern() -> None:
    """Sonar patterns are root-relative, with or without the leading slash."""
    assert _to_regex("/src/a.py").match("src/a.py")


def test_double_star_crosses_directories() -> None:
    rx = _to_regex("**/vendor/**")
    assert rx.match("a/b/vendor/c/d.py")
    assert rx.match("vendor/c.py")


def test_a_trailing_double_star_matches_the_rest() -> None:
    assert _to_regex("src/**").match("src/a/b/c.py")


def test_a_single_star_does_not_cross_a_separator() -> None:
    """fnmatch cannot express this, which is why the translation is explicit."""
    rx = _to_regex("src/*.py")
    assert rx.match("src/a.py")
    assert not rx.match("src/sub/a.py")


def test_a_question_mark_matches_exactly_one_non_separator() -> None:
    rx = _to_regex("a?.py")
    assert rx.match("ab.py")
    assert not rx.match("abc.py")
    assert not rx.match("a/.py")


def test_an_empty_pattern_matches_nothing() -> None:
    rx = _to_regex("   ")
    for candidate in ("", "a.py", "any/thing"):
        assert not rx.match(candidate)


def test_backslashes_in_a_pattern_are_normalised() -> None:
    assert _to_regex("src\\a.py").match("src/a.py")


def test_regex_metacharacters_in_a_pattern_are_literal() -> None:
    rx = _to_regex("a+b.py")
    assert rx.match("a+b.py")
    assert not rx.match("aab.py")


# -- collect.Matcher -----------------------------------------------------


def test_a_matcher_with_no_patterns_excludes_nothing(tmp_path: Path) -> None:
    assert Matcher([], tmp_path).excluded(tmp_path / "a.py") is False


def test_a_matcher_ignores_blank_patterns(tmp_path: Path) -> None:
    assert Matcher(["", "   "], tmp_path).excluded(tmp_path / "a.py") is False


def test_a_matcher_matches_on_the_bare_file_name_too(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert Matcher(["a.py"], tmp_path).excluded(tmp_path / "sub" / "a.py") is True


def test_a_matcher_handles_a_path_outside_its_root(tmp_path: Path) -> None:
    """Falls back to the whole path rather than raising on relative_to."""
    matcher = Matcher(["**/elsewhere/**"], tmp_path / "root")
    assert matcher.excluded(Path("/elsewhere/a.py")) is True


# -- collect._classify ---------------------------------------------------


def test_an_unknown_suffix_is_silently_uninteresting(tmp_path: Path) -> None:
    """No skip reason: a README is not a deliberate exclusion worth reporting."""
    path = tmp_path / "README.md"
    path.write_text("hi\n", encoding="utf-8")
    assert _classify(path, Matcher([], tmp_path), None, 1000) == (None, None)


def test_a_language_outside_the_wanted_set_is_silent(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert _classify(path, Matcher([], tmp_path), {"java"}, 1000) == (None, None)


def test_an_excluded_file_is_reported_as_excluded(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert _classify(path, Matcher(["**/a.py"], tmp_path), None, 1000) == (None, "excluded")


def test_an_oversized_file_is_reported_as_large(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x" * 100, encoding="utf-8")
    assert _classify(path, Matcher([], tmp_path), None, max_bytes=10) == (None, "large")


def test_an_unstattable_file_is_skipped_silently(tmp_path: Path) -> None:
    """A vanished or permission-denied file must not abort the walk."""
    assert _classify(tmp_path / "gone.py", Matcher([], tmp_path), None, 1000) == (None, None)


def test_an_accepted_file_reports_its_language(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert _classify(path, Matcher([], tmp_path), {"python"}, 1000) == ("python", None)


# -- collect._iter_files -------------------------------------------------


def test_iter_files_on_a_single_file_yields_just_it(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert list(_iter_files(path, Matcher([], tmp_path))) == [path]


def test_iter_files_prunes_before_descending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning must happen during the walk, not after: a virtualenv on network storage
    took over two minutes to enumerate."""
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "big.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    visited: list[str] = []
    real_walk = col.os.walk

    def spy(*args: object, **kwargs: object):  # noqa: ANN202 - an os.walk spy
        for dirpath, dirnames, filenames in real_walk(*args, **kwargs):  # type: ignore[arg-type]
            visited.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(col.os, "walk", spy)
    found = [p.name for p in _iter_files(tmp_path, Matcher([], tmp_path))]
    assert found == ["a.py"]
    assert not any(".venv" in v for v in visited)  # never descended into


def test_iter_files_skips_dot_directories(tmp_path: Path) -> None:
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    assert [p.name for p in _iter_files(tmp_path, Matcher([], tmp_path))] == ["b.py"]


def test_iter_files_honours_directory_exclusions(tmp_path: Path) -> None:
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    found = [p.name for p in _iter_files(tmp_path, Matcher(["vendor"], tmp_path))]
    assert found == ["b.py"]


def test_iter_files_does_not_revisit_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlink and junction loops are common on Windows and would otherwise never end.

    Both subdirectories are made to report the same (st_dev, st_ino), which is what a
    junction loop looks like from os.walk: the second one must be pruned.
    """
    for name, source in (("a", "x.py"), ("b", "y.py")):
        (tmp_path / name).mkdir()
        (tmp_path / name / source).write_text("x = 1\n", encoding="utf-8")

    class _SameInode:
        st_dev = 1
        st_ino = 1

    real_stat = Path.stat
    # Compared as plain strings: is_dir() and resolve() both call stat(), so touching
    # either from inside the patch recurses forever. The top directory keeps its real
    # stat, because _iter_files calls start.is_file() on it before walking.
    looping = {str(tmp_path / "a"), str(tmp_path / "b")}

    def same(self: Path, **kwargs: object) -> object:
        if str(self) in looping:
            return _SameInode()
        return real_stat(self, **kwargs)  # type: ignore[arg-type]

    # os.walk yields subdirectories in filesystem order, which NTFS sorts and ext4 does
    # not, so without this "b" is descended into first and the surviving file is y.py.
    # Which of the two survives is not the behaviour under test; that exactly one does
    # is. Sorting in place fixes the descent order so the assertion can name a file.
    real_walk = col.os.walk

    def ordered(*args: object, **kwargs: object):  # noqa: ANN202 - an os.walk wrapper
        for dirpath, dirnames, filenames in real_walk(*args, **kwargs):  # type: ignore[arg-type]
            dirnames.sort()
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(col.os, "walk", ordered)
    monkeypatch.setattr(Path, "stat", same)
    found = sorted(p.name for p in _iter_files(tmp_path, Matcher([], tmp_path)))
    assert found == ["x.py"]  # "b" shared "a"'s inode key and was skipped


def test_iter_files_skips_a_directory_it_cannot_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "top.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "hidden.py").write_text("x = 1\n", encoding="utf-8")
    real_stat = Path.stat

    def boom(self: Path, **kwargs: object) -> object:
        if str(self) == str(tmp_path / "locked"):
            raise OSError("permission denied")
        return real_stat(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", boom)
    assert [p.name for p in _iter_files(tmp_path, Matcher([], tmp_path))] == ["top.py"]


# -- collect.read_text ---------------------------------------------------


def test_read_text_strips_a_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_bytes(b"\xef\xbb\xbfx = 1\n")
    assert read_text(path) == "x = 1\n"


def test_read_text_falls_back_to_latin_1(tmp_path: Path) -> None:
    """Analyzers assume these encodings; a decode failure must not drop the file."""
    path = tmp_path / "a.py"
    path.write_bytes(b"x = '\xe9'\n")  # invalid utf-8
    assert read_text(path) == "x = '\xe9'\n"


def test_read_text_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert read_text(tmp_path / "gone.py") is None


def test_read_text_returns_none_when_every_encoding_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")

    def boom(*_a: object, **_k: object) -> str:
        raise LookupError("unknown codec")

    monkeypatch.setattr(Path, "read_text", boom)
    assert read_text(path) is None


# -- SourceFile ----------------------------------------------------------


def test_the_analyzer_for_a_language_without_one_is_none() -> None:
    assert col.SourceFile(path=Path("a.txt"), language="plaintext").analyzer is None


def test_analyzers_for_deduplicates() -> None:
    files = [
        col.SourceFile(path=Path("a.py"), language="python"),
        col.SourceFile(path=Path("b.ipynb"), language="jupyter"),  # also sonarpython
        col.SourceFile(path=Path("c.ts"), language="typescript"),
    ]
    assert col.analyzers_for(files) == {"sonarpython", "sonarjs"}


# -- Binding.is_cloud ----------------------------------------------------


@pytest.mark.parametrize(
    ("binding", "is_cloud"),
    [
        (Binding(organization="org"), True),
        (Binding(url="https://sonarcloud.io"), True),
        (Binding(url="https://sonarqube.us"), True),
        (Binding(url="https://sonar.internal.example.com"), False),
        (Binding(), False),
    ],
)
def test_binding_cloud_detection(binding: Binding, is_cloud: bool) -> None:
    assert binding.is_cloud is is_cloud
