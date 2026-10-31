import unittest

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import common  # noqa: F401  (sets up sys.path)
from ghosted import textutil
from ghosted.demo import DEMO_REPO, fixture
from ghosted.evidence import EvidenceIndex, best_strength, find_refs, is_similar, latest_release, tokens


class TextUtilTests(unittest.TestCase):
    def test_span_phrase(self):
        self.assertEqual(textutil.span_phrase(0), "less than a day")
        self.assertEqual(textutil.span_phrase(1), "1 day")
        self.assertEqual(textutil.span_phrase(44), "44 days")
        self.assertEqual(textutil.span_phrase(400), "about 1 year and 1 month")
        self.assertEqual(textutil.span_phrase(1380), "about 3 years and 9 months")
        self.assertEqual(textutil.span_phrase(730), "about 2 years")

    def test_span_short(self):
        self.assertEqual(textutil.span_short(10), "10d")
        self.assertEqual(textutil.span_short(45), "1mo")
        self.assertEqual(textutil.span_short(730), "2y")
        self.assertEqual(textutil.span_short(1380), "3y 9mo")

    def test_parse_ts(self):
        self.assertEqual(textutil.parse_ts("2021-05-04T10:00:00Z").year, 2021)
        self.assertEqual(textutil.parse_ts("2021-05-04T10:00:00+02:00").hour, 8)
        self.assertIsNotNone(textutil.parse_ts("2021-05-04T10:00:00"))
        self.assertIsNone(textutil.parse_ts("garbage"))
        self.assertIsNone(textutil.parse_ts(None))
        self.assertEqual(textutil.day_str("2021-05-04T10:00:00Z"), "2021-05-04")

    def test_clean_markdown_and_truncate(self):
        text = "Hello<!-- secret template hint -->  world\r\n\r\n\r\n\r\nbye"
        self.assertEqual(textutil.clean_markdown(text, 100), "Hello world\n\nbye")
        self.assertEqual(textutil.clean_markdown(None, 10), "")
        self.assertEqual(textutil.truncate("abcdef", 4), "abc…")
        self.assertEqual(textutil.truncate("abc", 4), "abc")
        self.assertEqual(textutil.truncate(None, 4), "")


class RefTests(unittest.TestCase):
    def refs(self, text, repo="me/repo"):
        return find_refs(text, repo)

    def test_closing_keywords(self):
        for phrase in ("Fixes #3", "fixed #3", "Closes: #3", "resolves #3", "RESOLVED  #3", "fix #3"):
            closing, mentioned = self.refs(phrase)
            self.assertEqual(closing, {3}, phrase)
            self.assertEqual(mentioned, {3}, phrase)

    def test_plain_mentions(self):
        closing, mentioned = self.refs("See #5 and GH-6, also (#7).")
        self.assertEqual(closing, set())
        self.assertEqual(mentioned, {5, 6, 7})

    def test_negation_is_not_closing(self):
        closing, mentioned = self.refs("This does not fix #3 but helps")
        self.assertEqual(closing, set())
        self.assertEqual(mentioned, {3})
        closing, _ = self.refs("partially fixes #4")
        self.assertEqual(closing, set())

    def test_other_repositories_are_ignored(self):
        closing, mentioned = self.refs("Fixes other/project#3 and mine/repo#4")
        self.assertEqual((closing, mentioned), (set(), set()))
        closing, mentioned = self.refs("see owner/repo#9")
        self.assertEqual(mentioned, set())

    def test_qualified_references_to_this_repository_count(self):
        closing, mentioned = self.refs("Fixes me/repo#5 and see me/repo#6, not other/x#7")
        self.assertEqual(closing, {5})
        self.assertEqual(mentioned, {5, 6})

    def test_urls(self):
        closing, mentioned = self.refs(
            "Closes https://github.com/me/repo/issues/9 and see https://github.com/me/repo/pull/10 "
            "but not https://github.com/x/y/issues/11")
        self.assertEqual(closing, {9})
        self.assertEqual(mentioned, {9, 10})

    def test_no_refs(self):
        self.assertEqual(self.refs("nothing to see here, version 1.2.3"), (set(), set()))
        self.assertEqual(self.refs(""), (set(), set()))
        self.assertEqual(self.refs(None), (set(), set()))


class SimilarityTests(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(tokens("Fix typos in README install instructions"),
                         {"typo", "readme", "install", "instruction"})
        self.assertNotIn("this", tokens("this is a problem"))

    def test_needs_two_words_and_half_coverage(self):
        issue = tokens("Docs: typo in README install section")
        self.assertTrue(is_similar(issue, tokens("Fix typos in README install instructions")))
        self.assertFalse(is_similar(issue, tokens("Fix typos elsewhere")))   # one shared word
        self.assertFalse(is_similar(tokens("Crash"), tokens("Crash handling")))  # too short a title
        self.assertFalse(is_similar(set(), tokens("anything goes")))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.index = EvidenceIndex(DEMO_REPO, self.data["prs"], self.data["commits"],
                                   self.data["releases"])
        self.issues = {i["number"]: i for i in self.data["issues"]}

    def evidence(self, number):
        raw = self.issues[number]
        return self.index.for_issue({"number": number, "title": raw["title"],
                                     "created_at": raw["created_at"]})

    def test_closing_pr(self):
        ev = self.evidence(3)
        self.assertEqual([(e["ref"], e["strength"]) for e in ev], [("PR #41", "closing")])
        self.assertTrue(ev[0]["url"].endswith("/pull/41"))

    def test_squash_commit_is_not_double_counted(self):
        # commit "Handle empty config files (#41)" is just PR #41's merge commit
        self.assertEqual(len(self.evidence(3)), 1)
        self.assertEqual([e["ref"] for e in self.evidence(36)], ["PR #61", "release v0.6.0"])

    def test_commit_closing_and_release_via_pr(self):
        self.assertEqual([(e["ref"], e["strength"]) for e in self.evidence(15)],
                         [("commit b2c3d4e", "closing")])
        by_ref = dict((e["ref"], e) for e in self.evidence(9))
        self.assertEqual(by_ref["PR #58"]["strength"], "mention")
        self.assertEqual(by_ref["release v0.5.0"]["strength"], "mention")

    def test_release_lists_the_fixing_pr(self):
        raw = fixture()
        raw["releases"][0]["body"] = "- Handled empty files in https://github.com/%s/pull/41" % DEMO_REPO
        index = EvidenceIndex(DEMO_REPO, raw["prs"], raw["commits"], raw["releases"])
        ev = index.for_issue({"number": 3, "title": "Crash when config file is empty",
                              "created_at": self.issues[3]["created_at"]})
        notes = dict((e["ref"], e["note"]) for e in ev)
        self.assertIn("PR #41", notes["release v0.6.0"])

    def test_similar_only(self):
        ev = self.evidence(18)
        self.assertEqual([(e["ref"], e["strength"]) for e in ev], [("commit a1b2c3d", "similar")])
        self.assertEqual(best_strength(ev), "similar")

    def test_no_evidence(self):
        for number in (7, 12, 21, 24, 27, 30, 33):
            self.assertEqual(self.evidence(number), [], number)
        self.assertIsNone(best_strength([]))

    def test_changes_before_the_issue_existed_do_not_count(self):
        late = {"number": 3, "title": "Crash when config file is empty",
                "created_at": "2099-01-01T00:00:00Z"}
        self.assertEqual(self.index.for_issue(late), [])

    def test_unmerged_prs_and_drafts_are_ignored(self):
        index = EvidenceIndex(
            DEMO_REPO,
            [{"number": 5, "title": "x", "body": "Fixes #3", "merged_at": None}],
            [], [{"tag_name": "v9", "draft": True, "body": "(#3)", "published_at": "2099-01-01T00:00:00Z"}])
        self.assertEqual(index.for_issue({"number": 3, "title": "Crash", "created_at": "2020-01-01T00:00:00Z"}), [])

    def test_garbage_records_do_not_crash(self):
        index = EvidenceIndex(DEMO_REPO, [None, "x", {"merged_at": "2022-01-01T00:00:00Z"}],
                              [None, {}, {"commit": None}], [None, {"draft": False}])
        self.assertEqual(index.for_issue({"number": 1, "title": "t", "created_at": None}), [])

    def test_limit(self):
        prs = [{"number": 100 + i, "title": "PR %d" % i, "body": "Fixes #3",
                "merged_at": "2022-01-%02dT00:00:00Z" % (i + 1), "html_url": "u"} for i in range(9)]
        index = EvidenceIndex(DEMO_REPO, prs, [], [])
        ev = index.for_issue({"number": 3, "title": "t", "created_at": "2021-01-01T00:00:00Z"}, limit=5)
        self.assertEqual(len(ev), 5)

    def test_latest_release(self):
        rel = latest_release(self.data["releases"] + [
            {"tag_name": "v1.0.0-rc1", "prerelease": True, "published_at": "2099-01-01T00:00:00Z"},
            {"tag_name": "v2", "draft": True, "published_at": "2099-01-01T00:00:00Z"}])
        self.assertEqual(rel["tag"], "v0.6.0")
        self.assertIsNone(latest_release([]))


if __name__ == "__main__":
    unittest.main()
