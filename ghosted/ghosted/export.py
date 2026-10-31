"""Review-sheet exports: Markdown (for humans), CSV (for spreadsheets), JSON (for scripts)."""
import csv
import io
import json
import re

from .textutil import day_str, one_line, span_short

VERDICT_LABELS = {"valid": "Still valid", "fixed": "Already fixed", "needs_info": "Needs info"}
CONTENT_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


def final_reply(result):
    edited = result.get("edited_reply")
    return edited if edited is not None else (result.get("reply") or "")


def pick(results, only):
    done = [r for r in results if r.get("status") == "done"]
    if only == "approved":
        return [r for r in done if r.get("review") == "approved"]
    return done


def csv_safe(value):
    """Stop spreadsheet apps from running text that starts like a formula."""
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def export_filename(scan, ext):
    repo = re.sub(r"[^A-Za-z0-9._-]+", "-", scan.get("repo") or "scan")
    repo = re.sub(r"\.{2,}", ".", repo).strip("-.") or "scan"
    stamp = (scan.get("created_at") or "")[:10].replace("-", "")
    return "ghosted-%s-%s.%s" % (repo, stamp or "export", ext)


def export_markdown(scan, results, only=None):
    chosen = pick(results, only)
    counts = {"fixed": 0, "valid": 0, "needs_info": 0}
    for r in chosen:
        if r.get("verdict") in counts:
            counts[r["verdict"]] += 1
    lines = [
        "# Ghosted review sheet: %s" % scan.get("repo"),
        "",
        "Created %s · engine: %s · **nothing was posted to GitHub**" % (
            day_str(scan.get("created_at")), scan.get("engine") or "unknown"),
        "",
        "%d issue%s%s: %d already fixed, %d still valid, %d need info." % (
            len(chosen), "" if len(chosen) == 1 else "s",
            " (approved only)" if only == "approved" else "",
            counts["fixed"], counts["valid"], counts["needs_info"]),
        "",
    ]
    for r in chosen:
        lines.append("---")
        lines.append("")
        lines.append("### #%s — %s" % (r["number"], one_line(r["title"], 200)))
        lines.append("")
        lines.append("- Link: %s" % (r.get("url") or "(none)"))
        lines.append("- Silence: %s · opened by @%s on %s" % (
            span_short(r.get("age_days") or 0), r.get("author") or "unknown",
            day_str(r.get("created_at"))))
        lines.append("- Verdict: **%s** (%s confidence) — %s" % (
            VERDICT_LABELS.get(r.get("verdict"), r.get("verdict")), r.get("confidence"),
            one_line(r.get("reason"), 300)))
        for item in r.get("evidence") or []:
            where = " (%s)" % item["date"] if item.get("date") else ""
            lines.append("- Evidence [%s]: %s%s %s" % (
                item.get("strength"), item.get("ref"), where, item.get("url") or ""))
        lines.append("- Review: %s" % r.get("review", "new"))
        lines.append("")
        for line in final_reply(r).splitlines() or [""]:
            lines.append("> " + line if line else ">")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_csv(scan, results, only=None):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["number", "title", "url", "author", "opened", "silence_days", "verdict",
                     "confidence", "reason", "evidence", "review", "reply"])
    for r in pick(results, only):
        evidence = "; ".join("%s (%s)" % (e.get("ref"), e.get("strength"))
                             for e in r.get("evidence") or [])
        writer.writerow([csv_safe(v) for v in (
            r["number"], r["title"], r.get("url"), r.get("author"), day_str(r.get("created_at")),
            r.get("age_days"), r.get("verdict"), r.get("confidence"), r.get("reason"), evidence,
            r.get("review"), final_reply(r))])
    return out.getvalue()


def export_json(scan, results, only=None):
    payload = {
        "repo": scan.get("repo"), "engine": scan.get("engine"), "created_at": scan.get("created_at"),
        "posted_to_github": False, "stats": scan.get("stats"),
        "issues": [{
            "number": r["number"], "title": r["title"], "url": r.get("url"),
            "author": r.get("author"), "opened": r.get("created_at"),
            "silence_days": r.get("age_days"), "verdict": r.get("verdict"),
            "confidence": r.get("confidence"), "reason": r.get("reason"),
            "evidence": r.get("evidence"), "flags": r.get("flags"), "review": r.get("review"),
            "reply": final_reply(r),
        } for r in pick(results, only)],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


EXPORTERS = {"md": export_markdown, "csv": export_csv, "json": export_json}
