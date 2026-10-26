"""Deterministic due-date detection.

Language models are unreliable at date arithmetic, so the dates are found by
plain code and handed to the model as facts ("due in 3 days").  The rules are
deliberately conservative: a missed date is better than an invented one.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

_MON = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a couple of": 2, "couple of": 2, "a few": 3,
}

_CUE_WORDS = ("by", "before", "until", "till", "due", "deadline", "latest", "on", "expires", "expire", "ends", "closes")
_CUE_BEFORE = re.compile(r"\b(?:by|before|until|till|due|deadline|latest|on|expires?|ends?|closes?)\W*$", re.I)
_CUE_FOR_NUMERIC = re.compile(r"\b(?:by|before|until|till|due|deadline|on|from|expires?)\W{0,3}$", re.I)
_PAST_CUE = re.compile(r"\b(?:last|from|since|previous|prev)\W*$", re.I)
_RECURRING_CUE = re.compile(r"\b(?:every|each|all)\W*$", re.I)

RE_ISO = re.compile(r"\b(?P<y>20\d{2})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")
RE_MONTH_DAY = re.compile(
    r"\b(?P<mon>%s)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>20\d{2}))?" % _MON, re.I
)
RE_DAY_MONTH = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>%s)\b\.?(?:,?\s+(?P<year>20\d{2}))?" % _MON, re.I
)
RE_NUMERIC = re.compile(
    r"(?<![\d/.\-:])(?P<a>\d{1,2})[/.](?P<b>\d{1,2})(?:[/.](?P<y>\d{4}|\d{2}))?(?![\d/:\-]|\.\d)"
)
RE_DASHED = re.compile(r"(?<![\d/.\-])(?P<a>\d{1,2})-(?P<b>\d{1,2})-(?P<y>\d{4})(?!\d)")

RE_TODAY = re.compile(
    r"\b(?:today|tonight|tonite|this\s+(?:morning|afternoon|evening)|eod|cob|end\s+of\s+(?:the\s+)?day)\b", re.I
)
RE_TOMORROW = re.compile(r"\b(?:tomorrow|tmrw|tmr)\b", re.I)
RE_YESTERDAY = re.compile(r"\byesterday\b", re.I)
RE_IN = re.compile(
    r"\bin\s+(?:about\s+|around\s+)?(?P<n>\d{1,3}|a\s+couple\s+of|couple\s+of|a\s+few|an|a|one|two|three|four|five|"
    r"six|seven|eight|nine|ten)\s*(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b",
    re.I,
)
RE_NEXT_WEEK = re.compile(r"\bnext\s+week\b", re.I)
RE_THIS_WEEK = re.compile(r"\b(?:this\s+week|end\s+of\s+(?:the\s+)?week|eow)\b", re.I)
RE_WEEKEND = re.compile(r"\b(?:this\s+weekend|by\s+(?:the\s+)?weekend|over\s+the\s+weekend)\b", re.I)
RE_EOM = re.compile(r"\b(?:end\s+of\s+(?:the\s+)?month|eom|month[- ]end|this\s+month)\b", re.I)
RE_NEXT_MONTH = re.compile(r"\bnext\s+month\b", re.I)
RE_EOY = re.compile(r"\b(?:end\s+of\s+(?:the\s+)?year|eoy|year[- ]end)\b", re.I)
RE_WEEKDAY = re.compile(
    r"\b(?:(?P<mod>by|on|this|next|coming|before|until|till)\s+)?"
    r"(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b(?!s\b)",
    re.I,
)
RE_WEEKDAY_ABBR = re.compile(
    r"\b(?P<mod>by|on|this|next|before|until|till)\s+(?P<wd>mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\b\.?", re.I
)

RE_URGENT = re.compile(
    r"\b(?:asap|urgent(?:ly)?|immediately|right\s+away|right\s+now|critical|emergency|time[- ]sensitive)\b", re.I
)
RE_OVERDUE = re.compile(r"\b(?:overdue|past\s+due|late\s+fee|was\s+due|already\s+late|missed\s+the\s+deadline)\b", re.I)


@dataclass(frozen=True)
class Due:
    date: dt.date
    phrase: str
    fuzzy: bool = False
    days: int = 0

    def to_dict(self) -> dict:
        return {"date": self.date.isoformat(), "phrase": self.phrase, "fuzzy": self.fuzzy, "days": self.days}


def describe_days(days: int) -> str:
    """'today', 'tomorrow', 'in 3 days', '2 days ago'."""
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if days > 1:
        return "in %d days" % days
    return "%d days ago" % -days


def add_months(day: dt.date, months: int) -> dt.date:
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(day.day, last))


def _month_number(word: str) -> int:
    return MONTHS[word.lower().rstrip(".")]


def _resolve_month_day(today: dt.date, month: int, day: int, year: Optional[int]) -> Optional[dt.date]:
    """Pick the year for a date written without one: this year unless that is long past."""
    if year is not None:
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    for candidate_year in (today.year, today.year + 1, today.year + 2):
        try:
            candidate = dt.date(candidate_year, month, day)
        except ValueError:
            continue
        if (candidate - today).days >= -60:
            return candidate
    return None


def _numeric_date(today: dt.date, a: int, b: int, year: Optional[str], order: str) -> Optional[dt.date]:
    """Interpret a/b(/year); fall back to the other order when only one is valid."""
    orders = (("d", "m"), ("m", "d")) if order != "mdy" else (("m", "d"), ("d", "m"))
    resolved_year: Optional[int] = None
    if year:
        resolved_year = int(year)
        if resolved_year < 100:
            resolved_year += 2000
    for first, _second in orders:
        day, month = (a, b) if first == "d" else (b, a)
        if 1 <= month <= 12 and 1 <= day <= 31:
            found = _resolve_month_day(today, month, day, resolved_year)
            if found is not None:
                return found
    return None


def _weekday_date(today: dt.date, weekday: int, modifier: str) -> dt.date:
    if modifier == "next":
        monday_next_week = today + dt.timedelta(days=7 - today.weekday())
        return monday_next_week + dt.timedelta(days=weekday)
    return today + dt.timedelta(days=(weekday - today.weekday()) % 7)


Candidate = Tuple[int, dt.date, str, bool, int]  # start, date, phrase, fuzzy, score


def _score(text: str, start: int, phrase: str) -> int:
    score = 0
    if re.match(r"(?:by|before|until|till|on|due)\b", phrase, re.I):
        score += 2
    if _CUE_BEFORE.search(text[max(0, start - 20):start]):
        score += 2
    return score


def find_due(text: str, today: Optional[dt.date] = None, date_order: str = "dmy") -> Optional[Due]:
    """Find the most deadline-like date in `text`, or None."""
    today = today or dt.date.today()
    found: List[Candidate] = []

    def add(match: "re.Match[str]", date: Optional[dt.date], fuzzy: bool = False) -> None:
        if date is None:
            return
        if abs((date - today).days) > 3650:
            return
        phrase = match.group(0).strip()
        found.append((match.start(), date, phrase, fuzzy, _score(text, match.start(), phrase)))

    for m in RE_ISO.finditer(text):
        try:
            add(m, dt.date(int(m.group("y")), int(m.group("m")), int(m.group("d"))))
        except ValueError:
            pass

    for m in RE_MONTH_DAY.finditer(text):
        year = int(m.group("year")) if m.group("year") else None
        add(m, _resolve_month_day(today, _month_number(m.group("mon")), int(m.group("day")), year))

    for m in RE_DAY_MONTH.finditer(text):
        year = int(m.group("year")) if m.group("year") else None
        add(m, _resolve_month_day(today, _month_number(m.group("mon")), int(m.group("day")), year))

    for m in RE_DASHED.finditer(text):
        add(m, _numeric_date(today, int(m.group("a")), int(m.group("b")), m.group("y"), date_order))

    for m in RE_NUMERIC.finditer(text):
        # "3/4 cup" and "24/7" are not dates: demand a year or a deadline word right before.
        if not m.group("y") and not _CUE_FOR_NUMERIC.search(text[max(0, m.start() - 14):m.start()]):
            continue
        add(m, _numeric_date(today, int(m.group("a")), int(m.group("b")), m.group("y"), date_order))

    for m in RE_TODAY.finditer(text):
        add(m, today)
    for m in RE_TOMORROW.finditer(text):
        add(m, today + dt.timedelta(days=1))
    for m in RE_YESTERDAY.finditer(text):
        add(m, today - dt.timedelta(days=1))

    for m in RE_IN.finditer(text):
        raw = re.sub(r"\s+", " ", m.group("n").lower())
        amount = int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw, 0)
        unit = m.group("unit").lower()
        if amount <= 0:
            continue
        if unit.startswith(("min", "hour", "hr")):
            add(m, today)
        elif unit.startswith("day"):
            add(m, today + dt.timedelta(days=amount))
        elif unit.startswith("week"):
            add(m, today + dt.timedelta(days=7 * amount))
        else:
            add(m, add_months(today, amount), fuzzy=True)

    for m in RE_NEXT_WEEK.finditer(text):
        add(m, today + dt.timedelta(days=7 - today.weekday()), fuzzy=True)
    for m in RE_THIS_WEEK.finditer(text):
        weekday = today.weekday()
        add(m, today + dt.timedelta(days=4 - weekday) if weekday <= 4 else today, fuzzy=True)
    for m in RE_WEEKEND.finditer(text):
        weekday = today.weekday()
        add(m, today + dt.timedelta(days=5 - weekday) if weekday < 5 else today, fuzzy=True)
    for m in RE_EOM.finditer(text):
        last = calendar.monthrange(today.year, today.month)[1]
        add(m, dt.date(today.year, today.month, last), fuzzy=True)
    for m in RE_NEXT_MONTH.finditer(text):
        add(m, add_months(today.replace(day=1), 1), fuzzy=True)
    for m in RE_EOY.finditer(text):
        add(m, dt.date(today.year, 12, 31), fuzzy=True)

    for regex in (RE_WEEKDAY, RE_WEEKDAY_ABBR):
        for m in regex.finditer(text):
            before = text[max(0, m.start() - 10):m.start()]
            if _PAST_CUE.search(before) or _RECURRING_CUE.search(before):
                continue
            modifier = (m.group("mod") or "").lower()
            weekday = WEEKDAYS[m.group("wd").lower()]
            add(m, _weekday_date(today, weekday, modifier))

    if not found:
        return None

    # Prefer a date with a deadline word in front of it; otherwise the first one in the text.
    best = sorted(found, key=lambda c: (-c[4], c[0]))[0]
    _start, date, phrase, fuzzy, _score_value = best
    return Due(date=date, phrase=phrase, fuzzy=fuzzy, days=(date - today).days)


def find_signals(text: str) -> dict:
    """Words that say 'urgent' or 'already late', independent of any date."""
    return {"urgent": bool(RE_URGENT.search(text)), "overdue": bool(RE_OVERDUE.search(text))}
