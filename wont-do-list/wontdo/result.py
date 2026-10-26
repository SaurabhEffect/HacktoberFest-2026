"""Everything that happens to a sorted list after the model has spoken.

All functions here work on plain dicts (the saved JSON), so they are easy to
test and the same code serves a fresh sort and an old one reopened from history.

An item has four buckets on it, on purpose:

* ``model_bucket``       what the model (or demo brain) said
* ``bucket_pre_budget``  after the safety net
* ``bucket``             after the energy budget
* ``user_bucket``        the person's own override, which always wins
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

from .constants import BUCKETS, EXPIRE, LABELS, LATER, NOTMINE, NOW, OUTCOMES, RISK_RANK, SKIP
from .util import now_iso


class ResultError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def display_bucket(item: dict) -> str:
    return item.get("user_bucket") or item["bucket"]


def fmt_minutes(minutes: int) -> str:
    if minutes < 60:
        return "%d min" % minutes
    hours, rest = divmod(minutes, 60)
    return "%d h" % hours if rest == 0 else "%d h %d min" % (hours, rest)


def _priority(item: dict):
    due = item.get("due")
    days = due["days"] if due else 9999
    return (days, -RISK_RANK.get(item["risk"], 0), item["minutes"], int(item["id"][1:]))


def apply_budget(items: List[dict], budget: int) -> None:
    """Fit Do now into today's energy. Safe to call again with a different budget."""
    for item in items:
        item["bucket"] = item["bucket_pre_budget"]
        item["flags"] = [flag for flag in item["flags"] if flag.get("kind") != "budget"]
    if budget <= 0:
        return

    fixed = [i for i in items if display_bucket(i) == NOW and (i.get("user_bucket") or i.get("must_do"))]
    total = sum(i["minutes"] for i in fixed)
    candidates = [i for i in items if i["bucket"] == NOW and not i.get("user_bucket") and not i.get("must_do")]
    for item in sorted(candidates, key=_priority):
        if total + item["minutes"] <= budget:
            total += item["minutes"]
            continue
        item["bucket"] = LATER
        item["flags"].append(
            {
                "kind": "budget",
                "title": "Over today's budget",
                "detail": "It would not fit in %s, so it moved to %s." % (fmt_minutes(budget), LABELS[LATER]),
                "rules": [],
                "was": NOW,
                "now": LATER,
            }
        )


def bucket_layout(items: List[dict]) -> Dict[str, List[str]]:
    """Task ids per bucket, in the order they should be shown."""
    groups: Dict[str, List[dict]] = {bucket: [] for bucket in BUCKETS}
    for item in items:
        groups[display_bucket(item)].append(item)
    groups[NOW].sort(key=lambda i: (not i.get("must_do"), _priority(i)))
    groups[LATER].sort(key=_priority)
    for bucket in SKIP:
        groups[bucket].sort(key=lambda i: int(i["id"][1:]))
    return {bucket: [i["id"] for i in groups[bucket]] for bucket in BUCKETS}


def compute_stats(items: List[dict]) -> dict:
    counts = dict.fromkeys(BUCKETS, 0)
    minutes = dict.fromkeys(BUCKETS, 0)
    rescued = 0
    fallback = 0
    must_do = 0
    for item in items:
        bucket = display_bucket(item)
        counts[bucket] += 1
        minutes[bucket] += item["minutes"]
        if any(flag.get("kind") in ("safety", "consistency") for flag in item["flags"]):
            rescued += 1
        if item.get("source") == "fallback":
            fallback += 1
        if item.get("must_do"):
            must_do += 1
    return {
        "total": len(items),
        "counts": counts,
        "minutes": minutes,
        "kept": counts[NOW] + counts[LATER],
        "skipped": counts[EXPIRE] + counts[NOTMINE],
        "minutes_skipped": minutes[EXPIRE] + minutes[NOTMINE],
        "rescued": rescued,
        "fallback": fallback,
        "must_do": must_do,
    }


def plan_checkin(item: dict, today: dt.date, checkin_days: int) -> Optional[str]:
    """When to ask 'did skipping this cost you anything?'."""
    if display_bucket(item) not in SKIP:
        return None
    due = item.get("due")
    if due and not due.get("fuzzy"):
        try:
            due_date = dt.date.fromisoformat(due["date"])
        except ValueError:
            due_date = None
        if due_date is not None and today <= due_date <= today + dt.timedelta(days=30):
            return (due_date + dt.timedelta(days=1)).isoformat()
    return (today + dt.timedelta(days=max(1, checkin_days))).isoformat()


def refresh(result: dict) -> dict:
    """Recompute everything derived from the items."""
    items = result["tasks"]
    result["stats"] = compute_stats(items)
    result["layout"] = bucket_layout(items)
    return result


def find_item(result: dict, tid: str) -> dict:
    for item in result["tasks"]:
        if item["id"] == tid:
            return item
    raise ResultError("not_found", "That task is not in this list.")


def move_task(result: dict, tid: str, bucket: Optional[str], today: dt.date, checkin_days: int) -> dict:
    """The person overrules the sort. `bucket=None` removes their override."""
    if bucket is not None and bucket not in BUCKETS:
        raise ResultError("bad_bucket", "Unknown bucket.")
    item = find_item(result, tid)
    before = display_bucket(item)
    item["user_bucket"] = None if bucket is None or bucket == item["bucket"] else bucket
    after = display_bucket(item)

    if after in SKIP:
        if before not in SKIP or not item.get("check_on"):
            item["check_on"] = plan_checkin(item, today, checkin_days)
    else:
        item["check_on"] = None
        item["outcome"] = None
        item.pop("outcome_at", None)
    return refresh(result)


def mark_outcome(result: dict, tid: str, outcome: Optional[str]) -> dict:
    """'Nothing happened' or 'That bit me' for a task the person skipped."""
    if outcome is not None and outcome not in OUTCOMES:
        raise ResultError("bad_outcome", "Outcome must be one of: %s." % ", ".join(OUTCOMES))
    item = find_item(result, tid)
    if display_bucket(item) not in SKIP:
        raise ResultError("not_skipped", "Only skipped tasks have a check-in.")
    item["outcome"] = outcome
    if outcome:
        item["outcome_at"] = now_iso()
    else:
        item.pop("outcome_at", None)
    return refresh(result)


def rebudget(result: dict, budget: int, today: dt.date, checkin_days: int) -> dict:
    """Change today's energy and re-flow Do now / Do later without asking the model again."""
    result["budget"] = budget
    apply_budget(result["tasks"], budget)
    for item in result["tasks"]:
        if display_bucket(item) in SKIP:
            if not item.get("check_on"):
                item["check_on"] = plan_checkin(item, today, checkin_days)
        else:
            item["check_on"] = None
    return refresh(result)
