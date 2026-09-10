"""HTTP behaviour of the Web API client, with urlopen replaced by a scripted fake.

No network. The fake records every Request object, so the auth scheme actually put on
the wire is asserted rather than inferred.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from pysonarlint.server import (
    Client,
    ServerError,
    ServerInfo,
    preflight,
    project_exists,
)

TOKEN = "squ_EXAMPLEfakeTOKENvalueNOTaREALsecret00"


class _Response:
    """The subset of an http.client.HTTPResponse that the client touches."""

    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeHttp:
    """Answers by URL suffix. Values may be a _Response, an exception, or a callable."""

    def __init__(self, routes: dict[str, Any], default: Any = None) -> None:
        self.routes = routes
        self.default = default if default is not None else _Response(404, b"")
        self.requests: list[urllib.request.Request] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
        monkeypatch.setattr(urllib.request, "urlopen", self)
        return self

    def __call__(self, req: urllib.request.Request, **_kwargs: object) -> _Response:
        self.requests.append(req)
        answer = self.default
        for suffix, value in self.routes.items():
            if suffix in req.full_url:
                answer = value
                break
        if callable(answer) and not isinstance(answer, _Response):
            answer = answer(req, len(self.requests))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def urls(self) -> list[str]:
        return [r.full_url for r in self.requests]

    def auth_headers(self) -> list[str | None]:
        return [r.get_header("Authorization") for r in self.requests]


def _ok(payload: object) -> _Response:
    return _Response(200, json.dumps(payload).encode())


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://s.example.com/x", code, "err", {}, None)  # type: ignore[arg-type]


class _ReadableHttpError(urllib.error.HTTPError):
    """HTTPError with a body, which is how the client learns why a call failed."""

    def __init__(self, code: int, body: bytes) -> None:
        super().__init__("https://s.example.com/x", code, "err", {}, None)  # type: ignore[arg-type]
        self._payload = body

    def read(self) -> bytes:  # type: ignore[override]
        return self._payload


# -- _request --------------------------------------------------------------


def test_request_sends_our_user_agent_and_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({"api/system/status": _ok({"version": "10.5", "status": "UP"})})
    http.install(monkeypatch)
    Client("https://s.example.com").status()
    req = http.requests[0]
    assert req.get_header("User-agent") == "pysonarlint"
    assert req.get_header("Accept") == "application/json"
    assert req.get_method() == "GET"


def test_request_joins_the_path_without_doubling_the_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp({}, default=_ok({"version": "10.5", "status": "UP"})).install(monkeypatch)
    Client("https://s.example.com/")._request("/api/system/status")
    assert http.urls == ["https://s.example.com/api/system/status"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://s.example.com",
        "gopher://s.example.com",
    ],
)
def test_non_http_schemes_are_refused(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The URL comes from project files, and the request carries an Authorization header."""
    http = FakeHttp({}).install(monkeypatch)
    with pytest.raises(ServerError, match="non-HTTP"):
        Client(url, TOKEN)._request("api/system/status")
    assert http.requests == []  # nothing was attempted


def test_plain_http_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({}, default=_ok({"version": "10.5", "status": "UP"})).install(monkeypatch)
    assert Client("http://s.example.com").status().version == "10.5"


def test_http_error_is_returned_as_a_status_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({}, default=_ReadableHttpError(403, b'{"errors":[]}')).install(monkeypatch)
    code, body, _ = Client("https://s.example.com")._request("api/whatever")
    assert code == 403
    assert body == b'{"errors":[]}'


def test_http_error_with_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({}, default=_http_error(401)).install(monkeypatch)
    code, body, _ = Client("https://s.example.com")._request("api/whatever")
    assert (code, body) == (401, b"")


def test_url_error_is_translated_to_a_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({}, default=urllib.error.URLError("name or service not known")).install(monkeypatch)
    with pytest.raises(ServerError, match="name or service not known"):
        Client("https://s.example.com")._request("api/whatever")


def test_os_error_is_translated_to_a_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """TimeoutError is an OSError subclass, which is the common case here."""
    FakeHttp({}, default=TimeoutError("timed out")).install(monkeypatch)
    with pytest.raises(ServerError, match="timed out"):
        Client("https://s.example.com")._request("api/whatever")


# -- _json -----------------------------------------------------------------


def test_json_returns_none_for_an_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({}, default=_Response(204, b"")).install(monkeypatch)
    assert Client("https://s.example.com")._json("api/x") == (204, None)


def test_json_returns_none_for_an_unparseable_body(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({}, default=_Response(200, b"<html>a proxy login page</html>")).install(monkeypatch)
    assert Client("https://s.example.com")._json("api/x") == (200, None)


# -- auth scheme -----------------------------------------------------------


def test_no_authorization_header_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({}, default=_ok({"valid": True})).install(monkeypatch)
    Client("https://s.example.com")._request("api/x")
    assert http.auth_headers() == [None]


def test_anonymous_request_omits_the_header_even_with_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp({}, default=_ok({})).install(monkeypatch)
    Client("https://s.example.com", TOKEN)._request("api/x", authed=False)
    assert http.auth_headers() == [None]


def test_bearer_is_the_default_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({}, default=_ok({})).install(monkeypatch)
    client = Client("https://s.example.com", TOKEN)
    client._request("api/x")
    assert client._bearer is True
    assert http.auth_headers() == [f"Bearer {TOKEN}"]


def test_basic_auth_sends_the_token_as_the_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older servers want Basic with the token as user and an empty password."""
    import base64

    http = FakeHttp({}, default=_ok({})).install(monkeypatch)
    client = Client("https://s.example.com", TOKEN)
    client._bearer = False
    client._request("api/x")
    header = http.auth_headers()[0]
    assert header is not None and header.startswith("Basic ")
    assert base64.b64decode(header.split()[1]).decode() == f"{TOKEN}:"


# -- status() --------------------------------------------------------------


def test_status_from_the_anonymous_call(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp(
        {"api/system/status": _ok({"version": "2025.6.1.117629", "status": "UP"})}
    ).install(monkeypatch)
    info = Client("https://s.example.com", TOKEN).status()
    assert (info.version, info.status, info.reachable) == ("2025.6.1.117629", "UP", True)
    assert http.auth_headers() == [None]  # deliberately anonymous


def test_status_defaults_the_status_field_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({"api/system/status": _ok({"version": "10.5"})}).install(monkeypatch)
    assert Client("https://s.example.com").status().status == "UNKNOWN"


def test_status_retries_with_auth_when_the_anonymous_call_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some instances require auth even for api/system/status."""

    def answer(req: urllib.request.Request, _n: int) -> Any:
        if req.get_header("Authorization"):
            return _ok({"version": "10.5", "status": "UP"})
        return _http_error(401)

    http = FakeHttp({"api/system/status": answer}).install(monkeypatch)
    info = Client("https://s.example.com", TOKEN).status()
    assert info.version == "10.5"
    assert http.auth_headers() == [None, f"Bearer {TOKEN}"]


def test_status_falls_back_to_api_server_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Very old or oddly-proxied instances only answer the plain-text version endpoint."""
    http = FakeHttp(
        {
            "api/system/status": _http_error(404),
            "api/server/version": _Response(200, b"9.9.4.87374\n"),
        }
    ).install(monkeypatch)
    info = Client("https://s.example.com", TOKEN).status()
    assert (info.version, info.status) == ("9.9.4.87374", "UP")
    assert "api/server/version" in http.urls[-1]


def test_status_rejects_a_non_numeric_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTML login page must not be read as a version string."""
    FakeHttp(
        {
            "api/system/status": _http_error(404),
            "api/server/version": _Response(200, b"<html>Sign in</html>"),
        }
    ).install(monkeypatch)
    with pytest.raises(ServerError, match="did not answer as a SonarQube API"):
        Client("https://s.example.com").status()


def test_status_rejects_an_empty_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp(
        {
            "api/system/status": _http_error(500),
            "api/server/version": _Response(200, b"   "),
        }
    ).install(monkeypatch)
    with pytest.raises(ServerError, match="HTTP 500"):
        Client("https://s.example.com").status()


def test_status_rejects_a_non_dict_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp(
        {"api/system/status": _ok(["not", "a", "dict"]), "api/server/version": _Response(200, b"x")}
    ).install(monkeypatch)
    with pytest.raises(ServerError):
        Client("https://s.example.com").status()


# -- validate_token() ------------------------------------------------------


def test_validate_token_is_false_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({}).install(monkeypatch)
    assert Client("https://s.example.com").validate_token() is False
    assert http.requests == []  # no pointless round trip


def test_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({"api/authentication/validate": _ok({"valid": True})}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).validate_token() is True
    assert "format=json" in http.urls[0]


def test_http_200_with_valid_false_means_a_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load-bearing: the endpoint answers 200 for a rejected token, so the body decides."""
    FakeHttp({"api/authentication/validate": _ok({"valid": False})}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).validate_token() is False


@pytest.mark.parametrize("code", [401, 403])
def test_validate_token_is_false_on_an_auth_failure(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeHttp({"api/authentication/validate": _http_error(code)}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).validate_token() is False


def test_validate_token_is_false_on_an_unexpected_status(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({"api/authentication/validate": _ReadableHttpError(500, b"boom")}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).validate_token() is False


def test_validate_token_is_false_when_the_body_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeHttp({"api/authentication/validate": _Response(200, b"true")}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).validate_token() is False


# -- check_browse() --------------------------------------------------------


def test_check_browse_probes_the_project_when_a_key_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp({}, default=_ok({"component": {}})).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).check_browse("my proj/key") is True
    assert "api/components/show?component=my%20proj/key" in http.urls[0]


def test_check_browse_falls_back_to_installed_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({}, default=_ok({"plugins": []})).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).check_browse(None) is True
    assert "api/plugins/installed" in http.urls[0]


def test_check_browse_403_means_an_analysis_token(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({}, default=_http_error(403)).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).check_browse("p") is False


@pytest.mark.parametrize("code", [404, 500])
def test_check_browse_is_indeterminate_on_other_statuses(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None, not False: we only ever report a definite verdict."""
    FakeHttp({}, default=_http_error(code)).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).check_browse("p") is None


def test_check_browse_is_indeterminate_on_a_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeHttp({}, default=urllib.error.URLError("down")).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).check_browse("p") is None


# -- quality_profiles() ----------------------------------------------------


def test_quality_profiles_for_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp(
        {"api/qualityprofiles/search": _ok({"profiles": [{"key": "a"}, {"key": "b"}]})}
    ).install(monkeypatch)
    profiles = Client("https://s.example.com", TOKEN).quality_profiles("my:proj")
    assert [p["key"] for p in profiles] == ["a", "b"]
    assert "?project=my%3Aproj" in http.urls[0]


def test_quality_profiles_without_a_project_key(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({"api/qualityprofiles/search": _ok({"profiles": []})}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).quality_profiles(None) == []
    assert http.urls[0].endswith("api/qualityprofiles/search")


def test_quality_profiles_drops_non_object_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({"api/qualityprofiles/search": _ok({"profiles": [{"key": "a"}, "junk"]})}).install(
        monkeypatch
    )
    assert Client("https://s.example.com", TOKEN).quality_profiles(None) == [{"key": "a"}]


@pytest.mark.parametrize(
    "answer",
    [
        _http_error(403),
        _ok({}),  # no "profiles" key at all
        _ok({"profiles": "not a list"}),
        _Response(200, b"not json"),
    ],
)
def test_quality_profiles_is_empty_when_unusable(
    answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeHttp({"api/qualityprofiles/search": answer}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).quality_profiles(None) == []


# -- resolved_issues() -----------------------------------------------------


def _issue(component: str, line: int, rule: str) -> dict[str, Any]:
    return {"component": component, "line": line, "rule": rule}


def test_resolved_issues_single_page(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp(
        {
            "api/issues/search": _ok(
                {
                    "total": 2,
                    "issues": [
                        _issue("k:src/a.py", 3, "python:S1"),
                        _issue("k:b.py", 9, "python:S2"),
                    ],
                }
            )
        }
    ).install(monkeypatch)
    found = Client("https://s.example.com", TOKEN).resolved_issues("k")
    assert found == {("src/a.py", 3, "python:S1"), ("b.py", 9, "python:S2")}
    assert len(http.requests) == 1
    assert "resolutions=FALSE-POSITIVE,WONTFIX" in http.urls[0]
    assert "ps=500&p=1" in http.urls[0]


def test_resolved_issues_paginates_until_the_total_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(req: urllib.request.Request, _n: int) -> _Response:
        page = int(req.full_url.rsplit("p=", 1)[1])
        return _ok({"total": 501, "issues": [_issue("k:f.py", page, "python:S1")]})

    http = FakeHttp({"api/issues/search": answer}).install(monkeypatch)
    found = Client("https://s.example.com", TOKEN).resolved_issues("k")
    assert len(http.requests) == 2  # 2 * 500 >= 501
    assert found == {("f.py", 1, "python:S1"), ("f.py", 2, "python:S1")}


def test_resolved_issues_stops_at_the_page_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server reporting an implausible total must not loop forever."""
    http = FakeHttp(
        {"api/issues/search": _ok({"total": 10**9, "issues": [_issue("k:f.py", 1, "r")]})}
    ).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).resolved_issues("k") == {("f.py", 1, "r")}
    assert len(http.requests) == 20  # the documented hard stop


def test_resolved_issues_stops_on_a_missing_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a usable total the loop still terminates once a page comes back empty."""

    def answer(_req: urllib.request.Request, n: int) -> _Response:
        if n == 1:
            return _ok({"issues": [_issue("k:f.py", 1, "r")]})
        return _ok({"issues": []})

    http = FakeHttp({"api/issues/search": answer}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).resolved_issues("k") == {("f.py", 1, "r")}
    assert len(http.requests) == 2


@pytest.mark.parametrize(
    "answer",
    [
        _http_error(403),
        urllib.error.URLError("down"),
        _Response(200, b"not json"),
        _ok({"issues": []}),
        _ok({"issues": "not a list"}),
    ],
)
def test_resolved_issues_is_empty_when_the_first_page_is_unusable(
    answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeHttp({"api/issues/search": answer}).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).resolved_issues("k") == set()


def test_resolved_issues_ignores_malformed_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp(
        {
            "api/issues/search": _ok(
                {
                    "total": 5,
                    "issues": [
                        "not an object",
                        {"component": "k:a.py"},  # no line, no rule
                        {"component": "k:b.py", "line": "3", "rule": "r"},  # line not an int
                        {"component": "", "line": 1, "rule": "r"},  # no path
                        _issue("k:good.py", 7, "python:S1"),
                    ],
                }
            )
        }
    ).install(monkeypatch)
    assert Client("https://s.example.com", TOKEN).resolved_issues("k") == {
        ("good.py", 7, "python:S1")
    }


# -- project_exists() ------------------------------------------------------


def test_project_exists_true(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": "UP"}),
            "api/components/show": _ok({"component": {"key": "p"}}),
        }
    ).install(monkeypatch)
    assert project_exists("https://s.example.com", TOKEN, "p") is True
    assert http.auth_headers()[-1] == f"Bearer {TOKEN}"  # 10.5 supports bearer


def test_project_exists_false_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": "UP"}),
            "api/components/show": _http_error(404),
        }
    ).install(monkeypatch)
    assert project_exists("https://s.example.com", TOKEN, "p") is False


def test_project_exists_is_indeterminate_on_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Indeterminate is treated as 'assume present', so permissions never break a run."""
    FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": "UP"}),
            "api/components/show": _http_error(403),
        }
    ).install(monkeypatch)
    assert project_exists("https://s.example.com", TOKEN, "p") is None


def test_project_exists_is_indeterminate_when_the_server_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeHttp({}, default=urllib.error.URLError("down")).install(monkeypatch)
    assert project_exists("https://s.example.com", TOKEN, "p") is None


def test_project_exists_uses_basic_on_an_older_server(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp(
        {
            "api/system/status": _ok({"version": "10.3", "status": "UP"}),
            "api/components/show": _ok({}),
        }
    ).install(monkeypatch)
    project_exists("https://s.example.com", TOKEN, "p")
    header = http.auth_headers()[-1]
    assert header is not None and header.startswith("Basic ")


def test_project_exists_uses_bearer_on_cloud_regardless_of_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        {
            "api/system/status": _ok({"version": "8.0", "status": "UP"}),
            "api/components/show": _ok({}),
        }
    ).install(monkeypatch)
    project_exists("https://sonarcloud.io", TOKEN, "p")
    assert http.auth_headers()[-1] == f"Bearer {TOKEN}"


# -- preflight() -----------------------------------------------------------


def test_preflight_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({}, default=urllib.error.URLError("name or service not known")).install(monkeypatch)
    result = preflight("https://s.example.com", TOKEN)
    assert result.ok is False
    assert result.info is None
    assert any("name or service not known" in p for p in result.problems)


def test_preflight_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({"api/system/status": _ok({"version": "8.9", "status": "UP"})}).install(monkeypatch)
    result = preflight("https://s.example.com", TOKEN)
    assert result.ok is False
    assert result.info is not None and result.info.version == "8.9"
    assert any("connected mode needs 9.9+" in p for p in result.problems)


@pytest.mark.parametrize("status", ["STARTING", "DOWN", "RESTARTING"])
def test_preflight_not_up(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({"api/system/status": _ok({"version": "10.5", "status": status})}).install(monkeypatch)
    result = preflight("https://s.example.com", TOKEN)
    assert result.ok is False
    assert any(f"server status is {status}, not UP" in p for p in result.problems)


@pytest.mark.parametrize("status", ["UP", "up", "DB_MIGRATION_NEEDED", ""])
def test_preflight_accepts_these_statuses(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": status}),
            "api/authentication/validate": _ok({"valid": True}),
            "api/components/show": _ok({}),
        }
    ).install(monkeypatch)
    assert preflight("https://s.example.com", TOKEN, "p").ok is True


def test_preflight_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp({"api/system/status": _ok({"version": "10.5", "status": "UP"})}).install(monkeypatch)
    result = preflight("https://s.example.com", None)
    assert result.ok is False
    assert result.problems == ["no token"]
    assert any("pysonarlint login" in h for h in result.hints)
    assert result.token_valid is None


def test_preflight_rejected_token(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": "UP"}),
            "api/authentication/validate": _ok({"valid": False}),
        }
    ).install(monkeypatch)
    result = preflight("https://s.example.com", TOKEN)
    assert result.ok is False
    assert result.token_valid is False
    assert result.problems == ["token rejected by the server"]
    assert any("expired or revoked" in h for h in result.hints)


def test_preflight_retries_with_basic_when_bearer_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy that drops the bearer header would otherwise look like a bad token."""

    def validate(req: urllib.request.Request, _n: int) -> _Response:
        header = req.get_header("Authorization") or ""
        return _ok({"valid": header.startswith("Basic ")})

    FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": "UP"}),
            "api/authentication/validate": validate,
            "api/components/show": _ok({}),
        }
    ).install(monkeypatch)
    result = preflight("https://s.example.com", TOKEN, "p")
    assert result.ok is True
    assert result.token_valid is True
    assert any("Basic auth but not Bearer" in h for h in result.hints)


def test_preflight_does_not_retry_basic_on_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """SonarQube Cloud is bearer-only, so a Basic retry would be pointless noise."""
    http = FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": "UP"}),
            "api/authentication/validate": _ok({"valid": False}),
        }
    ).install(monkeypatch)
    result = preflight("https://sonarcloud.io", TOKEN)
    assert result.ok is False
    validates = [u for u in http.urls if "authentication/validate" in u]
    assert len(validates) == 1


def test_preflight_does_not_retry_basic_on_a_pre_bearer_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below 10.4 the first attempt was already Basic; retrying it proves nothing."""
    http = FakeHttp(
        {
            "api/system/status": _ok({"version": "10.3", "status": "UP"}),
            "api/authentication/validate": _ok({"valid": False}),
        }
    ).install(monkeypatch)
    assert preflight("https://s.example.com", TOKEN).ok is False
    assert len([u for u in http.urls if "authentication/validate" in u]) == 1


def test_preflight_diagnoses_an_analysis_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Global or Project Analysis token authenticates but cannot read project data."""
    FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": "UP"}),
            "api/authentication/validate": _ok({"valid": True}),
            "api/components/show": _http_error(403),
        }
    ).install(monkeypatch)
    result = preflight("https://s.example.com/", TOKEN, "p")
    assert result.ok is False
    assert result.token_valid is True
    assert result.is_user_token is False
    assert any("Analysis token looks like" in p for p in result.problems)
    assert any("https://s.example.com/account/security/" in h for h in result.hints)


def test_preflight_accepts_an_indeterminate_browse_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Indeterminate must not be treated as a rejection."""
    FakeHttp(
        {
            "api/system/status": _ok({"version": "10.5", "status": "UP"}),
            "api/authentication/validate": _ok({"valid": True}),
            "api/components/show": _http_error(500),
        }
    ).install(monkeypatch)
    result = preflight("https://s.example.com", TOKEN, "p")
    assert result.ok is True
    assert result.is_user_token is None


def test_preflight_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp(
        {
            "api/system/status": _ok({"version": "2025.6.1.117629", "status": "UP"}),
            "api/authentication/validate": _ok({"valid": True}),
            "api/plugins/installed": _ok({"plugins": []}),
        }
    ).install(monkeypatch)
    result = preflight("https://s.example.com", TOKEN)
    assert result.ok is True
    assert result.problems == []
    assert result.hints == []
    assert result.is_user_token is True


def test_server_info_reachable_defaults_true() -> None:
    assert ServerInfo(version="10.5", status="UP").reachable is True
