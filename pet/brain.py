"""What the pet does, decided from what the app reports and what the mouse does. No drawing here: the window code
asks state() and bubble() on every frame and calls the on_* methods.

Order of precedence: being dragged > a one-off move (the mail hop, a reaction to a touch) > something broken > busy
(sending, drafting) > a new mail not looked at yet > unread mail > asleep (off hours, or nothing touched for a while)
> idle.

A click on the little letter she holds (or on a new-mail bubble) opens KOL Mail; a click anywhere else on her is a
touch, and she reacts to it depending on where (REACTIONS), with a line in a small speech bubble. A pet without a
part map has no letter to aim at: a click anywhere opens KOL Mail, as before.

An outfit with a move of its own ("show": throwing a snowball, a dance) plays it when her body is touched, and now
and then by herself while nothing else is going on (SHOW_EVERY). Choosing another outfit makes the pet leave: the app
starts it again in the new one."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

BUBBLE_FOR = 15.0        # seconds a new-mail bubble stays up unless clicked
SAY_FOR = 2.4            # seconds a line said after a touch stays up
NOTIFY_FOR = 120.0       # seconds the pet keeps holding up the envelope after new mail
DOZE_AFTER = 20 * 60.0   # seconds without anything happening before it falls asleep
FIRST_POLL_MAX_AGE = 60  # on start, mail older than this is not announced again (a restarted pet saw it already)
OFFLINE_EXIT = 90.0      # seconds without an answer from the app before the pet gives up

# body part -> (the state she plays, what she may say)
REACTIONS = {
    "head": ("pat", ["嘿嘿，再摸摸～", "头发要被揉乱啦", "被摸头了，开心！"]),
    "face": ("poke", ["唔……别戳脸啦", "脸要被戳扁了！", "干嘛呀～"]),
    "body": ("giggle", ["哈哈好痒！", "不要挠啦～", "咯咯咯……"]),
    "tail": ("spout", ["噗——尾巴不可以随便摸！", "喷你一脸水花！", "呜哇，尾巴好敏感"]),
    "feet": ("hop", ["嘿咻！", "跳一下～", "脚丫子痒痒"]),
}
SHOW = ("show", ["看我的！", "给你表演一个～", "看好啦！"])  # the body, in an outfit with a move of its own
ONE_OFF = {state for state, _ in REACTIONS.values()} | {SHOW[0], "mail", "hop"}  # play once and end
SHOW_EVERY = (180.0, 480.0)  # seconds between two moves she shows by herself, picked at random in between
CALM_FIRST = 60.0            # seconds she just stands there after anything else before she shows off by herself
CALM = ("idle", "waiting", "show")


@dataclass
class Bubble:
    title: str
    line1: str
    line2: str
    link: str
    more: int = 0        # further mails that arrived while this bubble was up
    until: float = 0.0
    say: bool = False    # a line after a touch, not a mail


class Brain:
    def __init__(self, loop_ms: Callable[[str], float], boot: str, now: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, pick: Callable[[list], str] = random.choice,
                 outfit: str = "", shows: bool = False, wait: Callable[[float, float], float] = random.uniform):
        """outfit: the one the pet was started in (settings' "pet"); shows: it has a move of its own."""
        self.loop_ms, self.boot, self.now, self.wall, self.pick = loop_ms, boot, now, wall, pick
        self.outfit, self.shows, self.wait = outfit, shows, wait
        self.next_show = now() + wait(*SHOW_EVERY)
        self.unread = 0
        self.busy: str | None = None
        self.error: str | None = None
        self.off_hours = False
        self.settings: dict = {}
        self.url = ""
        self.first_poll = True
        self.last_answer = now()
        self.last_activity = now()
        self.oneshot: tuple[str, float] | None = None
        self.notify_until = 0.0
        self.bubble_: Bubble | None = None
        self.said: Bubble | None = None
        self.drag_dir: str | None = None
        self.quit_reason: str | None = None

    # ---------------------------------------------------------------- from the app

    def on_poll(self, data: dict):
        t = self.now()
        self.last_answer = t
        if data.get("boot") and self.boot and data["boot"] != self.boot:
            self.quit_reason = "the app restarted: its own pet takes over"
            return
        self.settings = data.get("settings") or {}
        if self.settings.get("enabled") is False:
            self.quit_reason = "switched off"
            return
        if self.outfit and self.settings.get("pet", self.outfit) != self.outfit:
            self.quit_reason = "another outfit was chosen: the app starts her again in it"
            return
        self.url = data.get("url") or self.url
        self.unread = int(data.get("unread") or 0)
        self.busy = data.get("busy")
        self.error = data.get("error")
        self.off_hours = bool(data.get("off_hours"))
        events = data.get("events") or []
        if self.first_poll:
            events = [e for e in events if self.wall() - float(e.get("at", 0)) <= FIRST_POLL_MAX_AGE]
            self.first_poll = False
        for e in events:
            if e.get("kind") == "mail":
                self.on_mail(e)

    def on_offline(self):
        if self.now() - self.last_answer > OFFLINE_EXIT:
            self.quit_reason = "the app stopped answering"

    def on_mail(self, e: dict):
        t = self.now()
        self.last_activity = t
        self.oneshot = ("mail", t + self.loop_ms("mail") / 1000)
        self.notify_until = t + NOTIFY_FOR
        self.said = None  # a mail beats small talk
        if self.settings.get("muted"):
            return
        b = self.bubble_
        if b and b.until > t:
            b.more += int(e.get("count") or 1)
            b.title, b.line1, b.line2, b.link = e.get("title", ""), e.get("line1", ""), e.get("line2", ""), e.get("link", "")
            b.until = t + BUBBLE_FOR
        else:
            self.bubble_ = Bubble(e.get("title", ""), e.get("line1", ""), e.get("line2", ""), e.get("link") or self.url,
                                  until=t + BUBBLE_FOR)

    # ---------------------------------------------------------------- from the mouse

    def open_link(self) -> str:
        """The page a click on the letter opens: the mail in the bubble if one is up, otherwise KOL Mail itself."""
        t = self.now()
        self.last_activity = t
        self.notify_until = 0.0
        link = self.bubble_.link if self.bubble_ and self.bubble_.until > t else ""
        self.bubble_ = None
        return link or self.url

    def on_click(self) -> str:
        """A pet without a part map: any click opens KOL Mail (and she hops)."""
        link = self.open_link()
        self.oneshot = ("hop", self.now() + self.loop_ms("hop") / 1000)
        return link

    def on_touch(self, part: str) -> str | None:
        """A click on her. The letter opens KOL Mail (returned); any other part makes her react and say something."""
        if part == "mail":
            return self.open_link()
        reaction = SHOW if part == "body" and self.shows else REACTIONS.get(part)
        if not reaction:
            return None
        t = self.now()
        self.last_activity = t
        state, lines = reaction
        self.oneshot = (state, t + self.loop_ms(state) / 1000)
        if state == "show":  # she just showed it: the next one by herself is a while away again
            self.next_show = self.oneshot[1] + self.wait(*SHOW_EVERY)
        if not (self.bubble_ and self.bubble_.until > t) and not self.settings.get("muted"):
            self.said = Bubble(self.pick(lines), "", "", "", until=t + SAY_FOR, say=True)
        return None

    def on_bubble_click(self) -> str | None:
        """A mail bubble opens its mail; a speech bubble just goes away."""
        if self.bubble_ and self.bubble_.until > self.now():
            link = self.bubble_.link or self.url
            self.bubble_ = None
            self.notify_until = 0.0
            self.last_activity = self.now()
            return link
        self.said = None
        return None

    def dismiss_bubble(self):
        self.bubble_ = None
        self.said = None

    def on_drag(self, dx: int):
        self.last_activity = self.now()
        if dx < 0:
            self.drag_dir = "drag-left"
        elif dx > 0 or self.drag_dir is None:
            self.drag_dir = "drag-right"

    def on_drop(self):
        self.drag_dir = None
        self.oneshot = ("hop", self.now() + self.loop_ms("hop") / 1000)

    def on_hover(self):
        self.last_activity = self.now()

    # ---------------------------------------------------------------- what to show

    def state(self) -> str:
        t = self.now()
        name = self._state(t)
        if self.shows and name not in CALM:  # back from a nap, a reaction, mail or work: not at once
            self.next_show = max(self.next_show, t + CALM_FIRST)
        return name

    def _state(self, t: float) -> str:
        if self.drag_dir:
            return self.drag_dir
        if self.oneshot:
            name, until = self.oneshot
            if t < until:
                return name
            self.oneshot = None
        if self.error:
            return "failed"
        if self.busy in ("sending", "thinking"):
            return self.busy
        if t < self.notify_until:
            return "notify"
        if self.unread > 0:
            return self.show_now(t) or "waiting"
        if self.off_hours or t - self.last_activity > DOZE_AFTER:
            return "sleeping"
        return self.show_now(t) or "idle"

    def show_now(self, t: float) -> str | None:
        """Now and then, while she only stands there, the outfit's own move. Not a touch: she still dozes off."""
        if not self.shows or t < self.next_show:
            return None
        self.oneshot = ("show", t + self.loop_ms("show") / 1000)
        self.next_show = t + self.loop_ms("show") / 1000 + self.wait(*SHOW_EVERY)
        return "show"

    def bubble(self) -> Bubble | None:
        t = self.now()
        if self.bubble_ and self.bubble_.until <= t:
            self.bubble_ = None
        if self.said and self.said.until <= t:
            self.said = None
        return self.bubble_ or self.said
