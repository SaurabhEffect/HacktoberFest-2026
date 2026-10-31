"""Plain-text reply templates.

They have two jobs: they power the rule-based preview engine, and they are the safe fallback
whenever a model's draft cites something that cannot be verified.
"""
import re

from .evidence import STRENGTH_RANK
from .textutil import one_line, span_phrase

_VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
_ENV = re.compile(
    r"\b(windows|linux|ubuntu|debian|fedora|arch|macos|mac os|osx|android|ios|docker|python|node|"
    r"nodejs|java|rust|golang|ruby|php|\.net|dotnet|chrome|firefox|safari|edge|gcc|clang)\b", re.I)
_STEPS = re.compile(r"\b(steps?|reproduc\w*|repro|example|snippet|minimal|to reproduce)\b", re.I)
_EXPECTED = re.compile(r"\b(expected|actual|instead|should|supposed|expect)\b", re.I)
_ERRORISH = re.compile(r"\b(traceback|exception|stack ?trace|segfault|panic|error:|errno)\b", re.I)


def join_and(parts):
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def ensure_sentence(text):
    text = one_line(text, 240).strip()
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text


def missing_details(text):
    """What a maintainer would have to ask the reporter for, based on what is missing."""
    text = text or ""
    asks = []
    if not _VERSION.search(text) and not re.search(r"\bversion\b", text, re.I):
        asks.append("which version you are using")
    if not _ENV.search(text):
        asks.append("your OS and runtime (for example the Python or Node version)")
    if "```" not in text and not _STEPS.search(text):
        asks.append("the exact steps, or a minimal example, that reproduce it")
    if not _EXPECTED.search(text):
        asks.append("what you expected to happen instead")
    if not asks:
        asks.append("the exact error message or output you see")
    return asks[:4]


def detail_score(issue):
    """How actionable the report looks (0-6); used by the rule-based preview."""
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    score = 0
    if "```" in body or _ERRORISH.search(body):
        score += 1
    if _VERSION.search(body):
        score += 1
    if _STEPS.search(body):
        score += 1
    if _EXPECTED.search(body):
        score += 1
    if len(body) >= 200:
        score += 1
    if len(title.split()) >= 4:
        score += 1
    return score


def is_feature_request(issue):
    labels = " ".join(issue.get("labels") or []).lower()
    title = (issue.get("title") or "").lower()
    return ("enhancement" in labels or "feature" in labels or
            bool(re.match(r"^(please |feature[: ]|add |support |allow |provide |publish )", title)))


def ref_label(item):
    """'PR #45', 'commit a1b2c3d' or 'release v1.2.0' (already formatted in the evidence item)."""
    return item.get("ref") or item.get("kind") or "a later change"


def best_evidence(evidence):
    ranked = sorted(evidence or [], key=lambda e: -STRENGTH_RANK.get(e.get("strength"), 0))
    return ranked[0] if ranked else None


def _greeting(issue):
    author = issue.get("author") or ""
    return "Hi @%s," % author if author else "Hi there,"


def build_reply(verdict, issue, evidence, reason=""):
    """A short, plain, kind reply for the given verdict. No promises, no invented facts."""
    span = span_phrase(issue.get("age_days") or 0)
    hello = _greeting(issue)
    top = best_evidence(evidence)

    if verdict == "fixed" and top:
        strong = top.get("strength") in ("closing", "mention")
        when = " (%s)" % top["date"] if top.get("date") else ""
        others = max(0, len(evidence) - 1)
        if strong:
            extra = ""
            if others:
                extra = " There %s also %d other related change%s. " % (
                    "is" if others == 1 else "are", others, "" if others == 1 else "s")
            return ("%s sorry this sat unanswered for %s. Looking back through the history, this "
                    "looks like it was addressed in %s%s.%s Could you try the latest version and "
                    "let us know whether it works for you now? If it does, this can be closed; if "
                    "not, tell us what you still see and we will take another look."
                    % (hello, span, ref_label(top), when, extra or ""))
        return ("%s sorry this sat unanswered for %s. A later change, %s%s, looks related, "
                "though it does not mention this issue directly. Could you check whether you "
                "still see the problem on the latest version and tell us what you find?"
                % (hello, span, ref_label(top), when))

    if verdict == "valid":
        why = ensure_sentence(reason) or "The report describes a concrete problem."
        return ("%s thank you for the report, and sorry it sat unanswered for %s. Reading it "
                "again today, this still looks like a genuine problem. %s I could not find a "
                "later change that addresses it. If you are still affected, a short comment here "
                "would help us prioritise." % (hello, span, why))

    asks = join_and(missing_details(
        "%s\n%s" % (issue.get("title") or "", issue.get("body") or "")))
    return ("%s thanks for opening this, and sorry it sat unanswered for %s. To look into it "
            "properly we would need a little more detail: %s. If you are still running into "
            "this, could you add that here?" % (hello, span, asks))
