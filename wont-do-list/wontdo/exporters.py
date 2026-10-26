"""Take a sorted list out of the app: Markdown, CSV, JSON or a calendar file."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import List, Optional, Tuple

from .constants import BUCKETS, KEEP, LABELS, LATER, NOW, SKIP
from .result import fmt_minutes
from .util import ics_escape, ics_fold

FORMATS = {
    "md": ("text/markdown; charset=utf-8", "md"),
    "csv": ("text/csv; charset=utf-8", "csv"),
    "json": ("application/json; charset=utf-8", "json"),
    "ics": ("text/calendar; charset=utf-8", "ics"),
}


def _by_id(result: dict) -> dict:
    return {item["id"]: item for item in result["tasks"]}


def _ordered(result: dict, bucket: str) -> List[dict]:
    items = _by_id(result)
    return [items[tid] for tid in result["layout"][bucket] if tid in items]


def _due_text(item: dict) -> str:
    due = item.get("due")
    if not due:
        return ""
    try:
        text = dt.date.fromisoformat(due["date"]).strftime("%a %d %b")
    except ValueError:
        return ""
    return text + (" (approx.)" if due.get("fuzzy") else "")


def _engine_text(result: dict) -> str:
    engine = result.get("engine") or {}
    if engine.get("kind") == "demo":
        return "keyword rules (demo brain, no AI model)"
    label = engine.get("model") or "a local model"
    if engine.get("provider"):
        label += " via " + engine["provider"]
    if engine.get("kind") == "mixed":
        label += ", with keyword rules for some tasks"
    return label


def _headline(result: dict) -> str:
    stats = result["stats"]
    return "%d tasks in. %d for today (%s). %d you can skip, about %s of effort." % (
        stats["total"],
        stats["counts"][NOW],
        fmt_minutes(stats["minutes"][NOW]),
        stats["skipped"],
        fmt_minutes(stats["minutes_skipped"]),
    )


def to_markdown(result: dict) -> str:
    created = result.get("today", "")
    try:
        created = dt.date.fromisoformat(created).strftime("%d %b %Y")
    except ValueError:
        pass
    lines = ["# The Won't-Do List — %s" % created, "", _headline(result), "", "Sorted by %s." % _engine_text(result), ""]
    for bucket in BUCKETS:
        items = _ordered(result, bucket)
        if not items:
            continue
        minutes = sum(i["minutes"] for i in items)
        lines.append("## %s (%d · %s)" % (LABELS[bucket], len(items), fmt_minutes(minutes)))
        lines.append("")
        for item in items:
            meta = ["~%s" % fmt_minutes(item["minutes"])]
            if _due_text(item):
                meta.append("due " + _due_text(item))
            if bucket in SKIP and item.get("check_on"):
                meta.append("check back " + item["check_on"])
            prefix = "- [ ] " if bucket in KEEP else "- "
            lines.append("%s%s — %s" % (prefix, item["text"], ", ".join(meta)))
            if item.get("why"):
                lines.append("  - Why: %s" % item["why"])
            if item.get("if_ignored"):
                lines.append("  - If ignored: %s" % item["if_ignored"])
            for flag in item["flags"]:
                if flag["kind"] in ("safety", "consistency", "due"):
                    lines.append("  - %s" % flag["title"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _csv_safe(value: object) -> str:
    """Stop spreadsheet apps from treating text as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(result: dict) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["bucket", "task", "minutes", "risk", "due", "why", "if_ignored", "safety_net", "check_on", "outcome"])
    for bucket in BUCKETS:
        for item in _ordered(result, bucket):
            safety = "; ".join(f["title"] for f in item["flags"] if f["kind"] in ("safety", "consistency", "due"))
            writer.writerow(
                [_csv_safe(v) for v in (
                    LABELS[bucket], item["text"], item["minutes"], item["risk"], _due_text(item),
                    item.get("why", ""), item.get("if_ignored", ""), safety, item.get("check_on") or "",
                    item.get("outcome") or "",
                )]
            )
    return out.getvalue()


def to_json(result: dict) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False) + "\n"


def to_ics(result: dict, now: Optional[dt.datetime] = None) -> str:
    """All-day events for Do now / Do later tasks that have a date (Do now without one lands on the sort day)."""
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    sort_day = dt.date.fromisoformat(result["today"])
    sid = result.get("sid") or "list"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//The Won't-Do List//wontdo//EN", "CALSCALE:GREGORIAN"]
    for bucket in (NOW, LATER):
        for item in _ordered(result, bucket):
            due = item.get("due")
            if due:
                try:
                    day = dt.date.fromisoformat(due["date"])
                except ValueError:
                    continue
            elif bucket == NOW:
                day = sort_day
            else:
                continue
            title = item["text"] + (" (approx. date)" if due and due.get("fuzzy") else "")
            description = "\n".join(
                part
                for part in (
                    "Why: %s" % item["why"] if item.get("why") else "",
                    "If ignored: %s" % item["if_ignored"] if item.get("if_ignored") else "",
                    "About %s." % fmt_minutes(item["minutes"]),
                )
                if part
            )
            lines += [
                "BEGIN:VEVENT",
                "UID:%s-%s@wontdo.local" % (sid, item["id"]),
                "DTSTAMP:%s" % stamp,
                "DTSTART;VALUE=DATE:%s" % day.strftime("%Y%m%d"),
                "DTEND;VALUE=DATE:%s" % (day + dt.timedelta(days=1)).strftime("%Y%m%d"),
                "SUMMARY:%s" % ics_escape(title),
                "DESCRIPTION:%s" % ics_escape(description),
                "CATEGORIES:%s" % ics_escape(LABELS[bucket]),
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


def export(result: dict, fmt: str) -> Tuple[bytes, str, str]:
    """Return (body, content type, file extension) for a format name."""
    if fmt not in FORMATS:
        raise ValueError("Unknown format. Use one of: %s." % ", ".join(sorted(FORMATS)))
    builders = {"md": to_markdown, "csv": to_csv, "json": to_json, "ics": to_ics}
    content_type, extension = FORMATS[fmt]
    return builders[fmt](result).encode("utf-8"), content_type, extension
