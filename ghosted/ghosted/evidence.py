"""Evidence: look for proof that an ignored issue was addressed later on.

Everything here is pure and deterministic (no network, no model), so it is easy to test.
For one issue we look through the repository's later history:

* merged pull requests that say "Fixes #N" (strength ``closing``) or just mention ``#N``
  (strength ``mention``),
* commits that do the same,
* release notes that mention the issue, or the pull request that fixed it,
* and, as a weak hint only, changes whose titles share several distinctive words with the
  issue title (strength ``similar``).
"""
import re

from .textutil import day_str, one_line, parse_ts, truncate

STRENGTH_RANK = {"closing": 3, "mention": 2, "similar": 1}

_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_CLOSING_HASH = re.compile(
    r"(?<![\w])" + _KEYWORD + r"\b[\s:]*(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>\d+)\b", re.I)
_CLOSING_URL = re.compile(
    r"(?<![\w])" + _KEYWORD +
    r"\b[\s:]*https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/(?:issues|pull)/(?P<num>\d+)\b", re.I)
_MENTION_HASH = re.compile(r"(?<![\w/])#(?P<num>\d+)\b")
_MENTION_QUALIFIED = re.compile(r"(?<![\w./-])(?P<repo>[\w.-]+/[\w.-]+?)#(?P<num>\d+)\b")
_MENTION_GH = re.compile(r"(?<![\w])GH-(?P<num>\d+)\b", re.I)
_URL_ANY = re.compile(
    r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/(?:issues|pull)/(?P<num>\d+)\b", re.I)
_NEGATION = re.compile(
    r"(?:\bnot|\bnever|\bwithout|\bpartial(?:ly)?|\bpartly|\btowards?|n't|\bno longer)\s*$", re.I)
_WORD = re.compile(r"[a-z][a-z0-9_]{3,}")

_STOP = frozenset("""
about above after again also another because been before being between both cannot could does
doesn done each else even every from have having here into just like make many more most much
must need only other over please really same should since some such than that their them then
there these they this those through under until upon using very want well were what when where
which while will with within without would your issue issues problem problems error errors bug
bugs feature features request requests support add adds added allow allows allowed able unable
working works work doesnt dont cant wont thanks thank fixes fixed fixing fix update updates
updated change changes changed improve improves improved implement implements implemented
""".split())


def stem(word):
    """A crude plural-stripper, good enough to match 'typos' with 'typo'."""
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text):
    out = set()
    for raw in _WORD.findall((text or "").lower()):
        if raw in _STOP:
            continue
        word = stem(raw)
        if word in _STOP or word.isdigit():
            continue
        out.add(word)
    return out


def is_similar(issue_tokens, other_tokens):
    """At least two shared words, covering at least half of the issue title's words."""
    if not issue_tokens or not other_tokens:
        return None
    shared = issue_tokens & other_tokens
    if len(shared) >= 2 and len(shared) / float(len(issue_tokens)) >= 0.5:
        return shared
    return None


def find_refs(text, full_name=None):
    """Return (closing_numbers, mentioned_numbers) for same-repository issue references."""
    closing = set()
    mentioned = set()
    text = text or ""
    repo = (full_name or "").lower()

    def same_repo(found):
        return not found or not repo or found.lower() == repo

    for match in _CLOSING_HASH.finditer(text):
        if not same_repo(match.group("repo")):
            continue
        num = int(match.group("num"))
        mentioned.add(num)
        before = text[max(0, match.start() - 24):match.start()]
        if not _NEGATION.search(before):
            closing.add(num)
    for match in _CLOSING_URL.finditer(text):
        if not same_repo(match.group("repo")):
            continue
        num = int(match.group("num"))
        mentioned.add(num)
        before = text[max(0, match.start() - 24):match.start()]
        if not _NEGATION.search(before):
            closing.add(num)
    for match in _MENTION_HASH.finditer(text):
        mentioned.add(int(match.group("num")))
    for match in _MENTION_GH.finditer(text):
        mentioned.add(int(match.group("num")))
    for match in _MENTION_QUALIFIED.finditer(text):
        if same_repo(match.group("repo")):
            mentioned.add(int(match.group("num")))
    for match in _URL_ANY.finditer(text):
        if same_repo(match.group("repo")):
            mentioned.add(int(match.group("num")))
    return closing, mentioned


def _strip_bullet(line):
    return re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()


class EvidenceIndex(object):
    """Pre-digested view of a repository's merged PRs, commits and releases."""

    def __init__(self, full_name, prs, commits, releases):
        self.full_name = full_name
        self.prs = [rec for rec in (self._prep_pr(p) for p in prs or []) if rec]
        self.commits = [rec for rec in (self._prep_commit(c) for c in commits or []) if rec]
        self.releases = [rec for rec in (self._prep_release(r) for r in releases or []) if rec]

    # -- preparing raw GitHub records -------------------------------------------------
    def _base(self, kind, ref, title, url, when_raw, text, number=None):
        closing, mentioned = find_refs(text, self.full_name)
        return {
            "kind": kind, "ref": ref, "title": one_line(title, 140), "url": url or "",
            "when": parse_ts(when_raw), "date": day_str(when_raw), "text": text,
            "closing": closing, "mentions": mentioned, "number": number,
            "tokens": tokens(title),
        }

    def _prep_pr(self, pr):
        if not isinstance(pr, dict) or not pr.get("merged_at"):
            return None
        number = pr.get("number")
        title = pr.get("title") or ""
        text = "%s\n%s" % (title, pr.get("body") or "")
        ref = "PR #%s" % number if number is not None else "PR"
        rec = self._base("pr", ref, title, pr.get("html_url"), pr.get("merged_at"), text, number)
        # a PR never counts as a reference to itself
        rec["closing"].discard(number)
        rec["mentions"].discard(number)
        return rec

    def _prep_commit(self, commit):
        if not isinstance(commit, dict):
            return None
        info = commit.get("commit") or {}
        message = info.get("message") or ""
        who = info.get("committer") or info.get("author") or {}
        when = who.get("date")
        sha = str(commit.get("sha") or "")[:7]
        title = message.split("\n", 1)[0]
        return self._base("commit", "commit %s" % sha if sha else "commit", title,
                          commit.get("html_url"), when, message)

    def _prep_release(self, release):
        if not isinstance(release, dict) or release.get("draft"):
            return None
        body = release.get("body") or ""
        name = release.get("name") or release.get("tag_name") or "release"
        tag = release.get("tag_name") or name
        rec = self._base("release", "release %s" % tag, name, release.get("html_url"),
                         release.get("published_at") or release.get("created_at"),
                         "%s\n%s" % (name, body))
        rec["lines"] = [_strip_bullet(line) for line in body.splitlines() if line.strip()]
        return rec

    # -- per issue --------------------------------------------------------------------
    @staticmethod
    def _item(rec, strength, note):
        return {
            "kind": rec["kind"], "strength": strength, "ref": rec["ref"], "title": rec["title"],
            "url": rec["url"], "date": rec["date"], "note": truncate(note, 160),
        }

    @staticmethod
    def _after(rec, created):
        return created is None or rec["when"] is None or rec["when"] > created

    def for_issue(self, issue, limit=5):
        number = issue["number"]
        created = parse_ts(issue.get("created_at"))
        title_tokens = tokens(issue.get("title"))
        found = []
        seen_pr_numbers = set()
        strong_pr_numbers = set()

        for rec in self.prs:
            if not self._after(rec, created):
                continue
            if number in rec["closing"]:
                strength, note = "closing", "declares that it fixes this issue"
            elif number in rec["mentions"]:
                strength, note = "mention", "mentions this issue"
            else:
                shared = is_similar(title_tokens, rec["tokens"])
                if not shared:
                    continue
                strength = "similar"
                note = "similar title (shared words: %s)" % ", ".join(sorted(shared))
            found.append(self._item(rec, strength, note))
            seen_pr_numbers.add(rec["number"])
            if strength != "similar":
                strong_pr_numbers.add(rec["number"])

        for rec in self.commits:
            if not self._after(rec, created):
                continue
            if rec["mentions"] & seen_pr_numbers:
                continue  # the squash-merge commit of a PR we already listed
            if number in rec["closing"]:
                strength, note = "closing", "its message says it fixes this issue"
            elif number in rec["mentions"]:
                strength, note = "mention", "its message mentions this issue"
            else:
                shared = is_similar(title_tokens, rec["tokens"])
                if not shared:
                    continue
                strength = "similar"
                note = "similar title (shared words: %s)" % ", ".join(sorted(shared))
            found.append(self._item(rec, strength, note))

        for rec in self.releases:
            if not self._after(rec, created):
                continue
            if number in rec["closing"] or number in rec["mentions"]:
                found.append(self._item(rec, "mention", "release notes mention this issue"))
                continue
            via = rec["mentions"] & strong_pr_numbers
            if via:
                shipped = ", ".join("PR #%s" % n for n in sorted(via))
                found.append(self._item(rec, "mention", "release notes list the fix (%s)" % shipped))
                continue
            best = None
            for line in rec.get("lines", []):
                shared = is_similar(title_tokens, tokens(line))
                if shared and (best is None or len(shared) > best[0]):
                    best = (len(shared), line)
            if best:
                found.append(self._item(rec, "similar", "release note line: " + one_line(best[1], 110)))

        found.sort(key=lambda e: (-STRENGTH_RANK[e["strength"]], e["date"], e["ref"]))
        weak = [e for e in found if e["strength"] == "similar"][:3]
        strong = [e for e in found if e["strength"] != "similar"]
        return (strong + weak)[:limit]


def best_strength(evidence):
    """The strongest evidence strength in a list, or None."""
    best = None
    for item in evidence or []:
        if best is None or STRENGTH_RANK.get(item.get("strength"), 0) > STRENGTH_RANK[best]:
            best = item.get("strength")
    return best


def latest_release(releases):
    """The newest published, non-draft, non-prerelease release as {'tag', 'date'}, or None."""
    best = None
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        when = parse_ts(rel.get("published_at") or rel.get("created_at"))
        if when is None:
            continue
        if best is None or when > best[0]:
            best = (when, rel)
    if not best:
        return None
    rel = best[1]
    return {"tag": rel.get("tag_name") or rel.get("name") or "latest",
            "date": day_str(rel.get("published_at") or rel.get("created_at"))}
