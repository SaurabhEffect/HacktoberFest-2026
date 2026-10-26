"""Command line: start the web app, sort a file in the terminal, or check your model setup."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from .doctor import run_doctor
from .exporters import FORMATS, export
from .judge import SortOptions, sort_tasks
from .llm import LLMError, detect_backend, make_client
from .parser import ParseError
from .server import DEFAULT_PORT, serve
from .store import Store, StoreError
from .util import today_local

DEFAULT_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _settings_with_flags(args: argparse.Namespace) -> dict:
    settings = Store(args.data_dir).get_settings()
    if getattr(args, "base_url", None):
        settings["base_url"] = args.base_url
    if getattr(args, "model", None):
        settings["model"] = args.model
    return settings


def cmd_serve(args: argparse.Namespace) -> int:
    return serve(args.host, args.port, args.data_dir, open_browser=args.open)


def _read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8-sig") as handle:  # utf-8-sig: Windows Notepad adds a BOM
        return handle.read()


def cmd_sort(args: argparse.Namespace) -> int:
    try:
        text = _read_input(args.file)
    except UnicodeDecodeError:
        print("Error: %s is not a UTF-8 text file." % args.file, file=sys.stderr)
        return 1
    store = Store(args.data_dir)
    settings = _settings_with_flags(args)
    client = None
    if not args.demo:
        backend = detect_backend(settings)
        if not backend.reachable or backend.model_missing:
            print("No usable model: %s" % (backend.error or "model not installed"), file=sys.stderr)
            if backend.hint:
                print(backend.hint, file=sys.stderr)
            print("Use --demo to sort with keyword rules instead.", file=sys.stderr)
            return 2
        client = make_client(settings, backend)
    opts = SortOptions(
        budget=args.budget if args.budget is not None else settings["budget_minutes"],
        engine="demo" if args.demo else "auto",
        chunk_size=settings["chunk_size"],
        date_order=settings["date_order"],
        never_skip=settings["never_skip"],
        lessons=store.lessons() if client is not None and settings["lessons"] else [],
        checkin_days=settings["checkin_days"],
        allow_fallback=settings["demo_fallback"],
    )

    def progress(done: int, total: int, message: str) -> None:
        print("  %s (%d/%d)" % (message, done, total), file=sys.stderr)

    try:
        result = sort_tasks(text, client, opts, progress=progress if client is not None else None)
    except (ParseError, LLMError) as err:
        print("Error: %s" % err.message, file=sys.stderr)
        return 1
    if args.save:
        store.save_result(result)
    body, _type, _ext = export(result, args.format)
    sys.stdout.write(body.decode("utf-8"))
    for warning in result["warnings"]:
        print("Note: %s" % warning, file=sys.stderr)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings_with_flags(args)
    outcome = run_doctor(settings, today=today_local())
    marks = {"ok": "✓", "warn": "!", "fail": "✗"}
    for step in outcome["steps"]:
        print("%s %s: %s" % (marks[step["level"]], step["name"], step["detail"]))
        if step["hint"]:
            print("    %s" % step["hint"])
    if outcome["raw"]:
        print("\nRaw reply from the model:\n%s" % outcome["raw"])
    print("\nAll good." if outcome["ok"] else "\nSomething needs fixing before the model can be used.")
    return 0 if outcome["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wontdo",
        description="The Won't-Do List: sort what you meant to do into what to do, what can wait, what can expire "
        "and what was never yours, with a model running on your own machine.",
    )
    parser.add_argument("--version", action="version", version="wontdo " + __version__)
    sub = parser.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve", help="start the web app (the default)")
    serve_p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1, this machine only)")
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_p.add_argument("--open", action="store_true", help="open your browser")

    sort_p = sub.add_parser("sort", help="sort a task file in the terminal")
    sort_p.add_argument("file", nargs="?", default="-", help="text file with one task per line, or - for stdin")
    sort_p.add_argument("--budget", type=int, help="minutes of energy for today (0 = no limit)")
    sort_p.add_argument("--demo", action="store_true", help="use keyword rules instead of a model")
    sort_p.add_argument("--format", choices=sorted(FORMATS), default="md")
    sort_p.add_argument("--save", action="store_true", help="also keep the result in your history")
    sort_p.add_argument("--model")
    sort_p.add_argument("--base-url", dest="base_url")

    doctor_p = sub.add_parser("doctor", help="check that your model server and model work")
    doctor_p.add_argument("--model")
    doctor_p.add_argument("--base-url", dest="base_url")

    for sub_parser in (serve_p, sort_p, doctor_p):
        sub_parser.add_argument("--data-dir", default=DEFAULT_DATA, help="where lists are stored (default: ./data)")
    return parser


def _use_utf8_streams() -> None:
    """Pipes on Windows default to the legacy code page; lists contain ’ — ✓ and friends."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "serve")
    args = build_parser().parse_args(argv)
    if args.cmd != "serve":
        _use_utf8_streams()
    handlers = {"serve": cmd_serve, "sort": cmd_sort, "doctor": cmd_doctor}
    try:
        return handlers[args.cmd](args)
    except StoreError as err:
        print("Error: %s" % err.message, file=sys.stderr)
        return 1
    except FileNotFoundError as err:
        print("Error: file not found: %s" % err.filename, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
