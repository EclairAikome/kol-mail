"""The desktop pet's side of the app: what it is told, how it is set up, and keeping its process running.

The pet (pet/main.py) is a separate process with a small always-on-top window. It asks GET /api/pet every two seconds
and gets back the new events since the last one it saw (a new mail, a burst of mails), the unread count, whether the
app is busy drafting or sending, and its own settings. Events are kept for ten minutes, so a pet that starts a little
late still shows the mail that just arrived; older ones are never replayed.

Settings live in data/pet.json and only the app writes them (the page's settings dialog, the pet's own menu and its
new position after a drag all go through POST /api/pet/settings)."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

EVENT_TTL = 600           # seconds an event stays available to a pet that has not fetched it yet
ALIVE_WITHIN = 15         # a pet that asked this recently is running
SCALES = (0.75, 1.0, 1.25)
DEFAULTS = {"enabled": True, "scale": 1.0, "muted": False, "pet": "whale-girl", "x": None, "y": None}
BOOT = uuid.uuid4().hex   # changes on every app start: a pet left over from the previous run sees it and exits
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASS|SESSION|CREDENTIAL|AUTH|COOKIE", re.IGNORECASE)


class Feed:
    def __init__(self, keep: int = 50):
        self.events: collections.deque = collections.deque(maxlen=keep)
        self.seq = 0
        self.last_poll = 0.0
        self.busy: collections.Counter = collections.Counter()

    def push(self, kind: str, **fields) -> int:
        self.seq += 1
        self.events.append({"seq": self.seq, "at": time.time(), "kind": kind, **fields})
        return self.seq

    def since(self, after: int) -> list[dict]:
        now = time.time()
        return [e for e in self.events if e["seq"] > after and now - e["at"] <= EVENT_TTL]

    def polled(self):
        self.last_poll = time.monotonic()

    def alive(self) -> bool:
        return self.last_poll > 0 and time.monotonic() - self.last_poll < ALIVE_WITHIN

    @contextlib.contextmanager
    def working(self, what: str):
        """While inside, the pet shows `what` ("thinking" or "sending")."""
        self.busy[what] += 1
        try:
            yield
        finally:
            self.busy[what] -= 1

    def current_busy(self) -> str | None:
        for what in ("sending", "thinking"):  # sending wins: it is the shorter and the more important one
            if self.busy[what] > 0:
                return what
        return None


# ---------------------------------------------------------------- settings (data/pet.json)

def load_settings(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return clean({**DEFAULTS, **(raw if isinstance(raw, dict) else {})})


def clean(s: dict) -> dict:
    out = dict(DEFAULTS)
    out["enabled"] = s.get("enabled") is not False
    out["muted"] = s.get("muted") is True
    try:
        scale = float(s.get("scale"))
    except (TypeError, ValueError):
        scale = 1.0
    out["scale"] = min(SCALES, key=lambda v: abs(v - scale))
    name = str(s.get("pet") or DEFAULTS["pet"])
    out["pet"] = name if name.replace("-", "").isalnum() else DEFAULTS["pet"]
    for key in ("x", "y"):
        v = s.get(key)
        out[key] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 100000 else None
    return out


def save_settings(path: Path, changes: dict) -> dict:
    """Merge the allowed keys of `changes` into data/pet.json (atomic) and return the cleaned result."""
    current = load_settings(path)
    merged = clean({**current, **{k: v for k, v in changes.items() if k in DEFAULTS}})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return merged


# ---------------------------------------------------------------- the pet process

class Keeper:
    """Starts pet/main.py and starts it again if it dies, while the pet is enabled. Exit code 0 means the pet chose
    to leave (switched off, or it belonged to an older run of the app): it is only started again when wanted.
    Exit code 3 means another pet already runs; check again a little later. Exit code 4 means this computer cannot
    show a pet: nothing is tried again until the settings change."""

    ANOTHER, UNSUPPORTED = 3, 4

    def __init__(self, base: Path, data: Path, port: int, settings_file: Path, log):
        self.base, self.data, self.port, self.settings_file, self.log = base, data, port, settings_file, log
        self.proc: asyncio.subprocess.Process | None = None
        self.wake = asyncio.Event()
        self.error: str | None = None

    def command(self) -> list[str]:
        exe = Path(sys.executable)
        if os.name == "nt" and (exe.parent / "pythonw.exe").exists():
            exe = exe.parent / "pythonw.exe"  # a GUI process: never a console window, whatever starts it
        return [str(exe), "-I", "-X", "utf8", str(self.base / "pet" / "main.py"), "--port", str(self.port),
                "--data", str(self.data), "--boot", BOOT]

    async def run(self):
        quick_failures = 0
        while True:
            self.wake.clear()
            if not load_settings(self.settings_file).get("enabled"):
                await self.wake.wait()
                continue
            started = time.monotonic()
            try:
                code = await self._run_once()
            except OSError as e:
                self.error = f"桌宠启动失败：{e}"
                self.log("pet failed to start", e)
                code = -1
            if code == 0:
                self.error = None
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), 30)
                continue
            if code == self.ANOTHER:
                await asyncio.sleep(10)
                continue
            if code == self.UNSUPPORTED:
                self.error = "这台电脑显示不了桌宠（原因在 data/pet.log），来信改用系统通知"
                await self.wake.wait()
                continue
            quick_failures = quick_failures + 1 if time.monotonic() - started < 60 else 0
            if code != -1:
                self.error = f"桌宠意外退出（退出码 {code}），详见 data/pet.log"
                self.log("pet exited", code)
            delay = 5 if quick_failures < 3 else 300  # a pet that keeps crashing is retried rarely
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), delay)

    async def _run_once(self) -> int:
        log_path = self.data / "pet.log"
        with contextlib.suppress(OSError):
            if log_path.stat().st_size > 1_000_000:
                os.replace(log_path, log_path.with_suffix(".log.1"))
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        # The pet only talks to 127.0.0.1: it gets the environment without anything that looks like a secret.
        env = {k: v for k, v in os.environ.items() if not SECRET_NAME.search(k)}
        with open(log_path, "ab") as out:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command(), cwd=str(self.base), stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out,
                env=env, **extra)
        try:
            return await self.proc.wait()
        finally:
            self.proc = None

    def changed(self):
        """Settings changed: start a pet that is now wanted. A pet that is no longer wanted reads that itself."""
        self.wake.set()

    def stop(self):
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
