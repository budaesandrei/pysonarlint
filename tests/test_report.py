"""Output formats and exit-code thresholds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pysonarlint.analyze import Result
from pysonarlint.lsp import Diagnostic
from pysonarlint.report import (
    filter_issues,
    render_github,
    render_json,
    render_sarif,
    render_text,
)


def _issue(line: int = 1, severity: int = 2, rule: str = "python:S1481") -> Diagnostic:
    return Diagnostic(
        path=Path("/repo/app/main.py"),
        line=line,
        column=5,
        end_line=line,
        end_column=9,
        rule=rule,
        message='Remove the unused local variable "x".',
        severity=severity,
    )


def _result(*issues: Diagnostic) -> Result:
    return Result(
        issues=list(issues),
        analyzed=[Path("/repo/app/main.py")],
        engine_version="5.9.1",
        duration=1.5,
    )


ROOT = Path("/repo")


def test_json_is_valid_and_stable() -> None:
    payload = json.loads(render_json(_result(_issue()), ROOT))
    assert payload["version"] == 1
    assert payload["tool"] == "pysonarlint"
    assert payload["mode"] == "standalone"
    assert payload["summary"]["issues"] == 1
    entry = payload["issues"][0]
    assert entry["file"] == "app/main.py"
    assert entry["rule"] == "python:S1481"
    assert entry["severity"] == "warning"
    assert entry["line"] == 1


def test_json_includes_rule_url_for_agents() -> None:
    payload = json.loads(render_json(_result(_issue()), ROOT))
    assert payload["issues"][0]["ruleUrl"] == "https://rules.sonarsource.com/python/RSPEC-1481/"


def test_json_reports_incomplete_runs() -> None:
    result = _result()
    result.incomplete = True
    payload = json.loads(render_json(result, ROOT))
    assert payload["summary"]["incomplete"] is True


def test_json_empty_result_is_still_valid() -> None:
    payload = json.loads(render_json(_result(), ROOT))
    assert payload["issues"] == []
    assert payload["summary"]["issues"] == 0


def test_sarif_shape() -> None:
    doc = json.loads(render_sarif(_result(_issue()), ROOT))
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "pysonarlint"
    assert run["tool"]["driver"]["rules"][0]["id"] == "python:S1481"
    location = run["results"][0]["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "app/main.py"
    assert location["region"]["startLine"] == 1


def test_sarif_levels_map_from_severity() -> None:
    doc = json.loads(render_sarif(_result(_issue(severity=1), _issue(line=2, severity=3)), ROOT))
    levels = [r["level"] for r in doc["runs"][0]["results"]]
    assert levels == ["error", "note"]


def test_github_annotations() -> None:
    out = render_github(_result(_issue()), ROOT)
    assert out.startswith("::warning file=app/main.py,line=1,col=5,title=python:S1481::")


def test_github_annotation_is_single_line() -> None:
    issue = _issue()
    issue.message = "first line\nsecond line"
    assert "\n" not in render_github(_result(issue), ROOT)


def test_text_output_mentions_rule_and_location() -> None:
    out = render_text(_result(_issue()), ROOT, stream=None)
    assert "app/main.py" in out
    assert "python:S1481" in out
    assert "1:5" in out


def test_text_output_when_clean() -> None:
    out = render_text(_result(), ROOT, stream=None)
    assert "No issues" in out


@pytest.mark.parametrize(
    ("threshold", "kept"),
    [
        ("error", 1),
        ("warning", 2),
        ("info", 3),
        ("hint", 4),
        (None, 4),
    ],
)
def test_severity_threshold_is_inclusive(threshold: str | None, kept: int) -> None:
    issues = [_issue(line=i, severity=i) for i in (1, 2, 3, 4)]
    assert len(filter_issues(issues, threshold)) == kept


def test_unknown_threshold_keeps_everything() -> None:
    issues = [_issue()]
    assert len(filter_issues(issues, "nonsense")) == 1


def test_json_flags_degraded_connected_runs() -> None:
    """Agents need to know the rule set was local, even though results are valid."""
    result = _result(_issue())
    result.degraded_from_connected = True
    payload = json.loads(render_json(result, ROOT))
    assert payload["summary"]["degradedFromConnected"] is True
    assert payload["mode"] == "standalone"
    assert payload["summary"]["issues"] == 1  # results are still reported


def test_json_normal_run_is_not_degraded() -> None:
    payload = json.loads(render_json(_result(_issue()), ROOT))
    assert payload["summary"]["degradedFromConnected"] is False
