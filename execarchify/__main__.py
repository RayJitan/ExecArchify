"""Command line entry point."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .index import discover, configure, synchronize


def output(value):
    data = (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode(
        "utf-8"
    )
    sys.stdout.buffer.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="execarchify", description="Local, Git-aware code graph for agents."
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser(
        "init",
        help="Index a single Git worktree and optional explicit shared repositories",
    )
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--shared-repo", action="append", default=[], metavar="PATH")
    init.add_argument(
        "--no-watch",
        action="store_true",
        help="Index and exit without starting a background observer",
    )
    init.add_argument("--interval", type=float, default=2, metavar="SECONDS")
    for name, description in (
        ("context", "Show workspace or source evidence"),
        ("resolve", "Find code from a name or phrase"),
        ("trace", "Explore the source-anchored neighborhood"),
        ("impact", "Inspect working changes or two commits"),
        ("mcp", "Run the stdio MCP server"),
        ("serve", "Open the local HTTP explorer"),
        ("status", "Inspect index and observer status"),
        ("sync", "Refresh scoped facts now"),
        ("watch", "Run the observer in the foreground"),
        ("stop", "Stop the workspace observer"),
        ("export", "Export the architecture overview for Archify"),
        ("_watch", "Internal observer process"),
    ):
        cmd = sub.add_parser(name, help=description)
        cmd.add_argument("--workspace", "-w", default=".", metavar="PATH")
        if name == "context":
            cmd.add_argument("--id")
            cmd.add_argument(
                "--architecture",
                action="store_true",
                help="Include evidenced cross-group relationships",
            )
        if name == "resolve":
            cmd.add_argument("query", nargs="?", default="")
            cmd.add_argument("--group")
            cmd.add_argument("--repo")
        if name == "trace":
            cmd.add_argument("id")
            cmd.add_argument(
                "--direction", choices=("both", "in", "out"), default="both"
            )
            cmd.add_argument("--depth", type=int, default=1)
        if name == "impact":
            cmd.add_argument("--base")
            cmd.add_argument("--head")
            cmd.add_argument("--depth", type=int, default=2)
        if name in ("resolve", "trace", "impact"):
            cmd.add_argument("--limit", type=int)
        if name in ("watch", "_watch"):
            cmd.add_argument("--interval", type=float, default=2)
        if name in ("serve", "mcp"):
            cmd.add_argument("--no-watch", action="store_true")
        if name == "serve":
            cmd.add_argument("--port", type=int, default=8765)
        if name == "export":
            cmd.add_argument("--format", choices=("archify",), default="archify")
            cmd.add_argument(
                "--output", metavar="FILE", help="Write portable UTF-8 JSON to a file"
            )
    args = parser.parse_args(argv)
    try:
        from . import watch

        if args.command == "init":
            if not 0.15 <= args.interval <= 3600:
                raise ValueError("interval must be between 0.15 and 3600 seconds")
            ws = discover(args.path)
            configure(ws, args.shared_repo)
            result = synchronize(ws)
            observer = (
                watch.status(ws) if args.no_watch else watch.start(ws, args.interval)
            )
            output(
                dict(
                    result,
                    root=str(ws.root),
                    database=str(ws.database),
                    observer=observer,
                )
            )
            return 0
        ws = discover(args.workspace)
        if args.command in ("watch", "_watch"):
            watch.observe(ws, args.interval)
        elif args.command == "stop":
            output(watch.stop(ws))
        elif args.command == "sync":
            output(synchronize(ws))
        elif args.command in ("mcp", "serve"):
            if not args.no_watch:
                watch.start(ws)
            if args.command == "mcp":
                from .mcp import serve_mcp

                serve_mcp(ws)
            else:
                from .web import serve

                serve(ws, port=args.port)
        elif args.command == "export":
            from .export import overview

            ir = overview(ws)
            if args.output:
                Path(args.output).write_text(
                    json.dumps(ir, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            else:
                output(ir)
        else:
            from .api import call

            parameters = {
                k: v
                for k, v in vars(args).items()
                if k not in ("workspace", "command") and v is not None
            }
            if args.command == "status":
                value = call(ws, "context")
                output(
                    {
                        k: value[k]
                        for k in ("root", "generation", "repositories", "stats", "gaps")
                    }
                    | {"database": str(ws.database), "observer": watch.status(ws)}
                )
            else:
                output(call(ws, args.command, parameters))
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print("execarchify:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
