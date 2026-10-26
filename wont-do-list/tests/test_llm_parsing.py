import json
import unittest

from wontdo.constants import EXPIRE, LATER, NOTMINE, NOW
from wontdo.llm import (
    LLMError,
    ModelInfo,
    canon_bucket,
    choose_model,
    coerce_int,
    extract_rows,
    model_score,
    nearest_minutes,
    normalize_base,
    normalize_verdict,
)
from wontdo.util import is_local_url


def row(i, bucket="do_later", **extra):
    base = {"id": i, "if_ignored": "x", "risk": "low", "bucket": bucket, "why": "y", "minutes": 15}
    base.update(extra)
    return base


GOOD = json.dumps({"tasks": [row(1, "do_now"), row(2, "let_expire")]})


class ExtractRows(unittest.TestCase):
    def ids(self, text):
        rows, salvaged = extract_rows(text)
        return [r["id"] for r in rows], salvaged

    def test_clean_json(self):
        self.assertEqual(self.ids(GOOD), ([1, 2], False))

    def test_code_fence_and_chatter(self):
        self.assertEqual(self.ids("Sure!\n```json\n%s\n```\nDone." % GOOD), ([1, 2], False))
        self.assertEqual(self.ids("I checked each task. %s Let me know." % GOOD), ([1, 2], False))

    def test_think_blocks_are_removed(self):
        self.assertEqual(self.ids("<think>hmm {not json} ...</think>" + GOOD), ([1, 2], False))

    def test_trailing_commas_and_smart_quotes(self):
        messy = '{"tasks":[{"id":1,"bucket":"do_now","why":"a",},{"id":2,"bucket":"do_later",},]}'
        self.assertEqual(self.ids(messy), ([1, 2], False))
        quoted = GOOD.replace('"id"', "“id”", 1)
        self.assertEqual(self.ids(quoted)[0], [1, 2])

    def test_bare_array(self):
        self.assertEqual(self.ids(json.dumps([row(1), row(2)])), ([1, 2], False))

    def test_dict_keyed_by_id(self):
        keyed = {"1": {"bucket": "do_now"}, "2": {"bucket": "let_expire"}}
        self.assertEqual(self.ids(json.dumps(keyed)), ([1, 2], False))

    def test_other_wrapper_keys(self):
        self.assertEqual(self.ids(json.dumps({"results": [row(1)]})), ([1], False))

    def test_single_object(self):
        self.assertEqual(self.ids(json.dumps(row(7))), ([7], False))

    def test_truncated_output_recovers_complete_rows(self):
        full = json.dumps({"tasks": [row(1), row(2), row(3)]})
        cut = full[: full.index('"id": 3') + 15]
        ids, salvaged = self.ids(cut)
        self.assertEqual(ids, [1, 2])
        self.assertTrue(salvaged)

    def test_strings_with_braces_do_not_confuse_the_scanner(self):
        tricky = json.dumps({"tasks": [row(1, why="use {curly} and \"quotes\""), row(2)]})
        self.assertEqual(self.ids(tricky), ([1, 2], False))
        self.assertEqual(self.ids(tricky[: tricky.rindex("}")])[0], [1, 2])

    def test_unusable_replies_raise(self):
        for text in ("", "   ", "I cannot help with that.", "{}", '{"tasks": []}', "[1, 2, 3]", "{broken"):
            with self.assertRaises(LLMError) as ctx:
                extract_rows(text)
            self.assertEqual(ctx.exception.kind, "format", text)

    def test_huge_input_is_bounded(self):
        rows, _ = extract_rows(GOOD + " " * 500_000)
        self.assertEqual(len(rows), 2)


class Normalize(unittest.TestCase):
    def test_exact_values(self):
        v = normalize_verdict(row(1, "do_now", risk="high", minutes=30))
        self.assertEqual(v, {"bucket": NOW, "risk": "high", "minutes": 30, "why": "y", "if_ignored": "x"})

    def test_bucket_synonyms(self):
        cases = {"Do Now": NOW, "do-now": NOW, "NOW": NOW, "later": LATER, "Defer": LATER, "let it expire": EXPIRE,
                 "skip": EXPIRE, "Not my problem": NOTMINE, "delegate": NOTMINE, "do_later": LATER}
        for raw, want in cases.items():
            self.assertEqual(canon_bucket(raw), want, raw)

    def test_unknown_bucket_is_rejected(self):
        self.assertIsNone(canon_bucket("banana"))
        self.assertIsNone(normalize_verdict(row(1, "banana")))
        self.assertIsNone(normalize_verdict({"id": 1}))

    def test_risk_words(self):
        self.assertEqual(normalize_verdict(row(1, risk="HIGH"))["risk"], "high")
        self.assertEqual(normalize_verdict(row(1, risk="moderate"))["risk"], "medium")
        self.assertEqual(normalize_verdict(row(1, risk="???"))["risk"], "low")
        self.assertEqual(normalize_verdict({"bucket": "do_now"})["risk"], "low")

    def test_minutes_snap_to_known_sizes(self):
        self.assertEqual(nearest_minutes(14), 15)
        self.assertEqual(nearest_minutes("15 min"), 15)
        self.assertEqual(nearest_minutes("2 hours"), 120)
        self.assertEqual(nearest_minutes(1000), 240)
        self.assertEqual(nearest_minutes(0), 5)
        self.assertEqual(nearest_minutes(None), 15)
        self.assertEqual(nearest_minutes("soon"), 15)
        self.assertEqual(nearest_minutes(45), 30)  # ties go to the smaller size

    def test_text_is_clipped_and_cleaned(self):
        v = normalize_verdict(row(1, why="a\n\nb  c" + "z" * 300))
        self.assertLessEqual(len(v["why"]), 160)
        self.assertNotIn("\n", v["why"])
        self.assertEqual(normalize_verdict({"bucket": "do_now", "consequence": "boom"})["if_ignored"], "boom")

    def test_coerce_int(self):
        self.assertEqual(coerce_int(3), 3)
        self.assertEqual(coerce_int("3"), 3)
        self.assertEqual(coerce_int("#3"), 3)
        self.assertEqual(coerce_int(3.0), 3)
        for bad in (None, True, "x", 3.5, [], {}):
            self.assertIsNone(coerce_int(bad), bad)


class ChoosingAModel(unittest.TestCase):
    def models(self, *names):
        return [ModelInfo(name=n, params=p) for n, p in names]

    def test_preferred_wins_even_if_missing(self):
        self.assertEqual(choose_model(self.models(("a", "")), "custom:1b"), "custom:1b")

    def test_prefers_small_instruct_family(self):
        ms = self.models(("llama3.1:70b", "70.6B"), ("qwen2.5:7b", "7.6B"), ("gemma3:4b", "4.3B"))
        self.assertEqual(choose_model(ms), "gemma3:4b")

    def test_skips_embedding_models(self):
        ms = self.models(("nomic-embed-text:latest", "137M"), ("llama3.2:3b", "3.2B"))
        self.assertEqual(choose_model(ms), "llama3.2:3b")

    def test_only_embedding_models_still_returns_something(self):
        self.assertEqual(choose_model(self.models(("nomic-embed-text", ""))), "nomic-embed-text")

    def test_size_is_read_from_the_name_when_params_missing(self):
        self.assertGreater(model_score(ModelInfo("mystery-4b")), model_score(ModelInfo("mystery-70b")))

    def test_huge_models_are_deprioritised(self):
        self.assertLess(model_score(ModelInfo("qwen2.5:72b", "72B")), model_score(ModelInfo("qwen2.5:3b", "3B")))

    def test_empty(self):
        self.assertEqual(choose_model([]), "")


class Urls(unittest.TestCase):
    def test_local(self):
        for url in ("http://127.0.0.1:11434", "http://localhost:8080", "http://[::1]:1234", "http://127.0.0.5", "http://0.0.0.0:80",
                    "http://[::ffff:127.0.0.1]:80", "http://foo.localhost"):
            self.assertTrue(is_local_url(url), url)

    def test_not_local(self):
        for url in ("http://192.168.1.20:11434", "https://api.example.com", "http://10.0.0.2", "http://localhost.evil.com", "", "nonsense"):
            self.assertFalse(is_local_url(url), url)

    def test_normalize_base(self):
        self.assertEqual(normalize_base("localhost:8080/"), "http://localhost:8080")
        self.assertEqual(normalize_base("http://x:1/v1"), "http://x:1")
        self.assertEqual(normalize_base("http://x:1/api/"), "http://x:1")
        self.assertEqual(normalize_base(""), "")


if __name__ == "__main__":
    unittest.main()
