"""A small, read-only GitHub REST client (standard library only).

Ghosted never writes to GitHub. This class only sends GET requests, and the test-suite
fails if any other HTTP method ever reaches the (fake) GitHub server.
"""
import json
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from . import net

API_BASE = "https://api.github.com"


class GitHubError(Exception):
    """Anything that went wrong talking to GitHub. The message is safe to show to the user."""


class NotFound(GitHubError):
    pass


class AuthError(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, message, reset=None):
        GitHubError.__init__(self, message)
        self.reset = reset


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class GitHubClient(object):
    def __init__(self, token=None, api_base=None, timeout=20, retries=2, backoff=1.0):
        self.token = token or None
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.remaining = None
        self.limit = None
        self.reset = None
        self.request_count = 0

    # -- plumbing ---------------------------------------------------------------------
    def _headers(self):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ghosted-local/1.0",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def _note_limits(self, headers):
        remaining = _to_int(headers.get("x-ratelimit-remaining"))
        if remaining is not None:
            self.remaining = remaining
        limit = _to_int(headers.get("x-ratelimit-limit"))
        if limit is not None:
            self.limit = limit
        reset = _to_int(headers.get("x-ratelimit-reset"))
        if reset is not None:
            self.reset = reset

    def _rate_message(self, reset):
        when = ""
        if reset:
            try:
                stamp = datetime.fromtimestamp(reset, timezone.utc).strftime("%H:%M")
                when = " It resets at %s UTC." % stamp
            except (OverflowError, OSError, ValueError):
                pass
        hint = ""
        if not self.token:
            hint = " Adding a GitHub token raises the limit from 60 to 5,000 requests per hour."
        return "GitHub's API rate limit is used up.%s%s" % (when, hint)

    def _translate(self, exc):
        message = ""
        try:
            message = str(json.loads(exc.body).get("message", ""))
        except (ValueError, AttributeError):
            message = (exc.body or "")[:120]
        lowered = message.lower()
        if exc.status == 401:
            return AuthError("GitHub rejected the token (401). Check it, or leave the field blank.")
        if exc.status in (403, 429):
            if exc.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in lowered \
                    or "retry-after" in exc.headers:
                reset = _to_int(exc.headers.get("x-ratelimit-reset"))
                return RateLimited(self._rate_message(reset), reset)
            return AuthError("GitHub refused access (403): %s" % (message or "forbidden"))
        if exc.status == 404:
            return NotFound("GitHub says that repository was not found. "
                            "If it is private, add a token that can read it.")
        if exc.status == 409:
            return GitHubError("That repository is empty.")
        if exc.status == 451:
            return GitHubError("GitHub blocked access to this repository for legal reasons (451).")
        return GitHubError("GitHub returned HTTP %d%s" % (exc.status, (": " + message) if message else ""))

    def _get(self, path, params=None):
        url = self.api_base + path
        if params:
            url += "?" + urlencode(params)
        attempt = 0
        while True:
            self.request_count += 1
            try:
                _status, headers, raw = net.request("GET", url, headers=self._headers(),
                                                    timeout=self.timeout)
            except net.HttpError as exc:
                self._note_limits(exc.headers)
                if exc.status in (500, 502, 503, 504) and attempt < self.retries:
                    attempt += 1
                    time.sleep(self.backoff * attempt)
                    continue
                raise self._translate(exc)
            except net.NetError as exc:
                if attempt < self.retries:
                    attempt += 1
                    time.sleep(self.backoff * attempt)
                    continue
                raise GitHubError("Could not reach GitHub (%s). Check your connection." % exc)
            self._note_limits(headers)
            try:
                return json.loads(raw.decode("utf-8")), headers
            except ValueError:
                raise GitHubError("GitHub sent back something that was not JSON.")

    def paged(self, path, params=None, max_pages=1, per_page=100):
        """Walk a paginated list endpoint. Returns (items, maybe_truncated)."""
        items = []
        for page in range(1, max_pages + 1):
            query = dict(params or {})
            query["per_page"] = per_page
            query["page"] = page
            data, _headers = self._get(path, query)
            if not isinstance(data, list):
                raise GitHubError("GitHub returned an unexpected response for %s." % path)
            items.extend(data)
            if len(data) < per_page:
                return items, False
        return items, True

    # -- endpoints --------------------------------------------------------------------
    def repo(self, full_name):
        data, _headers = self._get("/repos/%s" % quote(full_name, safe="/"))
        if not isinstance(data, dict):
            raise GitHubError("GitHub returned an unexpected response for that repository.")
        return data

    def open_issues(self, full_name, order="oldest", max_pages=10):
        """Open issues AND pull requests (GitHub mixes them), oldest or newest first."""
        direction = "desc" if order == "newest" else "asc"
        return self.paged("/repos/%s/issues" % quote(full_name, safe="/"),
                          {"state": "open", "sort": "created", "direction": direction}, max_pages)

    def merged_prs(self, full_name, max_pages=5):
        items, truncated = self.paged("/repos/%s/pulls" % quote(full_name, safe="/"),
                                      {"state": "closed", "sort": "updated", "direction": "desc"},
                                      max_pages)
        return [pr for pr in items if pr.get("merged_at")], truncated

    def commits(self, full_name, branch=None, max_pages=5):
        params = {"sha": branch} if branch else {}
        try:
            return self.paged("/repos/%s/commits" % quote(full_name, safe="/"), params, max_pages)
        except GitHubError as exc:
            if "empty" in str(exc):
                return [], False
            raise

    def releases(self, full_name):
        items, _truncated = self.paged("/repos/%s/releases" % quote(full_name, safe="/"),
                                       {}, 1, per_page=30)
        return items

    def timeline(self, full_name, number):
        items, _truncated = self.paged(
            "/repos/%s/issues/%d/timeline" % (quote(full_name, safe="/"), int(number)), {}, 2)
        return items
