import copy
import datetime as dt
import unittest

from wontdo import demo
from wontdo.constants import EXPIRE, LATER, NOTMINE, NOW
from wontdo.dates import find_due, find_signals
from wontdo.result import (
    ResultError,
    apply_budget,
    compute_stats,
    display_bucket,
    fmt_minutes,
    mark_outcome,
    move_task,
    plan_checkin,
    rebudget,
    refresh,
)

TODAY = dt.date(2026, 10, 20)


def verdict(text):
    return demo.judge_one(text, find_due(text, TODAY), find_signals(text))


class DemoBrain(unittest.TestCase):
    def bucket(self, text):
        return verdict(text)["bucket"]

    def test_wishes_expire(self):
        for text in ("Learn Rust someday", "Reorganize photo folders", "Reply to the webinar invite", "maybe try pottery"):
            self.assertEqual(self.bucket(text), EXPIRE, text)

    def test_handoffs(self):
        for text in ("Waiting for the landlord to fix the tap", "the plumber to send me a quote", "Delegate the slides"):
            self.assertEqual(self.bucket(text), NOTMINE, text)

    def test_deadlines_pull_things_forward(self):
        self.assertEqual(self.bucket("Pay electricity bill by tomorrow"), NOW)
        self.assertEqual(self.bucket("Finish slides by friday"), LATER)
        self.assertEqual(verdict("Finish slides by friday")["risk"], "medium")

    def test_a_wish_with_a_close_deadline_is_not_a_wish(self):
        self.assertEqual(self.bucket("learn the routine by tomorrow"), NOW)

    def test_urgent_and_quick_wins(self):
        self.assertEqual(self.bucket("send the form asap"), NOW)
        self.assertEqual(self.bucket("Reply to Anita"), NOW)

    def test_default_is_later(self):
        self.assertEqual(self.bucket("Fix the squeaky door"), LATER)

    def test_shape_and_minutes(self):
        v = verdict("Write the quarterly report")
        self.assertEqual(set(v), {"bucket", "risk", "minutes", "why", "if_ignored"})
        self.assertIn(v["minutes"], (5, 10, 15, 30, 60, 120, 240))
        self.assertEqual(demo.estimate_minutes("Reply to Anita"), 5)
        self.assertEqual(demo.estimate_minutes("Learn Spanish"), 240)
        self.assertEqual(demo.estimate_minutes("water the plants"), 15)

    def test_deterministic(self):
        self.assertEqual(verdict("maybe learn guitar"), verdict("maybe learn guitar"))

    def test_batch_keys_are_positions(self):
        entries = [{"text": "a"}, {"text": "b"}]
        self.assertEqual(sorted(demo.judge_demo(entries)), [1, 2])


def item(n, bucket=LATER, minutes=15, risk="low", due_days=None, must=False, user=None, flags=None):
    return {
        "id": "t%d" % n, "text": "task %d" % n, "group": "", "bucket": bucket, "bucket_pre_budget": bucket,
        "model_bucket": bucket, "user_bucket": user, "why": "", "if_ignored": "", "minutes": minutes, "risk": risk,
        "due": None if due_days is None else {"date": (TODAY + dt.timedelta(days=due_days)).isoformat(), "phrase": "", "fuzzy": False, "days": due_days},
        "must_do": must, "flags": flags or [], "source": "model", "check_on": None, "outcome": None,
    }


def result_of(*items, budget=0):
    return refresh({"today": TODAY.isoformat(), "budget": budget, "tasks": list(items)})


class Budget(unittest.TestCase):
    def test_fills_in_priority_order_and_demotes_the_rest(self):
        items = [item(1, NOW, 30, due_days=5), item(2, NOW, 30, due_days=1), item(3, NOW, 30, due_days=3)]
        apply_budget(items, 60)
        self.assertEqual([i["bucket"] for i in items], [LATER, NOW, NOW])
        self.assertEqual(items[0]["flags"][0]["kind"], "budget")

    def test_zero_budget_means_no_limit(self):
        items = [item(n, NOW, 240) for n in range(1, 6)]
        apply_budget(items, 0)
        self.assertTrue(all(i["bucket"] == NOW for i in items))

    def test_must_do_is_never_demoted_even_over_budget(self):
        items = [item(1, NOW, 120, must=True), item(2, NOW, 15)]
        apply_budget(items, 30)
        self.assertEqual([i["bucket"] for i in items], [NOW, LATER])

    def test_must_do_time_counts_against_the_budget(self):
        items = [item(1, NOW, 20, must=True), item(2, NOW, 15), item(3, NOW, 15)]
        apply_budget(items, 40)
        self.assertEqual([i["bucket"] for i in items], [NOW, NOW, LATER])

    def test_user_pinned_items_are_kept_and_counted(self):
        items = [item(1, LATER, 30, user=NOW), item(2, NOW, 30)]
        apply_budget(items, 40)
        self.assertEqual(items[1]["bucket"], LATER)
        self.assertEqual(display_bucket(items[0]), NOW)

    def test_other_buckets_are_untouched(self):
        items = [item(1, EXPIRE, 240), item(2, NOTMINE, 240)]
        apply_budget(items, 5)
        self.assertEqual([i["bucket"] for i in items], [EXPIRE, NOTMINE])

    def test_idempotent_and_reversible(self):
        items = [item(1, NOW, 30), item(2, NOW, 30), item(3, NOW, 30)]
        apply_budget(items, 30)
        once = copy.deepcopy(items)
        apply_budget(items, 30)
        self.assertEqual(items, once)
        apply_budget(items, 90)
        self.assertTrue(all(i["bucket"] == NOW for i in items))
        self.assertTrue(all(i["flags"] == [] for i in items))

    def test_overdue_comes_first(self):
        items = [item(1, NOW, 30, due_days=1), item(2, NOW, 30, due_days=-2)]
        apply_budget(items, 30)
        self.assertEqual([i["bucket"] for i in items], [LATER, NOW])

    def test_higher_risk_breaks_ties(self):
        items = [item(1, NOW, 30, risk="low"), item(2, NOW, 30, risk="high")]
        apply_budget(items, 30)
        self.assertEqual([i["bucket"] for i in items], [LATER, NOW])

    def test_fmt_minutes(self):
        self.assertEqual([fmt_minutes(m) for m in (5, 60, 90, 180, 405)], ["5 min", "1 h", "1 h 30 min", "3 h", "6 h 45 min"])


class LayoutAndStats(unittest.TestCase):
    def test_layout_orders_each_bucket(self):
        r = result_of(item(1, NOW, 30, due_days=3), item(2, NOW, 30, due_days=1), item(3, NOW, 30, must=True, due_days=2),
                      item(4, LATER, due_days=None), item(5, LATER, due_days=2), item(6, EXPIRE), item(7, NOTMINE))
        self.assertEqual(r["layout"][NOW], ["t3", "t2", "t1"])
        self.assertEqual(r["layout"][LATER], ["t5", "t4"])
        self.assertEqual(r["layout"][EXPIRE], ["t6"])
        self.assertEqual(r["layout"][NOTMINE], ["t7"])

    def test_layout_covers_every_task_exactly_once(self):
        r = result_of(*[item(n, b) for n, b in enumerate((NOW, LATER, EXPIRE, NOTMINE, LATER, NOW), 1)])
        placed = sum(r["layout"].values(), [])
        self.assertEqual(sorted(placed), sorted(i["id"] for i in r["tasks"]))

    def test_stats(self):
        flag = {"kind": "safety"}
        r = result_of(item(1, NOW, 10), item(2, LATER, 60), item(3, EXPIRE, 120, flags=[flag]), item(4, NOTMINE, 30))
        s = r["stats"]
        self.assertEqual((s["total"], s["kept"], s["skipped"], s["minutes_skipped"], s["rescued"]), (4, 2, 2, 150, 1))
        self.assertEqual(s["counts"], {NOW: 1, LATER: 1, EXPIRE: 1, NOTMINE: 1})
        self.assertEqual(s["minutes"][NOW], 10)

    def test_stats_use_the_persons_override(self):
        r = result_of(item(1, EXPIRE, 30, user=NOW))
        self.assertEqual(compute_stats(r["tasks"])["counts"][NOW], 1)


class Overrides(unittest.TestCase):
    def test_moving_a_skipped_task_up_clears_the_checkin(self):
        r = result_of(item(1, EXPIRE))
        r["tasks"][0]["check_on"] = "2026-10-27"
        move_task(r, "t1", NOW, TODAY, 7)
        t = r["tasks"][0]
        self.assertEqual((display_bucket(t), t["user_bucket"], t["check_on"], t["outcome"]), (NOW, NOW, None, None))

    def test_moving_a_kept_task_down_schedules_a_checkin(self):
        r = result_of(item(1, LATER))
        move_task(r, "t1", EXPIRE, TODAY, 7)
        self.assertEqual(r["tasks"][0]["check_on"], "2026-10-27")
        self.assertEqual(r["layout"][EXPIRE], ["t1"])

    def test_choosing_the_engines_own_bucket_removes_the_override(self):
        r = result_of(item(1, LATER))
        move_task(r, "t1", EXPIRE, TODAY, 7)
        move_task(r, "t1", LATER, TODAY, 7)
        self.assertIsNone(r["tasks"][0]["user_bucket"])
        self.assertIsNone(r["tasks"][0]["check_on"])

    def test_none_clears_the_override(self):
        r = result_of(item(1, LATER))
        move_task(r, "t1", NOW, TODAY, 7)
        move_task(r, "t1", None, TODAY, 7)
        self.assertEqual(display_bucket(r["tasks"][0]), LATER)

    def test_moving_between_skip_buckets_keeps_the_checkin_and_outcome(self):
        r = result_of(item(1, EXPIRE))
        r["tasks"][0]["check_on"] = "2026-10-25"
        mark_outcome(r, "t1", "fine")
        move_task(r, "t1", NOTMINE, TODAY, 7)
        self.assertEqual((r["tasks"][0]["check_on"], r["tasks"][0]["outcome"]), ("2026-10-25", "fine"))

    def test_bad_input(self):
        r = result_of(item(1, LATER))
        with self.assertRaises(ResultError):
            move_task(r, "t1", "banana", TODAY, 7)
        with self.assertRaises(ResultError):
            move_task(r, "t99", NOW, TODAY, 7)

    def test_outcomes(self):
        r = result_of(item(1, EXPIRE), item(2, LATER))
        mark_outcome(r, "t1", "regret")
        self.assertEqual(r["tasks"][0]["outcome"], "regret")
        self.assertTrue(r["tasks"][0]["outcome_at"])
        mark_outcome(r, "t1", None)
        self.assertNotIn("outcome_at", r["tasks"][0])
        with self.assertRaises(ResultError) as ctx:
            mark_outcome(r, "t2", "fine")
        self.assertEqual(ctx.exception.code, "not_skipped")
        with self.assertRaises(ResultError):
            mark_outcome(r, "t1", "meh")

    def test_rebudget_reflows_and_keeps_overrides(self):
        r = result_of(item(1, NOW, 30), item(2, NOW, 30), item(3, LATER, 30, user=NOW))
        rebudget(r, 60, TODAY, 7)
        # t3 is pinned (30 min), t1 fits beside it, t2 does not: ties go to the earlier task.
        self.assertEqual([display_bucket(i) for i in r["tasks"]], [NOW, LATER, NOW])
        rebudget(r, 0, TODAY, 7)
        self.assertEqual([display_bucket(i) for i in r["tasks"]], [NOW, NOW, NOW])
        self.assertEqual(r["budget"], 0)


class Checkins(unittest.TestCase):
    def test_default_is_n_days_out(self):
        self.assertEqual(plan_checkin(item(1, EXPIRE), TODAY, 7), "2026-10-27")
        self.assertEqual(plan_checkin(item(1, EXPIRE), TODAY, 3), "2026-10-23")

    def test_dated_task_checks_the_day_after_the_deadline(self):
        self.assertEqual(plan_checkin(item(1, EXPIRE, due_days=5), TODAY, 7), "2026-10-26")

    def test_far_past_or_fuzzy_dates_fall_back_to_the_default(self):
        self.assertEqual(plan_checkin(item(1, EXPIRE, due_days=90), TODAY, 7), "2026-10-27")
        self.assertEqual(plan_checkin(item(1, EXPIRE, due_days=-3), TODAY, 7), "2026-10-27")
        fuzzy = item(1, EXPIRE, due_days=4)
        fuzzy["due"]["fuzzy"] = True
        self.assertEqual(plan_checkin(fuzzy, TODAY, 7), "2026-10-27")

    def test_kept_tasks_have_none(self):
        self.assertIsNone(plan_checkin(item(1, NOW), TODAY, 7))


if __name__ == "__main__":
    unittest.main()
