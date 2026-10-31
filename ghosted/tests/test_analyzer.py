import json
import unittest

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import common  # noqa: F401
from ghosted import analyzer, templates
from ghosted.errors import LLMFormatError
from ghosted.analyzer import analyze_issue, apply_guardrails, build_prompt, extract_json, normalize

ISSUE = {"number": 12, "title": "Crash when config is empty", "url": "https://github.com/o/r/issues/12",
         "author": "mira", "created_at": "2021-05-04T10:00:00Z", "labels": ["bug"],
         "body": "Crashes on 1.4.2 with Python 3.11.\n\nSteps: load an empty file.", "age_days": 1380}
CTX = {"repo": "o/r", "latest_release": {"tag": "v2.0.0", "date": "2022-01-01"}}
PR = {"kind": "pr", "strength": "closing", "ref": "PR #45", "title": "Handle empty config",
      "url": "https://github.com/o/r/pull/45", "date": "2021-07-02", "note": "declares that it fixes this issue"}
MENTION = dict(PR, strength="mention", note="mentions this issue")
SIMILAR = dict(PR, strength="similar", note="similar title")


def answer(**kw):
    base = {"verdict": "valid", "confidence": "high", "reason": "A real problem.",
            "reply": "Hi @mira, sorry for the silence. This still looks like a real bug.", "flags": []}
    base.update(kw)
    return base


class ExtractJsonTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_chatter_around(self):
        self.assertEqual(extract_json('Sure! Here you go: {"a": {"b": 2}} Hope that helps.'),
                         {"a": {"b": 2}})

    def test_think_block(self):
        self.assertEqual(extract_json('<think>hmm {"x": 0}</think>\n{"a": 1}'), {"a": 1})

    def test_raw_newline_inside_string(self):
        self.assertEqual(extract_json('{"reply": "line one\nline two"}'), {"reply": "line one\nline two"})

    def test_garbage(self):
        for bad in ("", "   ", None, "no json here", "[1, 2]", "{broken"):
            with self.assertRaises(LLMFormatError, msg=repr(bad)):
                extract_json(bad)


class NormalizeTests(unittest.TestCase):
    def test_synonyms(self):
        for word, want in (("valid", "valid"), ("Still Valid", "valid"), ("FIXED", "fixed"),
                           ("already fixed", "fixed"), ("needs info", "needs_info"),
                           ("needs-info", "needs_info"), ("NEEDS_INFO", "needs_info")):
            self.assertEqual(normalize({"verdict": word})["verdict"], want, word)

    def test_unknown_or_missing_verdict(self):
        for obj in ({"verdict": "maybe"}, {}, {"verdict": None}, {"verdict": ["valid", "fixed"]}):
            with self.assertRaises(LLMFormatError):
                normalize(obj)

    def test_defaults_and_cleanup(self):
        out = normalize({"verdict": "valid", "confidence": "certain", "reason": "  a\n b  ",
                         "reply": '"## Hi\n\n\n\nthere"'})
        self.assertEqual(out["confidence"], "medium")
        self.assertEqual(out["reason"], "a b")
        self.assertEqual(out["reply"], "Hi\n\nthere")

    def test_reply_may_arrive_as_list_or_number(self):
        self.assertEqual(normalize({"verdict": "valid", "reply": ["Hi", "there"]})["reply"], "Hi there")
        self.assertEqual(normalize({"verdict": "valid", "reply": 5})["reply"], "5")


class GuardrailTests(unittest.TestCase):
    def guard(self, analysis, evidence=(), issue=ISSUE):
        return apply_guardrails(analysis, issue, list(evidence), CTX)

    def test_good_answer_passes_untouched(self):
        out = self.guard(answer())
        self.assertEqual(out["flags"], [])
        self.assertEqual(out["verdict"], "valid")
        self.assertIn("real bug", out["reply"])

    def test_fixed_without_evidence_is_downgraded(self):
        out = self.guard(answer(verdict="fixed", reply="Hi @mira, this was fixed in v9.9.9!"))
        self.assertEqual(out["verdict"], "needs_info")
        self.assertEqual(out["confidence"], "low")
        self.assertIn("fixed_without_evidence", out["flags"])
        self.assertNotIn("9.9.9", out["reply"])
        self.assertIn("@mira", out["reply"])

    def test_closing_evidence_overrules_a_model_that_says_valid(self):
        out = self.guard(answer(verdict="valid"), [PR])
        self.assertEqual(out["verdict"], "fixed")
        self.assertIn("verdict_from_evidence", out["flags"])
        self.assertIn("PR #45", out["reply"])
        self.assertEqual(out["confidence"], "medium")

    def test_closing_evidence_and_agreeing_model_gets_high_confidence(self):
        out = self.guard(answer(verdict="fixed", confidence="low",
                                reply="Hi @mira, sorry. This was addressed in PR #45."), [PR])
        self.assertEqual(out["verdict"], "fixed")
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["flags"], [])

    def test_similar_only_caps_confidence(self):
        out = self.guard(answer(verdict="fixed", confidence="high",
                                reply="Hi @mira, sorry. A later change, PR #45, looks related."), [SIMILAR])
        self.assertEqual(out["confidence"], "low")

    def test_invented_pr_number_replaces_reply(self):
        out = self.guard(answer(verdict="fixed", reply="Hi @mira, fixed in #999 already."), [MENTION])
        self.assertIn("reply_replaced", out["flags"])
        self.assertNotIn("999", out["reply"])
        self.assertIn("PR #45", out["reply"])

    def test_real_pr_number_is_fine(self):
        out = self.guard(answer(verdict="fixed", reply="Hi @mira, sorry. Fixed in #45 apparently."), [MENTION])
        self.assertEqual(out["flags"], [])

    def test_invented_version_replaces_reply(self):
        out = self.guard(answer(reply="Hi @mira, please upgrade to 7.7.7 and retest."))
        self.assertIn("reply_replaced", out["flags"])
        self.assertNotIn("7.7.7", out["reply"])

    def test_versions_from_the_issue_and_release_are_fine(self):
        out = self.guard(answer(reply="Hi @mira, on 1.4.2 with Python 3.11, and v2.0.0 is the latest."))
        self.assertEqual(out["flags"], [])

    def test_foreign_link_replaces_reply(self):
        out = self.guard(answer(reply="Hi @mira, see https://evil.example/fix for details."))
        self.assertIn("reply_replaced", out["flags"])
        self.assertNotIn("evil.example", out["reply"])

    def test_repo_and_evidence_links_are_fine(self):
        out = self.guard(answer(verdict="fixed",
                                reply="Hi @mira, see https://github.com/o/r/pull/45 for the fix."), [MENTION])
        self.assertEqual(out["flags"], [])

    def test_other_mentions_are_defused(self):
        out = self.guard(answer(reply="Hi @mira, sorry. cc @someone-else and `@dataclass` too."))
        self.assertIn("@mira", out["reply"])
        self.assertNotIn("@someone-else", out["reply"])
        self.assertIn("someone-else", out["reply"])
        self.assertIn("`@dataclass`", out["reply"])

    def test_empty_reply_gets_a_template(self):
        out = self.guard(answer(reply="   "))
        self.assertIn("reply_replaced", out["flags"])
        self.assertTrue(out["reply"].startswith("Hi @mira"))

    def test_bad_reason_is_replaced(self):
        out = self.guard(answer(reason="Fixed by #777 for sure."))
        self.assertIn("reason_replaced", out["flags"])
        self.assertNotIn("777", out["reason"])

    def test_templates_themselves_never_trip_the_guardrails(self):
        for verdict in ("valid", "fixed", "needs_info"):
            for evidence in ([], [PR], [MENTION], [SIMILAR], [PR, MENTION, SIMILAR]):
                reply = templates.build_reply(verdict, ISSUE, evidence, "Looks real.")
                self.assertEqual(analyzer.unverified_claims(reply, ISSUE, evidence, CTX), [],
                                 (verdict, evidence, reply))


class PromptTests(unittest.TestCase):
    def test_prompt_contains_the_facts_and_no_secrets(self):
        system, user = build_prompt(ISSUE, [PR], CTX)
        self.assertIn("ISSUE #12: Crash when config is empty", user)
        self.assertIn("Reporter: @mira", user)
        self.assertIn("<issue_text>", user)
        self.assertIn("[closing] PR #45 on 2021-07-02", user)
        self.assertIn("Latest release: v2.0.0", user)
        self.assertIn("untrusted", system)

    def test_prompt_without_evidence_says_so(self):
        _system, user = build_prompt(ISSUE, [], CTX)
        self.assertIn("(none:", user)

    def test_long_bodies_are_truncated(self):
        issue = dict(ISSUE, body="x" * 9000)
        _system, user = build_prompt(issue, [], CTX)
        self.assertLess(len(user), 3500)


class StubClient(object):
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def chat(self, system, user):
        self.prompts.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class AnalyzeIssueTests(unittest.TestCase):
    GOOD = json.dumps({"verdict": "valid", "confidence": "high", "reason": "Real.",
                       "reply": "Hi @mira, sorry for the silence. Still a real bug."})

    def test_rules_engine_needs_no_client(self):
        out = analyze_issue(None, ISSUE, [PR], CTX)
        self.assertEqual(out["verdict"], "fixed")

    def test_retries_once_on_bad_json(self):
        client = StubClient(["I think it is valid!", self.GOOD])
        out = analyze_issue(client, ISSUE, [], CTX)
        self.assertEqual(out["verdict"], "valid")
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("ONLY one JSON object", client.prompts[1])

    def test_gives_up_after_the_retry(self):
        client = StubClient(["nope", "still nope"])
        with self.assertRaises(LLMFormatError):
            analyze_issue(client, ISSUE, [], CTX)

    def test_unknown_verdict_triggers_the_retry_too(self):
        client = StubClient([json.dumps({"verdict": "maybe"}), self.GOOD])
        self.assertEqual(analyze_issue(client, ISSUE, [], CTX)["verdict"], "valid")

    def test_other_errors_propagate_immediately(self):
        from ghosted.errors import LLMConnectionError
        client = StubClient([LLMConnectionError("down")])
        with self.assertRaises(LLMConnectionError):
            analyze_issue(client, ISSUE, [], CTX)
        self.assertEqual(len(client.prompts), 1)


class RulesAndTemplateTests(unittest.TestCase):
    def test_detail_score_and_asks(self):
        thin = {"title": "doesnt work", "body": "it dont work pls help", "labels": []}
        self.assertLess(templates.detail_score(thin), 2)
        asks = templates.missing_details(thin["title"] + " " + thin["body"])
        self.assertEqual(len(asks), 4)
        rich = templates.missing_details(ISSUE["body"])
        self.assertNotIn("which version you are using", rich)

    def test_reply_shapes(self):
        fixed = templates.build_reply("fixed", ISSUE, [PR, MENTION])
        self.assertIn("PR #45 (2021-07-02)", fixed)
        self.assertIn("1 other related change", fixed)
        self.assertIn("about 3 years and 9 months", fixed)
        weak = templates.build_reply("fixed", ISSUE, [SIMILAR])
        self.assertIn("does not mention this issue directly", weak)
        needs = templates.build_reply("needs_info", dict(ISSUE, body=""), [])
        self.assertIn("a little more detail", needs)
        ghost = templates.build_reply("valid", dict(ISSUE, author=""), [], "")
        self.assertTrue(ghost.startswith("Hi there,"))
        self.assertLess(len(templates.build_reply("fixed", ISSUE, [PR]).split()), 90)

    def test_join_and(self):
        self.assertEqual(templates.join_and(["a"]), "a")
        self.assertEqual(templates.join_and(["a", "b", "c"]), "a, b and c")
        self.assertEqual(templates.join_and([]), "")


if __name__ == "__main__":
    unittest.main()
