"""The corner feedback button as the page uses it: /api/feedback posts the text to the POPO robot webhook."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import feedback  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

HOOK = "https://open.popo.example/robot/webhook/send?key=test"
PAGE = {"x-kol-mail": "1", "origin": "http://127.0.0.1:8765"}


@pytest.fixture
def robot(monkeypatch):
    """Stand-in for the POPO robot: records what was posted and answers like POPO does."""
    seen = {"answer": (200, {"errcode": 0, "errmsg": "ok"}), "posts": []}

    def handle(request: httpx.Request):
        seen["posts"].append((str(request.url), json.loads(request.content)))
        status, body = seen["answer"]
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(feedback.httpx, "AsyncClient",
                        lambda **k: real(transport=httpx.MockTransport(handle), **k))
    monkeypatch.setitem(app.CFG, "feedback", {"popo_webhook": HOOK, "body_key": "message"})
    monkeypatch.setattr(app, "feedback_limit", feedback.Limiter())
    return seen


def client():
    return TestClient(app.app, base_url="http://127.0.0.1:8765")


def test_feedback_reaches_the_robot(robot):
    r = client().post("/api/feedback", json={"text": "按钮点了没反应", "kind": "bug"}, headers=PAGE)
    assert r.status_code == 200, r.text
    url, body = robot["posts"][0]
    assert url == HOOK
    assert set(body) == {"message"}
    # the robot's security setting is the custom keyword "KOL Mail": a text without it is refused
    assert "KOL Mail" in body["message"] and "按钮点了没反应" in body["message"]


def test_status_tells_the_page_the_button_works(robot):
    r = client().get("/api/status", headers=PAGE)
    assert r.status_code == 200, r.text
    assert r.json()["feedback_ready"] is True


def test_popo_refusal_is_shown(robot):
    robot["answer"] = (200, {"errcode": 1, "errmsg": "keywords not in content"})
    r = client().post("/api/feedback", json={"text": "hi", "kind": "idea"}, headers=PAGE)
    assert r.status_code == 502
    assert "POPO 拒收" in r.json()["error"]
