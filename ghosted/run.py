#!/usr/bin/env python3
"""Start Ghosted:  python run.py   (then open the address it prints)."""
import argparse
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main(argv=None):
    if sys.version_info < (3, 8):
        sys.exit("Ghosted needs Python 3.8 or newer.")
    from ghosted import __version__
    from ghosted.server import App, make_server

    parser = argparse.ArgumentParser(description="Ghosted: late replies to ignored GitHub issues.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1, this computer only)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    parser.add_argument("--db", default=str(ROOT / "data" / "ghosted.db"),
                        help="where to keep scans (default ./data/ghosted.db)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    local_only = args.host in ("127.0.0.1", "localhost", "::1")
    app = App(str(db_path), allow_any_host=not local_only, verbose=args.verbose)

    server = None
    last_error = None
    for port in range(args.port, args.port + 10):
        try:
            server = make_server(app, args.host, port)
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        sys.exit("Could not open a port between %d and %d (%s)." % (args.port, args.port + 9, last_error))

    shown_host = "localhost" if local_only else args.host
    url = "http://%s:%d" % (shown_host, server.server_address[1])
    def say(text=""):
        print(text, flush=True)  # flush: piped output (some IDE run windows) would otherwise hide the address

    say("")
    say("  Ghosted %s is awake: %s" % (__version__, url))
    say("  It only ever READS from GitHub. Nothing is posted, anywhere.")
    if not local_only:
        say("  WARNING: listening on %s, so other computers can reach this page." % args.host)
    say("  Press Ctrl+C to stop.")
    say("")
    if not args.no_browser:
        threading.Timer(0.7, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\n  Goodbye.")
    finally:
        server.server_close()
        app.store.close()


if __name__ == "__main__":
    main()
