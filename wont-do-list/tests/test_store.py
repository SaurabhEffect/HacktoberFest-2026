import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from wontdo import store as store_module
from wontdo.constants import EXPIRE, LATER, NOTMINE, NOW
from wontdo.judge import SortOptions, sort_tasks
from wontdo.store import DEFAULTS, Store, StoreError, public_settings, validate_settings

TODAY = dt.date(2026, 10, 20)

LIST = """Pay electricity bill by Friday
Learn Rust someday
Waiting for the landlord to fix the tap
Reply to the webinar invite
Book dentist for a check-up"""


def make_result(text=LIST, today=TODAY, budget=90):
    return sort_tasks(text, None, SortOptions(today=today, budget=budget, engine="demo"))


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wontdo-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = Store(os.path.join(self.tmp, "data"))
        self.counter = 0

    def save(self, result=None):
        """Save with predictable, increasing ids so 'newest first' is testable."""
        result = result or make_result()
        self.counter += 1
        sid = "20261020-1200%02d-%04x" % (self.counter, self.counter)
        with mock.patch("wontdo.store.new_sid", return_value=sid):
            return self.store.save_result(result)

    def item(self, sid, text_start):
        result = self.store.get_result(sid)
        return next(i for i in result["tasks"] if i["text"].startswith(text_start))


class Settings(StoreCase):
    def test_defaults_when_nothing_saved(self):
        self.assertEqual(self.store.get_settings(), DEFAULTS)

    def test_update_persists_and_merges(self):
        self.store.update_settings({"budget_minutes": 45})
        again = Store(self.store.dir)
        self.assertEqual(again.get_settings()["budget_minutes"], 45)
        self.assertEqual(again.get_settings()["chunk_size"], DEFAULTS["chunk_size"])
        again.update_settings({"chunk_size": 4})
        self.assertEqual(again.get_settings()["budget_minutes"], 45)

    def test_defaults_are_never_shared(self):
        self.store.get_settings()["never_skip"].append("mom")
        self.assertEqual(self.store.get_settings()["never_skip"], [])
        self.assertEqual(DEFAULTS["never_skip"], [])

    def test_validation(self):
        bad = [
            {"nonsense": 1}, {"provider": "cloud"}, {"budget_minutes": -1}, {"budget_minutes": 961},
            {"budget_minutes": "lots"}, {"budget_minutes": True}, {"budget_minutes": None},
            {"chunk_size": 0}, {"chunk_size": 21}, {"timeout_s": 5}, {"checkin_days": 0},
            {"temperature": 1.5}, {"temperature": "hot"}, {"date_order": "ymd"}, {"allow_remote": "yes"},
            {"lessons": 1}, {"autosave": None}, {"base_url": "ftp://host"}, {"base_url": "http://"},
            {"model": "x" * 121}, {"api_key": "a\nb"}, {"never_skip": 5}, {"never_skip": ["a"]},
            {"never_skip": ["x" * 41]}, {"never_skip": ["word%02d" % n for n in range(51)]},
        ]
        for patch in bad:
            with self.subTest(patch=patch):
                with self.assertRaises(StoreError) as ctx:
                    validate_settings(patch)
                self.assertEqual(ctx.exception.code, "bad_setting")

    def test_cleaning(self):
        clean = validate_settings(
            {
                "base_url": "localhost:11434/", "budget_minutes": "60", "temperature": 0.123, "provider": "ollama",
                "never_skip": "mom, Mom\n  my   boss ,, ", "model": "  gemma3:4b ",
            }
        )
        self.assertEqual(clean["base_url"], "http://localhost:11434")
        self.assertEqual(clean["budget_minutes"], 60)
        self.assertEqual(clean["temperature"], 0.12)
        self.assertEqual(clean["never_skip"], ["mom", "my boss"])
        self.assertEqual(clean["model"], "gemma3:4b")
        self.assertEqual(validate_settings({"base_url": ""})["base_url"], "")

    def test_a_bad_update_changes_nothing(self):
        self.store.update_settings({"budget_minutes": 45})
        with self.assertRaises(StoreError):
            self.store.update_settings({"budget_minutes": 30, "chunk_size": 999})
        self.assertEqual(self.store.get_settings()["budget_minutes"], 45)

    def test_api_key_none_means_leave_it(self):
        self.store.update_settings({"api_key": "sekret"})
        self.store.update_settings({"api_key": None, "budget_minutes": 30})
        self.assertEqual(self.store.get_settings()["api_key"], "sekret")
        self.store.update_settings({"api_key": ""})
        self.assertEqual(self.store.get_settings()["api_key"], "")

    def test_public_settings_hide_the_key(self):
        shown = public_settings({"api_key": "sekret", "model": "m"})
        self.assertNotIn("api_key", shown)
        self.assertIs(shown["api_key_set"], True)
        self.assertIs(public_settings({"api_key": ""})["api_key_set"], False)

    def test_corrupt_file_is_set_aside_not_fatal(self):
        with open(os.path.join(self.store.dir, "settings.json"), "w") as handle:
            handle.write("{not json")
        self.assertEqual(self.store.get_settings(), DEFAULTS)
        self.assertTrue(os.path.exists(os.path.join(self.store.dir, "settings.json.bad")))

    def test_one_bad_hand_edited_value_does_not_lose_the_rest(self):
        with open(os.path.join(self.store.dir, "settings.json"), "w") as handle:
            json.dump({"budget_minutes": 45, "chunk_size": "banana", "mystery": 1, "model": "gemma3:4b"}, handle)
        settings = self.store.get_settings()
        self.assertEqual((settings["budget_minutes"], settings["model"]), (45, "gemma3:4b"))
        self.assertEqual(settings["chunk_size"], DEFAULTS["chunk_size"])
        self.assertNotIn("mystery", settings)

    def test_a_settings_file_that_is_not_an_object(self):
        with open(os.path.join(self.store.dir, "settings.json"), "w") as handle:
            handle.write("[1, 2]")
        self.assertEqual(self.store.get_settings(), DEFAULTS)


class History(StoreCase):
    def test_round_trip(self):
        result = make_result()
        sid = self.save(result)
        loaded = self.store.get_result(sid)
        self.assertEqual(loaded["sid"], sid)
        self.assertEqual(loaded["tasks"], result["tasks"])
        self.assertTrue(os.path.exists(os.path.join(self.store.history_dir, sid + ".json")))

    def test_ids_are_validated(self):
        for bad in ("../settings", "..", "x", "20261020-120001", "20261020-120001-zzzz", "20261020-120001-0001/../../x", "", None, 5):
            with self.subTest(sid=bad):
                with self.assertRaises(StoreError) as ctx:
                    self.store.get_result(bad)
                self.assertEqual(ctx.exception.code, "bad_id")

    def test_not_found(self):
        with self.assertRaises(StoreError) as ctx:
            self.store.get_result("20261020-120001-0001")
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(StoreError):
            self.store.delete_result("20261020-120001-0001")

    def test_delete(self):
        sid = self.save()
        self.store.delete_result(sid)
        self.assertEqual(self.store.list_history(), [])

    def test_list_newest_first_with_a_preview(self):
        first = self.save()
        second = self.save(make_result("Pay rent\nBuy milk\nCall mum\nWater plants"))
        rows = self.store.list_history()
        self.assertEqual([r["sid"] for r in rows], [second, first])
        self.assertEqual(rows[0]["total"], 4)
        self.assertEqual(rows[0]["preview"], ["Pay rent", "Buy milk", "Call mum"])
        self.assertEqual(rows[0]["engine"], "demo")
        self.assertEqual(set(rows[0]["counts"]), {NOW, LATER, EXPIRE, NOTMINE})

    def test_unreadable_history_files_are_skipped(self):
        good = self.save()
        with open(os.path.join(self.store.history_dir, "20261020-130000-abcd.json"), "w") as handle:
            handle.write("{oops")
        with open(os.path.join(self.store.history_dir, "20261020-130001-abcd.json"), "w") as handle:
            handle.write('{"no": "tasks"}')
        with open(os.path.join(self.store.history_dir, "notes.txt"), "w") as handle:
            handle.write("hello")
        self.assertEqual([r["sid"] for r in self.store.list_history()], [good])

    def test_old_lists_are_pruned(self):
        with mock.patch.object(store_module, "MAX_HISTORY", 3):
            sids = [self.save() for _ in range(5)]
        kept = [r["sid"] for r in self.store.list_history()]
        self.assertEqual(kept, sids[:1:-1])  # the three newest

    def test_forget_removes_lists_but_not_settings(self):
        self.store.update_settings({"budget_minutes": 45})
        for _ in range(3):
            self.save()
        self.assertEqual(self.store.forget_history(), 3)
        self.assertEqual(self.store.list_history(), [])
        self.assertEqual(self.store.get_settings()["budget_minutes"], 45)
        self.assertEqual(self.store.forget_history(), 0)

    def test_no_temp_files_are_left_behind(self):
        self.save()
        self.store.update_settings({"budget_minutes": 45})
        leftovers = [n for d in (self.store.dir, self.store.history_dir) for n in os.listdir(d) if n.endswith(".part")]
        self.assertEqual(leftovers, [])


class Editing(StoreCase):
    def test_move_persists_and_survives_a_reload(self):
        sid = self.save()
        learn = self.item(sid, "Learn Rust")["id"]
        self.assertEqual(self.item(sid, "Learn Rust")["bucket"], EXPIRE)
        before = self.store.get_result(sid)
        self.store.move(sid, learn, LATER, TODAY, 7)
        again = Store(self.store.dir).get_result(sid)
        moved = next(i for i in again["tasks"] if i["id"] == learn)
        self.assertEqual((moved["user_bucket"], moved["bucket"]), (LATER, EXPIRE))
        self.assertIsNone(moved["check_on"])
        self.assertIn(learn, again["layout"][LATER])
        self.assertNotIn(learn, again["layout"][EXPIRE])
        self.assertEqual(again["stats"]["counts"][EXPIRE], before["stats"]["counts"][EXPIRE] - 1)

    def test_moving_back_to_the_models_choice_clears_the_override(self):
        sid = self.save()
        learn = self.item(sid, "Learn Rust")["id"]
        self.store.move(sid, learn, LATER, TODAY, 7)
        self.store.move(sid, learn, EXPIRE, TODAY, 7)
        self.assertIsNone(self.item(sid, "Learn Rust")["user_bucket"])
        self.assertTrue(self.item(sid, "Learn Rust")["check_on"])

    def test_skipping_something_plans_a_checkin(self):
        sid = self.save()
        book = self.item(sid, "Book dentist")["id"]
        self.store.move(sid, book, NOTMINE, TODAY, 5)
        self.assertEqual(self.item(sid, "Book dentist")["check_on"], "2026-10-25")

    def test_move_errors(self):
        sid = self.save()
        with self.assertRaises(StoreError) as ctx:
            self.store.move(sid, "t1", "banana", TODAY, 7)
        self.assertEqual(ctx.exception.code, "bad_bucket")
        with self.assertRaises(StoreError) as ctx:
            self.store.move(sid, "t99", LATER, TODAY, 7)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(StoreError) as ctx:
            self.store.move("20250101-000000-ffff", "t1", LATER, TODAY, 7)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_outcomes(self):
        sid = self.save()
        learn = self.item(sid, "Learn Rust")["id"]
        self.store.outcome(sid, learn, "regret")
        self.assertEqual(self.item(sid, "Learn Rust")["outcome"], "regret")
        self.assertTrue(self.item(sid, "Learn Rust")["outcome_at"])
        self.store.outcome(sid, learn, None)
        self.assertIsNone(self.item(sid, "Learn Rust")["outcome"])
        pay = self.item(sid, "Pay electricity")["id"]
        with self.assertRaises(StoreError) as ctx:
            self.store.outcome(sid, pay, "fine")
        self.assertEqual(ctx.exception.code, "not_skipped")
        with self.assertRaises(StoreError) as ctx:
            self.store.outcome(sid, learn, "meh")
        self.assertEqual(ctx.exception.code, "bad_outcome")

    def test_pulling_a_task_back_forgets_its_outcome(self):
        sid = self.save()
        learn = self.item(sid, "Learn Rust")["id"]
        self.store.outcome(sid, learn, "fine")
        self.store.move(sid, learn, NOW, TODAY, 7)
        self.assertIsNone(self.item(sid, "Learn Rust")["outcome"])

    def test_budget_is_changed_without_asking_the_model_again(self):
        sid = self.save(make_result("\n".join("Reply to Anita about item %d" % n for n in range(1, 6)), budget=0))
        self.assertEqual(self.store.get_result(sid)["stats"]["counts"][NOW], 5)
        self.store.set_budget(sid, 10, TODAY, 7)  # five minutes each: two fit
        result = self.store.get_result(sid)
        self.assertEqual((result["budget"], result["stats"]["counts"][NOW], result["stats"]["counts"][LATER]), (10, 2, 3))
        self.store.set_budget(sid, 0, TODAY, 7)
        self.assertEqual(self.store.get_result(sid)["stats"]["counts"][NOW], 5)

    def test_concurrent_edits_do_not_lose_each_other(self):
        sid = self.save(make_result("\n".join("Reorganize photo folder %d" % n for n in range(1, 13))))
        ids = [i["id"] for i in self.store.get_result(sid)["tasks"]]
        errors = []

        def worker(tid):
            try:
                self.store.move(sid, tid, LATER, TODAY, 7)
            except Exception as err:
                errors.append(err)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertTrue(all(i["user_bucket"] == LATER for i in self.store.get_result(sid)["tasks"]))


class Checkins(StoreCase):
    def test_empty(self):
        out = self.store.checkins(TODAY)
        self.assertEqual((out["due"], out["waiting"], out["resolved"]), ([], [], []))
        self.assertIsNone(out["stats"]["regret_rate"])
        self.assertEqual(out["stats"]["lists"], 0)

    def test_due_waiting_and_resolved(self):
        sid = self.save(make_result("Learn Rust someday\nWaiting for the landlord\nWatch the webinar\nPay rent"))
        items = {i["text"]: i["id"] for i in self.store.get_result(sid)["tasks"]}
        # check-in dates: make one overdue, one due today, leave one in the future
        path = os.path.join(self.store.history_dir, sid + ".json")
        data = read_json(path)
        for task in data["tasks"]:
            if task["text"].startswith("Learn"):
                task["check_on"] = "2026-10-18"
            if task["text"].startswith("Waiting"):
                task["check_on"] = "2026-10-20"
        write_json(path, data)
        out = self.store.checkins(TODAY)
        self.assertEqual([r["text"] for r in out["due"]], ["Learn Rust someday", "Waiting for the landlord"])
        self.assertEqual([r["days_over"] for r in out["due"]], [2, 0])
        self.assertEqual([r["text"] for r in out["waiting"]], ["Watch the webinar"])
        self.assertEqual(out["stats"]["skipped"], 3)
        self.assertNotIn("Pay rent", [r["text"] for r in out["due"] + out["waiting"]])

        self.store.outcome(sid, items["Learn Rust someday"], "fine")
        self.store.outcome(sid, items["Waiting for the landlord"], "regret")
        out = self.store.checkins(TODAY)
        self.assertEqual(len(out["due"]), 0)
        self.assertEqual(len(out["resolved"]), 2)
        self.assertEqual((out["stats"]["resolved"], out["stats"]["regrets"], out["stats"]["regret_rate"]), (2, 1, 0.5))

    def test_a_missing_or_broken_check_on_date_is_treated_as_tomorrow(self):
        sid = self.save(make_result("Learn Rust someday"))
        path = os.path.join(self.store.history_dir, sid + ".json")
        data = read_json(path)
        data["tasks"][0]["check_on"] = "not a date"
        write_json(path, data)
        self.assertEqual(len(self.store.checkins(TODAY)["waiting"]), 1)

    def test_overrides_count_as_the_displayed_bucket(self):
        sid = self.save(make_result("Learn Rust someday\nPay rent"))
        items = {i["text"]: i["id"] for i in self.store.get_result(sid)["tasks"]}
        self.store.move(sid, items["Learn Rust someday"], NOW, TODAY, 7)
        self.assertEqual(self.store.checkins(TODAY)["stats"]["skipped"], 0)


class Lessons(StoreCase):
    def test_nothing_to_learn_from_an_untouched_list(self):
        self.save()
        self.assertEqual(self.store.lessons(), [])

    def test_regret_and_pulled_back_and_prefer_to_skip(self):
        sid = self.save()
        learn = self.item(sid, "Learn Rust")["id"]
        tap = self.item(sid, "Waiting for the landlord")["id"]
        book = self.item(sid, "Book dentist")["id"]
        self.store.outcome(sid, learn, "regret")
        self.store.move(sid, tap, LATER, TODAY, 7)
        self.store.move(sid, book, EXPIRE, TODAY, 7)
        lessons = {entry["text"]: entry for entry in self.store.lessons()}
        self.assertEqual(lessons["Learn Rust someday"]["should_be"], LATER)
        self.assertIn("regretted", lessons["Learn Rust someday"]["reason"])
        self.assertEqual(lessons["Waiting for the landlord to fix the tap"]["should_be"], LATER)
        self.assertIn("pulled it back", lessons["Waiting for the landlord to fix the tap"]["reason"])
        self.assertEqual(lessons["Book dentist for a check-up"]["should_be"], EXPIRE)
        self.assertIn("prefer to skip", lessons["Book dentist for a check-up"]["reason"])

    def test_newest_list_wins_for_the_same_task_and_texts_are_not_repeated(self):
        old = self.save(make_result("Learn Rust someday"))
        new = self.save(make_result("learn rust someday!"))
        self.store.outcome(old, "t1", "regret")
        self.store.move(new, "t1", NOW, TODAY, 7)
        lessons = self.store.lessons()
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["should_be"], NOW)

    def test_limit_and_balance(self):
        text = "\n".join("Learn thing %d someday" % n for n in range(1, 11))
        sid = self.save(make_result(text))
        for n in range(1, 9):
            self.store.outcome(sid, "t%d" % n, "regret")
        for n in (9, 10):
            self.store.move(sid, "t%d" % n, NOW, TODAY, 7)
        # t9, t10 are "pulled back"; make two "prefer to skip" from a kept list too
        keep = self.save(make_result("Book dentist\nBook optician\nBook plumber"))
        self.store.move(keep, "t1", EXPIRE, TODAY, 7)
        self.store.move(keep, "t2", EXPIRE, TODAY, 7)
        self.store.move(keep, "t3", EXPIRE, TODAY, 7)
        lessons = self.store.lessons(limit=6)
        self.assertEqual(len(lessons), 6)
        self.assertEqual(sum(1 for entry in lessons if entry["should_be"] == EXPIRE), 2)
        self.assertEqual(len(self.store.lessons(limit=3)), 3)


if __name__ == "__main__":
    unittest.main()
