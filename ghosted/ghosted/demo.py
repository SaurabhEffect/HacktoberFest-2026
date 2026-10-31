"""A made-up graveyard, so Ghosted can be tried without GitHub or a model.

`ghost-town/forgotten-lib` is fictional. Its data has exactly the shape of GitHub's REST API, and
it goes through the very same pipeline as a real repository.
"""
from datetime import timedelta

from .textutil import now_utc

DEMO_REPO = "ghost-town/forgotten-lib"
_BASE = "https://github.com/" + DEMO_REPO


def _iso(now, days_ago, hour=10):
    moment = (now - timedelta(days=days_ago)).replace(hour=hour, minute=17, second=0, microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def fixture(now=None):
    """Return {'repo', 'issues', 'prs', 'commits', 'releases'} in GitHub's JSON shape."""
    now = now or now_utc()

    def issue(number, title, body, days_ago, author, labels=(), comments=0, pull=False):
        item = {
            "number": number, "title": title, "body": body, "state": "open",
            "html_url": "%s/%s/%d" % (_BASE, "pull" if pull else "issues", number),
            "user": {"login": author}, "comments": comments,
            "labels": [{"name": name} for name in labels],
            "created_at": _iso(now, days_ago), "updated_at": _iso(now, days_ago),
        }
        if pull:
            item["pull_request"] = {"url": "https://api.github.com/repos/%s/pulls/%d" % (DEMO_REPO, number)}
        return item

    issues = [
        issue(3, "Crash when config file is empty",
              "Calling `load()` on a zero-byte file raises `IndexError: list index out of range`.\n\n"
              "```\nTraceback (most recent call last):\n  File \"forgotten_lib/loader.py\", line 41, in load\n"
              "    first = lines[0]\nIndexError: list index out of range\n```\n\n"
              "Version 0.4.0 on Python 3.9. I would expect an empty dict instead.",
              1400, "mira-k", ["bug"]),
        issue(7, "load() ignores ENV overrides on Windows",
              "On Windows, `load()` silently ignores `FORGOTTEN_*` environment variables.\n\n"
              "Steps to reproduce:\n1. `set FORGOTTEN_DEBUG=1`\n"
              "2. `python -c \"import forgotten_lib; print(forgotten_lib.load('app.toml'))\"`\n\n"
              "Expected: the override shows up. Actual: the value from the file wins.\n\n"
              "Version 0.5.0, Python 3.11 on Windows 11.",
              1380, "tomasz-w", ["bug", "windows"]),
        issue(9, "Add YAML support",
              "Our whole team keeps its settings in YAML. Would you consider adding a YAML loader "
              "next to the existing JSON one? Happy to test a branch.",
              1350, "dana-r", ["enhancement"]),
        issue(12, "doesnt work", "it dont work pls help", 1300, "anon-4412"),
        issue(15, "Unicode keys raise KeyError",
              "Keys like `café` are stored correctly but lookups with the decomposed form "
              "(`café`) raise `KeyError`.\n\n```python\ncfg = load('app.json')\ncfg['café']  # KeyError\n```\n\n"
              "Version 0.5.0. I expected both spellings to find the same key.",
              1290, "li-wei", ["bug"]),
        issue(18, "Docs: typo in README install section",
              "The install section says `pip install forgotten_lib` but the package on PyPI "
              "is called `forgotten-lib`.",
              1260, "ben-o", ["documentation"]),
        issue(21, "Config.set() races under concurrent load",
              "When several threads call `Config.set()` at once, some writes are silently lost.\n\n"
              "```python\nimport threading, forgotten_lib\ncfg = forgotten_lib.Config()\n"
              "def work(i):\n    for j in range(1000):\n        cfg.set(f'k{i}_{j}', j)\n"
              "threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]\n"
              "[t.start() for t in threads]; [t.join() for t in threads]\nprint(len(cfg))  # expected 8000\n```\n\n"
              "Version 0.5.0 on Python 3.10 (Linux). Roughly 7,200 keys survive each run.",
              1230, "ola-n", ["bug", "concurrency"]),
        issue(24, "Python 3.12 DeprecationWarning from imp module",
              "Importing the package on Python 3.12 prints:\n\n```\nforgotten_lib/plugins.py:3: "
              "DeprecationWarning: the imp module is deprecated in favour of importlib\n```\n\n"
              "Version 0.6.0. It will break outright on 3.13, where `imp` is gone.",
              1000, "priya-s", ["bug"]),
        issue(27, "Please publish on conda-forge",
              "It would be great if the package were available on conda-forge as well. Many of "
              "our users cannot use pip inside their locked-down environments.",
              900, "grace-h", ["enhancement"]),
        issue(30, "Segfault???", "Segfault on my machine", 700, "anon-9012"),
        issue(33, "Release 0.6 breaks pickling of Config objects",
              "After upgrading from 0.5.0 to 0.6.0, `pickle.dumps(cfg)` fails:\n\n```\n"
              "TypeError: cannot pickle '_thread.RLock' object\n```\n\n"
              "Worked fine in 0.5.0. Steps: `cfg = forgotten_lib.load('app.toml'); pickle.dumps(cfg)`. "
              "I expected a bytes object, like before.",
              520, "jonas-e", ["bug", "regression"]),
        issue(36, "TOML support",
              "Would love a TOML loader, since `pyproject.toml` is becoming the standard place "
              "for tool settings.",
              1200, "yuki-t", ["enhancement"]),
        # Things the pipeline must leave alone:
        issue(40, "How do I use layered configs?", "Is there a doc page for layering?",
              1100, "newbie-1", ["question"], comments=2),
        issue(50, "WIP: async API", "Draft. Do not merge.", 800, "maintainer", pull=True),
        issue(66, "Is this project still maintained?", "Asking before I build on it.",
              5, "curious-cat"),
    ]

    prs = [
        {"number": 41, "title": "Handle empty config files", "body": "Fixes #3",
         "merged_at": _iso(now, 1330), "html_url": _BASE + "/pull/41", "state": "closed",
         "user": {"login": "maintainer"}},
        {"number": 58, "title": "Add YAML loader",
         "body": "Implements the YAML loader requested in #9.",
         "merged_at": _iso(now, 1270), "html_url": _BASE + "/pull/58", "state": "closed",
         "user": {"login": "maintainer"}},
        {"number": 61, "title": "Add TOML loader", "body": "Implements #36.",
         "merged_at": _iso(now, 1170), "html_url": _BASE + "/pull/61", "state": "closed",
         "user": {"login": "maintainer"}},
        {"number": 55, "title": "Experimental plugin API", "body": "Closed without merging.",
         "merged_at": None, "html_url": _BASE + "/pull/55", "state": "closed",
         "user": {"login": "drive-by"}},
    ]

    def commit(sha, message, days_ago):
        return {"sha": sha, "html_url": "%s/commit/%s" % (_BASE, sha),
                "commit": {"message": message,
                           "committer": {"date": _iso(now, days_ago, 14), "name": "maintainer"},
                           "author": {"date": _iso(now, days_ago, 14), "name": "maintainer"}}}

    commits = [  # newest first, like GitHub
        commit("9f8e7d6c5b4a39281706f5e4d3c2b1a098765432", "Bump version to 0.6.0", 1150),
        commit("0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f607182930", "Add TOML loader (#61)", 1170),
        commit("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", "Fix typos in README install instructions", 1200),
        commit("b2c3d4e5f60718293a4b5c6d7e8f901234567890",
               "Normalize unicode keys when merging layers\n\nCloses #15", 1240),
        commit("c3d4e5f60718293a4b5c6d7e8f90123456789012", "Add YAML loader (#58)", 1270),
        commit("d4e5f60718293a4b5c6d7e8f9012345678901234", "Handle empty config files (#41)", 1330),
        commit("e5f60718293a4b5c6d7e8f901234567890123456", "Initial commit", 1500),
    ]

    releases = [
        {"tag_name": "v0.6.0", "name": "v0.6.0", "draft": False, "prerelease": False,
         "published_at": _iso(now, 1150), "html_url": _BASE + "/releases/tag/v0.6.0",
         "body": "## Highlights\n- TOML support (#36)\n- Small README fixes\n"},
        {"tag_name": "v0.5.0", "name": "v0.5.0", "draft": False, "prerelease": False,
         "published_at": _iso(now, 1250), "html_url": _BASE + "/releases/tag/v0.5.0",
         "body": "## What's new\n- YAML loading support (#9)\n- Faster `load()` for large files\n"},
        {"tag_name": "v0.4.0", "name": "v0.4.0", "draft": False, "prerelease": False,
         "published_at": _iso(now, 1500), "html_url": _BASE + "/releases/tag/v0.4.0",
         "body": "Initial public release."},
    ]

    repo = {
        "full_name": DEMO_REPO, "name": "forgotten-lib", "owner": {"login": "ghost-town"},
        "description": "A tiny config loader that nobody maintains any more.",
        "stargazers_count": 217, "archived": False, "pushed_at": _iso(now, 1150),
        "default_branch": "main", "open_issues_count": len(issues), "language": "Python",
        "html_url": _BASE,
    }
    return {"repo": repo, "issues": issues, "prs": prs, "commits": commits, "releases": releases}


class DemoGitHub(object):
    """Quacks like github.GitHubClient, but reads the built-in fixture."""

    def __init__(self, now=None):
        data = fixture(now)
        self._data = data
        self.remaining = None
        self.limit = None
        self.request_count = 0

    def repo(self, full_name):
        return dict(self._data["repo"])

    def open_issues(self, full_name, order="oldest", max_pages=10):
        items = sorted(self._data["issues"], key=lambda i: i["created_at"], reverse=(order == "newest"))
        return list(items), False

    def merged_prs(self, full_name, max_pages=5):
        return [p for p in self._data["prs"] if p.get("merged_at")], False

    def commits(self, full_name, branch=None, max_pages=5):
        return list(self._data["commits"]), False

    def releases(self, full_name):
        return list(self._data["releases"])
