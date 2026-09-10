"""URI encoding, message framing, and the server-to-client replies.

These cover the two bugs that produced a confident but wrong "no issues" result:
unencoded spaces in file URIs, and answering sonarlint/* requests with a generic
empty object.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pysonarlint.lsp import (
    _ANALYSIS_DONE_RE,
    Diagnostic,
    LanguageServer,
    LspError,
    _from_uri,
    _uri,
)


def test_uri_percent_encodes_spaces() -> None:
    """A raw space made the server reject initialize with an opaque internal error."""
    uri = _uri(Path("C:/Users/x/My Documents/project"))
    assert " " not in uri
    assert "%20" in uri


def test_uri_keeps_drive_colon_unencoded() -> None:
    uri = _uri(Path("C:/tmp/x.py"))
    assert "C:/" in uri
    assert "%3A" not in uri


def test_uri_round_trip_with_space(tmp_path: Path) -> None:
    original = tmp_path / "a dir with spaces" / "file.py"
    original.parent.mkdir(parents=True)
    original.write_text("x = 1\n")
    assert _from_uri(_uri(original)) == original.resolve()


def test_uri_round_trip_with_unicode(tmp_path: Path) -> None:
    original = tmp_path / "caf\u00e9" / "na\u00efve.py"
    original.parent.mkdir(parents=True)
    original.write_text("x = 1\n")
    assert _from_uri(_uri(original)) == original.resolve()


@pytest.mark.parametrize(
    "message",
    [
        "Analysis detected 5 issues and 0 Security Hotspots in 1309ms",
        "Analysis detected 1 issue and 2 Security Hotspots in 42ms",
        "[Info - 00:00:00.000] [sonarlint] Analysis detected 0 issues and 0 Security Hotspots in 5ms",
    ],
)
def test_completion_marker_matches(message: str) -> None:
    assert _ANALYSIS_DONE_RE.search(message)


def test_completion_marker_ignores_unrelated_logs() -> None:
    assert not _ANALYSIS_DONE_RE.search("Starting analysis with configuration")
    assert not _ANALYSIS_DONE_RE.search("1 file indexed")


def _server() -> LanguageServer:
    return LanguageServer(Path("java"), Path("ls.jar"), ())


def test_is_open_in_editor_must_be_true() -> None:
    """Returning anything else makes the server discard every diagnostic."""
    assert _server()._reply("sonarlint/isOpenInEditor", {}) is True


def test_ssl_confirmation_declines_by_default() -> None:
    """Declining only blocks optional downloads; it never trusts an unknown cert."""
    assert _server()._reply("sonarlint/askSslCertificateConfirmation", {}) is False


def test_should_analyse_file_is_affirmative() -> None:
    reply = _server()._reply("sonarlint/shouldAnalyseFile", {})
    assert reply == {"shouldBeAnalysed": True}


def test_configuration_request_without_items_still_answers() -> None:
    server = _server()
    server.settings = {"output": {"showAnalyzerLogs": False}}
    assert server._reply("workspace/configuration", {}) == [server.settings]


def test_token_request_accepts_either_id_spelling() -> None:
    """The field has been serverId and connectionId across versions."""
    server = _server()
    seen: list[str] = []
    server.token_provider = lambda ident: seen.append(ident) or "squ_x"  # type: ignore[func-returns-value]
    assert server._reply("sonarlint/getTokenForServer", {"serverId": "a"}) == "squ_x"
    assert server._reply("sonarlint/getTokenForServer", {"connectionId": "b"}) == "squ_x"
    assert seen == ["a", "b"]


def test_token_request_without_a_provider_is_none() -> None:
    assert _server()._reply("sonarlint/getTokenForServer", {"serverId": "a"}) is None


def test_unknown_request_returns_none_not_empty_dict() -> None:
    assert _server()._reply("sonarlint/somethingNew", {}) is None


def test_list_files_in_folder(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    server = _server()
    found = server._reply("sonarlint/listFilesInFolder", {"folderUri": _uri(tmp_path)})
    names = {f["fileName"] for f in found["foundFiles"]}
    assert names == {"a.py"}  # directories are excluded


def test_list_files_in_missing_folder_is_empty() -> None:
    reply = _server()._reply("sonarlint/listFilesInFolder", {"folderUri": "file:///no/such/dir"})
    assert reply == {"foundFiles": []}


def test_content_length_parsing() -> None:
    assert LanguageServer._content_length(b"Content-Length: 42") == 42
    assert LanguageServer._content_length(b"content-length: 7\r\nX: y") == 7
    assert LanguageServer._content_length(b"Content-Type: application/json") is None
    assert LanguageServer._content_length(b"Content-Length: abc") is None


def test_diagnostic_severity_names() -> None:
    def make(severity: int) -> Diagnostic:
        return Diagnostic(
            path=Path("x.py"),
            line=1,
            column=1,
            end_line=1,
            end_column=2,
            rule="python:S1",
            message="m",
            severity=severity,
        )

    assert make(1).severity_name == "error"
    assert make(2).severity_name == "warning"
    assert make(3).severity_name == "info"
    assert make(4).severity_name == "hint"


@pytest.mark.parametrize(
    "message",
    [
        "[Warn] Could not retrieve connected analysis configuration, falling back to standalone configuration",
        "[Error] No token for connection https-sonar-example-com",
        "[Debug] No connections configured, skipping binding suggestions.",
    ],
)
def test_degradation_messages_are_detected(message: str) -> None:
    """A run the server downgraded to standalone must never be labelled connected."""
    from pysonarlint.lsp import _DEGRADED_PATTERNS

    assert any(p in message for p in _DEGRADED_PATTERNS)


def test_normal_logs_are_not_treated_as_degradation() -> None:
    from pysonarlint.lsp import _DEGRADED_PATTERNS

    for benign in ("Starting analysis with configuration", "1 file indexed", "Index files"):
        assert not any(p in benign for p in _DEGRADED_PATTERNS)


def test_configuration_reply_is_positionally_aligned() -> None:
    """The server asks for named sections; only `sonarlint` carries our settings and
    the reply must line up with the requested items or the binding is not applied."""
    server = _server()
    server.settings = {"connectedMode": {"project": {"projectKey": "p"}}}
    reply = server._reply(
        "workspace/configuration",
        {"items": [{"section": "sonarlint"}, {"section": "omnisharp.useModernNet"}]},
    )
    assert len(reply) == 2
    assert reply[0] == server.settings
    assert reply[1] is None


def test_files_exclude_section_gets_an_object() -> None:
    assert _server()._reply(
        "workspace/configuration", {"items": [{"section": "files.exclude"}]}
    ) == [{}]


def test_stop_swallows_a_broken_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JVM that has already exited must not turn a successful run into a failure.

    stop() runs in language_server()'s finally, so an LspError escaping it reaches
    analyze()'s handler after every issue has been collected, and the CLI reports
    exit 2 (tool failure) instead of 1 (issues found).
    """
    server = _server()

    class _Proc:
        stdin = None
        stdout = None
        stderr = None
        killed = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            type(self).killed = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

    server._proc = _Proc()

    def _broken(_payload: object) -> None:
        raise LspError("language server closed its input: broken pipe")

    monkeypatch.setattr(server, "_write", _broken)
    server.stop()  # must not raise
    assert _Proc.killed
