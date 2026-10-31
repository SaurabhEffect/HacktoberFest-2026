"""The rule-based preview engine.

It uses no model at all: only the evidence and a few text heuristics. The UI labels it clearly
as a preview. It exists so the app can be tried (and tested) without a model running, and it
mirrors the shape of a model's answer so everything downstream is identical.
"""
from .evidence import best_strength
from .templates import build_reply, best_evidence, detail_score, is_feature_request, ref_label


def analyze(issue, evidence, ctx=None):
    strength = best_strength(evidence)
    top = best_evidence(evidence)

    if strength == "closing":
        verdict, confidence = "fixed", "high"
        reason = "%s (%s) declares that it fixes this issue." % (ref_label(top), top.get("date") or "?")
    elif strength == "mention":
        verdict, confidence = "fixed", "medium"
        reason = "%s (%s) mentions this issue." % (ref_label(top), top.get("date") or "?")
    elif strength == "similar":
        verdict, confidence = "fixed", "low"
        reason = "%s looks related, but it never mentions this issue, so check it." % ref_label(top)
    else:
        score = detail_score(issue)
        body_len = len(issue.get("body") or "")
        if is_feature_request(issue) and body_len >= 40:
            verdict, confidence = "valid", "medium"
            reason = "A clear feature request, and nothing in the later history delivers it."
        elif score >= 2:
            verdict = "valid"
            confidence = "high" if score >= 4 else "medium"
            reason = "A concrete report, and nothing in the later history addresses it."
        else:
            verdict, confidence = "needs_info", "medium"
            reason = "Too thin to act on: it does not say how to reproduce the problem."

    return {
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason,
        "reply": build_reply(verdict, issue, evidence, reason),
        "flags": [],
    }
