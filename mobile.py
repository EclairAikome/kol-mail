"""Off-hours replies reviewed on the phone through a private Telegram bot.

A KOL writes while the owner is off work. Nothing goes out by itself: kol-mail opens a "case", asks the model for
2-3 strategies and pushes them to the owner's Telegram chat with the bot. The owner taps a strategy, reads the draft
as paragraph-by-paragraph Chinese, asks questions or requests changes by typing, and finally taps 发送 and confirms.
If the owner never approves, nothing is sent; the draft waits in kol-mail for the next working day.

This module holds the bot client, the case store and the message formats. app.py runs the loop and does the mail work.
The bot only ever talks to one chat (the owner, bound once with a pairing code); everything else is ignored."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

API = "https://api.telegram.org/bot{token}/{method}"
TEXT_LIMIT = 3900  # Telegram allows 4096 characters per message; leave room for the header
PAIRING_TTL = 600
PAIRING_TRIES = 5  # wrong codes before the current one is void; with 8 digits a guess is hopeless
HOLD_MAX = 30                # pushes kept while Telegram cannot be reached
HOLD_SECONDS = 12 * 3600     # a push older than this is not sent any more


class BotError(Exception):
    """transient: Telegram could not be reached or had a server error (5xx, 429), so the same call can work later
    (seen on a colleague's Mac 2026-10-28: ConnectError, ReadTimeout, Bad Gateway within one night). Otherwise
    Telegram refused the call itself: a wrong token, a blocked bot, a message it will never take."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class Bot:
    def __init__(self, token: str):
        self.token = token
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(70, connect=15))

    async def call(self, method: str, **params):
        try:
            r = await self.client.post(API.format(token=self.token, method=method), json=params)
        except httpx.HTTPError as e:
            raise BotError(f"连不上 Telegram：{type(e).__name__}", transient=True) from e
        try:
            data = r.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):  # not Telegram's answer at all: a VPN or proxy page, a gateway in between
            raise BotError(f"Telegram 返回了无法解析的内容（HTTP {r.status_code}）", transient=True)
        if not data.get("ok"):
            code = data.get("error_code") if isinstance(data.get("error_code"), int) else r.status_code
            raise BotError(str(data.get("description") or f"HTTP {r.status_code}")[:200],
                           transient=code == 429 or code >= 500)
        return data.get("result")

    async def updates(self, offset: int, timeout: int = 50) -> list[dict]:
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message", "callback_query"]) or []

    async def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None,
                   reply_to: int | None = None) -> list[int]:
        """Plain text (no parse mode, so nothing in a KOL's mail can format or inject markup), split into
        Telegram-sized pieces; the buttons go on the last piece. Returns the message ids."""
        ids = []
        pieces = split_text(text)
        for n, piece in enumerate(pieces):
            params = {"chat_id": chat_id, "text": piece, "disable_web_page_preview": True}
            if buttons and n == len(pieces) - 1:
                params["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                              for row in buttons]}
            if reply_to and n == 0:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            result = await self.call("sendMessage", **params)
            ids.append(int(result["message_id"]))
        return ids

    async def answer(self, callback_id: str, text: str = ""):
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except BotError:
            pass  # only the little "loading" spinner on the phone depends on it

    async def drop_buttons(self, chat_id: int, message_id: int):
        try:
            await self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                            reply_markup={"inline_keyboard": []})
        except BotError:
            pass

    async def close(self):
        await self.client.aclose()


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    text = (text or "").strip() or "（空）"
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


# ---------------------------------------------------------------- pairing

class Pairing:
    """A one-time code shown in kol-mail; whoever sends /start <code> to the bot within 10 minutes becomes its owner."""

    def __init__(self):
        self.code: str | None = None
        self.expires = 0.0
        self.misses = 0

    def new(self) -> str:
        self.code = f"{secrets.randbelow(10**8):08d}"
        self.expires = time.time() + PAIRING_TTL
        self.misses = 0
        return self.code

    def take(self, text: str) -> bool:
        if self.code is None or time.time() >= self.expires:
            return False
        parts = (text or "").split()
        given = parts[1] if len(parts) == 2 and parts[0].split("@")[0] == "/start" else ""
        ok = given.isascii() and secrets.compare_digest(given.encode(), self.code.encode())
        if ok:
            self.code = None
        else:
            self.misses += 1
            if self.misses >= PAIRING_TRIES:
                self.code = None  # make a new one in kol-mail
        return ok


# ---------------------------------------------------------------- cases

STATUS_ZH = {"new": "准备中", "strategies": "等你选方案", "drafted": "等你确认", "confirming": "等你确认发送",
             "sending": "发送中", "sent": "已发送", "uncertain": "发送结果不确定", "skipped": "不回了",
             "handled": "已在电脑上处理", "superseded": "有更新的来信", "failed": "出错了"}
OPEN = ("new", "strategies", "drafted", "confirming")


class Cases:
    def __init__(self, path: Path, tz=None):
        self.tz = tz
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL, peer TEXT NOT NULL, ref TEXT NOT NULL UNIQUE,
                mailbox TEXT, email_id TEXT, who TEXT, subject TEXT,
                status TEXT NOT NULL, strategies TEXT, chosen INTEGER, body TEXT, pairs_ok INTEGER DEFAULT 0,
                zh TEXT, language TEXT, error TEXT, messages TEXT DEFAULT '[]',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    def _now(self) -> str:
        return datetime.now(self.tz).astimezone(self.tz).isoformat(timespec="seconds")

    def get_kv(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set_kv(self, key: str, value: str):
        self.db.execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
        self.db.commit()

    def open_case(self, channel: str, peer: str, ref: str, **fields) -> int | None:
        """A new case for this incoming message, or None if it already has one. An older open case for the same
        person is superseded: the newer message is the one to answer."""
        now = self._now()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO cases (channel, peer, ref, mailbox, email_id, who, subject, status, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (channel, peer.lower(), ref, fields.get("mailbox"), fields.get("email_id"), fields.get("who"),
             fields.get("subject"), "new", now, now))
        if cur.rowcount != 1:
            self.db.commit()
            return None
        case_id = cur.lastrowid
        self.db.execute(f"UPDATE cases SET status='superseded', updated_at=? WHERE channel=? AND peer=? AND id<>? AND "
                        f"status IN ({','.join('?' * len(OPEN))})", (now, channel, peer.lower(), case_id, *OPEN))
        self.db.commit()
        return case_id

    def get(self, case_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["strategies"] = json.loads(d["strategies"]) if d["strategies"] else None
        d["messages"] = json.loads(d["messages"] or "[]")
        return d

    def update(self, case_id: int, **fields):
        if "strategies" in fields and fields["strategies"] is not None:
            fields["strategies"] = json.dumps(fields["strategies"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE cases SET {sets}, updated_at=? WHERE id=?", (*fields.values(), self._now(), case_id))
        self.db.commit()

    def move(self, case_id: int, old: tuple[str, ...], new: str) -> bool:
        """Compare-and-set on the status, so two taps on the phone can never both act."""
        cur = self.db.execute(f"UPDATE cases SET status=?, updated_at=? WHERE id=? AND status IN "
                              f"({','.join('?' * len(old))})", (new, self._now(), case_id, *old))
        self.db.commit()
        return cur.rowcount == 1

    def remember_message(self, case_id: int, message_ids: list[int]):
        case = self.get(case_id)
        if case:
            self.update(case_id, messages=json.dumps((case["messages"] + message_ids)[-50:]))

    def by_message(self, message_id: int) -> dict | None:
        for row in self.db.execute("SELECT id, messages FROM cases ORDER BY id DESC LIMIT 200"):
            if message_id in json.loads(row["messages"] or "[]"):
                return self.get(row["id"])
        return None

    def all_open(self) -> list[dict]:
        rows = self.db.execute(f"SELECT id FROM cases WHERE status IN ({','.join('?' * len(OPEN))}) "
                               f"ORDER BY updated_at DESC", OPEN).fetchall()
        return [self.get(r[0]) for r in rows]

    def latest_open(self) -> dict | None:
        row = self.db.execute(f"SELECT id FROM cases WHERE status IN ({','.join('?' * len(OPEN))}) "
                              f"ORDER BY updated_at DESC LIMIT 1", OPEN).fetchone()
        return self.get(row[0]) if row else None

    def for_ref(self, ref: str) -> dict | None:
        row = self.db.execute("SELECT id FROM cases WHERE ref=?", (ref,)).fetchone()
        return self.get(row[0]) if row else None

    def open_for_peer(self, channel: str, peer: str) -> list[dict]:
        rows = self.db.execute(f"SELECT id FROM cases WHERE channel=? AND peer=? AND status IN "
                               f"({','.join('?' * len(OPEN))})", (channel, peer.lower(), *OPEN)).fetchall()
        return [self.get(r[0]) for r in rows]

    def count_since(self, channel: str, since: datetime) -> int:
        rows = self.db.execute("SELECT created_at FROM cases WHERE channel=?", (channel,)).fetchall()
        return sum(1 for r in rows if datetime.fromisoformat(r[0]) >= since)

    def stuck(self) -> list[dict]:
        """Cases a crash left half way: 'new' never got its strategies, 'sending' may or may not have gone out."""
        rows = self.db.execute("SELECT id FROM cases WHERE status IN ('new','sending')").fetchall()
        return [self.get(r[0]) for r in rows]

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT id FROM cases ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self.get(r[0]) for r in rows]

    # Pushes Telegram could not take (unreachable, a server error), kept here so a restart does not lose them.

    def held(self) -> list[dict]:
        try:
            items = json.loads(self.get_kv("tg_outbox", "[]"))
        except ValueError:
            return []
        return [p for p in items if isinstance(p, dict) and p.get("id")] if isinstance(items, list) else []

    def hold(self, chat_id: int, text: str, buttons, case_id: int | None, reply_to: int | None,
             now: float | None = None):
        push = {"id": secrets.token_hex(6), "at": time.time() if now is None else now, "chat": chat_id,
                "text": text, "buttons": buttons or None, "case_id": case_id, "reply_to": reply_to, "tries": 0}
        items = self.held() + [push]
        self.set_kv("tg_outbox", json.dumps(items[-HOLD_MAX:], ensure_ascii=False))
        return items[:-HOLD_MAX]  # pushed out by newer ones

    def unhold(self, push_id: str):
        self.set_kv("tg_outbox", json.dumps([p for p in self.held() if p["id"] != push_id], ensure_ascii=False))

    def tried(self, push_id: str) -> int:
        items = self.held()
        for p in items:
            if p["id"] == push_id:
                p["tries"] = int(p.get("tries") or 0) + 1
                self.set_kv("tg_outbox", json.dumps(items, ensure_ascii=False))
                return p["tries"]
        return 0

    def drop_held(self):
        self.set_kv("tg_outbox", "[]")


HOLD_TRIES = 5  # failed resends while Telegram does answer the bot: by then the push itself is the problem
BUTTON_VERSION = re.compile(r"^[pc]:(\d+):(\w+)$")  # 发送 / 确认发送 carry the draft they were shown with


def stale_push(push: dict, case: dict | None, chat_id: int, now: float) -> str:
    """Why a held push is not worth sending any more, or "" when it still is."""
    if push.get("chat") and push["chat"] != chat_id:
        return "the phone was paired again"
    if push.get("buttons") and case:
        if case["status"] not in OPEN:
            return f"case {case['id']} is {case['status']}"
        for row in push["buttons"]:
            for _label, data in row:
                m = BUTTON_VERSION.match(str(data))
                if m and m.group(2) != version(case.get("body") or ""):
                    return f"case {case['id']} has a newer draft"
        return ""  # waiting for the owner: it stays however long Telegram is away (HOLD_MAX still bounds it)
    if now - float(push.get("at") or 0) > HOLD_SECONDS:
        return "too old"
    return ""


async def resend_held(bot: Bot, chat_id: int, cases: Cases, now: float | None = None, note=lambda *a: None) -> int:
    """Send the held pushes, oldest first, and stop at the first one Telegram still cannot take. Pushes that
    stale_push rules out are dropped: buttons for a case closed meanwhile (sent, skipped, answered on the computer) or
    for an older draft could only ask for something already done or replaced. A long text that failed after its first
    piece arrives with that piece twice; a lost push is worse. note(...) gets a line for every push dropped. Returns
    how many went out."""
    now = time.time() if now is None else now
    sent = 0
    for push in cases.held():
        case = cases.get(push["case_id"]) if push.get("case_id") else None
        why = stale_push(push, case, chat_id, now)
        if why:
            cases.unhold(push["id"])
            note("held push dropped:", why)
            continue
        try:
            ids = await bot.send(chat_id, push["text"], push.get("buttons"), push.get("reply_to"))
        except BotError as e:
            if not e.transient:
                cases.unhold(push["id"])  # refused for good (too long, blocked bot): trying again cannot help
                note("held push dropped: Telegram refused it:", str(e))
                continue
            if cases.tried(push["id"]) >= HOLD_TRIES:
                cases.unhold(push["id"])
                note(f"held push dropped after {HOLD_TRIES} failed resends:", str(e))
                continue
            break
        cases.unhold(push["id"])
        if case:
            cases.remember_message(case["id"], ids)
        sent += 1
    return sent


# ---------------------------------------------------------------- owner commands
# Fixed commands, executed by plain code (no AI), only for the paired owner. Telegram's menu only shows latin command
# names, so each has an English name for the menu and a Chinese one to type.

COMMANDS = {  # name -> (Chinese aliases, menu description)
    "template": (("模板", "固定模板"), "下班改发固定模板自动回复"),
    "ai": (("AI", "ai审批", "AI审批", "手机审批"), "下班改成 AI 起草、手机审批"),
    "status": (("状态",), "看现在的开关和待处理的来信"),
    "holiday": (("休假", "放假"), "把一段日期设成假期：/holiday 10-25 11-02"),
    "unholiday": (("取消休假", "取消放假"), "把一段日期从假期里去掉"),
    "help": (("帮助",), "列出所有指令"),
}
MAX_HOLIDAY_DAYS = 60
DATE_RE = re.compile(r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?")


def parse_command(text: str) -> tuple[str, str] | None:
    """(command, rest of the line) when the whole message is one of the owner commands, else None. A sentence that
    merely contains a command word stays a comment on a draft."""
    t = (text or "").strip()
    head, _, rest = t.partition(" ")
    word = head.lstrip("/").split("@")[0]
    for name, (aliases, _desc) in COMMANDS.items():
        if word.lower() == name or word in aliases:
            if name in ("holiday", "unholiday") or not rest.strip():
                return name, rest.strip()
    return None


def parse_dates(text: str, today: date) -> list[date]:
    """'10-25 11-02', '10-25 到 11-02', '2026-12-24' or '12月24日': every day from the first to the last date given.
    A date without a year is the next such day from today (so in December, 01-03 means next January)."""
    found = []
    for y, m, d in DATE_RE.findall(text or ""):
        year = int(y) if y else today.year
        day = date(year, int(m), int(d))
        if not y and day < today - timedelta(days=1):
            day = date(year + 1, int(m), int(d))
        found.append(day)
    if not found or len(found) > 2:
        raise ValueError("看不懂日期。写法例如 /holiday 10-25 11-02，或者 /holiday 2026-12-24")
    start, end = found[0], found[-1]
    if end < start:
        raise ValueError("结束日期早于开始日期")
    days = (end - start).days + 1
    if days > MAX_HOLIDAY_DAYS:
        raise ValueError(f"一次最多 {MAX_HOLIDAY_DAYS} 天")
    return [start + timedelta(days=i) for i in range(days)]


def help_text() -> str:
    lines = ["可以用的指令（也可以直接打中文名）："]
    for name, (aliases, desc) in COMMANDS.items():
        lines.append(f"/{name}（{aliases[0]}）  {desc}")
    lines.append("其他文字会当成对某封草稿的提问或修改意见；有几封在等时，长按那条草稿选“回复”再写。")
    return "\n".join(lines)


def menu() -> list[dict]:
    return [{"command": name, "description": desc[:60]} for name, (_a, desc) in COMMANDS.items()]


# ---------------------------------------------------------------- message formats (Chinese, plain text)

def strategies_text(case: dict, incoming_zh: str, situation: str, options: list[dict], recommended: int) -> str:
    channel = "邮件" if case["channel"] == "email" else "Telegram"
    lines = [f"📩 #{case['id']} 新{channel} · {case['who']}"]
    if case.get("subject"):
        lines.append(f"主题：{case['subject']}")
    if incoming_zh:
        text = incoming_zh.strip()
        lines += ["", "【来信中文】", text[:1500] + ("\n（后面还有，已截断，完整内容在电脑上看）" if len(text) > 1500 else "")]
    lines += ["", "【局面】", situation]
    for i, o in enumerate(options):
        tag = "（推荐）" if i == recommended else ""
        lines += ["", f"方案 {i + 1}{tag} · {o['title_zh']}", f"怎么回：{o['approach_zh']}", f"风险：{o['risk_zh']}"]
    lines += ["", "点一个方案让我起草。也可以直接回复这条消息，写你想怎么回。"]
    return "\n".join(lines)


def strategy_buttons(case_id: int, count: int) -> list[list[tuple[str, str]]]:
    return [[(f"方案 {i + 1}", f"s:{case_id}:{i}") for i in range(count)], [("不回了", f"x:{case_id}")]]


def draft_text(case: dict, rows: list[dict], ok: bool, notes: str, approval: str, warnings: list[str]) -> str:
    """Every paragraph as it will go out, with its Chinese right under it."""
    lines = [f"✍️ #{case['id']} 回复 {case['who']} 的草稿（原文 + 中文对照）", ""]
    for r in rows:
        lines.append(r["orig"])
        if r.get("failed"):
            lines.append("【这一段没能生成可信的中文对照】")
        elif not r.get("same"):
            lines.append("〔中文〕" + r["zh"])
        lines.append("")
    if notes:
        lines.append(f"这一版：{notes}")
    checks = [w for w in warnings if w] + ([approval] if approval and approval.strip() not in ("", "无") else [])
    if checks:
        lines.append("⚠️ 发送前确认：" + "；".join(checks))
    if not ok:
        lines.append("⚠️ 中文对照不完整，现在不能发送。回复“重新对照”再试，或者让我改写。")
    lines.append("直接回复这条消息可以提问或提修改意见。")
    return "\n".join(lines)


def version(body: str) -> str:
    """Short fingerprint of a draft: send buttons carry it, so a button under an older draft can never send a newer
    text the owner has not read."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:10]


def draft_buttons(case_id: int, ok: bool, ver: str = "") -> list[list[tuple[str, str]]]:
    row = [("看外语原文", f"o:{case_id}")]
    if ok:
        row.append(("发送", f"p:{case_id}:{ver}"))
    return [row, [("换个方案", f"r:{case_id}"), ("不回了", f"x:{case_id}")]]


def confirm_text(case: dict, to: str, subject: str, zh: str) -> str:
    where = f"收件人：{to}\n主题：{subject}" if case["channel"] == "email" else f"发给 Telegram：{to}"

    def gist(text: str) -> str:
        return text.strip()[:600] + ("…" if len(text.strip()) > 600 else "")
    return (f"确认发送 #{case['id']}？发出后无法撤回。\n{where}\n\n要发出的原文：\n{gist(case.get('body') or '')}"
            f"\n\n中文对照：\n{gist(zh)}")


def confirm_buttons(case_id: int, ver: str) -> list[list[tuple[str, str]]]:
    return [[("确认发送", f"c:{case_id}:{ver}"), ("先不发", f"n:{case_id}")]]
