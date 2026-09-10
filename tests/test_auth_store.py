"""Credential storage and the loopback token-grant handshake.

Storage is redirected to tmp_path via PYSONARLINT_HOME_DIR, so the developer's real
credential file is never read or written. `grant_token` is driven against a real
loopback listener from a client thread; no browser is opened and no external host is
contacted.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pysonarlint import auth
from pysonarlint.auth import (
    IDE_NAME,
    PORT_RANGE,
    AuthError,
    Credential,
    _bare_token,
    _bind_listener,
    _load_all,
    _store_path,
    _token_from_form,
    _token_from_json,
    forget,
    grant_token,
    load_token,
    port_available,
    save_credential,
)

TOKEN = "squ_EXAMPLEfakeTOKENvalueNOTaREALsecret00"
OTHER_TOKEN = "squ_ANOTHERfakeTOKENvalueNOTaREALsecret1"
URL = "https://sonar.example.com"


@pytest.fixture(autouse=True)
def _store_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every credential operation at a throwaway directory."""
    home = tmp_path / "home"
    monkeypatch.setenv("PYSONARLINT_HOME_DIR", str(home))
    monkeypatch.delenv("SONAR_USER_HOME", raising=False)
    return home


class _FakeOsName:
    """`os` with a different `name`, and everything else proxied through.

    Setting the real `os.name` would be simpler and is a trap: `pathlib` reads it at
    Path construction time, so a POSIX-flavoured `os.name` on Windows makes every
    `Path(...)` raise -- including the ones inside pytest's own failure reporting, which
    turns any unrelated assertion failure into an INTERNALERROR.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str) -> object:
        return getattr(os, attr)


# -- _store_path precedence ------------------------------------------------


def test_store_path_prefers_our_own_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYSONARLINT_HOME_DIR", str(tmp_path / "ours"))
    monkeypatch.setenv("SONAR_USER_HOME", str(tmp_path / "sonar"))
    assert _store_path() == tmp_path / "ours" / "credentials.json"


def test_store_path_honours_sonar_user_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scanner's own variable, so one setting relocates both tools."""
    monkeypatch.delenv("PYSONARLINT_HOME_DIR", raising=False)
    monkeypatch.setenv("SONAR_USER_HOME", str(tmp_path / "sonar"))
    assert _store_path() == tmp_path / "sonar" / "pysonarlint" / "credentials.json"


def test_store_path_uses_appdata_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYSONARLINT_HOME_DIR", raising=False)
    monkeypatch.delenv("SONAR_USER_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(auth, "os", _FakeOsName("nt"))
    assert _store_path() == tmp_path / "AppData" / "pysonarlint" / "credentials.json"


def test_store_path_falls_back_to_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYSONARLINT_HOME_DIR", raising=False)
    monkeypatch.delenv("SONAR_USER_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "h"))
    monkeypatch.setattr(auth, "os", _FakeOsName("posix"))
    assert _store_path() == tmp_path / "h" / ".config" / "pysonarlint" / "credentials.json"


def test_store_path_falls_back_when_appdata_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYSONARLINT_HOME_DIR", raising=False)
    monkeypatch.delenv("SONAR_USER_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "h"))
    monkeypatch.setattr(auth, "os", _FakeOsName("nt"))
    assert _store_path() == tmp_path / "h" / ".config" / "pysonarlint" / "credentials.json"


# -- round trip ------------------------------------------------------------


def test_save_then_load(_store_in_tmp: Path) -> None:
    saved, where = save_credential(Credential(url=URL, token=TOKEN, organization="org"))
    assert saved is True
    assert str(_store_in_tmp) in where
    assert load_token(URL) == TOKEN


def test_the_token_is_never_stored_in_the_clear(_store_in_tmp: Path) -> None:
    """Either DPAPI-sealed or base64 in a mode-0600 file; never a plain grep hit."""
    save_credential(Credential(url=URL, token=TOKEN))
    raw = (_store_in_tmp / "credentials.json").read_bytes()
    assert TOKEN.encode() not in raw


def test_the_stored_file_declares_its_encryption(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    outer = json.loads((_store_in_tmp / "credentials.json").read_text(encoding="utf-8"))
    assert outer["version"] == 1
    assert outer["encryption"] in ("dpapi", "none")


def test_load_is_case_and_slash_insensitive(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    assert load_token("https://SONAR.EXAMPLE.COM/") == TOKEN


def test_load_token_for_an_unknown_server_is_none(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    assert load_token("https://other.example.com") is None


def test_load_token_with_no_store_at_all_is_none() -> None:
    assert load_token(URL) is None


def test_several_servers_coexist(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    save_credential(Credential(url="https://two.example.com", token=OTHER_TOKEN))
    assert load_token(URL) == TOKEN
    assert load_token("https://two.example.com") == OTHER_TOKEN


def test_saving_again_replaces_the_token(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    save_credential(Credential(url=URL, token=OTHER_TOKEN))
    assert load_token(URL) == OTHER_TOKEN


def test_organization_survives_the_round_trip(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN, organization="my-org"))
    entry = _load_all()[URL.lower()]
    assert entry["organization"] == "my-org"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_the_file_is_not_group_or_world_readable(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    assert (_store_in_tmp / "credentials.json").stat().st_mode & 0o077 == 0


# DPAPI is only available on Windows, and even there it needs a live user keystore. The
# two outcomes that matter are the sealed path and the plaintext fallback, so the sealer
# is stubbed and both are exercised on every platform.


def _fake_dpapi(monkeypatch: pytest.MonkeyPatch, *, available: bool) -> None:
    if available:
        monkeypatch.setattr(auth, "_dpapi_encrypt", lambda data: b"SEALED:" + data)
        monkeypatch.setattr(
            auth, "_dpapi_decrypt", lambda data: data[7:] if data.startswith(b"SEALED:") else None
        )
    else:
        monkeypatch.setattr(auth, "_dpapi_encrypt", lambda _data: None)
        monkeypatch.setattr(auth, "_dpapi_decrypt", lambda _data: None)


def test_dpapi_is_unavailable_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POSIX branch, which is what selects the mode-0600 plaintext file."""
    monkeypatch.setattr(auth, "os", _FakeOsName("posix"))
    assert auth._dpapi_encrypt(b"secret") is None
    assert auth._dpapi_decrypt(b"secret") is None


def test_posix_save_also_chmods_the_file(
    _store_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.open's mode is honoured differently across platforms, so chmod is belt and braces."""
    chmods: list[tuple[object, int]] = []
    fake = _FakeOsName("posix")
    fake.chmod = lambda path, mode: chmods.append((path, mode))  # type: ignore[attr-defined]
    monkeypatch.setattr(auth, "os", fake)
    assert save_credential(Credential(url=URL, token=TOKEN))[0] is True
    assert chmods == [(_store_in_tmp / "credentials.json", 0o600)]


def test_the_sealed_path_round_trips(_store_in_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dpapi(monkeypatch, available=True)
    saved, where = save_credential(Credential(url=URL, token=TOKEN))
    assert saved is True
    assert "DPAPI-encrypted" in where
    outer = json.loads((_store_in_tmp / "credentials.json").read_text(encoding="utf-8"))
    assert outer["encryption"] == "dpapi"
    assert load_token(URL) == TOKEN


def test_the_plaintext_fallback_round_trips(
    _store_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where DPAPI is unavailable the file is base64 at mode 0600, and still works."""
    _fake_dpapi(monkeypatch, available=False)
    saved, where = save_credential(Credential(url=URL, token=TOKEN))
    assert saved is True
    assert "plaintext, mode 0600" in where
    outer = json.loads((_store_in_tmp / "credentials.json").read_text(encoding="utf-8"))
    assert outer["encryption"] == "none"
    assert load_token(URL) == TOKEN


def test_forget_preserves_the_sealed_encoding(
    _store_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_dpapi(monkeypatch, available=True)
    save_credential(Credential(url=URL, token=TOKEN))
    save_credential(Credential(url="https://two.example.com", token=OTHER_TOKEN))
    assert forget(URL) is True
    outer = json.loads((_store_in_tmp / "credentials.json").read_text(encoding="utf-8"))
    assert outer["encryption"] == "dpapi"
    assert load_token("https://two.example.com") == OTHER_TOKEN


def test_forget_preserves_the_plaintext_encoding(
    _store_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_dpapi(monkeypatch, available=False)
    save_credential(Credential(url=URL, token=TOKEN))
    save_credential(Credential(url="https://two.example.com", token=OTHER_TOKEN))
    assert forget(URL) is True
    outer = json.loads((_store_in_tmp / "credentials.json").read_text(encoding="utf-8"))
    assert outer["encryption"] == "none"
    assert load_token("https://two.example.com") == OTHER_TOKEN


def test_an_undecryptable_sealed_store_reads_as_empty(
    _store_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file sealed for a different OS user must be ignored, not crash the run."""
    _fake_dpapi(monkeypatch, available=True)
    save_credential(Credential(url=URL, token=TOKEN))
    monkeypatch.setattr(auth, "_dpapi_decrypt", lambda _data: None)
    assert _load_all() == {}
    assert load_token(URL) is None


def test_save_reports_failure_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyring problem must degrade to 'not saved', never lose a verified login."""

    def boom(*_a: object, **_k: object) -> int:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "open", boom)
    saved, why = save_credential(Credential(url=URL, token=TOKEN))
    assert saved is False
    assert "could not write" in why


# -- tolerating a damaged store -------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "{}",  # no data key
        '{"data": "!!!not base64!!!"}',
        '{"version": 1, "encryption": "none", "data": "'
        + base64.b64encode(b"junk").decode()
        + '"}',
        '{"version": 1, "encryption": "none", "data": "'
        + base64.b64encode(b'{"credentials": "not a dict"}').decode()
        + '"}',
    ],
)
def test_a_damaged_store_reads_as_empty(_store_in_tmp: Path, content: str) -> None:
    """A broken credential file must not block an otherwise-working analysis."""
    _store_in_tmp.mkdir(parents=True, exist_ok=True)
    (_store_in_tmp / "credentials.json").write_text(content, encoding="utf-8")
    assert _load_all() == {}
    assert load_token(URL) is None


def test_a_non_string_token_in_the_store_is_ignored(_store_in_tmp: Path) -> None:
    inner = json.dumps({"version": 1, "credentials": {URL.lower(): {"token": 42}}}).encode()
    _write_plaintext_store(_store_in_tmp, inner)
    assert load_token(URL) is None


def test_an_empty_token_in_the_store_is_ignored(_store_in_tmp: Path) -> None:
    inner = json.dumps({"version": 1, "credentials": {URL.lower(): {"token": ""}}}).encode()
    _write_plaintext_store(_store_in_tmp, inner)
    assert load_token(URL) is None


def test_a_non_dict_entry_in_the_store_is_ignored(_store_in_tmp: Path) -> None:
    inner = json.dumps({"version": 1, "credentials": {URL.lower(): "just a string"}}).encode()
    _write_plaintext_store(_store_in_tmp, inner)
    assert load_token(URL) is None


def _write_plaintext_store(home: Path, inner: bytes) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "credentials.json").write_text(
        json.dumps({"version": 1, "encryption": "none", "data": base64.b64encode(inner).decode()}),
        encoding="utf-8",
    )


# -- forget ---------------------------------------------------------------


def test_forget_removes_only_the_named_server(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    save_credential(Credential(url="https://two.example.com", token=OTHER_TOKEN))
    assert forget(URL) is True
    assert load_token(URL) is None
    assert load_token("https://two.example.com") == OTHER_TOKEN


def test_forget_normalises_the_url(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    assert forget("https://SONAR.EXAMPLE.COM/") is True
    assert load_token(URL) is None


def test_forget_an_unknown_server_is_false(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    assert forget("https://never-stored.example.com") is False
    assert load_token(URL) == TOKEN  # nothing was disturbed


def test_forget_with_no_store_is_false() -> None:
    assert forget(URL) is False


def test_forget_reports_failure_instead_of_raising(
    _store_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_credential(Credential(url=URL, token=TOKEN))

    def boom(*_a: object, **_k: object) -> int:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", boom)
    assert forget(URL) is False


def test_forgetting_the_last_credential_leaves_a_valid_empty_store(_store_in_tmp: Path) -> None:
    save_credential(Credential(url=URL, token=TOKEN))
    assert forget(URL) is True
    assert _load_all() == {}
    # And the file is still well-formed enough to save into again.
    assert save_credential(Credential(url=URL, token=OTHER_TOKEN))[0] is True
    assert load_token(URL) == OTHER_TOKEN


# -- body-parsing helpers -------------------------------------------------


def test_token_from_json_is_final_for_a_well_formed_object() -> None:
    """A JSON object's verdict is final, so no later strategy can second-guess it."""
    assert _token_from_json('{"token":"squ_a"}') == (True, "squ_a")
    assert _token_from_json('{"login":"x"}') == (True, None)


def test_token_from_json_declines_non_objects() -> None:
    assert _token_from_json("[1,2]") == (False, None)
    assert _token_from_json("squ_bare") == (False, None)
    assert _token_from_json("{not valid json") == (False, None)
    assert _token_from_json('{"a": 1') == (False, None)


def test_token_from_json_ignores_a_blank_value() -> None:
    assert _token_from_json('{"token":"   "}') == (True, None)


def test_token_from_json_accepts_the_value_key() -> None:
    assert _token_from_json('{"value":" squ_v "}') == (True, "squ_v")


def test_token_from_form_by_declared_type() -> None:
    assert _token_from_form("token=squ_a", "application/x-www-form-urlencoded") == (True, "squ_a")


def test_token_from_form_by_shape() -> None:
    """A single-line body containing '=' is form-shaped even when mislabelled."""
    assert _token_from_form("value=squ_v", "text/plain") == (True, "squ_v")


def test_token_from_form_declines_a_body_that_is_not_form_shaped() -> None:
    assert _token_from_form("squ_bare", "text/plain") == (False, None)
    assert _token_from_form("a=1\nb=2", "text/plain") == (False, None)


def test_token_from_form_declines_when_no_token_key_is_present() -> None:
    assert _token_from_form("other=x", "application/x-www-form-urlencoded") == (False, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("squ_abc", "squ_abc"),
        ("", None),
        ("two\nlines", None),
        ("<html/>", None),
        ("[1,2]", None),
        ("x" * 201, None),
        ("x" * 200, "x" * 200),
    ],
)
def test_bare_token(text: str, expected: str | None) -> None:
    assert _bare_token(text) == expected


# -- port helpers ---------------------------------------------------------


def test_port_available_is_true_when_the_range_is_free() -> None:
    assert port_available() is True


def test_port_available_is_false_when_every_port_is_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Sock:
        def __enter__(self) -> _Sock:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def bind(self, _addr: tuple[str, int]) -> None:
            raise OSError("in use")

    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: _Sock())
    assert port_available() is False


def test_bind_listener_uses_a_port_in_the_ide_window() -> None:
    """The server only redirects to loopback ports in this range, so it is not ours to pick."""
    server, port = _bind_listener()
    try:
        assert port in PORT_RANGE
    finally:
        server.server_close()


def test_bind_listener_skips_busy_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first two ports are refused, so it must walk on rather than give up.

    Which real ports happen to be free varies by machine, so the socket layer is
    replaced entirely and only the walk order is asserted.
    """
    tried: list[int] = []

    def flaky(address: tuple[str, int], _handler: object) -> object:
        tried.append(address[1])
        if len(tried) <= 2:
            raise OSError("address already in use")
        return object()

    monkeypatch.setattr(auth, "HTTPServer", flaky)
    _server, port = _bind_listener()
    assert tried == [PORT_RANGE.start, PORT_RANGE.start + 1, PORT_RANGE.start + 2]
    assert port == PORT_RANGE.start + 2


def test_bind_listener_raises_when_the_whole_range_is_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def busy(*_a: object, **_k: object) -> None:
        raise OSError("address already in use")

    monkeypatch.setattr(auth, "HTTPServer", busy)
    with pytest.raises(AuthError, match="no free port in 64120-64130"):
        _bind_listener()


# -- grant_token end to end ----------------------------------------------


def _post(port: int, body: bytes, content_type: str = "text/plain;charset=UTF-8") -> int:
    """POST to the loopback listener the way SonarQube's browser page does."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/sonarlint/api/token",
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Origin": "https://sonar.example.com"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _get(port: int, path: str) -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


# The exact body SonarQube Server 2025.6.1 POSTs to /sonarlint/api/token: JSON, but
# labelled text/plain.
REAL_PAYLOAD = json.dumps(
    {
        "login": "example-user",
        "name": "SonarLint-pysonarlint-1",
        "createdAt": "2026-09-10T10:00:06-0400",
        "expirationDate": "2027-09-09T20:00:00-0400",
        "token": TOKEN,
        "type": "USER_TOKEN",
    }
).encode()


def _drive_grant(
    monkeypatch: pytest.MonkeyPatch,
    client,
    *,
    timeout: float = 5.0,
) -> str:
    """Run grant_token, firing `client(port)` from a thread once the URL is known."""
    monkeypatch.setattr(auth.webbrowser, "open", lambda _url: pytest.fail("no browser in tests"))
    threads: list[threading.Thread] = []

    def on_url(url: str) -> None:
        port = int(url.rsplit("port=", 1)[1])
        thread = threading.Thread(target=client, args=(port,), daemon=True)
        threads.append(thread)
        thread.start()

    try:
        return grant_token(URL, timeout=timeout, open_browser=False, on_url=on_url)
    finally:
        for thread in threads:
            thread.join(timeout=5)


def test_grant_token_receives_the_real_sonarqube_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body is JSON but labelled text/plain, which is the whole reason it is sniffed."""
    statuses: list[int] = []
    assert (
        _drive_grant(monkeypatch, lambda port: statuses.append(_post(port, REAL_PAYLOAD))) == TOKEN
    )
    assert statuses == [200]


def test_grant_token_accepts_a_form_encoded_post(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _drive_grant(
        monkeypatch,
        lambda port: _post(port, b"token=" + TOKEN.encode(), "application/x-www-form-urlencoded"),
    )
    assert token == TOKEN


def test_the_url_names_us_and_the_bound_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def client(port: int) -> None:
        _post(port, REAL_PAYLOAD)

    def on_url(url: str) -> None:
        seen.append(url)
        threading.Thread(target=client, args=(int(url.rsplit("port=", 1)[1]),), daemon=True).start()

    monkeypatch.setattr(auth.webbrowser, "open", lambda _u: pytest.fail("no browser in tests"))
    grant_token(URL, timeout=5.0, open_browser=False, on_url=on_url)
    assert seen[0].startswith(f"{URL}/sonarlint/auth?ideName={IDE_NAME}&port=")
    assert int(seen[0].rsplit("port=", 1)[1]) in PORT_RANGE


def test_the_status_probe_identifies_us_before_the_token_is_posted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server probes for a live IDE first, so GET must be handled as well as POST."""
    probes: list[dict[str, object]] = []

    def client(port: int) -> None:
        status, body = _get(port, "/sonarlint/api/status")
        probes.append({"status": status, "body": json.loads(body)})
        _post(port, REAL_PAYLOAD)

    assert _drive_grant(monkeypatch, client) == TOKEN
    assert probes[0]["status"] == 200
    assert probes[0]["body"]["ideName"] == IDE_NAME


def test_a_non_status_get_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bytes] = []

    def client(port: int) -> None:
        seen.append(_get(port, "/")[1])
        _post(port, REAL_PAYLOAD)

    _drive_grant(monkeypatch, client)
    assert b"listening" in seen[0]


def test_an_options_preflight_is_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browsers send a CORS preflight, including the private-network one."""
    headers: list[dict[str, str]] = []

    def client(port: int) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/sonarlint/api/token",
            method="OPTIONS",
            headers={"Origin": "https://sonar.example.com"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            headers.append(dict(resp.headers))
        _post(port, REAL_PAYLOAD)

    _drive_grant(monkeypatch, client)
    assert headers[0]["Access-Control-Allow-Origin"] == "https://sonar.example.com"
    assert headers[0]["Access-Control-Allow-Private-Network"] == "true"


def test_a_post_without_a_token_is_rejected_and_the_wait_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses: list[int] = []

    def client(port: int) -> None:
        statuses.append(_post(port, b'{"login":"x"}'))  # no token field
        statuses.append(_post(port, REAL_PAYLOAD))

    assert _drive_grant(monkeypatch, client) == TOKEN
    assert statuses == [400, 200]


def test_an_empty_post_body_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses: list[int] = []

    def client(port: int) -> None:
        statuses.append(_post(port, b""))
        statuses.append(_post(port, REAL_PAYLOAD))

    _drive_grant(monkeypatch, client)
    assert statuses[0] == 400


def test_grant_token_times_out_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth.webbrowser, "open", lambda _u: pytest.fail("no browser in tests"))
    # The GET both proves the listener is really serving and leaves `done` unset, so the
    # wait expires deterministically rather than on a wall-clock guess.
    with pytest.raises(AuthError) as exc:
        _drive_grant(monkeypatch, lambda port: _get(port, "/"), timeout=0.15)
    assert "timed out after 0s" in str(exc.value)
    assert "SONAR_TOKEN" in str(exc.value)  # the manual escape hatch


def test_grant_token_reports_a_completed_browser_that_sent_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handshake finishing without a token must be an error, not a stored None."""

    def client(port: int) -> None:
        _get(port, "/")  # guarantees the listener is serving before `done` is forced
        auth._Handler.done.set()

    with pytest.raises(AuthError, match="sent no token"):
        _drive_grant(monkeypatch, client)


def test_grant_token_survives_a_browser_that_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless environments have no browser; the URL was printed, so carry on waiting."""
    opened: list[str] = []

    def refuse(url: str) -> bool:
        opened.append(url)
        raise RuntimeError("no display")

    monkeypatch.setattr(auth.webbrowser, "open", refuse)
    threads: list[threading.Thread] = []

    def on_url(url: str) -> None:
        port = int(url.rsplit("port=", 1)[1])
        thread = threading.Thread(target=_post, args=(port, REAL_PAYLOAD), daemon=True)
        threads.append(thread)
        thread.start()

    try:
        assert grant_token(URL, timeout=5.0, open_browser=True, on_url=on_url) == TOKEN
    finally:
        for thread in threads:
            thread.join(timeout=5)
    assert opened  # it was attempted, and the failure was swallowed


def test_the_listener_is_released_after_a_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked listener would occupy a port in the small window the server redirects to."""
    ports: list[int] = []

    def client(port: int) -> None:
        ports.append(port)
        _post(port, REAL_PAYLOAD)

    _drive_grant(monkeypatch, client)
    # Probed by connecting rather than by re-binding. The grant just completed a TCP
    # exchange on this port, so on POSIX the closed server socket sits in TIME_WAIT and
    # a plain bind() fails even though nothing is listening. SO_REUSEADDR would mask
    # that, but on Windows it also permits binding over a *live* listener, which is the
    # very thing this test exists to catch. A refused connection means released on both.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        assert sock.connect_ex(("127.0.0.1", ports[0])) != 0  # nonzero == nothing accepting
