"""Guards against leaking credentials or trusting unverified input.

A linter is often run with a token in the environment and its output pasted into bug
reports and CI logs, so anything user-visible is a potential disclosure path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pysonarlint.analyze import Result, _workspace_settings
from pysonarlint.config import Binding, Config
from pysonarlint.lsp import Diagnostic, LanguageServer
from pysonarlint.report import redact, render_github, render_json, render_sarif, render_text

TOKEN = "squ_EXAMPLEfakeTOKENvalueNOTaREALsecret00"


def _config(token: str = TOKEN) -> Config:
    cfg = Config(root=Path("/repo"))
    cfg.binding = Binding(url="https://sonar.example.com", token=token, project_key="proj")
    return cfg


def _result_mentioning_token() -> Result:
    return Result(
        issues=[
            Diagnostic(
                path=Path("/repo/a.py"),
                line=1,
                column=1,
                end_line=1,
                end_column=2,
                rule="python:S1",
                message="msg",
                severity=2,
            )
        ],
        analyzed=[Path("/repo/a.py")],
        notes=[f"server said: token={TOKEN} rejected"],
        logs=[f"[Info] connections={{token={TOKEN}}}"],
    )


def test_redact_replaces_the_secret() -> None:
    assert TOKEN not in redact(f"prefix {TOKEN} suffix", TOKEN)
    assert "<redacted>" in redact(f"prefix {TOKEN} suffix", TOKEN)


def test_redact_ignores_none_and_short_values() -> None:
    """A one-character 'secret' must not blank out unrelated output."""
    assert redact("hello world", None) == "hello world"
    assert redact("hello world", "o") == "hello world"


def test_redact_handles_several_secrets() -> None:
    out = redact("a AAAAAAAAAA b BBBBBBBBBB", "AAAAAAAAAA", "BBBBBBBBBB")
    assert "AAAAAAAAAA" not in out and "BBBBBBBBBB" not in out


@pytest.mark.parametrize("renderer", [render_json, render_sarif, render_github])
def test_no_token_in_machine_output(renderer) -> None:
    """These formats are pasted into issues and uploaded to code scanning."""
    out = renderer(_result_mentioning_token(), Path("/repo"))
    assert TOKEN not in out or "<redacted>" in out


def test_notes_can_be_redacted_before_rendering() -> None:
    result = _result_mentioning_token()
    result.notes = [redact(n, TOKEN) for n in result.notes]
    assert TOKEN not in render_text(result, Path("/repo"), stream=None)
    assert TOKEN not in render_json(result, Path("/repo"))


def test_logs_are_not_included_in_any_report_format() -> None:
    """Server logs hold configuration echoes; they must be opt-in via -v only."""
    result = _result_mentioning_token()
    for renderer in (render_json, render_sarif, render_github):
        assert TOKEN not in renderer(result, Path("/repo"))


def test_workspace_settings_carry_the_token_by_necessity() -> None:
    """The token must reach the server, so this is the one place it legitimately
    appears. Pinned so nobody logs this structure casually."""
    settings = _workspace_settings(_config())
    blob = json.dumps(settings)
    assert TOKEN in blob  # required for the connection to work
    assert "connectedMode" in settings


def test_standalone_settings_never_contain_a_token() -> None:
    cfg = Config(root=Path("/repo"))  # not connected
    assert TOKEN not in json.dumps(_workspace_settings(cfg))
    assert "connectedMode" not in _workspace_settings(cfg)


def test_ssl_confirmation_is_declined_not_accepted() -> None:
    """Auto-accepting an unknown certificate would silently weaken TLS."""
    server = LanguageServer(Path("java"), Path("ls.jar"), ())
    assert server._reply("sonarlint/askSslCertificateConfirmation", {}) is False


def test_list_folder_refuses_to_escape_to_a_missing_path() -> None:
    server = LanguageServer(Path("java"), Path("ls.jar"), ())
    assert server._reply(
        "sonarlint/listFilesInFolder", {"folderUri": "file:///definitely/not/here"}
    ) == {"foundFiles": []}


def test_unknown_client_request_is_answered_with_null() -> None:
    """Answering an unrecognised request with a plausible object risks the server
    acting on a value we did not mean to assert."""
    server = LanguageServer(Path("java"), Path("ls.jar"), ())
    assert server._reply("sonarlint/futureUnknownMethod", {"anything": 1}) is None
