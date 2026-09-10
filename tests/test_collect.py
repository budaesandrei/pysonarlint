"""Glob translation and traversal pruning."""

from __future__ import annotations

from pathlib import Path

import pytest

from pysonarlint.collect import LANGUAGES, Matcher, _to_regex, collect


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/site-packages/**", "a/b/site-packages/x/y.py", True),
        ("**/site-packages/**", "site-packages/x.py", True),
        ("**/site-packages/**", "app/services/traces.py", False),
        ("**/*.pyc", "app/x.pyc", True),
        ("**/*.pyc", "x.pyc", True),
        ("**/*.pyc", "app/x.py", False),
        ("**/test_*.py", "tests/test_x.py", True),
        # A single * must not cross a path separator, which is why fnmatch is unusable.
        ("app/*.py", "app/main.py", True),
        ("app/*.py", "app/sub/main.py", False),
        ("app/**/*.py", "app/sub/deep/main.py", True),
        ("?.py", "a.py", True),
        ("?.py", "ab.py", False),
    ],
)
def test_glob_translation(pattern: str, path: str, expected: bool) -> None:
    assert bool(_to_regex(pattern).match(path)) is expected


def test_empty_pattern_never_matches() -> None:
    assert not _to_regex("").match("anything.py")


def test_matcher_is_relative_to_root(tmp_path: Path) -> None:
    matcher = Matcher(["**/vendor/**"], tmp_path)
    assert matcher.excluded(tmp_path / "src" / "vendor" / "lib.py")
    assert not matcher.excluded(tmp_path / "src" / "app.py")


def test_matcher_without_patterns_excludes_nothing(tmp_path: Path) -> None:
    assert not Matcher([], tmp_path).excluded(tmp_path / "anything.py")


def test_collect_prunes_virtualenvs(tmp_path: Path) -> None:
    """The pruned directories must never be descended into."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n")
    for junk in (".venv", "__pycache__", "node_modules", ".git"):
        deep = tmp_path / junk / "nested"
        deep.mkdir(parents=True)
        (deep / "ignored.py").write_text("y = 2\n")

    files, _notes = collect([tmp_path], root=tmp_path, languages={"python"})
    names = {f.path.name for f in files}
    assert names == {"main.py"}


def test_collect_honours_exclusions(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("a = 1\n")
    (tmp_path / "skip.py").write_text("b = 2\n")
    files, notes = collect(
        [tmp_path], root=tmp_path, exclusions=["**/skip.py"], languages={"python"}
    )
    assert {f.path.name for f in files} == {"keep.py"}
    assert any("excluded" in n for n in notes)


def test_collect_reports_skipped_large_files(tmp_path: Path) -> None:
    """Silently dropping files would make a reduced run look clean."""
    (tmp_path / "big.py").write_text("# " + "x" * 500)
    files, notes = collect([tmp_path], root=tmp_path, languages={"python"}, max_bytes=100)
    assert not files
    assert any("larger than" in n for n in notes)


def test_collect_notes_missing_target(tmp_path: Path) -> None:
    _files, notes = collect([tmp_path / "nope"], root=tmp_path)
    assert any("does not exist" in n for n in notes)


def test_collect_deduplicates_overlapping_targets(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    files, _ = collect([tmp_path, tmp_path / "a.py"], root=tmp_path, languages={"python"})
    assert len(files) == 1


def test_collect_filters_by_language(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.js").write_text("var x = 1\n")
    files, _ = collect([tmp_path], root=tmp_path, languages={"python"})
    assert {f.path.name for f in files} == {"a.py"}


def test_python_extensions_are_mapped() -> None:
    assert LANGUAGES[".py"] == "python"
    assert LANGUAGES[".pyi"] == "python"
