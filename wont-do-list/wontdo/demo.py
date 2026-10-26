"""The demo brain: keyword rules that stand in when no model is running.

This is NOT artificial intelligence and the app says so everywhere it is used.
It exists so the app can be tried in ten seconds, so tests can run without a
model, and so a batch that fails inside a real model still gets an answer
instead of a hole in the list.
"""

from __future__ import annotations

import re
import zlib
from typing import Dict, Sequence

from .constants import EXPIRE, LATER, NOTMINE, NOW
from .dates import describe_days

_HANDOFF = re.compile(
    r"""^\s*(?:waiting\s+(?:on|for)|wait\s+for|chase\s+up)\b
      | \b(?:landlord|vendor|contractor|plumber|electrician|hr|ops|the\s+team|someone)\s+(?:to|will|should)\b
      | \bto\s+(?:send|share|confirm|fix|approve|sign)\s+(?:me|us)\b
      | \btheir\s+(?:job|call|decision|turn)\b
      | \bnot\s+my\s+(?:job|problem)\b
      | \bdelegate\b""",
    re.I | re.X,
)
_WISH = re.compile(
    r"""\b(?:someday|some\s+day|one\s+day|eventually|maybe|might|would\s+be\s+nice|bucket\s+list
      | learn(?:ing)?\s+(?:to\s+)?\w+ | read\s+about | look\s+into | explore | try\s+out
      | reorgani[sz]e | organi[sz]e\s+(?:old|my|the)\s+(?:photos?|files?|desktop|bookmarks?|downloads?|folders?)
      | declutter | sort\s+through | inbox\s+zero
      | clean\s+up\s+(?:old|my)\s+(?:files?|photos?|desktop|downloads?)
      | rewatch | binge | webinar | newsletter | survey | unsubscribe | nice\s+to\s+have | photo\s+folders?)\b""",
    re.I | re.X,
)
_QUICK = re.compile(r"^\s*(?:reply|respond|text|ping|message|dm|email|call|remind|ask|confirm|thank)\b", re.I)

_MINUTES = [
    (re.compile(r"\b(?:learn|course|build|thesis|redesign|renovat\w*|trip\s+planning)\b", re.I), 240),
    (re.compile(r"\b(?:report|presentation|taxes|file\s+(?:my\s+)?returns?|clean\s+(?:the\s+)?(?:garage|house|basement)|paint|assemble|migrate|refactor)\b", re.I), 120),
    (re.compile(r"\b(?:write|draft|prepare|plan|study|practice|workout|research|organi[sz]e|reorgani[sz]e|declutter|sort|set\s*up|install|configure)\b", re.I), 60),
    (re.compile(r"\b(?:review|update|fix|clean|laundry|groceries|shopping|cook|meal\s+prep)\b", re.I), 30),
    (re.compile(r"\b(?:book|schedule|buy|pick\s+up|print|fill|sign|renew|register)\b", re.I), 15),
    (re.compile(r"\b(?:pay|transfer|cancel|unsubscribe|order|call)\b", re.I), 10),
    (re.compile(r"\b(?:reply|text|ping|message|dm|remind|ask|thank|confirm)\b", re.I), 5),
]

_EXPIRE_WHY = ["A wish, not a commitment.", "Optional. No one is waiting on it.", "No deadline and no stakes."]
_EXPIRE_IGNORED = ["Nothing changes.", "It quietly stops mattering.", "Nobody notices."]
_NOTMINE_WHY = ["Someone else owns this.", "You are waiting on someone else."]
_NOTMINE_IGNORED = ["It waits on them, not you.", "Nothing moves until they act."]


def estimate_minutes(text: str) -> int:
    for pattern, minutes in _MINUTES:
        if pattern.search(text):
            return minutes
    return 15


def _pick(options: Sequence[str], text: str) -> str:
    return options[zlib.crc32(text.encode("utf-8")) % len(options)]


def judge_one(text: str, due, signals: dict) -> dict:
    """Classify one task with keyword rules. `due` is a dates.Due or None."""
    days = due.days if due is not None else None
    minutes = estimate_minutes(text)
    urgent = bool(signals.get("urgent") or signals.get("overdue"))

    if _HANDOFF.search(text):
        return {
            "bucket": NOTMINE, "risk": "none", "minutes": 5,
            "why": _pick(_NOTMINE_WHY, text), "if_ignored": _pick(_NOTMINE_IGNORED, text),
        }
    if _WISH.search(text) and not (days is not None and days <= 7) and not urgent:
        return {
            "bucket": EXPIRE, "risk": "none", "minutes": minutes,
            "why": _pick(_EXPIRE_WHY, text), "if_ignored": _pick(_EXPIRE_IGNORED, text),
        }
    if days is not None and days <= 2:
        return {
            "bucket": NOW, "risk": "high" if days < 0 or urgent else "medium", "minutes": minutes,
            "why": "Due %s." % describe_days(days), "if_ignored": "It turns into a late task.",
        }
    if urgent:
        return {
            "bucket": NOW, "risk": "high", "minutes": minutes,
            "why": "Marked urgent.", "if_ignored": "Someone is probably waiting on it.",
        }
    if minutes <= 10 and _QUICK.match(text):
        return {
            "bucket": NOW, "risk": "low", "minutes": minutes,
            "why": "Quick win, under ten minutes.", "if_ignored": "It keeps nagging at you.",
        }
    if days is not None and days <= 7:
        return {
            "bucket": LATER, "risk": "medium", "minutes": minutes,
            "why": "Due %s, but not today." % describe_days(days), "if_ignored": "It becomes urgent by then.",
        }
    return {
        "bucket": LATER, "risk": "low", "minutes": minutes,
        "why": "Matters, but not urgent.", "if_ignored": "Nothing breaks this week.",
    }


def judge_demo(entries: Sequence[dict]) -> Dict[int, dict]:
    """Verdicts for a numbered batch: {1: verdict, 2: verdict, ...}."""
    return {
        number: judge_one(entry["text"], entry.get("due"), entry.get("signals", {}))
        for number, entry in enumerate(entries, 1)
    }
