"""A fake model server for tests.

It speaks Ollama's native API (/api/tags, /api/version, /api/chat) and the
OpenAI-compatible API (/v1/models, /v1/chat/completions) over real sockets, so the
real client code runs end to end. `mode` makes it misbehave the way small models do.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_MODELS = [
    {"name": "gemma3:4b", "size": 3_300_000_000, "details": {"parameter_size": "4.3B", "family": "gemma3"}},
    {"name": "nomic-embed-text:latest", "size": 274_000_000, "details": {"parameter_size": "137M", "family": "nomic-bert"}},
    {"name": "llama3.1:70b", "size": 40_000_000_000, "details": {"parameter_size": "70.6B", "family": "llama"}},
]


def decide(text: str) -> dict:
    """The mock's own simple triage, written independently of the demo brain."""
    low = text.lower()
    if re.search(r"\b(bill|rent|tax|passport)\b", low):
        return {"if_ignored": "Late fee.", "risk": "high", "bucket": "do_now", "why": "Real deadline.", "minutes": 10}
    if re.search(r"someday|learn|maybe|photos", low):
        return {"if_ignored": "Nothing.", "risk": "none", "bucket": "let_expire", "why": "No stakes.", "minutes": 60}
    if re.search(r"waiting|landlord", low):
        return {"if_ignored": "It waits on them.", "risk": "low", "bucket": "not_mine", "why": "Theirs.", "minutes": 5}
    return {"if_ignored": "Nothing breaks this week.", "risk": "low", "bucket": "do_later", "why": "Not urgent.", "minutes": 15}


class MockLLM:
    def __init__(self, models=None, mode="ok", delay=0.0, api_key=""):
        self.models = models if models is not None else [dict(m) for m in DEFAULT_MODELS]
        self.mode = mode
        self.delay = delay
        self.api_key = api_key
        self.ollama_enabled = True
        self.openai_enabled = True
        self.reject_schema_format = False  # Ollama: 400 unless format == "json"
        self.reject_json_schema = False  # OpenAI: 400 for response_format json_schema
        self.reject_json_object = False  # OpenAI: 400 for response_format json_object
        self.reject_think = False  # Ollama: 400 when `think` is sent
        self.fail_first_n = 0  # first N chat calls return HTTP 500
        self.die_after = None  # after N chat calls, every call returns HTTP 500
        self.chat_calls = 0
        self.calls = []  # (path, payload)
        self.answers = []  # every answer text sent
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _reply(self, status, payload):
                body = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass  # the client gave up (timeout tests) - nothing to tell it

            def _auth_ok(self):
                if not mock.api_key:
                    return True
                return self.headers.get("Authorization") == "Bearer %s" % mock.api_key

            def do_GET(self):
                if not self._auth_ok():
                    return self._reply(401, {"error": {"message": "bad key"}})
                if self.path == "/api/tags" and mock.ollama_enabled:
                    return self._reply(200, {"models": mock.models})
                if self.path == "/api/version" and mock.ollama_enabled:
                    return self._reply(200, {"version": "0.12.0"})
                if self.path == "/v1/models" and mock.openai_enabled:
                    return self._reply(200, {"object": "list", "data": [{"id": m["name"], "object": "model"} for m in mock.models]})
                return self._reply(404, {"error": "not found"})

            def do_POST(self):
                if not self._auth_ok():
                    return self._reply(401, {"error": {"message": "bad key"}})
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                with mock._lock:
                    mock.calls.append((self.path, payload))
                if self.path == "/api/chat" and mock.ollama_enabled:
                    return mock._ollama_chat(self, payload)
                if self.path == "/v1/chat/completions" and mock.openai_enabled:
                    return mock._openai_chat(self, payload)
                return self._reply(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def make_answer(self, user_text: str) -> str:
        pairs = re.findall(r"^(\d+)\. (.+)$", user_text, re.M)
        rows = []
        for number, line in pairs:
            row = {"id": int(number)}
            row.update(decide(line))
            rows.append(row)
        mode = self.mode
        if mode == "all_expire":
            for row in rows:
                row.update({"bucket": "let_expire", "risk": "none", "if_ignored": "Nothing.", "why": "Skip it."})
        if mode == "missing_last" and len(rows) > 1:
            rows = rows[:-1]
        if mode == "zero_based":
            for index, row in enumerate(rows):
                row["id"] = index
        if mode == "synonyms":
            for row in rows:
                row["bucket"] = {"do_now": "Do Now", "do_later": "later", "let_expire": "Skip", "not_mine": "Not my problem"}[row["bucket"]]
                row["minutes"] = "%d min" % row["minutes"]
                row["risk"] = row["risk"].upper()
        if mode == "no_ids":
            for row in rows:
                del row["id"]
        if mode == "bad_bucket":
            for row in rows:
                row["bucket"] = "banana"
        text = json.dumps({"tasks": rows})
        if mode == "array":
            text = json.dumps(rows)
        elif mode == "keyed":
            text = json.dumps({str(r["id"]): {k: v for k, v in r.items() if k != "id"} for r in rows})
        elif mode == "fenced":
            text = "Here you go!\n```json\n%s\n```\nHope that helps." % text
        elif mode == "chatty":
            text = "Sure. I looked at each task carefully. %s\nLet me know if you want changes." % text
        elif mode == "trailing_comma":
            text = text.replace("}]}", "},]}").replace('"}', '",}')
        elif mode == "truncated":
            text = text[: int(len(text) * 0.6)]
        elif mode == "garbage":
            text = "I'm sorry, I cannot help with that."
        elif mode == "empty":
            text = ""
        elif mode == "think":
            text = "<think>Let me reason about each task...</think>" + text
        elif mode == "smart_quotes":
            text = text.replace('"', "“", 2)
        return text

    def _maybe_fail(self, handler) -> bool:
        with self._lock:
            self.chat_calls += 1
            number = self.chat_calls
        if self.delay:
            time.sleep(self.delay)
        dead = self.die_after is not None and number > self.die_after
        if number <= self.fail_first_n or self.mode == "error500" or dead:
            handler._reply(500, {"error": "the model crashed"})
            return True
        return False

    def _ollama_chat(self, handler, payload) -> None:
        names = [m["name"] for m in self.models]
        if payload.get("model") not in names:
            return handler._reply(404, {"error": 'model "%s" not found, try pulling it first' % payload.get("model")})
        if self.reject_think and "think" in payload:
            return handler._reply(400, {"error": '"%s" does not support thinking' % payload["model"]})
        if self.reject_schema_format and isinstance(payload.get("format"), dict):
            return handler._reply(400, {"error": "invalid format"})
        if self._maybe_fail(handler):
            return
        user = payload["messages"][-1]["content"]
        text = self.make_answer(user)
        with self._lock:
            self.answers.append(text)
        handler._reply(200, {"model": payload["model"], "message": {"role": "assistant", "content": text}, "done": True})

    def _openai_chat(self, handler, payload) -> None:
        response_format = (payload.get("response_format") or {}).get("type")
        if response_format == "json_schema" and self.reject_json_schema:
            return handler._reply(400, {"error": {"message": "unsupported response_format"}})
        if response_format == "json_object" and self.reject_json_object:
            return handler._reply(400, {"error": {"message": "unsupported response_format"}})
        if self._maybe_fail(handler):
            return
        user = payload["messages"][-1]["content"]
        text = self.make_answer(user)
        with self._lock:
            self.answers.append(text)
        handler._reply(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
