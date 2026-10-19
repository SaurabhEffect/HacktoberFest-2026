"""Run the app:  python -m reelcaptioner  [--demo] [--model small] [--port 8000]"""
from __future__ import annotations

import argparse
import errno
import os
import socket
import sys
import threading
import webbrowser

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def _port_in_use(host: str, port: int) -> bool:
    """True when something is already listening there (checked before uvicorn gives a cryptic error)."""
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if os.name != "nt":                      # on Windows this option would hide a busy port
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError as exc:
        return exc.errno in (errno.EADDRINUSE, 10048)
    return False


def _url(host: str, port: int) -> str:
    """Where to point a browser: a wildcard address is not something a browser can open."""
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="reelcaptioner", description="Hinglish Reel Captioner (local, offline)")
    parser.add_argument("--host", help="interface to bind (default 127.0.0.1, keep it local)")
    parser.add_argument("--port", type=_port, help="port (default 8000)")
    parser.add_argument("--model", help="Whisper model: tiny, base, small, medium, large-v3-turbo, or a folder path")
    parser.add_argument("--data-dir", help="where uploads and results are stored (default ./data)")
    parser.add_argument("--demo", action="store_true", help="demo mode: no model download, plays a sample script")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = parser.parse_args(argv)

    if args.host:
        os.environ["HRC_HOST"] = args.host
    if args.port:
        os.environ["HRC_PORT"] = str(args.port)
    if args.model:
        os.environ["HRC_MODEL"] = args.model
    if args.data_dir:
        os.environ["HRC_DATA_DIR"] = args.data_dir
    if args.demo:
        os.environ["HRC_ASR_BACKEND"] = "mock"
        os.environ.setdefault("HRC_MOCK_DELAY", "1.5")

    import uvicorn

    from .app import create_app
    from .config import ConfigError, get_settings
    from .media import tools_status

    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    tools = tools_status()
    if not tools["ffmpeg"]:
        print("! FFmpeg was not found. Install it (https://ffmpeg.org/download.html) and restart.", file=sys.stderr)
    elif not tools["libass"]:
        print("! This FFmpeg has no subtitle renderer (libass). Exports work, burning captions will not.",
              file=sys.stderr)
    if settings.host not in LOCAL_HOSTS:
        print(f"! Listening on {settings.host}. The app has no password, so anyone who can reach this address can "
              "see and delete every project in it. Keep it on 127.0.0.1 unless you trust the network.",
              file=sys.stderr)

    url = _url(settings.host, settings.port)
    if _port_in_use(settings.host, settings.port):
        other = settings.port + 1 if settings.port < 65535 else settings.port - 1
        print(f"Port {settings.port} is already in use. Is the app already running?\n"
              f"Open {url}, or start another copy with:  python -m reelcaptioner --port {other}", file=sys.stderr)
        return 1
    print(f"\n  Hinglish Reel Captioner is running at {url}")
    print(f"  Speech model: {'demo mode' if settings.asr_backend == 'mock' else settings.model}"
          f" | data folder: {settings.data_dir}\n")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")
    except OSError as exc:
        print(f"Could not start the server on {url}: {exc}\nTry another port with --port 8001.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
