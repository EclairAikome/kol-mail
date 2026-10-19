"""Per-user settings. Everything that identifies a person, a mailbox or a machine lives in data/settings.json (never
in the code, never in git). The installer writes it; missing keys fall back to DEFAULTS. Edits apply after a restart.

Environment variables still override a few values for one-off runs (KOL_MAIL_PORT, KOL_LLM_BACKEND, ...)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("KOL_MAIL_DATA", BASE / "data"))
SETTINGS_FILE = Path(os.environ.get("KOL_MAIL_SETTINGS", DATA / "settings.json"))
VERSION_FILE = BASE / "VERSION"

DEFAULTS = {
    "port": 8765,
    "utc_offset_hours": 8,
    "mail": {
        "account_name": "",          # the account name inside mcp-email-server
        "address": "",               # your own address, e.g. you@example.com
        "company_domain": "",        # e.g. @company.com: colleagues never get automatic replies
        "mcp_exe": "",               # mcp-email-server.exe
    },
    "identity": {
        "name": "",                  # how you sign, e.g. Alex
        "title": "Marketing Manager",
        "company": "",
        "signature": "",             # exact signature block appended to every reply
    },
    "paths": {
        "playbook": "",              # negotiation playbook (Markdown) the drafts follow
        "koc_knowledge": "",         # the only facts the KOC bot may use
        "sheet_tool": "",            # kol-sheet\kol.py (optional)
        "sheet_python": "",          # a Python with pywin32 for the sheet tool
        "sheet_workbook": "",        # KOL总表.xlsx; empty = next to the sheet tool (the original single-user setup)
        "autoreply_record": "",      # Markdown log of automatic replies (optional)
        "telegram_mcp": "",          # chigwell/telegram-mcp checkout (optional)
    },
    "llm": {
        "backend": "claude",         # claude | codemaker
        "models": {},                # {"translate": "...", "draft": "..."}; empty = the backend's defaults
        "claude_bin": "",
        "codemaker_bin": "",
    },
    "koc": {
        "allowed_numbers": ["1", "5", "7", "20", "24", "50"],
        "commission_percents": ["5", "1"],
        "profit_share_percent": "20",
        "withdraw_minimum": "50",
        "site_domain": "",           # e.g. example.com: always allowed as a link
        "fallback_subject": "Collaboration",
    },
    "mobile": {
        "telegram_bot_token": "",    # from @BotFather; the bot only ever talks to owner_chat_id
        "owner_chat_id": 0,
    },
    "feedback": {
        "popo_webhook": "",          # anonymous feedback goes here (set by the installer, never in git)
        "body_key": "message",       # JSON field the robot reads the text from
    },
    "update": {
        "repo": "",                  # owner/name on GitHub
        "enabled": True,
    },
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{SETTINGS_FILE} 顶层必须是一个对象")
    return _merge(DEFAULTS, raw)


def save(settings: dict) -> None:
    """Atomic write; only used for values the app sets itself (e.g. the Telegram owner after pairing)."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def update(path: list[str], value) -> dict:
    """Set one nested key in settings.json, keeping everything else as the user wrote it."""
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raw = {}
    node = raw
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    save(raw)
    return _merge(DEFAULTS, raw)


def version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


S = load()
