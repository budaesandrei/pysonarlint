"""Browser-based token grant and credential storage.

The handshake mirrors what SonarQube for IDE does: stand up a loopback HTTP listener,
send the user to <server>/sonarlint/auth?ideName=...&port=..., and receive the generated
token by POST. The server first probes the listener with a GET to confirm something is
really there, so both verbs must be handled.

Tokens are stored encrypted at rest via DPAPI on Windows (bound to the OS user account)
and mode-0600 files elsewhere. Storage failures degrade to "not saved" rather than
raising, so a login is never lost to a keyring problem.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# The port window SonarQube for IDE uses. The server will only redirect to a loopback
# port in this range, so it is not ours to choose freely.
PORT_RANGE = range(64120, 64131)

IDE_NAME = "pysonarlint"
_TIMEOUT = 180.0


class AuthError(RuntimeError):
    """The token grant could not be completed."""


@dataclass
class Credential:
    url: str
    token: str
    organization: str | None = None


def _store_path() -> Path:
    """Where credentials live. Honours SONAR_USER_HOME like the scanner does."""
    if home := os.environ.get("PYSONARLINT_HOME_DIR"):
        base = Path(home)
    elif sonar_home := os.environ.get("SONAR_USER_HOME"):
        base = Path(sonar_home) / "pysonarlint"
    elif os.name == "nt" and (appdata := os.environ.get("APPDATA")):
        base = Path(appdata) / "pysonarlint"
    else:
        base = Path.home() / ".config" / "pysonarlint"
    return base / "credentials.json"


def _dpapi_encrypt(data: bytes) -> bytes | None:
    """Encrypt with the Windows user's DPAPI key, or None if unavailable."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        blob_in = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
        blob_out = BLOB()
        if not crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:  # noqa: BLE001 - any failure means "no DPAPI"
        return None


def _dpapi_decrypt(data: bytes) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        blob_in = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
        blob_out = BLOB()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:  # noqa: BLE001
        return None


def save_credential(cred: Credential) -> tuple[bool, str]:
    """Persist a credential. Returns (saved, human-readable location or reason)."""
    path = _store_path()
    try:
        existing = _load_all()
        existing[_key(cred.url)] = {
            "url": cred.url,
            "token": cred.token,
            "organization": cred.organization,
        }
        raw = json.dumps({"version": 1, "credentials": existing}).encode("utf-8")

        if (sealed := _dpapi_encrypt(raw)) is not None:
            payload = json.dumps(
                {"version": 1, "encryption": "dpapi", "data": base64.b64encode(sealed).decode()}
            ).encode("utf-8")
        else:
            payload = json.dumps(
                {"version": 1, "encryption": "none", "data": base64.b64encode(raw).decode()}
            ).encode("utf-8")

        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive permissions before writing content.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(path, 0o600)
        how = "DPAPI-encrypted" if sealed is not None else "plaintext, mode 0600"
        return True, f"{path} ({how})"
    except OSError as exc:
        return False, f"could not write {path}: {exc}"


def _load_all() -> dict[str, dict[str, object]]:
    path = _store_path()
    if not path.is_file():
        return {}
    try:
        outer = json.loads(path.read_text(encoding="utf-8"))
        blob = base64.b64decode(str(outer.get("data", "")))
        if outer.get("encryption") == "dpapi":
            opened = _dpapi_decrypt(blob)
            if opened is None:
                return {}
            blob = opened
        inner = json.loads(blob.decode("utf-8"))
        creds = inner.get("credentials")
        return creds if isinstance(creds, dict) else {}
    except (OSError, ValueError):  # JSONDecodeError subclasses ValueError
        return {}


def _key(url: str) -> str:
    return url.rstrip("/").lower()


def load_token(url: str) -> str | None:
    """Retrieve a stored token for a server URL."""
    entry = _load_all().get(_key(url))
    if isinstance(entry, dict):
        token = entry.get("token")
        return token if isinstance(token, str) and token else None
    return None


def forget(url: str) -> bool:
    """Remove a stored credential. True if something was removed."""
    creds = _load_all()
    if _key(url) not in creds:
        return False
    del creds[_key(url)]
    path = _store_path()
    try:
        raw = json.dumps({"version": 1, "credentials": creds}).encode("utf-8")
        if (sealed := _dpapi_encrypt(raw)) is not None:
            payload = {"version": 1, "encryption": "dpapi", "data": base64.b64encode(sealed).decode()}
        else:
            payload = {"version": 1, "encryption": "none", "data": base64.b64encode(raw).decode()}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return True
    except OSError:
        return False


class _Handler(BaseHTTPRequestHandler):
    """Handles the two requests the server makes: a status probe, then the token POST."""

    received: str | None = None
    done: threading.Event

    def log_message(self, *_args: object) -> None:  # noqa: D102 - silence stderr logging
        pass

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        """The server probes for a live IDE before offering to generate a token."""
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/status"):
            body = json.dumps({"ideName": IDE_NAME, "description": "terminal"}).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"pysonarlint is listening")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        token = self._extract_token(raw)

        self.send_response(200 if token else 400)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK" if token else b"no token in request")

        if token:
            type(self).received = token
            type(self).done.set()

    def _extract_token(self, raw: bytes) -> str | None:
        """Pull the token out of the browser's POST body.

        SonarQube posts a JSON object to /sonarlint/api/token but labels it
        `text/plain`, so the body must be sniffed rather than trusted to the
        Content-Type header. The payload looks like:

            {"login":"...","name":"SonarLint-pysonarlint-1","createdAt":"...",
             "expirationDate":"...","token":"squ_...","type":"USER_TOKEN"}
        """
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return None

        # Sniff for JSON regardless of the declared content type.
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                for key in ("token", "value"):
                    candidate = data.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
                return None

        content_type = (self.headers.get("Content-Type") or "").lower()
        if "form-urlencoded" in content_type or ("=" in text and "\n" not in text):
            parsed = parse_qs(text)
            for key in ("token", "value"):
                if parsed.get(key):
                    return parsed[key][0].strip()

        # A bare token. Reject anything that is obviously not one so a stray body
        # never gets stored and reported as a working credential.
        if "\n" in text or len(text) > 200 or text.startswith(("<", "[")):
            return None
        return text or None


def _bind_listener() -> tuple[HTTPServer, int]:
    """Bind the first free port in the range the server will redirect to."""
    last: OSError | None = None
    for port in PORT_RANGE:
        try:
            server = HTTPServer(("127.0.0.1", port), _Handler)
        except OSError as exc:
            last = exc
            continue
        return server, port
    raise AuthError(
        f"no free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}; "
        f"another IDE may be listening ({last})"
    )


def grant_token(
    url: str,
    *,
    timeout: float = _TIMEOUT,
    open_browser: bool = True,
    on_url: Callable[[str], None] | None = None,
) -> str:
    """Run the browser handshake and return the granted token.

    Raises AuthError on timeout or if the port range is unavailable.
    """
    server, port = _bind_listener()
    _Handler.received = None
    _Handler.done = threading.Event()

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()

    auth_url = f"{url.rstrip('/')}/sonarlint/auth?ideName={IDE_NAME}&port={port}"
    if on_url is not None:
        on_url(auth_url)
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001
            # Headless environments have no browser; the URL was already printed.
            pass

    try:
        if not _Handler.done.wait(timeout):
            raise AuthError(
                f"timed out after {timeout:.0f}s waiting for the browser. "
                "Generate a User token manually and set SONAR_TOKEN instead."
            )
        token = _Handler.received
        if not token:
            raise AuthError("the browser completed but sent no token")
        return token
    finally:
        server.shutdown()
        server.server_close()


def port_available() -> bool:
    """Whether any port in the required range is free."""
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                continue
    return False
