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
) -> Result:
    """Analyze `files` and return everything the server reported."""
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

    # A project key that does not resolve must not break the run. Drop it and keep the
    # connection, which is what the IDE effectively does: you still get the server's
    # analyzers and default profile rather than an error.
    if cfg.connected and cfg.binding.project_key:
        from .server import project_exists

        present = project_exists(cfg.binding.url or "", cfg.binding.token or "", cfg.binding.project_key)
        if present is False:
            result.notes.append(
                f"project '{cfg.binding.project_key}' does not exist on "
                f"{cfg.binding.url}; using the server's default rules instead of a "
                "project-specific profile"
            )
            cfg.binding.project_key = None

    started = time.monotonic()
    try:
        with language_server(engine.java, engine.server_jar, jars, verbose=verbose) as ls:
            if cfg.connected and cfg.binding.token:
                token = cfg.binding.token
                ls.token_provider = lambda _sid: token
            # The server pulls these via workspace/configuration; without them it
            # reports connections={} and quietly analyzes in standalone.
            ls.settings = _workspace_settings(cfg)

            ls.initialize(cfg.root, _initialization_options(cfg))

            opened = 0
            for source in files:
                text = read_text(source.path)
                if text is None:
                    result.notes.append(f"could not read {source.path}")
                    continue
                ls.open_document(source.path, text, source.language)
                result.analyzed.append(source.path)
                opened += 1

            if not opened:
                result.notes.append("no files could be read")
                return result

            # Barrier: proves the server has processed every didOpen above. In connected
            # mode this also covers storage sync, which can be slow, but it must not be
            # able to consume the entire budget.
            ls.barrier(timeout=min(timeout * 0.6, 240.0))
            settled = _await_settle(ls, started, timeout, settle)
            result.issues = ls.diagnostics()
            result.logs = ls.logs

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
    except LspError as exc:
        result.notes.append(f"analysis failed: {exc}")
        result.incomplete = True
    finally:
        result.duration = time.monotonic() - started

    if result.incomplete:
        result.notes.append(
            f"analysis did not finish within {timeout:.0f}s; results may be partial"
        )
    return result


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
