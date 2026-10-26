"""Sort a pasted list: parse it, find dates, ask the model, apply the safety net, fit the budget.

One rule runs through this file: **every pasted task comes back in some bucket.**
If the model fails on a batch, the demo brain answers for it and a warning says so.
Nothing is ever silently dropped.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import demo
from .dates import find_due, find_signals
from .llm import LLMError, coerce_int, extract_rows, normalize_verdict
from .parser import parse_tasks
from .prompts import VERDICT_SCHEMA, build_prompt, token_budget
from .result import apply_budget, plan_checkin, refresh
from .safety import reconcile, scan
from .util import now_iso

ProgressFn = Callable[[int, int, str], None]

NO_RETRY = ("unreachable", "model_missing", "remote_blocked", "timeout")
FATAL_ON_FIRST_BATCH = ("unreachable", "model_missing", "remote_blocked", "timeout", "http")


class Cancelled(Exception):
    """The person pressed Cancel."""


@dataclass
class SortOptions:
    budget: int = 90  # minutes of energy for today; 0 means no limit
    today: Optional[dt.date] = None
    engine: str = "auto"  # "auto" uses the model when there is one; "demo" forces keyword rules
    chunk_size: int = 8
    date_order: str = "dmy"
    never_skip: Sequence[str] = ()
    lessons: Sequence[dict] = ()
    checkin_days: int = 7
    allow_fallback: bool = True  # let the demo brain answer a batch the model cannot


def make_chunks(entries: List[dict], size: int, max_chars: int = 2400) -> List[List[dict]]:
    """Group tasks into batches small enough for a small model to answer reliably."""
    chunks: List[List[dict]] = []
    current: List[dict] = []
    chars = 0
    for entry in entries:
        weight = len(entry["text"]) + 40
        if current and (len(current) >= max(1, size) or chars + weight > max_chars):
            chunks.append(current)
            current, chars = [], 0
        current.append(entry)
        chars += weight
    if current:
        chunks.append(current)
    return chunks


def _verdicts_from_rows(rows: List[dict], count: int) -> Dict[int, dict]:
    """Map the model's rows onto batch positions 1..count, tolerating sloppy ids."""
    ids = [coerce_int(row.get("id")) for row in rows]
    ids_ok = all(i is not None and 1 <= i <= count for i in ids) and len(set(ids)) == len(ids)
    verdicts: Dict[int, dict] = {}
    for position, row in enumerate(rows, 1):
        if ids_ok:
            key: Optional[int] = ids[position - 1]
        elif len(rows) == count:
            key = position  # ids are wrong but the model answered in order
        else:
            candidate = ids[position - 1]
            key = candidate if candidate is not None and 1 <= candidate <= count else None
        if key is None or key in verdicts:
            continue
        verdict = normalize_verdict(row)
        if verdict:
            verdicts[key] = verdict
    return verdicts


def _ask_once(client, entries: List[dict], opts: SortOptions, today: dt.date) -> Tuple[Dict[int, dict], bool]:
    system, user = build_prompt(entries, today, opts.lessons)
    raw = client.complete_json(system, user, VERDICT_SCHEMA, token_budget(len(entries)))
    rows, salvaged = extract_rows(raw)
    return _verdicts_from_rows(rows, len(entries)), salvaged


def _ask_with_retry(client, entries: List[dict], opts: SortOptions, today: dt.date):
    """Ask the model about a batch, re-asking once about whatever it left out."""
    verdicts: Dict[int, dict] = {}
    error: Optional[LLMError] = None
    salvaged_any = False
    pending = list(range(1, len(entries) + 1))
    for _attempt in range(2):
        subset = [entries[p - 1] for p in pending]
        try:
            got, salvaged = _ask_once(client, subset, opts, today)
        except LLMError as err:
            error = err
            if err.kind in NO_RETRY:
                break
            continue
        error = None
        salvaged_any = salvaged_any or salvaged
        for local, verdict in got.items():
            verdicts[pending[local - 1]] = verdict
        pending = [p for p in pending if p not in verdicts]
        if not pending:
            break
    return verdicts, error, salvaged_any


def _engine_info(client, used_model: bool, fallback_count: int, total: int) -> dict:
    if client is None or not used_model or fallback_count >= total:
        return {"kind": "demo", "provider": "", "model": "", "base_url": ""}
    return {
        "kind": "mixed" if fallback_count else "model",
        "provider": getattr(client, "provider", ""),
        "model": getattr(client, "model", ""),
        "base_url": getattr(client, "base", ""),
    }


def sort_tasks(
    raw_text: str,
    client,
    opts: Optional[SortOptions] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
) -> dict:
    """Sort a pasted list and return the full result dict. See result.py for its shape."""
    opts = opts or SortOptions()
    today = opts.today or dt.date.today()
    parsed = parse_tasks(raw_text)

    entries: List[dict] = []
    for task in parsed.tasks:
        entries.append(
            {
                "task": task,
                "text": task.text,
                "group": task.group,
                "due": find_due(task.text, today, opts.date_order),
                "signals": find_signals(task.text),
                "hits": scan(task.text, opts.never_skip),
            }
        )
    total = len(entries)

    def report(done: int, message: str) -> None:
        if progress:
            progress(done, total, message)

    verdicts: Dict[int, Tuple[dict, str]] = {}  # task.idx -> (verdict, source)
    warnings: List[str] = []
    use_model = client is not None and opts.engine != "demo"

    if not use_model:
        report(0, "Sorting with keyword rules")
        for position, verdict in demo.judge_demo(entries).items():
            verdicts[entries[position - 1]["task"].idx] = (verdict, "demo")
        report(total, "Done")
    else:
        done = 0
        model_answered = False
        chunks = make_chunks(entries, opts.chunk_size)
        for number, chunk in enumerate(chunks, 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            first, last = done + 1, done + len(chunk)
            if first == last:
                report(done, "Asking the model about task %d of %d" % (first, total))
            else:
                report(done, "Asking the model about tasks %d–%d of %d" % (first, last, total))
            got, error, salvaged = _ask_with_retry(client, chunk, opts, today)

            if error is not None and not got:
                fatal = error.kind in FATAL_ON_FIRST_BATCH and not model_answered
                if fatal or not opts.allow_fallback:
                    raise error
            if salvaged:
                warnings.append("The model's reply for tasks %d–%d was messy; recovered what was usable." % (first, last))

            missing = [p for p in range(1, len(chunk) + 1) if p not in got]
            if missing:
                if not opts.allow_fallback:
                    raise error or LLMError("format", "The model skipped %d task(s)." % len(missing))
                fallback = demo.judge_demo([chunk[p - 1] for p in missing])
                for local, verdict in fallback.items():
                    got_position = missing[local - 1]
                    verdicts[chunk[got_position - 1]["task"].idx] = (verdict, "fallback")
                reason = error.message if error is not None else "it left some tasks out"
                warnings.append(
                    "Tasks %d–%d: %d task(s) were sorted by keyword rules because %s"
                    % (first, last, len(missing), reason[:1].lower() + reason[1:])
                )
            for position, verdict in got.items():
                verdicts[chunk[position - 1]["task"].idx] = (verdict, "model")
            if got:
                model_answered = True
            done += len(chunk)
            report(done, "Judged %d of %d" % (done, total) if number < len(chunks) else "Done")

    items: List[dict] = []
    for entry in entries:
        task = entry["task"]
        verdict, source = verdicts[task.idx]
        due = entry["due"]
        signals = entry["signals"]
        rec = reconcile(
            verdict["bucket"],
            verdict["risk"],
            entry["hits"],
            due.days if due is not None else None,
            bool(signals["urgent"] or signals["overdue"]),
        )
        flags = list(rec.flags)
        if source == "fallback":  # a pure demo run is labelled once, on the whole result
            flags.append(
                {
                    "kind": "fallback",
                    "title": "Keyword rules",
                    "detail": "The model could not answer for this task, so simple keyword rules sorted it.",
                    "rules": [],
                    "was": "",
                    "now": "",
                }
            )
        items.append(
            {
                "id": "t%d" % task.idx,
                "text": task.text,
                "group": task.group,
                "bucket": rec.bucket,
                "bucket_pre_budget": rec.bucket,
                "model_bucket": verdict["bucket"],
                "user_bucket": None,
                "why": verdict["why"],
                "if_ignored": verdict["if_ignored"],
                "minutes": verdict["minutes"],
                "risk": verdict["risk"],
                "due": due.to_dict() if due is not None else None,
                "urgent": bool(signals["urgent"] or signals["overdue"]),
                "must_do": rec.must_do,
                "flags": flags,
                "source": source,
                "check_on": None,
                "outcome": None,
            }
        )

    fallback_count = sum(1 for item in items if item["source"] == "fallback")
    result = {
        "version": 1,
        "created": now_iso(),
        "today": today.isoformat(),
        "engine": _engine_info(client, use_model, fallback_count, total),
        "budget": opts.budget,
        "input": dict(parsed.summary(), chars=len(raw_text)),
        "tasks": items,
        "warnings": warnings,
    }
    apply_budget(items, opts.budget)
    for item in items:
        item["check_on"] = plan_checkin(item, today, opts.checkin_days)
    return refresh(result)
