"""Framing, dispatch, and diagnostic normalisation, with no subprocess and no JVM.

The transport is exercised by feeding bytes through the frame splitter, messages through
`_dispatch`, and canned streams through the reader loops. Only `start()` needs a Popen,
and there it is replaced with a fake so the launch arguments can still be asserted.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from queue import Queue
from typing import Any

import pytest

from pysonarlint.lsp import (
    Diagnostic,
    LanguageServer,
    LspError,
    _from_uri,
    _uri,
    language_server,
)


def _server(analyzers: tuple[Path, ...] = ()) -> LanguageServer:
    return LanguageServer(Path("java"), Path("ls.jar"), analyzers)


class FakeStdin:
    def __init__(self, *, fail: bool = False) -> None:
        self.buf = b""
        self.flushes = 0
        self.closed = False
        self._fail = fail

    def write(self, data: bytes) -> int:
        if self._fail:
            raise OSError("broken pipe")
        self.buf += data
        return len(data)

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closed = True


class FakeProc:
    """A Popen stand-in. No process is ever created."""

    def __init__(self, *, alive: bool = True, stdin: FakeStdin | None = None) -> None:
        self.stdin = stdin if stdin is not None else FakeStdin()
        self.stdout = None
        self.stderr = None
        self._alive = alive
        self.killed = False
        self.waits: list[float | None] = []

    def poll(self) -> int | None:
        return None if self._alive else 0

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        self._alive = False
        return 0


# -- the command line ----------------------------------------------------


def test_the_command_names_the_jar_and_stdio() -> None:
    cmd = _server()._cmd
    assert cmd[0] == "java"
    assert "-jar" in cmd
    assert cmd[cmd.index("-jar") + 1] == "ls.jar"
    assert "-stdio" in cmd


def test_the_jvm_is_tuned_for_a_short_single_shot_run() -> None:
    cmd = _server()._cmd
    assert "-Djava.awt.headless=true" in cmd
    assert "-XX:+UseSerialGC" in cmd  # avoids G1's startup cost


def test_only_the_needed_analyzers_are_passed(tmp_path: Path) -> None:
    """Passing the full set makes the server fetch optional extras over the network,
    which fails noisily behind a TLS-inspecting proxy.

    The paths are compared via str(Path) so the assertion holds on both separators.
    """
    jars = (tmp_path / "sonarpython.jar", tmp_path / "sonarjs.jar")
    flags = [a for a in _server(jars)._cmd if a.startswith("-analyzers=")]
    assert flags == [f"-analyzers={jars[0]}", f"-analyzers={jars[1]}"]
    assert [f.rsplit("=", 1)[1] for f in flags] == [str(jars[0]), str(jars[1])]


def test_no_analyzer_flag_when_there_are_none() -> None:
    assert not [a for a in _server()._cmd if a.startswith("-analyzers=")]


# -- _uri / _from_uri ----------------------------------------------------


class _ResolvedAs:
    """A path stand-in with a fixed resolved posix form.

    `_uri` only ever needs `.resolve().as_posix()`. A real `Path("C:/repo/a.py")` will
    not do for the Windows cases: on POSIX that string is a *relative* path, so
    `.resolve()` prepends the current directory and the drive-letter form under test is
    never produced. Fixing the resolved form keeps the assertion about `_uri` rather
    than about the machine the tests happen to run on.
    """

    def __init__(self, posix: str) -> None:
        self._posix = posix

    def resolve(self) -> _ResolvedAs:
        return self

    def as_posix(self) -> str:
        return self._posix


def test_uri_starts_with_the_file_scheme() -> None:
    assert _uri(Path("/srv/x.py")).startswith("file://")


def test_uri_encodes_every_character_that_needs_it() -> None:
    uri = _uri(_ResolvedAs("C:/a b/c#d/e?f.py"))
    assert " " not in uri
    assert "#" not in uri
    assert "?" not in uri


def test_from_uri_decodes_percent_escapes() -> None:
    assert _from_uri("file:///srv/a%20b/c.py").as_posix().endswith("/srv/a b/c.py")


def test_from_uri_round_trips_a_plain_path(tmp_path: Path) -> None:
    path = tmp_path / "plain.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert _from_uri(_uri(path)) == path.resolve()


class _FakeOsName:
    """`os` reporting a different `name`, proxying everything else.

    Patching the real `os.name` is not an option: `pathlib` reads it when a Path is
    constructed, so a POSIX-flavoured `os.name` on Windows makes every `Path(...)` raise,
    including inside pytest's own reporting.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str) -> object:
        import os as _os

        return getattr(_os, attr)


def test_the_posix_uri_form_has_exactly_two_slashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other platform's branch, which CI also covers natively on ubuntu and macos."""
    monkeypatch.setattr("pysonarlint.lsp.os", _FakeOsName("posix"))
    uri = _uri(_ResolvedAs("/repo/a.py"))
    assert uri.startswith("file:///repo/")
    assert not uri.startswith("file:////")


def test_the_windows_uri_form_has_three_slashes_before_the_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pysonarlint.lsp.os", _FakeOsName("nt"))
    assert _uri(_ResolvedAs("C:/repo/a.py")).startswith("file:///C:/")


def test_from_uri_keeps_a_posix_leading_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX the leading slash is part of the path and must not be stripped."""
    monkeypatch.setattr("pysonarlint.lsp.os", _FakeOsName("posix"))
    assert _from_uri("file:///repo/a.py").as_posix().endswith("/repo/a.py")


def test_from_uri_strips_the_slash_before_a_windows_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pysonarlint.lsp.os", _FakeOsName("nt"))
    assert str(_from_uri("file:///C:/repo/a.py")).replace("\\", "/") == "C:/repo/a.py"


# -- frame splitting -----------------------------------------------------


def _frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode()
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


def test_a_single_complete_frame_is_split_off() -> None:
    bodies, rest = LanguageServer._frames_from_buffer(_frame({"a": 1}))
    assert [json.loads(b) for b in bodies] == [{"a": 1}]
    assert rest == b""


def test_several_frames_in_one_read() -> None:
    buf = _frame({"a": 1}) + _frame({"b": 2}) + _frame({"c": 3})
    bodies, rest = LanguageServer._frames_from_buffer(buf)
    assert [json.loads(b) for b in bodies] == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert rest == b""


def test_a_partial_body_is_left_for_the_next_read() -> None:
    """The whole point of the buffer: a message can straddle two reads."""
    full = _frame({"message": "a longish value"})
    first, second = full[:20], full[20:]
    bodies, rest = LanguageServer._frames_from_buffer(first)
    assert bodies == []
    assert rest == first
    bodies, rest = LanguageServer._frames_from_buffer(rest + second)
    assert [json.loads(b) for b in bodies] == [{"message": "a longish value"}]
    assert rest == b""


def test_a_complete_header_with_a_partial_body_is_held_back() -> None:
    """The header is fully parsed, so the length is known, but the body has not arrived."""
    full = _frame({"message": "x" * 100})
    head_end = full.index(b"\r\n\r\n") + 4
    partial = full[: head_end + 10]
    bodies, rest = LanguageServer._frames_from_buffer(partial)
    assert bodies == []
    assert rest == partial  # nothing was consumed


def test_a_partial_header_is_left_for_the_next_read() -> None:
    bodies, rest = LanguageServer._frames_from_buffer(b"Content-Length: 12\r\n")
    assert bodies == []
    assert rest == b"Content-Length: 12\r\n"


def test_a_complete_frame_followed_by_a_partial_one() -> None:
    second = _frame({"b": 2})
    buf = _frame({"a": 1}) + second[: len(second) - 3]
    bodies, rest = LanguageServer._frames_from_buffer(buf)
    assert [json.loads(b) for b in bodies] == [{"a": 1}]
    assert rest == second[: len(second) - 3]


def test_an_empty_buffer_yields_nothing() -> None:
    assert LanguageServer._frames_from_buffer(b"") == ([], b"")


def test_an_unparseable_header_resyncs_onto_the_next_frame() -> None:
    """Garbage on the wire must not wedge the reader for the rest of the session."""
    buf = b"Content-Length: not-a-number\r\n\r\n" + _frame({"good": True})
    bodies, rest = LanguageServer._frames_from_buffer(buf)
    assert [json.loads(b) for b in bodies] == [{"good": True}]
    assert rest == b""


def test_a_header_with_no_content_length_resyncs() -> None:
    buf = b"Content-Type: application/json\r\n\r\n" + _frame({"good": True})
    bodies, _rest = LanguageServer._frames_from_buffer(buf)
    assert [json.loads(b) for b in bodies] == [{"good": True}]


def test_extra_headers_before_the_separator_are_tolerated() -> None:
    body = json.dumps({"a": 1}).encode()
    buf = b"Content-Type: application/vscode-jsonrpc\r\nContent-Length: %d\r\n\r\n%s" % (
        len(body),
        body,
    )
    bodies, _rest = LanguageServer._frames_from_buffer(buf)
    assert [json.loads(b) for b in bodies] == [{"a": 1}]


# -- _dispatch_raw -------------------------------------------------------


def test_an_undecodable_frame_is_skipped_not_fatal() -> None:
    server = _server()
    server._dispatch_raw(b"\xff\xfe not utf-8")
    server._dispatch_raw(b"{not json}")
    assert server.logs == []  # nothing crashed, nothing was recorded


def test_a_valid_frame_is_dispatched() -> None:
    server = _server()
    server._dispatch_raw(
        json.dumps({"method": "window/logMessage", "params": {"message": "hello"}}).encode()
    )
    assert server.logs == ["hello"]


# -- _dispatch: responses ------------------------------------------------


def test_a_response_is_handed_to_the_waiting_queue() -> None:
    server = _server()
    queue: Queue[dict[str, Any]] = Queue(maxsize=1)
    server._responses[7] = queue
    server._dispatch({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
    assert queue.get_nowait()["result"] == {"ok": True}


def test_a_response_for_an_abandoned_request_is_dropped() -> None:
    """A request that already timed out has no queue; this must not raise."""
    _server()._dispatch({"jsonrpc": "2.0", "id": 99, "result": None})


def test_a_zero_id_response_is_still_routed() -> None:
    """`if msg_id:` instead of `is not None` would lose id 0."""
    server = _server()
    queue: Queue[dict[str, Any]] = Queue(maxsize=1)
    server._responses[0] = queue
    server._dispatch({"jsonrpc": "2.0", "id": 0, "result": "zero"})
    assert queue.get_nowait()["result"] == "zero"


# -- _dispatch: notifications -------------------------------------------


def test_publish_diagnostics_is_recorded() -> None:
    server = _server()
    server._dispatch(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": "file:///repo/a.py", "diagnostics": [{"message": "m"}]},
        }
    )
    assert server.progress() == (0, 1, 1)


def test_an_empty_publication_never_erases_existing_findings() -> None:
    """The server re-publishes [] for finished documents as new ones are opened, and
    honouring that wiped real results: 3 issues where there had been 31."""
    server = _server()
    params = {"uri": "file:///repo/a.py", "diagnostics": [{"message": "m"}, {"message": "n"}]}
    server._on_publish_diagnostics(params)
    server._on_publish_diagnostics({"uri": "file:///repo/a.py", "diagnostics": []})
    assert server.diagnostic_count() == 2


def test_an_empty_first_publication_is_recorded_as_clean() -> None:
    server = _server()
    server._on_publish_diagnostics({"uri": "file:///repo/a.py", "diagnostics": []})
    assert server.progress() == (0, 1, 0)


def test_a_later_publication_replaces_an_earlier_non_empty_one() -> None:
    server = _server()
    server._on_publish_diagnostics({"uri": "file:///repo/a.py", "diagnostics": [{"message": "m"}]})
    server._on_publish_diagnostics(
        {"uri": "file:///repo/a.py", "diagnostics": [{"message": "m"}, {"message": "n"}]}
    )
    assert server.diagnostic_count() == 2


def test_publish_diagnostics_with_no_params_is_harmless() -> None:
    server = _server()
    server._dispatch({"method": "textDocument/publishDiagnostics"})
    assert server.progress() == (0, 1, 0)


# -- log messages --------------------------------------------------------


def test_log_messages_are_kept_in_order() -> None:
    server = _server()
    for message in ("first", "second"):
        server._on_log_message({"message": message})
    assert server.logs == ["first", "second"]


def test_the_logs_property_returns_a_copy() -> None:
    server = _server()
    server._on_log_message({"message": "x"})
    server.logs.append("mutated")
    assert server.logs == ["x"]


def test_a_log_message_with_no_text_is_still_recorded() -> None:
    server = _server()
    server._on_log_message({})
    assert server.logs == [""]


def test_the_completion_line_advances_the_analysis_count() -> None:
    """There is no 'analysis complete' notification, so the server's own line is it."""
    server = _server()
    server._on_log_message({"message": "Analysis detected 5 issues and 0 Security Hotspots in 9ms"})
    server._on_log_message({"message": "Analysis detected 1 issue and 2 Security Hotspots in 8ms"})
    assert server.progress()[0] == 2


def test_unrelated_logs_do_not_advance_the_analysis_count() -> None:
    server = _server()
    server._on_log_message({"message": "Starting analysis with configuration"})
    assert server.progress()[0] == 0


def test_a_degradation_message_is_captured() -> None:
    """A run the server downgraded must never be labelled connected."""
    server = _server()
    server._on_log_message(
        {
            "message": "[Warn - 00:00:01] [sonarlint] Could not retrieve connected analysis "
            "configuration, falling back to standalone configuration"
        }
    )
    reasons = server.degraded_reasons()
    assert len(reasons) == 1
    assert "falling back to standalone" in reasons[0]
    assert not reasons[0].startswith("[Warn")  # the log prefix is stripped


def test_degradation_reasons_are_deduplicated_and_sorted() -> None:
    server = _server()
    for message in (
        "[Error] No token for connection b",
        "[Error] No token for connection a",
        "[Error] No token for connection a",
    ):
        server._on_log_message({"message": message})
    assert server.degraded_reasons() == [
        "No token for connection a",
        "No token for connection b",
    ]


def test_a_very_long_degradation_message_is_bounded() -> None:
    server = _server()
    server._on_log_message({"message": "falling back to standalone " + "x" * 500})
    assert len(server.degraded_reasons()[0]) == 160


def test_a_healthy_run_reports_no_degradation() -> None:
    server = _server()
    server._on_log_message({"message": "Starting analysis with configuration"})
    assert server.degraded_reasons() == []


# -- server -> client requests -------------------------------------------


def test_a_server_request_is_answered_over_the_wire() -> None:
    server = _server()
    written: list[dict[str, Any]] = []
    server._write = written.append  # type: ignore[method-assign]
    server._dispatch({"id": 4, "method": "sonarlint/isOpenInEditor", "params": {}})
    assert written == [{"jsonrpc": "2.0", "id": 4, "result": True}]


def test_an_unknown_server_request_is_answered_with_null() -> None:
    """Answering with a plausible object risks the server acting on an unintended value."""
    server = _server()
    written: list[dict[str, Any]] = []
    server._write = written.append  # type: ignore[method-assign]
    server._dispatch({"id": 5, "method": "sonarlint/somethingNew", "params": {}})
    assert written == [{"jsonrpc": "2.0", "id": 5, "result": None}]


def test_a_handler_that_raises_is_answered_with_null() -> None:
    """A client bug must not wedge the server waiting on a reply that never comes."""
    server = _server()
    written: list[dict[str, Any]] = []
    server._write = written.append  # type: ignore[method-assign]

    def boom(_ident: str) -> str:
        raise RuntimeError("bad provider")

    server.token_provider = boom
    server._dispatch({"id": 6, "method": "sonarlint/getTokenForServer", "params": {}})
    assert written == [{"jsonrpc": "2.0", "id": 6, "result": None}]


def test_a_reply_that_cannot_be_written_is_swallowed() -> None:
    """The server may be shutting down; a traceback from a reader thread is pure noise."""
    server = _server()

    def dead(_payload: dict[str, Any]) -> None:
        raise LspError("language server is not running")

    server._write = dead  # type: ignore[method-assign]
    server._dispatch({"id": 7, "method": "sonarlint/isOpenInEditor", "params": {}})


def test_a_notification_without_a_handler_is_ignored() -> None:
    server = _server()
    server._write = lambda _p: pytest.fail("a notification needs no reply")  # type: ignore[method-assign]
    server._dispatch({"method": "some/unsolicitedNotification", "params": {}})


def test_is_ignored_by_scm_is_false() -> None:
    assert _server()._reply("sonarlint/isIgnoredByScm", {}) is False


def test_get_java_config_is_null() -> None:
    assert _server()._reply("sonarlint/getJavaConfig", {}) is None


@pytest.mark.parametrize(
    "method",
    ["sonarlint/getFileExclusions", "sonarlint/filterOutExcludedFiles"],
)
def test_exclusion_queries_exclude_nothing(method: str) -> None:
    assert _server()._reply(method, {}) == {"excludedFiles": []}


def test_should_analyse_file_check_is_affirmative() -> None:
    assert _server()._reply("sonarlint/shouldAnalyseFileCheck", {}) == {"shouldBeAnalysed": True}


def test_workspace_folders_is_an_empty_list() -> None:
    assert _server()._reply("workspace/workspaceFolders", {}) == []


@pytest.mark.parametrize("method", ["client/registerCapability", "window/showMessageRequest"])
def test_these_are_answered_with_null(method: str) -> None:
    assert _server()._reply(method, {}) is None


def test_a_none_method_is_answered_with_null() -> None:
    assert _server()._reply(None, {}) is None


# -- listFilesInFolder ---------------------------------------------------


def test_list_folder_includes_the_content_of_open_documents(tmp_path: Path) -> None:
    """The server's file-system view should see what we sent it, not just what is on disk."""
    path = tmp_path / "a.py"
    path.write_text("on disk\n", encoding="utf-8")
    server = _server()
    server._opened[path.resolve()] = "in memory"
    found = server._reply("sonarlint/listFilesInFolder", {"folderUri": _uri(tmp_path)})
    entry = found["foundFiles"][0]
    assert entry["fileName"] == "a.py"
    assert entry["content"] == "in memory"


def test_list_folder_omits_content_for_files_we_never_opened(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    found = _server()._reply("sonarlint/listFilesInFolder", {"folderUri": _uri(tmp_path)})
    assert "content" not in found["foundFiles"][0]


def test_list_folder_without_a_uri_is_empty() -> None:
    assert _server()._reply("sonarlint/listFilesInFolder", {}) == {"foundFiles": []}


def test_list_folder_on_a_file_rather_than_a_directory_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert _server()._reply("sonarlint/listFilesInFolder", {"folderUri": _uri(path)}) == {
        "foundFiles": []
    }


def test_list_folder_survives_an_unreadable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_self: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert _server()._reply("sonarlint/listFilesInFolder", {"folderUri": _uri(tmp_path)}) == {
        "foundFiles": []
    }


# -- request / _notify ---------------------------------------------------


def test_request_writes_a_numbered_message_and_returns_the_result() -> None:
    server = _server()
    written: list[dict[str, Any]] = []

    def answer(payload: dict[str, Any]) -> None:
        written.append(payload)
        server._responses[payload["id"]].put({"id": payload["id"], "result": {"ok": 1}})

    server._write = answer  # type: ignore[method-assign]
    assert server.request("some/method", {"p": 1}) == {"ok": 1}
    assert written[0] == {"jsonrpc": "2.0", "id": 1, "method": "some/method", "params": {"p": 1}}


def test_request_ids_increment() -> None:
    server = _server()
    seen: list[int] = []

    def answer(payload: dict[str, Any]) -> None:
        seen.append(payload["id"])
        server._responses[payload["id"]].put({"id": payload["id"], "result": None})

    server._write = answer  # type: ignore[method-assign]
    server.request("a", {})
    server.request("b", {})
    assert seen == [1, 2]


def test_request_cleans_up_its_queue() -> None:
    server = _server()

    def answer(payload: dict[str, Any]) -> None:
        server._responses[payload["id"]].put({"id": payload["id"], "result": None})

    server._write = answer  # type: ignore[method-assign]
    server.request("a", {})
    assert server._responses == {}


def test_request_raises_on_a_server_side_error() -> None:
    server = _server()

    def answer(payload: dict[str, Any]) -> None:
        server._responses[payload["id"]].put(
            {"id": payload["id"], "error": {"code": -32601, "message": "Unknown command"}}
        )

    server._write = answer  # type: ignore[method-assign]
    with pytest.raises(LspError, match="Unknown command"):
        server.request("bad/method", {})


def test_request_raises_on_a_malformed_error_object() -> None:
    server = _server()

    def answer(payload: dict[str, Any]) -> None:
        server._responses[payload["id"]].put({"id": payload["id"], "error": {"code": -1}})

    server._write = answer  # type: ignore[method-assign]
    with pytest.raises(LspError, match="failed"):
        server.request("bad/method", {})


def test_request_times_out() -> None:
    server = _server()
    server._write = lambda _p: None  # type: ignore[method-assign]
    with pytest.raises(LspError, match="timed out after 0s"):
        server.request("slow/method", {}, timeout=0.05)
    assert server._responses == {}  # cleaned up even on the failure path


def test_notify_writes_a_message_with_no_id() -> None:
    server = _server()
    written: list[dict[str, Any]] = []
    server._write = written.append  # type: ignore[method-assign]
    server._notify("initialized", {})
    assert written == [{"jsonrpc": "2.0", "method": "initialized", "params": {}}]


# -- _write --------------------------------------------------------------


def test_write_frames_the_payload_with_a_content_length() -> None:
    server = _server()
    proc = FakeProc()
    server._proc = proc  # type: ignore[assignment]
    server._write({"a": 1})
    head, _sep, body = proc.stdin.buf.partition(b"\r\n\r\n")
    assert head == b"Content-Length: %d" % len(body)
    assert json.loads(body) == {"a": 1}
    assert proc.stdin.flushes == 1


def test_write_encodes_non_ascii_correctly() -> None:
    server = _server()
    proc = FakeProc()
    server._proc = proc  # type: ignore[assignment]
    server._write({"text": "caf\u00e9"})
    head, _sep, body = proc.stdin.buf.partition(b"\r\n\r\n")
    declared = int(head.split(b":")[1])
    assert declared == len(body)  # bytes, not characters
    assert json.loads(body)["text"] == "caf\u00e9"


def test_write_without_a_process_raises() -> None:
    with pytest.raises(LspError, match="not running"):
        _server()._write({"a": 1})


def test_write_to_a_dead_process_raises() -> None:
    server = _server()
    server._proc = FakeProc(alive=False)  # type: ignore[assignment]
    with pytest.raises(LspError, match="not running"):
        server._write({"a": 1})


def test_write_with_no_stdin_raises() -> None:
    server = _server()
    proc = FakeProc()
    proc.stdin = None  # type: ignore[assignment]
    server._proc = proc  # type: ignore[assignment]
    with pytest.raises(LspError, match="not running"):
        server._write({"a": 1})


def test_write_to_a_closed_pipe_raises() -> None:
    server = _server()
    server._proc = FakeProc(stdin=FakeStdin(fail=True))  # type: ignore[assignment]
    with pytest.raises(LspError, match="closed its input"):
        server._write({"a": 1})


# -- _read_loop ----------------------------------------------------------
#
# The loop itself needs only a byte stream, not a process, so it is driven directly
# rather than through Popen.


class FakeStdout:
    """Serves canned chunks, then EOF. `read1` is what the loop prefers."""

    def __init__(self, chunks: list[bytes], *, error: Exception | None = None) -> None:
        self._chunks = list(chunks)
        self._error = error
        self.reads = 0
        self.closed = False

    def read1(self, _size: int) -> bytes:
        self.reads += 1
        if self._error is not None and not self._chunks:
            raise self._error
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


class FakeStdoutNoRead1:
    """A stream that only offers read(), exercising the hasattr fallback."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def _with_stdout(stream: object) -> LanguageServer:
    server = _server()
    proc = FakeProc()
    proc.stdout = stream  # type: ignore[assignment]
    server._proc = proc  # type: ignore[assignment]
    return server


def test_the_read_loop_dispatches_every_framed_message() -> None:
    stream = FakeStdout(
        [
            _frame({"method": "window/logMessage", "params": {"message": "one"}}),
            _frame({"method": "window/logMessage", "params": {"message": "two"}}),
        ]
    )
    server = _with_stdout(stream)
    server._read_loop()  # returns at EOF
    assert server.logs == ["one", "two"]


def test_the_read_loop_reassembles_a_message_split_across_reads() -> None:
    full = _frame({"method": "window/logMessage", "params": {"message": "split"}})
    server = _with_stdout(FakeStdout([full[:15], full[15:30], full[30:]]))
    server._read_loop()
    assert server.logs == ["split"]


def test_the_read_loop_handles_two_messages_in_one_chunk() -> None:
    chunk = _frame({"method": "window/logMessage", "params": {"message": "a"}}) + _frame(
        {"method": "window/logMessage", "params": {"message": "b"}}
    )
    server = _with_stdout(FakeStdout([chunk]))
    server._read_loop()
    assert server.logs == ["a", "b"]


def test_the_read_loop_falls_back_to_read_when_there_is_no_read1() -> None:
    server = _with_stdout(
        FakeStdoutNoRead1([_frame({"method": "window/logMessage", "params": {"message": "x"}})])
    )
    server._read_loop()
    assert server.logs == ["x"]


@pytest.mark.parametrize("error", [OSError("pipe closed"), ValueError("closed file")])
def test_the_read_loop_exits_quietly_when_the_stream_dies(error: Exception) -> None:
    """A closed pipe during shutdown is normal; a traceback from a daemon thread is noise."""
    server = _with_stdout(
        FakeStdout(
            [_frame({"method": "window/logMessage", "params": {"message": "before"}})], error=error
        )
    )
    server._read_loop()
    assert server.logs == ["before"]


def test_the_read_loop_returns_without_a_process() -> None:
    _server()._read_loop()


def test_the_read_loop_returns_without_stdout() -> None:
    server = _server()
    server._proc = FakeProc()  # type: ignore[assignment]
    server._read_loop()


# -- _drain_stderr -------------------------------------------------------


class FakeStderr:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.closed = False

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def close(self) -> None:
        self.closed = True


def test_stderr_lines_are_collected_as_logs() -> None:
    server = _server()
    proc = FakeProc()
    proc.stderr = FakeStderr([b"one\n", b"two\r\n"])  # type: ignore[assignment]
    server._proc = proc  # type: ignore[assignment]
    server._drain_stderr()
    assert server.logs == ["one", "two"]


def test_blank_stderr_lines_are_dropped() -> None:
    server = _server()
    proc = FakeProc()
    proc.stderr = FakeStderr([b"\n", b"   \n", b"real\n"])  # type: ignore[assignment]
    server._proc = proc  # type: ignore[assignment]
    server._drain_stderr()
    assert server.logs == ["real"]


def test_undecodable_stderr_bytes_are_replaced_not_fatal() -> None:
    server = _server()
    proc = FakeProc()
    proc.stderr = FakeStderr([b"caf\xff\n"])  # type: ignore[assignment]
    server._proc = proc  # type: ignore[assignment]
    server._drain_stderr()
    assert len(server.logs) == 1
    assert server.logs[0].startswith("caf")


def test_drain_stderr_returns_without_a_process() -> None:
    _server()._drain_stderr()


def test_drain_stderr_returns_without_stderr() -> None:
    server = _server()
    server._proc = FakeProc()  # type: ignore[assignment]
    server._drain_stderr()


# -- start ---------------------------------------------------------------
#
# The one place a real subprocess would be needed. Popen is replaced with a fake so the
# launch arguments and the thread wiring can be asserted without a JVM; the loops those
# threads run are tested directly above.


def test_start_launches_with_all_three_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        proc = FakeProc()
        proc.stdout = FakeStdout([])  # type: ignore[assignment]
        proc.stderr = FakeStderr([])  # type: ignore[assignment]
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    server = _server()
    server.start()
    try:
        assert calls[0]["cmd"] == server._cmd
        assert calls[0]["stdin"] is subprocess.PIPE
        assert calls[0]["stdout"] is subprocess.PIPE
        assert calls[0]["stderr"] is subprocess.PIPE
    finally:
        server.stop()


def test_start_spawns_both_reader_threads_as_daemons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon threads, so a wedged reader can never keep the interpreter alive."""

    def fake_popen(_cmd: list[str], **_kwargs: object) -> FakeProc:
        proc = FakeProc()
        # Blocking-free streams: both loops see EOF immediately and exit.
        proc.stdout = FakeStdout([])  # type: ignore[assignment]
        proc.stderr = FakeStderr([])  # type: ignore[assignment]
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    server = _server()
    server.start()
    try:
        assert server._reader is not None and server._reader.daemon is True
        assert server._stderr_reader is not None and server._stderr_reader.daemon is True
        server._reader.join(timeout=5)
        server._stderr_reader.join(timeout=5)
    finally:
        server.stop()


def test_start_reports_a_missing_java_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(LspError, match="could not start language server"):
        _server().start()


# The documented value of subprocess.CREATE_NO_WINDOW. The attribute itself only exists
# on Windows, so forcing the nt branch off Windows needs the constant supplied.
_CREATE_NO_WINDOW = 0x08000000


def test_start_hides_the_console_window_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this a console flashes up on every run on Windows."""
    calls: list[object] = []

    def fake_popen(_cmd: list[str], **kwargs: object) -> FakeProc:
        calls.append(kwargs.get("creationflags"))
        proc = FakeProc()
        proc.stdout = FakeStdout([])  # type: ignore[assignment]
        proc.stderr = FakeStderr([])  # type: ignore[assignment]
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # Present on Windows, absent elsewhere: assert against the real attribute where it
    # exists, and inject it where it does not, so the nt branch is exercised on every
    # platform rather than only on the one that defines the constant.
    expected = getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", expected, raising=False)
    monkeypatch.setattr("pysonarlint.lsp.os", _FakeOsName("nt"))
    server = _server()
    server.start()
    try:
        assert calls[0] == expected
    finally:
        server.stop()


def test_no_creation_flags_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fake_popen(_cmd: list[str], **kwargs: object) -> FakeProc:
        calls.append(kwargs.get("creationflags"))
        proc = FakeProc()
        proc.stdout = FakeStdout([])  # type: ignore[assignment]
        proc.stderr = FakeStderr([])  # type: ignore[assignment]
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("pysonarlint.lsp.os", _FakeOsName("posix"))
    server = _server()
    server.start()
    try:
        assert calls[0] == 0
    finally:
        server.stop()


# -- stop ----------------------------------------------------------------


def test_stop_without_a_process_is_harmless() -> None:
    _server().stop()


def test_stop_sends_exit_and_closes_stdin() -> None:
    server = _server()
    proc = FakeProc()
    server._proc = proc  # type: ignore[assignment]
    server.stop()
    assert b'"method": "exit"' in proc.stdin.buf
    assert proc.stdin.closed is True
    assert server._proc is None


def test_stop_kills_a_process_that_will_not_exit() -> None:
    server = _server()

    class Stubborn(FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise subprocess.TimeoutExpired("java", timeout or 0)
            self._alive = False
            return 0

    proc = Stubborn()
    server._proc = proc  # type: ignore[assignment]
    server.stop()
    assert proc.killed is True


def test_stop_kills_the_process_when_closing_stdin_fails() -> None:
    """Teardown must always end with a dead process, never a leaked JVM."""
    server = _server()

    class Unclosable(FakeStdin):
        def close(self) -> None:
            raise OSError("already closed")

    proc = FakeProc(stdin=Unclosable())
    server._proc = proc  # type: ignore[assignment]
    server.stop()
    assert proc.killed is True
    assert server._proc is None


def test_stop_kills_the_process_when_wait_fails() -> None:
    server = _server()

    class Broken(FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.SubprocessError("cannot reap")

    proc = Broken()
    server._proc = proc  # type: ignore[assignment]
    server.stop()
    assert proc.killed is True


# NOT TESTED HERE, DELIBERATELY: stop() on a process whose stdin write fails.
#
# `_notify("exit")` raises LspError, which subclasses RuntimeError and so is NOT caught by
# stop()'s `except (OSError, ValueError, subprocess.SubprocessError)`. The exception
# escapes stop(); since stop() runs in language_server()'s `finally`, it turns a completed
# analysis into "analysis failed", incomplete=True and CLI exit 2 even though every issue
# had already been collected. A test here would have to assert either the buggy behaviour
# or a fix that does not exist, so this is reported rather than pinned.


def test_stop_does_not_talk_to_an_already_exited_process() -> None:
    server = _server()
    proc = FakeProc(alive=False)
    server._proc = proc  # type: ignore[assignment]
    server.stop()
    assert proc.stdin.buf == b""
    assert server._proc is None


def test_stop_closes_the_output_streams() -> None:
    class Stream:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    server = _server()
    proc = FakeProc(alive=False)
    proc.stdout, proc.stderr = Stream(), Stream()  # type: ignore[assignment]
    server._proc = proc  # type: ignore[assignment]
    server.stop()
    assert proc.stdout.closed and proc.stderr.closed  # type: ignore[union-attr]


# -- the language_server context manager ---------------------------------


def test_the_context_manager_starts_and_always_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(LanguageServer, "start", lambda _self: events.append("start"))
    monkeypatch.setattr(LanguageServer, "stop", lambda _self: events.append("stop"))
    with language_server(Path("java"), Path("ls.jar"), ()) as server:
        assert isinstance(server, LanguageServer)
        events.append("body")
    assert events == ["start", "body", "stop"]


def test_the_context_manager_stops_on_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(LanguageServer, "start", lambda _self: events.append("start"))
    monkeypatch.setattr(LanguageServer, "stop", lambda _self: events.append("stop"))
    with pytest.raises(RuntimeError), language_server(Path("java"), Path("ls.jar"), ()):
        raise RuntimeError("boom")
    assert events == ["start", "stop"]


def test_verbose_is_carried_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LanguageServer, "start", lambda _self: None)
    monkeypatch.setattr(LanguageServer, "stop", lambda _self: None)
    with language_server(Path("java"), Path("ls.jar"), (), verbose=True) as server:
        assert server._verbose is True


# -- initialize / open_document / barrier --------------------------------


def test_initialize_declares_the_capabilities_the_server_needs() -> None:
    server = _server()
    sent: list[dict[str, Any]] = []

    def answer(payload: dict[str, Any]) -> None:
        sent.append(payload)
        if "id" in payload:
            server._responses[payload["id"]].put({"id": payload["id"], "result": {"caps": 1}})

    server._write = answer  # type: ignore[method-assign]
    result = server.initialize(Path("/repo"), {"productKey": "pysonarlint"})
    params = sent[0]["params"]
    assert result == {"caps": 1}
    assert params["initializationOptions"] == {"productKey": "pysonarlint"}
    assert params["capabilities"]["workspace"]["configuration"] is True
    assert params["capabilities"]["workspace"]["workspaceFolders"] is True
    assert (
        params["capabilities"]["textDocument"]["publishDiagnostics"]["relatedInformation"] is True
    )
    assert params["workspaceFolders"][0]["name"] == "repo"
    assert sent[1]["method"] == "initialized"


def test_initialize_without_a_root_sends_nulls() -> None:
    server = _server()

    def answer(payload: dict[str, Any]) -> None:
        if "id" in payload:
            server._responses[payload["id"]].put({"id": payload["id"], "result": None})
            assert payload["params"]["rootUri"] is None
            assert payload["params"]["workspaceFolders"] is None

    server._write = answer  # type: ignore[method-assign]
    assert server.initialize(None, {}) == {}  # a null result becomes {}


def test_open_document_sends_did_open_and_remembers_the_text(tmp_path: Path) -> None:
    server = _server()
    sent: list[dict[str, Any]] = []
    server._write = sent.append  # type: ignore[method-assign]
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    server.open_document(path, "x = 1\n", "python")
    doc = sent[0]["params"]["textDocument"]
    assert sent[0]["method"] == "textDocument/didOpen"
    assert doc["languageId"] == "python"
    assert doc["version"] == 1
    assert doc["text"] == "x = 1\n"
    assert server._opened[path.resolve()] == "x = 1\n"


def test_open_document_defaults_to_python(tmp_path: Path) -> None:
    server = _server()
    sent: list[dict[str, Any]] = []
    server._write = sent.append  # type: ignore[method-assign]
    server.open_document(tmp_path / "a.py", "x = 1\n")
    assert sent[0]["params"]["textDocument"]["languageId"] == "python"


def test_the_barrier_uses_an_ordered_request() -> None:
    server = _server()
    sent: list[dict[str, Any]] = []

    def answer(payload: dict[str, Any]) -> None:
        sent.append(payload)
        server._responses[payload["id"]].put({"id": payload["id"], "result": None})

    server._write = answer  # type: ignore[method-assign]
    server.barrier(timeout=5.0)
    assert sent[0]["method"] == "workspace/executeCommand"


def test_an_unknown_command_error_is_a_perfectly_good_barrier() -> None:
    """It proves the server reached the message, which is all the barrier asserts."""
    server = _server()

    def answer(payload: dict[str, Any]) -> None:
        server._responses[payload["id"]].put(
            {"id": payload["id"], "error": {"message": "Unsupported command"}}
        )

    server._write = answer  # type: ignore[method-assign]
    server.barrier(timeout=5.0)  # must not raise


def test_a_barrier_timeout_is_fatal() -> None:
    server = _server()
    server._write = lambda _p: None  # type: ignore[method-assign]
    with pytest.raises(LspError, match="timed out"):
        server.barrier(timeout=0.05)


# -- diagnostics() -------------------------------------------------------


def _publish(server: LanguageServer, uri: str, items: list[dict[str, Any]]) -> None:
    server._on_publish_diagnostics({"uri": uri, "diagnostics": items})


def _wire(
    *,
    line: int = 0,
    character: int = 0,
    end_line: int | None = None,
    end_character: int = 5,
    code: str = "python:S100",
    message: str = "m",
    severity: int = 2,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "range": {
            "start": {"line": line, "character": character},
            "end": {"line": end_line if end_line is not None else line, "character": end_character},
        },
        "code": code,
        "message": message,
        "severity": severity,
        "source": "sonarqube",
        **extra,
    }


def test_diagnostics_are_normalised_to_one_based_positions() -> None:
    """LSP is zero-based and every editor and CI annotation format is one-based."""
    server = _server()
    _publish(server, "file:///repo/a.py", [_wire(line=4, character=8, end_character=12)])
    issue = server.diagnostics()[0]
    assert (issue.line, issue.column) == (5, 9)
    assert (issue.end_line, issue.end_column) == (5, 13)


def test_diagnostics_carry_the_rule_message_and_severity() -> None:
    server = _server()
    _publish(
        server, "file:///repo/a.py", [_wire(code="python:S1481", message="unused", severity=1)]
    )
    issue = server.diagnostics()[0]
    assert issue.rule == "python:S1481"
    assert issue.message == "unused"
    assert issue.severity == 1
    assert issue.severity_name == "error"
    assert issue.source == "sonarqube"


def test_a_missing_severity_defaults_to_warning() -> None:
    server = _server()
    _publish(server, "file:///repo/a.py", [{"range": {}, "code": "r", "message": "m"}])
    assert server.diagnostics()[0].severity == 2


def test_a_diagnostic_with_no_range_lands_on_line_one() -> None:
    server = _server()
    _publish(server, "file:///repo/a.py", [{"code": "r", "message": "m"}])
    issue = server.diagnostics()[0]
    assert (issue.line, issue.column, issue.end_line, issue.end_column) == (1, 1, 1, 1)


def test_a_multi_line_range_is_preserved() -> None:
    server = _server()
    _publish(server, "file:///repo/a.py", [_wire(line=2, end_line=7, end_character=3)])
    issue = server.diagnostics()[0]
    assert (issue.line, issue.end_line, issue.end_column) == (3, 8, 4)


def test_only_the_known_extra_keys_are_kept() -> None:
    """`extra` is a whitelist, so a future field cannot silently enter the json contract."""
    server = _server()
    _publish(
        server,
        "file:///repo/a.py",
        [
            _wire(
                data={"ruleKey": "python:S100"},
                tags=["convention"],
                relatedInformation=[{"message": "here"}],
                someFutureField="ignored",
            )
        ],
    )
    extra = server.diagnostics()[0].extra
    assert set(extra) == {"data", "tags", "relatedInformation"}


def test_a_diagnostic_with_no_extras_has_an_empty_dict() -> None:
    server = _server()
    _publish(server, "file:///repo/a.py", [_wire()])
    assert server.diagnostics()[0].extra == {}


def test_diagnostics_are_sorted_by_path_then_line_then_column_then_rule() -> None:
    server = _server()
    _publish(
        server,
        "file:///repo/b.py",
        [_wire(line=0, code="python:S1")],
    )
    _publish(
        server,
        "file:///repo/a.py",
        [
            _wire(line=9, character=0, code="python:S3"),
            _wire(line=0, character=4, code="python:S2"),
            _wire(line=0, character=0, code="python:S9"),
            _wire(line=0, character=0, code="python:S1"),
        ],
    )
    ordered = [(Path(i.path).name, i.line, i.column, i.rule) for i in server.diagnostics()]
    assert ordered == [
        ("a.py", 1, 1, "python:S1"),
        ("a.py", 1, 1, "python:S9"),
        ("a.py", 1, 5, "python:S2"),
        ("a.py", 10, 1, "python:S3"),
        ("b.py", 1, 1, "python:S1"),
    ]


def test_diagnostics_with_nothing_published_is_empty() -> None:
    assert _server().diagnostics() == []


def test_the_path_is_decoded_from_the_uri() -> None:
    server = _server()
    _publish(server, "file:///repo/a%20b/c.py", [_wire()])
    assert server.diagnostics()[0].path.as_posix().endswith("/repo/a b/c.py")


def test_diagnostic_count_totals_across_files() -> None:
    server = _server()
    _publish(server, "file:///repo/a.py", [_wire(), _wire(line=1)])
    _publish(server, "file:///repo/b.py", [_wire()])
    assert server.diagnostic_count() == 3


def test_diagnostic_defaults() -> None:
    issue = Diagnostic(
        path=Path("a.py"),
        line=1,
        column=1,
        end_line=1,
        end_column=2,
        rule="r",
        message="m",
        severity=2,
    )
    assert issue.source == ""
    assert issue.extra == {}


def test_an_unrecognised_severity_reads_as_warning() -> None:
    issue = Diagnostic(
        path=Path("a.py"),
        line=1,
        column=1,
        end_line=1,
        end_column=2,
        rule="r",
        message="m",
        severity=99,
    )
    assert issue.severity_name == "warning"


# -- thread safety -------------------------------------------------------


def test_concurrent_publications_are_all_recorded() -> None:
    """The reader thread publishes while the main thread reads, so the lock is load-bearing."""
    server = _server()
    barrier = threading.Barrier(4)

    def publish(index: int) -> None:
        barrier.wait()
        for n in range(25):
            _publish(server, f"file:///repo/f{index}_{n}.py", [_wire()])

    threads = [threading.Thread(target=publish, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert server.diagnostic_count() == 100
    assert server.progress()[1] == 100
