# Security policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub's private vulnerability reporting](https://github.com/budaesandrei/pysonarlint/security/advisories/new)
rather than a public issue.

Include what you found, how to reproduce it, and what an attacker could achieve. I will
acknowledge within a few days. This is a personal project maintained in spare time, so
please allow reasonable time for a fix before public disclosure.

## Supported versions

The latest release only. While the project is `0.x` there are no backports.

## What is in scope

pysonarlint handles SonarQube credentials and executes a JVM, so the areas worth
scrutinising are:

- **Credential disclosure.** Tokens must never appear in `--format json`, `sarif`,
  `github`, in notes, or in `--verbose` server logs. Secrets are scrubbed by exact match
  and by token-shaped pattern; a bypass is a valid report.
- **Credential storage.** DPAPI-encrypted on Windows; **plaintext with mode `0600`** on
  macOS and Linux. The latter is documented rather than hidden: anything running as your
  user can read it. Prefer `SONAR_TOKEN` in the environment if that matters to you.
- **The token-grant listener.** `pysonarlint login` binds a loopback HTTP server on
  ports 64120-64130 to receive the token. It accepts only loopback connections and shuts
  down as soon as the grant completes or times out. Reports about that window are welcome.
- **Process execution.** The tool executes a JRE and jars discovered on disk. A path that
  could be influenced by an untrusted party into executing something unexpected is in
  scope.
- **TLS.** Verification cannot be disabled by any flag. A code path that weakens it is a
  valid report.

## What is out of scope

- Vulnerabilities in SonarSource's own analyzers, language server or bundled JRE. Report
  those to SonarSource. pysonarlint executes that software but does not ship it.
- Issues requiring an attacker who already has code-execution as your user.
