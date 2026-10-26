"""The off-hours switch as the app uses it: "off" (the default) never answers anybody, and the settings dialog
changes it only when its box was changed."""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import autoreply  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

KOL_MAIL = {"email_id": "7", "message_id": "<m1@x.com>", "sender": "Ana <ana@gmail.com>",
            "subject": "Collab", "date": "2026-10-16T12:00:00Z"}


class OffHours(autoreply.Schedule):
    """Always off work, so the test does not depend on the time it runs."""

    def off_period_start(self, moment):
        return datetime(2026, 10, 16, 18, tzinfo=autoreply.TZ)

    def reply_period(self, received_at, now):
        return datetime(2026, 10, 16, 18, tzinfo=autoreply.TZ)


def run_offhours(monkeypatch, mode: str) -> list[str]:
    cfg = autoreply.Config(schedule=OffHours(), email_enabled=True, telegram_enabled=True, offhours_mode=mode,
                           templates={"email": {"en": "Off now."}})
    calls = []

    async def reply(*a, **k):
        calls.append("template")

    monkeypatch.setattr(app, "load_autoreply_config", lambda: cfg)
    monkeypatch.setattr(app, "offhours_email_reply", reply)
    monkeypatch.setattr(app, "open_email_case", lambda *a, **k: calls.append("case"))
    monkeypatch.setattr(app, "already_answered", lambda *a: False)
    asyncio.run(app.offhours_email([KOL_MAIL]))
    return calls


@pytest.mark.parametrize("mode, expected", [("off", []), ("template", ["template"]), ("ai", ["case"])])
def test_off_hours_email_follows_the_mode(monkeypatch, mode, expected):
    assert run_offhours(monkeypatch, mode) == expected


def test_off_hours_telegram_sends_nothing_when_off(monkeypatch):
    cfg = autoreply.Config(schedule=OffHours(), telegram_enabled=True, offhours_mode="off")
    monkeypatch.setattr(app, "load_autoreply_config", lambda: cfg)

    async def must_not_read(*a, **k):
        raise AssertionError("an off switch must not even read the chat")

    monkeypatch.setattr(app.tg, "call", must_not_read)
    asyncio.run(app.offhours_telegram({"chat_id": 12345, "first_message_id": 1}, "Ana"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "autoreply.json"
    path.write_text((app.BASE / "autoreply.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(app, "AUTOREPLY_CONFIG", path)
    monkeypatch.setattr(app, "PLAYBOOK", tmp_path / "playbook.md")
    return TestClient(app.starlette_app), path


def mode_in(path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["offhours_mode"]


def test_settings_show_and_change_the_template_switch(client):
    c, path = client
    assert c.get("/api/setup").json()["offhours_mode"] == "off"
    assert c.post("/api/setup", json={"signature": "Me", "offhours_template": True}).json() == {"ok": True}
    assert mode_in(path) == "template"
    assert c.post("/api/setup", json={"signature": "Me", "offhours_template": False}).status_code == 200
    assert mode_in(path) == "off"


def test_saving_settings_without_touching_the_box_keeps_the_phone_mode(client):
    c, path = client
    autoreply.set_switch(path, "offhours_mode", "ai")
    assert c.post("/api/setup", json={"signature": "Me", "preferences": "Push CPS first."}).status_code == 200
    assert mode_in(path) == "ai"
