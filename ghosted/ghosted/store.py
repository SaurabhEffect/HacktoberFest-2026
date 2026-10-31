"""SQLite persistence (standard library). One connection, guarded by a lock."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    demo        INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    stage       TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    engine      TEXT NOT NULL DEFAULT '',
    params      TEXT NOT NULL DEFAULT '{}',
    ctx         TEXT NOT NULL DEFAULT '{}',
    repo_info   TEXT NOT NULL DEFAULT '{}',
    stats       TEXT NOT NULL DEFAULT '{}',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id      INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    created_at   TEXT,
    age_days     INTEGER NOT NULL DEFAULT 0,
    labels       TEXT NOT NULL DEFAULT '[]',
    body         TEXT NOT NULL DEFAULT '',
    evidence     TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'pending',
    verdict      TEXT,
    confidence   TEXT,
    reason       TEXT NOT NULL DEFAULT '',
    reply        TEXT NOT NULL DEFAULT '',
    edited_reply TEXT,
    flags        TEXT NOT NULL DEFAULT '[]',
    error        TEXT NOT NULL DEFAULT '',
    review       TEXT NOT NULL DEFAULT 'new',
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_scan ON results(scan_id, seq);
"""

_SCAN_JSON = ("params", "ctx", "repo_info", "stats")
_RESULT_JSON = ("labels", "evidence", "flags")
SCAN_FIELDS = frozenset([
    "status", "stage", "message", "total", "done", "engine", "params", "ctx", "repo_info",
    "stats", "error", "finished_at"])
RESULT_FIELDS = frozenset([
    "status", "verdict", "confidence", "reason", "reply", "edited_reply", "flags", "error",
    "review", "evidence"])


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _load(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


class Store(object):
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # -- conversions ------------------------------------------------------------------
    @staticmethod
    def _scan_dict(row):
        if row is None:
            return None
        data = dict(row)
        for key in _SCAN_JSON:
            data[key] = _load(data.get(key), {})
        data["demo"] = bool(data.get("demo"))
        return data

    @staticmethod
    def _result_dict(row):
        if row is None:
            return None
        data = dict(row)
        for key in _RESULT_JSON:
            data[key] = _load(data.get(key), [])
        return data

    # -- scans ------------------------------------------------------------------------
    def create_scan(self, repo, demo, params, engine):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO scans (repo, demo, status, stage, message, engine, params, created_at) "
                "VALUES (?, ?, 'queued', 'queued', 'Waiting to start…', ?, ?, ?)",
                (repo, 1 if demo else 0, engine, _dump(params), now_iso()))
            self._conn.commit()
            return cur.lastrowid

    def update_scan(self, scan_id, **fields):
        if not fields:
            return
        unknown = set(fields) - SCAN_FIELDS
        if unknown:
            raise ValueError("unknown scan fields: %s" % ", ".join(sorted(unknown)))
        cols, values = [], []
        for key, value in fields.items():
            cols.append("%s = ?" % key)
            values.append(_dump(value) if key in _SCAN_JSON else value)
        values.append(scan_id)
        with self._lock:
            self._conn.execute("UPDATE scans SET %s WHERE id = ?" % ", ".join(cols), values)
            self._conn.commit()

    def get_scan(self, scan_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return self._scan_dict(row)

    def list_scans(self, limit=20):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._scan_dict(r) for r in rows]

    def delete_scan(self, scan_id):
        with self._lock:
            cur = self._conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_interrupted(self):
        """After a restart, scans still marked running can never finish. Say so."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE scans SET status = 'error', stage = 'interrupted', "
                "message = 'Interrupted', error = 'Ghosted was restarted while this scan was running. "
                "Press Resume to continue where it stopped.', finished_at = ? "
                "WHERE status IN ('queued', 'running')", (now_iso(),))
            self._conn.commit()
            return cur.rowcount

    # -- results ----------------------------------------------------------------------
    def add_results(self, scan_id, rows):
        stamp = now_iso()
        with self._lock:
            for seq, row in enumerate(rows):
                self._conn.execute(
                    "INSERT INTO results (scan_id, seq, number, title, url, author, created_at, "
                    "age_days, labels, body, evidence, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scan_id, seq, row["number"], row["title"], row.get("url", ""),
                     row.get("author", ""), row.get("created_at"), int(row.get("age_days") or 0),
                     _dump(row.get("labels") or []), row.get("body", ""),
                     _dump(row.get("evidence") or []), stamp))
            self._conn.commit()

    def get_results(self, scan_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM results WHERE scan_id = ? ORDER BY seq", (scan_id,)).fetchall()
        return [self._result_dict(r) for r in rows]

    def get_result(self, result_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM results WHERE id = ?", (result_id,)).fetchone()
        return self._result_dict(row)

    def update_result(self, result_id, **fields):
        if not fields:
            return
        unknown = set(fields) - RESULT_FIELDS
        if unknown:
            raise ValueError("unknown result fields: %s" % ", ".join(sorted(unknown)))
        cols, values = [], []
        for key, value in fields.items():
            cols.append("%s = ?" % key)
            values.append(_dump(value) if key in _RESULT_JSON else value)
        cols.append("updated_at = ?")
        values.append(now_iso())
        values.append(result_id)
        with self._lock:
            self._conn.execute("UPDATE results SET %s WHERE id = ?" % ", ".join(cols), values)
            self._conn.commit()

    def finish_result(self, result_id, analysis):
        self.update_result(
            result_id, status="done", verdict=analysis["verdict"],
            confidence=analysis["confidence"], reason=analysis["reason"],
            reply=analysis["reply"], flags=analysis.get("flags") or [], error="",
            edited_reply=None)

    def fail_result(self, result_id, message):
        self.update_result(result_id, status="error", error=message)

    def reset_unfinished(self, scan_id):
        """Failed results go back to pending so a retry picks them up. Returns pending count."""
        with self._lock:
            self._conn.execute(
                "UPDATE results SET status = 'pending', error = '', updated_at = ? "
                "WHERE scan_id = ? AND status = 'error'", (now_iso(), scan_id))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM results WHERE scan_id = ? AND status = 'pending'",
                (scan_id,)).fetchone()
        return row["n"]

    def pending_results(self, scan_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM results WHERE scan_id = ? AND status = 'pending' ORDER BY seq",
                (scan_id,)).fetchall()
        return [self._result_dict(r) for r in rows]

    def counts(self, scan_id):
        """{'done': n, 'error': n, 'pending': n, 'valid': n, 'fixed': n, 'needs_info': n}"""
        out = {"done": 0, "error": 0, "pending": 0, "valid": 0, "fixed": 0, "needs_info": 0,
               "approved": 0}
        with self._lock:
            for row in self._conn.execute(
                    "SELECT status, verdict, review, COUNT(*) AS n FROM results "
                    "WHERE scan_id = ? GROUP BY status, verdict, review", (scan_id,)):
                out[row["status"]] = out.get(row["status"], 0) + row["n"]
                if row["status"] == "done" and row["verdict"] in out:
                    out[row["verdict"]] += row["n"]
                if row["review"] == "approved":
                    out["approved"] += row["n"]
        return out
