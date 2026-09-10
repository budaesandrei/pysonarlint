"""Resolve where to analyze and, optionally, which server to bind to.

Connected mode is strictly opt-in enrichment: it activates only when BOTH a server URL
and a token resolve. Partial configuration degrades to standalone with a note, never a
prompt and never an error, so `pysonarlint` in a fresh checkout always just works.

Every source that contributes a value is recorded, because silent precedence between
similarly-named settings is the worst kind of bug to debug.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Editor config dirs that may hold sonarlint.* settings, as <home>/<dir>/User/settings.json
_EDITOR_USER_DIRS = (
    ("Code", "vscode"),
    ("Code - Insiders", "vscode-insiders"),
    ("VSCodium", "vscodium"),
    ("Cursor", "cursor"),
    ("Windsurf", "windsurf"),
    ("Kiro", "kiro"),
)

_MARKERS = (".git", ".hg", ".svn")

_SETTINGS_FILE = "settings.json"


@dataclass
class Binding:
    """A resolved connected-mode binding. Complete only if url and token are both set."""

    url: str | None = None
    project_key: str | None = None
    organization: str | None = None
    token: str | None = None
    region: str | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.url and self.token)

    @property
    def is_cloud(self) -> bool:
        if self.organization:
            return True
        return bool(self.url and re.search(r"sonarcloud\.io|sonarqube\.us", self.url))


@dataclass
class Config:
    """Everything resolved for one run."""

    root: Path
    binding: Binding = field(default_factory=Binding)
    sources: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def connected(self) -> bool:
        return self.binding.is_complete

    def _set(self, field_name: str, value: str | None, source: str) -> None:
        """Fill a binding field only if empty, recording which source won."""
        if not value:
            return
        if getattr(self.binding, field_name, None):
            return
        setattr(self.binding, field_name, value)
        self.provenance[field_name] = source


def find_root(start: Path) -> Path:
    """Nearest enclosing repo root, else the starting directory.

    sonar-scanner looks only in its cwd, but we emulate an IDE, and Python tooling
    (pytest, ruff, black) all walk up. The resolved root is always reported.
    """
    start = start.resolve()
    base = start if start.is_dir() else start.parent
    for candidate in (base, *base.parents):
        if any((candidate / m).exists() for m in _MARKERS):
            return candidate
    return base


def _ancestors(start: Path, root: Path) -> list[Path]:
    """Directories from `start` up to and including `root`, nearest first."""
    start = start.resolve()
    base = start if start.is_dir() else start.parent
    chain = [base, *base.parents]
    root = root.resolve()
    # Keep only root and its descendants, nearest first.
    return [d for d in chain if d == root or root in d.parents] or [base]


def _read_properties(path: Path) -> dict[str, str]:
    """Parse a .properties file: key=value, # and ! comments, no interpolation."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        key, sep, value = line.partition("=")
        if not sep:
            key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _load_json(path: Path) -> dict[str, Any]:
    """Read JSON, tolerating the trailing commas and // comments editors allow."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        stripped = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return {}


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _connection_id(url_or_org: str) -> str:
    """Mirror the IDE's default connection id derivation."""
    return re.sub(r"[^a-z\d]+", "-", url_or_org.rstrip("/"), flags=re.I).strip("-")


class Resolver:
    """Walks the precedence chain and produces a Config."""

    def __init__(self, target: Path, *, root: Path | None = None) -> None:
        self.target = target.resolve()
        self.root = (root or find_root(self.target)).resolve()
        self.cfg = Config(root=self.root)

    def resolve(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        project_key: str | None = None,
        organization: str | None = None,
        standalone: bool = False,
    ) -> Config:
        # 1. Explicit CLI flags always win.
        self.cfg._set("url", _normalize_url(url), "--server-url")
        self.cfg._set("token", token, "--token")
        self.cfg._set("project_key", project_key, "--project-key")
        self.cfg._set("organization", organization, "--organization")

        # 2. Environment. Scanner convention first, then the MCP server's spelling.
        self._from_env()

        # 3-6. Project files, nearest ancestor first.
        self._from_sonarlint_json()
        self._from_properties()
        self._from_editor_settings()
        self._from_pyproject()

        # A token saved by `pysonarlint login`, looked up by server URL. Last resort,
        # so an explicit flag or env var always wins.
        if self.cfg.binding.url and not self.cfg.binding.token:
            try:
                from .auth import load_token

                if stored := load_token(self.cfg.binding.url):
                    self.cfg._set("token", stored, "stored credentials")
            except Exception:  # noqa: BLE001 - a broken store must not block analysis
                pass

        if standalone:
            if self.cfg.binding.url or self.cfg.binding.token:
                self.cfg.notes.append("connected mode disabled by --standalone")
            self.cfg.binding = Binding()
            self.cfg.provenance = {
                k: v for k, v in self.cfg.provenance.items() if k not in Binding.__annotations__
            }
        else:
            self._explain_partial()
        return self.cfg

    # -- sources -----------------------------------------------------------

    def _from_env(self) -> None:
        env = os.environ
        for key, name in (("SONAR_TOKEN", "token"), ("SONARQUBE_TOKEN", "token")):
            if value := env.get(key):
                self.cfg._set(name, value.strip(), f"${key}")
        for key in ("SONAR_HOST_URL", "SONARQUBE_URL"):
            if value := env.get(key):
                self.cfg._set("url", _normalize_url(value), f"${key}")
        for key in ("SONARQUBE_ORG", "SONAR_ORGANIZATION"):
            if value := env.get(key):
                self.cfg._set("organization", value.strip(), f"${key}")
        if value := env.get("SONARQUBE_PROJECT_KEY"):
            self.cfg._set("project_key", value.strip(), "$SONARQUBE_PROJECT_KEY")
        if value := env.get("SONAR_REGION"):
            self.cfg._set("region", value.strip().upper(), "$SONAR_REGION")

    def _from_sonarlint_json(self) -> None:
        """.sonarlint/*.json - the IDE's own shareable binding file.

        Committed to the repo by design and never contains a token, so it is the
        strongest signal about *which project* this is.
        """
        for directory in _ancestors(self.target, self.root):
            folder = directory / ".sonarlint"
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                data = _load_json(path)
                if not data:
                    continue
                rel = self._rel(path)
                lower = {k.lower(): v for k, v in data.items() if isinstance(k, str)}
                self.cfg._set("project_key", _s(lower.get("projectkey")), rel)
                self.cfg._set("url", _normalize_url(_s(lower.get("sonarqubeuri"))), rel)
                self.cfg._set("organization", _s(lower.get("sonarcloudorganization")), rel)
                self.cfg._set("region", (_s(lower.get("region")) or "").upper() or None, rel)

    def _from_properties(self) -> None:
        for directory in _ancestors(self.target, self.root):
            for name in ("sonar-project.properties", ".sonarcloud.properties"):
                path = directory / name
                if not path.is_file():
                    continue
                props = _read_properties(path)
                rel = self._rel(path)
                self.cfg._set("url", _normalize_url(props.get("sonar.host.url")), rel)
                self.cfg._set("project_key", props.get("sonar.projectKey"), rel)
                self.cfg._set("organization", props.get("sonar.organization"), rel)
                self.cfg._set("token", props.get("sonar.token"), rel)
                if not self.cfg.sources and (src := _split_list(props.get("sonar.sources"))):
                    self.cfg.sources = src
                    self.cfg.provenance["sources"] = rel
                if excl := _split_list(props.get("sonar.exclusions")):
                    self.cfg.exclusions.extend(excl)
                    self.cfg.provenance.setdefault("exclusions", rel)

    def _from_editor_settings(self) -> None:
        """Workspace binding plus the user-level connection list it refers to.

        These live in different scopes: the binding is per-workspace, but connections
        (and tokens) are application-scoped, so both must be read and joined by id.
        """
        binding_id: str | None = None
        for directory in _ancestors(self.target, self.root):
            path = directory / ".vscode" / _SETTINGS_FILE
            if not path.is_file():
                continue
            data = _load_json(path)
            project = data.get("sonarlint.connectedMode.project")
            if isinstance(project, dict):
                rel = self._rel(path)
                self.cfg._set("project_key", _s(project.get("projectKey")), rel)
                binding_id = _s(project.get("connectionId")) or _s(project.get("serverId"))
                break

        for product, _slug in _EDITOR_USER_DIRS:
            path = _user_settings_path(product)
            if not path or not path.is_file():
                continue
            data = _load_json(path)
            conns: list[tuple[dict[str, Any], bool]] = []
            for entry in data.get("sonarlint.connectedMode.connections.sonarqube") or []:
                if isinstance(entry, dict):
                    conns.append((entry, False))
            for entry in data.get("sonarlint.connectedMode.connections.sonarcloud") or []:
                if isinstance(entry, dict):
                    conns.append((entry, True))
            for entry in data.get("sonarlint.connectedMode.servers") or []:
                if isinstance(entry, dict):
                    conns.append((entry, False))
            if not conns:
                continue
            label = f"{product} {_SETTINGS_FILE}"
            match = self._pick_connection(conns, binding_id)
            if match is None:
                continue
            entry, is_cloud = match
            self.cfg._set("url", _normalize_url(_s(entry.get("serverUrl"))), label)
            self.cfg._set("token", _s(entry.get("token")), label)
            if is_cloud:
                self.cfg._set("organization", _s(entry.get("organizationKey")), label)
                self.cfg._set("region", (_s(entry.get("region")) or "").upper() or None, label)
            break

    @staticmethod
    def _pick_connection(
        conns: list[tuple[dict[str, Any], bool]], binding_id: str | None
    ) -> tuple[dict[str, Any], bool] | None:
        """Match the workspace's connectionId; fall back only if it is unambiguous."""
        if binding_id:
            for entry, is_cloud in conns:
                ident = (
                    _s(entry.get("connectionId"))
                    or _s(entry.get("serverId"))
                    or _connection_id(_s(entry.get("serverUrl")) or _s(entry.get("organizationKey")) or "")
                )
                if ident and ident == binding_id:
                    return entry, is_cloud
            return None
        return conns[0] if len(conns) == 1 else None

    def _from_pyproject(self) -> None:
        """[tool.pysonarlint] - our own table, not a Sonar convention.

        No Sonar tool reads pyproject.toml for IDE bindings; `pysonar` reads
        [tool.sonar] for CI scanning. We deliberately use a distinct table so we never
        reinterpret another tool's settings.
        """
        for directory in _ancestors(self.target, self.root):
            path = directory / "pyproject.toml"
            if not path.is_file():
                continue
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            table = (data.get("tool") or {}).get("pysonarlint")
            if not isinstance(table, dict):
                continue
            rel = f"{self._rel(path)} [tool.pysonarlint]"
            self.cfg._set("url", _normalize_url(_s(table.get("server_url") or table.get("hostUrl"))), rel)
            self.cfg._set("project_key", _s(table.get("project_key") or table.get("projectKey")), rel)
            self.cfg._set("organization", _s(table.get("organization")), rel)
            if excl := table.get("exclusions"):
                if isinstance(excl, list):
                    self.cfg.exclusions.extend(str(e) for e in excl)
                    self.cfg.provenance.setdefault("exclusions", rel)
            break

    # -- helpers -----------------------------------------------------------

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _explain_partial(self) -> None:
        """Say exactly what is missing, so partial config is never mysterious."""
        b = self.cfg.binding
        if b.is_complete:
            return
        if b.url and not b.token:
            where = self.cfg.provenance.get("url", "config")
            self.cfg.notes.append(
                f"standalone: found server {b.url} (from {where}) but no token. "
                "Set SONAR_TOKEN, or run 'pysonarlint login' to grant one."
            )
        elif b.token and not b.url:
            self.cfg.notes.append(
                "standalone: found a token but no server URL. "
                "Set SONAR_HOST_URL or add sonar.host.url to sonar-project.properties."
            )


def _s(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalize_url(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    cleaned = url.strip().rstrip("/")
    if not re.match(r"^https?://", cleaned, re.I):
        cleaned = "https://" + cleaned
    return cleaned


def _user_settings_path(product: str) -> Path | None:
    """User-level settings.json for an editor, per-platform."""
    if os.name == "nt":
        if appdata := os.environ.get("APPDATA"):
            return Path(appdata) / product / "User" / _SETTINGS_FILE
        return None
    home = Path.home()
    mac = home / "Library" / "Application Support" / product / "User" / _SETTINGS_FILE
    if mac.exists():
        return mac
    return home / ".config" / product / "User" / _SETTINGS_FILE


def resolve(
    target: Path,
    *,
    root: Path | None = None,
    url: str | None = None,
    token: str | None = None,
    project_key: str | None = None,
    organization: str | None = None,
    standalone: bool = False,
) -> Config:
    return Resolver(target, root=root).resolve(
        url=url,
        token=token,
        project_key=project_key,
        organization=organization,
        standalone=standalone,
    )
