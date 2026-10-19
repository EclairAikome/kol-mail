"""Rules for the work-hours KOC chatbot: who it may answer and when. No network access here; app.py wires it up.

A "KOC" is defined by the conversation, not by follower count (decided 2026-10-16): someone from the KOL sheet who is
not on the priority list and only asks standard affiliate-program questions. The content part is judged by Claude
and then checked again by check_reply below."""

from __future__ import annotations

import email.utils
import random
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import config
from autoreply import TZ, Schedule, email_skip_reason

PRIORITY_STAGES = {"洽谈中", "已谈成", "已婉拒", "退信"}  # declined or bounced people writing again: the owner looks
PRIORITY_INTENTS = {"纯固薪", "CPS+固薪"}
MAX_AGE = timedelta(hours=1)  # older mail (e.g. found after a restart, or a forged old Date) is left to the owner


def is_priority(row: dict) -> bool:
    return row.get("stage") in PRIORITY_STAGES or row.get("intent") in PRIORITY_INTENTS


def ineligible(sender: str, subject: str, received: datetime, now: datetime, schedule: Schedule,
               sheet: dict[str, dict] | None) -> str | None:
    """Why the bot must not take this email, or None if it may. sheet maps address -> {"stage", "intent"};
    None means the sheet could not be read (or is too old), and then the bot stays out.
    Who is checked before when, so a dry run that ignores the clock still sees the priority list."""
    skip = email_skip_reason(sender, subject)
    if skip:
        return skip
    if sheet is None:
        return "总表还没读到，机器人不碰"
    address = email.utils.parseaddr(sender)[1].lower()
    row = sheet.get(address)
    if row is None:
        return "对方不在 KOL 总表里"
    if is_priority(row):
        return f"重点名单（{row.get('stage') or ''}{'，' + row['intent'] if row.get('intent') else ''}）"
    if not schedule.is_work_time(received):
        return "不是工作时间到的信"
    if now - received > MAX_AGE:
        return "来信超过 1 小时，留给你"
    return None


# ---------------------------------------------------------------- what a bot reply may contain
# An allowlist, not a blocklist: a reply may only contain what the knowledge card gives the bot to say. Anything
# else (other numbers, money, contact details, links, claims) sends the mail to the owner instead.

ROUTES = ("answer", "ask_stats", "offer_profit_share", "human")
MAX_REPLY_CHARS = 2000
_K = config.S["koc"]
# The numbers the knowledge card gives the bot (by default 1%/5% commission, 7-day settlement, 20% share, 24 h,
# 50 USD minimum withdrawal). They live in settings.json, so the rules here carry no business terms.
ALLOWED_NUMBERS = {str(n) for n in _K["allowed_numbers"]}
COMMISSION_PERCENTS = {str(n) for n in _K["commission_percents"]}
PROFIT_SHARE = str(_K["profit_share_percent"])
WITHDRAW_MIN = re.escape(str(_K["withdraw_minimum"] or config.DEFAULTS["koc"]["withdraw_minimum"]))
SITE_DOMAIN = (_K["site_domain"] or "").lower()
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]")
LINKISH = re.compile(r"[^\s<>()\[\]{}\"'`]*(?://|\w\.[^\W\d_]{2,})[^\s<>()\[\]{}\"'`]*")
NUMBER = re.compile(r"\d+(?:[.,\s]\d{3})*(?:[.,]\d+)?")
PERCENT_WORD = re.compile(r"%|％|\bper ?cent|\bpercent|\bpor ?ciento|\bpour ?cent|\bper ?cento|\bprozent|\bprocent"
                          r"|\bpersen|\bpor ?cento|パーセント|процент\w*|\byüzde", re.IGNORECASE)
CURRENCY = re.compile(r"[$€£¥₽₹₩₺₫฿₱₦]|\b(?:us\$|usd|usdt|usdc|eur|euros?|dollars?|d[oó]lares|dollari|gbp|pounds?|rub"
                      r"|руб\w*|pesos?|reais|brl|rmb|cny|yuan|yen|baht|lira|tl|zł|pln|czk|kč|huf|jpy|krw"
                      r"|bucks|btc|eth|rupees?|inr)\b|원|円|元|万", re.IGNORECASE)
SPELLED_MONEY = re.compile(r"\b(?:hundred|thousand|million|fifty|twenty|thirty|forty|sixty|seventy|eighty|ninety|ten"
                           r"|cien|mille|cento|tausend|hundert|сто|тысяч\w*)\b", re.IGNORECASE)
CONTACT = re.compile(r"@|\+\s?\d|telegram|whats ?app|wechat|signal|discord|skype|paypal|venmo|cash ?app|\bwise\b|iban"
                     r"|swift|\bbank|crypto|wallet|bitcoin|binance|western union|zelle|t\.me", re.IGNORECASE)
AUTHORIZED = re.compile(r"authori[sz]|autori[sz]|autoriz|official|oficial|officiel|ufficial|offiziell|licen[cs]|lizen"
                        r"|официальн|авторизован|лиценз|resmi|公式|官方|授权|授權|正版", re.IGNORECASE)
WITHDRAW_WORDS = re.compile(r"withdraw|payout|cash ?out|retir|retrait|prelievo|prelevare|auszahl|saque|sacar|вывод|снять",
                            re.IGNORECASE)
SENTENCE = re.compile(r"[^.!?。！？\n]+")


LIST_MARKER = re.compile(r"^\s*\d{1,2}[.)]\s", re.MULTILINE)  # "1. Register…" steps are not numbers the bot states


def _norm_url(u: str) -> str:
    """Compare links without scheme, "www." and trailing punctuation or slash."""
    u = re.sub(r"^https?://", "", u.rstrip(".,;:!?)").lower())
    return re.sub(r"^www\.", "", u).rstrip("/")


def _withdrawal_amount_ok(sentence: str) -> bool:
    """The one amount a bot may state: "50" right next to the only currency word of a sentence about withdrawing."""
    if len(CURRENCY.findall(sentence)) != 1 or SPELLED_MONEY.search(sentence) or not WITHDRAW_WORDS.search(sentence):
        return False
    return bool(re.search(rf"(?:[$€]|us\$|usd)\s*{WITHDRAW_MIN}\b|\b{WITHDRAW_MIN}\s*(?:[$€]|usd|us\$|dollars?|d[oó]lares"
                          r"|dollari)", sentence, re.IGNORECASE))


def check_reply(text: str, route: str, allow_cjk: bool, allowed_urls: frozenset[str]) -> list[str]:
    """Problems that stop a bot reply from going out (it then goes to the owner). Empty list means it may be sent.
    allow_cjk: the creator wrote in Chinese, Japanese or Korean (judged from their mail, not by the model)."""
    problems = []
    if route not in ROUTES or route == "human":
        problems.append(f"路由不对：{route}")
    if len(text) > MAX_REPLY_CHARS:
        problems.append("回复太长")
    allowed = {_norm_url(u) for u in allowed_urls} | ({SITE_DOMAIN} if SITE_DOMAIN else set())
    rest = LIST_MARKER.sub(" ", text)
    for m in LINKISH.finditer(rest):
        token = m.group(0)
        if _norm_url(token) in allowed:
            rest = rest.replace(token, " ")
        else:
            problems.append(f"回复里有知识卡以外的网址：{token[:60]}")
    if CONTACT.search(rest):
        problems.append("回复里出现了联系方式或付款渠道")
    if AUTHORIZED.search(rest):
        problems.append("回复提到了官方或授权")
    if not allow_cjk and CJK.search(rest):
        problems.append("回复里混进了中文，可能泄露内部信息")
    percents = COMMISSION_PERCENTS | ({PROFIT_SHARE} if route == "offer_profit_share" else set())
    for m in PERCENT_WORD.finditer(rest):
        before = re.search(r"(\d+)\s*$", rest[max(0, m.start() - 8):m.start()])
        if not before or before.group(1) not in percents:
            problems.append("回复里出现了不该有的比例")
            break
    for sentence in SENTENCE.findall(rest):
        if (CURRENCY.search(sentence) or SPELLED_MONEY.search(sentence)) and not _withdrawal_amount_ok(sentence):
            problems.append("回复里出现了金额")
            break
    odd = sorted({re.sub(r"\D", "", n) for n in NUMBER.findall(rest)} - ALLOWED_NUMBERS)
    if odd:
        problems.append("回复里出现了知识卡以外的数字：" + "、".join(odd[:5]))
    return problems


# ---------------------------------------------------------------- timing and the waiting queue

DELAY_MIN, DELAY_MAX = 15 * 60, 30 * 60  # seconds after arrival; replying instantly looks like a bot


def due_time(received: datetime, now: datetime, rng=random) -> datetime:
    """A random moment 15 to 30 minutes after arrival, or a few minutes from now if the mail was found late."""
    due = received + timedelta(seconds=rng.uniform(DELAY_MIN, DELAY_MAX))
    return due if due > now else now + timedelta(seconds=rng.uniform(60, 300))


class Queue:
    """KOC mails waiting for their delayed reply, persisted so a restart neither loses nor repeats them.
    Statuses: waiting -> working -> sent / uncertain / human / skipped; superseded when a newer mail from the
    same person arrives first."""

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS koc_jobs (
                message_id TEXT PRIMARY KEY, address TEXT NOT NULL, email_id TEXT NOT NULL,
                received_at TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
                route TEXT, language TEXT, reason TEXT, summary TEXT, reply TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS koc_jobs_address ON koc_jobs (address, status)")
        self.db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(TZ).isoformat(timespec="seconds")

    def add(self, message_id: str, address: str, email_id: str, received: datetime, due: datetime) -> bool:
        address = address.lower()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO koc_jobs (message_id, address, email_id, received_at, due_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (message_id, address, str(email_id), received.astimezone(TZ).isoformat(), due.astimezone(TZ).isoformat(),
             self._now(), self._now()))
        if cur.rowcount == 1:  # one reply per person, to their latest mail
            mine = received.astimezone(TZ)
            others = self.db.execute("SELECT message_id, received_at FROM koc_jobs WHERE address=? AND message_id<>?"
                                     " AND status IN ('waiting','working')", (address, message_id)).fetchall()
            for other_id, other_at in others:
                if datetime.fromisoformat(other_at) <= mine:
                    self.db.execute("UPDATE koc_jobs SET status='superseded', updated_at=? WHERE message_id=? AND "
                                    "status='waiting'", (self._now(), other_id))
                else:  # a newer mail from them is already queued: that reply covers this one
                    self.db.execute("UPDATE koc_jobs SET status='superseded', updated_at=? WHERE message_id=?",
                                    (self._now(), message_id))
        self.db.commit()
        return cur.rowcount == 1

    def due(self, now: datetime) -> list[dict]:
        rows = self.db.execute("SELECT * FROM koc_jobs WHERE status='waiting' ORDER BY due_at").fetchall()
        return [dict(r) for r in rows if datetime.fromisoformat(r["due_at"]) <= now]

    def take(self, message_id: str) -> bool:
        cur = self.db.execute("UPDATE koc_jobs SET status='working', updated_at=? WHERE message_id=? AND status='waiting'",
                              (self._now(), message_id))
        self.db.commit()
        return cur.rowcount == 1

    def finish(self, message_id: str, status: str, **fields) -> None:
        cols = {k: fields.get(k) for k in ("route", "language", "reason", "summary", "reply") if k in fields}
        sets = ", ".join(f"{k}=?" for k in cols)
        self.db.execute(f"UPDATE koc_jobs SET status=?, updated_at=?{', ' + sets if sets else ''} WHERE message_id=?",
                        (status, self._now(), *cols.values(), message_id))
        self.db.commit()

    def statuses(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        rows = self.db.execute(f"SELECT message_id, status FROM koc_jobs WHERE message_id IN ({','.join('?' * len(ids))})",
                               ids).fetchall()
        return {r[0]: r[1] for r in rows}

    def mark(self, message_id: str) -> dict | None:
        """What the page shows next to a mail: status, when it is due and why it went to the owner."""
        row = self.db.execute("SELECT status, due_at, reason FROM koc_jobs WHERE message_id=?", (message_id,)).fetchone()
        return dict(row) if row else None

    def bot_replies(self, address: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM koc_jobs WHERE address=? AND status IN ('sent','uncertain')",
                               (address.lower(),)).fetchone()[0]

    def replies_to(self, address: str) -> list[str]:
        """Texts the bot sent (or may have sent) to this person, to tell its mails apart from the owner's own."""
        rows = self.db.execute("SELECT reply FROM koc_jobs WHERE address=? AND status IN ('sent','uncertain')",
                               (address.lower(),)).fetchall()
        return [r[0] for r in rows if r[0]]

    def offered_profit_share(self, address: str) -> bool:
        return self.db.execute("SELECT 1 FROM koc_jobs WHERE address=? AND status IN ('sent','uncertain') AND "
                               "route='offer_profit_share'", (address.lower(),)).fetchone() is not None

    def sent_since(self, since: datetime) -> int:
        rows = self.db.execute("SELECT updated_at FROM koc_jobs WHERE status IN ('sent','uncertain')").fetchall()
        return sum(1 for r in rows if datetime.fromisoformat(r[0]) >= since)

    def resolve(self, address: str) -> None:
        """The owner answered this person by hand: their hand-overs no longer wait on the owner. Uncertain sends stay as
        they are, because they still count as replies (and as a profit-share offer) for the per-person limits."""
        self.db.execute("UPDATE koc_jobs SET status='handled', updated_at=? WHERE address=? AND status='human'",
                        (self._now(), address.lower()))
        self.db.commit()

    def summary(self, since: datetime) -> dict[str, int]:
        rows = self.db.execute("SELECT status, created_at FROM koc_jobs").fetchall()
        out: dict[str, int] = {}
        for status, created in rows:
            if status in ("waiting", "working") or datetime.fromisoformat(created) >= since:
                out[status] = out.get(status, 0) + 1
        return out

    def interrupted(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM koc_jobs WHERE status='working'").fetchall()]

    def recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT * FROM koc_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
