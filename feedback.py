"""Anonymous feedback from the page's corner button to the maintainer's POPO group, through a group robot webhook.

The webhook address is a secret handed out with the installer (settings.json feedback.popo_webhook), never stored
in git. Nothing identifying goes along: no name, no address, no machine name, only the text and the app version."""

from __future__ import annotations

import json
import time

import httpx

MAX_CHARS = 2000
PER_HOUR = 5


class FeedbackError(Exception):
    pass


class Limiter:
    def __init__(self, per_hour: int = PER_HOUR):
        self.per_hour = per_hour
        self.sent: list[float] = []

    def allowed(self) -> bool:
        now = time.time()
        self.sent = [t for t in self.sent if now - t < 3600]
        return len(self.sent) < self.per_hour

    def record(self):
        self.sent.append(time.time())


def message(text: str, version: str, kind: str) -> str:
    return f"【KOL Mail 匿名反馈 · {kind}】\n{text.strip()}\n\n(版本 {version})"


async def send(webhook: str, text: str, body_key: str = "message", extra: dict | None = None) -> None:
    """POST {body_key: text, **extra} as JSON. POPO answers HTTP 200 with a JSON errcode/code field; anything else
    (or a non-zero code) is an error the page shows."""
    if not webhook.startswith("https://"):
        raise FeedbackError("反馈地址没有配置好，请联系维护者")
    payload = {body_key: text, **(extra or {})}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(webhook, json=payload)
    except httpx.HTTPError as e:
        raise FeedbackError(f"发不出去：{type(e).__name__}") from e
    if r.status_code != 200:
        raise FeedbackError(f"POPO 返回 HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        return
    code = data.get("errcode", data.get("code", 0)) if isinstance(data, dict) else 0
    if code not in (0, "0", 200, "200", None):
        raise FeedbackError(f"POPO 拒收：{json.dumps(data, ensure_ascii=False)[:150]}")
