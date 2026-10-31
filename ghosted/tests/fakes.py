"""Fake GitHub and fake model servers for tests. Real HTTP on loopback, no mocking of our code."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class _Server(object):
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _send(handler, status, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass


class FakeGitHub(_Server):
    """Serves the read-only endpoints Ghosted uses, with pagination and failure injection."""

    def __init__(self, repos=None):
        self.repos = dict((k.lower(), v) for k, v in (repos or {}).items())
        self.requests = []      # (path, query, headers)
        self.writes = []        # any non-GET request: must stay empty
        self.overrides = {}     # path suffix -> {"status", "body", "headers", "times"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer._get(self)

            def _write(self):
                outer.writes.append((self.command, self.path))
                _send(self, 405, {"message": "read-only"})

            do_POST = do_PUT = do_PATCH = do_DELETE = _write

        _Server.__init__(self, Handler)

    def _get(self, handler):
        parsed = urlparse(handler.path)
        query = dict((k, v[0]) for k, v in parse_qs(parsed.query).items())
        self.requests.append((parsed.path, query, dict(handler.headers.items())))
        limits = {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4990"}

        for suffix, spec in list(self.overrides.items()):
            if parsed.path.endswith(suffix):
                times = spec.get("times")
                if times is not None:
                    if times <= 0:
                        del self.overrides[suffix]
                        continue
                    spec["times"] = times - 1
                return _send(handler, spec["status"], spec.get("body", {"message": "boom"}),
                             dict(limits, **spec.get("headers", {})))

        match = re.match(r"^/repos/([^/]+)/([^/]+)(?:/(issues|pulls|commits|releases))?$", parsed.path)
        if not match:
            return _send(handler, 404, {"message": "Not Found"}, limits)
        data = self.repos.get(("%s/%s" % (match.group(1), match.group(2))).lower())
        if data is None:
            return _send(handler, 404, {"message": "Not Found"}, limits)
        kind = match.group(3)
        if kind is None:
            return _send(handler, 200, data["repo"], limits)
        if kind == "issues":
            items = sorted(data["issues"], key=lambda i: i["created_at"],
                           reverse=(query.get("direction") == "desc"))
        elif kind == "pulls":
            items = list(data["prs"])
        elif kind == "commits":
            items = list(data["commits"])
        else:
            items = list(data["releases"])
        per_page = int(query.get("per_page", 30))
        page = int(query.get("page", 1))
        items = items[(page - 1) * per_page: page * per_page]
        _send(handler, 200, items, limits)


class HTTPFail(object):
    """Return this from a responder to make the fake model server answer with an HTTP error."""

    def __init__(self, status, body):
        self.status = status
        self.body = body


def good_answer(user_text):
    """What a well-behaved model would answer for the prompt Ghosted sends."""
    found = re.search(r"ISSUE #(\d+)", user_text)
    number = int(found.group(1)) if found else 0
    who = re.search(r"Reporter: @(\S+)", user_text)
    author = who.group(1) if who else "there"
    evidence = re.findall(r"^\d+\. \[(closing|mention|similar)\] (.+?) on (\S+):", user_text, re.M)
    strong = [e for e in evidence if e[0] in ("closing", "mention")]
    if strong:
        _strength, ref, date = strong[0]
        answer = {"verdict": "fixed", "confidence": "high",
                  "reason": "%s says it fixes this." % ref,
                  "reply": "Hi @%s, sorry this sat unanswered for so long. It looks like this was "
                           "addressed in %s (%s). Could you try the latest version and tell us "
                           "whether it works?" % (author, ref, date)}
    elif "(none:" in user_text and ("Steps" in user_text or "```" in user_text):
        answer = {"verdict": "valid", "confidence": "medium",
                  "reason": "Clear report and nothing later addresses it.",
                  "reply": "Hi @%s, thank you for the report and sorry for the silence. This still "
                           "looks like a real problem. If you are still affected, a comment here "
                           "would help." % author}
    else:
        answer = {"verdict": "needs_info", "confidence": "medium",
                  "reason": "Too vague to act on.",
                  "reply": "Hi @%s, sorry for the silence. Could you share your version, your "
                           "OS and the steps to reproduce this?" % author}
    answer["_issue"] = number
    return answer


def _default_responder(payload, call_index):
    user = [m for m in payload["messages"] if m["role"] == "user"][-1]["content"]
    answer = good_answer(user)
    answer.pop("_issue")
    return json.dumps(answer)


class FakeLLM(_Server):
    """Speaks both Ollama's /api/* and the OpenAI-style /v1/* dialects."""

    def __init__(self, models=("gemma3:4b",), responder=None, reject_schema=False,
                 reject_response_format=False, delay=0.0):
        self.models = list(models)
        self.responder = responder or _default_responder
        self.reject_schema = reject_schema
        self.reject_response_format = reject_response_format
        self.delay = delay
        self.chat_payloads = []
        self.calls = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/api/tags":
                    _send(self, 200, {"models": [{"name": n} for n in outer.models]})
                elif self.path == "/v1/models":
                    _send(self, 200, {"data": [{"id": n} for n in outer.models]})
                else:
                    _send(self, 404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                outer._chat(self, payload)

        _Server.__init__(self, Handler)

    def _chat(self, handler, payload):
        with self._lock:
            self.chat_payloads.append(payload)
            index = self.calls
            self.calls += 1
        ollama = handler.path == "/api/chat"
        if handler.path not in ("/api/chat", "/v1/chat/completions"):
            return _send(handler, 404, {"error": "not found"})
        if ollama and self.reject_schema and isinstance(payload.get("format"), dict):
            return _send(handler, 400, {"error": "invalid format: expected \"json\" or a JSON schema"})
        if not ollama and self.reject_response_format and "response_format" in payload:
            return _send(handler, 400, {"error": {"message": "response_format.type is not supported"}})
        wanted = payload.get("model")
        if wanted not in self.models and wanted + ":latest" not in self.models:
            return _send(handler, 404, {"error": "model '%s' not found" % wanted})
        if self.delay:
            time.sleep(self.delay)
        content = self.responder(payload, index)
        if isinstance(content, HTTPFail):
            return _send(handler, content.status, content.body)
        if ollama:
            _send(handler, 200, {"model": wanted, "done": True,
                                 "message": {"role": "assistant", "content": content}})
        else:
            _send(handler, 200, {"choices": [{"index": 0, "message": {"role": "assistant",
                                                                       "content": content}}]})
