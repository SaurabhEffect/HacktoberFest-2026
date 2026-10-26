import contextlib
import datetime as dt
import http.client
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

from tests.mock_llm import MockLLM
from wontdo import server as server_module
from wontdo.llm import LLMError
from wontdo.parser import ParseError
from wontdo.server import MAX_BODY, ApiError, JobManager, make_server

TODAY = dt.date(2026, 10, 20)

LIST = """Pay electricity bill by tomorrow
Learn Rust someday
Waiting for the landlord to fix the tap
Reply to the webinar invite
Book dentist for a check-up"""

INDEX = "<!doctype html><title>test</title><p>hello</p>"


def body_bytes(body):
    return body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ServerCase(unittest.TestCase):
    point_at_mock = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wontdo-server-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.web = os.path.join(self.tmp, "web")
        os.makedirs(os.path.join(self.web, "fonts"))
        os.makedirs(os.path.join(self.web, "empty-dir"))
        for name, body in (("index.html", INDEX), ("app.js", "console.log(1)"), ("app.css", "body{}"), ("notes.py", "print(1)")):
            with open(os.path.join(self.web, name), "w") as handle:
                handle.write(body)
        with open(os.path.join(self.web, "fonts", "f.woff2"), "wb") as handle:
            handle.write(b"wOF2....")
        with open(os.path.join(self.tmp, "secret.txt"), "w") as handle:
            handle.write("top secret")

        self.mock = MockLLM().start()
        self.addCleanup(self.mock.stop)
        self.server = make_server("127.0.0.1", 0, os.path.join(self.tmp, "data"), web_dir=self.web, today_fn=lambda: TODAY)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self._stop_server)
        if self.point_at_mock:
            self.use_model(self.mock.url)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, headers=None, raw=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            sent = {"Host": host or "127.0.0.1:%d" % self.port}
            payload = raw
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                sent["Content-Type"] = "application/json"
            sent.update(headers or {})
            conn.request(method, path, body=payload, headers=sent)
            response = conn.getresponse()
            data = response.read()
            header_map = {k.lower(): v for k, v in response.getheaders()}
            if "json" in header_map.get("content-type", "") and data:
                data = json.loads(data.decode("utf-8"))
            return response.status, header_map, data
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def ok(self, method, path, body=None, **kw):
        status, _, data = self.call(method, path, body, **kw)
        self.assertEqual(status, 200, data)
        return data

    def use_model(self, url):
        self.ok("PUT", "/api/settings", {"base_url": url})

    def wait_for(self, job_id, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.ok("GET", "/api/jobs/" + job_id)
            if snapshot["status"] != "running":
                return snapshot
            time.sleep(0.02)
        self.fail("the job never finished")

    def start_sort(self, text=LIST, **extra):
        body = dict(extra, text=text)
        status, _, data = self.call("POST", "/api/sort", body)
        return status, data

    def sort(self, text=LIST, **extra):
        status, data = self.start_sort(text, **extra)
        self.assertEqual(status, 202, data)
        return self.wait_for(data["job"])

    def sorted_list(self, text=LIST, **extra):
        snapshot = self.sort(text, **extra)
        self.assertEqual(snapshot["status"], "done", snapshot)
        return snapshot["result"]


class StaticFiles(ServerCase):
    point_at_mock = False

    def test_index_and_security_headers(self):
        status, headers, body = self.get("/")
        self.assertEqual((status, body), (200, INDEX.encode()))
        self.assertTrue(headers["content-type"].startswith("text/html"))
        csp = headers["content-security-policy"]
        for part in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "object-src 'none'", "frame-ancestors 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(self.get("/index.html")[0], 200)

    def test_types_and_caching(self):
        self.assertEqual(self.get("/app.js")[1]["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(self.get("/app.css")[1]["content-type"], "text/css; charset=utf-8")
        _, headers, body = self.get("/fonts/f.woff2")
        self.assertEqual((headers["content-type"], body), ("font/woff2", b"wOF2...."))
        self.assertIn("immutable", headers["cache-control"])

    def test_not_found_cases(self):
        for path in ("/nope.html", "/empty-dir", "/empty-dir/", "/notes.py", "/fonts", "/index.html/extra"):
            with self.subTest(path=path):
                status, _, data = self.get(path)
                self.assertEqual(status, 404)
                self.assertEqual(data["error"]["code"], "not_found")

    def test_path_traversal_is_refused(self):
        attempts = [
            "/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/%2e%2e%2fsecret.txt", "/fonts/../../secret.txt",
            "/fonts/%2e%2e/%2e%2e/secret.txt", "/..%5csecret.txt", "/%5c..%5csecret.txt", "/index.html%00.png", "/C:/Windows/win.ini",
            "//etc/passwd", "/....//secret.txt", "/\\..\\secret.txt",
        ]
        for path in attempts:
            with self.subTest(path=path):
                status, _, data = self.get(path)
                self.assertIn(status, (200, 404), path)
                if status == 200:  # e.g. "//etc/passwd" is just a path under the web folder
                    self.fail("served something for %s: %r" % (path, data[:40]))
                self.assertNotIn(b"top secret", json.dumps(data).encode())

    def test_symlinks_that_leave_the_folder_are_refused(self):
        try:
            os.symlink(os.path.join(self.tmp, "secret.txt"), os.path.join(self.web, "link.txt"))
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.assertEqual(self.get("/link.txt")[0], 404)

    def test_head_has_headers_and_no_body(self):
        status, headers, body = self.call("HEAD", "/")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["content-length"], str(len(INDEX)))

    def test_static_files_cannot_be_posted_to(self):
        self.assertEqual(self.call("POST", "/", {})[0], 405)
        self.assertEqual(self.call("DELETE", "/index.html")[0], 405)


class Protections(ServerCase):
    point_at_mock = False

    def test_unknown_host_is_refused(self):
        for host in ("evil.example:%d" % self.port, "evil.example", "127.0.0.1", "127.0.0.1:1", "localhost.evil.example:%d" % self.port):
            with self.subTest(host=host):
                for path in ("/", "/api/status", "/api/history"):
                    status, _, data = self.get(path, host=host)
                    self.assertEqual(status, 403, (host, path))
                    self.assertEqual(data["error"]["code"], "bad_host")

    def test_known_host_names_work(self):
        for host in ("localhost:%d" % self.port, "127.0.0.1:%d" % self.port, "LOCALHOST:%d" % self.port):
            self.assertEqual(self.get("/api/settings", host=host)[0], 200, host)

    def test_cross_site_writes_are_refused(self):
        for origin in ("http://evil.example", "http://127.0.0.1:%d" % (self.port + 1), "null", "http://localhost:%d" % self.port):
            with self.subTest(origin=origin):
                status, _, data = self.call("PUT", "/api/settings", {"budget_minutes": 5}, headers={"Origin": origin})
                self.assertEqual(status, 403)
                self.assertEqual(data["error"]["code"], "bad_origin")
        self.assertEqual(self.ok("GET", "/api/settings")["budget_minutes"], 90)

    def test_same_origin_writes_work(self):
        origin = "http://127.0.0.1:%d" % self.port
        self.assertEqual(self.call("PUT", "/api/settings", {"budget_minutes": 45}, headers={"Origin": origin})[0], 200)

    def test_writes_must_be_json(self):
        status, _, data = self.call("POST", "/api/parse", raw=b'{"text": "a"}', headers={"Content-Type": "text/plain"})
        self.assertEqual((status, data["error"]["code"]), (415, "bad_request"))
        status, _, data = self.call("POST", "/api/parse", raw=b'{"text": "a"}', headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        status, _, data = self.call("POST", "/api/parse", raw=b'{"text": "a"}')
        self.assertEqual(status, 415)

    def test_bad_bodies(self):
        json_type = {"Content-Type": "application/json"}
        for raw in (b"{oops", b"[1, 2]", b'"text"', b"\xff\xfe", b"null"):
            with self.subTest(raw=raw):
                status, _, data = self.call("POST", "/api/parse", raw=raw, headers=json_type)
                self.assertEqual((status, data["error"]["code"]), (400, "bad_json"))

    def test_an_empty_body_is_an_empty_object(self):
        self.assertEqual(self.ok("POST", "/api/parse", raw=b"")["count"], 0)

    def test_oversized_bodies_are_refused_before_reading(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/parse")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_BODY + 1))
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 413)
        self.assertEqual(json.loads(response.read())["error"]["code"], "too_long")
        conn.close()

    def test_bad_content_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/parse")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "banana")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_api_responses_are_never_cached(self):
        self.assertEqual(self.get("/api/settings")[1]["cache-control"], "no-store")

    def test_unknown_endpoint_and_wrong_method(self):
        status, _, data = self.get("/api/nope")
        self.assertEqual((status, data["error"]["code"]), (404, "not_found"))
        self.assertEqual(self.call("DELETE", "/api/status")[0], 405)
        self.assertEqual(self.call("GET", "/api/sort")[0], 405)
        self.assertEqual(self.get("/api/jobs/not-a-job-id")[0], 404)

    def test_binding_beyond_this_machine_drops_the_host_allowlist(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for host, expect_any in (("127.0.0.1", False), ("localhost", False), ("0.0.0.0", True)):
            server = make_server(host, 0, tmp, web_dir=self.web)
            try:
                self.assertEqual(server.allow_any_host, expect_any, host)
            finally:
                server.server_close()


class StatusAndSettings(ServerCase):
    def test_status_with_a_model(self):
        data = self.ok("GET", "/api/status")
        self.assertEqual(data["mode"], "model")
        self.assertEqual(data["today"], "2026-10-20")
        self.assertEqual(data["backend"]["model"], "gemma3:4b")
        self.assertTrue(data["backend"]["reachable"])
        self.assertTrue(os.path.isdir(data["data_dir"]))
        self.assertEqual(data["budget_minutes"], 90)

    def test_status_without_a_model_is_demo_mode(self):
        self.use_model("http://127.0.0.1:%d" % free_port())
        data = self.ok("GET", "/api/status")
        self.assertEqual(data["mode"], "demo")
        self.assertFalse(data["backend"]["reachable"])
        self.assertTrue(data["backend"]["hint"])

    def test_status_notices_a_model_that_is_not_installed(self):
        self.ok("PUT", "/api/settings", {"model": "ghost:7b"})
        data = self.ok("GET", "/api/status")
        self.assertEqual(data["mode"], "demo")
        self.assertTrue(data["backend"]["model_missing"])

    def test_settings_round_trip_and_the_key_stays_secret(self):
        shown = self.ok("PUT", "/api/settings", {"api_key": "sekret", "budget_minutes": 60, "never_skip": "mom, boss"})
        self.assertNotIn("api_key", shown)
        self.assertTrue(shown["api_key_set"])
        self.assertEqual((shown["budget_minutes"], shown["never_skip"]), (60, ["mom", "boss"]))
        again = self.ok("GET", "/api/settings")
        self.assertNotIn("sekret", json.dumps(again))
        self.assertTrue(again["api_key_set"])
        self.ok("PUT", "/api/settings", {"api_key": None})  # None leaves it alone
        self.assertTrue(self.ok("GET", "/api/settings")["api_key_set"])
        self.ok("PUT", "/api/settings", {"api_key": ""})
        self.assertFalse(self.ok("GET", "/api/settings")["api_key_set"])

    def test_invalid_settings(self):
        for patch in ({"budget_minutes": "lots"}, {"mystery": 1}, {"provider": "cloud"}, {"chunk_size": 99}):
            status, _, data = self.call("PUT", "/api/settings", patch)
            self.assertEqual((status, data["error"]["code"]), (400, "bad_setting"), patch)

    def test_the_settings_file_is_plain_json_in_the_data_folder(self):
        self.ok("PUT", "/api/settings", {"budget_minutes": 45})
        with open(os.path.join(self.tmp, "data", "settings.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["budget_minutes"], 45)


class Parse(ServerCase):
    def test_counts(self):
        data = self.ok("POST", "/api/parse", {"text": "- a task\n- another task\n- [x] done already\n- A TASK"})
        self.assertEqual((data["count"], data["done_skipped"], data["duplicates_merged"]), (2, 1, 1))

    def test_empty_and_whitespace(self):
        for text in ("", "   \n  ", "- \n-"):
            self.assertEqual(self.ok("POST", "/api/parse", {"text": text})["count"], 0, repr(text))

    def test_one_long_line_gets_a_hint(self):
        data = self.ok("POST", "/api/parse", {"text": "buy milk, call mum, pay rent, book dentist"})
        self.assertEqual((data["count"], data["hint"]), (1, "one_line"))

    def test_too_many_tasks_is_reported_not_raised(self):
        data = self.ok("POST", "/api/parse", {"text": "\n".join("task %d" % n for n in range(250))})
        self.assertEqual(data["error"]["code"], "too_many")

    def test_text_must_be_a_string(self):
        status, _, data = self.call("POST", "/api/parse", {"text": 5})
        self.assertEqual((status, data["error"]["code"]), (400, "bad_request"))


class Sorting(ServerCase):
    def test_full_flow(self):
        result = self.sorted_list()
        self.assertEqual(result["engine"]["kind"], "model")
        self.assertEqual(result["input"]["count"], 5)
        by_text = {i["text"]: i for i in result["tasks"]}
        self.assertEqual(by_text["Learn Rust someday"]["bucket"], "expire")
        self.assertEqual(by_text["Pay electricity bill by tomorrow"]["bucket"], "now")

        history = self.ok("GET", "/api/history")["items"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total"], 5)
        sid = history[0]["sid"]
        self.assertEqual(result["sid"], sid)
        self.assertEqual(self.ok("GET", "/api/history/" + sid)["tasks"], result["tasks"])

    def test_progress_is_reported(self):
        status, data = self.start_sort("\n".join("Task %d" % n for n in range(1, 4)))
        snapshot = self.wait_for(data["job"])
        self.assertEqual(snapshot["progress"]["total"], 3)
        self.assertEqual(snapshot["progress"]["done"], 3)
        self.assertIn("Done", snapshot["progress"]["message"])
        self.assertGreaterEqual(snapshot["elapsed"], 0)

    def test_demo_engine_needs_no_model(self):
        self.use_model("http://127.0.0.1:%d" % free_port())
        result = self.sorted_list(engine="demo")
        self.assertEqual(result["engine"]["kind"], "demo")
        self.assertEqual(len(self.ok("GET", "/api/history")["items"]), 1)

    def test_no_model_is_a_clear_503_not_a_silent_demo(self):
        self.use_model("http://127.0.0.1:%d" % free_port())
        status, data = self.start_sort()
        self.assertEqual((status, data["error"]["code"]), (503, "no_model"))
        self.assertTrue(data["error"]["hint"])
        self.assertEqual(self.ok("GET", "/api/history")["items"], [])

    def test_missing_model_is_a_clear_503(self):
        self.ok("PUT", "/api/settings", {"model": "ghost:7b"})
        status, data = self.start_sort()
        self.assertEqual((status, data["error"]["code"]), (503, "model_missing"))
        self.assertIn("ollama pull ghost:7b", data["error"]["hint"])

    def test_bad_requests(self):
        cases = [
            ({"text": ""}, 400, "empty"), ({"text": "   "}, 400, "empty"), ({"text": 5}, 400, "bad_request"),
            ({"text": "a", "engine": "gpt"}, 400, "bad_request"), ({"text": "a", "budget": -5}, 400, "bad_request"),
            ({"text": "a", "budget": 961}, 400, "bad_request"), ({"text": "a", "budget": "90"}, 400, "bad_request"),
            ({"text": "a", "budget": True}, 400, "bad_request"), ({"text": "a", "budget": 1.5}, 400, "bad_request"),
            ({"text": "\n".join("t%d" % n for n in range(250))}, 400, "too_many"),
            ({"text": "x" * 130_000}, 413, "too_long"),
        ]
        for body, status, code in cases:
            with self.subTest(body=str(body)[:50]):
                got_status, _, data = self.call("POST", "/api/sort", body)
                self.assertEqual((got_status, data["error"]["code"]), (status, code))

    def test_a_budget_in_the_request_wins_over_the_setting(self):
        text = "\n".join("Reply to Anita about item %d" % n for n in range(1, 6))
        self.mock.mode = "ok"
        result = self.sorted_list(text, engine="demo", budget=10)
        self.assertEqual((result["budget"], result["stats"]["counts"]["now"]), (10, 2))
        self.ok("PUT", "/api/settings", {"budget_minutes": 15})
        self.assertEqual(self.sorted_list(text, engine="demo")["budget"], 15)

    def test_settings_shape_the_run(self):
        self.ok("PUT", "/api/settings", {"chunk_size": 2, "never_skip": ["tap"]})
        result = self.sorted_list()
        self.assertEqual(len(self.mock.calls), 3)  # 5 tasks in batches of 2
        by_text = {i["text"]: i for i in result["tasks"]}
        self.assertEqual(by_text["Waiting for the landlord to fix the tap"]["bucket"], "later")

    def test_lessons_from_corrections_reach_the_next_prompt(self):
        first = self.sorted_list("Organize my photos folder")  # the mock model lets this expire
        self.assertEqual(first["tasks"][0]["bucket"], "expire")
        self.ok("PATCH", "/api/history/%s/tasks/t1" % first["sid"], {"outcome": "regret"})
        self.mock.calls.clear()
        self.sorted_list("Buy milk")
        system = self.mock.calls[0][1]["messages"][0]["content"]
        self.assertIn('"Organize my photos folder" -> do_later (they regretted skipping it)', system)
        self.ok("PUT", "/api/settings", {"lessons": False})
        self.mock.calls.clear()
        self.sorted_list("Buy bread")
        self.assertNotIn("Organize my photos folder", self.mock.calls[0][1]["messages"][0]["content"])

    def test_autosave_off_returns_the_result_but_keeps_nothing(self):
        self.ok("PUT", "/api/settings", {"autosave": False})
        result = self.sorted_list()
        self.assertEqual(len(result["tasks"]), 5)
        self.assertNotIn("sid", result)
        self.assertEqual(self.ok("GET", "/api/history")["items"], [])

    def test_model_failure_becomes_a_job_error_and_saves_nothing(self):
        self.mock.mode = "error500"
        snapshot = self.sort()
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["error"]["code"], "http")
        self.assertTrue(snapshot["error"]["message"])
        self.assertIsNone(snapshot["result"])
        self.assertEqual(self.ok("GET", "/api/history")["items"], [])

    def test_a_flaky_model_still_produces_a_full_list(self):
        self.mock.mode = "garbage"
        result = self.sorted_list()
        self.assertEqual(len(result["tasks"]), 5)
        self.assertEqual(result["engine"]["kind"], "demo")
        self.assertTrue(result["warnings"])

    def test_turning_fallback_off_turns_failures_into_errors(self):
        self.ok("PUT", "/api/settings", {"demo_fallback": False})
        self.mock.mode = "garbage"
        snapshot = self.sort()
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["error"]["code"], "format")

    def test_only_one_job_at_a_time_and_cancel(self):
        self.mock.delay = 0.3
        self.ok("PUT", "/api/settings", {"chunk_size": 1})
        status, first = self.start_sort("\n".join("Task %d" % n for n in range(1, 9)))
        self.assertEqual(status, 202)
        status, data = self.start_sort("Another one")
        self.assertEqual((status, data["error"]["code"]), (409, "busy"))
        time.sleep(0.45)
        self.ok("POST", "/api/jobs/%s/cancel" % first["job"])
        snapshot = self.wait_for(first["job"])
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertIsNone(snapshot["result"])
        self.assertLess(len(self.mock.calls), 8)
        self.assertEqual(self.ok("GET", "/api/history")["items"], [])
        self.mock.delay = 0
        self.assertEqual(self.sort("Fresh start")["status"], "done")

    def test_unknown_jobs(self):
        self.assertEqual(self.get("/api/jobs/aaaaaaaaaaaa")[0], 404)
        self.assertEqual(self.call("POST", "/api/jobs/aaaaaaaaaaaa/cancel", {})[0], 404)

    def test_the_server_keeps_answering_while_a_sort_runs(self):
        self.mock.delay = 0.4
        status, data = self.start_sort("Task one")
        self.assertEqual(status, 202)
        started = time.time()
        self.assertEqual(self.get("/api/history")[0], 200)
        self.assertEqual(self.get("/api/settings")[0], 200)
        self.assertLess(time.time() - started, 0.3)
        self.wait_for(data["job"])


class HistoryEditing(ServerCase):
    def setUp(self):
        super().setUp()
        self.result = self.sorted_list()
        self.sid = self.result["sid"]
        self.items = {i["text"]: i["id"] for i in self.result["tasks"]}

    def patch(self, text, body, sid=None):
        return self.call("PATCH", "/api/history/%s/tasks/%s" % (sid or self.sid, self.items.get(text, text)), body)

    def test_move_and_move_back(self):
        status, _, data = self.patch("Learn Rust someday", {"bucket": "later"})
        self.assertEqual(status, 200)
        moved = next(i for i in data["tasks"] if i["id"] == self.items["Learn Rust someday"])
        self.assertEqual((moved["user_bucket"], moved["bucket"]), ("later", "expire"))
        self.assertIn(moved["id"], data["layout"]["later"])
        _, _, data = self.patch("Learn Rust someday", {"bucket": None})
        self.assertIsNone(next(i for i in data["tasks"] if i["id"] == moved["id"])["user_bucket"])
        self.assertEqual(self.ok("GET", "/api/history/" + self.sid)["layout"], self.result["layout"])

    def test_outcomes_feed_the_checkins(self):
        self.patch("Learn Rust someday", {"outcome": "fine"})
        self.patch("Waiting for the landlord to fix the tap", {"outcome": "regret"})
        checkins = self.ok("GET", "/api/checkins")
        self.assertEqual(checkins["stats"]["resolved"], 2)
        self.assertEqual(checkins["stats"]["regret_rate"], 0.5)
        self.assertEqual({r["text"] for r in checkins["resolved"]}, {"Learn Rust someday", "Waiting for the landlord to fix the tap"})
        self.assertEqual(checkins["waiting"], [])  # the mock model keeps everything else

    def test_validation(self):
        cases = [
            ("Learn Rust someday", {}, 400, "bad_request"),
            ("Learn Rust someday", {"bucket": "banana"}, 400, "bad_bucket"),
            ("Learn Rust someday", {"outcome": "meh"}, 400, "bad_outcome"),
            ("Pay electricity bill by tomorrow", {"outcome": "fine"}, 400, "not_skipped"),
            ("t99", {"bucket": "now"}, 404, "not_found"),
        ]
        for text, body, status, code in cases:
            with self.subTest(body=body):
                got, _, data = self.patch(text, body)
                self.assertEqual((got, data["error"]["code"]), (status, code))
        self.assertEqual(self.patch("t1", {"bucket": "now"}, sid="../settings")[0], 404)  # never reaches the router as an id
        status, _, data = self.call("PATCH", "/api/history/..%2Fsettings/tasks/t1", {"bucket": "now"})
        self.assertEqual((status, data["error"]["code"]), (400, "bad_id"))
        self.assertEqual(self.patch("t1", {"bucket": "now"}, sid="20250101-000000-ffff")[0], 404)

    def test_budget(self):
        data = self.ok("POST", "/api/history/%s/budget" % self.sid, {"budget": 5})
        self.assertEqual(data["budget"], 5)
        self.assertEqual(self.ok("GET", "/api/history/" + self.sid)["budget"], 5)
        for bad in ({}, {"budget": "5"}, {"budget": -1}, {"budget": 961}, {"budget": True}):
            self.assertEqual(self.call("POST", "/api/history/%s/budget" % self.sid, bad)[0], 400, bad)

    def test_exports(self):
        for fmt, marker, content_type in (
            ("md", b"# The Won't-Do List", "text/markdown"), ("csv", b"bucket,task,minutes", "text/csv"),
            ("json", b'"tasks"', "application/json"), ("ics", b"BEGIN:VCALENDAR", "text/calendar"),
        ):
            with self.subTest(fmt=fmt):
                status, headers, body = self.get("/api/export/%s?fmt=%s" % (self.sid, fmt))
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith(content_type), headers["content-type"])
                self.assertIn(marker, body_bytes(body))
                self.assertNotIn("content-disposition", headers)
        status, headers, _ = self.get("/api/export/%s?fmt=csv&download=1" % self.sid)
        self.assertEqual(headers["content-disposition"], 'attachment; filename="wontdo-%s.csv"' % self.sid)
        self.assertEqual(self.get("/api/export/%s" % self.sid)[1]["content-type"][:13], "text/markdown")

    def test_export_errors(self):
        self.assertEqual(self.get("/api/export/%s?fmt=pdf" % self.sid)[0], 400)
        self.assertEqual(self.get("/api/export/20250101-000000-ffff?fmt=md")[0], 404)
        self.assertEqual(self.get("/api/export/not-an-id?fmt=md")[0], 400)

    def test_export_reflects_the_persons_edits(self):
        self.patch("Learn Rust someday", {"bucket": "now"})
        _, _, body = self.get("/api/export/%s?fmt=md" % self.sid)
        self.assertIn("- [ ] Learn Rust someday", body.decode())

    def test_delete(self):
        self.assertEqual(self.ok("DELETE", "/api/history/" + self.sid), {"ok": True})
        self.assertEqual(self.get("/api/history/" + self.sid)[0], 404)
        self.assertEqual(self.call("DELETE", "/api/history/" + self.sid)[0], 404)

    def test_forget_needs_confirmation(self):
        self.assertEqual(self.call("POST", "/api/forget", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/forget", {"confirm": "yes"})[0], 400)
        self.assertEqual(len(self.ok("GET", "/api/history")["items"]), 1)
        self.assertEqual(self.ok("POST", "/api/forget", {"confirm": "forget"}), {"removed": 1})
        self.assertEqual(self.ok("GET", "/api/history")["items"], [])


class ModelTest(ServerCase):
    def test_a_working_model(self):
        data = self.ok("POST", "/api/test-model", {})
        self.assertTrue(data["ok"])
        self.assertEqual([s["level"] for s in data["steps"]], ["ok"] * 5)

    def test_a_broken_model_explains_itself(self):
        self.mock.mode = "garbage"
        data = self.ok("POST", "/api/test-model", {})
        self.assertFalse(data["ok"])
        self.assertIn("sorry", data["raw"])

    def test_nothing_running(self):
        self.use_model("http://127.0.0.1:%d" % free_port())
        data = self.ok("POST", "/api/test-model", {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["steps"][0]["level"], "fail")


class JobManagerTests(unittest.TestCase):
    def wait(self, job):
        for _ in range(300):
            if job.status != "running":
                return
            time.sleep(0.01)
        self.fail("job still running")

    def test_one_at_a_time(self):
        manager = JobManager()
        gate = threading.Event()
        job = manager.start(lambda j: gate.wait(5))
        with self.assertRaises(ApiError) as ctx:
            manager.start(lambda j: None)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "busy"))
        gate.set()
        self.wait(job)
        self.assertEqual(job.status, "done")
        self.wait(manager.start(lambda j: None))

    def test_known_errors_become_job_errors(self):
        manager = JobManager()
        for exc, code in ((LLMError("http", "boom", hint="h"), "http"), (ParseError("empty", "nothing"), "empty")):
            def work(job, exc=exc):
                raise exc
            job = manager.start(work)
            self.wait(job)
            self.assertEqual((job.status, job.error["code"]), ("error", code))
        self.assertEqual(job.error["message"], "nothing")

    def test_unexpected_errors_are_contained(self):
        manager = JobManager()

        def work(job):
            raise RuntimeError("bug")

        with contextlib.redirect_stderr(io.StringIO()):
            job = manager.start(work)
            self.wait(job)
        self.assertEqual((job.status, job.error["code"]), ("error", "internal"))
        self.assertNotIn("bug", job.error["message"])  # internals are not shown to the page

    def test_cancelled(self):
        manager = JobManager()

        def work(job):
            raise server_module.Cancelled()

        job = manager.start(work)
        self.wait(job)
        self.assertEqual(job.status, "cancelled")

    def test_only_recent_jobs_are_kept(self):
        manager = JobManager(keep=3)
        jobs = []
        for _ in range(5):
            job = manager.start(lambda j: None)
            self.wait(job)
            jobs.append(job)
        with self.assertRaises(ApiError):
            manager.get(jobs[0].id)
        self.assertIs(manager.get(jobs[-1].id), jobs[-1])


if __name__ == "__main__":
    unittest.main()
