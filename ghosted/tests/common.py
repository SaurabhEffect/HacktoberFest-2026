"""Shared test setup: make the project importable and provide a running Ghosted instance."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from ghosted import net  # noqa: E402
from ghosted.demo import DEMO_REPO, fixture  # noqa: E402
from ghosted.server import App, make_server  # noqa: E402

from fakes import FakeGitHub, FakeLLM  # noqa: E402


class Instance(object):
    """A real Ghosted server, backed by a fake GitHub and a fake model server."""

    def __init__(self, llm_kwargs=None, repos=None, env=None):
        self.tmp = tempfile.mkdtemp(prefix="ghosted-test-")
        self.github = FakeGitHub(repos if repos is not None else {DEMO_REPO: fixture()})
        self.llm = FakeLLM(**(llm_kwargs or {}))
        self.app = App(os.path.join(self.tmp, "t.db"), github_api_base=self.github.url,
                       env=env if env is not None else {}, github_kwargs={"backoff": 0.0, "retries": 1})
        self.httpd = make_server(self.app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.github.stop()
        self.llm.stop()
        self.app.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- HTTP helpers -----------------------------------------------------------------
    def call(self, method, path, body=None, headers=None, raw=False):
        """Returns (status, parsed JSON or text)."""
        send = dict(headers or {})
        payload = body
        if method not in ("GET", "HEAD"):
            send.setdefault("Content-Type", "application/json")
            payload = body if body is not None else {}
        try:
            status, hdrs, data = net.request(method, self.base + path, headers=send, body=payload,
                                             timeout=60)
        except net.HttpError as exc:
            status, hdrs, data = exc.status, exc.headers, exc.body.encode("utf-8")
        if raw:
            return status, hdrs, data
        text = data.decode("utf-8")
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def scan_request(self, **overrides):
        body = {"repo": DEMO_REPO, "backend": "ollama", "base_url": self.llm.url,
                "model": "gemma3:4b", "min_age_days": 30, "max_issues": 50}
        body.update(overrides)
        return body

    def start_scan(self, **overrides):
        status, data = self.call("POST", "/api/scans", self.scan_request(**overrides))
        return status, data

    def wait_scan(self, scan_id, timeout=60, until=("done", "error", "cancelled")):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data = self.call("GET", "/api/scans/%d" % scan_id)
            if status == 200 and data["scan"]["status"] in until and not data["scan"]["active"]:
                return data
            time.sleep(0.05)
        raise AssertionError("scan %s did not finish in %ss: %s" % (scan_id, timeout, data["scan"]))

    def run_scan(self, **overrides):
        status, data = self.start_scan(**overrides)
        assert status == 202, (status, data)
        return self.wait_scan(data["scan"]["id"])
