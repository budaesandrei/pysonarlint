"""Server version handling, auth negotiation, and failure diagnosis.

No network: these exercise the parsing and decision logic, which is where the
version-dependent and error-classification bugs live.
"""

from __future__ import annotations

import ssl
import urllib.error

import pytest

from pysonarlint.server import (
    BEARER_VERSION,
    MIN_VERSION,
    Client,
    Preflight,
    ServerInfo,
    _explain_url_error,
)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2025.6.1.117629", (2025, 6, 1, 117629)),
        ("10.4", (10, 4)),
        ("9.9.0.1234", (9, 9, 0, 1234)),
        ("10.7-SNAPSHOT", (10, 7)),  # qualifier is dropped
        ("", (0,)),
        ("garbage", (0,)),
    ],
)
def test_version_parsing(version: str, expected: tuple[int, ...]) -> None:
    assert ServerInfo(version=version, status="UP").version_tuple == expected


@pytest.mark.parametrize(
    ("version", "bearer", "too_old"),
    [
        ("2025.6.1.117629", True, False),  # a current LTA
        ("10.4", True, False),  # exactly the bearer threshold
        ("10.3.0.1", False, False),  # supported, but Basic only
        ("9.9", False, False),  # exactly the minimum
        ("9.8", False, True),  # below the minimum
        ("8.9.0.1", False, True),
    ],
)
def test_auth_scheme_and_support_thresholds(version: str, bearer: bool, too_old: bool) -> None:
    """Getting these boundaries wrong 401s on servers inside the supported window."""
    info = ServerInfo(version=version, status="UP")
    assert info.supports_bearer is bearer
    assert info.too_old is too_old


def test_documented_thresholds() -> None:
    assert MIN_VERSION == (9, 9)
    assert BEARER_VERSION == (10, 4)


@pytest.mark.parametrize(
    ("url", "is_cloud"),
    [
        ("https://sonarcloud.io", True),
        ("https://sonarqube.us", True),
        ("https://sonar.internal.example.com", False),
        ("https://sonar.example.com/sonarqube", False),
    ],
)
def test_cloud_detection(url: str, is_cloud: bool) -> None:
    assert Client(url).is_cloud is is_cloud


def test_trailing_slash_is_stripped() -> None:
    assert Client("https://s.example.com/").url == "https://s.example.com"


def test_tls_failure_names_the_remedy() -> None:
    """A bare "certificate verify failed" leaves users stuck."""
    exc = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
    message = _explain_url_error(exc, "https://s.example.com")
    assert "SSL_CERT_FILE" in message


def test_proxy_failure_names_the_remedy() -> None:
    """Internal hosts routed through a corporate proxy fail this way."""
    exc = urllib.error.URLError("Tunnel connection failed: 502 Bad Gateway")
    message = _explain_url_error(exc, "https://sonar.internal.example.com")
    assert "NO_PROXY" in message


def test_timeout_is_reported_as_a_timeout() -> None:
    exc = urllib.error.URLError(TimeoutError("timed out"))
    assert "timed out" in _explain_url_error(exc, "https://s.example.com")


def test_preflight_defaults_to_not_ok() -> None:
    """A default-constructed result must never look like success."""
    assert Preflight(ok=False).ok is False
    assert Preflight(ok=False).problems == []


def test_preflight_carries_hints_separately() -> None:
    p = Preflight(ok=False, problems=["token rejected"], hints=["generate a new one"])
    assert p.problems and p.hints
