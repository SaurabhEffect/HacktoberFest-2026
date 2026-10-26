import csv
import datetime as dt
import io
import json
import unittest

from wontdo.constants import EXPIRE, NOW
from wontdo.exporters import FORMATS, export, to_csv, to_ics, to_json, to_markdown
from wontdo.judge import SortOptions, sort_tasks
from wontdo.result import move_task

TODAY = dt.date(2026, 10, 20)
STAMP = dt.datetime(2026, 10, 20, 9, 30, 0, tzinfo=dt.timezone.utc)

LIST = """Pay electricity bill by tomorrow
Learn Rust someday
Waiting for the landlord to fix the tap
Reply to the webinar invite
Book dentist for a check-up"""


def make_result(text=LIST, budget=90):
    result = sort_tasks(text, None, SortOptions(today=TODAY, budget=budget, engine="demo"))
    result["sid"] = "20261020-093000-abcd"
    return result


class Markdown(unittest.TestCase):
    def test_structure(self):
        md = to_markdown(make_result())
        lines = md.splitlines()
        self.assertEqual(lines[0], "# The Won't-Do List — 20 Oct 2026")
        self.assertIn("5 tasks in.", md)
        self.assertIn("keyword rules (demo brain, no AI model)", md)
        headings = [line for line in lines if line.startswith("## ")]
        self.assertEqual([h.split(" (")[0] for h in headings], ["## Do now", "## Do later", "## Let it expire", "## Not my problem"])
        self.assertIn("- [ ] Pay electricity bill by tomorrow — ~10 min, due Wed 21 Oct", md)
        self.assertTrue(any(line.startswith("- Learn Rust someday — ~") and "check back 2026-10-27" in line for line in lines))
        self.assertIn("  - If ignored:", md)
        self.assertTrue(md.endswith("\n") and not md.endswith("\n\n"))

    def test_empty_buckets_are_left_out(self):
        md = to_markdown(make_result("Pay rent"))
        self.assertNotIn("Let it expire", md)
        self.assertNotIn("Not my problem", md)

    def test_safety_flags_are_shown(self):
        md = to_markdown(make_result("Pay rent someday maybe"))
        self.assertIn("  - Safety net: money", md.splitlines())

    def test_engine_labels(self):
        result = make_result()
        result["engine"] = {"kind": "model", "provider": "ollama", "model": "gemma3:4b", "base_url": ""}
        self.assertIn("Sorted by gemma3:4b via ollama.", to_markdown(result))
        result["engine"]["kind"] = "mixed"
        self.assertIn("with keyword rules for some tasks", to_markdown(result))

    def test_user_moves_are_reflected(self):
        result = make_result()
        move_task(result, "t2", NOW, TODAY, 7)
        md = to_markdown(result)
        self.assertIn("- [ ] Learn Rust someday", md)


class Csv(unittest.TestCase):
    def rows(self, result):
        return list(csv.reader(io.StringIO(to_csv(result), newline="")))

    def test_header_and_rows(self):
        rows = self.rows(make_result())
        self.assertEqual(rows[0], ["bucket", "task", "minutes", "risk", "due", "why", "if_ignored", "safety_net", "check_on", "outcome"])
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[1][:2], ["Do now", "Pay electricity bill by tomorrow"])
        self.assertEqual({r[0] for r in rows[1:]}, {"Do now", "Do later", "Let it expire", "Not my problem"})
        self.assertTrue(all(len(r) == 10 for r in rows))

    def test_crlf_and_special_characters_survive(self):
        result = make_result('Email "Anita", then Raj; then call home\nNotes\twith tab')
        rows = self.rows(result)
        self.assertEqual(rows[1][1], 'Email "Anita", then Raj; then call home')
        self.assertTrue(to_csv(result).endswith("\r\n"))

    def test_formula_injection_is_defused(self):
        result = make_result("=HYPERLINK(\"http://evil\")\n+1 call\n-2 things\n@sum(A1)")
        texts = [r[1] for r in self.rows(result)[1:]]
        self.assertTrue(all(t.startswith("'") for t in texts), texts)

    def test_outcome_and_check_on_columns(self):
        result = make_result("Learn Rust someday")
        result["tasks"][0]["outcome"] = "regret"
        row = self.rows(result)[1]
        self.assertEqual((row[8], row[9]), ("2026-10-27", "regret"))


class Json(unittest.TestCase):
    def test_round_trip(self):
        result = make_result()
        self.assertEqual(json.loads(to_json(result)), result)


class Ics(unittest.TestCase):
    def events(self, text):
        events, current = [], None
        for line in text.split("\r\n"):
            if line == "BEGIN:VEVENT":
                current = {}
            elif line == "END:VEVENT":
                events.append(current)
                current = None
            elif current is not None and ":" in line and not line.startswith(" "):
                key, _, value = line.partition(":")
                current[key] = value
        return events

    def test_structure(self):
        text = to_ics(make_result(), now=STAMP)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n", text.replace("\r\n", ""))
        for physical in text.split("\r\n"):
            self.assertLessEqual(len(physical.encode("utf-8")), 75)

    def test_events_only_for_kept_tasks_with_a_day(self):
        events = self.events(to_ics(make_result(), now=STAMP))
        summaries = [e["SUMMARY"] for e in events]
        self.assertIn("Pay electricity bill by tomorrow", summaries)
        self.assertNotIn("Learn Rust someday", summaries)
        pay = next(e for e in events if e["SUMMARY"].startswith("Pay electricity"))
        self.assertEqual(pay["DTSTART;VALUE=DATE"], "20261021")
        self.assertEqual(pay["DTEND;VALUE=DATE"], "20261022")
        self.assertEqual(pay["DTSTAMP"], "20261020T093000Z")
        self.assertEqual(pay["UID"], "20261020-093000-abcd-t1@wontdo.local")
        self.assertEqual(pay["CATEGORIES"], "Do now")

    def test_a_do_later_task_with_a_date_gets_an_event_on_that_day(self):
        result = make_result("Book the dentist by Friday")
        self.assertEqual(result["tasks"][0]["bucket"], "later")
        events = self.events(to_ics(result, now=STAMP))
        self.assertEqual((events[0]["DTSTART;VALUE=DATE"], events[0]["CATEGORIES"]), ("20261023", "Do later"))

    def test_undated_do_now_lands_on_the_sort_day_and_undated_do_later_is_left_out(self):
        result = make_result("Reply to Anita\nBook optician")
        by_text = {i["text"]: i for i in result["tasks"]}
        self.assertEqual(by_text["Reply to Anita"]["bucket"], NOW)
        events = self.events(to_ics(result, now=STAMP))
        self.assertEqual([e["SUMMARY"] for e in events], ["Reply to Anita"])
        self.assertEqual(events[0]["DTSTART;VALUE=DATE"], "20261020")

    def test_fuzzy_dates_say_so(self):
        result = make_result("Send the report next week")
        self.assertTrue(result["tasks"][0]["due"]["fuzzy"])
        events = self.events(to_ics(result, now=STAMP))
        self.assertTrue(events[0]["SUMMARY"].endswith("(approx. date)"))

    def test_escaping_and_folding(self):
        long_text = "Call Raj, Anita; and the bank about the very long reference number for the property tax assessment of the flat"
        result = make_result(long_text + "\nReply to Anita")  # two lines, so the ; is not read as a separator
        text = to_ics(result, now=STAMP)
        self.assertIn("\\,", text)
        self.assertIn("\\;", text)
        unfolded = text.replace("\r\n ", "")
        self.assertIn("SUMMARY:" + long_text.replace(",", "\\,").replace(";", "\\;"), unfolded)
        for physical in text.split("\r\n"):
            self.assertLessEqual(len(physical.encode("utf-8")), 75)

    def test_folding_never_splits_a_character(self):
        result = make_result("Pay the bill for café ☕ and 日本語 and emoji 🎉 and the 日本語 café again by tomorrow")
        text = to_ics(result, now=STAMP)
        text.encode("utf-8")  # would raise on a split surrogate
        self.assertIn("日本語", text.replace("\r\n ", ""))

    def test_overrides_move_events(self):
        result = make_result("Pay rent by tomorrow")
        move_task(result, "t1", EXPIRE, TODAY, 7)
        self.assertEqual(self.events(to_ics(result, now=STAMP)), [])

    def test_empty_calendar_is_still_valid(self):
        result = make_result("Learn Rust someday")
        text = to_ics(result, now=STAMP)
        self.assertNotIn("BEGIN:VEVENT", text)
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))


class Dispatch(unittest.TestCase):
    def test_every_format(self):
        result = make_result()
        for fmt, (content_type, extension) in FORMATS.items():
            body, got_type, got_ext = export(result, fmt)
            self.assertIsInstance(body, bytes)
            self.assertEqual((got_type, got_ext), (content_type, extension))
            self.assertGreater(len(body), 50)

    def test_unknown_format(self):
        with self.assertRaises(ValueError) as ctx:
            export(make_result(), "pdf")
        self.assertIn("csv, ics, json, md", str(ctx.exception))

    def test_unicode_survives_every_format(self):
        result = make_result("Pay the bill for café ☕ by Friday")
        for fmt in FORMATS:
            self.assertIn("café", export(result, fmt)[0].decode("utf-8").replace("\r\n ", ""))


if __name__ == "__main__":
    unittest.main()
