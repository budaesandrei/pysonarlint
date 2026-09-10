"""Find the files worth analyzing.

Pruning must happen *during* traversal, not after. A real target in testing held 12,989
files of which 32 were sources; the rest was a virtualenv on network-backed storage.
Enumerating everything and filtering afterwards took over two minutes, so directories
are skipped before they are ever descended into.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Directories never worth descending into. Checked by exact name before recursing.
PRUNE_DIRS = frozenset(
    {
        ".git", ".hg", ".svn",
        ".venv", "venv", "env", ".env", "virtualenv",
        "site-packages", "dist-packages", "node_modules",
        "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox", ".nox",
        ".idea", ".vscode-test", ".gradle", ".terraform",
        "dist", "build", ".eggs", "htmlcov", ".coverage",
        ".sonarlint", ".scannerwork", ".next", ".cache",
    }
)

# Language ids the SonarLint server understands, keyed by file suffix. Only extensions
# whose analyzer we can actually supply are listed.
LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ipynb": "jupyter",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".php": "php",
    ".go": "go",
    ".java": "java",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".tf": "terraform",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".docker": "dockerfile",
}

# Analyzer jar needed per language id, so we load only what the run requires.
ANALYZER_FOR_LANGUAGE: dict[str, str] = {
    "python": "sonarpython",
    "jupyter": "sonarpython",
    "javascript": "sonarjs",
    "javascriptreact": "sonarjs",
    "typescript": "sonarjs",
    "typescriptreact": "sonarjs",
    "css": "sonarjs",
    "scss": "sonarjs",
    "html": "sonarhtml",
    "php": "sonarphp",
    "go": "sonargo",
    "java": "sonarjava",
    "xml": "sonarxml",
    "yaml": "sonariac",
    "json": "sonariac",
    "terraform": "sonariac",
    "dockerfile": "sonariac",
    "shellscript": "sonartext",
}

MAX_BYTES = 3_000_000  # the analyzers themselves skip very large files


@dataclass(frozen=True)
class SourceFile:
    path: Path
    language: str

    @property
    def analyzer(self) -> str | None:
        return ANALYZER_FOR_LANGUAGE.get(self.language)


def _to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a Sonar-style path glob.

    Sonar uses ** for any number of directories and * within a segment, which
    fnmatch cannot express (its * crosses separators). Translate explicitly.
    """
    p = pattern.strip().replace("\\", "/")
    if not p:
        # An empty pattern must match nothing. "\b\B" is a contradiction by
        # construction (a position cannot be both a word boundary and not one).
        return re.compile(r"\b\B")
    if p.startswith("/"):
        p = p[1:]
    out: list[str] = []
    i = 0
    while i < len(p):
        ch = p[i]
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE if os.name == "nt" else 0)


class Matcher:
    """Path exclusion test over Sonar-style globs, relative to a root."""

    def __init__(self, patterns: Iterable[str], root: Path) -> None:
        self.root = root
        self._regexes = [_to_regex(p) for p in patterns if p and p.strip()]

    def excluded(self, path: Path) -> bool:
        if not self._regexes:
            return False
        try:
            rel = path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            rel = path.as_posix()
        candidates = (rel, path.name)
        return any(rx.match(c) for rx in self._regexes for c in candidates)


def _iter_files(start: Path, matcher: Matcher, follow_links: bool = False) -> Iterator[Path]:
    """Walk `start`, pruning uninteresting directories before descending."""
    if start.is_file():
        yield start
        return
    seen_dirs: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(start, followlinks=follow_links):
        here = Path(dirpath)

        # Guard against symlink and junction loops, common on Windows.
        try:
            st = here.stat()
            key = (st.st_dev, st.st_ino)
            if key in seen_dirs:
                dirnames[:] = []
                continue
            seen_dirs.add(key)
        except OSError:
            dirnames[:] = []
            continue

        # Prune in place so os.walk never descends. This is the whole point.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in PRUNE_DIRS
            and not d.startswith(".")
            and not matcher.excluded(here / d)
        ]

        for name in filenames:
            yield here / name


def collect(
    targets: Iterable[Path],
    *,
    root: Path,
    exclusions: Iterable[str] = (),
    languages: Iterable[str] | None = None,
    max_bytes: int = MAX_BYTES,
) -> tuple[list[SourceFile], list[str]]:
    """Resolve targets into analyzable files.

    Returns the files plus notes about anything deliberately skipped, because a
    silently reduced file set reads as "clean" when it is not.
    """
    matcher = Matcher(exclusions, root)
    wanted = set(languages) if languages else None
    seen: set[Path] = set()
    files: list[SourceFile] = []
    notes: list[str] = []
    skipped_large = 0
    skipped_excluded = 0

    for target in targets:
        target = target.resolve()
        if not target.exists():
            notes.append(f"skipped {target}: does not exist")
            continue
        for path in _iter_files(target, matcher):
            if path in seen:
                continue
            language = LANGUAGES.get(path.suffix.lower())
            if language is None:
                continue  # not a language we can analyze
            if wanted is not None and language not in wanted:
                continue
            if matcher.excluded(path):
                skipped_excluded += 1
                continue
            try:
                if path.stat().st_size > max_bytes:
                    skipped_large += 1
                    continue
            except OSError:
                continue
            seen.add(path)
            files.append(SourceFile(path=path, language=language))

    if skipped_large:
        notes.append(f"skipped {skipped_large} file(s) larger than {max_bytes // 1000}kB")
    if skipped_excluded:
        notes.append(f"excluded {skipped_excluded} file(s) by configured exclusions")

    files.sort(key=lambda f: str(f.path))
    return files, notes


def read_text(path: Path) -> str | None:
    """Read a source file, trying the encodings analyzers assume."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return None
    return None


def analyzers_for(files: Iterable[SourceFile]) -> set[str]:
    """The minimal analyzer set covering these files."""
    return {a for f in files if (a := f.analyzer)}
