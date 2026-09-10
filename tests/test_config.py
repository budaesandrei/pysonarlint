"""Config precedence and the standalone-unless-complete rule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pysonarlint.config import _normalize_url, _read_properties, find_root, resolve


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's real environment and credential store out of the tests."""
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
    # Redirect credential lookups to an empty directory.
    monkeypatch.setenv("PYSONARLINT_HOME_DIR", str(tmp_path / "no-creds"))


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_no_configuration_means_standalone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    cfg = resolve(repo)
    assert cfg.connected is False
    assert cfg.binding.url is None


def test_url_without_token_stays_standalone(tmp_path: Path) -> None:
    """The core rule: connected mode needs both halves."""
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text(
        "sonar.host.url=https://sonar.example.com\nsonar.projectKey=proj\n"
    )
    cfg = resolve(repo)
    assert cfg.connected is False
    assert cfg.binding.url == "https://sonar.example.com"
    assert cfg.binding.project_key == "proj"
    assert any("no token" in n for n in cfg.notes)


def test_token_without_url_stays_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SONAR_TOKEN", "squ_x")
    cfg = resolve(_repo(tmp_path))
    assert cfg.connected is False
    assert any("no server URL" in n for n in cfg.notes)


def test_both_halves_enable_connected_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text("sonar.host.url=https://s.example.com\n")
    monkeypatch.setenv("SONAR_TOKEN", "squ_x")
    cfg = resolve(repo)
    assert cfg.connected is True
    assert cfg.provenance["token"] == "$SONAR_TOKEN"
    assert cfg.provenance["url"] == "sonar-project.properties"


def test_standalone_flag_suppresses_a_complete_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SONAR_TOKEN", "squ_x")
    monkeypatch.setenv("SONAR_HOST_URL", "https://s.example.com")
    cfg = resolve(_repo(tmp_path), standalone=True)
    assert cfg.connected is False
    assert cfg.binding.url is None


def test_cli_flag_beats_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SONAR_HOST_URL", "https://env.example.com")
    cfg = resolve(_repo(tmp_path), url="https://flag.example.com", token="t")
    assert cfg.binding.url == "https://flag.example.com"
    assert cfg.provenance["url"] == "--server-url"


def test_sonarlint_json_beats_properties(tmp_path: Path) -> None:
    """The IDE's own binding file is the most intentional signal."""
    repo = _repo(tmp_path)
    (repo / ".sonarlint").mkdir()
    (repo / ".sonarlint" / "connectedMode.json").write_text(
        json.dumps({"sonarQubeUri": "https://from-json.example.com", "projectKey": "json-proj"})
    )
    (repo / "sonar-project.properties").write_text(
        "sonar.host.url=https://from-props.example.com\nsonar.projectKey=props-proj\n"
    )
    cfg = resolve(repo)
    assert cfg.binding.url == "https://from-json.example.com"
    assert cfg.binding.project_key == "json-proj"


def test_nearest_properties_file_wins(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text("sonar.projectKey=outer\n")
    inner = repo / "sub" / "project"
    inner.mkdir(parents=True)
    (inner / "sonar-project.properties").write_text("sonar.projectKey=inner\n")
    cfg = resolve(inner)
    assert cfg.binding.project_key == "inner"


def test_sonarcloud_organization_marks_cloud(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".sonarlint").mkdir()
    (repo / ".sonarlint" / "connectedMode.json").write_text(
        json.dumps({"sonarCloudOrganization": "my-org", "projectKey": "p", "region": "US"})
    )
    cfg = resolve(repo)
    assert cfg.binding.organization == "my-org"
    assert cfg.binding.is_cloud is True
    assert cfg.binding.region == "US"


def test_pyproject_table_is_read(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[tool.pysonarlint]\nserver_url = "https://toml.example.com"\nproject_key = "tp"\n'
    )
    cfg = resolve(repo)
    assert cfg.binding.url == "https://toml.example.com"
    assert cfg.binding.project_key == "tp"


def test_exclusions_are_collected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "sonar-project.properties").write_text("sonar.exclusions=**/*.pyc, **/vendor/**\n")
    cfg = resolve(repo)
    assert "**/*.pyc" in cfg.exclusions
    assert "**/vendor/**" in cfg.exclusions


def test_find_root_stops_at_repo_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    deep = repo / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_root(deep) == repo


def test_find_root_falls_back_to_directory(tmp_path: Path) -> None:
    assert find_root(tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://s.example.com/", "https://s.example.com"),
        ("http://s.example.com", "http://s.example.com"),
        ("s.example.com", "https://s.example.com"),
        ("  https://s.example.com  ", "https://s.example.com"),
        ("", None),
        (None, None),
    ],
)
def test_url_normalization(raw: str | None, expected: str | None) -> None:
    assert _normalize_url(raw) == expected


def test_properties_parser_handles_comments_and_colons(tmp_path: Path) -> None:
    path = tmp_path / "p.properties"
    path.write_text("# comment\n! also comment\n\na=1\nb : 2\nc=has=equals\n")
    props = _read_properties(path)
    assert props == {"a": "1", "b": "2", "c": "has=equals"}


def test_malformed_json_does_not_crash(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".sonarlint").mkdir()
    (repo / ".sonarlint" / "connectedMode.json").write_text("{ not valid json ,,, }")
    cfg = resolve(repo)
    assert cfg.connected is False


def test_json_with_comments_and_trailing_commas(tmp_path: Path) -> None:
    """Editors permit these, so we must too."""
    repo = _repo(tmp_path)
    vscode = repo / ".vscode"
    vscode.mkdir()
    (vscode / "settings.json").write_text(
        '{\n  // a comment\n  "sonarlint.connectedMode.project": {\n'
        '    "projectKey": "commented",\n  },\n}\n'
    )
    cfg = resolve(repo)
    assert cfg.binding.project_key == "commented"
