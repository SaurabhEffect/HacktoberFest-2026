"""Talk to a local model server. Two dialects are supported:

* ``ollama``  - Ollama's native /api/chat (the default; works with any model you have pulled)
* ``openai``  - any OpenAI-compatible server: LM Studio, llama.cpp's server, vLLM, ...

Only the standard library is used. Loopback addresses never go through a proxy.
"""
import json

from . import net
from .errors import LLMConnectionError, LLMError, LLMFormatError, LLMTimeout

BACKENDS = ("ollama", "openai", "rules")
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OPENAI_URL = "http://localhost:1234/v1"

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["valid", "fixed", "needs_info"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reason", "reply"],
}

# Ollama servers that rejected a JSON-schema `format` (older versions): remember per base URL.
_NO_SCHEMA = set()
# OpenAI-compatible servers that rejected `response_format`.
_NO_RESPONSE_FORMAT = set()


def normalize_base_url(backend, base_url):
    """Clean up what the user typed into the 'server address' box."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_OLLAMA_URL if backend == "ollama" else DEFAULT_OPENAI_URL
    if "://" not in url:
        url = "http://" + url
    if backend == "ollama":
        for suffix in ("/api/chat", "/api/tags", "/api"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        return url.rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def model_available(models, wanted):
    """Ollama resolves a bare name to ':latest', so mirror that."""
    if not wanted:
        return False
    return wanted in models or (":" not in wanted and wanted + ":latest" in models)


def sort_models(names):
    """Chat-capable models first, Gemma on top (it is this week's partner), then alphabetical."""
    chat = [n for n in names if "embed" not in n.lower()]
    return sorted(chat, key=lambda n: (0 if "gemma" in n.lower() else 1, n.lower()))


class LLMClient(object):
    def __init__(self, backend="ollama", base_url=None, model="", timeout=180):
        if backend not in ("ollama", "openai"):
            raise ValueError("unknown model backend: %r" % (backend,))
        self.backend = backend
        self.base_url = normalize_base_url(backend, base_url)
        self.model = (model or "").strip()
        self.timeout = timeout

    @property
    def label(self):
        return "%s · %s" % ("Ollama" if self.backend == "ollama" else "OpenAI-compatible",
                                 self.model or "?")

    @property
    def pretty_backend(self):
        return "Ollama" if self.backend == "ollama" else "the model server"

    # -- errors -----------------------------------------------------------------------
    def _connection_help(self):
        if self.backend == "ollama":
            return ("Can't reach Ollama at %s. Is it running? Start it with `ollama serve` "
                    "(or open the Ollama app)." % self.base_url)
        return ("Can't reach the model server at %s. Is it running, with its local server "
                "turned on?" % self.base_url)

    def _translate_net(self, exc):
        if exc.timeout:
            return LLMTimeout(
                "The model took longer than %d seconds to answer. Try a smaller model, or raise "
                "the timeout in Settings." % self.timeout)
        return LLMConnectionError(self._connection_help())

    @staticmethod
    def _error_text(exc):
        try:
            data = json.loads(exc.body)
        except (ValueError, TypeError):
            return (exc.body or "").strip()[:200]
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            err = err.get("message")
        return str(err or "")[:200]

    def _translate_http(self, exc):
        text = self._error_text(exc)
        lowered = text.lower()
        if exc.status == 404 and "model" in lowered and "not found" in lowered:
            hint = ""
            if self.backend == "ollama":
                hint = " Pull it first: ollama pull %s" % self.model
            return LLMError("The model '%s' was not found.%s" % (self.model, hint))
        if exc.status == 404:
            return LLMError("The server answered 404 at %s. Is this really %s?"
                            % (self.base_url, "an Ollama server" if self.backend == "ollama"
                               else "an OpenAI-compatible server"))
        return LLMError("The model server returned an error (HTTP %d)%s"
                        % (exc.status, ": " + text if text else "."))

    # -- listing models ---------------------------------------------------------------
    def list_models(self, timeout=6):
        try:
            if self.backend == "ollama":
                _s, _h, raw = net.request("GET", self.base_url + "/api/tags", timeout=timeout)
                data = json.loads(raw.decode("utf-8"))
                names = [m.get("name") or m.get("model") for m in data.get("models", [])
                         if isinstance(m, dict)]
            else:
                _s, _h, raw = net.request("GET", self.base_url + "/models", timeout=timeout)
                data = json.loads(raw.decode("utf-8"))
                names = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        except net.HttpError as exc:
            raise self._translate_http(exc)
        except net.NetError as exc:
            raise self._translate_net(exc)
        except (ValueError, AttributeError, TypeError):
            raise LLMError("That address answered, but not like %s would."
                           % ("Ollama" if self.backend == "ollama" else "an OpenAI-compatible server"))
        return sort_models([n for n in names if n])

    # -- chatting ---------------------------------------------------------------------
    def chat(self, system, user):
        """One round trip. Returns the model's raw text (which should be JSON)."""
        if not self.model:
            raise LLMError("No model selected.")
        if self.backend == "ollama":
            return self._chat_ollama(system, user)
        return self._chat_openai(system, user)

    def _post(self, url, payload):
        try:
            _s, _h, raw = net.request("POST", url, body=payload, timeout=self.timeout)
        except net.NetError as exc:
            raise self._translate_net(exc)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise LLMFormatError("The model server's response was not JSON.")

    def _chat_ollama(self, system, user):
        url = self.base_url + "/api/chat"
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": 0.2, "num_ctx": 4096, "num_predict": 600},
            "format": "json" if self.base_url in _NO_SCHEMA else VERDICT_SCHEMA,
        }
        try:
            data = self._post(url, payload)
        except net.HttpError as exc:
            if exc.status == 400 and isinstance(payload["format"], dict):
                _NO_SCHEMA.add(self.base_url)  # older Ollama: fall back to plain JSON mode
                payload["format"] = "json"
                try:
                    data = self._post(url, payload)
                except net.HttpError as exc2:
                    raise self._translate_http(exc2)
            else:
                raise self._translate_http(exc)
        message = data.get("message") if isinstance(data, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LLMFormatError("Ollama's answer had no message text.")
        return content

    def _chat_openai(self, system, user):
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 600,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if self.base_url not in _NO_RESPONSE_FORMAT:
            payload["response_format"] = {"type": "json_object"}
        try:
            data = self._post(url, payload)
        except net.HttpError as exc:
            if exc.status == 400 and "response_format" in payload:
                _NO_RESPONSE_FORMAT.add(self.base_url)
                payload.pop("response_format", None)
                try:
                    data = self._post(url, payload)
                except net.HttpError as exc2:
                    raise self._translate_http(exc2)
            else:
                raise self._translate_http(exc)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMFormatError("The server's answer had no message text.")
        if not isinstance(content, str):
            raise LLMFormatError("The server's answer had no message text.")
        return content
