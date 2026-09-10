"""Session orchestration: initialization options, settings, and the completion barriers.

A `LanguageServer` stand-in replaces the JVM entirely. The waits are exercised with a
fake `progress()` that changes on demand, so nothing here depends on wall-clock timing.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pysonarlint import analyze as an
from pysonarlint.analyze import (
    Result,
    _await_file,
    _await_settle,
    _connection_id,
    _drop_unresolvable_project_key,
    _initialization_options,
    _open_documents,
    _record_outcome,
    _run_session,
    _version,
    _workspace_settings,
    analyze,
)
from pysonarlint.collect import SourceFile
from pysonarlint.config import Binding, Config
from pysonarlint.engine import Engine
from pysonarlint.lsp import Diagnostic, LspError
from pysonarlint.progress import Progress

TOKEN = "squ_EXAMPLEfakeTOKENvalueNOTaREALsecret00"


def _cfg(**binding: object) -> Config:
    cfg = Config(root=Path("/repo"))
    if binding:
        cfg.binding = Binding(**binding)  # type: ignore[arg-type]
    return cfg


def _engine(tmp_path: Path, analyzers: tuple[str, ...] = ("sonarpython",)) -> Engine:
    return Engine(
        java=tmp_path / "java",
        server_jar=tmp_path / "ls.jar",
        analyzers=tuple(tmp_path / f"{a}.jar" for a in analyzers),
        root=tmp_path,
        version="4.30.0",
    )


def _issue(path: str = "/repo/a.py", line: int = 1) -> Diagnostic:
    return Diagnostic(
        path=Path(path),
        line=line,
        column=1,
        end_line=line,
        end_column=2,
        rule="python:S100",
        message="m",
        severity=2,
    )


class FakeLs:
    """A language server that never starts a JVM.

    `states` is the sequence `progress()` walks through, one step per call, holding the
    last value once exhausted. That makes the settle logic testable without sleeping.
    """

    def __init__(
        self,
        *,
        states: list[tuple[int, int, int]] | None = None,
        issues: list[Diagnostic] | None = None,
        degraded: list[str] | None = None,
        logs: list[str] | None = None,
    ) -> None:
        self._states = states or [(1, 1, 0)]
        self._calls = 0
        self._issues = issues or []
        self._degraded = degraded or []
        self.logs = logs or []
        self.settings: dict[str, object] = {}
        self.token_provider = None
        self.initialized: list[tuple[Path | None, dict[str, object]]] = []
        self.opened: list[tuple[Path, str, str]] = []
        self.barriers: list[float] = []

    def progress(self) -> tuple[int, int, int]:
        state = self._states[min(self._calls, len(self._states) - 1)]
        self._calls += 1
        return state

    def initialize(self, root: Path | None, options: dict[str, object]) -> dict[str, object]:
        self.initialized.append((root, options))
        return {}

    def barrier(self, timeout: float = 300.0) -> None:
        self.barriers.append(timeout)

    def open_document(self, path: Path, text: str, language_id: str = "python") -> None:
        self.opened.append((path, text, language_id))

    def diagnostics(self) -> list[Diagnostic]:
        return list(self._issues)

    def diagnostic_count(self) -> int:
        return len(self._issues)

    def degraded_reasons(self) -> list[str]:
        return list(self._degraded)


# -- Result --------------------------------------------------------------


def test_result_defaults_are_all_negative() -> None:
    """A default Result must never look like a finished, connected, clean run with data."""
    result = Result()
    assert result.issues == []
    assert result.connected is False
    assert result.incomplete is False
    assert result.degraded_from_connected is False
    assert result.secrets == ()
    assert result.files_with_issues == 0


def test_files_with_issues_counts_distinct_paths() -> None:
    result = Result(
        issues=[_issue("/repo/a.py", 1), _issue("/repo/a.py", 9), _issue("/repo/b.py", 1)]
    )
    assert len(result.issues) == 3
    assert result.files_with_issues == 2


# -- _version and _connection_id -----------------------------------------


def test_version_is_the_package_version() -> None:
    from pysonarlint import __version__

    assert _version() == __version__


@pytest.mark.parametrize(
    ("binding", "expected"),
    [
        (Binding(url="https://sonar.example.com"), "https-sonar-example-com"),
        (Binding(url="https://sonar.example.com/"), "https-sonar-example-com"),
        (Binding(url="https://sonar.example.com/sonarqube"), "https-sonar-example-com-sonarqube"),
        (Binding(organization="my-org"), "my-org"),
        (Binding(organization="My_Org 2"), "My-Org-2"),
        (Binding(), "pysonarlint"),  # nothing to derive from
        (Binding(url="://"), "pysonarlint"),  # nothing but separators
    ],
)
def test_connection_id_mirrors_the_ide_derivation(binding: Binding, expected: str) -> None:
    assert _connection_id(binding) == expected


# -- _initialization_options ---------------------------------------------


def test_initialization_options_identify_us_and_disable_telemetry() -> None:
    options = _initialization_options(_cfg())
    assert options["productKey"] == "pysonarlint"
    assert options["productName"] == "pysonarlint"
    assert options["showVerboseLogs"] is False
    assert options["telemetryStorage"] is None  # explicit, not left to default
    assert options["enableNotebooks"] is True


def test_initialization_options_name_the_workspace() -> None:
    cfg = Config(root=Path("/some/where/my-project"))
    assert _initialization_options(cfg)["workspaceName"] == "my-project"


def test_standalone_options_carry_no_connection() -> None:
    options = _initialization_options(_cfg())
    assert "connectedModeConnections" not in options
    assert "connectedModeProject" not in options


def test_connected_server_options() -> None:
    cfg = _cfg(url="https://sonar.example.com", token=TOKEN, project_key="proj")
    options = _initialization_options(cfg)
    conn = options["connectedModeConnections"]["sonarqube"][0]  # type: ignore[index]
    assert conn["connectionId"] == "https-sonar-example-com"
    assert conn["serverUrl"] == "https://sonar.example.com"
    assert conn["token"] == TOKEN
    assert options["connectedModeProject"] == {
        "connectionId": "https-sonar-example-com",
        "projectKey": "proj",
    }


def test_connected_cloud_options_use_the_organization_and_region() -> None:
    cfg = _cfg(url="https://sonarcloud.io", token=TOKEN, organization="my-org", region="US")
    conn = _initialization_options(cfg)["connectedModeConnections"]["sonarcloud"][0]  # type: ignore[index]
    assert conn["connectionId"] == "my-org"
    assert conn["organizationKey"] == "my-org"
    assert conn["region"] == "US"
    assert "serverUrl" not in conn


def test_cloud_region_defaults_to_eu() -> None:
    cfg = _cfg(url="https://sonarcloud.io", token=TOKEN, organization="my-org")
    conn = _initialization_options(cfg)["connectedModeConnections"]["sonarcloud"][0]  # type: ignore[index]
    assert conn["region"] == "EU"


def test_connected_without_a_project_key_omits_the_binding() -> None:
    cfg = _cfg(url="https://sonar.example.com", token=TOKEN)
    options = _initialization_options(cfg)
    assert "connectedModeConnections" in options
    assert "connectedModeProject" not in options


# -- _workspace_settings -------------------------------------------------


def test_standalone_settings_are_minimal() -> None:
    assert _workspace_settings(_cfg()) == {"output": {"showAnalyzerLogs": False}}


def test_connected_server_settings() -> None:
    """Without these the server reports connections={} and quietly runs standalone."""
    cfg = _cfg(url="https://sonar.example.com", token=TOKEN, project_key="proj")
    settings = _workspace_settings(cfg)["connectedMode"]
    conn = settings["connections"]["sonarqube"][0]  # type: ignore[index]
    assert conn["serverUrl"] == "https://sonar.example.com"
    assert settings["project"] == {  # type: ignore[index]
        "connectionId": "https-sonar-example-com",
        "projectKey": "proj",
    }


def test_connected_cloud_settings() -> None:
    cfg = _cfg(url="https://sonarcloud.io", token=TOKEN, organization="org", region="US")
    settings = _workspace_settings(cfg)["connectedMode"]
    conn = settings["connections"]["sonarcloud"][0]  # type: ignore[index]
    assert conn["organizationKey"] == "org"
    assert conn["region"] == "US"


def test_cloud_settings_region_defaults_to_eu() -> None:
    cfg = _cfg(url="https://sonarcloud.io", token=TOKEN, organization="org")
    conn = _workspace_settings(cfg)["connectedMode"]["connections"]["sonarcloud"][0]  # type: ignore[index]
    assert conn["region"] == "EU"


# -- analyze() early exits -----------------------------------------------


def test_analyze_with_no_files_says_so_and_never_starts_a_jvm(tmp_path: Path) -> None:
    result = analyze([], _cfg(), _engine(tmp_path))
    assert result.notes == ["no analyzable files found"]
    assert result.issues == []
    assert result.incomplete is False


def test_analyze_reports_a_missing_analyzer_jar(tmp_path: Path) -> None:
    """A silently reduced analyzer set would read as 'clean'."""
    files = [SourceFile(path=tmp_path / "a.go", language="go")]
    result = analyze(files, _cfg(), _engine(tmp_path, analyzers=("sonarpython",)))
    assert result.notes == ["no analyzer jar available for: sonargo"]
    assert result.incomplete is False


def test_analyze_records_the_token_as_a_secret(tmp_path: Path) -> None:
    cfg = _cfg(url="https://s.example.com", token=TOKEN)
    result = analyze([], cfg, _engine(tmp_path))
    assert result.secrets == (TOKEN,)


def test_analyze_records_no_secret_when_standalone(tmp_path: Path) -> None:
    assert analyze([], _cfg(), _engine(tmp_path)).secrets == ()


def test_analyze_reports_a_dead_language_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed launch must be incomplete, never a clean zero-issue result."""
    import contextlib

    @contextlib.contextmanager
    def boom(*_a: object, **_k: object):  # noqa: ANN202 - a context-manager stand-in
        raise LspError("could not start language server: [Errno 2] no such file")
        yield  # pragma: no cover - unreachable, present only to make this a generator

    monkeypatch.setattr(an, "language_server", boom)
    files = [SourceFile(path=tmp_path / "a.py", language="python")]
    result = analyze(files, _cfg(), _engine(tmp_path), timeout=5.0)
    assert result.incomplete is True
    assert any("analysis failed" in n for n in result.notes)
    assert any("results may be partial" in n for n in result.notes)
    assert result.duration >= 0


def test_analyze_drives_a_full_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib

    source = tmp_path / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    ls = FakeLs(states=[(0, 0, 0), (1, 1, 1), (1, 1, 1), (1, 1, 1)], issues=[_issue()])

    @contextlib.contextmanager
    def fake_server(*_a: object, **_k: object):  # noqa: ANN202 - a context-manager stand-in
        yield ls

    monkeypatch.setattr(an, "language_server", fake_server)
    result = analyze(
        [SourceFile(path=source, language="python")],
        _cfg(),
        _engine(tmp_path),
        timeout=10.0,
        settle=0.0,
    )
    assert result.issues == [_issue()]
    assert result.analyzed == [source]
    assert result.incomplete is False
    assert ls.opened[0][0] == source


# -- _drop_unresolvable_project_key --------------------------------------


def test_a_standalone_run_never_asks_the_server_about_the_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pysonarlint.server.project_exists",
        lambda *_a: pytest.fail("standalone must not make a request"),
    )
    result = Result()
    _drop_unresolvable_project_key(_cfg(project_key="proj"), result)
    assert result.notes == []


def test_a_connected_run_without_a_project_key_asks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pysonarlint.server.project_exists", lambda *_a: pytest.fail("nothing to check")
    )
    _drop_unresolvable_project_key(_cfg(url="https://s.example.com", token=TOKEN), Result())


def test_an_unresolvable_project_key_is_dropped_but_the_connection_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The IDE behaves this way: server rules, just not a project-specific profile."""
    monkeypatch.setattr("pysonarlint.server.project_exists", lambda *_a: False)
    cfg = _cfg(url="https://s.example.com", token=TOKEN, project_key="gone")
    result = Result()
    _drop_unresolvable_project_key(cfg, result)
    assert cfg.binding.project_key is None
    assert cfg.connected is True  # url + token are untouched
    assert any("does not exist on" in n for n in result.notes)


@pytest.mark.parametrize("verdict", [True, None])
def test_a_resolvable_or_indeterminate_project_key_is_kept(
    verdict: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Indeterminate means 'assume present', so a transient failure never downgrades."""
    monkeypatch.setattr("pysonarlint.server.project_exists", lambda *_a: verdict)
    cfg = _cfg(url="https://s.example.com", token=TOKEN, project_key="proj")
    result = Result()
    _drop_unresolvable_project_key(cfg, result)
    assert cfg.binding.project_key == "proj"
    assert result.notes == []


# -- _await_file ---------------------------------------------------------


def test_await_file_returns_once_a_new_analysis_completes() -> None:
    ls = FakeLs(states=[(0, 0, 0), (0, 0, 0), (1, 1, 3)])
    assert _await_file(ls, 0, time.monotonic(), timeout=10.0) is True


def test_await_file_returns_immediately_when_already_past() -> None:
    ls = FakeLs(states=[(5, 0, 0)])
    assert _await_file(ls, 4, time.monotonic(), timeout=10.0) is True


def test_await_file_gives_up_on_timeout() -> None:
    """`started` is in the past, so the budget is already spent: no real waiting."""
    ls = FakeLs(states=[(0, 0, 0)])
    assert _await_file(ls, 0, time.monotonic() - 100.0, timeout=1.0) is False


# -- _await_settle -------------------------------------------------------


def test_settle_requires_a_reported_completion_not_just_quiet() -> None:
    """At the start nothing has happened, so 'no change' is trivially true. Requiring
    positive evidence is what stops a file with plenty of issues reporting zero."""
    ls = FakeLs(states=[(0, 0, 0)])
    assert _await_settle(ls, time.monotonic() - 100.0, timeout=1.0, settle=0.0) is False


def test_settle_succeeds_once_a_completion_is_reported_and_things_go_quiet() -> None:
    ls = FakeLs(states=[(0, 0, 0), (1, 1, 5), (1, 1, 5)])
    assert _await_settle(ls, time.monotonic(), timeout=10.0, settle=0.0) is True


def test_settle_waits_out_a_changing_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settle window must not close while results are still arriving.

    Driven by a fake clock: each loop pass advances 1s, so a 2.5s settle window cannot
    close until two consecutive passes see the same state.
    """
    ls = FakeLs(states=[(1, 1, 1), (1, 1, 2), (1, 1, 3), (1, 1, 3), (1, 1, 3), (1, 1, 3)])
    clock = iter(range(0, 100))
    monkeypatch.setattr(an.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(an.time, "sleep", lambda _s: None)
    assert _await_settle(ls, 0.0, timeout=50.0, settle=2.5) is True
    # Three changing passes, then the window opens and needs 2.5s of quiet on top.
    assert ls._calls >= 5


def test_settle_gives_up_when_the_budget_is_spent() -> None:
    ls = FakeLs(states=[(0, 0, 0)])
    assert _await_settle(ls, time.monotonic() - 100.0, timeout=1.0, settle=99.0) is False


# -- _open_documents -----------------------------------------------------


def test_documents_are_opened_one_at_a_time(tmp_path: Path) -> None:
    """Batching didOpen makes the server publish empty diagnostics for the whole batch,
    producing '0 issues' on files that definitely have some."""
    files = []
    for name in ("a.py", "b.py", "c.py"):
        path = tmp_path / name
        path.write_text("x = 1\n", encoding="utf-8")
        files.append(SourceFile(path=path, language="python"))
    # One completion per file, so each _await_file returns after exactly one step.
    ls = FakeLs(states=[(n, n, n) for n in range(0, 12)])
    result = Result()
    opened, unread = _open_documents(
        ls, files, result, Progress(enabled=False), started=time.monotonic(), timeout=30.0
    )
    assert (opened, unread) == (3, 0)
    assert [p.name for p, _t, _l in ls.opened] == ["a.py", "b.py", "c.py"]
    assert result.analyzed == [f.path for f in files]


def test_an_unreadable_file_is_counted_not_fatal(tmp_path: Path) -> None:
    good = tmp_path / "a.py"
    good.write_text("x = 1\n", encoding="utf-8")
    files = [
        SourceFile(path=tmp_path / "missing.py", language="python"),
        SourceFile(path=good, language="python"),
    ]
    ls = FakeLs(states=[(n, n, n) for n in range(0, 8)])
    result = Result()
    opened, unread = _open_documents(
        ls, files, result, Progress(enabled=False), started=time.monotonic(), timeout=30.0
    )
    assert (opened, unread) == (1, 1)
    assert result.analyzed == [good]


def test_a_timed_out_file_stops_the_run_and_says_which(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    files = [SourceFile(path=tmp_path / n, language="python") for n in ("a.py", "b.py")]
    ls = FakeLs(states=[(0, 0, 0)])  # never completes
    result = Result()
    opened, _unread = _open_documents(
        ls, files, result, Progress(enabled=False), started=time.monotonic() - 100.0, timeout=1.0
    )
    assert opened == 1  # it stopped after the first
    assert any("timed out waiting for a.py" in n for n in result.notes)
    assert any("later files were not analyzed" in n for n in result.notes)


# -- _record_outcome -----------------------------------------------------


def test_a_server_reported_downgrade_is_never_labelled_connected() -> None:
    """Trust the server over our own intent; the issues are still valid."""
    cfg = _cfg(url="https://s.example.com", token=TOKEN)
    ls = FakeLs(degraded=["No token for connection https-s-example-com"])
    result = Result(connected=True, issues=[_issue()])
    _record_outcome(ls, cfg, result, settled=True)
    assert result.connected is False
    assert result.degraded_from_connected is True
    assert result.incomplete is False  # a downgrade is not a failure
    assert result.issues == [_issue()]
    assert any("analyzed with local rules instead" in n for n in result.notes)


def test_no_downgrade_note_when_the_server_is_happy() -> None:
    cfg = _cfg(url="https://s.example.com", token=TOKEN)
    result = Result(connected=True)
    _record_outcome(FakeLs(), cfg, result, settled=True)
    assert result.connected is True
    assert result.degraded_from_connected is False
    assert result.notes == []


def test_a_standalone_run_ignores_degradation_messages() -> None:
    result = Result()
    _record_outcome(FakeLs(degraded=["falling back to standalone"]), _cfg(), result, settled=True)
    assert result.notes == []


def test_results_that_arrived_are_kept_even_if_the_settle_window_expired() -> None:
    """Real findings must not be thrown away just because the wait ran long."""
    result = Result(issues=[_issue()])
    _record_outcome(FakeLs(states=[(1, 1, 1)]), _cfg(), result, settled=False)
    assert result.incomplete is False
    assert result.notes == ["analysis timed out after producing results"]


def test_producing_nothing_at_all_is_incomplete() -> None:
    result = Result()
    _record_outcome(FakeLs(states=[(0, 0, 0)]), _cfg(), result, settled=False)
    assert result.incomplete is True
    assert result.notes == []


# -- _run_session --------------------------------------------------------


def test_run_session_wires_settings_and_the_token_provider(tmp_path: Path) -> None:
    source = tmp_path / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    cfg = _cfg(url="https://s.example.com", token=TOKEN, project_key="proj")
    ls = FakeLs(states=[(n, n, n) for n in range(0, 10)], issues=[_issue()], logs=["[Info] hi"])
    result = Result()
    _run_session(
        ls,
        [SourceFile(path=source, language="python")],
        cfg,
        result,
        Progress(enabled=False),
        started=time.monotonic(),
        timeout=30.0,
        settle=0.0,
    )
    assert ls.settings == _workspace_settings(cfg)
    assert ls.token_provider is not None
    assert ls.token_provider("any-connection-id") == TOKEN
    assert ls.initialized[0][0] == cfg.root
    assert result.issues == [_issue()]
    assert result.logs == ["[Info] hi"]


def test_run_session_leaves_the_token_provider_unset_when_standalone(tmp_path: Path) -> None:
    source = tmp_path / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    ls = FakeLs(states=[(n, n, n) for n in range(0, 10)])
    _run_session(
        ls,
        [SourceFile(path=source, language="python")],
        _cfg(),
        Result(),
        Progress(enabled=False),
        started=time.monotonic(),
        timeout=30.0,
        settle=0.0,
    )
    assert ls.token_provider is None


def test_the_warmup_barrier_cannot_consume_the_whole_budget(tmp_path: Path) -> None:
    """In connected mode this covers storage sync, which can be slow."""
    source = tmp_path / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    ls = FakeLs(states=[(n, n, n) for n in range(0, 10)])
    _run_session(
        ls,
        [SourceFile(path=source, language="python")],
        _cfg(),
        Result(),
        Progress(enabled=False),
        started=time.monotonic(),
        timeout=1000.0,
        settle=0.0,
    )
    assert ls.barriers == [180.0]  # capped, not 40% of 1000


def test_the_warmup_barrier_scales_down_for_a_short_timeout(tmp_path: Path) -> None:
    source = tmp_path / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    ls = FakeLs(states=[(n, n, n) for n in range(0, 10)])
    _run_session(
        ls,
        [SourceFile(path=source, language="python")],
        _cfg(),
        Result(),
        Progress(enabled=False),
        started=time.monotonic(),
        timeout=100.0,
        settle=0.0,
    )
    assert ls.barriers == [40.0]


def test_run_session_reports_when_nothing_could_be_read(tmp_path: Path) -> None:
    ls = FakeLs(states=[(1, 1, 0)])
    result = Result()
    _run_session(
        ls,
        [SourceFile(path=tmp_path / "gone.py", language="python")],
        _cfg(),
        result,
        Progress(enabled=False),
        started=time.monotonic(),
        timeout=30.0,
        settle=0.0,
    )
    assert "could not read 1 file(s)" in result.notes
    assert "no files could be read" in result.notes
    assert result.issues == []  # it returned before collecting anything
