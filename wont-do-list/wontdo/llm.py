"""Talk to a local model server.

Supported out of the box, using only the standard library:

* Ollama (``/api/chat`` with a JSON-schema ``format``)
* anything OpenAI-compatible: llama.cpp's ``llama-server``, LM Studio, vLLM, ...
  (``/v1/chat/completions`` with a ``response_format`` schema)

Small models still get JSON wrong now and then, so this module also holds the
tolerant parsing that recovers every complete verdict from a messy reply.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .constants import BUCKET_ALIASES, DEFAULT_MINUTES, MINUTES, RISKS
from .util import clip, is_local_url

OLLAMA_URL = "http://127.0.0.1:11434"
AUTO_OPENAI_URLS = ("http://127.0.0.1:8080", "http://127.0.0.1:1234")

PREFERRED_FAMILIES = (
    "gemma3", "qwen2.5", "qwen3", "llama3.2", "llama3.1", "phi4", "phi3", "mistral", "gemma2", "smollm", "granite",
)
NOT_CHAT_MODELS = re.compile(r"embed|bge|minilm|nomic|e5-|gte-|rerank|whisper|llava|vision|clip|tts", re.I)
REASONING_MODELS = re.compile(r"qwen3|deepseek-r1|magistral|smollm3", re.I)


class LLMError(Exception):
    """A model call failed. `kind` says how, so callers can react sensibly."""

    def __init__(self, kind: str, message: str, status: Optional[int] = None, hint: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.hint = hint

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "hint": self.hint}


@dataclass
class ModelInfo:
    name: str
    params: str = ""
    size_bytes: int = 0
    family: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "params": self.params, "size_bytes": self.size_bytes, "family": self.family}


@dataclass
class Backend:
    """What `detect_backend` found."""

    reachable: bool = False
    provider: str = ""
    base_url: str = ""
    models: List[ModelInfo] = field(default_factory=list)
    model: str = ""
    model_missing: bool = False
    error: str = ""
    hint: str = ""
    version: str = ""

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "provider": self.provider,
            "base_url": self.base_url,
            "models": [m.to_dict() for m in self.models],
            "model": self.model,
            "model_missing": self.model_missing,
            "error": self.error,
            "hint": self.hint,
            "version": self.version,
        }


_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_DEFAULT_OPENER = urllib.request.build_opener()


def _error_message(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return clip(body.decode("utf-8", "replace"), 300)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        err = err.get("message") or err.get("type")
    return clip(err or data, 300)


def http_json(
    method: str,
    url: str,
    payload: Optional[dict] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Any:
    """One JSON request. Raises LLMError with a useful `kind` on any failure."""
    data = None
    head = {"Accept": "application/json", "User-Agent": "wontdo/%s" % __version__}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        head["Content-Type"] = "application/json"
    if headers:
        head.update(headers)
    request = urllib.request.Request(url, data=data, method=method, headers=head)
    opener = _LOCAL_OPENER if is_local_url(url) else _DEFAULT_OPENER
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as err:
        raise LLMError("http", _error_message(err.read()) or "HTTP %d" % err.code, status=err.code) from None
    except urllib.error.URLError as err:
        if isinstance(err.reason, (socket.timeout, TimeoutError)):
            raise LLMError("timeout", "The model server did not answer in time.") from None
        raise LLMError("unreachable", "Cannot reach %s (%s)." % (url, err.reason)) from None
    except (socket.timeout, TimeoutError):
        raise LLMError("timeout", "The model took too long to answer.") from None
    except (http.client.HTTPException, OSError) as err:
        raise LLMError("unreachable", "Connection to %s failed (%s)." % (url, err)) from None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise LLMError("format", "The server did not return JSON.") from None


def normalize_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    for suffix in ("/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def _parse_params(text: str) -> float:
    """'4.3B' -> 4.3, '270M' -> 0.27, unknown -> 0."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*([bm])\b", text.lower())
    if not match:
        return 0.0
    value = float(match.group(1))
    return value / 1000.0 if match.group(2) == "m" else value


class OllamaClient:
    provider = "ollama"

    def __init__(self, base_url: str, model: str = "", timeout: float = 300.0, temperature: float = 0.1, seed: int = 7):
        self.base = normalize_base(base_url)
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.seed = seed
        self.last_raw = ""
        self._think_off = bool(REASONING_MODELS.search(model))
        self._schema_format = True

    def version(self, timeout: float = 3.0) -> str:
        data = http_json("GET", self.base + "/api/version", timeout=timeout)
        return str(data.get("version", "")) if isinstance(data, dict) else ""

    def list_models(self, timeout: float = 3.0) -> List[ModelInfo]:
        data = http_json("GET", self.base + "/api/tags", timeout=timeout)
        models = []
        for row in (data.get("models") or []) if isinstance(data, dict) else []:
            name = row.get("name") or row.get("model")
            if not name:
                continue
            details = row.get("details") or {}
            models.append(
                ModelInfo(
                    name=name,
                    params=str(details.get("parameter_size", "")),
                    size_bytes=int(row.get("size") or 0),
                    family=str(details.get("family", "")),
                )
            )
        return models

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int) -> str:
        for _attempt in range(4):
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "stream": False,
                "format": schema if self._schema_format else "json",
                "options": {
                    "temperature": self.temperature,
                    "seed": self.seed,
                    "num_ctx": 4096,
                    "num_predict": max_tokens,
                },
                "keep_alive": "15m",
            }
            if self._think_off:
                payload["think"] = False
            try:
                data = http_json("POST", self.base + "/api/chat", payload, timeout=self.timeout)
            except LLMError as err:
                text = err.message.lower()
                if err.status == 404 and "not found" in text:
                    raise LLMError(
                        "model_missing",
                        "The model “%s” is not installed." % self.model,
                        status=404,
                        hint="Run: ollama pull %s" % self.model,
                    ) from None
                if err.status == 400 and "think" in text and self._think_off:
                    self._think_off = False
                    continue
                if err.status == 400 and self._schema_format:
                    self._schema_format = False
                    continue
                raise
            message = (data.get("message") or {}) if isinstance(data, dict) else {}
            content = message.get("content") or ""
            self.last_raw = content
            if not content.strip():
                if message.get("thinking"):
                    raise LLMError(
                        "format",
                        "The model only produced reasoning text and no answer.",
                        hint="Try a non-reasoning model such as gemma3:4b or qwen2.5:3b.",
                    )
                raise LLMError("format", "The model returned an empty answer.")
            return content
        raise LLMError("http", "The model server rejected the request.")


class OpenAIClient:
    """Any server that speaks the OpenAI chat API: llama-server, LM Studio, vLLM, Ollama's /v1."""

    provider = "openai"

    def __init__(
        self,
        base_url: str,
        model: str = "",
        timeout: float = 300.0,
        temperature: float = 0.1,
        seed: int = 7,
        api_key: str = "",
    ):
        self.base = normalize_base(base_url)
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.seed = seed
        self.api_key = api_key
        self.last_raw = ""
        self._modes = ["json_schema", "json_object", "none"]

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer %s" % self.api_key} if self.api_key else {}

    def version(self, timeout: float = 3.0) -> str:
        return ""

    def list_models(self, timeout: float = 3.0) -> List[ModelInfo]:
        data = http_json("GET", self.base + "/v1/models", headers=self._headers(), timeout=timeout)
        rows = []
        if isinstance(data, dict):
            rows = data.get("data") or data.get("models") or []
        models = []
        for row in rows:
            name = (row.get("id") or row.get("name") or row.get("model")) if isinstance(row, dict) else None
            if name:
                models.append(ModelInfo(name=str(name)))
        return models

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int) -> str:
        while True:
            mode = self._modes[0]
            payload: Dict[str, Any] = {
                "model": self.model or "local",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": self.temperature,
                "max_tokens": max_tokens,
                "seed": self.seed,
                "stream": False,
            }
            if mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "triage", "strict": True, "schema": schema},
                }
            elif mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            try:
                data = http_json(
                    "POST", self.base + "/v1/chat/completions", payload, headers=self._headers(), timeout=self.timeout
                )
            except LLMError as err:
                if err.status in (400, 422, 500) and len(self._modes) > 1 and mode != "none":
                    self._modes.pop(0)  # this server does not support that response_format
                    continue
                if err.status == 404 and "model" in err.message.lower():
                    raise LLMError(
                        "model_missing", "The model “%s” is not available on this server." % self.model,
                        status=404, hint="Pick a model from the list in Settings.",
                    ) from None
                raise
            try:
                content = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                raise LLMError("format", "The server's reply had no message.") from None
            self.last_raw = content
            if not content.strip():
                raise LLMError("format", "The model returned an empty answer.")
            return content


def model_score(model: ModelInfo) -> float:
    name = model.name.lower()
    score = 0.0
    for index, family in enumerate(PREFERRED_FAMILIES):
        if family in name:
            score += len(PREFERRED_FAMILIES) - index
            break
    size = _parse_params(model.params) or _parse_params(name)
    if size:
        if 1.5 <= size <= 9:
            score += 5
        elif size < 1.5:
            score += 1
        elif size <= 14:
            score += 2
        else:
            score -= 3  # too slow on a laptop CPU
    return score


def choose_model(models: Sequence[ModelInfo], preferred: str = "") -> str:
    """The person's choice if they made one, otherwise the best small chat model installed."""
    if preferred:
        return preferred
    usable = [m for m in models if not NOT_CHAT_MODELS.search(m.name)] or list(models)
    if not usable:
        return ""
    best = usable[0]
    for candidate in usable[1:]:
        if model_score(candidate) > model_score(best):
            best = candidate
    return best.name


def _candidates(settings: dict) -> List[Tuple[str, str]]:
    provider = settings.get("provider", "auto")
    base = normalize_base(settings.get("base_url", ""))
    if base:
        return [(p, base) for p in ("ollama", "openai") if provider in ("auto", p)]
    found: List[Tuple[str, str]] = []
    if provider in ("auto", "ollama"):
        found.append(("ollama", OLLAMA_URL))
    if provider in ("auto", "openai"):
        found.extend(("openai", url) for url in AUTO_OPENAI_URLS)
    return found


def detect_backend(settings: dict, timeout: float = 1.5) -> Backend:
    """Find a model server and the model to use, without sending any task text."""
    errors: List[LLMError] = []
    for provider, url in _candidates(settings):
        if not is_local_url(url) and not settings.get("allow_remote"):
            errors.append(
                LLMError(
                    "remote_blocked",
                    "%s is not on this machine, so your list would leave it." % url,
                    hint="Turn on “Allow a model server on another machine” in Settings if that is what you want.",
                )
            )
            continue
        client = _make_raw_client(provider, url, settings)
        try:
            models = client.list_models(timeout=timeout)
        except LLMError as err:
            errors.append(err)
            continue
        version = ""
        if provider == "ollama":
            try:
                version = client.version(timeout=timeout)
            except LLMError:
                version = ""
        preferred = settings.get("model", "")
        chosen = choose_model(models, preferred)
        missing = bool(preferred) and preferred not in [m.name for m in models]
        error = hint = ""
        if not chosen:
            error = "A model server is running but has no models installed."
            hint = "Install one, for example: ollama pull gemma3:4b"
        elif missing:
            error = "The model “%s” is not installed on this server." % preferred
            if provider == "ollama":
                hint = "Run: ollama pull %s — or pick another model in Settings." % preferred
            else:
                hint = "Load it in your model server, or pick another model in Settings."
        return Backend(
            reachable=True,
            provider=provider,
            base_url=url,
            models=models,
            model=chosen,
            model_missing=missing or not chosen,
            error=error,
            hint=hint,
            version=version,
        )
    blocked = [e for e in errors if e.kind == "remote_blocked"]
    if blocked:
        message = blocked[0].message
        hint = blocked[0].hint
    elif normalize_base(settings.get("base_url", "")):
        message = "Cannot reach a model server at %s." % normalize_base(settings.get("base_url", ""))
        hint = "Check the address in Settings, and that the server is running."
    else:
        message = "No model server found on this machine (looked for Ollama, llama.cpp and LM Studio)."
        hint = "Start Ollama, then run: ollama pull gemma3:4b"
    return Backend(reachable=False, error=message, hint=hint)


def _make_raw_client(provider: str, url: str, settings: dict, model: str = ""):
    timeout = float(settings.get("timeout_s", 300))
    temperature = float(settings.get("temperature", 0.1))
    if provider == "ollama":
        return OllamaClient(url, model=model, timeout=timeout, temperature=temperature)
    return OpenAIClient(url, model=model, timeout=timeout, temperature=temperature, api_key=settings.get("api_key", ""))


def make_client(settings: dict, backend: Backend):
    """Build the client for a backend that `detect_backend` reported reachable."""
    if not backend.reachable or not backend.model:
        raise LLMError("unreachable", backend.error or "No model available.", hint=backend.hint)
    return _make_raw_client(backend.provider, backend.base_url, settings, model=backend.model)


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_MISSING = object()
_LIST_KEYS = ("tasks", "results", "items", "verdicts", "answers", "output")
_MAX_SCAN = 200_000


def _clean(text: str) -> str:
    text = _THINK.sub("", text).replace("<think>", "").replace("</think>", "")
    fence = _FENCE.search(text)
    if fence and ("{" in fence.group(1) or "[" in fence.group(1)):
        text = fence.group(1)
    return text.strip()[:_MAX_SCAN]


def _balanced(text: str, start: int, open_char: str, close_char: str) -> Optional[int]:
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return pos
    return None


def _loads_lenient(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        pass
    fixed = re.sub(r",\s*([}\]])", r"\1", text).replace("“", '"').replace("”", '"')
    try:
        return json.loads(fixed)
    except ValueError:
        return _MISSING


def _rows_from(value: Any) -> Optional[List[dict]]:
    if isinstance(value, list):
        rows = [row for row in value if isinstance(row, dict)]
        return rows or None
    if isinstance(value, dict):
        for key in _LIST_KEYS:
            if isinstance(value.get(key), list):
                return _rows_from(value[key])
        if value and all(str(k).strip().isdigit() and isinstance(v, dict) for k, v in value.items()):
            return [dict(v, id=int(str(k).strip())) for k, v in value.items()]
        if "bucket" in value:
            return [value]
    return None


def _scan_objects(text: str) -> List[dict]:
    """Recover every complete {...} that looks like a verdict from truncated or broken output."""
    rows: List[dict] = []
    skip_until = 0
    for match in re.finditer(r"\{", text):
        start = match.start()
        if start < skip_until:
            continue
        end = _balanced(text, start, "{", "}")
        if end is None:
            continue
        value = _loads_lenient(text[start : end + 1])
        if isinstance(value, dict) and "id" in value and "bucket" in value:
            rows.append(value)
            skip_until = end + 1
    return rows


def extract_rows(text: str) -> Tuple[List[dict], bool]:
    """Pull verdict rows out of a model reply. Returns (rows, salvaged)."""
    cleaned = _clean(text or "")
    value = _loads_lenient(cleaned)
    if value is _MISSING:
        for open_char, close_char in (("{", "}"), ("[", "]")):
            start = cleaned.find(open_char)
            if start >= 0:
                end = _balanced(cleaned, start, open_char, close_char)
                if end is not None:
                    value = _loads_lenient(cleaned[start : end + 1])
                    if value is not _MISSING:
                        break
    rows = _rows_from(value) if value is not _MISSING else None
    if rows:
        return rows, False
    rows = _scan_objects(cleaned)
    if rows:
        return rows, True
    raise LLMError("format", "The model's answer was not usable JSON.")


def coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        return int(value.strip().lstrip("#"))
    return None


def canon_bucket(value: Any) -> Optional[str]:
    key = re.sub(r"[^a-z]+", "_", str(value or "").strip().lower()).strip("_")
    return BUCKET_ALIASES.get(key)


def nearest_minutes(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_MINUTES
    text = str(value).lower()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return DEFAULT_MINUTES
    number = float(match.group(0))
    if re.search(r"\b(?:h|hr|hrs|hour|hours)\b", text):
        number *= 60
    return min(MINUTES, key=lambda m: (abs(m - number), m))


_RISK_WORDS = {
    "med": "medium", "moderate": "medium", "minor": "low", "minimal": "low", "critical": "high",
    "severe": "high", "urgent": "high", "": "low",
}


def normalize_verdict(raw: dict) -> Optional[dict]:
    """Coerce one raw row into the exact shape the rest of the app expects, or None if unusable."""
    bucket = canon_bucket(raw.get("bucket"))
    if bucket is None:
        return None
    risk = str(raw.get("risk", "")).strip().lower()
    if risk not in RISKS:
        risk = _RISK_WORDS.get(risk, "low")
    return {
        "bucket": bucket,
        "risk": risk,
        "minutes": nearest_minutes(raw.get("minutes")),
        "why": clip(raw.get("why") or "", 160),
        "if_ignored": clip(raw.get("if_ignored") or raw.get("consequence") or "", 160),
    }
