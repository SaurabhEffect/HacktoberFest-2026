"""A ten-second check that your model server, your model and this app agree with each other."""

from __future__ import annotations

import datetime as dt
import time
from typing import List, Optional

from .constants import KEEP, LABELS
from .judge import SortOptions, sort_tasks
from .llm import LLMError, detect_backend, make_client
from .util import clip

SAMPLE = "Pay electricity bill by Friday\nLearn Rust someday\nWaiting for the landlord to fix the tap\n"


def _step(steps: List[dict], name: str, level: str, detail: str = "", hint: str = "") -> None:
    steps.append({"name": name, "level": level, "detail": detail, "hint": hint})


def run_doctor(settings: dict, today: Optional[dt.date] = None) -> dict:
    """Probe the model server, then sort three sample tasks for real and check the answer."""
    steps: List[dict] = []
    backend = detect_backend(settings, timeout=2.0)

    if not backend.reachable:
        _step(steps, "Find a model server", "fail", backend.error, backend.hint)
        return {"ok": False, "steps": steps, "backend": backend.to_dict(), "raw": ""}
    _step(steps, "Find a model server", "ok", "%s at %s" % (backend.provider, backend.base_url))

    if not backend.models:
        _step(steps, "List installed models", "fail", "The server has no models.", "Install one: ollama pull gemma3:4b")
        return {"ok": False, "steps": steps, "backend": backend.to_dict(), "raw": ""}
    names = ", ".join(m.name for m in backend.models[:6]) + (" …" if len(backend.models) > 6 else "")
    _step(steps, "List installed models", "ok", "%d found: %s" % (len(backend.models), names))

    if backend.model_missing:
        _step(
            steps, "Choose a model", "fail", "“%s” is not installed." % backend.model,
            "Run: ollama pull %s, or pick another model in Settings." % backend.model,
        )
        return {"ok": False, "steps": steps, "backend": backend.to_dict(), "raw": ""}
    _step(steps, "Choose a model", "ok", backend.model)

    client = make_client(settings, backend)
    started = time.monotonic()
    raw = ""
    try:
        result = sort_tasks(
            SAMPLE, client,
            SortOptions(budget=0, today=today, chunk_size=3, allow_fallback=False, date_order=settings.get("date_order", "dmy")),
        )
    except LLMError as err:
        raw = getattr(client, "last_raw", "")
        _step(steps, "Answer in the right JSON shape", "fail", err.message, err.hint or "Try a different model, or see the raw reply below.")
        return {"ok": False, "steps": steps, "backend": backend.to_dict(), "raw": raw[:1500]}
    elapsed = time.monotonic() - started
    _step(steps, "Answer in the right JSON shape", "ok", "3 tasks sorted in %.1f s" % elapsed)

    by_text = {item["text"]: item for item in result["tasks"]}
    bill = by_text.get("Pay electricity bill by Friday")
    said = ", ".join("%s → %s" % (clip(i["text"], 26), LABELS[i["model_bucket"]]) for i in result["tasks"])
    if bill is not None and bill["model_bucket"] in KEEP:
        _step(steps, "Sensible verdicts", "ok", said)
    else:
        _step(
            steps, "Sensible verdicts", "warn", said,
            "This model wanted to let a bill expire. The safety net would catch it, but a stronger model will annoy you less.",
        )
    return {"ok": True, "steps": steps, "backend": backend.to_dict(), "raw": ""}
