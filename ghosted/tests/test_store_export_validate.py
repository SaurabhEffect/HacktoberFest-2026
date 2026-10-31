import csv
import io
import json
import os
import tempfile
import unittest

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import common  # noqa: F401
from ghosted import export
from ghosted.llm import normalize_base_url, model_available, sort_models
from ghosted.pipeline import normalize_issue, parse_repo, validate_scan_request
from ghosted.store import Store
from ghosted.textutil import now_utc


def row(number, **kw):
    base = {"number": number, "title": "Issue %d" % number, "url": "https://github.com/o/r/issues/%d" % number,
            "author": "u%d" % number, "created_at": "2022-01-01T00:00:00Z", "age_days": 100 + number,
            "labels": ["bug"], "body": "body", "evidence": [{"ref": "PR #1", "strength": "closing",
                                                              "date": "2022-02-02", "url": "https://x/1"}]}
    base.update(kw)
    return base


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "s.db"))

    def tearDown(self):
        self.store.close()

    def test_scan_roundtrip(self):
        sid = self.store.create_scan("o/r", False, {"model": "m"}, "Ollama · m")
        scan = self.store.get_scan(sid)
        self.assertEqual((scan["repo"], scan["status"], scan["demo"]), ("o/r", "queued", False))
        self.assertEqual(scan["params"], {"model": "m"})
        self.store.update_scan(sid, status="running", total=3, stats={"a": [1, 2]}, repo_info={"x": "y"})
        scan = self.store.get_scan(sid)
        self.assertEqual((scan["status"], scan["total"], scan["stats"], scan["repo_info"]),
                         ("running", 3, {"a": [1, 2]}, {"x": "y"}))
        self.assertIsNone(self.store.get_scan(999))

    def test_unknown_fields_are_refused(self):
        sid = self.store.create_scan("o/r", False, {}, "e")
        with self.assertRaises(ValueError):
            self.store.update_scan(sid, **{"status = 'x', repo": "evil"})
        self.store.add_results(sid, [row(1)])
        rid = self.store.get_results(sid)[0]["id"]
        with self.assertRaises(ValueError):
            self.store.update_result(rid, number=5)

    def test_results_lifecycle(self):
        sid = self.store.create_scan("o/r", False, {}, "e")
        self.store.add_results(sid, [row(5), row(6), row(7)])
        results = self.store.get_results(sid)
        self.assertEqual([r["number"] for r in results], [5, 6, 7])
        self.assertEqual(results[0]["labels"], ["bug"])
        self.assertEqual(results[0]["evidence"][0]["ref"], "PR #1")
        self.assertEqual(results[0]["status"], "pending")

        self.store.finish_result(results[0]["id"], {"verdict": "fixed", "confidence": "high",
                                                    "reason": "r", "reply": "hello", "flags": ["f"]})
        self.store.fail_result(results[1]["id"], "boom")
        counts = self.store.counts(sid)
        self.assertEqual((counts["done"], counts["error"], counts["pending"], counts["fixed"]), (1, 1, 1, 1))

        self.assertEqual(self.store.reset_unfinished(sid), 2)
        self.assertEqual([r["number"] for r in self.store.pending_results(sid)], [6, 7])
        done = self.store.get_result(results[0]["id"])
        self.assertEqual((done["status"], done["flags"], done["review"]), ("done", ["f"], "new"))

    def test_finish_result_clears_a_previous_edit(self):
        sid = self.store.create_scan("o/r", False, {}, "e")
        self.store.add_results(sid, [row(1)])
        rid = self.store.get_results(sid)[0]["id"]
        self.store.update_result(rid, edited_reply="mine")
        self.store.finish_result(rid, {"verdict": "valid", "confidence": "low", "reason": "r", "reply": "new"})
        self.assertIsNone(self.store.get_result(rid)["edited_reply"])

    def test_delete_cascades(self):
        sid = self.store.create_scan("o/r", False, {}, "e")
        self.store.add_results(sid, [row(1)])
        self.assertTrue(self.store.delete_scan(sid))
        self.assertEqual(self.store.get_results(sid), [])
        self.assertFalse(self.store.delete_scan(sid))

    def test_mark_interrupted(self):
        a = self.store.create_scan("o/r", False, {}, "e")
        b = self.store.create_scan("o/r", False, {}, "e")
        self.store.update_scan(a, status="running")
        self.store.update_scan(b, status="done")
        self.assertEqual(self.store.mark_interrupted(), 1)
        self.assertEqual(self.store.get_scan(a)["status"], "error")
        self.assertIn("restarted", self.store.get_scan(a)["error"])
        self.assertEqual(self.store.get_scan(b)["status"], "done")

    def test_list_is_newest_first(self):
        ids = [self.store.create_scan("o/r%d" % i, False, {}, "e") for i in range(3)]
        self.assertEqual([s["id"] for s in self.store.list_scans()], ids[::-1])

    def test_unicode_survives(self):
        sid = self.store.create_scan("o/r", False, {}, "e")
        self.store.add_results(sid, [row(1, title="café ☕ 日本語")])
        self.assertEqual(self.store.get_results(sid)[0]["title"], "café ☕ 日本語")


class ValidateTests(unittest.TestCase):
    def test_parse_repo(self):
        for text, want in (("a/b", "a/b"), (" a/b ", "a/b"), ("https://github.com/a/b", "a/b"),
                           ("github.com/a/b/issues/3", "a/b"), ("git@github.com:a/b.git", "a/b"),
                           ("a/b.git", "a/b"), ("a/b.js", "a/b.js"), ("a/.github", "a/.github")):
            self.assertEqual(parse_repo(text), want, text)
        for bad in ("", None, 5, "a", "a/b/c", "../x", "a/..", "a b/c", "-a/b", "https://gitlab.com/a/b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_repo(bad)

    def test_defaults(self):
        params, token = validate_scan_request({"repo": "a/b", "model": "gemma3:4b"})
        self.assertEqual((params["backend"], params["max_issues"], params["min_age_days"],
                          params["order"], params["timeout"]), ("ollama", 20, 30, "oldest", 180))
        self.assertEqual(params["base_url"], "http://localhost:11434")
        self.assertIsNone(token)

    def test_demo_ignores_repo(self):
        params, _ = validate_scan_request({"demo": True, "repo": "nonsense", "backend": "rules"})
        self.assertEqual((params["repo"], params["demo"], params["model"]), ("ghost-town/forgotten-lib", True, ""))

    def test_model_is_required_unless_rules(self):
        with self.assertRaises(ValueError):
            validate_scan_request({"repo": "a/b"})
        params, _ = validate_scan_request({"repo": "a/b", "backend": "rules"})
        self.assertEqual(params["base_url"], "")

    def test_clamping_and_types(self):
        params, _ = validate_scan_request({"repo": "a/b", "backend": "rules", "max_issues": 9999,
                                           "min_age_days": -5, "timeout": "7"})
        self.assertEqual((params["max_issues"], params["min_age_days"], params["timeout"]), (200, 0, 10))
        for bad in ({"max_issues": "lots"}, {"max_issues": True}, {"order": "sideways"},
                    {"backend": "skynet"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                validate_scan_request(dict({"repo": "a/b", "backend": "rules"}, **bad))
        with self.assertRaises(ValueError):
            validate_scan_request("not a dict")

    def test_token_rules(self):
        _params, token = validate_scan_request({"repo": "a/b", "backend": "rules", "token": "  ghp_abc123  "})
        self.assertEqual(token, "ghp_abc123")
        for bad in ("has space", "new\nline", "emoji\U0001f47b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_scan_request({"repo": "a/b", "backend": "rules", "token": bad})

    def test_server_address_and_model_name_checks(self):
        with self.assertRaises(ValueError):
            validate_scan_request({"repo": "a/b", "model": "m", "base_url": "ftp://x"})
        with self.assertRaises(ValueError):
            validate_scan_request({"repo": "a/b", "model": "bad name; rm -rf"})
        params, _ = validate_scan_request({"repo": "a/b", "model": "m", "backend": "openai",
                                           "base_url": "localhost:1234"})
        self.assertEqual(params["base_url"], "http://localhost:1234/v1")

    def test_normalize_issue(self):
        raw = {"number": 3, "title": "T", "body": "<!-- hi -->text", "user": None, "labels": [{"name": "a"}, "b", None],
               "created_at": "2020-01-01T00:00:00Z", "html_url": "u"}
        issue = normalize_issue(raw, now_utc())
        self.assertEqual((issue["body"], issue["author"], issue["labels"]), ("text", "", ["a", "b"]))
        self.assertGreater(issue["age_days"], 365)

    def test_llm_helpers(self):
        self.assertEqual(normalize_base_url("ollama", "localhost:11434/"), "http://localhost:11434")
        self.assertEqual(normalize_base_url("ollama", "http://h:1/api/chat"), "http://h:1")
        self.assertEqual(normalize_base_url("openai", "http://h:1/v1/chat/completions"), "http://h:1/v1")
        self.assertEqual(normalize_base_url("openai", ""), "http://localhost:1234/v1")
        self.assertTrue(model_available(["gemma3:latest"], "gemma3"))
        self.assertFalse(model_available(["gemma3:4b"], "gemma3"))
        self.assertEqual(sort_models(["zeta", "nomic-embed-text", "gemma3:4b", "alpha"]),
                         ["gemma3:4b", "alpha", "zeta"])


class ExportTests(unittest.TestCase):
    SCAN = {"repo": "o/r", "created_at": "2026-10-30T10:00:00Z", "engine": "Ollama · m", "stats": {"x": 1}}

    def results(self):
        def done(number, verdict, review="new", edited=None, title=None):
            return dict(row(number, title=title or "Issue %d" % number), status="done", verdict=verdict,
                        confidence="high", reason="because", reply="Hi\nthere", edited_reply=edited,
                        review=review, flags=[])
        return [done(1, "fixed", "approved"), done(2, "valid", edited="my edit"),
                done(3, "needs_info", title="=HYPERLINK(\"http://evil\")"),
                dict(row(4), status="error", verdict=None, reply="", edited_reply=None, review="new", flags=[])]

    def test_markdown(self):
        text = export.export_markdown(self.SCAN, self.results())
        self.assertIn("# Ghosted review sheet: o/r", text)
        self.assertIn("nothing was posted to GitHub", text)
        self.assertIn("3 issues: 1 already fixed, 1 still valid, 1 need info.", text)
        self.assertIn("> my edit", text)
        self.assertIn("> Hi\n> there", text)
        self.assertNotIn("Issue 4", text)

    def test_only_approved(self):
        text = export.export_markdown(self.SCAN, self.results(), "approved")
        self.assertIn("1 issue (approved only)", text)
        self.assertNotIn("Issue 2", text)

    def test_csv_is_safe_and_parses(self):
        text = export.export_csv(self.SCAN, self.results())
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][0], "number")
        titles = [r[1] for r in rows[1:]]
        self.assertIn("'=HYPERLINK(\"http://evil\")", titles)
        self.assertEqual(rows[2][11], "my edit")

    def test_json(self):
        data = json.loads(export.export_json(self.SCAN, self.results(), "approved"))
        self.assertFalse(data["posted_to_github"])
        self.assertEqual([i["number"] for i in data["issues"]], [1])

    def test_filename(self):
        self.assertEqual(export.export_filename(self.SCAN, "md"), "ghosted-o-r-20261030.md")
        self.assertEqual(export.export_filename({"repo": "a/..\\evil", "created_at": ""}, "csv"),
                         "ghosted-a-.-evil-export.csv")


if __name__ == "__main__":
    unittest.main()
