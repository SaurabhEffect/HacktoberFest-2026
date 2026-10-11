# Tiny cached JSON-over-HTTP helper shared by every upstream data source.

from __future__ import annotations
import contextlib
import contextvars
import os
import threading
import time
from typing import Any
import requests
USER_AGENT = os.getenv(
    "APP_USER_AGENT",
    "this-week-outside/1.0 (open-source field guide; Hacktoberfest 2026)",
)
_cache: dict[tuple, tuple[float, Any]] = {}
_lock = threading.Lock()
_MAX_ENTRIES = 512

FORCE_DEMO = os.getenv("TWO_DEMO", "").strip().lower() in ("1", "true", "yes", "on")
_demo: contextvars.ContextVar[bool] = contextvars.ContextVar("two_demo", default=False)

def demo_active() -> bool:
    return FORCE_DEMO or _demo.get()

@contextlib.contextmanager
def demo_scope(on: bool = True):
    token = _demo.set(bool(on))
    try:
        yield
    finally:
        _demo.reset(token)

class UpstreamError(RuntimeError):
    """An external data source failed or returned something we can't use."""

def get_json(url: str, params: dict | None = None, ttl: int = 900, timeout: float = 12) -> Any:
    """GET ``url`` and return parsed JSON, cached in memory for ``ttl`` seconds."""
    if demo_active():
        from . import demo
        return demo.get_json(url, params)
    key = (url, tuple(sorted((params or {}).items())))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        host = url.split("/")[2] if "//" in url else url
        raise UpstreamError(f"{host} didn't answer ({type(exc).__name__}). Try again in a moment.") from exc
    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            # Drop expired entries first; if still full, drop the oldest half.
            for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
                _cache.pop(k, None)
            if len(_cache) >= _MAX_ENTRIES:
                for k in sorted(_cache, key=lambda k: _cache[k][0])[: _MAX_ENTRIES // 2]:
                    _cache.pop(k, None)
        _cache[key] = (now + ttl, data)
    return data

def clear_cache() -> None:
    with _lock:
        _cache.clear()
