"""Command line interface.

argparse rather than click or typer: the CLI is small, and a linter invoked in a loop
should not pay import cost for a framework it barely uses.

Exit codes follow linter convention: 0 clean, 1 issues found, 2 tool failure. A tool
failure is never conflated with a clean result.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .analyze import analyze
from .collect import LANGUAGES, collect
from .config import resolve
from .engine import EngineNotFound, discover
from .report import FORMATTERS, filter_issues, redact, render_text

EXIT_CLEAN = 0
EXIT_ISSUES = 1
EXIT_ERROR = 2


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=None,
        help="files or directories to analyze (default: current directory)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=sorted(FORMATTERS),
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, help="write to a file instead of stdout"
    )
    parser.add_argument(
        "--severity",
        choices=("error", "warning", "info", "hint"),
        help="only report issues at or above this severity",
    )
    parser.add_argument(
        "--fail-on",
        choices=("error", "warning", "info", "hint", "never"),
        default="warning",
        help="exit 1 when an issue at or above this severity is found (default: warning)",
    )
    parser.add_argument(
        "--language",
        action="append",
        dest="languages",
        choices=sorted(set(LANGUAGES.values())),
        help="restrict to a language (repeatable; default: python)",
    )
    parser.add_argument("--all-languages", action="store_true", help="analyze every supported language")
    parser.add_argument("--standalone", action="store_true", help="ignore connected mode configuration")
    parser.add_argument("--server-url", help="SonarQube server URL")
    parser.add_argument("--token", help="authentication token (prefer $SONAR_TOKEN)")
    parser.add_argument("--project-key", help="server-side project key")
    parser.add_argument("--organization", help="SonarQube Cloud organization key")
    parser.add_argument("--root", type=Path, help="project root (default: nearest repo root)")
    parser.add_argument("--timeout", type=float, default=600.0, help="analysis timeout in seconds")
    parser.add_argument("--sonarlint-home", type=Path, help="path to a SonarQube for IDE extension")
    parser.add_argument("-v", "--verbose", action="store_true", help="include server logs on stderr")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress notes and summary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pysonarlint",
        description="Report the issues SonarQube for IDE would highlight, from the terminal.",
    )
    parser.add_argument("--version", action="version", version=f"pysonarlint {__version__}")
    sub = parser.add_subparsers(dest="command")

    analyze_cmd = sub.add_parser("analyze", help="analyze files (default command)")
    _add_common(analyze_cmd)

    status_cmd = sub.add_parser("status", help="show engine and configuration status")
    status_cmd.add_argument("paths", nargs="*", type=Path, default=None)
    status_cmd.add_argument("--sonarlint-home", type=Path)
    status_cmd.add_argument("--root", type=Path)
    status_cmd.add_argument("-f", "--format", choices=("text", "json"), default="text")

    login_cmd = sub.add_parser("login", help="grant a token via the browser and store it")
    login_cmd.add_argument("--server-url", help="server URL (default: auto-detected)")
    login_cmd.add_argument("--token", help="store a token you already have, skipping the browser")
    login_cmd.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    login_cmd.add_argument("--timeout", type=float, default=180.0)
    login_cmd.add_argument("--root", type=Path)

    logout_cmd = sub.add_parser("logout", help="remove a stored token")
    logout_cmd.add_argument("--server-url", help="server URL (default: auto-detected)")
    logout_cmd.add_argument("--root", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # Allow `pysonarlint .` and `pysonarlint --format json` without naming the
    # subcommand, while keeping explicit subcommands available.
    known = {"analyze", "status", "login", "logout"}
    if not argv or (argv[0] not in known and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "analyze")

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "login":
            return _cmd_login(args)
        if args.command == "logout":
            return _cmd_logout(args)
        return _cmd_analyze(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR
    except EngineNotFound as exc:
        print(f"pysonarlint: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _targets(args: argparse.Namespace) -> list[Path]:
    return [p.resolve() for p in (args.paths or [Path.cwd()])]


def _cmd_analyze(args: argparse.Namespace) -> int:
    targets = _targets(args)
    missing = [t for t in targets if not t.exists()]
    if missing:
        for path in missing:
            print(f"pysonarlint: no such file or directory: {path}", file=sys.stderr)
        return EXIT_ERROR

    engine = discover(args.sonarlint_home)
    cfg = resolve(
        targets[0],
        root=args.root,
        url=args.server_url,
        token=args.token,
        project_key=args.project_key,
        organization=args.organization,
        standalone=args.standalone,
    )

    languages = None if args.all_languages else set(args.languages or ["python"])
    files, collect_notes = collect(
        targets,
        root=cfg.root,
        exclusions=cfg.exclusions,
        languages=languages,
    )

    result = analyze(files, cfg, engine, timeout=args.timeout, verbose=args.verbose)
    result.notes = [redact(n, cfg.binding.token) for n in (*cfg.notes, *collect_notes, *result.notes)]
    if args.severity:
        result.issues = filter_issues(result.issues, args.severity)

    formatter = FORMATTERS[args.format]
    if args.format == "text":
        text = render_text(result, cfg.root, stream=sys.stdout, show_notes=not args.quiet)
    else:
        text = formatter(result, cfg.root)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    if args.verbose and result.logs:
        # The server echoes its own configuration, which has held the token in some
        # versions. Redact before anything reaches a terminal or a bug report.
        for line in result.logs:
            print(redact(line, cfg.binding.token), file=sys.stderr)

    # A run that could not finish must not look clean.
    if result.incomplete:
        return EXIT_ERROR
    if args.fail_on == "never":
        return EXIT_CLEAN
    return EXIT_ISSUES if filter_issues(result.issues, args.fail_on) else EXIT_CLEAN


def _cmd_status(args: argparse.Namespace) -> int:
    target = (args.paths or [Path.cwd()])[0].resolve()
    cfg = resolve(target, root=args.root)

    engine = None
    engine_error = None
    try:
        engine = discover(args.sonarlint_home)
    except EngineNotFound as exc:
        engine_error = str(exc)

    if args.format == "json":
        import json

        print(
            json.dumps(
                {
                    "engine": (
                        {
                            "version": engine.version,
                            "root": str(engine.root),
                            "java": str(engine.java),
                            "analyzers": [a.stem for a in engine.analyzers],
                        }
                        if engine
                        else None
                    ),
                    "engineError": engine_error,
                    "root": str(cfg.root),
                    "mode": "connected" if cfg.connected else "standalone",
                    "binding": {
                        "url": cfg.binding.url,
                        "projectKey": cfg.binding.project_key,
                        "organization": cfg.binding.organization,
                        "hasToken": bool(cfg.binding.token),
                    },
                    # Renamed from "token" so no consumer mistakes a provenance
                    # label for a credential, and never carries the value itself.
                    "provenance": {
                        (f"{k}Source" if k == "token" else k): v
                        for k, v in cfg.provenance.items()
                    },
                    "notes": cfg.notes,
                },
                indent=2,
            )
        )
        return EXIT_CLEAN if engine else EXIT_ERROR

    if engine:
        print(f"engine     {engine.version}  {engine.root}")
        print(f"java       {engine.java}")
        print(f"analyzers  {len(engine.analyzers)} ({', '.join(a.stem for a in engine.analyzers[:5])}, ...)")
    else:
        print("engine     NOT FOUND")
        print(f"           {engine_error}")
    print(f"root       {cfg.root}")
    print(f"mode       {'connected' if cfg.connected else 'standalone'}")
    if cfg.binding.url:
        print(f"server     {cfg.binding.url}  ({cfg.provenance.get('url', '?')})")
    if cfg.binding.project_key:
        print(f"project    {cfg.binding.project_key}  ({cfg.provenance.get('project_key', '?')})")
    if cfg.binding.token:
        print(f"token      set  ({cfg.provenance.get('token', '?')})")
    for note in cfg.notes:
        print(f"note       {note}")
    return EXIT_CLEAN if engine else EXIT_ERROR


def _cmd_login(args: argparse.Namespace) -> int:
    from .auth import AuthError, Credential, grant_token, save_credential
    from .server import preflight

    cfg = resolve(Path.cwd(), root=args.root, standalone=False)
    url = args.server_url or cfg.binding.url
    if not url:
        print(
            "pysonarlint: no server URL. Pass --server-url, set SONAR_HOST_URL, "
            "or add sonar.host.url to sonar-project.properties.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    url = url.rstrip("/")

    # Fail early on an unreachable or too-old server rather than after a browser trip.
    check = preflight(url, None)
    if check.info is None:
        for problem in check.problems:
            print(f"pysonarlint: {problem}", file=sys.stderr)
        return EXIT_ERROR
    print(f"server {url} is {check.info.status}, version {check.info.version}")

    token = args.token
    if not token:
        try:
            token = grant_token(
                url,
                timeout=args.timeout,
                open_browser=not args.no_browser,
                on_url=lambda u: print(f"opening {u}\nwaiting for the browser..."),
            )
        except AuthError as exc:
            print(f"pysonarlint: {exc}", file=sys.stderr)
            return EXIT_ERROR

    verify = preflight(url, token, cfg.binding.project_key)
    if not verify.ok:
        for problem in verify.problems:
            print(f"pysonarlint: {problem}", file=sys.stderr)
        for hint in verify.hints:
            print(f"  hint: {hint}", file=sys.stderr)
        return EXIT_ERROR

    saved, where = save_credential(Credential(url=url, token=token, organization=cfg.binding.organization))
    print("token verified" + (f" and saved to {where}" if saved else f" but NOT saved: {where}"))
    if not saved:
        print("  set SONAR_TOKEN in your environment to keep using it", file=sys.stderr)
    return EXIT_CLEAN


def _cmd_logout(args: argparse.Namespace) -> int:
    from .auth import forget

    cfg = resolve(Path.cwd(), root=args.root, standalone=False)
    url = args.server_url or cfg.binding.url
    if not url:
        print("pysonarlint: no server URL to forget", file=sys.stderr)
        return EXIT_ERROR
    if forget(url.rstrip("/")):
        print(f"removed stored token for {url}")
        return EXIT_CLEAN
    print(f"no stored token for {url}")
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
