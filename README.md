# pysonarlint

[![CI](https://github.com/budaesandrei/pysonarlint/actions/workflows/ci.yml/badge.svg)](https://github.com/budaesandrei/pysonarlint/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/budaesandrei/pysonarlint/branch/main/graph/badge.svg)](https://codecov.io/gh/budaesandrei/pysonarlint)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

<!-- Uncomment once published to PyPI; until then these render "package not found".
[![PyPI](https://img.shields.io/pypi/v/pysonarlint.svg)](https://pypi.org/project/pysonarlint/)
[![Python versions](https://img.shields.io/pypi/pyversions/pysonarlint.svg)](https://pypi.org/project/pysonarlint/)
[![Downloads](https://img.shields.io/pypi/dm/pysonarlint.svg)](https://pypi.org/project/pysonarlint/)
-->

The issues SonarQube for IDE (formerly SonarLint) would highlight, from your terminal.

```console
$ pysonarlint src/
src/app/core/client.py
      98:9  warning  Refactor this function to reduce its Cognitive Complexity from 21 to the 15 allowed.  python:S3776
     218:16  warning  Enable server certificate validation on this SSL/TLS connection.  python:S4830

src/app/api/routes.py
     308:5  warning  Use "Annotated" type hints for FastAPI dependency injection  python:S8410
     542:15  warning  Document this HTTPException with status code 500 in the "responses" parameter.  python:S8415

31 issues in 10 of 32 files (31 warning)
standalone mode, engine 5.9.1, 31.2s
```

These are the real SonarSource rules, not an approximation. pysonarlint drives the same
analyzers your IDE uses, so a finding here is a finding there.

## Why not just use pylint or ruff?

They are excellent and you should use them too. Sonar's rule set is different in kind:
framework-aware rules (`S8410` on FastAPI dependency injection, `S8415` on undocumented
`HTTPException` responses), security rules (`S4830` on disabled certificate validation),
cognitive-complexity metrics, and ReDoS detection in regular expressions. If your team
already gates merges on SonarQube, this is how you see those findings before you push.

## Install

```console
pip install pysonarlint
```

pysonarlint is pure Python with **no dependencies**. It does need the SonarSource
analyzers, which are Java and not redistributable, so it reuses an existing
**SonarQube for IDE** installation:

1. Install the *SonarQube for IDE* extension in VS Code (or Cursor, Windsurf, VSCodium,
   Kiro, or a VS Code Server / devcontainer).
2. That is all. The extension bundles its own Java runtime, so you do not need Java on
   your `PATH`.

Check what was found:

```console
$ pysonarlint status
engine     5.9.1  ~/.vscode/extensions/sonarsource.sonarlint-vscode-5.9.1-win32-x64
java       ~/.vscode/extensions/.../jre/21.0.12.1-win32-x86_64.tar/bin/java.exe
analyzers  13 (sonarpython, sonarjs, sonarjava, sonariac, sonarhtml, ...)
root       ~/work/my-project
mode       standalone
```

Point it elsewhere with `--sonarlint-home` or `PYSONARLINT_HOME`, and override the JRE
with `PYSONARLINT_JAVA`.

## Usage

```console
pysonarlint                          # analyze the current directory
pysonarlint src/ tests/              # specific paths
pysonarlint app/main.py              # a single file
pysonarlint --severity error         # only the worst
pysonarlint -f json                  # machine-readable
pysonarlint -f sarif -o results.sarif
pysonarlint --all-languages          # not just Python
```

Python only by default. Add `--language js --language terraform`, or `--all-languages`.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | No issues at or above the threshold |
| `1` | Issues found |
| `2` | The tool itself failed, or the analysis could not complete |

`2` is never used for "found issues", and a run that cannot finish never reports `0`.
Tune the threshold with `--fail-on error` (or `--fail-on never` to always exit `0`).

### Output formats

| `--format` | For |
|------------|-----|
| `text` | humans (default; colour when the terminal supports it) |
| `json` | agents and scripts; stable keys, includes a `ruleUrl` per issue |
| `sarif` | GitHub code scanning and other SARIF 2.1.0 consumers |
| `github` | inline `::warning file=...` annotations in Actions |

## Connected mode

Standalone needs no configuration and no token. Connected mode additionally applies your
server's quality profile and its resolved issues, and it turns on **only when both** a
server URL and a token are available. Anything less stays standalone and says why:

```console
$ pysonarlint
...
note: standalone: found server https://sonar.example.com (from sonar-project.properties)
      but no token. Set SONAR_TOKEN, or run 'pysonarlint login' to grant one.
```

Get a token through the browser, exactly as the IDE does:

```console
$ pysonarlint login
server https://sonar.example.com is UP, version 2025.6.1.117629
opening https://sonar.example.com/sonarlint/auth?ideName=pysonarlint&port=64120
waiting for the browser...
token verified and saved to ~/AppData/Roaming/pysonarlint/credentials.json (DPAPI-encrypted)
```

**Where tokens are stored.** On Windows, encrypted with DPAPI and bound to your OS
account. On macOS and Linux, **as plaintext in a file with mode `0600`** (owner-only) --
readable by anything running as you, and by root. If that is not acceptable, do not use
`login`: set `SONAR_TOKEN` in your environment or a secrets manager instead, which
pysonarlint prefers over stored credentials anyway. `pysonarlint logout` removes a
stored token.

> **Use a User token.** SonarQube's *Global Analysis* and *Project Analysis* tokens are
> restricted to submitting analysis reports and cannot read the project data connected
> mode needs. If you paste one, pysonarlint tells you so instead of failing obscurely.
> Mint one at `<server>/account/security/` with **Type: User**.

### Degradation is never a failure

Connected mode is an enhancement, so losing it costs you the server's rule set and
nothing else. The analysis always runs:

| Situation | Behaviour |
|---|---|
| No token | Standalone, with a note saying how to get one |
| Project key not found on the server | Key dropped, connection kept, server defaults used |
| Server unreachable, or token rejected | Standalone, with the reason |
| Server declines the binding | Standalone, quoting the server's own explanation |

In every case you still get issues and a normal exit code. `--format json` sets
`summary.degradedFromConnected` so an agent can tell that the rule set was local, and
the reason appears in `notes`.

**Current limitation:** connected mode does not yet apply a server profile in practice.
The connection registers and preflight passes, but the language server reports
`No token for connection` and analyzes with local rules. Runs are correctly labelled
`standalone` when this happens rather than claiming otherwise, so results are never
misrepresented; standalone analysis is unaffected.

### Configuration discovery

Resolved in this order, first match winning. The source of every value is reported by
`pysonarlint status`, so precedence is never a mystery.

| | Source | Supplies |
|---|--------|----------|
| 1 | `--server-url`, `--token`, `--project-key`, `--organization` | everything |
| 2 | `SONAR_TOKEN` / `SONARQUBE_TOKEN`, `SONAR_HOST_URL` / `SONARQUBE_URL`, `SONARQUBE_ORG`, `SONAR_REGION` | everything |
| 3 | `.sonarlint/connectedMode.json` | URL, project key, organization |
| 4 | `sonar-project.properties`, `.sonarcloud.properties` | URL, project key, exclusions |
| 5 | `.vscode/settings.json` + editor user settings | project key, URL, token |
| 6 | `pyproject.toml` `[tool.pysonarlint]` | URL, project key, exclusions |
| 7 | stored credentials from `pysonarlint login` | token |

Config is searched from the target up to the repository root, so it works from any
subdirectory. `[tool.pysonarlint]` is our own table: no Sonar tool reads `pyproject.toml`
for IDE bindings, and `[tool.sonar]` belongs to the `pysonar` CI scanner, so we do not
touch it.

Behind a corporate proxy, the standard `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`
variables are honoured. Internal Sonar hosts usually need to be in `NO_PROXY`. For a
private CA, point `SSL_CERT_FILE` at a PEM bundle.

## Use with Claude Code and other agents

The `json` format is designed for this. Add a rule to `CLAUDE.md`:

```markdown
After changing Python files, run `pysonarlint -f json --severity warning` and fix
what it reports. Each issue includes a `ruleUrl` explaining the rule. Do not suppress
issues to make the output clean.
```

Or wire it into a hook so it runs automatically after edits. The payload is stable:

```json
{
  "version": 1,
  "mode": "standalone",
  "summary": { "issues": 2, "filesAnalyzed": 32, "incomplete": false },
  "issues": [
    {
      "file": "app/core/client.py",
      "line": 218,
      "column": 16,
      "rule": "python:S4830",
      "severity": "warning",
      "message": "Enable server certificate validation on this SSL/TLS connection.",
      "ruleUrl": "https://rules.sonarsource.com/python/RSPEC-4830/"
    }
  ]
}
```

Always check `summary.incomplete`. If it is `true`, the issue list is partial and should
not be treated as authoritative.

## Performance

Each run boots a JVM, which costs a few seconds; a 32-file project takes roughly 30
seconds end to end. Analysis is genuinely finished when the tool exits: completion is read
from the analyzer's own signal rather than inferred from a quiet period, because a timing
heuristic happily reports zero issues on a file that simply took a moment.

Prefer passing specific paths over analyzing a whole repository on every save.

## Prior art

[`sonarlint-ls-cli`](https://github.com/vincentfenet/sonarlint-ls-cli) explored the same
idea. pysonarlint differs in ways that matter in practice: it discovers the engine and JRE
itself, waits for a real completion signal instead of an idle timer, treats an
unfinishable run as an error rather than a clean result, supports connected mode with a
browser token grant, and emits JSON and SARIF.

`sonar-scanner-cli` is not an alternative for local use: it contains no analysis engine,
requires a server and a token even to start, and computes results server-side rather than
printing them.

## Trademarks and affiliation

pysonarlint is an independent, unofficial, community-maintained project. It is **not** a
Sonar product and is **not** affiliated with, sponsored by, endorsed by, or supported by
SonarSource SA.

Sonar™, SonarSource™, SonarQube™, SonarQube for IDE™ and SonarLint™ are trademarks of
SonarSource SA. All other trademarks are the property of their respective owners. These
marks appear here solely to describe, factually, the third-party software this tool
interoperates with.

This project bundles, redistributes and mirrors **no** SonarSource code, binaries or
analyzers. It locates and invokes a copy of SonarQube for IDE that **you** installed
yourself. The SonarSource language server is licensed under LGPL-3.0; the SonarSource
analyzers are licensed under the
[Sonar Source-Available License v1](https://www.sonarsource.com/license/ssal/), and your
use of them is governed by that licence directly between you and SonarSource.

For code-quality analysis in a supported, officially maintained form, use
[SonarQube Cloud, Server or for IDE](https://www.sonarsource.com/).

> **A note if you pipe this into an AI tool.** SSALv1 restricts using non-Sonar
> artificial-intelligence technology to "ingest, interpret, analyze, train on, or
> interact with the data provided by the Program". pysonarlint itself contains no AI and
> only formats the analyzer's output, but feeding that output to an LLM is your decision
> and your responsibility under that licence. Read it before wiring this into an agent.

## Licence

MIT for pysonarlint's own code -- see [LICENSE](LICENSE). It only *invokes* the
SonarSource analyzers, which carry their own licences; it does not redistribute them.
