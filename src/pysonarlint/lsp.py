"""Minimal LSP client for the SonarLint language server.

Hand-rolled rather than pygls-based: we need exactly one transport (framed JSON-RPC over
stdio) and none of pygls' server machinery or import cost.

The completion signal matters. Diagnostics arrive as unsolicited notifications with no
"analysis finished" marker, so a naive client waits for an idle gap and calls it done --
which silently truncates on a slow file and still exits 0. Instead we exploit JSON-RPC
ordering: the server processes messages in order, so a request issued *after* the
didOpen batch cannot be answered before those documents have been handled. Its response
is our barrier.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any

_HEADER_SEP = b"\r\n\r\n"

# The server logs this once per completed analysis batch. It is our only reliable
# completion signal: "Analysis detected 5 issues and 0 Security Hotspots in 1309ms".
_ANALYSIS_DONE_RE = re.compile(
    r"Analysis detected (\d+) issues? and (\d+) Security Hotspots?", re.I
)

# Server log fragments that mean a requested binding was NOT applied. Without these a
# run reports "connected mode" while actually using the standalone rule set.
_DEGRADED_PATTERNS = (
    "falling back to standalone",
    "No token for connection",
    "No connections configured",
    "Could not retrieve connected analysis configuration",
)


class LspError(RuntimeError):
    """The language server misbehaved or died."""


@dataclass
class Diagnostic:
    """One reported issue, normalised out of LSP's wire shape."""

    path: Path
    line: int
    column: int
    end_line: int
    end_column: int
    rule: str
    message: str
    severity: int
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def severity_name(self) -> str:
        return {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
            self.severity, "warning"
        )


def _uri(path: Path) -> str:
    """Path -> file URI, percent-encoded.

    Encoding is not cosmetic: a raw space in the path (common on Windows, e.g. under
    "Program Files" or a synced-folder name) makes the server reject initialize with an
    opaque "Internal error". The drive letter's colon is left intact, as LSP clients
    emit it.
    """
    from urllib.parse import quote

    as_posix = path.resolve().as_posix()
    encoded = quote(as_posix, safe="/:")
    if os.name == "nt":
        return "file:///" + encoded.lstrip("/")
    return "file://" + encoded


def _from_uri(uri: str) -> Path:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    raw = unquote(parsed.path)
    if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


class LanguageServer:
    """A running sonarlint-ls process speaking framed JSON-RPC over stdio."""

    def __init__(
        self,
        java: Path,
        server_jar: Path,
        analyzers: tuple[Path, ...],
        *,
        verbose: bool = False,
    ) -> None:
        self._cmd = [
            str(java),
            "-Djava.awt.headless=true",
            "-XX:+UseSerialGC",  # short-lived, single-shot: avoids G1's startup cost
            "-jar",
            str(server_jar),
            "-stdio",
            *([f"-analyzers={a}" for a in analyzers] if analyzers else []),
        ]
        self._verbose = verbose
        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._responses: dict[int, Queue[dict[str, Any]]] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._published: set[str] = set()
        self._analyses_completed = 0
        self._degraded: set[str] = set()
        self._opened: dict[Path, str] = {}
        self._logs: list[str] = []
        # Set by connected mode to hand the server a token on demand.
        self.token_provider: Callable[[str], str | None] | None = None
        # Returned for every workspace/configuration request; carries the connection
        # and binding that make connected mode actually take effect.
        self.settings: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                self._cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation,
            )
        except OSError as exc:
            raise LspError(f"could not start language server: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                self._notify("exit", {})
                if proc.stdin:
                    proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except (OSError, ValueError, subprocess.SubprocessError):
            proc.kill()
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            self._proc = None

    # -- transport ---------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise LspError("language server is not running")
        body = json.dumps(payload).encode("utf-8")
        try:
            proc.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
            proc.stdin.flush()
        except OSError as exc:
            raise LspError(f"language server closed its input: {exc}") from exc

    def _read_loop(self) -> None:
        """Parse framed messages until stdout closes."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        buf = b""
        while True:
            try:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            buf += chunk
            while True:
                head, sep, rest = buf.partition(_HEADER_SEP)
                if not sep:
                    break
                length = self._content_length(head)
                if length is None or len(rest) < length:
                    if length is None:
                        buf = rest  # unparseable header: resync
                        continue
                    break
                raw, buf = rest[:length], rest[length:]
                try:
                    self._dispatch(json.loads(raw.decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

    @staticmethod
    def _content_length(head: bytes) -> int | None:
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    return int(line.split(b":", 1)[1].strip())
                except ValueError:
                    return None
        return None

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                with self._lock:
                    self._logs.append(line)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if (msg_id := msg.get("id")) is not None and "method" not in msg:
            with self._lock:
                queue = self._responses.get(msg_id)
            if queue:
                queue.put(msg)
            return

        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            with self._lock:
                # Later publications supersede earlier ones for the same document.
                self._diagnostics[uri] = params.get("diagnostics", [])
                self._published.add(uri)
        elif method == "window/logMessage":
            message = str(params.get("message", ""))
            with self._lock:
                self._logs.append(message)
                # There is no "analysis complete" notification, so we watch for the
                # server's own completion line. Diagnostics can arrive several seconds
                # after the request barrier returns, so a purely time-based wait
                # reports zero issues on a file that has plenty.
                if _ANALYSIS_DONE_RE.search(message):
                    self._analyses_completed += 1
                # The server degrades to standalone on its own if a binding cannot be
                # used. Capture that so we never label such a run "connected".
                for pattern in _DEGRADED_PATTERNS:
                    if pattern in message:
                        self._degraded.add(message.split("] ")[-1].strip()[:160])
        elif msg.get("id") is not None:
            # Server->client request. Reply so it never blocks waiting on us.
            try:
                result = self._reply(method, params)
            except Exception:  # noqa: BLE001 - a client bug must not wedge the server
                result = None
            try:
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except (LspError, OSError, ValueError):
                # The server may be shutting down; nothing useful to do from a
                # reader thread, and a traceback here is pure noise.
                pass

    def _reply(self, method: str | None, params: dict[str, Any]) -> Any:
        """Answer a server->client request.

        The sonarlint/* methods are non-standard extensions and the answers are
        load-bearing: replying with a generic empty object makes the server silently
        discard analysis results.
        """
        if method == "sonarlint/isOpenInEditor":
            # We are the editor. Claiming otherwise makes the server drop diagnostics
            # for the document on the floor.
            return True
        if method == "sonarlint/isIgnoredByScm":
            return False
        if method == "sonarlint/askSslCertificateConfirmation":
            # Declining is the safe default: it only ever blocks optional analyzer
            # downloads we don't need, and never silently trusts an unknown cert.
            return False
        if method == "sonarlint/listFilesInFolder":
            return {"foundFiles": self._list_folder(params)}
        if method == "sonarlint/getJavaConfig":
            return None
        if method in ("sonarlint/shouldAnalyseFile", "sonarlint/shouldAnalyseFileCheck"):
            return {"shouldBeAnalysed": True}
        if method in ("sonarlint/getFileExclusions", "sonarlint/filterOutExcludedFiles"):
            return {"excludedFiles": []}
        if method == "sonarlint/getTokenForServer":
            if not self.token_provider:
                return None
            # The identifier has been spelled serverId and connectionId across
            # versions; the value is the same connection id either way.
            ident = str(params.get("serverId") or params.get("connectionId") or "")
            return self.token_provider(ident)
        if method == "workspace/configuration":
            # How the server obtains connection and binding settings. Each item names a
            # specific section, and the reply must be positionally aligned with them.
            # Returning a bare [] leaves the server with connections={} and no binding,
            # so connected mode degrades while still looking connected.
            items = params.get("items") or [{"section": "sonarlint"}]
            return [self._setting_for(str(item.get("section") or "")) for item in items]
        if method == "workspace/workspaceFolders":
            return []
        if method == "client/registerCapability":
            return None
        if method == "window/showMessageRequest":
            return None
        return None

    def _setting_for(self, section: str) -> Any:
        """Value for one requested configuration section.

        Only `sonarlint` carries our settings; the others are VS Code extension
        settings the server probes for opportunistically and must get null for.
        """
        if section == "sonarlint":
            return self.settings
        if section == "files.exclude":
            return {}
        return None

    def _list_folder(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Enumerate a folder's immediate files for the server's file system view."""
        uri = params.get("folderUri") or ""
        if not uri:
            return []
        try:
            folder = _from_uri(uri)
            if not folder.is_dir():
                return []
            out = []
            for child in folder.iterdir():
                if not child.is_file():
                    continue
                entry: dict[str, Any] = {"fileName": child.name, "filePath": str(child)}
                if child in self._opened:
                    entry["content"] = self._opened[child]
                out.append(entry)
            return out
        except OSError:
            return []

    # -- requests ----------------------------------------------------------

    def request(self, method: str, params: dict[str, Any], timeout: float = 120.0) -> Any:
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
            queue: Queue[dict[str, Any]] = Queue(maxsize=1)
            self._responses[msg_id] = queue
        try:
            self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            try:
                reply = queue.get(timeout=timeout)
            except Empty:
                raise LspError(f"{method} timed out after {timeout:.0f}s") from None
        finally:
            with self._lock:
                self._responses.pop(msg_id, None)
        if error := reply.get("error"):
            raise LspError(f"{method} failed: {error.get('message', error)}")
        return reply.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # -- protocol ----------------------------------------------------------

    def initialize(self, root: Path | None, options: dict[str, Any]) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": _uri(root) if root else None,
                "workspaceFolders": (
                    [{"uri": _uri(root), "name": root.name}] if root else None
                ),
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                    "workspace": {"configuration": True, "workspaceFolders": True},
                },
                "initializationOptions": options,
            },
        )
        self._notify("initialized", {})
        return result or {}

    def open_document(self, path: Path, text: str, language_id: str = "python") -> None:
        self._opened[path.resolve()] = text
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": _uri(path),
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )

    def barrier(self, timeout: float = 300.0) -> None:
        """Block until the server has processed everything sent before this call.

        JSON-RPC responses are ordered, so a fresh request cannot be answered until
        prior messages are handled. This replaces guessing with an idle timer.
        """
        try:
            self.request("workspace/executeCommand", {"command": "__pysonarlint_barrier"}, timeout)
        except LspError as exc:
            # An "unknown command" error is a perfectly good barrier: it proves the
            # server reached this message. Only a timeout is fatal.
            if "timed out" in str(exc):
                raise

    def diagnostics(self) -> list[Diagnostic]:
        with self._lock:
            snapshot = dict(self._diagnostics)
        out: list[Diagnostic] = []
        for uri, items in snapshot.items():
            path = _from_uri(uri)
            for d in items:
                rng = d.get("range", {})
                start, end = rng.get("start", {}), rng.get("end", {})
                out.append(
                    Diagnostic(
                        path=path,
                        line=int(start.get("line", 0)) + 1,
                        column=int(start.get("character", 0)) + 1,
                        end_line=int(end.get("line", 0)) + 1,
                        end_column=int(end.get("character", 0)) + 1,
                        rule=str(d.get("code", "")),
                        message=str(d.get("message", "")),
                        severity=int(d.get("severity", 2)),
                        source=str(d.get("source", "")),
                        extra={
                            k: v
                            for k, v in d.items()
                            if k in ("data", "tags", "relatedInformation")
                        },
                    )
                )
        out.sort(key=lambda d: (str(d.path), d.line, d.column, d.rule))
        return out

    def diagnostic_count(self) -> int:
        """Total diagnostics published so far, for settle detection."""
        with self._lock:
            return sum(len(v) for v in self._diagnostics.values())

    def degraded_reasons(self) -> list[str]:
        """Server-reported reasons a requested binding was not applied."""
        with self._lock:
            return sorted(self._degraded)

    def progress(self) -> tuple[int, int, int]:
        """(analyses completed, documents published, diagnostics) for settle logic."""
        with self._lock:
            return (
                self._analyses_completed,
                len(self._published),
                sum(len(v) for v in self._diagnostics.values()),
            )

    @property
    def logs(self) -> list[str]:
        with self._lock:
            return list(self._logs)


@contextmanager
def language_server(
    java: Path,
    server_jar: Path,
    analyzers: tuple[Path, ...],
    *,
    verbose: bool = False,
) -> Iterator[LanguageServer]:
    server = LanguageServer(java, server_jar, analyzers, verbose=verbose)
    server.start()
    try:
        yield server
    finally:
        server.stop()
