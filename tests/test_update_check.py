"""Clicking the version number checks for an update at once; opening the page checks once a day."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PAGE = {"x-kol-mail": "1", "origin": "http://127.0.0.1:8765"}


def test_force_reaches_the_updater_and_a_plain_open_does_not(monkeypatch):
    calls = []

    async def check(force=False, install=True):
        calls.append(force)
        return {"current": "0.2.4", "latest": "0.2.4", "state": "current"}
    monkeypatch.setattr(app.UPDATE, "check", check)
    c = TestClient(app.app, base_url="http://127.0.0.1:8765")
    assert c.post("/api/update", json={}, headers=PAGE).json()["state"] == "current"
    assert c.post("/api/update", json={"force": True}, headers=PAGE).json()["state"] == "current"
    assert calls == [False, True]


def test_page_wires_the_version_number_to_a_forced_check():
    page = (app.BASE / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="version" role="button" tabindex="0"' in page
    assert '$("#version").onclick = () => checkUpdate(true);' in page
    assert 'post("/api/update", force ? { force: true } : {})' in page
