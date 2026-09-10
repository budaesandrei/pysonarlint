# Contributing

Thanks for looking. Issues and pull requests are welcome.

## Getting set up

```bash
git clone https://github.com/budaesandrei/pysonarlint
cd pysonarlint
uv sync --group dev
uv run pytest -q
```

The test suite deliberately needs **no** Java and no SonarSource install: everything that
touches the analyzer is exercised through parsing and decision logic. That keeps CI fast
and portable. It also means the tests cannot catch protocol regressions on their own, so
if you change anything in `lsp.py`, please also run the tool for real against a project
and say so in the PR.

## What good looks like here

- **Prove behaviour, do not assert it.** This tool's failure mode is silence: a bug makes
  it report zero issues and exit successfully. Three separate bugs during initial
  development did exactly that. If you fix something in the analysis path, include a test
  that fails before your change.
- **Never let a broken run look clean.** A run that cannot complete must not exit `0`.
- **No new runtime dependencies** without a strong reason. The package is dependency-free
  on purpose: it is invoked in loops and should not pay import cost.
- **Do not bundle, vendor, mirror or auto-download any SonarSource jar, JRE or binary.**
  This is a hard line, not a preference. It is what keeps the project legally clean. The
  tool may only execute software the user installed themselves.
- Comments should explain *why*, especially where behaviour is surprising. Several places
  look odd until you know the protocol detail behind them; those have comments, and new
  ones should too.

## Before opening a PR

```bash
uv run pytest -q
uv run --with ruff ruff check src tests
uv run --with ruff ruff format src tests
```

If you have the tool installed, run it on itself: `pysonarlint src`. Findings that are not
worth fixing should be justified in the PR rather than suppressed silently.

## Reporting a bug

Include the output of `pysonarlint status`, the command you ran, and what you expected.
`--verbose` adds the language-server log, which is usually where the answer is. Secrets are
redacted from that output, but glance over it before pasting.

Security issues: see [SECURITY.md](SECURITY.md) — please do not open a public issue.
