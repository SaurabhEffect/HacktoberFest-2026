"""The local web server: a small JSON API plus the static front end (standard library only)."""
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .demo import DEMO_REPO
from .errors import LLMError
from .export import CONTENT_TYPES, EXPORTERS, export_filename
from .llm import DEFAULT_OLLAMA_URL, LLMClient, normalize_base_url
from .pipeline import Busy, ScanRunner, validate_scan_request
from .store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 64 * 1024
REVIEW_STATES = ("new", "approved", "skipped")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".ico": "image/x-icon", ".woff2": "font/woff2", ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8",
}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


class Raw(object):
    """A non-JSON API response (file downloads)."""

    def __init__(self, body, content_type, filename=None):
        self.body = body
        self.content_type = content_type
        self.filename = filename


def ollama_url_from_env(env):
    raw = (env.get("OLLAMA_HOST") or "").strip()
    if not raw:
        return DEFAULT_OLLAMA_URL
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname or "localhost"
    if host in ("0.0.0.0", "::"):
        host = "localhost"
    if ":" in host:
        host = "[%s]" % host
    try:
        port = parsed.port or 11434
    except ValueError:
        port = 11434
    return "%s://%s:%d" % (parsed.scheme or "http", host, port)


class App(object):
    def __init__(self, db_path, github_api_base=None, env=None, github_kwargs=None,
                 allow_any_host=False, verbose=False):
        env = os.environ if env is None else env
        self.store = Store(db_path)
        self.store.mark_interrupted()
        self.env_token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or None
        self.runner = ScanRunner(self.store, github_api_base, self.env_token, github_kwargs)
        self.defaults = {"backend": "ollama", "base_url": ollama_url_from_env(env),
                         "model": (env.get("GHOSTED_MODEL") or "").strip()}
        self.allow_any_host = allow_any_host
        self.verbose = verbose


# -- presenting rows -----------------------------------------------------------------------
def public_scan(app, scan, with_counts=False):
    out = {k: scan[k] for k in ("id", "repo", "demo", "status", "stage", "message", "total", "done",
                                "engine", "params", "stats", "repo_info", "error", "created_at",
                                "finished_at")}
    out["active"] = app.runner.is_active(scan["id"])
    if with_counts:
        out["counts"] = app.store.counts(scan["id"])
    return out


def public_result(row):
    return dict(row)


# -- API handlers: each returns (status, payload) or a Raw ---------------------------------------
class Request(object):
    def __init__(self, groups, query, read_json):
        self.groups = groups
        self.query = query
        self._read_json = read_json

    def json(self):
        return self._read_json()

    def arg(self, name, default=""):
        values = self.query.get(name)
        return values[0] if values else default


def _scan_or_404(app, req):
    scan = app.store.get_scan(int(req.groups[0]))
    if scan is None:
        raise ApiError(404, "That scan doesn't exist (it may have been deleted).")
    return scan


def api_health(app, req):
    return 200, {"ok": True, "version": __version__}


def api_config(app, req):
    return 200, {"version": __version__, "defaults": dict(app.defaults),
                 "has_env_token": bool(app.env_token), "demo_repo": DEMO_REPO}


def api_models(app, req):
    backend = req.arg("backend", "ollama")
    if backend not in ("ollama", "openai"):
        raise ApiError(400, "Pick Ollama or an OpenAI-compatible server.")
    typed = req.arg("base_url")
    if not typed and backend == "ollama":
        typed = app.defaults["base_url"]
    base = normalize_base_url(backend, typed)
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError(400, "The model server address must look like http://localhost:11434.")
    try:
        models = LLMClient(backend, base, "").list_models()
    except LLMError as exc:
        return 200, {"ok": False, "models": [], "error": str(exc), "base_url": base}
    return 200, {"ok": True, "models": models, "base_url": base}


def api_list_scans(app, req):
    return 200, {"scans": [public_scan(app, s, True) for s in app.store.list_scans(30)]}


def api_create_scan(app, req):
    try:
        params, token = validate_scan_request(req.json(), app.defaults)
    except ValueError as exc:
        raise ApiError(400, str(exc))
    try:
        scan_id = app.runner.start(params, token)
    except Busy as exc:
        raise ApiError(409, str(exc))
    return 202, {"scan": public_scan(app, app.store.get_scan(scan_id))}


def api_get_scan(app, req):
    scan = _scan_or_404(app, req)
    results = [public_result(r) for r in app.store.get_results(scan["id"])]
    return 200, {"scan": public_scan(app, scan, True), "results": results}


def api_delete_scan(app, req):
    scan = _scan_or_404(app, req)
    if app.runner.is_active(scan["id"]):
        raise ApiError(409, "That scan is still running. Cancel it first.")
    app.store.delete_scan(scan["id"])
    return 200, {"ok": True}


def api_cancel_scan(app, req):
    scan = _scan_or_404(app, req)
    if not app.runner.cancel(scan["id"]):
        raise ApiError(409, "That scan isn't running.")
    return 200, {"ok": True}


def api_retry_scan(app, req):
    scan = _scan_or_404(app, req)
    try:
        app.runner.retry(scan["id"])
    except Busy as exc:
        raise ApiError(409, str(exc))
    except LookupError:
        raise ApiError(404, "That scan doesn't exist.")
    except ValueError as exc:
        raise ApiError(400, str(exc))
    return 202, {"scan": public_scan(app, app.store.get_scan(scan["id"]))}


def api_export_scan(app, req):
    scan = _scan_or_404(app, req)
    fmt = req.arg("format", "md")
    if fmt not in EXPORTERS:
        raise ApiError(400, "Export format must be md, csv or json.")
    only = "approved" if req.arg("only") == "approved" else None
    body = EXPORTERS[fmt](scan, app.store.get_results(scan["id"]), only)
    return Raw(body.encode("utf-8"), CONTENT_TYPES[fmt], export_filename(scan, fmt))


def api_patch_result(app, req):
    row = app.store.get_result(int(req.groups[0]))
    if row is None:
        raise ApiError(404, "That issue isn't in the queue any more.")
    body = req.json()
    if not isinstance(body, dict):
        raise ApiError(400, "Expected a JSON object.")
    changes = {}
    if "review" in body:
        if body["review"] not in REVIEW_STATES:
            raise ApiError(400, "review must be one of: %s." % ", ".join(REVIEW_STATES))
        changes["review"] = body["review"]
    if "reply" in body:
        reply = body["reply"]
        if reply is not None and not isinstance(reply, str):
            raise ApiError(400, "reply must be text (or null to go back to the model's draft).")
        if reply is not None and len(reply) > 4000:
            raise ApiError(400, "That reply is too long (4,000 characters at most).")
        changes["edited_reply"] = reply
    if not changes:
        raise ApiError(400, "Nothing to change. Send review and/or reply.")
    app.store.update_result(row["id"], **changes)
    return 200, {"result": public_result(app.store.get_result(row["id"]))}


def api_regenerate(app, req):
    try:
        row = app.runner.regenerate(int(req.groups[0]))
    except LookupError:
        raise ApiError(404, "That issue isn't in the queue any more.")
    except Busy as exc:
        raise ApiError(409, str(exc))
    except LLMError as exc:
        raise ApiError(502, str(exc))
    return 200, {"result": public_result(row)}


ROUTES = [
    ("GET", r"/api/health", api_health),
    ("GET", r"/api/config", api_config),
    ("GET", r"/api/models", api_models),
    ("GET", r"/api/scans", api_list_scans),
    ("POST", r"/api/scans", api_create_scan),
    ("GET", r"/api/scans/(\d+)", api_get_scan),
    ("DELETE", r"/api/scans/(\d+)", api_delete_scan),
    ("POST", r"/api/scans/(\d+)/cancel", api_cancel_scan),
    ("POST", r"/api/scans/(\d+)/retry", api_retry_scan),
    ("GET", r"/api/scans/(\d+)/export", api_export_scan),
    ("PATCH", r"/api/results/(\d+)", api_patch_result),
    ("POST", r"/api/results/(\d+)/regenerate", api_regenerate),
]
_COMPILED = [(method, re.compile("^" + pattern + "$"), fn) for method, pattern, fn in ROUTES]


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Ghosted/" + __version__

        # -- plumbing -------------------------------------------------------------------
        def log_message(self, fmt, *args):
            if app.verbose:
                sys.stderr.write("[ghosted] %s\n" % (fmt % args))

        def _send(self, status, body, content_type, extra=None, head=False):
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", CSP)
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if not head:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the browser went away; nothing to do

        def _json(self, status, payload, head=False):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", head=head)

        def _host_ok(self):
            if app.allow_any_host:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
            return name in ("localhost", "127.0.0.1", "[::1]")

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            if not origin or app.allow_any_host:
                return True
            return urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").strip().lower()

        def _read_json(self):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise ApiError(415, "Send JSON (Content-Type: application/json).")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "Bad Content-Length.")
            if length < 0 or length > MAX_BODY:
                raise ApiError(413, "That request is too large.")
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "The request wasn't valid JSON.")

        # -- routing --------------------------------------------------------------------
        def _handle(self, method):
            parsed = urlparse(self.path)
            if not self._host_ok():
                return self._send(403, b"Forbidden host.", "text/plain; charset=utf-8",
                                  head=(method == "HEAD"))
            if parsed.path.startswith("/api/"):
                return self._api(method, parsed)
            if method in ("GET", "HEAD"):
                return self._static(parsed.path, head=(method == "HEAD"))
            self._send(405, b"Method not allowed.", "text/plain; charset=utf-8")

        def _api(self, method, parsed):
            head = method == "HEAD"
            if method not in ("GET", "HEAD") and not self._origin_ok():
                return self._json(403, {"error": "Cross-site requests are not allowed."})
            lookup = "GET" if method == "HEAD" else method
            path_matched = False
            try:
                for route_method, pattern, fn in _COMPILED:
                    match = pattern.match(parsed.path)
                    if not match:
                        continue
                    path_matched = True
                    if route_method != lookup:
                        continue
                    if method not in ("GET", "HEAD"):
                        # A JSON content type cannot be sent cross-site without a CORS preflight,
                        # which this server never grants. Enforced for every write, body or not.
                        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                        if ctype != "application/json":
                            raise ApiError(415, "Send JSON (Content-Type: application/json).")
                    req = Request(match.groups(), parse_qs(parsed.query), self._read_json)
                    result = fn(app, req)
                    if isinstance(result, Raw):
                        extra = {}
                        if result.filename:
                            extra["Content-Disposition"] = 'attachment; filename="%s"' % result.filename
                        return self._send(200, result.body, result.content_type, extra,
                                          head=(method == "HEAD"))
                    status, payload = result
                    return self._json(status, payload, head=head)
                if path_matched:
                    return self._json(405, {"error": "That method isn't allowed here."}, head=head)
                return self._json(404, {"error": "No such API route."}, head=head)
            except ApiError as exc:
                return self._json(exc.status, {"error": exc.message}, head=head)
            except Exception:  # noqa: BLE001 - never let a handler bug kill the connection silently
                traceback.print_exc()
                return self._json(500, {"error": "Something broke inside Ghosted. Details are in "
                                                 "the terminal."}, head=head)

        def _static(self, path, head=False):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            root = WEB_DIR.resolve()
            inside = target == root or str(target).startswith(str(root) + os.sep)
            kind = STATIC_TYPES.get(target.suffix.lower())
            if not inside or kind is None or not target.is_file():
                return self._send(404, b"Not found.", "text/plain; charset=utf-8", head=head)
            try:
                body = target.read_bytes()
            except OSError:
                return self._send(404, b"Not found.", "text/plain; charset=utf-8", head=head)
            self._send(200, body, kind, head=head)

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("HEAD")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

        def do_PUT(self):
            self._handle("PUT")

    return Handler


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR means "take this port even while another process is listening on
    # it", so a second server would silently share the port instead of run.py moving on to the
    # next one. Everywhere else it only skips the TIME_WAIT delay after a restart, which we want.
    allow_reuse_address = os.name != "nt"


def make_server(app, host="127.0.0.1", port=8765):
    return _LocalServer((host, port), make_handler(app))
