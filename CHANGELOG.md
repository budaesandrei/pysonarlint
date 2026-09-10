# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
version is `0.x`, the CLI surface may change in a minor release.

## [Unreleased]

### Added

- Live progress on stderr: file count, a percentage bar, the current file and a running
  issue count, plus named phases so the JVM start no longer looks like a hang. Silences
  itself when stderr is not a terminal, so piped `json`/`sarif` output is unaffected.
  `--no-progress` disables it.

### Fixed

- **Analysis reported no issues for files that had them.** Sending every `didOpen` at
  once made the server batch the documents, and a batched analysis publishes empty
  diagnostics for every file in it. Documents are now opened one at a time, each waiting
  for the analyzer's own completion signal.
- **Empty publications erased real findings.** The server re-publishes `[]` for finished
  documents as new ones open; honouring that wiped results already collected. An empty
  publication can no longer clear findings.
- `--sonarlint-home` reported `engine unknown`, because the version came from the
  directory name. It now falls back to the language server jar's manifest.

## [0.1.0] - 2026-09-10

First release.

### Added

- `pysonarlint analyze` (the default command): reports the issues SonarQube for IDE would
  highlight, by driving the analyzers from an existing editor-extension install.
- Engine discovery across VS Code, VS Code Insiders, VS Code Server, Cursor, Windsurf,
  VSCodium and Kiro, using the extension's own bundled JRE so Java need not be on `PATH`.
  Override with `--sonarlint-home` / `PYSONARLINT_HOME` and `PYSONARLINT_JAVA`.
- Output formats: `text`, `json` (a stable contract for coding agents, including a
  `ruleUrl` per issue), `sarif` (2.1.0) and `github` (Actions annotations).
- Exit codes `0` clean / `1` issues found / `2` tool failure, with `--fail-on` and
  `--severity` thresholds.
- `pysonarlint status`, which reports the resolved engine, root and binding along with the
  source of every configuration value.
- Optional connected mode, enabled only when a server URL *and* a token both resolve.
  Discovery from CLI flags, environment, `.sonarlint/connectedMode.json`,
  `sonar-project.properties`, editor `settings.json` and `pyproject.toml`
  `[tool.pysonarlint]`.
- `pysonarlint login` / `logout`: the browser token-grant handshake, with tokens
  DPAPI-encrypted on Windows and stored mode `0600` elsewhere.
- Language selection via `--language` and `--all-languages`; Python only by default.

### Security

- TLS verification cannot be disabled. A private CA is supported through `SSL_CERT_FILE`,
  which keeps verification on.
- Secrets are scrubbed from notes, logs and every machine-readable format, by exact match
  and by token-shaped pattern, so a credential cannot reach a bug report or a
  code-scanning upload.

### Known limitations

- Connected mode does not yet apply a server-side quality profile. The connection
  registers and preflight passes, but the language server declines the binding and falls
  back to local rules. Such runs are labelled `standalone` rather than misreported.
- Each run starts a JVM, so expect seconds rather than milliseconds. Pass specific paths
  rather than a whole repository when speed matters.

[Unreleased]: https://github.com/budaesandrei/pysonarlint/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/budaesandrei/pysonarlint/releases/tag/v0.1.0
