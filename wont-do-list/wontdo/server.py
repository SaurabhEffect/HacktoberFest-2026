"""The local web server: a small JSON API plus the static front end. Standard library only.

It binds to 127.0.0.1 by default and refuses requests that do not look like they
came from the page it serves (Host and Origin checks, JSON-only writes), so a
random website you visit cannot talk to it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .doctor import run_doctor
from .exporters import export
from .judge import Cancelled, SortOptions, sort_tasks
from .llm import Backend, LLMError, detect_backend, make_client
from .parser import ParseError, parse_tasks
from .result import ResultError
from .store import Store, StoreError, public_settings
from .util import binds_to_this_machine_only

MAX_BODY = 256 * 1024
DEFAULT_PORT = 8742

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}

ERROR_STATUS = {
    "empty": 400, "too_long": 413, "too_many": 400, "bad_setting": 400, "bad_json": 400, "bad_request": 400,
    "bad_id": 400, "bad_bucket": 400, "bad_outcome": 400, "not_skipped": 400, "not_found": 404,
    "busy": 409, "no_model": 503, "unreachable": 503, "model_missing": 503, "remote_blocked": 403,
    "timeout": 504, "format": 502, "http": 502,
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint


class Response:
    def __init__(self, body: bytes, content_type: str, status: int = 200, headers: Optional[Dict[str, str]] = None):
        self.body = body
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)


class Job:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.status = "running"
        self.done = 0
        self.total = 0
        self.message = "Starting"
        self.started = time.time()
        self.finished: Optional[float] = None
        self.result: Optional[dict] = None
        self.error: Optional[dict] = None
        self.cancel = threading.Event()

    def snapshot(self) -> dict:
        end = self.finished or time.time()
        return {
            "id": self.id,
            "status": self.status,
            "progress": {"done": self.done, "total": self.total, "message": self.message},
            "elapsed": round(end - self.started, 1),
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    """Runs one sort at a time in a background thread; the page polls for progress."""

    def __init__(self, keep: int = 20) -> None:
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()
        self._keep = keep

    def start(self, work: Callable[[Job], None]) -> Job:
        with self._lock:
            if any(job.status == "running" for job in self._jobs.values()):
                raise ApiError(409, "busy", "A list is already being sorted.", "Wait for it to finish or cancel it.")
            job = Job()
            self._jobs[job.id] = job
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)
        threading.Thread(target=self._run, args=(job, work), daemon=True).start()
        return job

    @staticmethod
    def _run(job: Job, work: Callable[[Job], None]) -> None:
        try:
            work(job)
            job.status = "done"
        except Cancelled:
            job.status = "cancelled"
            job.message = "Cancelled"
        except (ParseError, LLMError, StoreError, ResultError) as err:
            job.status = "error"
            code = getattr(err, "code", None) or getattr(err, "kind", "error")
            job.error = {"code": code, "message": err.message, "hint": getattr(err, "hint", "")}
        except Exception:  # a job thread must never die silently
            traceback.print_exc(file=sys.stderr)
            job.status = "error"
            job.error = {"code": "internal", "message": "Something went wrong while sorting.", "hint": "Details are in the terminal."}
        finally:
            job.finished = time.time()

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ApiError(404, "not_found", "No such job.")
        return job


class Request:
    def __init__(self, method: str, path: str, query: Dict[str, List[str]], body: dict, groups: Tuple[str, ...]):
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.groups = groups


class App:
    """Everything the handlers share. Built once per server."""

    def __init__(self, data_dir: str, web_dir: str, today_fn: Optional[Callable[[], dt.date]] = None):
        self.store = Store(data_dir)
        self.web_dir = os.path.realpath(web_dir)
        self.jobs = JobManager()
        self.today = today_fn or dt.date.today
        self._backend_lock = threading.Lock()
        self._backend_cache: Tuple[float, Any, Optional[Backend]] = (0.0, None, None)
        self.routes = [
            ("GET", r"/api/status", self.api_status),
            ("GET", r"/api/settings", self.api_get_settings),
            ("PUT", r"/api/settings", self.api_put_settings),
            ("POST", r"/api/parse", self.api_parse),
            ("POST", r"/api/sort", self.api_sort),
            ("GET", r"/api/jobs/([0-9a-f]{12})", self.api_job),
            ("POST", r"/api/jobs/([0-9a-f]{12})/cancel", self.api_cancel),
            ("POST", r"/api/test-model", self.api_test_model),
            ("GET", r"/api/history", self.api_history),
            ("GET", r"/api/history/([^/]+)", self.api_history_get),
            ("DELETE", r"/api/history/([^/]+)", self.api_history_delete),
            ("PATCH", r"/api/history/([^/]+)/tasks/([^/]+)", self.api_task_patch),
            ("POST", r"/api/history/([^/]+)/budget", self.api_budget),
            ("GET", r"/api/checkins", self.api_checkins),
            ("GET", r"/api/export/([^/]+)", self.api_export),
            ("POST", r"/api/forget", self.api_forget),
        ]
        self._compiled = [(m, re.compile("^" + p + "$"), fn) for m, p, fn in self.routes]

    def backend(self, settings: Optional[dict] = None, refresh: bool = False) -> Backend:
        settings = settings or self.store.get_settings()
        key = (settings["provider"], settings["base_url"], settings["model"], settings["allow_remote"], settings["api_key"])
        with self._backend_lock:
            stamp, cached_key, cached = self._backend_cache
            if not refresh and cached is not None and cached_key == key and time.time() - stamp < 5:
                return cached
        found = detect_backend(settings)
        with self._backend_lock:
            self._backend_cache = (time.time(), key, found)
        return found

    def forget_backend(self) -> None:
        with self._backend_lock:
            self._backend_cache = (0.0, None, None)

    def api_status(self, req: Request) -> Response:
        settings = self.store.get_settings()
        backend = self.backend(settings, refresh=req.query.get("refresh") == ["1"])
        usable = backend.reachable and bool(backend.model) and not backend.model_missing
        return json_response(
            {
                "version": __version__,
                "mode": "model" if usable else "demo",
                "backend": backend.to_dict(),
                "today": self.today().isoformat(),
                "data_dir": self.store.dir,
                "budget_minutes": settings["budget_minutes"],
            }
        )

    def api_get_settings(self, req: Request) -> Response:
        return json_response(public_settings(self.store.get_settings()))

    def api_put_settings(self, req: Request) -> Response:
        updated = self.store.update_settings(req.body)
        self.forget_backend()
        return json_response(public_settings(updated))

    def api_parse(self, req: Request) -> Response:
        text = req.body.get("text", "")
        if not isinstance(text, str):
            raise ApiError(400, "bad_request", "`text` must be a string.")
        if not text.strip():
            return json_response({"count": 0, "done_skipped": 0, "duplicates_merged": 0, "truncated": 0, "hint": ""})
        try:
            parsed = parse_tasks(text)
        except ParseError as err:
            if err.code == "empty":
                return json_response({"count": 0, "done_skipped": 0, "duplicates_merged": 0, "truncated": 0, "hint": ""})
            return json_response({"count": 0, "error": {"code": err.code, "message": err.message}})
        summary = parsed.summary()
        summary["hint"] = "one_line" if summary["count"] == 1 and text.count(",") >= 2 else ""
        return json_response(summary)

    def api_sort(self, req: Request) -> Response:
        text = req.body.get("text", "")
        if not isinstance(text, str):
            raise ApiError(400, "bad_request", "`text` must be a string.")
        engine = req.body.get("engine", "auto")
        if engine not in ("auto", "demo"):
            raise ApiError(400, "bad_request", "`engine` must be auto or demo.")
        settings = self.store.get_settings()
        budget = req.body.get("budget", settings["budget_minutes"])
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= 960:
            raise ApiError(400, "bad_request", "`budget` must be a whole number of minutes from 0 to 960.")
        parse_tasks(text)  # fail fast, with a friendly message, before any model work

        client = None
        if engine == "auto":
            backend = self.backend(settings, refresh=True)
            if not backend.reachable:
                raise ApiError(503, "no_model", backend.error, backend.hint)
            if backend.model_missing:
                raise ApiError(
                    503, "model_missing", backend.error or "The chosen model is not installed.",
                    backend.hint or "Run: ollama pull %s" % backend.model,
                )
            client = make_client(settings, backend)

        today = self.today()
        lessons = self.store.lessons() if settings["lessons"] and client is not None else []
        opts = SortOptions(
            budget=budget, today=today, engine=engine, chunk_size=settings["chunk_size"],
            date_order=settings["date_order"], never_skip=settings["never_skip"], lessons=lessons,
            checkin_days=settings["checkin_days"], allow_fallback=settings["demo_fallback"],
        )

        def work(job: Job) -> None:
            def progress(done: int, total: int, message: str) -> None:
                job.done, job.total, job.message = done, total, message

            result = sort_tasks(text, client, opts, progress=progress, cancel=job.cancel)
            if settings["autosave"]:
                self.store.save_result(result)
            job.result = result

        job = self.jobs.start(work)
        return json_response({"job": job.id}, status=202)

    def api_job(self, req: Request) -> Response:
        return json_response(self.jobs.get(req.groups[0]).snapshot())

    def api_cancel(self, req: Request) -> Response:
        job = self.jobs.get(req.groups[0])
        job.cancel.set()
        return json_response(job.snapshot())

    def api_test_model(self, req: Request) -> Response:
        settings = self.store.get_settings()
        outcome = run_doctor(settings, today=self.today())
        self.forget_backend()
        return json_response(outcome)

    def api_history(self, req: Request) -> Response:
        return json_response({"items": self.store.list_history()})

    def api_history_get(self, req: Request) -> Response:
        return json_response(self.store.get_result(unquote(req.groups[0])))

    def api_history_delete(self, req: Request) -> Response:
        self.store.delete_result(unquote(req.groups[0]))
        return json_response({"ok": True})

    def api_task_patch(self, req: Request) -> Response:
        sid, tid = unquote(req.groups[0]), unquote(req.groups[1])
        if "bucket" not in req.body and "outcome" not in req.body:
            raise ApiError(400, "bad_request", "Send `bucket` and/or `outcome`.")
        settings = self.store.get_settings()
        result = None
        if "bucket" in req.body:
            result = self.store.move(sid, tid, req.body["bucket"], self.today(), settings["checkin_days"])
        if "outcome" in req.body:
            result = self.store.outcome(sid, tid, req.body["outcome"])
        return json_response(result)

    def api_budget(self, req: Request) -> Response:
        budget = req.body.get("budget")
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= 960:
            raise ApiError(400, "bad_request", "`budget` must be a whole number of minutes from 0 to 960.")
        settings = self.store.get_settings()
        result = self.store.set_budget(unquote(req.groups[0]), budget, self.today(), settings["checkin_days"])
        return json_response(result)

    def api_checkins(self, req: Request) -> Response:
        return json_response(self.store.checkins(self.today()))

    def api_export(self, req: Request) -> Response:
        sid = unquote(req.groups[0])
        fmt = (req.query.get("fmt") or ["md"])[0]
        result = self.store.get_result(sid)
        try:
            body, content_type, extension = export(result, fmt)
        except ValueError as err:
            raise ApiError(400, "bad_request", str(err)) from None
        headers = {}
        if (req.query.get("download") or ["0"])[0] == "1":
            headers["Content-Disposition"] = 'attachment; filename="wontdo-%s.%s"' % (sid, extension)
        return Response(body, content_type, headers=headers)

    def api_forget(self, req: Request) -> Response:
        if req.body.get("confirm") != "forget":
            raise ApiError(400, "bad_request", "Send {\"confirm\": \"forget\"} to delete your history.")
        return json_response({"removed": self.store.forget_history()})

    def static(self, path: str) -> Response:
        if path in ("", "/"):
            path = "/index.html"
        cleaned = unquote(path)
        if "\x00" in cleaned or "\\" in cleaned or ":" in cleaned or any(part == ".." for part in cleaned.split("/")):
            raise ApiError(404, "not_found", "Not found.")
        full = os.path.realpath(os.path.join(self.web_dir, cleaned.lstrip("/")))
        try:
            inside = os.path.commonpath([full, self.web_dir]) == self.web_dir
        except ValueError:  # e.g. a different drive on Windows
            inside = False
        if not inside or not os.path.isfile(full):
            raise ApiError(404, "not_found", "Not found.")
        ext = os.path.splitext(full)[1].lower()
        content_type = MIME.get(ext)
        if content_type is None:
            raise ApiError(404, "not_found", "Not found.")
        with open(full, "rb") as handle:
            body = handle.read()
        cache = "public, max-age=604800, immutable" if ext == ".woff2" else "no-cache"
        return Response(body, content_type, headers={"Cache-Control": cache})


class Handler(BaseHTTPRequestHandler):
    server_version = "wontdo/" + __version__
    sys_version = ""
    timeout = 30  # seconds an idle or stalled connection may hold a thread

    # Silence the default per-request log line; errors still go to stderr.
    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def _host_ok(self) -> bool:
        if getattr(self.server, "allow_any_host", False):
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.server.allowed_hosts  # type: ignore[attr-defined]

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        return urlparse(origin).netloc.lower() == host

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if response.content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def _send_error(self, status: int, code: str, message: str, hint: str = "") -> None:
        self._send(json_response({"error": {"code": code, "message": message, "hint": hint}}, status))

    def _read_body(self) -> dict:
        length_header = self.headers.get("Content-Length")
        if not length_header or length_header == "0":
            return {}
        try:
            length = int(length_header)
        except ValueError:
            raise ApiError(400, "bad_request", "Bad Content-Length.") from None
        if length < 0 or length > MAX_BODY:
            raise ApiError(413, "too_long", "That request is too large.")
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            raise ApiError(415, "bad_request", "Send JSON with Content-Type: application/json.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "The request body is not valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "The request body must be a JSON object.")
        return data

    def _dispatch(self) -> None:
        method = self.command
        try:
            if not self._host_ok():
                raise ApiError(403, "bad_host", "This address is not allowed.")
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "bad_request", "Method not allowed.")
                self._send(self.app.static(path))
                return
            if method != "GET" and not self._origin_ok():
                raise ApiError(403, "bad_origin", "Cross-site requests are not allowed.")
            body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
            matched_path = False
            for route_method, pattern, handler in self.app._compiled:
                found = pattern.match(path)
                if not found:
                    continue
                matched_path = True
                if route_method != method:
                    continue
                request = Request(method, path, parse_qs(parsed.query), body, found.groups())
                self._send(handler(request))
                return
            if matched_path:
                raise ApiError(405, "bad_request", "Method not allowed.")
            raise ApiError(404, "not_found", "No such endpoint.")
        except ApiError as err:
            self._send_error(err.status, err.code, err.message, err.hint)
        except (StoreError, ParseError, ResultError) as err:
            self._send_error(ERROR_STATUS.get(err.code, 400), err.code, err.message)
        except LLMError as err:
            self._send_error(ERROR_STATUS.get(err.kind, 502), err.kind, err.message, err.hint)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError):
            return
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                self._send_error(500, "internal", "Something went wrong.", "Details are in the terminal.")
            except OSError:
                pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _dispatch


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows this flag would let two copies share one port
    app: App
    allowed_hosts: set
    allow_any_host: bool


def default_web_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def make_server(
    host: str,
    port: int,
    data_dir: str,
    web_dir: Optional[str] = None,
    today_fn: Optional[Callable[[], dt.date]] = None,
) -> Server:
    """Create (but do not start) the server. Port 0 picks a free port."""
    server = Server((host, port), Handler)
    bound_port = server.server_address[1]
    server.app = App(data_dir, web_dir or default_web_dir(), today_fn)
    server.allow_any_host = not binds_to_this_machine_only(host)
    server.allowed_hosts = {
        "%s:%d" % (name, bound_port) for name in ("127.0.0.1", "localhost", "[::1]")
    } | {host.lower() + ":%d" % bound_port}
    return server


def serve(host: str, port: int, data_dir: str, open_browser: bool = False) -> int:
    server = None
    for candidate in range(port, port + 20):
        try:
            server = make_server(host, candidate, data_dir)
            break
        except OSError:
            continue
    if server is None:
        print("Could not open a port between %d and %d." % (port, port + 19), file=sys.stderr)
        return 1
    local_only = binds_to_this_machine_only(host)
    shown = "localhost" if local_only else host
    url = "http://%s:%d/" % (shown, server.server_address[1])
    print("The Won't-Do List is running at %s" % url)
    print("Your lists are stored in %s" % server.app.store.dir)
    if not local_only:
        print(
            "Warning: %s is reachable from other machines on your network, and the app has no password." % host,
            file=sys.stderr,
        )
    print("Press Ctrl+C to stop.")
    if open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0
