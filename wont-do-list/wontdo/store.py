"""Everything the app remembers lives in plain JSON files in one folder.

    data/settings.json
    data/history/<id>.json     one file per sorted list

Open them in any editor. Delete the folder and the app forgets everything.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import threading
from typing import Any, Dict, List, Optional

from .constants import KEEP, LATER, SKIP
from .result import ResultError, display_bucket, mark_outcome, move_task, rebudget
from .util import SID_RE, atomic_write_text, clip, new_sid, norm_key, url_host

MAX_HISTORY = 500

DEFAULTS: Dict[str, Any] = {
    "provider": "auto",
    "base_url": "",
    "model": "",
    "api_key": "",
    "allow_remote": False,
    "demo_fallback": True,
    "budget_minutes": 90,
    "chunk_size": 8,
    "timeout_s": 300,
    "temperature": 0.1,
    "date_order": "dmy",
    "checkin_days": 7,
    "never_skip": [],
    "lessons": True,
    "autosave": True,
}


class StoreError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _int_in(value: Any, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise StoreError("bad_setting", "%s must be a number." % name)
    try:
        number = int(float(value))
    except ValueError:
        raise StoreError("bad_setting", "%s must be a number." % name) from None
    if not low <= number <= high:
        raise StoreError("bad_setting", "%s must be between %d and %d." % (name, low, high))
    return number


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise StoreError("bad_setting", "%s must be on or off." % name)


def _words(value: Any) -> List[str]:
    if isinstance(value, str):
        value = value.replace(",", "\n").split("\n")
    if not isinstance(value, list):
        raise StoreError("bad_setting", "Never-skip words must be a list.")
    words: List[str] = []
    seen = set()
    for raw in value:
        word = " ".join(str(raw).split())
        if not word:
            continue
        if not 2 <= len(word) <= 40:
            raise StoreError("bad_setting", "Never-skip words must be 2 to 40 characters: “%s”." % clip(word, 20))
        if word.lower() in seen:
            continue
        seen.add(word.lower())
        words.append(word)
    if len(words) > 50:
        raise StoreError("bad_setting", "Keep the never-skip list to 50 words or fewer.")
    return words


def validate_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Check a (partial) settings update and return it cleaned. Unknown keys are rejected."""
    clean: Dict[str, Any] = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            raise StoreError("bad_setting", "Unknown setting: %s." % clip(key, 40))
        if key == "provider":
            if value not in ("auto", "ollama", "openai"):
                raise StoreError("bad_setting", "Provider must be auto, ollama or openai.")
            clean[key] = value
        elif key == "base_url":
            text = str(value or "").strip()
            if text:
                if "://" not in text:
                    text = "http://" + text
                if not text.startswith(("http://", "https://")) or not url_host(text):
                    raise StoreError("bad_setting", "The model address must look like http://localhost:11434.")
            clean[key] = text.rstrip("/")
        elif key in ("model", "api_key"):
            text = str(value or "").strip()
            if len(text) > (120 if key == "model" else 300) or any(ord(c) < 32 for c in text):
                raise StoreError("bad_setting", "That %s value is not valid." % key.replace("_", " "))
            clean[key] = text
        elif key in ("allow_remote", "demo_fallback", "lessons", "autosave"):
            clean[key] = _bool(value, key.replace("_", " ").capitalize())
        elif key == "budget_minutes":
            clean[key] = _int_in(value, 0, 960, "Budget")
        elif key == "chunk_size":
            clean[key] = _int_in(value, 1, 20, "Batch size")
        elif key == "timeout_s":
            clean[key] = _int_in(value, 10, 1800, "Timeout")
        elif key == "checkin_days":
            clean[key] = _int_in(value, 1, 60, "Check-in days")
        elif key == "temperature":
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise StoreError("bad_setting", "Temperature must be a number.") from None
            if not 0.0 <= number <= 1.0:
                raise StoreError("bad_setting", "Temperature must be between 0 and 1.")
            clean[key] = round(number, 2)
        elif key == "date_order":
            if value not in ("dmy", "mdy"):
                raise StoreError("bad_setting", "Date order must be day-first or month-first.")
            clean[key] = value
        elif key == "never_skip":
            clean[key] = _words(value)
    return clean


def public_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Settings for the browser: the API key is never sent back."""
    shown = {k: v for k, v in settings.items() if k != "api_key"}
    shown["api_key_set"] = bool(settings.get("api_key"))
    return shown


class Store:
    def __init__(self, data_dir: str):
        self.dir = os.path.abspath(data_dir)
        self.history_dir = os.path.join(self.dir, "history")
        os.makedirs(self.history_dir, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def _settings_path(self) -> str:
        return os.path.join(self.dir, "settings.json")

    def get_settings(self) -> Dict[str, Any]:
        """Saved settings over the defaults. A bad hand-edited value is ignored, not fatal."""
        with self._lock:
            merged = copy.deepcopy(DEFAULTS)
            try:
                with open(self._settings_path, encoding="utf-8") as handle:
                    saved = json.load(handle)
                if not isinstance(saved, dict):
                    raise ValueError("settings must be an object")
            except FileNotFoundError:
                return merged
            except ValueError:
                try:
                    os.replace(self._settings_path, self._settings_path + ".bad")
                except OSError:
                    pass
                return merged
            for key, value in saved.items():
                if key not in DEFAULTS:
                    continue
                try:
                    merged.update(validate_settings({key: value}))
                except StoreError:
                    continue
            return merged

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a partial update. `api_key: None` means 'leave it as it is'."""
        clean = validate_settings({k: v for k, v in patch.items() if not (k == "api_key" and v is None)})
        with self._lock:
            current = self.get_settings()
            current.update(clean)
            atomic_write_text(self._settings_path, json.dumps(current, indent=2, ensure_ascii=False) + "\n")
            return current

    def _path(self, sid: str) -> str:
        if not isinstance(sid, str) or not SID_RE.match(sid):
            raise StoreError("bad_id", "That list id is not valid.")
        return os.path.join(self.history_dir, sid + ".json")

    def _write(self, sid: str, result: dict) -> None:
        atomic_write_text(self._path(sid), json.dumps(result, indent=1, ensure_ascii=False) + "\n")

    def _sids(self) -> List[str]:
        try:
            names = os.listdir(self.history_dir)
        except FileNotFoundError:
            return []
        return sorted((n[:-5] for n in names if n.endswith(".json") and SID_RE.match(n[:-5])), reverse=True)

    def _load(self, sid: str) -> Optional[dict]:
        path = self._path(sid)  # raises bad_id; kept outside the try because StoreError is a ValueError
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):  # missing, unreadable or half-edited by hand
            return None
        return data if isinstance(data, dict) and isinstance(data.get("tasks"), list) else None

    def save_result(self, result: dict) -> str:
        with self._lock:
            sid = new_sid()
            while os.path.exists(self._path(sid)):
                sid = new_sid()
            result["sid"] = sid
            self._write(sid, result)
            for old in self._sids()[MAX_HISTORY:]:
                try:
                    os.unlink(self._path(old))
                except OSError:
                    pass
            return sid

    def get_result(self, sid: str) -> dict:
        with self._lock:
            result = self._load(sid)
        if result is None:
            raise StoreError("not_found", "That list is not in your history.")
        result["sid"] = sid
        return result

    def delete_result(self, sid: str) -> None:
        with self._lock:
            try:
                os.unlink(self._path(sid))
            except FileNotFoundError:
                raise StoreError("not_found", "That list is not in your history.") from None

    def list_history(self) -> List[dict]:
        rows = []
        with self._lock:
            for sid in self._sids():
                result = self._load(sid)
                if result is None:
                    continue
                stats = result.get("stats") or {}
                rows.append(
                    {
                        "sid": sid,
                        "created": result.get("created", ""),
                        "total": stats.get("total", len(result["tasks"])),
                        "counts": stats.get("counts", {}),
                        "skipped": stats.get("skipped", 0),
                        "minutes_skipped": stats.get("minutes_skipped", 0),
                        "engine": (result.get("engine") or {}).get("kind", ""),
                        "model": (result.get("engine") or {}).get("model", ""),
                        "preview": [clip(t.get("text", ""), 60) for t in result["tasks"][:3]],
                    }
                )
        return rows

    def _update(self, sid: str, fn) -> dict:
        with self._lock:
            result = self.get_result(sid)
            try:
                fn(result)
            except ResultError as err:
                raise StoreError(err.code, err.message) from None
            self._write(sid, result)
            return result

    def move(self, sid: str, tid: str, bucket: Optional[str], today: dt.date, checkin_days: int) -> dict:
        return self._update(sid, lambda r: move_task(r, tid, bucket, today, checkin_days))

    def outcome(self, sid: str, tid: str, outcome: Optional[str]) -> dict:
        return self._update(sid, lambda r: mark_outcome(r, tid, outcome))

    def set_budget(self, sid: str, budget: int, today: dt.date, checkin_days: int) -> dict:
        return self._update(sid, lambda r: rebudget(r, budget, today, checkin_days))

    def forget_history(self) -> int:
        removed = 0
        with self._lock:
            for sid in self._sids():
                try:
                    os.unlink(self._path(sid))
                    removed += 1
                except OSError:
                    pass
        return removed

    def _all_results(self, limit: Optional[int] = None) -> List[dict]:
        results = []
        with self._lock:
            for sid in self._sids()[:limit]:
                result = self._load(sid)
                if result is not None:
                    result["sid"] = sid
                    results.append(result)
        return results

    def checkins(self, today: dt.date) -> dict:
        due: List[dict] = []
        waiting: List[dict] = []
        resolved: List[dict] = []
        total_skipped = minutes_skipped = regrets = 0
        for result in self._all_results():
            for item in result["tasks"]:
                if display_bucket(item) not in SKIP:
                    continue
                total_skipped += 1
                minutes_skipped += int(item.get("minutes", 0))
                row = {
                    "sid": result["sid"],
                    "tid": item["id"],
                    "text": item["text"],
                    "bucket": display_bucket(item),
                    "why": item.get("why", ""),
                    "if_ignored": item.get("if_ignored", ""),
                    "minutes": item.get("minutes", 0),
                    "check_on": item.get("check_on") or "",
                    "outcome": item.get("outcome"),
                    "sorted_on": result.get("today", ""),
                    "outcome_at": item.get("outcome_at", ""),
                }
                if item.get("outcome"):
                    resolved.append(row)
                    regrets += 1 if item["outcome"] == "regret" else 0
                    continue
                try:
                    when = dt.date.fromisoformat(row["check_on"])
                except ValueError:
                    when = today + dt.timedelta(days=1)
                row["days_over"] = (today - when).days
                (due if when <= today else waiting).append(row)
        due.sort(key=lambda r: (r["check_on"], r["sid"]))
        waiting.sort(key=lambda r: (r["check_on"], r["sid"]))
        resolved.sort(key=lambda r: r["outcome_at"], reverse=True)
        resolved_count = len(resolved)
        return {
            "due": due,
            "waiting": waiting,
            "resolved": resolved[:40],
            "stats": {
                "skipped": total_skipped,
                "minutes_skipped": minutes_skipped,
                "resolved": resolved_count,
                "regrets": regrets,
                "regret_rate": (regrets / resolved_count) if resolved_count else None,
                "due": len(due),
                "waiting": len(waiting),
                "lists": len(self._sids()),
            },
        }

    def lessons(self, limit: int = 6) -> List[dict]:
        """The person's own corrections, newest first, as examples for the next prompt."""
        ups: List[dict] = []
        downs: List[dict] = []
        seen = set()
        for result in self._all_results(limit=120):
            for item in result["tasks"]:
                key = norm_key(item["text"])
                if key in seen:
                    continue
                shown = display_bucket(item)
                user = item.get("user_bucket")
                if item.get("outcome") == "regret" and shown in SKIP:
                    ups.append({"text": item["text"], "should_be": LATER, "reason": "they regretted skipping it"})
                elif user in KEEP and item["bucket"] in SKIP:
                    ups.append({"text": item["text"], "should_be": user, "reason": "they pulled it back onto the list"})
                elif user in SKIP and item["bucket"] in KEEP:
                    downs.append({"text": item["text"], "should_be": user, "reason": "they prefer to skip tasks like this"})
                else:
                    continue
                seen.add(key)
        # Mostly "you were too eager to skip" lessons, but keep room for a couple of the opposite kind.
        take_up = min(len(ups), limit - min(len(downs), 2))
        take_down = min(len(downs), limit - take_up)
        return ups[:take_up] + downs[:take_down]
