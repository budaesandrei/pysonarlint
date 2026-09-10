"""Argument parsing, orchestration, and exit-code selection.

Exit codes are the CLI's contract with CI, so every branch that chooses one is pinned
here. No JVM and no network: `discover`, `collect` and `analyze` are replaced with fakes,
which is enough because the CLI only ever wires them together.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from pysonarlint import cli
from pysonarlint.analyze import Result
from pysonarlint.collect import SourceFile
from pysonarlint.config import Binding, Config
from pysonarlint.engine import Engine, EngineNotFound
from pysonarlint.lsp import Diagnostic

TOKEN = "squ_EXAMPLEfakeTOKENvalueNOTaREALsecret00"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own server settings and credential store out of reach."""
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
        "PYSONARLINT_HOME",
        "PYSONARLINT_JAVA",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PYSONARLINT_HOME_DIR", str(tmp_path / "no-creds"))


# -- fakes -----------------------------------------------------------------


def _engine(tmp_path: Path) -> Engine:
    return Engine(
        java=tmp_path / "jre" / "bin" / "java",
        server_jar=tmp_path / "server" / "sonarlint-ls.jar",
        analyzers=(tmp_path / "analyzers" / "sonarpython.jar",),
        root=tmp_path,
        version="4.30.0",
    )


def _issue(severity: int, *, line: int = 1, path: str = "/repo/a.py") -> Diagnostic:
    return Diagnostic(
        path=Path(path),
        line=line,
        column=1,
        end_line=line,
        end_column=2,
        rule="python:S100",
        message="msg",
        severity=severity,
    )


class _Harness:
    """Records what the CLI asked of its collaborators and dictates their answers."""

    def __init__(self, tmp_path: Path) -> None:
        self.engine = _engine(tmp_path)
        self.engine_error: EngineNotFound | None = None
        self.cfg = Config(root=tmp_path)
        self.result = Result(analyzed=[Path("/repo/a.py")], engine_version="4.30.0")
        self.collected: list[SourceFile] = [SourceFile(path=Path("/repo/a.py"), language="python")]
        self.collect_notes: list[str] = []
        self.discover_arg: Path | object | None = "unset"
        self.collect_kwargs: dict[str, object] = {}
        self.analyze_kwargs: dict[str, object] = {}

    def discover(self, explicit_root: Path | None = None) -> Engine:
        self.discover_arg = explicit_root
        if self.engine_error is not None:
            raise self.engine_error
        return self.engine

    def resolve(self, target: Path, **_kwargs: object) -> Config:
        self.cfg.root = self.cfg.root or target
        return self.cfg

    def collect(self, targets: list[Path], **kwargs: object) -> tuple[list[SourceFile], list[str]]:
        self.collect_kwargs = {"targets": targets, **kwargs}
        return self.collected, self.collect_notes

    def analyze(
        self, files: list[SourceFile], cfg: Config, engine: Engine, **kwargs: object
    ) -> Result:
        self.analyze_kwargs = {"files": files, "cfg": cfg, "engine": engine, **kwargs}
        return self.result


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Harness:
    h = _Harness(tmp_path)
    monkeypatch.setattr(cli, "discover", h.discover)
    monkeypatch.setattr(cli, "resolve", h.resolve)
    monkeypatch.setattr(cli, "collect", h.collect)
    monkeypatch.setattr(cli, "analyze", h.analyze)
    return h


# -- parser ----------------------------------------------------------------


def test_build_parser_analyze_defaults() -> None:
    args = cli.build_parser().parse_args(["analyze"])
    assert args.command == "analyze"
    assert args.paths == []
    assert args.format == "text"
    assert args.fail_on == "warning"
    assert args.severity is None
    assert args.timeout == pytest.approx(600.0)
    assert args.verbose is False
    assert args.quiet is False


def test_build_parser_accepts_repeated_language() -> None:
    args = cli.build_parser().parse_args(["analyze", "--language", "python", "--language", "java"])
    assert args.languages == ["python", "java"]


def test_build_parser_rejects_unknown_format() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["analyze", "--format", "yaml"])


def test_build_parser_status_and_login_and_logout() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["status", "--format", "json"]).format == "json"
    assert parser.parse_args(["login", "--no-browser"]).no_browser is True
    assert parser.parse_args(["logout", "--server-url", "https://s"]).server_url == "https://s"


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "pysonarlint" in capsys.readouterr().out


def test_help_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0


# -- implicit `analyze` subcommand ----------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], "analyze"),
        (["."], "analyze"),
        (["--format", "json"], "analyze"),
        (["analyze"], "analyze"),
        (["status"], "status"),
        (["login"], "login"),
        (["logout"], "logout"),
    ],
)
def test_implicit_subcommand_insertion(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: str
) -> None:
    seen: list[str] = []

    def record(name: str):  # noqa: ANN202 - a test double factory
        def inner(args: argparse.Namespace) -> int:
            seen.append(name)
            assert args.command == expected
            return 0

        return inner

    monkeypatch.setattr(cli, "_cmd_analyze", record("analyze"))
    monkeypatch.setattr(cli, "_cmd_status", record("status"))
    monkeypatch.setattr(cli, "_cmd_login", record("login"))
    monkeypatch.setattr(cli, "_cmd_logout", record("logout"))
    assert cli.main(argv) == 0
    assert seen == [expected]


def test_main_reads_sys_argv_when_given_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["pysonarlint", "--format", "github"])
    captured: list[argparse.Namespace] = []
    monkeypatch.setattr(cli, "_cmd_analyze", lambda args: captured.append(args) or 0)
    assert cli.main() == 0
    assert captured[0].format == "github"


# -- _targets --------------------------------------------------------------


def test_targets_defaults_to_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(paths=[])
    assert cli._targets(args) == [tmp_path.resolve()]


def test_targets_are_resolved_to_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert cli._targets(argparse.Namespace(paths=[Path("a.py")])) == [(tmp_path / "a.py").resolve()]


# -- exit codes ------------------------------------------------------------


def test_clean_run_exits_zero(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([str(tmp_path)]) == cli.EXIT_CLEAN
    assert "No issues" in capsys.readouterr().out


def test_issues_exit_one(harness: _Harness, tmp_path: Path) -> None:
    harness.result.issues = [_issue(2)]
    assert cli.main([str(tmp_path)]) == cli.EXIT_ISSUES


def test_incomplete_run_exits_two_even_with_no_issues(harness: _Harness, tmp_path: Path) -> None:
    """A run that could not finish must never be reported as clean."""
    harness.result.incomplete = True
    assert cli.main([str(tmp_path)]) == cli.EXIT_ERROR


def test_incomplete_beats_fail_on_never(harness: _Harness, tmp_path: Path) -> None:
    harness.result.incomplete = True
    assert cli.main([str(tmp_path), "--fail-on", "never"]) == cli.EXIT_ERROR


def test_fail_on_never_ignores_issues(harness: _Harness, tmp_path: Path) -> None:
    harness.result.issues = [_issue(1)]
    assert cli.main([str(tmp_path), "--fail-on", "never"]) == cli.EXIT_CLEAN


def test_info_issue_does_not_trip_the_default_threshold(harness: _Harness, tmp_path: Path) -> None:
    """Default --fail-on is warning, so an info-level finding is reported but not fatal."""
    harness.result.issues = [_issue(3)]
    assert cli.main([str(tmp_path)]) == cli.EXIT_CLEAN


def test_fail_on_error_ignores_a_warning(harness: _Harness, tmp_path: Path) -> None:
    harness.result.issues = [_issue(2)]
    assert cli.main([str(tmp_path), "--fail-on", "error"]) == cli.EXIT_CLEAN


def test_fail_on_hint_catches_everything(harness: _Harness, tmp_path: Path) -> None:
    harness.result.issues = [_issue(4)]
    assert cli.main([str(tmp_path), "--fail-on", "hint"]) == cli.EXIT_ISSUES


def test_missing_path_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope"
    assert cli.main([str(missing)]) == cli.EXIT_ERROR
    assert "no such file or directory" in capsys.readouterr().err


def test_every_missing_path_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([str(tmp_path / "a"), str(tmp_path / "b")]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert err.count("no such file or directory") == 2


# -- --severity ------------------------------------------------------------


def test_severity_filter_drops_lower_findings(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.result.issues = [_issue(1, line=1), _issue(3, line=2)]
    code = cli.main([str(tmp_path), "--severity", "error", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_ISSUES
    assert [i["severity"] for i in payload["issues"]] == ["error"]


def test_severity_filter_can_make_a_run_clean(harness: _Harness, tmp_path: Path) -> None:
    harness.result.issues = [_issue(3)]
    assert cli.main([str(tmp_path), "--severity", "error"]) == cli.EXIT_CLEAN


# -- --format --------------------------------------------------------------


def test_format_json(harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    harness.result.issues = [_issue(2)]
    cli.main([str(tmp_path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "pysonarlint"
    assert payload["summary"]["issues"] == 1


def test_format_sarif(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.result.issues = [_issue(2)]
    cli.main([str(tmp_path), "-f", "sarif"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "2.1.0"


def test_format_github(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.result.issues = [_issue(1)]
    cli.main([str(tmp_path), "-f", "github"])
    assert capsys.readouterr().out.startswith("::error file=")


# -- -o/--output -----------------------------------------------------------


def test_output_file_is_written_and_stdout_stays_quiet(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "reports" / "nested" / "out.json"
    harness.result.issues = [_issue(2)]
    cli.main([str(tmp_path), "-f", "json", "-o", str(out)])
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["issues"] == 1
    assert capsys.readouterr().out == ""


def test_output_file_ends_with_a_newline(harness: _Harness, tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    cli.main([str(tmp_path), "-o", str(out)])
    assert out.read_text(encoding="utf-8").endswith("\n")


# -- --quiet and --verbose ------------------------------------------------


def test_notes_are_shown_by_default(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.result.notes = ["something worth saying"]
    cli.main([str(tmp_path)])
    assert "something worth saying" in capsys.readouterr().out


def test_quiet_suppresses_notes(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.result.notes = ["something worth saying"]
    cli.main([str(tmp_path), "--quiet"])
    assert "something worth saying" not in capsys.readouterr().out


def test_verbose_echoes_server_logs_with_the_token_redacted(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.cfg.binding = Binding(url="https://s.example.com", token=TOKEN)
    harness.result.logs = [f"[Info] connections={{token={TOKEN}}}"]
    cli.main([str(tmp_path), "--verbose"])
    err = capsys.readouterr().err
    assert TOKEN not in err
    assert "<redacted>" in err


def test_logs_are_silent_without_verbose(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.result.logs = ["[Info] chatty"]
    cli.main([str(tmp_path)])
    assert "chatty" not in capsys.readouterr().err


def test_notes_from_config_and_collect_are_merged_and_redacted(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.cfg.binding = Binding(url="https://s.example.com", token=TOKEN)
    harness.cfg.notes = [f"config note {TOKEN}"]
    harness.collect_notes = ["collect note"]
    harness.result.notes = ["result note"]
    cli.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "config note <redacted>" in out
    assert "collect note" in out
    assert "result note" in out


# -- file collection wiring ----------------------------------------------


def test_language_selection_defaults_to_python(harness: _Harness, tmp_path: Path) -> None:
    cli.main([str(tmp_path)])
    assert harness.collect_kwargs["languages"] == {"python"}


def test_all_languages_passes_none(harness: _Harness, tmp_path: Path) -> None:
    cli.main([str(tmp_path), "--all-languages"])
    assert harness.collect_kwargs["languages"] is None


def test_explicit_languages_are_forwarded(harness: _Harness, tmp_path: Path) -> None:
    cli.main([str(tmp_path), "--language", "java", "--language", "yaml"])
    assert harness.collect_kwargs["languages"] == {"java", "yaml"}


def test_sonarlint_home_and_timeout_are_forwarded(harness: _Harness, tmp_path: Path) -> None:
    home = tmp_path / "ext"
    home.mkdir()
    cli.main([str(tmp_path), "--sonarlint-home", str(home), "--timeout", "12.5"])
    assert harness.discover_arg == home
    assert harness.analyze_kwargs["timeout"] == pytest.approx(12.5)


def test_progress_is_disabled_by_no_progress_and_by_verbose(
    harness: _Harness, tmp_path: Path
) -> None:
    cli.main([str(tmp_path), "--no-progress"])
    assert harness.analyze_kwargs["progress"].enabled is False
    cli.main([str(tmp_path), "--verbose"])
    assert harness.analyze_kwargs["progress"].enabled is False


def test_the_file_tally_is_announced_through_the_progress_reporter(
    harness: _Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tally is a progress note, not stdout, so it cannot contaminate piped json."""
    notes: list[str] = []

    class _Recorder:
        def __init__(self, **_kwargs: object) -> None:
            self.enabled = False

        def start(self, _message: str) -> None:
            pass

        def stop(self, _final: str | None = None) -> None:
            pass

        def note(self, message: str) -> None:
            notes.append(message)

    monkeypatch.setattr(cli, "Progress", _Recorder)
    harness.collected = [
        SourceFile(path=Path("/repo/a.py"), language="python"),
        SourceFile(path=Path("/repo/b.yml"), language="yaml"),
    ]
    cli.main([str(tmp_path), "--format", "json"])
    assert notes == ["found 2 file(s) to analyze (python, yaml)"]


def test_no_tally_note_when_nothing_was_collected(
    harness: _Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes: list[str] = []
    monkeypatch.setattr(
        cli,
        "Progress",
        lambda **_k: type(
            "P",
            (),
            {
                "enabled": False,
                "start": lambda _s, _m: None,
                "stop": lambda _s, _f=None: None,
                "note": lambda _s, m: notes.append(m),
            },
        )(),
    )
    harness.collected = []
    cli.main([str(tmp_path)])
    assert notes == []


# -- main() error paths ---------------------------------------------------


def test_keyboard_interrupt_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_analyze", boom)
    assert cli.main(["."]) == cli.EXIT_ERROR
    assert "interrupted" in capsys.readouterr().err


def test_engine_not_found_exits_two_with_the_message(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.engine_error = EngineNotFound("no extension anywhere")
    assert cli.main([str(tmp_path)]) == cli.EXIT_ERROR
    assert "no extension anywhere" in capsys.readouterr().err


# -- status --------------------------------------------------------------


def test_status_text_with_an_engine(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.cfg.binding = Binding(url="https://s.example.com", token=TOKEN, project_key="proj")
    harness.cfg.provenance = {"url": "$SONAR_HOST_URL", "token": "$SONAR_TOKEN", "project_key": "x"}
    harness.cfg.notes = ["a note"]
    assert cli.main(["status", str(tmp_path)]) == cli.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "engine     4.30.0" in out
    assert "mode       connected" in out
    assert "https://s.example.com  ($SONAR_HOST_URL)" in out
    assert "token      set  ($SONAR_TOKEN)" in out
    assert "note       a note" in out
    assert TOKEN not in out


def test_status_text_without_an_engine_exits_two(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.engine_error = EngineNotFound("nothing installed")
    assert cli.main(["status", str(tmp_path)]) == cli.EXIT_ERROR
    out = capsys.readouterr().out
    assert "engine     NOT FOUND" in out
    assert "nothing installed" in out
    assert "mode       standalone" in out


def test_status_json_shape(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.cfg.binding = Binding(url="https://s.example.com", token=TOKEN, organization="org")
    harness.cfg.provenance = {"token": "stored credentials", "url": "$SONAR_HOST_URL"}
    assert cli.main(["status", str(tmp_path), "--format", "json"]) == cli.EXIT_CLEAN
    payload = json.loads(capsys.readouterr().out)
    assert payload["engine"]["version"] == "4.30.0"
    assert payload["engine"]["analyzers"] == ["sonarpython"]
    assert payload["engineError"] is None
    assert payload["mode"] == "connected"
    assert payload["binding"]["hasToken"] is True
    assert "token" not in payload["binding"]
    # Renamed so a provenance label is never mistaken for the credential itself.
    assert payload["provenance"]["tokenSource"] == "stored credentials"
    assert TOKEN not in json.dumps(payload)


def test_status_json_without_an_engine(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.engine_error = EngineNotFound("nothing installed")
    assert cli.main(["status", str(tmp_path), "-f", "json"]) == cli.EXIT_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["engine"] is None
    assert payload["engineError"] == "nothing installed"


def test_status_defaults_to_cwd(
    harness: _Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status"]) == cli.EXIT_CLEAN


def test_status_uses_real_config_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through the real resolver, with only the engine faked away."""
    monkeypatch.setattr(cli, "discover", lambda _root=None: _engine(tmp_path))
    (tmp_path / ".git").mkdir()
    (tmp_path / "sonar-project.properties").write_text(
        "sonar.host.url=https://real.example.com\nsonar.projectKey=rp\n", encoding="utf-8"
    )
    assert cli.main(["status", str(tmp_path)]) == cli.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "https://real.example.com" in out
    assert "mode       standalone" in out  # no token, so not connected


# -- logout --------------------------------------------------------------


def test_logout_without_a_url_exits_two(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["logout"]) == cli.EXIT_ERROR
    assert "no server URL to forget" in capsys.readouterr().err


def test_logout_removes_a_stored_token(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []
    monkeypatch.setattr("pysonarlint.auth.forget", lambda url: bool(seen.append(url)) or True)
    assert cli.main(["logout", "--server-url", "https://s.example.com/"]) == cli.EXIT_CLEAN
    assert seen == ["https://s.example.com"]  # trailing slash stripped
    assert "removed stored token" in capsys.readouterr().out


def test_logout_when_nothing_is_stored_is_still_clean(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("pysonarlint.auth.forget", lambda _url: False)
    assert cli.main(["logout", "--server-url", "https://s.example.com"]) == cli.EXIT_CLEAN
    assert "no stored token" in capsys.readouterr().out


def test_logout_falls_back_to_the_configured_url(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.cfg.binding = Binding(url="https://from-config.example.com")
    seen: list[str] = []
    monkeypatch.setattr("pysonarlint.auth.forget", lambda url: bool(seen.append(url)) or True)
    assert cli.main(["logout"]) == cli.EXIT_CLEAN
    assert seen == ["https://from-config.example.com"]


# -- login ---------------------------------------------------------------


class _Check:
    """Stand-in for a server.Preflight result."""

    def __init__(self, *, ok: bool, info: object = None, problems=(), hints=()) -> None:
        self.ok = ok
        self.info = info
        self.problems = list(problems)
        self.hints = list(hints)


class _Info:
    def __init__(self) -> None:
        self.status = "UP"
        self.version = "2025.6"


def test_login_without_a_url_exits_two(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["login"]) == cli.EXIT_ERROR
    assert "no server URL" in capsys.readouterr().err


def test_login_reports_an_unreachable_server(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "pysonarlint.server.preflight",
        lambda *_a, **_k: _Check(ok=False, problems=["host unreachable"]),
    )
    assert cli.main(["login", "--server-url", "https://s.example.com"]) == cli.EXIT_ERROR
    assert "host unreachable" in capsys.readouterr().err


def test_login_with_an_existing_token_skips_the_browser(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "pysonarlint.server.preflight", lambda *_a, **_k: _Check(ok=True, info=_Info())
    )
    monkeypatch.setattr(
        "pysonarlint.auth.grant_token",
        lambda *_a, **_k: pytest.fail("the browser must not be used when --token is given"),
    )
    saved: list[object] = []
    monkeypatch.setattr(
        "pysonarlint.auth.save_credential",
        lambda cred: (bool(saved.append(cred)) or True, "~/.config/pysonarlint/credentials.json"),
    )
    code = cli.main(["login", "--server-url", "https://s.example.com/", "--token", TOKEN])
    out = capsys.readouterr().out
    assert code == cli.EXIT_CLEAN
    assert "token verified and saved to" in out
    assert saved[0].url == "https://s.example.com"
    assert saved[0].token == TOKEN


def test_login_grants_a_token_through_the_browser(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "pysonarlint.server.preflight", lambda *_a, **_k: _Check(ok=True, info=_Info())
    )

    def grant(url: str, **kwargs: object) -> str:
        kwargs["on_url"]("https://s.example.com/sonarlint/auth?port=64120")
        return TOKEN

    monkeypatch.setattr("pysonarlint.auth.grant_token", grant)
    monkeypatch.setattr(
        "pysonarlint.auth.save_credential",
        lambda _c: (True, "~/.config/pysonarlint/credentials.json"),
    )
    assert cli.main(["login", "--server-url", "https://s.example.com"]) == cli.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "opening https://s.example.com/sonarlint/auth" in out
    assert "token verified" in out


def test_login_reports_a_failed_grant(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pysonarlint.auth import AuthError

    monkeypatch.setattr(
        "pysonarlint.server.preflight", lambda *_a, **_k: _Check(ok=True, info=_Info())
    )

    def grant(*_a: object, **_k: object) -> str:
        raise AuthError("timed out after 180s")

    monkeypatch.setattr("pysonarlint.auth.grant_token", grant)
    assert cli.main(["login", "--server-url", "https://s.example.com"]) == cli.EXIT_ERROR
    assert "timed out after 180s" in capsys.readouterr().err


def test_login_reports_a_rejected_token_with_hints(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []

    def preflight(_url: str, token: str | None, _key: str | None = None) -> _Check:
        calls.append(token)
        if token is None:
            return _Check(ok=True, info=_Info())
        return _Check(ok=False, info=_Info(), problems=["token rejected"], hints=["generate a new"])

    monkeypatch.setattr("pysonarlint.server.preflight", preflight)
    code = cli.main(["login", "--server-url", "https://s.example.com", "--token", TOKEN])
    err = capsys.readouterr().err
    assert code == cli.EXIT_ERROR
    assert "token rejected" in err
    assert "hint: generate a new" in err


def test_login_says_so_when_the_token_cannot_be_saved(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "pysonarlint.server.preflight", lambda *_a, **_k: _Check(ok=True, info=_Info())
    )
    monkeypatch.setattr(
        "pysonarlint.auth.save_credential", lambda _c: (False, "could not write: denied")
    )
    code = cli.main(["login", "--server-url", "https://s.example.com", "--token", TOKEN])
    captured = capsys.readouterr()
    assert code == cli.EXIT_CLEAN  # the token is valid; only persistence failed
    assert "but NOT saved" in captured.out
    assert "set SONAR_TOKEN" in captured.err


def test_login_uses_the_configured_url_when_no_flag_is_given(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.cfg.binding = Binding(url="https://from-config.example.com", organization="org")
    seen: list[str] = []

    def preflight(url: str, _token: str | None, _key: str | None = None) -> _Check:
        seen.append(url)
        return _Check(ok=True, info=_Info())

    monkeypatch.setattr("pysonarlint.server.preflight", preflight)
    saved: list[object] = []
    monkeypatch.setattr(
        "pysonarlint.auth.save_credential",
        lambda cred: (bool(saved.append(cred)) or True, "~/.config/pysonarlint/credentials.json"),
    )
    assert cli.main(["login", "--token", TOKEN]) == cli.EXIT_CLEAN
    assert seen == ["https://from-config.example.com", "https://from-config.example.com"]
    assert saved[0].organization == "org"
