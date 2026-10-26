"""Talks to the app over its local HTTP API from a background thread, so the window never waits on the network.

Every request carries the headers the app's guard asks of a local caller (X-KOL-Mail, and Origin on POST). No proxy
is ever used: a company proxy set for the whole system would otherwise get these 127.0.0.1 requests and refuse them."""

from __future__ import annotations

import json
import queue
import threading
import traceback
import urllib.parse
import urllib.request

POLL_EVERY = 2.0
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def own_page(url: str, port: int) -> bool:
    """True only for the app's own page on this computer (http://127.0.0.1:<port>/ and a #fragment)."""
    try:
        u = urllib.parse.urlsplit(url)
        return (u.scheme == "http" and u.hostname == "127.0.0.1" and u.port == port and u.username is None
                and u.password is None and u.path == "/" and not u.query)
    except ValueError:
        return False


class Client(threading.Thread):
    def __init__(self, port: int, wake=lambda: None):
        super().__init__(daemon=True, name="pet-client")
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.wake = wake               # called after each answer, from this thread (the window posts itself a message)
        self.answers: queue.Queue = queue.Queue()  # dicts from /api/pet, or None when the app did not answer
        self.outbox: queue.Queue = queue.Queue()   # settings changes to send
        self.after = 0
        self.stopped = threading.Event()

    def _request(self, path: str, body: dict | None = None) -> dict:
        headers = {"X-KOL-Mail": "1"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Origin": self.base})
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST" if data else "GET")
        with OPENER.open(req, timeout=10) as r:
            answer = json.loads(r.read().decode("utf-8"))
        if not isinstance(answer, dict):
            raise ValueError("not an answer from KOL Mail")
        return answer

    def run(self):
        while not self.stopped.is_set():
            while not self.outbox.empty():
                try:
                    self._request("/api/pet/settings", self.outbox.get_nowait())
                except Exception:  # noqa: BLE001  a lost setting is retried by the user; the loop must go on
                    pass
            try:
                answer = self._request(f"/api/pet?after={self.after}")
                self.after = max(self.after, int(answer.get("seq") or 0))
            except Exception:  # noqa: BLE001  any failure (cut-off reply, odd JSON, refused) counts as no answer
                answer = None
            try:
                self.answers.put(answer)
                self.wake()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            self.stopped.wait(POLL_EVERY)

    def save(self, **changes):
        self.outbox.put(changes)
