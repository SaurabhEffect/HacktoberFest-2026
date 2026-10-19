"""Optional clean-up pass with a local LLM served by Ollama (Gemma, Qwen, Llama, ...).

Guard rails keep the model from ever changing timing or meaning: every card must come back
with exactly the same number of words and no more than ~55% of its letters changed,
otherwise the original card is kept untouched.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence

from .captions import Card
from .media import Cancelled

PREFERRED = ("gemma3", "gemma2", "gemma", "qwen2.5", "qwen3", "llama3", "llama", "mistral", "phi")
BATCH = 12

SYSTEM_PROMPT = (
    "You fix spelling in Roman-script Hinglish captions (Hindi written with English letters, mixed with "
    "English words). Rules: keep every line's number of words EXACTLY the same; keep the meaning and word "
    "order; use the common spelling people type (kya, hai, nahi, bahut, accha, theek, zyada, mujhe, karo); "
    "write English words with normal English spelling; keep punctuation; never add or remove words. "
    "Reply with JSON only: an array of strings, one per input line, in the same order."
)


class OllamaError(RuntimeError):
    pass


def _request(url: str, payload: Optional[dict], timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaError(str(exc)) from exc


def list_models(base_url: str, timeout: float = 1.5) -> List[str]:
    """Names of models installed in the local Ollama (empty when Ollama is not running)."""
    try:
        data = _request(base_url + "/api/tags", None, timeout)
    except OllamaError:
        return []
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def pick_model(installed: Sequence[str], wanted: str = "") -> str:
    if wanted and wanted in installed:
        return wanted
    for prefix in PREFERRED:
        for name in installed:
            if name.lower().startswith(prefix):
                return name
    return installed[0] if installed else ""


def _letters(text: str) -> str:
    return re.sub(r"\W+", "", text).casefold()


def _acceptable(old: str, new: str) -> bool:
    if not isinstance(new, str) or len(new.split()) != len(old.split()):
        return False
    a, b = _letters(old), _letters(new)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.45


def _ask(base_url: str, model: str, lines: List[str], timeout: float) -> List[str]:
    body = {
        "model": model, "stream": False, "format": "json", "options": {"temperature": 0},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps(lines, ensure_ascii=False)}],
    }
    reply = _request(base_url + "/api/chat", body, timeout)
    content = (reply.get("message") or {}).get("content", "")
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise OllamaError("The model did not return valid JSON.") from exc
    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list):
                parsed = value
                break
    if not isinstance(parsed, list):
        raise OllamaError("The model returned an unexpected shape.")
    return [str(x) for x in parsed]


def polish_cards(cards: Sequence[Card], base_url: str, model: str,
                 on_progress: Optional[Callable[[float], None]] = None,
                 cancel: Optional[threading.Event] = None, timeout: float = 180.0) -> Dict[str, int]:
    """Fix spelling in place. Returns counts: changed / kept / failed batches."""
    stats = {"changed": 0, "kept": 0, "failed": 0}
    items = list(cards)
    for start in range(0, len(items), BATCH):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        batch = items[start:start + BATCH]
        lines = [c.text for c in batch]
        try:
            replies = _ask(base_url, model, lines, timeout)
        except OllamaError:
            stats["failed"] += 1
            stats["kept"] += len(batch)
            replies = []
        if len(replies) == len(batch):
            for card, old, new in zip(batch, lines, replies, strict=True):
                tokens = new.split()
                # A card's text splits into one token per word, unless a "My words" rule put a space inside a word.
                # Then the reply cannot be mapped back word by word, so the card stays as it is.
                if (len(tokens) == len(card.words) and _acceptable(old, new) and tokens != old.split()):
                    for word, token in zip(card.words, tokens, strict=True):
                        word.w = token
                    stats["changed"] += 1
                else:
                    stats["kept"] += 1
        elif replies:
            stats["kept"] += len(batch)
        if on_progress:
            on_progress(min(1.0, (start + len(batch)) / max(len(items), 1)))
    return stats
