"""Telegram and Discord private chats with KOLs, kept next to the mail (data/chats.sqlite3).

KOL Mail reads them in (Telegram through its own login, Discord through the browser extension in extension/discord),
shows them in the 私信 view, drafts replies the way it drafts mail, and writes every message of a chat that is linked
to a KOL总表 row to the sheet's 沟通记录 and to the transcript the 9:40/17:00 triage reads. Nothing is sent from
here: app.py sends Telegram messages, and the extension fills Discord's input box for the owner to send.

What is kept is kept against the other side changing its mind: a message read again with other words keeps its
earlier words in msg_edit, the transcript holds a linked chat's whole history (msg.noted, apart from msg.logged,
which is the sheet's), and msg_file names the copy of each attachment app.py saved.

A conversation is keyed "tg:<chat id>" or "dc:<DM channel id>". Its peer token, "tg:<user id>(@username)" or
"dc:<user id>(name)", is what the sheet's 聊天账号 column holds (kol-sheet\\kol.py and sheet/usersheet.py match on the
part before the bracket)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHANNELS = {"tg": "telegram", "dc": "discord"}
LABELS = {"telegram": "Telegram", "discord": "Discord"}
KEY = re.compile(r"^(tg|dc):(-?\d{1,20})$")
DISCORD_ID = re.compile(r"^\d{5,20}$")  # a Discord snowflake: what KEY takes after "dc:"
TEXT_MAX = 4000          # characters kept of one message (a chat message is short; a pasted brief is not)
RUN_GAP = timedelta(minutes=30)  # messages of one side closer than this are one 沟通记录 row
BACKFILL = timedelta(days=3)     # linking a chat logs its last few days; older messages stay out of the sheet
QUEUE_MAX = 12           # messages one draft may hand to Discord
QUEUE_TTL = timedelta(hours=6)  # a Discord draft not sent by then is stale: the extension never fills it
PART_MAX = {"telegram": 4000, "discord": 1900}  # one chat message (Telegram's limit is 4096, Discord's 2000)
EXTRA_FIELD_MAX = 2000   # characters of one string in a message's extra (a voice transcript, a link preview)
RECONCILED = "noted_reconciled"  # kv: "1" once msg.noted was set from the transcript files (Store.reconcile)
EDITS_MAX = 20           # edits of one message kept one by one; past that a change already kept is not kept again

SCHEMA = """
CREATE TABLE IF NOT EXISTS conv (
  key TEXT PRIMARY KEY, channel TEXT NOT NULL, peer_id TEXT, peer_name TEXT, username TEXT,
  kol_id TEXT, kol_name TEXT, kol_email TEXT, linked_at TEXT,
  ignored INTEGER NOT NULL DEFAULT 0, seen_at TEXT, last_at TEXT, last_in_at TEXT, last_out_at TEXT,
  created_at TEXT NOT NULL, no_autolink INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS msg (
  conv TEXT NOT NULL, id TEXT NOT NULL, at TEXT NOT NULL, out INTEGER NOT NULL, author TEXT, text TEXT,
  extra TEXT, zh TEXT, logged INTEGER NOT NULL DEFAULT 0, noted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (conv, id));
CREATE INDEX IF NOT EXISTS msg_at ON msg (conv, at);
CREATE TABLE IF NOT EXISTS msg_edit (
  n INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT NOT NULL, id TEXT NOT NULL, found_at TEXT NOT NULL,
  old_text TEXT, old_extra TEXT, new_text TEXT, real INTEGER NOT NULL DEFAULT 1,
  noted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS msg_edit_conv ON msg_edit (conv, id);
CREATE TABLE IF NOT EXISTS msg_file (
  conv TEXT NOT NULL, id TEXT NOT NULL, n INTEGER NOT NULL, name TEXT, path TEXT, sha256 TEXT, size INTEGER,
  saved_at TEXT, error TEXT, tries INTEGER NOT NULL DEFAULT 0, src TEXT, PRIMARY KEY (conv, id, n));
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT NOT NULL, batch TEXT NOT NULL, n INTEGER NOT NULL,
  of_n INTEGER NOT NULL, text TEXT NOT NULL, zh TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        d = value
    else:
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def utc(value) -> str | None:
    d = parse_time(value)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def channel_of(key: str) -> str:
    return CHANNELS[key.split(":", 1)[0]]


def plain(text, limit: int = 120) -> str:
    """One line of a stranger's text, safe for a name or a list cell: no control or invisible characters."""
    s = "".join(ch for ch in str(text or "") if unicodedata.category(ch)[0] != "C" or ch == " ")
    return re.sub(r"\s+", " ", s).strip()[:limit]


def clean_text(text) -> str:
    """A message body: line breaks kept, other control and invisible characters dropped."""
    s = "".join(ch for ch in str(text or "") if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return re.sub(r"\n{3,}", "\n\n", s.replace("\r\n", "\n")).strip()[:TEXT_MAX]


def trim_extra(value, depth: int = 0):
    """A message's extra (media, link preview, transcript, attachments) with every string cut to EXTRA_FIELD_MAX and
    lists to 20 items, so what is stored is always whole JSON."""
    if isinstance(value, str):
        return value[:EXTRA_FIELD_MAX]
    if depth > 3:
        return None
    if isinstance(value, dict):
        return {str(k)[:60]: trim_extra(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [trim_extra(v, depth + 1) for v in value[:20]]
    return value if isinstance(value, (int, float, bool)) or value is None else str(value)[:EXTRA_FIELD_MAX]


def norm_words(text) -> str:
    """Text compared the way a person reads it: case, spacing and invisible characters aside."""
    return re.sub(r"\s+", " ", clean_text(text)).strip().casefold()


MARKS = re.compile(r"[*_~`#>|\s\u2022]+")  # what Markdown is written with, and what it is drawn as (a bullet)


def same_words(a, b) -> bool:
    """The same message read two ways: Discord's page shows **bold** as bold and a list as bullets, its own data
    keeps the marks. Only those marks and spacing are set aside, so a translation made for one reading is kept
    for the other; any other difference is an edit and loses it: a word, a digit, also a comma that became a
    point inside a number (1,000 and 1.000) or a full stop that became a question mark."""
    if (a or "") == (b or ""):
        return True
    x, y = (MARKS.sub("", re.sub(r"(?m)^\s*-\s+", "", unicodedata.normalize("NFKC", str(t or "")))).casefold()
            for t in (a, b))
    return bool(x) and x == y


def peer_token(conv: dict) -> str:
    """What the sheet's 聊天账号 column holds for this chat: the peer's id, plus a name a person recognises."""
    prefix = conv["key"].split(":", 1)[0]
    pid = str(conv.get("peer_id") or "")
    if not pid.isdigit():
        return ""
    shown = ("@" + conv["username"]) if conv.get("username") and prefix == "tg" else conv.get("peer_name") or ""
    shown = re.sub(r"[()\[\];\s]+", " ", plain(shown, 40)).strip()
    return f"{prefix}:{pid}" + (f"({shown})" if shown else "")


def name_key(text) -> str:
    """A name reduced for matching a chat to a KOL总表 row: letters and digits only, lower case, without the
    channel-type suffixes KOLs add (PixelFox_ = PixelFox, RiverOwlTTV = RiverOwl, MoonBakerYT = MoonBaker)."""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    s = "".join(ch for ch in s if ch.isalnum())
    for suffix in ("official", "ttv", "tv", "yt", "gaming", "games"):
        if s.endswith(suffix) and len(s) - len(suffix) >= 4:
            s = s[: -len(suffix)]
            break
    return s


def holds_token(cell, token: str) -> bool:
    """Whether a 聊天账号 cell names this account (token without its display part). The display parts in brackets
    are dropped first: a Discord name may itself read "tg:<someone else's id>"."""
    ids = re.sub(r"\([^)]*\)", " ", str(cell or "")).lower()
    return bool(token) and re.search(rf"(?<![\w:]){re.escape(token)}(?!\d)", ids) is not None


def token_row(conv: dict, rows: list[dict]) -> dict | None:
    """The one KOL总表 row whose 聊天账号 holds this chat's account, if exactly one does."""
    token = peer_token(conv).split("(", 1)[0].lower()
    hits = [r for r in rows if holds_token(r.get("chat"), token)]
    return hits[0] if len(hits) == 1 else None


def suggest(conv: dict, rows: list[dict], limit: int = 3) -> list[dict]:
    """KOL总表 rows that look like this chat's peer (rows: {"id", "kol", "chat"?, ...}). An exact 聊天账号 hit comes
    first; then names equal after name_key; then one containing the other (5 characters at least)."""
    token = peer_token(conv).split("(", 1)[0].lower()
    names = {name_key(conv.get("peer_name")), name_key(conv.get("username"))} - {""}
    scored = []
    for r in rows:
        if holds_token(r.get("chat"), token):
            scored.append((0, r))
            continue
        k = name_key(r.get("kol"))
        if not k:
            continue
        if k in names:
            scored.append((1, r))
        elif len(k) >= 5 and any(len(n) >= 5 and (k in n or n in k) for n in names):
            scored.append((2, r))
    scored.sort(key=lambda x: x[0])
    return [r for _, r in scored[:limit]]


def runs(messages: list[dict]) -> list[list[dict]]:
    """Messages (oldest first) grouped into what one 沟通记录 row says: one side's messages in a row, none of them
    more than RUN_GAP after the one before."""
    out: list[list[dict]] = []
    for m in messages:
        last = out[-1][-1] if out else None
        if last and bool(last["out"]) == bool(m["out"]) and \
                (parse_time(m["at"]) or datetime.min.replace(tzinfo=timezone.utc)) - \
                (parse_time(last["at"]) or datetime.min.replace(tzinfo=timezone.utc)) <= RUN_GAP:
            out[-1].append(m)
        else:
            out.append([m])
    return out


def gist(messages: list[dict], limit: int = 110) -> str:
    """The run on one line, in Chinese when the translation is there."""
    parts = [plain(m.get("zh") or m.get("text") or describe_extra(m), 400) for m in messages]
    one = " / ".join(p for p in parts if p)
    return (one[:limit] + "…") if len(one) > limit else one or "（没有文字）"


def describe_extra(m: dict) -> str:
    extra = m.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    if extra.get("media"):
        return f"[{plain(extra['media'], 60)}]"
    if extra.get("attachments"):
        return f"[附件 {len(extra['attachments'])} 个]"
    return ""


def file_names(m: dict) -> list[str]:
    """What a message carries besides its words, by name: a Discord message's attachments, or what telegram-mcp
    calls a Telegram message's media ("photo", "document: rates.pdf")."""
    extra = m.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    if not isinstance(extra, dict):
        return []
    if isinstance(extra.get("attachments"), list):
        return [plain(a.get("name"), 120) or "file" for a in extra["attachments"] if isinstance(a, dict)]
    return [plain(extra["media"], 120)] if extra.get("media") else []


def one_line(text, limit: int) -> str:
    """Text as one cell of the transcript's table: no pipe, no line break. splitlines also breaks at U+2028 and
    the like: a reader splitting lines must never see a row of its own."""
    return " / ".join(s.strip() for s in str(text or "").replace("|", "｜").splitlines() if s.strip())[:limit]


def row(conv: dict, at: datetime, out: bool, content: str) -> str:
    who = plain(conv.get("kol_name") or conv.get("peer_name"), 60).replace("|", "｜")
    return (f"| {at:%Y-%m-%d %H:%M} | {LABELS[channel_of(conv['key'])]} | {conv.get('kol_id') or '-'} | {who} "
            f"| {'发' if out else '收'} | {content} |")


OWN_MARKS = re.compile(r"【(已编辑|旧文本|新文本|附件补存)】")  # what only KOL Mail's own rows say


def not_ours(text: str) -> str:
    """Words from a chat in which one of the transcript's own marks stands: the mark is written with other
    brackets, so a message can never pass for a row saying a message was edited. Nothing else is changed."""
    return OWN_MARKS.sub(r"〔\1〕", text)


def transcript_line(conv: dict, m: dict, tz: timezone, files: str = "", legacy: bool = False) -> str:
    """One row of 私信记录\\<date>.md, the file the triage task reads: local time, channel, KOL, side, text.
    files: what app.py says of the message's attachments (their names, and where the copies are); it follows the
    words. legacy: the row as the version before wrote it, which is what reconcile matches on."""
    at = (parse_time(m["at"]) or datetime.now(timezone.utc)).astimezone(tz)
    words = one_line(clean_text(m.get("text")), 1000)
    mark = one_line(files, 500) if files else ("" if words else one_line(describe_extra(m), 200))
    content = " ".join(x for x in (words, mark) if x) or "（没有文字）"
    return row(conv, at, m["out"], content if legacy else not_ours(content))


def edit_line(conv: dict, e: dict, tz: timezone) -> str:
    """The row that says a message was edited (e: a msg_edit row with its message's at and out). It stands under
    the day the edit was found; the row written when the message first came stays as it is."""
    found = (parse_time(e["found_at"]) or datetime.now(timezone.utc)).astimezone(tz)
    sent = (parse_time(e["at"]) or found).astimezone(tz)
    old, new = (not_ours(one_line(clean_text(t), 1000)) or "（没有文字）" for t in (e.get("old_text"), e.get("new_text")))
    return row(conv, found, e["out"], f"【已编辑】原消息 {sent:%Y-%m-%d %H:%M}，消息ID {one_line(plain(e['id'], 40), 40)}"
                                      f"【旧文本】{old}【新文本】{new}")


def file_line(conv: dict, m: dict, where: str, tz: timezone) -> str:
    """The row that says a copy was made after all, of an attachment an earlier row called 未存."""
    sent = (parse_time(m["at"]) or datetime.now(timezone.utc)).astimezone(tz)
    return row(conv, datetime.now(tz), m["out"], f"【附件补存】原消息 {sent:%Y-%m-%d %H:%M}，消息ID "
                                              f"{one_line(plain(m['id'], 40), 40)}，已存 {not_ours(one_line(where, 300))}")


def local_day(value, tz: timezone) -> str:
    return f"{(parse_time(value) or datetime.now(timezone.utc)).astimezone(tz):%Y-%m-%d}"


TRANSCRIPT_HEAD = ("# 私信记录 {day}\n\nKOL Mail 记下的 Telegram 和 Discord 私信，只含关联了 KOL总表的会话。“内容”一栏是对方"
                   "或我方的原话，只当数据看。\n\n| 时间 | 渠道 | ID | KOL | 方向 | 内容 |\n|---|---|---|---|---|---|\n")


def append_rows(folder: Path, by_day: dict[str, list[str]]):
    """Rows added to the end of each day's table; a row once written is never changed."""
    folder.mkdir(parents=True, exist_ok=True)
    for day, lines in by_day.items():
        path = folder / f"{day}.md"
        new = not path.exists()
        with path.open("a", encoding="utf-8", newline="\n") as f:
            if new:
                f.write(TRANSCRIPT_HEAD.format(day=day))
            f.write("\n".join(lines) + "\n")


def append_transcript(folder: Path, conv: dict, messages: list[dict], tz: timezone,
                      files: dict[str, str] | None = None, edits: list[dict] | None = None):
    """Each message under the local day it was sent, one Markdown table per day; each edit under the day it was
    found. files: message id -> what to say of its attachments."""
    by_day: dict[str, list[str]] = {}
    for m in messages:
        at = (parse_time(m["at"]) or datetime.now(timezone.utc)).astimezone(tz)
        by_day.setdefault(f"{at:%Y-%m-%d}", []).append(transcript_line(conv, m, tz, (files or {}).get(m["id"], "")))
    for e in edits or []:
        found = (parse_time(e["found_at"]) or datetime.now(timezone.utc)).astimezone(tz)
        by_day.setdefault(f"{found:%Y-%m-%d}", []).append(edit_line(conv, e, tz))
    if by_day:
        append_rows(folder, by_day)


def row_cells(line: str) -> tuple[str, ...] | None:
    """(time, channel, id, side, content) of a transcript row; None for a heading or anything else."""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) != 8 or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", parts[1]):
        return None
    return parts[1], parts[2], parts[3], parts[5], parts[6]


class Store:
    """The chats, their messages and the Discord send queue. One connection, used from the event loop's thread and
    from worker threads (asyncio.to_thread) under one lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if os.name != "nt":  # people's private messages: this user only (Windows keeps the profile folder private)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")  # WAL keeps it safe; a sync of 60 chats is many writes
            self.db.executescript(SCHEMA)
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(conv)")}
            if "no_autolink" not in columns:  # a store from before 0.7.0's review
                self.db.execute("ALTER TABLE conv ADD COLUMN no_autolink INTEGER NOT NULL DEFAULT 0")
            if "noted" not in {r[1] for r in self.db.execute("PRAGMA table_info(msg)")}:
                # a store from before the transcript had a mark of its own: reconcile() finds what is in it
                self.db.execute("ALTER TABLE msg ADD COLUMN noted INTEGER NOT NULL DEFAULT 0")

    def _all(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _run(self, sql: str, args=()) -> int:
        with self.lock:
            return self.db.execute(sql, args).rowcount

    # ---------------------------------------------------------------- conversations

    def conv(self, key: str) -> dict | None:
        return self._one("SELECT * FROM conv WHERE key=?", (key,))

    def upsert_conv(self, key: str, peer_id=None, peer_name=None, username=None) -> dict:
        """Create the chat or refresh what we know of the peer; an empty value never wipes a known one."""
        if not KEY.match(key):
            raise ValueError(f"bad chat key {key!r}")
        pid = str(peer_id) if peer_id is not None and str(peer_id).lstrip("-").isdigit() else None
        name, user = plain(peer_name, 80) or None, (plain(username, 40).lstrip("@") or None)
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO conv (key, channel, created_at) VALUES (?, ?, ?)",
                            (key, channel_of(key), now()))
            self.db.execute("UPDATE conv SET peer_id=COALESCE(?, peer_id), peer_name=COALESCE(?, peer_name), "
                            "username=COALESCE(?, username) WHERE key=?", (pid, name, user, key))
        return self.conv(key)

    def convs(self, ignored: bool = False) -> list[dict]:
        """The chats, the latest first, each with a preview of its newest message. One query for the lot: the page
        asks for this list every ten seconds, and one query per chat held the lock that long with every chat more."""
        rows = self._all(
            "SELECT c.*, m.out AS p_out, m.text AS p_text, m.extra AS p_extra, m.zh AS p_zh, m.id AS p_id "
            "FROM conv c LEFT JOIN msg m ON m.conv = c.key AND m.rowid = "
            "(SELECT rowid FROM msg WHERE conv = c.key ORDER BY at DESC, id DESC LIMIT 1) "
            "WHERE c.ignored=? ORDER BY COALESCE(c.last_at, c.created_at) DESC", (1 if ignored else 0,))
        for r in rows:
            last = {"text": r.pop("p_text"), "extra": r.pop("p_extra"), "zh": r.pop("p_zh")}
            out, has = r.pop("p_out"), r.pop("p_id") is not None
            r["unread"] = unread(r)
            r["preview"] = plain(last["zh"] or last["text"] or describe_extra(last), 80) if has else ""
            r["preview_out"] = bool(has and out)
        return rows

    def link(self, key: str, kol_id: str | None, kol_name: str, kol_email: str | None):
        self._run("UPDATE conv SET kol_id=?, kol_name=?, kol_email=?, linked_at=?, ignored=0, no_autolink=0 "
                  "WHERE key=?",
                  (kol_id or None, plain(kol_name, 80) or None, (kol_email or "").strip().lower() or None, now(), key))

    def unlink(self, key: str):
        """The owner's own undo: 聊天账号 in the sheet may still name this account, so it is never linked again on its
        own (only by hand)."""
        self._run("UPDATE conv SET kol_id=NULL, kol_name=NULL, kol_email=NULL, linked_at=NULL, no_autolink=1 "
                  "WHERE key=?", (key,))

    def ignore(self, key: str, ignored: bool):
        self._run("UPDATE conv SET ignored=? WHERE key=?", (1 if ignored else 0, key))

    def seen(self, key: str):
        self._run("UPDATE conv SET seen_at=? WHERE key=?", (now(), key))

    # ---------------------------------------------------------------- messages

    def add_messages(self, key: str, messages: list[dict]) -> list[dict]:
        """Store messages ({"id", "at", "out", "author", "text", "extra"}); returns the ones not seen before, oldest
        first. A message seen again with other text (edited) is updated, and counts as new only once; the words it
        had before stay in msg_edit."""
        added = []
        with self.lock:
            self.db.execute("BEGIN")  # one commit for the lot, with conv's last_* times in the same transaction
            try:
                for m in sorted(messages, key=lambda m: (parse_time(m.get("at")) or datetime.min.replace(
                        tzinfo=timezone.utc), str(m.get("id")))):
                    mid, at = str(m.get("id") or "").strip(), utc(m.get("at"))
                    if not mid or not at or len(mid) > 40:
                        continue
                    text = clean_text(m.get("text"))
                    extra_obj = trim_extra(m.get("extra") or {})
                    extra = json.dumps(extra_obj, ensure_ascii=False)
                    cur = self.db.execute("INSERT OR IGNORE INTO msg (conv, id, at, out, author, text, extra) "
                                          "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                          (key, mid, at, 1 if m.get("out") else 0, plain(m.get("author"), 80), text,
                                           extra))
                    if cur.rowcount:
                        added.append({"conv": key, "id": mid, "at": at, "out": bool(m.get("out")),
                                      "author": plain(m.get("author"), 80), "text": text, "extra": extra_obj,
                                      "zh": None, "logged": 0, "noted": 0})
                    else:  # edited since, or a transcript / link preview came later: keep the newest; a changed
                        # text loses the translation of the old words (same_words: not for a mark or a space)
                        old = self.db.execute("SELECT text, extra, zh FROM msg WHERE conv=? AND id=?",
                                              (key, mid)).fetchone()
                        if old and (old[0] != text or old[1] != extra):
                            if (old[0] or "") != text:
                                self._keep_edit(key, mid, old[0] or "", old[1], text)
                            self.db.execute("UPDATE msg SET zh=?, text=?, extra=? WHERE conv=? AND id=?",
                                            (old[2] if same_words(old[0], text) else None, text, extra, key, mid))
                if added:
                    row = self.db.execute("SELECT MAX(at), MAX(CASE WHEN out=0 THEN at END), "
                                          "MAX(CASE WHEN out=1 THEN at END) FROM msg WHERE conv=?", (key,)).fetchone()
                    self.db.execute("UPDATE conv SET last_at=?, last_in_at=?, last_out_at=? WHERE key=?",
                                    (row[0], row[1], row[2], key))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return added

    def _keep_edit(self, key: str, mid: str, old_text: str, old_extra, new_text: str):
        """Inside add_messages' transaction: the words a message had before it was read with others. real is 0 when
        only the drawing differs (same_words: Discord's page and Discord's data), which is kept but is no edit to
        tell anyone of. Every real edit is kept, also back to words the message had before (a price changed and
        changed back). Only where two readings take turns is the same change kept once: a change of drawing, or
        a message with EDITS_MAX changes already, which no person made."""
        real = 0 if same_words(old_text, new_text) else 1
        if not real or self.db.execute("SELECT COUNT(*) FROM msg_edit WHERE conv=? AND id=?",
                                       (key, mid)).fetchone()[0] >= EDITS_MAX:
            if self.db.execute("SELECT 1 FROM msg_edit WHERE conv=? AND id=? AND old_text=? AND new_text=? LIMIT 1",
                               (key, mid, old_text, new_text)).fetchone():
                return
        self.db.execute("INSERT INTO msg_edit (conv, id, found_at, old_text, old_extra, new_text, real) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (key, mid, now(), old_text, old_extra, new_text, real))

    def edits(self, key: str, mid: str | None = None, waiting: bool = False) -> list[dict]:
        """A chat's kept edits, oldest first, each with its message's time and side; of one message with mid;
        waiting: only the real ones the transcript does not tell of yet."""
        sql = ("SELECT e.*, m.at AS at, m.out AS out FROM msg_edit e JOIN msg m ON m.conv = e.conv AND m.id = e.id "
               "WHERE e.conv=?")
        args: list = [key]
        if mid is not None:
            sql, args = sql + " AND e.id=?", args + [str(mid)]
        if waiting:
            sql += " AND e.real=1 AND e.noted=0"
        rows = self._all(sql + " ORDER BY e.n", args)
        for r in rows:
            r["out"] = bool(r["out"])
        return rows

    def mark_edits_noted(self, ns: list[int]):
        with self.lock:
            self.db.executemany("UPDATE msg_edit SET noted=1 WHERE n=?", [(n,) for n in ns])

    @staticmethod
    def _parsed(rows: list[dict]) -> list[dict]:
        for r in rows:
            r["out"] = bool(r["out"])
            try:
                r["extra"] = json.loads(r["extra"] or "{}")
            except ValueError:
                r["extra"] = {}
        return rows

    def messages(self, key: str, limit: int = 80, before: str | None = None) -> list[dict]:
        """The newest `limit` messages, oldest first; with before (a message id), the ones older than that message,
        which is how the page reaches the start of a long chat a screenful at a time."""
        if before is None:
            rows = self._all("SELECT * FROM msg WHERE conv=? ORDER BY at DESC, id DESC LIMIT ?", (key, limit))
        else:
            edge = self._one("SELECT at, id FROM msg WHERE conv=? AND id=?", (key, before))
            if not edge:
                return []
            rows = self._all("SELECT * FROM msg WHERE conv=? AND (at < ? OR (at = ? AND id < ?)) "
                             "ORDER BY at DESC, id DESC LIMIT ?", (key, edge["at"], edge["at"], edge["id"], limit))
        return self._parsed(rows)[::-1]

    def count(self, key: str) -> int:
        return (self._one("SELECT COUNT(*) AS n FROM msg WHERE conv=?", (key,)) or {}).get("n", 0)

    def from_page(self, key: str, ids: list[str]) -> set[str]:
        """Of these message ids, the ones stored from Discord's own data (extra src "page", see app.ext_feed). What
        the extension reads off the screen for the same message is the same words drawn another way: it never
        replaces them, or the two readings would overwrite each other, and the translation, on every sync."""
        found = set()
        ids = [str(i) for i in ids][:500]
        if not ids:
            return found
        marks = ",".join("?" * len(ids))
        for r in self._all(f"SELECT id, extra FROM msg WHERE conv=? AND id IN ({marks})", (key, *ids)):
            try:
                if (json.loads(r["extra"] or "{}") or {}).get("src") == "page":
                    found.add(r["id"])
            except (ValueError, AttributeError):
                pass
        return found

    def newest_id(self, key: str) -> str | None:
        row = self._one("SELECT id FROM msg WHERE conv=? ORDER BY at DESC, id DESC LIMIT 1", (key,))
        return row["id"] if row else None

    def set_zh(self, key: str, mid: str, zh: str):
        self._run("UPDATE msg SET zh=? WHERE conv=? AND id=?", (zh, key, mid))

    def unlogged(self, key: str) -> list[dict]:
        return [m for m in self.messages(key, 500) if not m["logged"]]

    def mark_logged(self, key: str, ids: list[str]):
        with self.lock:
            self.db.executemany("UPDATE msg SET logged=1 WHERE conv=? AND id=?", [(key, i) for i in ids])

    def unskip_after(self, key: str, after: datetime):
        """What came after the sheet's newest row for this chat is not in the sheet, whatever skip_old said."""
        self._run("UPDATE msg SET logged=0 WHERE conv=? AND logged=1 AND at>?",
                  (key, after.astimezone(timezone.utc).isoformat()))

    def skip_old(self, key: str, before: datetime):
        """On linking: what is older than the backfill window never goes to the sheet. (The transcript takes it all
        the same: noted is not touched.)"""
        self._run("UPDATE msg SET logged=1 WHERE conv=? AND at<?", (key, before.astimezone(timezone.utc).isoformat()))

    # ---------------------------------------------------------------- the transcript's own mark

    def unnoted(self, key: str) -> list[dict]:
        """Every message of the chat the transcript does not hold yet, oldest first: a chat's whole history when
        it is linked, however old."""
        return self._parsed(self._all("SELECT * FROM msg WHERE conv=? AND noted=0 ORDER BY at, id", (key,)))

    def mark_noted(self, key: str, ids: list[str]):
        with self.lock:
            self.db.executemany("UPDATE msg SET noted=1 WHERE conv=? AND id=?", [(key, i) for i in ids])

    def waiting(self, key: str) -> bool:
        """Whether the transcript is behind for this chat: a message or a real edit it does not tell of."""
        row = self._one("SELECT EXISTS(SELECT 1 FROM msg WHERE conv=? AND noted=0) OR "
                        "EXISTS(SELECT 1 FROM msg_edit WHERE conv=? AND real=1 AND noted=0) AS w", (key, key))
        return bool(row and row["w"])

    def reconcile(self, folder: Path, tz: timezone) -> tuple[int, int] | None:
        """Once, for a store from before msg.noted: every message whose row is in the transcript files is marked
        noted, so only what is missing gets written. A row is matched by its time, channel, side and words, first
        with the KOL id it would carry now, then without (the chat may have been linked to another row then).
        Only a message the version before had logged can be in the files: it wrote the row and the mark together.
        Returns (found, missing), or None when this store was reconciled before. OSError when the files cannot be
        read now (the folder's drive is away, a file is locked or is not text): nothing is marked and the next
        call tries again, since going on would write every message a second time."""
        if self.get_kv(RECONCILED) == "1":
            return None
        if not folder.is_dir() and not folder.parent.is_dir():
            raise OSError(f"{folder.parent} is not there")  # not "nothing written yet": the place itself is away
        have: dict[tuple, int] = {}
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError as e:
                raise OSError(f"{path.name} is not UTF-8 text: {e}") from e
            for line in lines:
                cells = row_cells(line)
                if cells:
                    have[cells] = have.get(cells, 0) + 1
        loose: dict[tuple, int] = {}
        todo, found = [], []
        for conv in self._all("SELECT * FROM conv"):
            for m in self._parsed(self._all("SELECT * FROM msg WHERE conv=? AND noted=0 AND logged=1 ORDER BY at, id",
                                            (conv["key"],))):
                cells = row_cells(transcript_line(conv, m, tz, legacy=True))
                if cells and have.get(cells):
                    have[cells] -= 1
                    found.append((conv["key"], m["id"]))
                else:
                    todo.append((conv["key"], m["id"], cells))
        for cells, n in have.items():
            if n:
                k = cells[:2] + cells[3:]
                loose[k] = loose.get(k, 0) + n
        missing = 0
        for key, mid, cells in todo:
            k = cells[:2] + cells[3:] if cells else None
            if k and loose.get(k):
                loose[k] -= 1
                found.append((key, mid))
            else:
                missing += 1
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany("UPDATE msg SET noted=1 WHERE conv=? AND id=?", found)
                self.db.execute("INSERT INTO kv (k, v) VALUES (?, '1') ON CONFLICT(k) DO UPDATE SET v='1'",
                                (RECONCILED,))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        row = self._one("SELECT COUNT(*) AS n FROM msg WHERE noted=0")
        return len(found), row["n"] if row else missing

    # ---------------------------------------------------------------- copies of attachments

    def files(self, key: str, mid: str | None = None) -> list[dict]:
        """What is known of the chat's attachments (of one message with mid): the copy's path, size and SHA-256
        once saved, or why it is not and how often it was tried."""
        if mid is None:
            return self._all("SELECT * FROM msg_file WHERE conv=? ORDER BY id, n", (key,))
        return self._all("SELECT * FROM msg_file WHERE conv=? AND id=? ORDER BY n", (key, str(mid)))

    def file_saved(self, key: str, mid: str, n: int, name: str, path: str, sha256: str, size: int):
        self._run("INSERT INTO msg_file (conv, id, n, name, path, sha256, size, saved_at, error, tries) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1) ON CONFLICT(conv, id, n) DO UPDATE SET name=excluded.name, "
                  "path=excluded.path, sha256=excluded.sha256, size=excluded.size, saved_at=excluded.saved_at, "
                  "error=NULL, tries=tries+1", (key, str(mid), n, name, path, sha256, size, now()))

    def file_failed(self, key: str, mid: str, n: int, name: str, error: str, src: str = ""):
        """A copy that could not be made; one already made is never set aside by a later failure. src: the address
        it was asked from (a Discord attachment's address stops working within a day; a reading of the chat
        that brings another address is a new chance, whatever the number of tries)."""
        self._run("INSERT INTO msg_file (conv, id, n, name, error, tries, src) VALUES (?, ?, ?, ?, ?, 1, ?) "
                  "ON CONFLICT(conv, id, n) DO UPDATE SET error=excluded.error, tries=tries+1, src=excluded.src "
                  "WHERE path IS NULL", (key, str(mid), n, name, plain(error, 300), src))

    def message(self, key: str, mid: str) -> dict | None:
        rows = self._parsed(self._all("SELECT * FROM msg WHERE conv=? AND id=?", (key, str(mid))))
        return rows[0] if rows else None

    # ---------------------------------------------------------------- Discord send queue

    def queue(self, key: str, parts: list[dict]) -> str:
        """A new draft for Discord replaces whatever of an older one was not sent yet."""
        batch = f"{int(time.time() * 1000)}"
        parts = parts[:QUEUE_MAX]
        with self.lock:
            self.db.execute("UPDATE outbox SET status='dropped', updated_at=? WHERE conv=? AND status IN "
                            "('pending', 'filled')", (now(), key))
            for i, p in enumerate(parts, 1):
                self.db.execute("INSERT INTO outbox (conv, batch, n, of_n, text, zh, status, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                                (key, batch, i, len(parts), p["text"], p.get("zh") or "", now(), now()))
        return batch

    def pending(self, key: str) -> list[dict]:
        """What is still to be sent in Discord. A draft goes stale, and is dropped, once the other side wrote after it
        was made, or after QUEUE_TTL: the extension must never fill in an answer to a conversation that moved on.
        One of our own messages with an item's exact words means the owner sent it (the extension may not have
        said so)."""
        with self.lock:
            conv = self.db.execute("SELECT last_in_at FROM conv WHERE key=?", (key,)).fetchone()
            stale_before = (datetime.now(timezone.utc) - QUEUE_TTL).isoformat()
            last_in = conv[0] if conv and conv[0] else ""
            self.db.execute("UPDATE outbox SET status='dropped', updated_at=? WHERE conv=? AND status IN "
                            "('pending', 'filled') AND (created_at < ? OR created_at < ?)",
                            (now(), key, stale_before, last_in))
            rows = [dict(r) for r in self.db.execute(
                "SELECT id, n, of_n, text, zh, status, created_at FROM outbox WHERE conv=? AND status IN "
                "('pending', 'filled') ORDER BY id", (key,)).fetchall()]
            if rows:
                ours = {norm_words(r[0]) for r in self.db.execute(
                    "SELECT text FROM msg WHERE conv=? AND out=1 AND at>=?", (key, min(r["created_at"] for r in rows)))}
                done = [r["id"] for r in rows if norm_words(r["text"]) in ours]
                if done:
                    self.db.executemany("UPDATE outbox SET status='sent', updated_at=? WHERE id=?",
                                        [(now(), i) for i in done])
                    rows = [r for r in rows if r["id"] not in done]
        return rows

    def update_item(self, key: str, item_id: int, status: str) -> bool:
        """True when this call changed the item. "filled" is a claim: only one caller (one Discord tab) moves an item
        from pending to filled, so two tabs never both paste it; sent and dropped are final."""
        if status not in ("filled", "sent", "dropped"):
            raise ValueError(status)
        allowed = "('pending')" if status == "filled" else "('pending', 'filled')"
        return bool(self._run(f"UPDATE outbox SET status=?, updated_at=? WHERE conv=? AND id=? AND status IN {allowed}",
                              (status, now(), key, item_id)))

    # ---------------------------------------------------------------- small state

    def backup(self, dest: Path):
        """A whole copy of the store as it is now (sqlite's own backup, safe while the store is in use), written
        next to dest first and moved over it once complete."""
        part = dest.with_name(dest.name + ".part")
        with contextlib.suppress(OSError):
            part.unlink()
        out = sqlite3.connect(str(part))
        try:
            with self.lock:
                self.db.backup(out)
        finally:
            out.close()
        os.replace(part, dest)

    def get_kv(self, k: str) -> str | None:
        row = self._one("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else None

    def set_kv(self, k: str, v: str):
        self._run("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def unread(conv: dict) -> bool:
    """The peer wrote last, after the owner last opened the chat."""
    last_in = parse_time(conv.get("last_in_at"))
    if not last_in:
        return False
    after = [t for t in (parse_time(conv.get("last_out_at")), parse_time(conv.get("seen_at"))) if t]
    return not after or last_in > max(after)
