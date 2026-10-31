"""A tiny HTTP helper built on urllib, so Ghosted needs no third-party packages."""
import http.client
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# Local model servers (Ollama, LM Studio, ...) must never be sent through a corporate proxy.
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_default_opener = urllib.request.build_opener()


class HttpError(Exception):
    """The server answered with a 4xx/5xx status."""

    def __init__(self, status, body="", headers=None, url=""):
        Exception.__init__(self, "HTTP %s from %s" % (status, url))
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.url = url


class NetError(Exception):
    """The request never got an HTTP answer (refused, DNS failure, timeout, ...)."""

    def __init__(self, message, timeout=False):
        Exception.__init__(self, message)
        self.timeout = timeout


def _opener_for(url):
    host = (urlparse(url).hostname or "").lower()
    return _direct_opener if host in _LOOPBACK else _default_opener


def request(method, url, headers=None, body=None, timeout=20):
    """Send one request. Returns (status, lowercase-header dict, raw bytes).

    Raises HttpError for 4xx/5xx answers and NetError for anything else that goes wrong.
    """
    send_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        send_headers.setdefault("Content-Type", "application/json")
    try:
        req = urllib.request.Request(url, data=data, headers=send_headers, method=method)
    except ValueError as exc:
        raise NetError("bad URL: %s" % exc)
    try:
        with _opener_for(url).open(req, timeout=timeout) as resp:
            raw = resp.read()
            hdrs = dict((k.lower(), v) for k, v in resp.headers.items())
            return resp.status, hdrs, raw
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # pragma: no cover - best effort only
            raw = b""
        hdrs = dict((k.lower(), v) for k, v in (exc.headers.items() if exc.headers else []))
        raise HttpError(exc.code, raw.decode("utf-8", "replace"), hdrs, url)
    except socket.timeout:
        raise NetError("timed out after %ss" % timeout, timeout=True)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, socket.timeout) or "timed out" in str(reason).lower()
        raise NetError(str(reason), timeout=timed_out)
    except (http.client.HTTPException, OSError, ValueError) as exc:
        text = str(exc) or exc.__class__.__name__
        raise NetError(text, timeout="timed out" in text.lower())
