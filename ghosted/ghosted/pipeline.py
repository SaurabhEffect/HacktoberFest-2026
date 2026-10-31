"""The scan pipeline: validate a request, gather ghosted issues, dig for evidence, ask the model.

One scan runs at a time, in a background thread. Everything it learns is written to the store as
it goes, so the browser can watch progress and a cancelled or interrupted scan can be resumed.
"""
import re
import threading
import traceback
from urllib.parse import urlparse

from . import demo as demo_data
from .analyzer import analyze_issue
from .errors import LLMConnectionError, LLMError
from .evidence import EvidenceIndex, latest_release
from .github import GitHubClient, GitHubError
from .llm import BACKENDS, LLMClient, model_available, normalize_base_url
from .textutil import age_in_days, clean_markdown, now_utc, parse_ts, truncate

_URL_FORM = re.compile(
    r"^(?:https?://|git@)?(?:www\.)?github\.com[/:]([A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"([A-Za-z0-9._-]{1,100}?)(?:\.git)?(?:[/?#].*)?$", re.I)
_PLAIN_FORM = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9_.:/@+\-]{1,120}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_\-.=+/]{1,255}$")

ORDERS = ("oldest", "newest")


class Busy(Exception):
    """Another scan is already running."""


class ScanAbort(Exception):
    """Stop the scan with this (user-facing) message, keeping what has been learned so far."""


def parse_repo(text):
    """'owner/name', a github.com URL or an SSH remote -> 'owner/name'. Raises ValueError."""
    raw = (text or "").strip() if isinstance(text, str) else ""
    if not raw:
        raise ValueError("Type a repository as owner/name, for example ghost-town/forgotten-lib.")
    match = _URL_FORM.match(raw) or _PLAIN_FORM.match(raw)
    if not match:
        raise ValueError("That doesn't look like a GitHub repository. Use owner/name or a "
                         "github.com link.")
    owner, name = match.group(1), match.group(2)
    if name in (".", "..") or owner in (".", ".."):
        raise ValueError("That doesn't look like a GitHub repository.")
    return "%s/%s" % (owner, name)


def _int_field(body, key, default, low, high, label):
    value = body.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError("%s must be a number." % label)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number." % label)
    return max(low, min(high, number))


def validate_scan_request(body, defaults=None):
    """Clean a scan request. Returns (params, token). Raises ValueError with a friendly message."""
    defaults = defaults or {}
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object.")
    demo = bool(body.get("demo"))
    repo = demo_data.DEMO_REPO if demo else parse_repo(body.get("repo"))

    backend = str(body.get("backend") or defaults.get("backend") or "ollama").strip().lower()
    if backend not in BACKENDS:
        raise ValueError("Unknown engine '%s'." % truncate(backend, 20))

    base_url, model = "", ""
    if backend != "rules":
        base_url = normalize_base_url(backend, body.get("base_url") or defaults.get("base_url"))
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("The model server address must look like http://localhost:11434.")
        model = str(body.get("model") or defaults.get("model") or "").strip()
        if not model:
            raise ValueError("Choose a model first (or switch the engine to the rule-based preview).")
        if not _MODEL_NAME.match(model):
            raise ValueError("That model name has characters Ghosted doesn't expect.")

    order = str(body.get("order") or "oldest").strip().lower()
    if order not in ORDERS:
        raise ValueError("Order must be 'oldest' or 'newest'.")

    token = body.get("token")
    token = token.strip() if isinstance(token, str) else ""
    if token and not _TOKEN.match(token):
        raise ValueError("That doesn't look like a GitHub token. Leave it blank to go without.")

    params = {
        "repo": repo, "demo": demo, "backend": backend, "base_url": base_url, "model": model,
        "max_issues": _int_field(body, "max_issues", 20, 1, 200, "How many issues"),
        "min_age_days": _int_field(body, "min_age_days", 30, 0, 20000, "Minimum silence"),
        "order": order,
        "timeout": _int_field(body, "timeout", 180, 10, 900, "Timeout"),
    }
    return params, (token or None)


def engine_label(params):
    if params["backend"] == "rules":
        return "Rule-based preview (no model)"
    name = "Ollama" if params["backend"] == "ollama" else "OpenAI-compatible"
    return "%s · %s" % (name, params["model"])


def normalize_issue(raw, now):
    labels = []
    for label in raw.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if name:
            labels.append(str(name))
    return {
        "number": raw["number"],
        "title": truncate(str(raw.get("title") or "(no title)"), 300),
        "url": raw.get("html_url") or "",
        "author": (raw.get("user") or {}).get("login") or "",
        "created_at": raw.get("created_at"),
        "labels": labels,
        "body": clean_markdown(raw.get("body"), 2000),
        "age_days": age_in_days(raw.get("created_at"), now),
    }


def row_to_issue(row):
    return {k: row[k] for k in ("number", "title", "url", "author", "created_at", "labels",
                                "body", "age_days")}


def _earliest(values):
    stamps = [s for s in (parse_ts(v) for v in values) if s]
    return min(stamps).strftime("%Y-%m-%d") if stamps else ""


class ScanRunner(object):
    def __init__(self, store, github_api_base=None, env_token=None, github_kwargs=None):
        self.store = store
        self.github_api_base = github_api_base
        self.env_token = env_token
        self.github_kwargs = dict(github_kwargs or {})
        self._lock = threading.Lock()
        self._active = None
        self._cancel = {}
        self._threads = {}

    # -- public -----------------------------------------------------------------------
    def start(self, params, token=None):
        with self._lock:
            if self._active is not None:
                raise Busy("Another scan is still running. Wait for it to finish, or cancel it.")
            scan_id = self.store.create_scan(params["repo"], params["demo"], params,
                                             engine_label(params))
            self._begin(scan_id, self._run, (scan_id, params, token))
        return scan_id

    def retry(self, scan_id):
        """Resume a scan: read the issues that failed or were never reached."""
        with self._lock:
            if self._active is not None:
                raise Busy("Another scan is still running. Wait for it to finish, or cancel it.")
            scan = self.store.get_scan(scan_id)
            if scan is None:
                raise LookupError("scan not found")
            if not self.store.get_results(scan_id):
                raise ValueError("This scan never got as far as reading issues, so there is "
                                 "nothing to resume. Start it again instead.")
            if self.store.reset_unfinished(scan_id) == 0:
                raise ValueError("Every issue in this scan has already been read.")
            self.store.update_scan(scan_id, status="queued", stage="queued", error="",
                                   message="Resuming…", finished_at=None)
            self._begin(scan_id, self._resume, (scan_id,))
        return scan_id

    def cancel(self, scan_id):
        event = self._cancel.get(scan_id)
        if event is None:
            return False
        event.set()
        return True

    def is_active(self, scan_id=None):
        with self._lock:
            return self._active is not None and (scan_id is None or self._active == scan_id)

    def wait(self, scan_id, timeout=30):
        thread = self._threads.get(scan_id)
        if thread is not None:
            thread.join(timeout)

    def regenerate(self, result_id):
        """Re-ask the model about one issue. Blocks until it answers."""
        row = self.store.get_result(result_id)
        if row is None:
            raise LookupError("result not found")
        if self.is_active(row["scan_id"]):
            raise Busy("This scan is still running. Regenerate once it has finished.")
        scan = self.store.get_scan(row["scan_id"])
        client = self._make_client(scan["params"])
        ctx = scan["ctx"] or {"repo": scan["repo"]}
        analysis = analyze_issue(client, row_to_issue(row), row["evidence"], ctx)
        self.store.finish_result(result_id, analysis)
        self.store.update_result(result_id, review="new")
        self._refresh_counts(row["scan_id"])
        return self.store.get_result(result_id)

    # -- plumbing ---------------------------------------------------------------------
    def _begin(self, scan_id, target, args):
        self._active = scan_id
        self._cancel[scan_id] = threading.Event()
        thread = threading.Thread(target=target, args=args, name="ghosted-scan-%d" % scan_id)
        thread.daemon = True
        self._threads[scan_id] = thread
        thread.start()

    def _release(self, scan_id):
        with self._lock:
            if self._active == scan_id:
                self._active = None
            self._cancel.pop(scan_id, None)

    def _make_client(self, params):
        if params.get("backend") == "rules":
            return None
        return LLMClient(params["backend"], params.get("base_url"), params.get("model"),
                         timeout=params.get("timeout") or 180)

    def _make_github(self, params, token):
        if params.get("demo"):
            return demo_data.DemoGitHub()
        return GitHubClient(token=token or self.env_token, api_base=self.github_api_base,
                            **self.github_kwargs)

    def _fail(self, scan_id, message):
        self.store.update_scan(scan_id, status="error", message="Stopped", error=message,
                               finished_at=_stamp())

    def _guard(self, scan_id, body):
        try:
            body()
        except ScanAbort as exc:
            self._fail(scan_id, str(exc))
        except (LLMError, GitHubError) as exc:
            self._fail(scan_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - last line of defence for a background thread
            traceback.print_exc()
            self._fail(scan_id, "Something unexpected broke (%s: %s). Details are in the terminal."
                       % (exc.__class__.__name__, truncate(str(exc), 160)))
        finally:
            self._release(scan_id)

    def _run(self, scan_id, params, token):
        self._guard(scan_id, lambda: self._scan(scan_id, params, token))

    def _resume(self, scan_id):
        def body():
            scan = self.store.get_scan(scan_id)
            client = self._make_client(scan["params"])
            self.store.update_scan(scan_id, status="running", stage="analyze",
                                   message="Picking up where it stopped…")
            if client is not None:
                self._preflight(client, scan_id)
            self._analyze(scan_id, client, scan["ctx"] or {"repo": scan["repo"]})
        self._guard(scan_id, body)

    def _preflight(self, client, scan_id):
        self.store.update_scan(scan_id, stage="model",
                               message="Checking that %s is awake…" % client.pretty_backend)
        try:
            models = client.list_models()
        except LLMConnectionError:
            raise
        except LLMError:
            if client.backend == "ollama":
                raise
            return  # some OpenAI-compatible servers have no /models; the first real call decides
        if client.backend == "ollama" and not model_available(models, client.model):
            installed = ", ".join(models[:6]) or "nothing yet"
            raise ScanAbort("Ollama doesn't have the model '%s'. Installed: %s. Pull it with: "
                            "ollama pull %s" % (client.model, installed, client.model))

    # -- the scan ---------------------------------------------------------------------
    def _scan(self, scan_id, params, token):
        store = self.store
        cancel = self._cancel[scan_id]
        full_name = params["repo"]
        store.update_scan(scan_id, status="running", stage="model", message="Warming up…")

        client = self._make_client(params)
        if client is not None:
            self._preflight(client, scan_id)
        gh = self._make_github(params, token)
        warnings = []

        store.update_scan(scan_id, stage="repo", message="Reading %s…" % full_name)
        info = gh.repo(full_name)
        repo_info = {
            "full_name": info.get("full_name") or full_name,
            "description": truncate(info.get("description") or "", 240),
            "stars": info.get("stargazers_count") or 0,
            "archived": bool(info.get("archived")),
            "pushed_at": info.get("pushed_at"),
            "default_branch": info.get("default_branch") or "",
            "language": info.get("language") or "",
            "url": info.get("html_url") or "https://github.com/" + full_name,
        }
        store.update_scan(scan_id, repo_info=repo_info)
        if repo_info["archived"]:
            warnings.append("This repository is archived, so GitHub won't accept new comments. "
                            "The drafts are still useful as a review of what was missed.")
        if cancel.is_set():
            return self._stopped(scan_id)

        store.update_scan(scan_id, stage="issues", message="Finding the issues nobody answered…")
        raw_items, truncated = gh.open_issues(full_name, params["order"])
        now = now_utc()
        items = [i for i in raw_items if isinstance(i, dict) and isinstance(i.get("number"), int)]
        real = [i for i in items if not i.get("pull_request")]
        silent = [i for i in real if not (i.get("comments") or 0)]
        normalized = [normalize_issue(i, now) for i in silent]
        eligible = [i for i in normalized if i["age_days"] >= params["min_age_days"]]
        eligible.sort(key=lambda i: (i["created_at"] or "", i["number"]),
                      reverse=(params["order"] == "newest"))
        chosen = eligible[:params["max_issues"]]
        if truncated:
            warnings.append("The repository has more than %d open items; only the %s %d were "
                            "looked at." % (len(raw_items), params["order"], len(raw_items)))
        stats = {
            "open_items": len(items), "pull_requests": len(items) - len(real),
            "with_replies": len(real) - len(silent), "ghosted_total": len(silent),
            "too_new": len(normalized) - len(eligible), "eligible": len(eligible),
            "analyzed": len(chosen), "warnings": warnings,
        }
        if not chosen:
            store.update_scan(scan_id, status="done", stage="finish", total=0, done=0, stats=stats,
                              message="No ignored issues found.", finished_at=_stamp())
            return
        if cancel.is_set():
            return self._stopped(scan_id)

        store.update_scan(scan_id, stage="evidence",
                          message="Digging through merged PRs, commits and releases…")
        prs, commits, releases = [], [], []
        try:
            prs, prs_cut = gh.merged_prs(full_name)
            if prs_cut:
                warnings.append("Only the latest %d merged PRs were searched (back to %s), so "
                                "older fixes can be missed."
                                % (len(prs), _earliest(p.get("merged_at") for p in prs)))
        except GitHubError as exc:
            warnings.append("Merged pull requests could not be read: %s" % exc)
        try:
            commits, commits_cut = gh.commits(full_name, repo_info["default_branch"] or None)
            if commits_cut:
                warnings.append("Only the latest %d commits were searched (back to %s), so older "
                                "fixes can be missed." % (len(commits), _earliest(
                                    (c.get("commit") or {}).get("committer", {}).get("date")
                                    for c in commits)))
        except GitHubError as exc:
            warnings.append("Commits could not be read: %s" % exc)
        try:
            releases = gh.releases(full_name)
        except GitHubError as exc:
            warnings.append("Releases could not be read: %s" % exc)

        index = EvidenceIndex(repo_info["full_name"], prs, commits, releases)
        ctx = {"repo": full_name, "pushed_at": repo_info["pushed_at"],
               "latest_release": latest_release(releases)}
        rows = []
        for issue in chosen:
            row = dict(issue)
            row["evidence"] = index.for_issue(issue)
            rows.append(row)
        store.add_results(scan_id, rows)
        stats["with_evidence"] = sum(1 for r in rows if r["evidence"])
        stats["oldest_days"] = max(r["age_days"] for r in rows)
        store.update_scan(scan_id, total=len(rows), done=0, ctx=ctx, stats=stats)
        if cancel.is_set():
            return self._stopped(scan_id)

        self._analyze(scan_id, client, ctx)

    def _analyze(self, scan_id, client, ctx):
        store = self.store
        cancel = self._cancel[scan_id]
        pending = store.pending_results(scan_id)
        streak = 0
        for row in pending:
            if cancel.is_set():
                return self._stopped(scan_id)
            store.update_scan(scan_id, status="running", stage="analyze",
                              message="Reading #%d — %s" % (row["number"], truncate(row["title"], 70)))
            try:
                analysis = analyze_issue(client, row_to_issue(row), row["evidence"], ctx)
            except LLMError as exc:
                store.fail_result(row["id"], str(exc))
                streak += 1
                self._refresh_counts(scan_id)
                if isinstance(exc, LLMConnectionError) and streak >= 2:
                    raise ScanAbort("%s Issues already read are saved; press Resume once the "
                                    "model is back." % exc)
                if streak >= 4:
                    raise ScanAbort("The model failed on %d issues in a row (last problem: %s) "
                                    "so I stopped. What was read is saved; fix the setup and "
                                    "press Resume." % (streak, exc))
                continue
            store.finish_result(row["id"], analysis)
            streak = 0
            self._refresh_counts(scan_id)
        self._complete(scan_id)

    def _refresh_counts(self, scan_id):
        counts = self.store.counts(scan_id)
        scan = self.store.get_scan(scan_id)
        stats = dict(scan["stats"] or {})
        stats.update({"valid": counts["valid"], "fixed": counts["fixed"],
                      "needs_info": counts["needs_info"], "errors": counts["error"],
                      "unread": counts["pending"]})
        self.store.update_scan(scan_id, done=counts["done"] + counts["error"], stats=stats)
        return counts

    def _complete(self, scan_id):
        counts = self._refresh_counts(scan_id)
        failed = counts["error"]
        message = "All done." if not failed else "Done, but %d issue%s could not be read." % (
            failed, "" if failed == 1 else "s")
        self.store.update_scan(scan_id, status="done", stage="finish", message=message,
                               error="", finished_at=_stamp())

    def _stopped(self, scan_id):
        counts = self._refresh_counts(scan_id)
        self.store.update_scan(
            scan_id, status="cancelled", stage="cancelled", finished_at=_stamp(),
            message="Stopped. %d issue%s read; press Resume to read the rest."
                    % (counts["done"], "" if counts["done"] == 1 else "s"))


def _stamp():
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

