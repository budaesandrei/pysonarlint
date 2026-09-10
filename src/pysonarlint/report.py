"""Render results for humans, agents, and CI.

The json format is the contract for LLM agents: stable keys, absolute and relative
paths, and enough context per issue to act on it without re-reading the file.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from .analyze import Result
from .lsp import Diagnostic

# Severity ordering for thresholds and sorting.
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "hint": 3}

_COLORS = {
    "error": "\033[31m",
    "warning": "\033[33m",
    "info": "\033[36m",
    "hint": "\033[90m",
}
_DIM = "\033[90m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _supports_color(stream) -> bool:  # noqa: ANN001
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    import os

    if os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        # Enable VT processing; Windows Terminal and modern conhost support it.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:  # noqa: BLE001
            return False
    return True


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def filter_issues(issues: Iterable[Diagnostic], min_severity: str | None) -> list[Diagnostic]:
    """Keep issues at or above a severity floor."""
    items = list(issues)
    if not min_severity:
        return items
    ceiling = SEVERITY_ORDER.get(min_severity.lower())
    if ceiling is None:
        return items
    return [i for i in items if SEVERITY_ORDER.get(i.severity_name, 1) <= ceiling]


def render_text(result: Result, root: Path, *, stream=None, show_notes: bool = True) -> str:  # noqa: ANN001
    """Human-readable grouped output."""
    stream = stream or sys.stdout
    color = _supports_color(stream)

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    lines: list[str] = _grouped_lines(result.issues, root, paint)

    if result.issues:
        lines.append(paint(_summary_line(result), _BOLD))
    else:
        lines.append(
            paint(f"No issues in {len(result.analyzed)} file(s)", "\033[32m" if color else "")
        )

    mode = "connected" if result.connected else "standalone"
    lines.append(
        paint(f"{mode} mode, engine {result.engine_version}, {result.duration:.1f}s", _DIM)
    )

    if show_notes:
        lines.extend(paint(f"note: {note}", _COLORS["info"]) for note in _safe_notes(result))
    return "\n".join(lines)


def _grouped_lines(
    issues: list[Diagnostic], root: Path, paint: Callable[[str, str], str]
) -> list[str]:
    """Issues grouped under a heading per file, files and issues both in order."""
    by_file: dict[Path, list[Diagnostic]] = {}
    for issue in issues:
        by_file.setdefault(issue.path, []).append(issue)

    lines: list[str] = []
    for path in sorted(by_file, key=lambda p: _rel(p, root)):
        items = sorted(by_file[path], key=lambda i: (i.line, i.column))
        lines.append(paint(_rel(path, root), _BOLD))
        lines.extend(_issue_lines(items, paint))
        lines.append("")
    return lines


def _issue_lines(items: list[Diagnostic], paint: Callable[[str, str], str]) -> list[str]:
    """One rendered line per issue, already grouped under a file heading."""
    lines: list[str] = []
    for issue in items:
        loc = f"{issue.line}:{issue.column}"
        sev = issue.severity_name
        lines.append(
            f"  {paint(loc.rjust(8), _DIM)}  "
            f"{paint(sev.ljust(7), _COLORS.get(sev, ''))}  "
            f"{issue.message}  {paint(issue.rule, _DIM)}"
        )
    return lines


def _summary_line(result: Result) -> str:
    """The "N issues in X of Y files (breakdown)" tail line."""
    total = len(result.issues)
    counts = _severity_counts(result.issues)
    breakdown = ", ".join(
        f"{counts[s]} {s}" for s in ("error", "warning", "info", "hint") if s in counts
    )
    return (
        f"{total} issue{'s' if total != 1 else ''} in "
        f"{result.files_with_issues} of {len(result.analyzed)} file"
        f"{'s' if len(result.analyzed) != 1 else ''} ({breakdown})"
    )


def render_json(result: Result, root: Path) -> str:
    """Machine-readable output. This is the agent-facing contract."""
    payload = {
        "version": 1,
        "tool": "pysonarlint",
        "mode": "connected" if result.connected else "standalone",
        "engineVersion": result.engine_version,
        "root": str(root),
        "summary": {
            "issues": len(result.issues),
            "filesAnalyzed": len(result.analyzed),
            "filesWithIssues": result.files_with_issues,
            "durationSeconds": round(result.duration, 3),
            "incomplete": result.incomplete,
            "degradedFromConnected": result.degraded_from_connected,
            "bySeverity": _severity_counts(result.issues),
        },
        "issues": [
            {
                "file": _rel(issue.path, root),
                "absolutePath": str(issue.path),
                "line": issue.line,
                "column": issue.column,
                "endLine": issue.end_line,
                "endColumn": issue.end_column,
                "rule": issue.rule,
                "severity": issue.severity_name,
                "message": issue.message,
                "ruleUrl": _rule_url(issue.rule),
            }
            for issue in result.issues
        ],
        "notes": _safe_notes(result),
    }
    return json.dumps(payload, indent=2)


def render_sarif(result: Result, root: Path) -> str:
    """SARIF 2.1.0, for GitHub code scanning and other standard consumers."""
    rules: dict[str, dict[str, object]] = {}
    for issue in result.issues:
        if issue.rule and issue.rule not in rules:
            entry: dict[str, object] = {
                "id": issue.rule,
                "shortDescription": {"text": issue.message[:120]},
            }
            if url := _rule_url(issue.rule):
                entry["helpUri"] = url
            rules[issue.rule] = entry

    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pysonarlint",
                        "informationUri": "https://github.com/budaesandrei/pysonarlint",
                        "version": result.engine_version,
                        "rules": list(rules.values()),
                    }
                },
                "results": [
                    {
                        "ruleId": issue.rule,
                        "level": _sarif_level(issue.severity_name),
                        "message": {"text": issue.message},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {
                                        "uri": _rel(issue.path, root),
                                        "uriBaseId": "%SRCROOT%",
                                    },
                                    "region": {
                                        "startLine": issue.line,
                                        "startColumn": issue.column,
                                        "endLine": issue.end_line,
                                        "endColumn": issue.end_column,
                                    },
                                }
                            }
                        ],
                    }
                    for issue in result.issues
                ],
            }
        ],
    }
    return json.dumps(sarif, indent=2)


def render_github(result: Result, root: Path) -> str:
    """GitHub Actions workflow commands, so issues annotate the diff."""
    out: list[str] = []
    for issue in result.issues:
        level = {"error": "error", "warning": "warning"}.get(issue.severity_name, "notice")
        message = issue.message.replace("\n", " ")
        out.append(
            f"::{level} file={_rel(issue.path, root)},line={issue.line},"
            f"col={issue.column},title={issue.rule}::{message}"
        )
    return "\n".join(out)


def _severity_counts(issues: Iterable[Diagnostic]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.severity_name] = counts.get(issue.severity_name, 0) + 1
    return counts


def _sarif_level(severity: str) -> str:
    return {"error": "error", "warning": "warning", "info": "note", "hint": "note"}.get(
        severity, "warning"
    )


def _safe_notes(result: Result) -> list[str]:
    """Notes with any secret scrubbed.

    Renderers must not rely on the caller having redacted first: notes quote server
    messages, and these formats end up in bug reports and code-scanning uploads.

    Known secrets are removed by exact match, and anything shaped like a Sonar token
    is removed by pattern as a backstop, so a value that never reached `secrets`
    still cannot be published.
    """
    return [_scrub_tokens(redact(n, *result.secrets)) for n in result.notes]


# Sonar token prefixes: squ_ (user), sqa_ (global analysis), sqp_ (project analysis).
_TOKEN_RE = re.compile(r"\bsq[uap]_[A-Za-z0-9]{8,}")


def _scrub_tokens(text: str) -> str:
    return _TOKEN_RE.sub("<redacted>", text)


def redact(text: str, *secrets: str | None) -> str:
    """Replace secret values with a placeholder.

    Applied to anything user-visible that could have passed near a credential. Short
    strings are ignored so a value like "x" cannot blank out unrelated output.
    """
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "<redacted>")
    return out


def _rule_url(rule: str) -> str | None:
    """Link to the rule description on the public rules site."""
    if ":" not in rule:
        return None
    repo, _, key = rule.partition(":")
    if not key.startswith("S"):
        return None
    return f"https://rules.sonarsource.com/{repo}/RSPEC-{key[1:]}/"


FORMATTERS = {
    "text": render_text,
    "json": render_json,
    "sarif": render_sarif,
    "github": render_github,
}
