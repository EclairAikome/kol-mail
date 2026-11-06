"""What the playbook learns from, kept in data/learning.sqlite3.

- episodes: one per reply sent, with the AI's last version (ai_body) next to what went out (sent_body), the owner's
  words for it (notes, feedback, Chinese corrections), the strategies offered and chosen, the compliance result, and
  later how the other side answered. Recording is bookkeeping after a send: it never fails or slows a send, and it
  makes no model call. After RETENTION the texts are dropped and only the sentence diff is kept.
- changes: every change to the playbook (by the review, the user's 手册更新, an SOP sync, an approval, an undo, a
  rollback, a hand edit), with its hunks, the version before and after, whether the user has seen the notice, and
  for an automatic change its 14-day trial.
- kv: small state (when the review last ran, the size baseline, the last playbook version seen, ...)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import playbook_learn as pl

RETENTION = 180 * 24 * 3600
TRIAL = 14 * 24 * 3600
TEXT_MAX = 50_000
STANCES = ("accept", "counter", "reject", "question", "other")
SILENT_AFTER = 7 * 24 * 3600
MAIL_CHANNELS = ("email", "email-new", "phone-email")
JSON_FIELDS = ("strategy_options", "feedback_log", "zh_edits", "compliance")
EPISODE_FIELDS = ("at", "channel", "mode", "account", "address", "message_id", "kol_id", "language",
                  "strategy_options", "recommended", "chosen", "ai_body", "sent_body", "body_zh", "feedback_log",
                  "zh_edits", "compliance", "edit_ratio", "playbook_sha", "stance_next", "outcome", "outcome_source",
                  "exception", "used_at", "kol_text", "notes", "summary")
CHANGE_STATES = ("trial", "kept", "pending", "rejected", "reverted", "done", "applied", "expired")


def _day(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%m-%d")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        cols = ", ".join(f"{f} {'REAL' if f in ('at', 'edit_ratio', 'used_at') else 'INTEGER' if f in ('recommended', 'chosen', 'exception') else 'TEXT'}"
                         for f in EPISODE_FIELDS)
        with self.lock:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
            self.db.execute("CREATE INDEX IF NOT EXISTS episodes_address ON episodes (address, at)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                hunks TEXT NOT NULL DEFAULT '[]', note TEXT, sha_before TEXT, sha_after TEXT, backup TEXT,
                seen_at REAL, trial_until REAL, alert TEXT, meta TEXT NOT NULL DEFAULT '{}')""")
            self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            self.db.commit()

    # ------------------------------------------------------------ kv
    def get(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            (key, str(value)))
            self.db.commit()

    # ------------------------------------------------------------ episodes
    def record(self, **fields) -> int:
        """One sent reply. edit_ratio is worked out here; exception (set by the caller when a red line was broken on
        purpose) keeps it out of every review."""
        ep = {k: fields.get(k) for k in EPISODE_FIELDS}
        ep["at"] = float(ep["at"] or time.time())
        for k in ("ai_body", "sent_body", "body_zh", "kol_text", "notes"):
            ep[k] = str(ep[k] or "")[:TEXT_MAX] or None
        ep["edit_ratio"] = pl.edit_ratio(ep["ai_body"] or "", ep["sent_body"] or "") if ep["ai_body"] else None
        ep["exception"] = 1 if fields.get("exception") else 0
        for k in JSON_FIELDS:
            ep[k] = json.dumps(ep[k], ensure_ascii=False) if ep[k] not in (None, "", [], {}) else None
        if ep["address"]:
            ep["address"] = str(ep["address"]).strip().lower()
        names = [k for k in EPISODE_FIELDS if ep[k] is not None]
        with self.lock:
            cur = self.db.execute(f"INSERT INTO episodes ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                                  [ep[k] for k in names])
            self.db.commit()
            return int(cur.lastrowid)

    def _episode(self, row: sqlite3.Row) -> dict:
        ep = dict(row)
        for k in JSON_FIELDS:
            try:
                ep[k] = json.loads(ep[k]) if ep[k] else ([] if k != "compliance" else {})
            except ValueError:
                ep[k] = [] if k != "compliance" else {}
        ep["day"] = _day(ep["at"])
        return ep

    def episode(self, ep_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()
        return self._episode(row) if row else None

    def episodes(self, *, unused: bool = False, limit: int = 200, since: float = 0) -> list[dict]:
        where, args = ["at >= ?"], [since]
        if unused:
            where.append("used_at IS NULL")
        with self.lock:
            rows = self.db.execute(f"SELECT * FROM episodes WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                                   [*args, limit]).fetchall()
        return [self._episode(r) for r in reversed(rows)]

    def review_batch(self, limit: int = 30) -> list[dict]:
        """Episodes not yet reviewed that can teach something: learnable and with a contrast."""
        return [ep for ep in self.episodes(unused=True, limit=500) if pl.learnable(ep) and pl.has_contrast(ep)][-limit:]

    def contrast_count(self) -> int:
        return len(pl.contrast_keys([ep for ep in self.episodes(unused=True, limit=500) if pl.learnable(ep)]))

    def mark_used(self, ids: list[int], at: float | None = None) -> None:
        if not ids:
            return
        with self.lock:
            self.db.executemany("UPDATE episodes SET used_at=? WHERE id=?", [(at or time.time(), i) for i in ids])
            self.db.commit()

    def note_reply(self, address: str, stance: str, at: float, ref: str = "") -> int | None:
        """The other side answered (stance from the translation of their mail): put it on our latest episode to them
        sent before that mail, unless that one has its answer already (a second mail in a row answers nothing new).
        ref: the incoming mail's Message-ID, so a mail translated twice counts once. Returns that episode's id."""
        stance = stance if stance in STANCES else "other"
        address = (address or "").strip().lower()
        if not address:
            return None
        if ref and self.get(f"reply:{ref}"):
            return None
        with self.lock:
            row = self.db.execute("SELECT id, stance_next FROM episodes WHERE address=? AND at<? "
                                  "ORDER BY at DESC LIMIT 1", (address, at)).fetchone()
            if not row or row[1]:
                return None
            self.db.execute("UPDATE episodes SET stance_next=? WHERE id=?", (stance, row[0]))
            self.db.commit()
        if ref:
            self.put(f"reply:{ref}", str(row[0]))
        return int(row[0])

    def set_outcome(self, ep_id: int, outcome: str, source: str) -> None:
        with self.lock:
            self.db.execute("UPDATE episodes SET outcome=?, outcome_source=? WHERE id=? AND outcome IS NULL",
                            (outcome, source, ep_id))
            self.db.commit()

    def open_outcomes(self, now: float | None = None) -> list[dict]:
        """Episodes with an address and no outcome yet (the latest per address), for the outcome checks."""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM episodes WHERE outcome IS NULL AND address IS NOT NULL AND id IN "
                "(SELECT MAX(id) FROM episodes GROUP BY address) ORDER BY at").fetchall()
        return [self._episode(r) for r in rows]

    def mark_silent(self, now: float | None = None) -> int:
        """Our last reply to someone, unanswered for SILENT_AFTER: outcome 沉默."""
        now = now or time.time()
        n = 0
        for ep in self.open_outcomes():
            if ep.get("channel") not in MAIL_CHANNELS:  # a chat's answers are not read for a stance: never "silent"
                continue
            if not ep.get("stance_next") and ep["at"] < now - SILENT_AFTER:
                self.set_outcome(ep["id"], "沉默（7 天没回）", "silence")
                n += 1
        return n

    def prune(self, now: float | None = None) -> int:
        """Episodes older than RETENTION keep only their sentence diff (summary)."""
        cutoff = (now or time.time()) - RETENTION
        with self.lock:
            rows = self.db.execute("SELECT id, ai_body, sent_body FROM episodes WHERE at<? AND "
                                   "(ai_body IS NOT NULL OR sent_body IS NOT NULL OR kol_text IS NOT NULL "
                                   "OR notes IS NOT NULL OR feedback_log IS NOT NULL)", (cutoff,)).fetchall()
            for r in rows:
                summary = "\n".join(pl.sentence_diff(r["ai_body"] or "", r["sent_body"] or ""))[:4000]
                self.db.execute("UPDATE episodes SET ai_body=NULL, sent_body=NULL, body_zh=NULL, kol_text=NULL, "
                                "notes=NULL, zh_edits=NULL, feedback_log=NULL, strategy_options=NULL, summary=? "
                                "WHERE id=?", (summary, r["id"]))
            self.db.commit()
        return len(rows)

    def rule_stats(self, sha: str) -> tuple[int, float, dict[str, float]]:
        """For drafts written with this playbook version: (how many, mean automatic rewrites, share of drafts each
        rule was flagged in)."""
        with self.lock:
            rows = self.db.execute("SELECT compliance FROM episodes WHERE playbook_sha=?", (sha,)).fetchall()
        n, total, hits = 0, 0.0, {}
        for (raw,) in rows:
            try:
                c = json.loads(raw) if raw else {}
            except ValueError:
                continue
            if not c.get("checked"):
                continue
            n += 1
            total += float(c.get("revisions") or 0)
            for rule in set(c.get("flagged") or []):
                hits[rule] = hits.get(rule, 0) + 1
        return n, (total / n if n else 0.0), {k: v / n for k, v in hits.items()} if n else {}

    # ------------------------------------------------------------ changes
    def add_change(self, source: str, status: str, hunks: list[dict], *, note: str = "", sha_before: str = "",
                   sha_after: str = "", backup: str = "", seen: bool = False, trial: bool = False,
                   meta: dict | None = None, at: float | None = None) -> int:
        at = at or time.time()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO changes (at, source, status, hunks, note, sha_before, sha_after, backup, seen_at, "
                "trial_until, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (at, source, status, json.dumps(hunks, ensure_ascii=False), note, sha_before, sha_after, backup,
                 at if seen else None, at + TRIAL if trial else None, json.dumps(meta or {}, ensure_ascii=False)))
            self.db.commit()
            return int(cur.lastrowid)

    def _change(self, row: sqlite3.Row) -> dict:
        c = dict(row)
        for k, empty in (("hunks", []), ("meta", {})):
            try:
                c[k] = json.loads(c[k]) if c[k] else empty
            except ValueError:
                c[k] = empty
        return c

    def change(self, change_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
        return self._change(row) if row else None

    def changes(self, statuses: tuple[str, ...] | None = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT * FROM changes", []
        if statuses:
            q += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            args = list(statuses)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
        return [self._change(r) for r in rows]

    def update_change(self, change_id: int, **fields) -> None:
        if "hunks" in fields:
            fields["hunks"] = json.dumps(fields["hunks"], ensure_ascii=False)
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        names = list(fields)
        with self.lock:
            self.db.execute(f"UPDATE changes SET {', '.join(f'{k}=?' for k in names)} WHERE id=?",
                            [*fields.values(), change_id])
            self.db.commit()

    def unseen(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM changes WHERE seen_at IS NULL ORDER BY id").fetchall()
        return [self._change(r) for r in rows]

    def mark_seen(self, ids: list[int], at: float | None = None) -> None:
        with self.lock:
            self.db.executemany("UPDATE changes SET seen_at=? WHERE id=? AND seen_at IS NULL",
                                [(at or time.time(), i) for i in ids])
            self.db.commit()

    def end_trials(self, now: float | None = None) -> list[int]:
        """Automatic changes whose 14 days are over and that were not undone: kept from now on."""
        now = now or time.time()
        with self.lock:
            rows = self.db.execute("SELECT id FROM changes WHERE status='trial' AND trial_until<=?", (now,)).fetchall()
            self.db.execute("UPDATE changes SET status='kept' WHERE status='trial' AND trial_until<=?", (now,))
            self.db.commit()
        return [r[0] for r in rows]

    def since(self, at: float, sources: tuple[str, ...] | None = None) -> list[dict]:
        rows = [c for c in self.changes(limit=500) if c["at"] >= at]
        return [c for c in rows if not sources or c["source"] in sources]
