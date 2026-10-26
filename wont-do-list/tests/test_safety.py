import unittest

from wontdo.constants import EXPIRE, LATER, NOTMINE, NOW
from wontdo.safety import reconcile, scan


def rules(text, never=()):
    return [hit["rule"] for hit in scan(text, never)]


class Tripwires(unittest.TestCase):
    def assertHit(self, text, rule):
        self.assertIn(rule, rules(text), text)

    def assertClear(self, text):
        self.assertEqual(rules(text), [], text)

    def test_money(self):
        for text in ("Pay electricity bill by Friday", "file income tax return", "rent due on the 1st", "pay Priya back",
                     "EMI reminder", "send ₹500 to Raj", "refund from Amazon", "credit card statement", "bijli bill bharna",
                     "settle up with Sam", "invoice the client"):
            self.assertHit(text, "money")

    def test_money_false_alarms_stay_quiet(self):
        for text in ("Call Bill about the deck", "Fine-tune the model", "pay attention in meetings", "rental car idea"):
            self.assertClear(text)

    def test_legal(self):
        for text in ("renew passport", "driving licence renewal", "talk to the lawyer", "sign the lease agreement",
                     "visa interview prep", "register the company", "file an FIR"):
            self.assertHit(text, "legal")

    def test_health(self):
        for text in ("Dentist appointment", "refill prescription", "book doctor visit", "get blood test results",
                     "take the pills", "Dr. Mehta checkup", "dawai lena", "vaccination for kids"):
            self.assertHit(text, "health")

    def test_health_false_alarms_stay_quiet(self):
        for text in ("buy a pillow", "update the doc", "learn rust someday"):
            self.assertClear(text)

    def test_safety(self):
        for text in ("fix water leak in kitchen", "smoke alarm batteries", "replace brake pads", "check tire pressure",
                     "carbon monoxide detector"):
            self.assertHit(text, "safety")

    def test_commitments(self):
        for text in ("confirm flight booking", "submit application", "exam preparation", "reservation for Friday",
                     "interview with Acme"):
            self.assertHit(text, "commitment")

    def test_people(self):
        for text in ("buy gift for mom's birthday", "anniversary dinner", "school fees", "pick up the kids", "attend the wedding"):
            self.assertHit(text, "people")

    def test_authority(self):
        for text in ("reply to my boss", "send the client a quote", "email professor about thesis", "update the investor deck"):
            self.assertHit(text, "authority")

    def test_ordinary_wishes_are_not_protected(self):
        for text in ("Learn Rust someday", "Reorganize photo folders", "Reply to the webinar invite", "Watch that documentary",
                     "Try the new ramen place", "Fix memory leak in cache"):
            self.assertClear(text)

    def test_multiple_rules_can_hit(self):
        self.assertEqual(set(rules("pay the doctor's invoice")), {"money", "health"})

    def test_hit_shape(self):
        hit = scan("pay rent")[0]
        self.assertEqual(set(hit), {"rule", "label", "match", "note"})
        self.assertEqual(hit["rule"], "money")


class NeverSkip(unittest.TestCase):
    def test_your_words_are_protected(self):
        self.assertEqual(rules("call mom about the garden", ["mom"]), ["yours"])

    def test_whole_words_only_and_case_blind(self):
        self.assertEqual(rules("Mommy issues", ["mom"]), [])
        self.assertEqual(rules("Call MOM", ["mom"]), ["yours"])

    def test_short_and_blank_words_are_ignored(self):
        self.assertEqual(rules("a b c", ["a", "", " "]), [])

    def test_regex_characters_are_literal(self):
        self.assertIn("yours", rules("learn c++ someday", ["c++"]))
        self.assertEqual(rules("learn cpp someday", ["c++"]), [])


class Reconcile(unittest.TestCase):
    def test_protected_tasks_cannot_be_skipped(self):
        for skip in (EXPIRE, NOTMINE):
            rec = reconcile(skip, "none", scan("pay rent"), None)
            self.assertEqual(rec.bucket, LATER)
            self.assertEqual(rec.flags[0]["kind"], "safety")
            self.assertEqual(rec.flags[0]["was"], skip)
            self.assertFalse(rec.must_do)

    def test_unprotected_skips_stand(self):
        for skip in (EXPIRE, NOTMINE):
            rec = reconcile(skip, "none", [], None)
            self.assertEqual((rec.bucket, rec.flags), (skip, []))

    def test_risky_tasks_cannot_be_skipped(self):
        for risk in ("medium", "high"):
            rec = reconcile(EXPIRE, risk, [], None)
            self.assertEqual(rec.bucket, LATER)
            self.assertEqual(rec.flags[0]["kind"], "consistency")
        self.assertEqual(reconcile(EXPIRE, "low", [], None).bucket, EXPIRE)

    def test_protected_and_due_soon_goes_to_now_and_is_must_do(self):
        for days in (2, 1, 0, -3):
            rec = reconcile(LATER, "low", scan("pay rent"), days)
            self.assertEqual((rec.bucket, rec.must_do), (NOW, True), days)
        self.assertEqual(rec.flags[-1]["kind"], "due")

    def test_protected_but_not_soon_stays_later(self):
        rec = reconcile(LATER, "low", scan("pay rent"), 5)
        self.assertEqual((rec.bucket, rec.must_do, rec.flags), (LATER, False, []))

    def test_expired_protected_task_due_soon_jumps_two_levels(self):
        rec = reconcile(EXPIRE, "none", scan("pay rent"), 1)
        self.assertEqual(rec.bucket, NOW)
        self.assertEqual([f["kind"] for f in rec.flags], ["safety", "due"])

    def test_high_risk_due_soon_is_must_do(self):
        rec = reconcile(LATER, "high", [], 1)
        self.assertEqual((rec.bucket, rec.must_do), (NOW, True))

    def test_urgent_word_counts_as_soon_for_protected_tasks_only(self):
        self.assertEqual(reconcile(LATER, "low", scan("pay rent"), None, urgent=True).bucket, NOW)
        self.assertEqual(reconcile(LATER, "low", [], None, urgent=True).bucket, LATER)

    def test_unprotected_due_soon_low_risk_is_left_alone(self):
        rec = reconcile(LATER, "low", [], 1)
        self.assertEqual((rec.bucket, rec.must_do), (LATER, False))

    def test_now_stays_now_and_gets_must_do_only_when_it_matters(self):
        self.assertTrue(reconcile(NOW, "low", scan("pay rent"), 0).must_do)
        self.assertFalse(reconcile(NOW, "low", [], 0).must_do)

    def test_the_model_can_never_end_up_below_where_the_rules_put_it(self):
        for model in (NOW, LATER, EXPIRE, NOTMINE):
            for risk in ("none", "low", "medium", "high"):
                rec = reconcile(model, risk, scan("pay rent"), None)
                self.assertIn(rec.bucket, (NOW, LATER), (model, risk))


if __name__ == "__main__":
    unittest.main()
