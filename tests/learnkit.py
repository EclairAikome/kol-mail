"""Shared pieces of the playbook-learning tests: a made-up playbook (never the company's), and app.py pointed at a
fresh learning store, playbook, history, rule overrides and SOP folder under tmp_path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import compliance  # noqa: E402
import episodes  # noqa: E402

PLAYBOOK = """# KOL 谈判手册（测试用）

这是测试用的假手册，内容是编的。

## 我的谈判方式

随机应变，每个人都不一样。我的修改意见优先。

## 零、两条红线

1. 不提前打款，也不付定金，发布后才付。
2. 不走银行转账。

## 一、合作模式的优先顺序

1. 先推纯 CPS，新用户订单 GMV 的 5%，老用户 1%。
2. 对方拒绝纯 CPS，先提两周试合作。

## 二、利润分成方案

纯 CPS 开口先报 5% 和 1% 的默认档。对方嫌低再升级成纯利分成，起步报 20%，最高到 50%。

## 十一、沟通风格

- 先回应对方说的内容，再说我们的。
- 用对方的语言回复。

## 十二、先读人，再定谈法

- 对方开口问价，这一封就给数字。

## 十三、案例库（测试）

| 达人 | 平台 | 成交 |
|---|---|---|
| Alpha | YouTube | 每条 100 美元 |
| Beta | Twitch | 每条 80 美元 |
| Gamma | TikTok | 纯 CPS |
"""


def setup(tmp_path: Path, monkeypatch, playbook: str = PLAYBOOK) -> Path:
    """app.py with its own learning store and playbook; returns the playbook path."""
    book = tmp_path / "谈判手册.md"
    book.write_text(playbook, encoding="utf-8")
    monkeypatch.setattr(app, "PLAYBOOK", book)
    monkeypatch.setattr(app, "LEARN", episodes.Store(tmp_path / "learning.sqlite3"))
    monkeypatch.setattr(app, "PLAYBOOK_HISTORY", tmp_path / "playbook_history")
    monkeypatch.setattr(app, "RULES_OVERRIDES", tmp_path / "rules_overrides.yaml")
    monkeypatch.setattr(compliance, "OVERRIDES_FILE", tmp_path / "rules_overrides.yaml")
    monkeypatch.setattr(app, "SOP_SNAPSHOTS", tmp_path / "sop")
    monkeypatch.setattr(app, "CASE_ARCHIVE", tmp_path / "案例库_归档.md")
    monkeypatch.setattr(app, "owner_chat", lambda: 0)  # no phone: no pushes
    monkeypatch.setitem(app.learn_state, "running", "")
    monkeypatch.setitem(app.learn_state, "last_activity", 0.0)
    compliance._cache.update(mtime=None, path=None, rules=None)
    app.sync_manual_edits()  # the first look remembers this version
    return book


class Req:
    def __init__(self, body=None, method="POST"):
        self.body, self.method = body or {}, method

    async def json(self):
        return self.body


def episode(**over) -> dict:
    """A sent reply the owner corrected: an AI draft, the text that went out, the owner's comment."""
    base = {"channel": "email", "address": "kol@example.org", "ai_body":
            "Hi Kai,\n\nWe can offer 20% of net profit right away.\n\nThanks for your patience!\n\nMe",
            "sent_body": "Hi Kai,\n\nWe can offer 5% on new users and 1% on returning ones.\n\nMe",
            "feedback_log": ["第一封别直接给分成，先报默认档"], "notes": "", "compliance": {"checked": True},
            "kol_text": "Hello, what commission do you pay for each order of my viewers?"}
    return {**base, **over}
