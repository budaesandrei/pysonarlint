"""Run an analysis: boot the engine, open documents, collect issues.

The server publishes diagnostics as unsolicited notifications with no completion
marker, so the shape of "when are we done" is the crux. We use an ordered-request
barrier plus a settle window that only extends while results are still arriving.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .collect import SourceFile, analyzers_for, read_text
from .config import Config
from .engine import Engine
from .lsp import Diagnostic, LspError, language_server
from .progress import Progress

# How long to keep waiting after the last new diagnostic before declaring completion.
_SETTLE_SECONDS = 2.5
# Absolute ceiling per run, regardless of activity.
_DEFAULT_TIMEOUT = 600.0


@dataclass
class Result:
    """Outcome of one analysis run."""

    issues: list[Diagnostic] = field(default_factory=list)
    analyzed: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    connected: bool = False
    engine_version: str = ""
    # Values scrubbed from notes and logs before they are rendered. Set by the caller
    # so redaction cannot be forgotten at an individual output site.
    secrets: tuple[str, ...] = ()
    duration: float = 0.0
    incomplete: bool = False
    # True when connected mode was configured but the server did not apply it. The
    # analysis is still valid; only the rule set differs.
    degraded_from_connected: bool = False

    @property
    def files_with_issues(self) -> int:
        return len({i.path for i in self.issues})


def _initialization_options(cfg: Config) -> dict[str, object]:
    """Options for the initialize request.

    productKey identifies us to the server. Telemetry is disabled explicitly rather
    than left to default.
    """
    options: dict[str, object] = {
        "productKey": "pysonarlint",
        "productName": "pysonarlint",
        "productVersion": _version(),
        "workspaceName": cfg.root.name,
        "showVerboseLogs": False,
        "telemetryStorage": None,
        "enableNotebooks": True,
        "additionalAttributes": {},
        "clientNodePath": "",
    }
    if cfg.connected:
        binding = cfg.binding
        connection_id = _connection_id(binding)
        if binding.is_cloud:
            options["connectedModeConnections"] = {
                "sonarcloud": [
                    {
                        "connectionId": connection_id,
                        "organizationKey": binding.organization,
                        "region": binding.region or "EU",
                        "token": binding.token,
                    }
                ]
            }
        else:
            options["connectedModeConnections"] = {
                "sonarqube": [
                    {
                        "connectionId": connection_id,
                        "serverUrl": binding.url,
                        "token": binding.token,
                    }
                ]
            }
        if binding.project_key:
            options["connectedModeProject"] = {
                "connectionId": connection_id,
                "projectKey": binding.project_key,
            }
    return options


def _workspace_settings(cfg: Config) -> dict[str, object]:
    """The `sonarlint.*` settings block the server fetches via workspace/configuration.

    Shaped exactly like the VS Code extension's settings, because that is what the
    language server parses.
    """
    settings: dict[str, object] = {"output": {"showAnalyzerLogs": False}}
    if not cfg.connected:
        return settings

    binding = cfg.binding
    connection_id = _connection_id(binding)
    if binding.is_cloud:
        settings["connectedMode"] = {
            "connections": {
                "sonarcloud": [
                    {
                        "connectionId": connection_id,
                        "organizationKey": binding.organization,
                        "region": binding.region or "EU",
                        "token": binding.token,
                    }
                ]
            },
            "project": {"connectionId": connection_id, "projectKey": binding.project_key},
        }
    else:
        settings["connectedMode"] = {
            "connections": {
                "sonarqube": [
                    {
                        "connectionId": connection_id,
                        "serverUrl": binding.url,
                        "token": binding.token,
                    }
                ]
            },
            "project": {"connectionId": connection_id, "projectKey": binding.project_key},
        }
    return settings


# Takes a Binding; annotating it here would create a circular import.
def _connection_id(binding) -> str:  # noqa: ANN001
    import re

    seed = binding.organization if binding.is_cloud else (binding.url or "")
    return re.sub(r"[^a-z\d]+", "-", seed.rstrip("/"), flags=re.I).strip("-") or "pysonarlint"


def _version() -> str:
    from . import __version__

    return __version__


def analyze(
    files: list[SourceFile],
    cfg: Config,
    engine: Engine,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    verbose: bool = False,
    settle: float = _SETTLE_SECONDS,
    progress: Progress | None = None,
) -> Result:
    """Analyze `files` and return everything the server reported."""
    reporter = progress or Progress(enabled=False)
    result = Result(
        connected=cfg.connected,
        engine_version=engine.version,
        secrets=tuple(s for s in (cfg.binding.token,) if s),
    )
    if not files:
        result.notes.append("no analyzable files found")
        return result

    # Load only the analyzers this run needs. Passing the full set makes the server
    # try to fetch optional extras over the network, which fails noisily on
    # TLS-inspecting corporate networks.
    needed = analyzers_for(files)
    jars = tuple(j for name in sorted(needed) if (j := engine.analyzer(name)))
    if not jars:
        result.notes.append(f"no analyzer jar available for: {', '.join(sorted(needed))}")
        return result

    _drop_unresolvable_project_key(cfg, result)

    started = time.monotonic()
    reporter.start("starting analysis engine (JVM)...")
    try:
        with language_server(engine.java, engine.server_jar, jars, verbose=verbose) as ls:
            _run_session(
                ls,
                files,
                cfg,
                result,
                reporter,
                started=started,
                timeout=timeout,
                settle=settle,
            )
    except LspError as exc:
        result.notes.append(f"analysis failed: {exc}")
        result.incomplete = True
    finally:
        reporter.stop()
        result.duration = time.monotonic() - started

    if result.incomplete:
        result.notes.append(
            f"analysis did not finish within {timeout:.0f}s; results may be partial"
        )
    return result


def _drop_unresolvable_project_key(cfg: Config, result: Result) -> None:
    """Discard a project key the server does not know about, keeping the connection."""
    # A project key that does not resolve must not break the run. Drop it and keep the
    # connection, which is what the IDE effectively does: you still get the server's
    # analyzers and default profile rather than an error.
    if not (cfg.connected and cfg.binding.project_key):
        return
    from .server import project_exists

    present = project_exists(cfg.binding.url or "", cfg.binding.token or "", cfg.binding.project_key)
    if present is False:
        result.notes.append(
            f"project '{cfg.binding.project_key}' does not exist on "
            f"{cfg.binding.url}; using the server's default rules instead of a "
            "project-specific profile"
        )
        cfg.binding.project_key = None


def _run_session(  # noqa: ANN001
    ls,
    files: list[SourceFile],
    cfg: Config,
    result: Result,
    reporter: Progress,
    *,
    started: float,
    timeout: float,
    settle: float,
) -> None:
    """Drive one initialized language-server session to completion."""
    if cfg.connected and cfg.binding.token:
        token = cfg.binding.token
        ls.token_provider = lambda _sid: token
    # The server pulls these via workspace/configuration; without them it
    # reports connections={} and quietly analyzes in standalone.
    ls.settings = _workspace_settings(cfg)

    ls.initialize(cfg.root, _initialization_options(cfg))

    # Warm-up barrier, before any document is opened. In connected mode this
    # also covers storage sync, which can be slow, but it must not be able to
    # consume the whole budget.
    reporter.set_message(
        "syncing with server..." if cfg.connected else "loading rules..."
    )
    ls.barrier(timeout=min(timeout * 0.4, 180.0))
    reporter.set_total(len(files))

    opened, unread = _open_documents(
        ls, files, result, reporter, started=started, timeout=timeout
    )

    if unread:
        result.notes.append(f"could not read {unread} file(s)")
    if not opened:
        result.notes.append("no files could be read")
        return

    settled = _await_settle(ls, started, timeout, settle)
    result.issues = ls.diagnostics()
    result.logs = ls.logs
    _record_outcome(ls, cfg, result, settled=settled)


def _open_documents(  # noqa: ANN001
    ls,
    files: list[SourceFile],
    result: Result,
    reporter: Progress,
    *,
    started: float,
    timeout: float,
) -> tuple[int, int]:
    """Open every readable document, waiting for each. Returns (opened, unread)."""
    # Open documents ONE AT A TIME, waiting for each analysis to complete.
    #
    # Sending every didOpen up front looks faster and is catastrophically
    # wrong: the server debounces rapid opens into batches, and a batched
    # analysis publishes empty diagnostics for every document in it. The
    # result is "0 issues" on files that definitely have issues, with a
    # successful exit code and nothing in the log to suggest a problem.
    # Verified against a 32-file project: batched found 0, sequential found
    # every issue including ones a single-file run reported.
    opened = 0
    unread = 0
    for source in files:
        text = read_text(source.path)
        if text is None:
            unread += 1
            continue
        before = ls.progress()[0]
        reporter.set_message(source.path.name)
        ls.open_document(source.path, text, source.language)
        result.analyzed.append(source.path)
        opened += 1
        ok = _await_file(ls, before, started, timeout)
        reporter.advance(issues=ls.diagnostic_count(), name=source.path.name)
        if not ok:
            result.notes.append(
                f"timed out waiting for {source.path.name}; "
                "later files were not analyzed"
            )
            break
    return opened, unread


def _record_outcome(ls, cfg: Config, result: Result, *, settled: bool) -> None:  # noqa: ANN001
    """Note connected-mode downgrades and whether the run actually finished."""
    # Trust the server over our own intent: if it says the binding was not
    # applied, this was a standalone run and must not be reported otherwise.
    # This is a downgrade, not a failure - the analysis itself is still valid,
    # so it is reported as a note and the issues are kept.
    if cfg.connected and (reasons := ls.degraded_reasons()):
        result.connected = False
        result.degraded_from_connected = True
        result.notes.append(
            "could not use connected mode, analyzed with local rules instead: "
            + "; ".join(reasons)
        )

    # Results that arrived are real even if the settle window expired. Only
    # treat the run as incomplete when nothing was produced at all.
    if not settled:
        if ls.progress()[0] > 0 or result.issues:
            result.notes.append("analysis timed out after producing results")
        else:
            result.incomplete = True


def _await_file(ls, completed_before: int, started: float, timeout: float) -> bool:  # noqa: ANN001
    """Wait for one document's analysis to complete.

    Returns False only on timeout. Completion is the server's own "Analysis detected"
    line, which is why documents must be opened one at a time: a batched analysis
    emits a single completion covering files whose diagnostics were never produced.
    """
    while ls.progress()[0] <= completed_before:
        if time.monotonic() - started >= timeout:
            return False
        time.sleep(0.1)
    return True


def _await_settle(ls, started: float, timeout: float, settle: float) -> bool:  # noqa: ANN001
    """Wait for analysis to genuinely finish. True if it completed, False on timeout.

    The request barrier only proves the documents were received; rule execution runs
    asynchronously and results can land several seconds later. Waiting for activity to
    go quiet is therefore not enough on its own -- at the start nothing has happened
    yet, so "no change for N seconds" is trivially true and we would report zero
    issues for a file that has plenty.

    So we require positive evidence: at least one analysis completion logged by the
    server, and then a quiet period. Only if the server never reports completion do we
    fall back to quiescence, and that path is reported as incomplete.
    """
    quiet_since: float | None = None
    last_state: tuple[int, int, int] | None = None

    while True:
        now = time.monotonic()
        state = ls.progress()
        analyses, _published, _diags = state

        if state != last_state:
            last_state = state
            quiet_since = now
        elif quiet_since is None:
            quiet_since = now

        # Completion reported and nothing new for a moment: genuinely done.
        if analyses > 0 and now - quiet_since >= settle:
            return True

        if now - started >= timeout:
            return False
        time.sleep(0.1)
