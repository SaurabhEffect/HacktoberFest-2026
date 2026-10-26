import datetime as dt
import unittest

from wontdo.dates import add_months, describe_days, find_due, find_signals

TODAY = dt.date(2026, 10, 20)  # a Tuesday


def due(text, today=TODAY, order="dmy"):
    return find_due(text, today, order)


class RelativeDates(unittest.TestCase):
    def test_today_words(self):
        for text in ("send it today", "finish tonight", "invoice by EOD", "reply by end of day", "this evening"):
            self.assertEqual(due(text).date, TODAY, text)

    def test_tomorrow(self):
        self.assertEqual(due("call mom tomorrow").date, dt.date(2026, 10, 21))
        self.assertEqual(due("ship tmrw").days, 1)

    def test_yesterday_is_overdue(self):
        self.assertEqual(due("missed it yesterday").days, -1)

    def test_in_n_units(self):
        self.assertEqual(due("submit form in 3 days").date, dt.date(2026, 10, 23))
        self.assertEqual(due("call back in two weeks").date, dt.date(2026, 11, 3))
        self.assertEqual(due("renew in a week").date, dt.date(2026, 10, 27))
        self.assertEqual(due("in a couple of days").date, dt.date(2026, 10, 22))
        self.assertEqual(due("in 2 hours").date, TODAY)

    def test_in_months_is_fuzzy(self):
        found = due("book flights in 2 months")
        self.assertTrue(found.fuzzy)
        self.assertEqual(found.date, dt.date(2026, 12, 20))

    def test_next_week_is_next_monday_and_fuzzy(self):
        found = due("plan trip next week")
        self.assertEqual(found.date, dt.date(2026, 10, 26))
        self.assertTrue(found.fuzzy)

    def test_this_week_is_friday(self):
        self.assertEqual(due("wrap up this week").date, dt.date(2026, 10, 23))
        saturday = dt.date(2026, 10, 24)
        self.assertEqual(due("wrap up this week", saturday).date, saturday)

    def test_weekend(self):
        self.assertEqual(due("laundry by the weekend").date, dt.date(2026, 10, 24))

    def test_month_end_and_year_end(self):
        self.assertEqual(due("close books by end of month").date, dt.date(2026, 10, 31))
        self.assertEqual(due("plan by end of year").date, dt.date(2026, 12, 31))
        self.assertEqual(due("start next month").date, dt.date(2026, 11, 1))


class Weekdays(unittest.TestCase):
    def test_by_weekday_is_next_occurrence(self):
        self.assertEqual(due("Pay electricity bill by Friday").date, dt.date(2026, 10, 23))
        self.assertEqual(due("do it on Monday").date, dt.date(2026, 10, 26))

    def test_same_weekday_means_today(self):
        self.assertEqual(due("report by Tuesday").date, TODAY)

    def test_next_weekday_is_following_week(self):
        self.assertEqual(due("finish report next Friday").date, dt.date(2026, 10, 30))
        self.assertEqual(due("sync next Monday").date, dt.date(2026, 10, 26))
        self.assertEqual(due("sync next Tuesday").date, dt.date(2026, 10, 27))

    def test_abbreviations_need_a_preposition(self):
        self.assertEqual(due("do it by sat").date, dt.date(2026, 10, 24))
        self.assertIsNone(due("I sat down"))
        self.assertIsNone(due("the sun was out"))

    def test_recurring_and_past_references_are_ignored(self):
        self.assertIsNone(due("standup every Monday"))
        self.assertIsNone(due("reply to the mail from last Friday"))
        self.assertIsNone(due("fridays are for planning"))


class WrittenDates(unittest.TestCase):
    def test_month_day_forms(self):
        self.assertEqual(due("tax filing oct 31").date, dt.date(2026, 10, 31))
        self.assertEqual(due("dentist on 5 nov").date, dt.date(2026, 11, 5))
        self.assertEqual(due("party November 12th").date, dt.date(2026, 11, 12))
        self.assertEqual(due("trip 3rd of December").date, dt.date(2026, 12, 3))
        self.assertEqual(due("Oct 25, 2026 deadline").date, dt.date(2026, 10, 25))

    def test_iso(self):
        self.assertEqual(due("ship it 2026-11-02").date, dt.date(2026, 11, 2))

    def test_year_rolls_forward_when_far_in_the_past(self):
        self.assertEqual(due("party jan 5").date, dt.date(2027, 1, 5))

    def test_recent_past_stays_this_year_and_is_overdue(self):
        found = due("rsvp oct 15")
        self.assertEqual(found.date, dt.date(2026, 10, 15))
        self.assertEqual(found.days, -5)

    def test_leap_day(self):
        self.assertEqual(due("renewal feb 29", dt.date(2027, 6, 1)).date, dt.date(2028, 2, 29))

    def test_impossible_dates_are_ignored(self):
        self.assertIsNone(due("meet on feb 30"))
        self.assertIsNone(due("meet on 32 oct"))

    def test_may_is_only_a_date_with_a_number(self):
        self.assertIsNone(due("you may want to call"))
        self.assertEqual(due("conference may 5").date, dt.date(2027, 5, 5))


class NumericDates(unittest.TestCase):
    def test_day_first_by_default(self):
        self.assertEqual(due("renew passport by 25/10").date, dt.date(2026, 10, 25))
        self.assertEqual(due("pay by 5/11").date, dt.date(2026, 11, 5))

    def test_month_first_when_asked(self):
        # 5/11 month-first is 11 May, which is long past in October, so it rolls to next year.
        self.assertEqual(due("pay by 5/11", order="mdy").date, dt.date(2027, 5, 11))
        self.assertEqual(due("pay by 10/25", order="mdy").date, dt.date(2026, 10, 25))

    def test_unambiguous_wins_over_the_setting(self):
        self.assertEqual(due("pay by 10/25").date, dt.date(2026, 10, 25))  # dmy fails, mdy works
        self.assertEqual(due("pay by 25/10", order="mdy").date, dt.date(2026, 10, 25))

    def test_with_year(self):
        self.assertEqual(due("sign by 05/12/2026").date, dt.date(2026, 12, 5))
        self.assertEqual(due("sign by 05-12-2026").date, dt.date(2026, 12, 5))

    def test_numbers_that_are_not_dates(self):
        for text in ("buy 3/4 inch bolts", "open 24/7 shop visit", "read for 1-2 hours", "version 1.2 release",
                     "walk 3.5 km", "score 10/10"):
            self.assertIsNone(due(text), text)


class Choosing(unittest.TestCase):
    def test_deadline_word_beats_position(self):
        found = due("tomorrow I will plan the report due by friday")
        self.assertEqual(found.date, dt.date(2026, 10, 23))

    def test_first_date_wins_without_cues(self):
        self.assertEqual(due("oct 25 or oct 28").date, dt.date(2026, 10, 25))

    def test_none_when_nothing_found(self):
        self.assertIsNone(due("learn rust someday"))
        self.assertIsNone(due(""))

    def test_days_field(self):
        self.assertEqual(due("by friday").days, 3)


class Signals(unittest.TestCase):
    def test_urgent_and_overdue(self):
        self.assertTrue(find_signals("reply ASAP")["urgent"])
        self.assertTrue(find_signals("this is urgent")["urgent"])
        self.assertTrue(find_signals("bill is overdue")["overdue"])
        self.assertFalse(find_signals("water the plants")["urgent"])


class Helpers(unittest.TestCase):
    def test_describe_days(self):
        self.assertEqual(describe_days(0), "today")
        self.assertEqual(describe_days(1), "tomorrow")
        self.assertEqual(describe_days(3), "in 3 days")
        self.assertEqual(describe_days(-1), "yesterday")
        self.assertEqual(describe_days(-4), "4 days ago")

    def test_add_months_clamps_day(self):
        self.assertEqual(add_months(dt.date(2026, 1, 31), 1), dt.date(2026, 2, 28))
        self.assertEqual(add_months(dt.date(2026, 11, 30), 3), dt.date(2027, 2, 28))
        self.assertEqual(add_months(dt.date(2026, 12, 15), 1), dt.date(2027, 1, 15))


if __name__ == "__main__":
    unittest.main()
