"""pysonarlint: the issues SonarQube for IDE would highlight, from your terminal."""

from __future__ import annotations

__all__ = ["__version__", "main"]


def _detect_version() -> str:
    """Read the installed distribution's version.

    Single source of truth is pyproject.toml, so `uv version --bump` cannot leave a
    hardcoded copy behind to disagree with the published package.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("pysonarlint")
    except PackageNotFoundError:
        return "0.0.0+dev"  # running from a source checkout


__version__ = _detect_version()


def main() -> int:
    """Console entry point."""
    from .cli import main as _main

    return _main()
