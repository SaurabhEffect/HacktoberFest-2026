"""Turn whatever the person pasted into a clean list of tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from .constants import MAX_INPUT_CHARS, MAX_TASK_CHARS, MAX_TASKS
from .util import clip, norm_key


class ParseError(ValueError):
    """The pasted text cannot be turned into a usable task list."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Task:
    idx: int  # 1-based position in the cleaned list
    text: str
    group: str = ""


@dataclass
class ParsedList:
    tasks: List[Task] = field(default_factory=list)
    done_skipped: int = 0
    duplicates_merged: int = 0
    truncated: int = 0

    def summary(self) -> Dict[str, int]:
        return {
            "count": len(self.tasks),
            "done_skipped": self.done_skipped,
            "duplicates_merged": self.duplicates_merged,
            "truncated": self.truncated,
        }


_BULLET = re.compile(
    "^(?:[-*+•▪◦●○‣⁃–—]\\s+|\\d{1,3}[.)]\\s+|\\(\\d{1,3}\\)\\s+|"
    "\\[\\s?\\]\\s*|☐\\s*|□\\s*)+"
)
_DONE = re.compile("^(?:[-*+•]\\s*)?(?:\\[[xX✓✔]\\]|[✅☑✔✓]️?)\\s*")
_STRIKE = re.compile(r"^~~.+~~$")
_MD_HEADING = re.compile(r"^#{1,6}\s+(?P<t>.+?)\s*#*$")
# A bold line is a heading only when it ends with a colon; "**Pay rent**" stays a task.
_BOLD_HEADING = re.compile(r"^\*\*(?P<t>[^*]{1,40}?)(?::\*\*|\*\*:)$")
# Paste debris only: zero-width space, word joiner and byte-order mark. The zero-width non-joiner
# and joiner stay, because Persian, Urdu, Malayalam, Devanagari and emoji sequences need them.
_ZERO_WIDTH = re.compile("[\u200b\u2060\ufeff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _is_plain_heading(line: str) -> bool:
    """'Work:' or 'Weekend errands:' on its own line introduces the tasks below it."""
    if not line.endswith(":") or len(line) > 40:
        return False
    if _BULLET.match(line):
        return False
    return len(line[:-1].split()) <= 5


def parse_tasks(raw: str) -> ParsedList:
    """Split pasted text into tasks. Raises ParseError when there is nothing usable."""
    if not isinstance(raw, str):
        raise ParseError("empty", "Paste your task list first.")
    if len(raw) > MAX_INPUT_CHARS:
        raise ParseError("too_long", "That is a lot of text. Paste up to %d characters at a time." % MAX_INPUT_CHARS)

    text = _CONTROL.sub("", _ZERO_WIDTH.sub("", raw.replace("\r\n", "\n").replace("\r", "\n")))
    lines = [line for line in text.split("\n") if line.strip()]
    if len(lines) == 1 and ";" in lines[0]:
        lines = [part for part in lines[0].split(";") if part.strip()]

    result = ParsedList()
    seen = set()
    group = ""

    for line in lines:
        stripped = line.strip()

        heading = _MD_HEADING.match(stripped) or _BOLD_HEADING.match(stripped)
        if heading:
            group = clip(heading.group("t").strip(" :"), 40)
            continue
        if _is_plain_heading(stripped):
            group = clip(stripped[:-1], 40)
            continue

        if _DONE.match(stripped) or _STRIKE.match(stripped):
            result.done_skipped += 1
            continue

        body = _BULLET.sub("", stripped).strip()
        body = re.sub(r"^\*\*(.+)\*\*$", r"\1", body)
        body = " ".join(body.split())
        if not re.search(r"\w", body, re.UNICODE):
            continue

        if len(body) > MAX_TASK_CHARS:
            body = body[: MAX_TASK_CHARS - 1].rstrip() + "…"
            result.truncated += 1

        key = norm_key(body)
        if key in seen:
            result.duplicates_merged += 1
            continue
        seen.add(key)
        result.tasks.append(Task(idx=len(result.tasks) + 1, text=body, group=group))

    if not result.tasks:
        raise ParseError("empty", "No tasks found. Paste one task per line.")
    if len(result.tasks) > MAX_TASKS:
        raise ParseError(
            "too_many",
            "That is %d tasks. Sort up to %d at a time, then paste the rest." % (len(result.tasks), MAX_TASKS),
        )
    return result
