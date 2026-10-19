"""Auto-reply rules for KOL Mail: working hours, off-hours periods, which messages to skip, reply-once bookkeeping
and template choice. No network access here; app.py wires these rules to email and Telegram."""

from __future__ import annotations

import email.utils
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import config  # noqa: E402

TZ = timezone(timedelta(hours=float(config.S["utc_offset_hours"])))  # a fixed offset: GMT+8 has no daylight saving
SELF_ADDRESS = (config.S["mail"]["address"] or "").lower()
# Colleagues get no automatic replies; without a setting, the domain of our own address is the company's.
OWN_DOMAIN = (config.S["mail"]["company_domain"] or ("@" + SELF_ADDRESS.split("@", 1)[1] if "@" in SELF_ADDRESS else "")).lower()
MACHINE_SENDER = re.compile(
    r"(^|[._+-])(no[-_]?reply|do[-_]?not[-_]?reply|notifications?|mailer-daemon|postmaster|bounces?)([._+-]|@)"
    r"|@amazonses\.com$|mailer-daemon")
AUTO_REPLY_SUBJECT = re.compile(
    r"^\s*((re|aw|sv|fw|fwd|r|rv|tr|wg)\s*:\s*)*"  # a reply prefix in front of an auto-reply is still an auto-reply
    r"(automatic reply|auto[- ]?reply|autoreply|out of (the )?office|undelivered mail|delivery status notification"
    r"|respuesta autom[aá]tica|r[ée]ponse automatique|risposta automatica|resposta autom[aá]tica"
    r"|abwesenheit|automatische antwort|автоматический ответ|автоответ|自动回复|自動回覆|自動応答)",
    re.IGNORECASE)


def email_skip_reason(sender: str, subject: str) -> str | None:
    """Why this email must not get an automatic reply, or None if it may."""
    address = email.utils.parseaddr(sender or "")[1].lower()
    if not address:
        return "没有发件地址"
    if address == SELF_ADDRESS:
        return "自己发的"
    if OWN_DOMAIN and address.endswith(OWN_DOMAIN):
        return "公司同事"
    if MACHINE_SENDER.search(address):
        return "系统或退信地址"
    if AUTO_REPLY_SUBJECT.search(subject or ""):
        return "对方的自动回复或退信"
    return None


@dataclass(frozen=True)
class Schedule:
    weekdays: frozenset[int] = frozenset(range(5))  # Monday=0 .. Friday=4
    start: time = time(10, 0)
    end: time = time(18, 0)
    holidays: frozenset[date] = field(default_factory=frozenset)

    def is_workday(self, day: date) -> bool:
        return day.weekday() in self.weekdays and day not in self.holidays

    def is_work_time(self, moment: datetime) -> bool:
        local = moment.astimezone(TZ)
        return self.is_workday(local.date()) and self.start <= local.time() < self.end

    def off_period_start(self, moment: datetime) -> datetime | None:
        """When the current off-work stretch began (the last closing time), or None while working.
        Everything between two working sessions, weekends and holidays included, is one period."""
        if self.is_work_time(moment):
            return None
        local = moment.astimezone(TZ)
        for back in range(0, 60):
            day = local.date() - timedelta(days=back)
            close = datetime.combine(day, self.end, TZ)
            if self.is_workday(day) and close <= local:
                return close
        return datetime.combine(local.date() - timedelta(days=60), self.end, TZ)  # absurdly long holiday

    def reply_period(self, received_at: datetime, now: datetime) -> datetime | None:
        """The off period to file an automatic reply under, or None when this message should not get one:
        the owner is working now, or the message arrived during work hours or in an earlier off period."""
        period = self.off_period_start(now)
        if period is None or received_at < period:
            return None
        return period


class ReplyLog:
    """One row per (channel, person, off period). A row is claimed *before* sending, so a crash mid-send can
    never lead to a second automatic message; the outcome is filled in afterwards."""

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS replies (
                channel TEXT NOT NULL, peer TEXT NOT NULL, period TEXT NOT NULL,
                ref TEXT, status TEXT NOT NULL DEFAULT 'claimed', detail TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (channel, peer, period))""")
        self.db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(TZ).isoformat(timespec="seconds")

    def claim(self, channel: str, peer: str, period: datetime, ref: str) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO replies (channel, peer, period, ref, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (channel, peer.lower(), period.isoformat(), ref, self._now(), self._now()))
        self.db.commit()
        return cur.rowcount == 1

    def handled(self, channel: str, peer: str, period: datetime) -> bool:
        row = self.db.execute("SELECT 1 FROM replies WHERE channel=? AND peer=? AND period=?",
                              (channel, peer.lower(), period.isoformat())).fetchone()
        return row is not None

    def count(self, channel: str, period: datetime) -> int:
        """Automatic messages already claimed in this off period, for the per-period cap."""
        return self.db.execute("SELECT COUNT(*) FROM replies WHERE channel=? AND period=?",
                               (channel, period.isoformat())).fetchone()[0]

    def replied_recently(self, channel: str, peer: str, hours: float) -> bool:
        """Guards against a second message when the period key shifts, e.g. after editing the holiday list."""
        since = (datetime.now(TZ) - timedelta(hours=hours)).isoformat(timespec="seconds")
        row = self.db.execute("SELECT 1 FROM replies WHERE channel=? AND peer=? AND created_at>=?",
                              (channel, peer.lower(), since)).fetchone()
        return row is not None

    def settle(self, channel: str, peer: str, period: datetime, status: str, detail: str = "") -> None:
        self.db.execute("UPDATE replies SET status=?, detail=?, updated_at=? WHERE channel=? AND peer=? AND period=?",
                        (status, detail, self._now(), channel, peer.lower(), period.isoformat()))
        self.db.commit()

    def release(self, channel: str, peer: str, period: datetime) -> None:
        """Undo a claim when the message certainly did not go out, so a later attempt may send it."""
        self.db.execute("DELETE FROM replies WHERE channel=? AND peer=? AND period=? AND status='claimed'",
                        (channel, peer.lower(), period.isoformat()))
        self.db.commit()

    def unfinished(self) -> list[dict]:
        """Claims whose send outcome was never recorded (the process died mid-send)."""
        rows = self.db.execute("SELECT * FROM replies WHERE status='claimed' ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT * FROM replies ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


MODES = ("off", "template", "ai")  # what happens to KOL messages that arrive off hours


@dataclass(frozen=True)
class Config:
    schedule: Schedule = field(default_factory=Schedule)
    email_enabled: bool = False
    telegram_enabled: bool = False
    koc_enabled: bool = False  # work-hours KOC chatbot (koc.py)
    # off (default): nothing is sent off hours; template: send the fixed text; ai: draft with AI, owner approves on
    # the phone. The channel switches above still decide which channels "template" and "ai" act on.
    offhours_mode: str = "off"
    templates: dict = field(default_factory=dict)  # {"email": {"en": "...", "es": "..."}, "telegram": {...}}

    def reply_text(self, channel: str, language: str) -> str:
        texts = self.templates.get(channel, {})
        code = (language or "").strip().lower().split("-")[0].split("_")[0]
        return texts.get(code) or texts["en"]


def load_config(path: Path) -> Config:
    """autoreply.json; a missing file means every channel stays off. Anything malformed raises ValueError."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    if not isinstance(raw, dict):
        raise ValueError("autoreply.json 顶层必须是一个对象")
    templates = raw.get("templates") or {}
    if not isinstance(templates, dict) or not all(
            isinstance(texts, dict) and all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                            for k, v in texts.items())
            for texts in templates.values()):
        raise ValueError("autoreply.json 的 templates 格式不对，应为 {渠道: {语言代码: 文案}}")
    holidays = raw.get("holidays") or []
    if not isinstance(holidays, list) or not all(isinstance(d, str) for d in holidays):
        raise ValueError('autoreply.json 的 holidays 应为日期字符串列表，例如 ["2026-10-19"]')
    mode = raw.get("offhours_mode", "off")
    if mode not in MODES:
        raise ValueError('autoreply.json 的 offhours_mode 只能是 "off"、"template" 或 "ai"')
    for channel in ("email", "telegram"):
        if mode == "template" and raw.get(f"{channel}_enabled") and not (templates.get(channel) or {}).get("en"):
            raise ValueError(f"autoreply.json: {channel} 已开启，但缺少英文文案 templates.{channel}.en")
    return Config(
        schedule=Schedule(holidays=frozenset(date.fromisoformat(d) for d in holidays)),
        email_enabled=raw.get("email_enabled") is True,
        telegram_enabled=raw.get("telegram_enabled") is True,
        koc_enabled=raw.get("koc_enabled") is True,
        offhours_mode=mode,
        templates=templates,
    )


SWITCHES = ("email_enabled", "telegram_enabled", "koc_enabled")


def change_holidays(path: Path, days: list[date], add: bool) -> list[date]:
    """Add or remove holiday dates in autoreply.json (atomic, everything else kept). Returns the dates that changed."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    current = {date.fromisoformat(d) for d in raw.get("holidays") or []}
    changed = [d for d in days if (d not in current) == add]
    current = current | set(days) if add else current - set(days)
    raw["holidays"] = sorted(d.isoformat() for d in current)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return changed


def set_switch(path: Path, key: str, value) -> None:
    """Flip one switch in autoreply.json, keeping everything else as it is (written atomically)."""
    if key == "offhours_mode":
        if value not in MODES:
            raise ValueError(f"offhours_mode 不能是 {value}")
    elif key not in SWITCHES:
        raise ValueError(f"不能从页面修改 {key}")
    else:
        value = bool(value)
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[key] = value
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
