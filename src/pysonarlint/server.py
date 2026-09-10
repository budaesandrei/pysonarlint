"""Talk to SonarQube Server or Cloud over the Web API.

Uses urllib rather than adding an httpx/requests dependency; it honours the standard
HTTP_PROXY / HTTPS_PROXY / NO_PROXY variables, which matters because internal Sonar
hosts commonly must bypass a corporate proxy.

Auth scheme is negotiated the way SonarLint does it: an anonymous status call first,
then Bearer for SonarQube Cloud or Server 10.4+, Basic (token as username, empty
password) for older servers.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# Below this, connected mode is not supported by SonarLint at all.
MIN_VERSION = (9, 9)
# At and above this, SonarQube Server accepts bearer tokens.
BEARER_VERSION = (10, 4)

_TIMEOUT = 30.0


class ServerError(RuntimeError):
    """A request failed in a way the user needs to know about."""


@dataclass
class ServerInfo:
    version: str
    status: str
    reachable: bool = True

    @property
    def version_tuple(self) -> tuple[int, ...]:
        """Numeric version parts, ignoring any qualifier.

        A release like "10.7-SNAPSHOT" must compare as (10, 7); stopping at the first
        non-numeric chunk would yield (10,) and wrongly place it below 10.4.
        """
        parts: list[int] = []
        for chunk in self.version.split("."):
            leading = re.match(r"\d+", chunk)
            if not leading:
                break
            parts.append(int(leading.group()))
            if leading.group() != chunk:
                break  # qualifier reached, e.g. "7-SNAPSHOT"
        return tuple(parts) or (0,)

    @property
    def supports_bearer(self) -> bool:
        return self.version_tuple >= BEARER_VERSION

    @property
    def too_old(self) -> bool:
        return self.version_tuple < MIN_VERSION


@dataclass
class Preflight:
    """Everything learned before trusting a connection."""

    ok: bool
    info: ServerInfo | None = None
    token_valid: bool | None = None
    is_user_token: bool | None = None
    problems: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


class Client:
    """Minimal Web API client."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        organization: str | None = None,
        timeout: float = _TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.organization = organization
        self.timeout = timeout
        self._bearer: bool | None = None
        # TLS verification is never disabled. A private CA is supported the standard
        # way, by pointing SSL_CERT_FILE at a bundle, which keeps verification on.
        # An opt-out flag here would be the easiest possible foot-gun for a tool that
        # carries a credential.
        self._ctx: ssl.SSLContext | None = None

    @property
    def is_cloud(self) -> bool:
        return "sonarcloud.io" in self.url or "sonarqube.us" in self.url

    def _request(self, path: str, *, authed: bool = True) -> tuple[int, bytes, dict[str, str]]:
        url = f"{self.url}/{path.lstrip('/')}"
        # The server URL comes from project files and the environment, so the scheme is
        # attacker-influenceable in principle. urlopen would happily accept file: or
        # ftp:, which combined with the Authorization header below is how a credential
        # ends up somewhere unintended.
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ServerError(f"refusing to request a non-HTTP(S) URL: {url}")
        req = urllib.request.Request(url, method="GET")  # nosec B310 - scheme checked above
        req.add_header("User-Agent", "pysonarlint")
        req.add_header("Accept", "application/json")
        if authed and self.token:
            if self._bearer is None:
                # Default to bearer; negotiation happens in preflight.
                self._bearer = True
            if self._bearer:
                req.add_header("Authorization", f"Bearer {self.token}")
            else:
                raw = base64.b64encode(f"{self.token}:".encode()).decode()
                req.add_header("Authorization", f"Basic {raw}")
        try:
            with urllib.request.urlopen(  # nosec B310 - scheme checked above
                req, timeout=self.timeout, context=self._ctx
            ) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b"", dict(exc.headers or {})
        except urllib.error.URLError as exc:
            raise ServerError(_explain_url_error(exc, url)) from exc
        except OSError as exc:  # TimeoutError is an OSError subclass
            raise ServerError(f"{url}: {exc}") from exc

    def _json(self, path: str, *, authed: bool = True) -> tuple[int, Any]:
        status, body, _ = self._request(path, authed=authed)
        if not body:
            return status, None
        try:
            return status, json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, None

    # -- individual calls --------------------------------------------------

    def status(self) -> ServerInfo:
        """Anonymous. Distinguishes 'unreachable' from 'bad token'."""
        code, data = self._json("api/system/status", authed=False)
        if code == 200 and isinstance(data, dict):
            return ServerInfo(
                version=str(data.get("version", "")), status=str(data.get("status", "UNKNOWN"))
            )
        # Some instances require auth even here.
        code, data = self._json("api/system/status", authed=True)
        if code == 200 and isinstance(data, dict):
            return ServerInfo(
                version=str(data.get("version", "")), status=str(data.get("status", "UNKNOWN"))
            )
        _, raw, _ = self._request("api/server/version", authed=True)
        text = raw.decode("utf-8", "replace").strip()
        if text and text[0].isdigit():
            return ServerInfo(version=text, status="UP")
        raise ServerError(
            f"{self.url} did not answer as a SonarQube API "
            f"(HTTP {code} from api/system/status). Check the URL."
        )

    def validate_token(self) -> bool:
        """Returns HTTP 200 with {"valid": false} for a bad token, so parse the body."""
        if not self.token:
            return False
        code, data = self._json("api/authentication/validate?format=json")
        if code in (401, 403):
            return False
        if code == 200 and isinstance(data, dict):
            return bool(data.get("valid"))
        return False

    def check_browse(self, project_key: str | None) -> bool | None:
        """Probe a Browse-gated endpoint to tell a User token from an analysis token.

        None means indeterminate rather than false; we only report a definite verdict.
        """
        path = (
            f"api/components/show?component={urllib.parse.quote(project_key)}"
            if project_key
            else "api/plugins/installed"
        )
        try:
            code, _ = self._json(path)
        except ServerError:
            return None
        if code == 200:
            return True
        if code == 403:
            return False
        return None

    def quality_profiles(self, project_key: str | None) -> list[dict[str, Any]]:
        path = "api/qualityprofiles/search"
        if project_key:
            path += f"?project={urllib.parse.quote(project_key)}"
        code, data = self._json(path)
        if code == 200 and isinstance(data, dict):
            profiles = data.get("profiles")
            if isinstance(profiles, list):
                return [p for p in profiles if isinstance(p, dict)]
        return []

    def resolved_issues(self, project_key: str) -> set[tuple[str, int, str]]:
        """Server-side issues marked false positive or won't fix.

        Returned as (file path suffix, line, rule) so local findings can be suppressed
        the way the IDE suppresses them.
        """
        out: set[tuple[str, int, str]] = set()
        page = 1
        while page <= 20:  # hard stop; each page is 500
            path = (
                "api/issues/search"
                f"?componentKeys={urllib.parse.quote(project_key)}"
                "&resolutions=FALSE-POSITIVE,WONTFIX"
                f"&ps=500&p={page}"
            )
            data = self._issues_page(path)
            if data is None:
                return out
            out.update(_resolved_keys(data["issues"]))
            total = data["total"]
            if isinstance(total, int) and page * 500 >= total:
                return out
            page += 1
        return out

    def _issues_page(self, path: str) -> dict[str, Any] | None:
        """One page of issues as {"issues": [...], "total": ...}, or None to stop."""
        try:
            code, data = self._json(path)
        except ServerError:
            return None
        if code != 200 or not isinstance(data, dict):
            return None
        issues = data.get("issues")
        if not isinstance(issues, list) or not issues:
            return None
        return {"issues": issues, "total": data.get("total")}


def _resolved_keys(issues: list[Any]) -> set[tuple[str, int, str]]:
    """Turn a page of issue objects into (file path suffix, line, rule) keys."""
    out: set[tuple[str, int, str]] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        component = str(issue.get("component", ""))
        _, _, rel = component.partition(":")
        line = issue.get("line")
        rule = str(issue.get("rule", ""))
        if rel and isinstance(line, int) and rule:
            out.add((rel, line, rule))
    return out


def project_exists(url: str, token: str, project_key: str) -> bool | None:
    """Whether a project key resolves on the server.

    None means indeterminate (network or permissions), which is treated as "assume
    present" so a transient failure never downgrades a working binding.
    """
    client = Client(url, token)
    try:
        info = client.status()
        client._bearer = client.is_cloud or info.supports_bearer
        code, _ = client._json(f"api/components/show?component={urllib.parse.quote(project_key)}")
    except ServerError:
        return None
    if code == 200:
        return True
    if code == 404:
        return False
    return None


def preflight(url: str, token: str | None, project_key: str | None = None) -> Preflight:
    """Check reachability, version and token before relying on connected mode.

    Produces specific diagnoses instead of a generic failure, because the three common
    causes (network, stale token, wrong token type) need different fixes.
    """
    client = Client(url, token)
    result = Preflight(ok=False)

    try:
        info = client.status()
    except ServerError as exc:
        result.problems.append(str(exc))
        return result

    result.info = info
    if info.too_old:
        result.problems.append(
            f"server is {info.version}; connected mode needs {'.'.join(map(str, MIN_VERSION))}+"
        )
        return result
    if info.status.upper() not in ("UP", "DB_MIGRATION_NEEDED", ""):
        result.problems.append(f"server status is {info.status}, not UP")
        return result

    # Negotiate the auth scheme exactly as SonarLint does.
    client._bearer = client.is_cloud or info.supports_bearer

    if not token:
        result.problems.append("no token")
        result.hints.append("run 'pysonarlint login' or set SONAR_TOKEN")
        return result

    valid = client.validate_token()
    if not valid and not client.is_cloud and info.supports_bearer:
        # Retry with Basic in case a proxy strips bearer headers.
        client._bearer = False
        valid = client.validate_token()
        if valid:
            result.hints.append("server accepted Basic auth but not Bearer")

    result.token_valid = valid
    if not valid:
        result.problems.append("token rejected by the server")
        result.hints.append("the token may be expired or revoked; generate a new User token")
        return result

    browse = client.check_browse(project_key)
    result.is_user_token = browse
    if browse is False:
        result.problems.append(
            "the token authenticates but cannot read project data, "
            "which is what a Global or Project Analysis token looks like"
        )
        result.hints.append(
            "connected mode requires a User token: "
            f"{url.rstrip('/')}/account/security/ -> Generate Tokens -> Type: User"
        )
        return result

    result.ok = True
    return result


def _explain_url_error(exc: urllib.error.URLError, url: str) -> str:
    """Turn a transport failure into an actionable message."""
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return (
            f"{url}: TLS certificate verification failed. If this host uses a corporate CA, "
            "set SSL_CERT_FILE to a PEM bundle that includes it."
        )
    if "Tunnel connection failed" in text or "502" in text:
        return (
            f"{url}: the HTTP proxy refused to tunnel to this host. "
            "Internal hosts usually need to bypass it: add the host to NO_PROXY."
        )
    if isinstance(reason, TimeoutError) or "timed out" in text:
        return f"{url}: connection timed out"
    return f"{url}: {text}"
