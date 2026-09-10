"""Locate the SonarSource analysis engine: a JRE, the language server, and analyzer jars.

The engine is not redistributable (sonar-python is SSALv1) and the jars exceed PyPI's
100 MiB per-file cap, so we never vendor it. We find an existing SonarQube-for-IDE
install instead, which also guarantees byte-identical results to what the IDE shows.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MIN_JAVA = 17

# Editors that ship the SonarQube for IDE extension, as <home>/<dir>/extensions.
_EDITOR_DIRS = (
    ".vscode",
    ".vscode-insiders",
    ".vscode-server",
    ".kiro",
    ".cursor",
    ".windsurf",
    ".vscodium",
)

_EXT_GLOB = "sonarsource.sonarlint-vscode-*"
_VERSION_RE = re.compile(r"sonarlint-vscode-(\d+(?:\.\d+)*)")


class EngineNotFound(RuntimeError):
    """No usable SonarLint installation could be located."""


@dataclass(frozen=True)
class Engine:
    """A resolved, runnable analysis engine."""

    java: Path
    server_jar: Path
    analyzers: tuple[Path, ...]
    root: Path
    version: str

    def analyzer(self, name: str) -> Path | None:
        return next((a for a in self.analyzers if a.stem == name), None)


def _version_key(path: Path) -> tuple[int, ...]:
    m = _VERSION_RE.search(path.name)
    return tuple(int(p) for p in m.group(1).split(".")) if m else (0,)


def _extension_roots() -> list[Path]:
    """Every SonarLint extension dir on this machine, newest version first."""
    home = Path.home()
    found: list[Path] = []
    for editor in _EDITOR_DIRS:
        ext_dir = home / editor / "extensions"
        if not ext_dir.is_dir():
            continue
        found.extend(p for p in ext_dir.glob(_EXT_GLOB) if p.is_dir())
    return sorted(found, key=_version_key, reverse=True)


def _bundled_java(root: Path) -> Path | None:
    """The extension's own JRE. Its directory name confusingly ends in '.tar'."""
    exe = "java.exe" if os.name == "nt" else "java"
    return next((p for p in (root / "jre").glob(f"*/bin/{exe}") if p.is_file()), None)


def _java_major(java: Path) -> int | None:
    """Parse the major version, or None if the binary is a stub that won't run.

    Java prints its version banner to stderr. A stub JDK directory (present on some
    corporate images) has the layout but no working binary, so this must actually
    execute rather than trust the path.
    """
    try:
        out = subprocess.run(
            [str(java), "-version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'version "(\d+)', out.stderr + out.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return major if major > 1 else 8  # 1.8.x -> 8


def _candidate_javas(root: Path | None) -> list[Path]:
    out: list[Path] = []
    if override := os.environ.get("PYSONARLINT_JAVA"):
        out.append(Path(override))
    if root and (bundled := _bundled_java(root)):
        out.append(bundled)
    if java_home := os.environ.get("JAVA_HOME"):
        exe = "java.exe" if os.name == "nt" else "java"
        out.append(Path(java_home) / "bin" / exe)
    if which := shutil.which("java"):
        out.append(Path(which))
    return out


def find_java(root: Path | None = None) -> Path:
    """First JRE that actually runs and is new enough."""
    tried: list[str] = []
    for cand in _candidate_javas(root):
        if not cand.is_file():
            continue
        major = _java_major(cand)
        if major is None:
            tried.append(f"{cand} (did not run)")
        elif major < MIN_JAVA:
            tried.append(f"{cand} (Java {major}, need {MIN_JAVA}+)")
        else:
            return cand
    detail = "; ".join(tried) or "none found"
    raise EngineNotFound(
        f"No usable Java {MIN_JAVA}+ runtime. Tried: {detail}. "
        "Install the SonarQube for IDE extension (ships its own JRE), "
        "or set PYSONARLINT_JAVA to a java executable."
    )


def discover(explicit_root: Path | None = None) -> Engine:
    """Resolve an engine, preferring an explicit root then the newest install."""
    roots = [explicit_root] if explicit_root else _extension_roots()
    if env_root := os.environ.get("PYSONARLINT_HOME"):
        roots.insert(0, Path(env_root))

    problems: list[str] = []
    for root in roots:
        if not root or not root.is_dir():
            problems.append(f"{root}: not a directory")
            continue
        server = root / "server" / "sonarlint-ls.jar"
        if not server.is_file():
            problems.append(f"{root}: no server/sonarlint-ls.jar")
            continue
        analyzers = tuple(sorted((root / "analyzers").glob("*.jar")))
        if not analyzers:
            problems.append(f"{root}: no analyzers/*.jar")
            continue
        m = _VERSION_RE.search(root.name)
        return Engine(
            java=find_java(root),
            server_jar=server,
            analyzers=analyzers,
            root=root,
            version=m.group(1) if m else "unknown",
        )

    hint = "\n  ".join(problems) if problems else "no extension directories exist"
    raise EngineNotFound(
        "Could not find a SonarQube for IDE installation.\n  "
        + hint
        + "\n\nInstall the 'SonarQube for IDE' extension in VS Code (or Cursor, Kiro, "
        "Windsurf, VSCodium), or point PYSONARLINT_HOME at an existing extension "
        "directory. pysonarlint reuses its analyzers so results match your IDE exactly."
    )
