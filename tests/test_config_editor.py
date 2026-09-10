"""Editor-scoped connection discovery, and the tolerance of unreadable config.

The binding is per-workspace but connections (and tokens) are application-scoped, so both
scopes have to be read and joined by connection id. Getting that join wrong is how a run
silently ends up standalone, or worse, bound to the wrong server.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from pysonarlint import config as cfgmod
from pysonarlint.config import (
    Resolver,
    _connection_id,
    _editor_connections,
    _load_json,
    _read_properties,
    _user_settings_path,
    resolve,
)

TOKEN = "squ_EXAMPLEfakeTOKENvalueNOTaREALsecret00"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in (
        "SONAR_TOKEN",
        "SONARQUBE_TOKEN",
        "SONAR_HOST_URL",
        "SONARQUBE_URL",
        "SONARQUBE_ORG",
        "SONAR_ORGANIZATION",
        "SONARQUBE_PROJECT_KEY",
        "SONAR_REGION",
        "APPDATA",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PYSONARLINT_HOME_DIR", str(tmp_path / "no-creds"))
    # No editor settings unless a test opts in.
    monkeypatch.setattr(cfgmod, "_user_settings_path", lambda _product: None)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


class _FakeOsName:
    """`os` with a different `name`; see the note in tests/test_lsp_transport.py."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str) -> object:
        return getattr(os, attr)


# -- environment variables not previously exercised -----------------------


@pytest.mark.parametrize("var", ["SONARQUBE_ORG", "SONAR_ORGANIZATION"])
def test_organization_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    monkeypatch.setenv(var, "  my-org  ")
    cfg = resolve(_repo(tmp_path))
    assert cfg.binding.organization == "my-org"
    assert cfg.provenance["organization"] == f"${var}"


def test_project_key_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SONARQUBE_PROJECT_KEY", " my:proj ")
    cfg = resolve(_repo(tmp_path))
    assert cfg.binding.project_key == "my:proj"
    assert cfg.provenance["project_key"] == "$SONARQUBE_PROJECT_KEY"


def test_region_from_the_environment_is_uppercased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SONAR_REGION", " us ")
    assert resolve(_repo(tmp_path)).binding.region == "US"


@pytest.mark.parametrize("var", ["SONARQUBE_TOKEN", "SONAR_TOKEN"])
def test_token_from_either_environment_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    monkeypatch.setenv(var, f"  {TOKEN}  ")
    cfg = resolve(_repo(tmp_path))
    assert cfg.binding.token == TOKEN
    assert cfg.provenance["token"] == f"${var}"


def test_the_scanner_spelling_wins_over_the_mcp_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SONAR_TOKEN", TOKEN)
    monkeypatch.setenv("SONARQUBE_TOKEN", "squ_second")
    assert resolve(_repo(tmp_path)).provenance["token"] == "$SONAR_TOKEN"


# -- sonar.sources -------------------------------------------------------


def test_sonar_sources_is_recorded_with_its_provenance(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text("sonar.sources=src, tests\n", encoding="utf-8")
    cfg = resolve(repo)
    assert cfg.sources == ["src", "tests"]
    assert cfg.provenance["sources"] == "sonar-project.properties"


def test_the_nearest_sonar_sources_wins(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text("sonar.sources=outer\n", encoding="utf-8")
    inner = repo / "sub"
    inner.mkdir()
    (inner / "sonar-project.properties").write_text("sonar.sources=inner\n", encoding="utf-8")
    assert resolve(inner).sources == ["inner"]


# -- pyproject exclusions ------------------------------------------------


def test_pyproject_exclusions_are_collected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[tool.pysonarlint]\nexclusions = ["**/vendor/**", "generated/*.py"]\n', encoding="utf-8"
    )
    cfg = resolve(repo)
    assert cfg.exclusions == ["**/vendor/**", "generated/*.py"]
    assert "[tool.pysonarlint]" in cfg.provenance["exclusions"]


def test_pyproject_camelcase_keys_are_accepted_as_aliases(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[tool.pysonarlint]\nhostUrl = "https://alias.example.com"\nprojectKey = "ak"\n',
        encoding="utf-8",
    )
    cfg = resolve(repo)
    assert cfg.binding.url == "https://alias.example.com"
    assert cfg.binding.project_key == "ak"


def test_a_pyproject_without_our_table_is_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert resolve(repo).binding.url is None


def test_an_unparseable_pyproject_is_skipped(tmp_path: Path) -> None:
    """A syntax error in someone else's file must not stop the analysis."""
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text("[tool.pysonarlint\nbroken = \n", encoding="utf-8")
    assert resolve(repo).binding.url is None


def test_the_nearest_pyproject_wins_and_stops_the_walk(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[tool.pysonarlint]\nserver_url = "https://outer.example.com"\n', encoding="utf-8"
    )
    inner = repo / "sub"
    inner.mkdir()
    (inner / "pyproject.toml").write_text(
        '[tool.pysonarlint]\nproject_key = "ik"\n', encoding="utf-8"
    )
    cfg = resolve(inner)
    assert cfg.binding.project_key == "ik"
    assert cfg.binding.url is None  # the walk stopped at the nearest pyproject.toml


# -- unreadable files ----------------------------------------------------


def test_unreadable_properties_read_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "p.properties"
    path.write_text("a=1\n", encoding="utf-8")

    def boom(*_a: object, **_k: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    assert _read_properties(path) == {}


def test_unreadable_json_reads_as_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")

    def boom(*_a: object, **_k: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    assert _load_json(path) == {}


def test_a_json_file_that_is_beyond_repair_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{{{ nonsense ]]]", encoding="utf-8")
    assert _load_json(path) == {}


def test_a_json_file_with_block_comments_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{\n  /* block */\n  "a": 1,\n}\n', encoding="utf-8")
    assert _load_json(path) == {"a": 1}


def test_a_path_outside_the_root_is_reported_absolutely(tmp_path: Path) -> None:
    """`_rel` must not raise when a config file is somehow outside the resolved root."""
    resolver = Resolver(tmp_path, root=tmp_path / "sub")
    outside = tmp_path.parent / "elsewhere.json"
    assert resolver._rel(outside) == str(outside)


# -- _connection_id ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://sonar.example.com", "https-sonar-example-com"),
        ("https://sonar.example.com/", "https-sonar-example-com"),
        ("my-org", "my-org"),
        ("My_Org 2", "My-Org-2"),
        ("://", ""),
    ],
)
def test_connection_id_derivation(raw: str, expected: str) -> None:
    assert _connection_id(raw) == expected


# -- _editor_connections -------------------------------------------------


def test_editor_connections_flattens_all_three_lists() -> None:
    data = {
        "sonarlint.connectedMode.connections.sonarqube": [{"connectionId": "sq"}],
        "sonarlint.connectedMode.connections.sonarcloud": [{"connectionId": "sc"}],
        "sonarlint.connectedMode.servers": [{"serverId": "legacy"}],
    }
    conns = _editor_connections(data)
    assert [(e.get("connectionId") or e.get("serverId"), cloud) for e, cloud in conns] == [
        ("sq", False),
        ("sc", True),
        ("legacy", False),
    ]


def test_editor_connections_ignores_non_objects() -> None:
    data = {"sonarlint.connectedMode.connections.sonarqube": ["a string", {"connectionId": "ok"}]}
    assert _editor_connections(data) == [({"connectionId": "ok"}, False)]


def test_editor_connections_on_an_empty_document() -> None:
    assert _editor_connections({}) == []


def test_editor_connections_tolerates_a_null_list() -> None:
    assert _editor_connections({"sonarlint.connectedMode.connections.sonarqube": None}) == []


# -- joining the workspace binding to the user connections ---------------


def _user_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict) -> Path:
    """Install a user-level settings.json for the first editor probed."""
    path = tmp_path / "user" / "Code" / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(
        cfgmod, "_user_settings_path", lambda product: path if product == "Code" else None
    )
    return path


def test_a_lone_connection_is_used_without_a_workspace_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.connections.sonarqube": [
                {"connectionId": "only", "serverUrl": "https://only.example.com", "token": TOKEN}
            ]
        },
    )
    cfg = resolve(repo)
    assert cfg.binding.url == "https://only.example.com"
    assert cfg.binding.token == TOKEN
    assert cfg.connected is True
    assert cfg.provenance["url"] == "Code settings.json"


def test_several_connections_without_a_binding_are_ambiguous_and_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guessing here could bind the run to the wrong server, so it refuses to guess."""
    repo = _repo(tmp_path)
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.connections.sonarqube": [
                {"connectionId": "a", "serverUrl": "https://a.example.com", "token": TOKEN},
                {"connectionId": "b", "serverUrl": "https://b.example.com", "token": TOKEN},
            ]
        },
    )
    cfg = resolve(repo)
    assert cfg.binding.url is None
    assert cfg.connected is False


def test_the_workspace_binding_selects_a_connection_by_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    vscode = repo / ".vscode"
    vscode.mkdir()
    (vscode / "settings.json").write_text(
        json.dumps(
            {"sonarlint.connectedMode.project": {"connectionId": "b", "projectKey": "the-proj"}}
        ),
        encoding="utf-8",
    )
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.connections.sonarqube": [
                {"connectionId": "a", "serverUrl": "https://a.example.com", "token": "squ_a"},
                {"connectionId": "b", "serverUrl": "https://b.example.com", "token": TOKEN},
            ]
        },
    )
    cfg = resolve(repo)
    assert cfg.binding.url == "https://b.example.com"
    assert cfg.binding.token == TOKEN
    assert cfg.binding.project_key == "the-proj"


def test_a_binding_naming_an_unknown_connection_yields_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    vscode = repo / ".vscode"
    vscode.mkdir()
    (vscode / "settings.json").write_text(
        json.dumps(
            {"sonarlint.connectedMode.project": {"connectionId": "gone", "projectKey": "p"}}
        ),
        encoding="utf-8",
    )
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.connections.sonarqube": [
                {"connectionId": "a", "serverUrl": "https://a.example.com", "token": TOKEN}
            ]
        },
    )
    cfg = resolve(repo)
    assert cfg.binding.project_key == "p"
    assert cfg.binding.token is None
    assert cfg.connected is False


def test_the_legacy_server_id_spelling_is_matched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    vscode = repo / ".vscode"
    vscode.mkdir()
    (vscode / "settings.json").write_text(
        json.dumps({"sonarlint.connectedMode.project": {"serverId": "legacy", "projectKey": "p"}}),
        encoding="utf-8",
    )
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.servers": [
                {"serverId": "legacy", "serverUrl": "https://legacy.example.com", "token": TOKEN}
            ]
        },
    )
    assert resolve(repo).binding.url == "https://legacy.example.com"


def test_a_connection_with_no_id_is_matched_by_its_derived_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IDE derives an id from the URL when none is stored."""
    repo = _repo(tmp_path)
    vscode = repo / ".vscode"
    vscode.mkdir()
    (vscode / "settings.json").write_text(
        json.dumps(
            {
                "sonarlint.connectedMode.project": {
                    "connectionId": "https-derived-example-com",
                    "projectKey": "p",
                }
            }
        ),
        encoding="utf-8",
    )
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.connections.sonarqube": [
                {"serverUrl": "https://derived.example.com", "token": TOKEN},
                {"connectionId": "other", "serverUrl": "https://other.example.com"},
            ]
        },
    )
    assert resolve(repo).binding.url == "https://derived.example.com"


def test_a_cloud_connection_contributes_the_organization_and_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.connections.sonarcloud": [
                {
                    "connectionId": "cloud",
                    "organizationKey": "my-org",
                    "region": "us",
                    "token": TOKEN,
                }
            ]
        },
    )
    cfg = resolve(repo)
    assert cfg.binding.organization == "my-org"
    assert cfg.binding.region == "US"
    assert cfg.binding.is_cloud is True


def test_an_editor_with_no_connections_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _user_settings(tmp_path, monkeypatch, {"editor.fontSize": 14})
    assert resolve(repo).binding.url is None


def test_a_missing_user_settings_file_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfgmod, "_user_settings_path", lambda _p: tmp_path / "does-not-exist.json")
    assert resolve(_repo(tmp_path)).binding.url is None


def test_a_workspace_settings_file_without_a_binding_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    vscode = repo / ".vscode"
    vscode.mkdir()
    (vscode / "settings.json").write_text('{"editor.fontSize": 14}', encoding="utf-8")
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.connections.sonarqube": [
                {"connectionId": "only", "serverUrl": "https://only.example.com", "token": TOKEN}
            ]
        },
    )
    # No binding, but a single unambiguous connection still applies.
    assert resolve(repo).binding.url == "https://only.example.com"


def test_an_explicit_flag_still_beats_an_editor_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _user_settings(
        tmp_path,
        monkeypatch,
        {
            "sonarlint.connectedMode.connections.sonarqube": [
                {"connectionId": "only", "serverUrl": "https://editor.example.com", "token": TOKEN}
            ]
        },
    )
    cfg = resolve(repo, url="https://flag.example.com")
    assert cfg.binding.url == "https://flag.example.com"
    assert cfg.provenance["url"] == "--server-url"


# -- stored credentials --------------------------------------------------


def test_a_stored_token_is_the_last_resort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text(
        "sonar.host.url=https://stored.example.com\n", encoding="utf-8"
    )
    monkeypatch.setattr("pysonarlint.auth.load_token", lambda _url: TOKEN)
    cfg = resolve(repo)
    assert cfg.binding.token == TOKEN
    assert cfg.provenance["token"] == "stored credentials"
    assert cfg.connected is True


def test_an_environment_token_beats_a_stored_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text(
        "sonar.host.url=https://s.example.com\n", encoding="utf-8"
    )
    monkeypatch.setenv("SONAR_TOKEN", TOKEN)
    monkeypatch.setattr(
        "pysonarlint.auth.load_token", lambda _url: pytest.fail("the env var already won")
    )
    assert resolve(repo).provenance["token"] == "$SONAR_TOKEN"


def test_a_broken_credential_store_never_blocks_the_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text(
        "sonar.host.url=https://s.example.com\n", encoding="utf-8"
    )

    def boom(_url: str) -> str:
        raise RuntimeError("keystore is on fire")

    monkeypatch.setattr("pysonarlint.auth.load_token", boom)
    cfg = resolve(repo)
    assert cfg.connected is False  # degraded, not crashed
    assert cfg.binding.url == "https://s.example.com"


def test_no_lookup_happens_without_a_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pysonarlint.auth.load_token", lambda _url: pytest.fail("nothing to look up")
    )
    assert resolve(_repo(tmp_path)).binding.token is None


# -- _user_settings_path -------------------------------------------------


def test_user_settings_path_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cfgmod, "os", _FakeOsName("nt"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    assert _user_settings_path("Code") == tmp_path / "AppData" / "Code" / "User" / "settings.json"


def test_user_settings_path_is_none_on_windows_without_appdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfgmod, "os", _FakeOsName("nt"))
    monkeypatch.delenv("APPDATA", raising=False)
    assert _user_settings_path("Code") is None


def test_user_settings_path_prefers_the_macos_location_when_it_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cfgmod, "os", _FakeOsName("posix"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    mac = tmp_path / "Library" / "Application Support" / "Code" / "User"
    mac.mkdir(parents=True)
    (mac / "settings.json").write_text("{}", encoding="utf-8")
    assert _user_settings_path("Code") == mac / "settings.json"


def test_user_settings_path_falls_back_to_dot_config_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cfgmod, "os", _FakeOsName("posix"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert _user_settings_path("Code") == tmp_path / ".config" / "Code" / "User" / "settings.json"


# -- tomllib is used, not a hand-rolled parser ---------------------------


def test_pyproject_is_parsed_with_tomllib() -> None:
    """Pinned so nobody replaces this with a regex; TOML is not line-oriented."""
    assert cfgmod.tomllib is tomllib
