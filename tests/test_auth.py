"""Token grant parsing and credential storage."""

from __future__ import annotations

import pytest

from pysonarlint.auth import PORT_RANGE, Credential, _Handler, _key

# The exact body SonarQube Server 2025.6.1 POSTs to /sonarlint/api/token. It is JSON
# but is labelled text/plain, which is why the body must be sniffed rather than the
# Content-Type trusted. Captured from a real grant.
REAL_PAYLOAD = (
    b'{"login":"example-user","name":"SonarLint-pysonarlint-1",'
    b'"createdAt":"2026-09-10T10:00:06-0400",'
    b'"expirationDate":"2027-09-09T20:00:00-0400",'
    b'"token":"squ_EXAMPLEfakeTOKENvalueNOTaREALsecret00","type":"USER_TOKEN"}'
)


class _FakeHandler(_Handler):
    """Exercises _extract_token without a live HTTP server."""

    def __init__(self, content_type: str) -> None:
        self.headers = {"Content-Type": content_type}  # type: ignore[assignment]


def _extract(content_type: str, body: bytes) -> str | None:
    return _Handler._extract_token(_FakeHandler(content_type), body)


def test_real_sonarqube_payload_is_parsed() -> None:
    """Regression: this arrives as text/plain, so a Content-Type check misses it and
    the whole 215-character JSON blob gets stored as if it were the token."""
    assert _extract("text/plain;charset=UTF-8", REAL_PAYLOAD) == (
        "squ_EXAMPLEfakeTOKENvalueNOTaREALsecret00"
    )


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("application/json", b'{"token":"squ_abc"}', "squ_abc"),
        ("application/json", b'{"value":"squ_v"}', "squ_v"),
        ("application/x-www-form-urlencoded", b"token=squ_form", "squ_form"),
        ("text/plain", b"squ_bare", "squ_bare"),
        # Must not be mistaken for a token.
        ("text/plain", b'{"login":"x"}', None),
        ("text/html", b"<html>error</html>", None),
        ("text/plain", b"", None),
        ("text/plain", b"line one\nline two", None),
        ("application/json", b"[1,2,3]", None),
    ],
)
def test_token_extraction(content_type: str, body: bytes, expected: str | None) -> None:
    assert _extract(content_type, body) == expected


def test_oversized_body_is_rejected() -> None:
    """A stray response body must never be stored as a credential."""
    assert _extract("text/plain", b"x" * 500) is None


def test_port_range_matches_the_ide_window() -> None:
    """The server only redirects to loopback ports in this window."""
    assert PORT_RANGE.start == 64120
    assert PORT_RANGE.stop - 1 == 64130


def test_credential_key_is_normalised() -> None:
    assert _key("https://S.example.com/") == _key("https://s.example.com")


def test_credential_defaults() -> None:
    cred = Credential(url="https://s.example.com", token="squ_x")
    assert cred.organization is None
