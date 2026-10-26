import unittest

from wontdo.constants import MAX_TASKS, MAX_TASK_CHARS
from wontdo.parser import ParseError, parse_tasks


def texts(raw):
    return [t.text for t in parse_tasks(raw).tasks]


class Splitting(unittest.TestCase):
    def test_one_task_per_line(self):
        self.assertEqual(texts("buy milk\ncall mom\npay rent"), ["buy milk", "call mom", "pay rent"])

    def test_blank_lines_and_whitespace(self):
        self.assertEqual(texts("\n\n  buy milk  \n\n\t call mom\n"), ["buy milk", "call mom"])

    def test_windows_and_old_mac_line_endings(self):
        self.assertEqual(texts("a\r\nb\rc"), ["a", "b", "c"])

    def test_single_line_with_semicolons(self):
        self.assertEqual(texts("buy milk; call mom; pay rent"), ["buy milk", "call mom", "pay rent"])

    def test_semicolons_inside_multiline_text_are_kept(self):
        self.assertEqual(texts("buy milk; eggs\ncall mom"), ["buy milk; eggs", "call mom"])

    def test_indexes_are_one_based_and_sequential(self):
        self.assertEqual([t.idx for t in parse_tasks("a\nb\nc").tasks], [1, 2, 3])


class Markers(unittest.TestCase):
    def test_bullets_and_numbers(self):
        raw = "- one\n* two\n• three\n1. four\n2) five\n(6) six\n— seven"
        self.assertEqual(texts(raw), ["one", "two", "three", "four", "five", "six", "seven"])

    def test_checkboxes(self):
        self.assertEqual(texts("- [ ] open task\n[ ] another\n☐ third"), ["open task", "another", "third"])

    def test_done_items_are_skipped_and_counted(self):
        parsed = parse_tasks("- [x] done\n[X] also done\n✅ yes\n~~struck~~\nreal task")
        self.assertEqual([t.text for t in parsed.tasks], ["real task"])
        self.assertEqual(parsed.done_skipped, 4)

    def test_numbers_that_are_part_of_the_task_survive(self):
        self.assertEqual(texts("2 tickets to book\n2026 taxes\n-5 degrees check"), ["2 tickets to book", "2026 taxes", "-5 degrees check"])

    def test_bold_task_is_unwrapped(self):
        self.assertEqual(texts("**Pay rent**"), ["Pay rent"])

    def test_lines_with_no_letters_are_dropped(self):
        self.assertEqual(texts("---\n***\nreal"), ["real"])


class Groups(unittest.TestCase):
    def test_markdown_headings_become_groups(self):
        parsed = parse_tasks("# Work\nreply to Anita\n## Home\nfix tap")
        self.assertEqual([(t.text, t.group) for t in parsed.tasks], [("reply to Anita", "Work"), ("fix tap", "Home")])

    def test_plain_and_bold_colon_headings(self):
        parsed = parse_tasks("Weekend errands:\nbuy milk\n**Home:**\nfix tap")
        self.assertEqual([t.group for t in parsed.tasks], ["Weekend errands", "Home"])

    def test_long_line_ending_in_colon_is_a_task(self):
        line = "remember to tell everybody about the very important thing:"
        self.assertEqual(texts(line), [line])

    def test_tasks_before_any_heading_have_no_group(self):
        self.assertEqual(parse_tasks("a\n# B\nb").tasks[0].group, "")


class Cleaning(unittest.TestCase):
    def test_duplicates_are_merged(self):
        parsed = parse_tasks("Pay rent\npay  RENT!\n- pay rent\nother")
        self.assertEqual([t.text for t in parsed.tasks], ["Pay rent", "other"])
        self.assertEqual(parsed.duplicates_merged, 2)

    def test_long_lines_are_truncated(self):
        parsed = parse_tasks("x" * 1000)
        self.assertEqual(len(parsed.tasks[0].text), MAX_TASK_CHARS)
        self.assertTrue(parsed.tasks[0].text.endswith("…"))
        self.assertEqual(parsed.truncated, 1)

    def test_control_and_zero_width_characters_are_removed(self):
        self.assertEqual(texts("pay\u200b rent\x00\x07 now"), ["pay rent now"])
        self.assertEqual(texts("\ufeffbuy\u2060 milk"), ["buy milk"])

    def test_joiners_that_carry_meaning_are_kept(self):
        persian = "می\u200cخواهم"  # needs the zero-width non-joiner to join correctly
        self.assertEqual(texts(persian), [persian])
        family = "plan \U0001F469\u200d\U0001F4BB day"  # an emoji sequence held together by a joiner
        self.assertEqual(texts(family), [family])

    def test_unicode_tasks(self):
        self.assertEqual(texts("रेंट भरना\n午餐を予約"), ["रेंट भरना", "午餐を予約"])

    def test_summary(self):
        self.assertEqual(parse_tasks("a\n[x] b\na").summary(), {"count": 1, "done_skipped": 1, "duplicates_merged": 1, "truncated": 0})


class Errors(unittest.TestCase):
    def assertParseError(self, raw, code):
        with self.assertRaises(ParseError) as ctx:
            parse_tasks(raw)
        self.assertEqual(ctx.exception.code, code)

    def test_empty_inputs(self):
        for raw in ("", "   \n\n ", "[x] only done", "---"):
            self.assertParseError(raw, "empty")

    def test_not_a_string(self):
        self.assertParseError(None, "empty")

    def test_too_many_tasks(self):
        self.assertParseError("\n".join("task %d" % i for i in range(MAX_TASKS + 1)), "too_many")
        self.assertEqual(len(parse_tasks("\n".join("task %d" % i for i in range(MAX_TASKS))).tasks), MAX_TASKS)

    def test_too_much_text(self):
        self.assertParseError("a\n" * 70000, "too_long")


if __name__ == "__main__":
    unittest.main()
