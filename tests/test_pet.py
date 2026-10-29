"""The desktop pet: what it decides to show, its settings, its pictures, and the app's side (/api/pet)."""

import asyncio
import ctypes
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import petfeed  # noqa: E402
from pet import brain, place, sprites  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PET_DIR = Path(__file__).resolve().parents[1] / "pet" / "whale-girl"
KOL_STATES = ["idle", "mail", "notify", "waiting", "sleeping", "failed", "thinking", "sending", "drag-left",
              "drag-right", "hop"]
TOUCH_STATES = ["pat", "poke", "giggle", "spout"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_brain(boot="b1"):
    clock, wall = Clock(), Clock()
    b = brain.Brain(lambda s: 1000, boot, now=clock, wall=wall)
    return b, clock, wall


def poll(b, wall, **kw):
    data = {"boot": "b1", "seq": 0, "events": [], "unread": 0, "busy": None, "error": None, "off_hours": False,
            "settings": {"enabled": True, "muted": False}, "url": "http://127.0.0.1:8765/"}
    data.update(kw)
    b.on_poll(data)


def mail(seq, at, **kw):
    return {"seq": seq, "at": at, "kind": "mail", "title": "新邮件 · Ana", "line1": "合作", "line2": "",
            "link": f"http://127.0.0.1:8765/#open=INBOX/{seq}", **kw}


# ---------------------------------------------------------------- brain

def test_a_new_mail_hops_then_holds_up_the_envelope_with_a_bubble():
    b, clock, wall = make_brain()
    poll(b, wall)
    poll(b, wall, events=[mail(1, wall.t)])
    assert b.state() == "mail"
    assert b.bubble().title == "新邮件 · Ana"
    clock.t += 1.5
    assert b.state() == "notify"
    clock.t += brain.BUBBLE_FOR
    assert b.bubble() is None
    clock.t += brain.NOTIFY_FOR
    assert b.state() == "idle"


def test_clicking_opens_the_mail_in_the_bubble_otherwise_the_app():
    b, clock, wall = make_brain()
    poll(b, wall)
    assert b.on_click() == "http://127.0.0.1:8765/"
    clock.t += 5
    poll(b, wall, events=[mail(2, wall.t)])
    assert b.on_click() == "http://127.0.0.1:8765/#open=INBOX/2"
    assert b.bubble() is None and b.state() == "hop"


def test_muted_changes_the_move_but_shows_no_bubble():
    b, clock, wall = make_brain()
    poll(b, wall, settings={"enabled": True, "muted": True})
    poll(b, wall, settings={"enabled": True, "muted": True}, events=[mail(1, wall.t)])
    assert b.state() == "mail" and b.bubble() is None


def test_a_second_mail_updates_the_bubble_and_counts_it():
    b, clock, wall = make_brain()
    poll(b, wall)
    poll(b, wall, events=[mail(1, wall.t)])
    poll(b, wall, events=[mail(2, wall.t, title="新邮件 · Bob")])
    assert b.bubble().title == "新邮件 · Bob" and b.bubble().more == 1


def test_on_start_older_mail_is_not_announced_again():
    b, clock, wall = make_brain()
    poll(b, wall, events=[mail(1, wall.t - 300), mail(2, wall.t - 5)])
    assert b.bubble().link.endswith("/2")


def test_precedence_drag_over_oneshot_over_error_over_busy_over_unread():
    b, clock, wall = make_brain()
    poll(b, wall, unread=3)
    assert b.state() == "waiting"
    poll(b, wall, unread=3, busy="thinking")
    assert b.state() == "thinking"
    poll(b, wall, unread=3, busy="sending", error="收信出错")
    assert b.state() == "failed"
    b.on_drag(-3)
    assert b.state() == "drag-left"
    b.on_drag(5)
    assert b.state() == "drag-right"
    b.on_drop()
    assert b.state() == "hop"


def test_falls_asleep_off_hours_or_when_left_alone():
    b, clock, wall = make_brain()
    poll(b, wall, off_hours=True)
    assert b.state() == "sleeping"
    poll(b, wall)
    assert b.state() == "idle"
    clock.t += brain.DOZE_AFTER + 1
    assert b.state() == "sleeping"
    b.on_hover()
    assert b.state() == "idle"


def test_leaves_when_switched_off_restarted_or_the_app_is_gone():
    b, clock, wall = make_brain()
    poll(b, wall, settings={"enabled": False})
    assert b.quit_reason == "switched off"
    b, clock, wall = make_brain()
    poll(b, wall, boot="b2")
    assert b.quit_reason and "restarted" in b.quit_reason
    b, clock, wall = make_brain()
    poll(b, wall)
    clock.t += brain.OFFLINE_EXIT - 1
    b.on_offline()
    assert b.quit_reason is None
    clock.t += 2
    b.on_offline()
    assert b.quit_reason == "the app stopped answering"


def test_only_the_app_s_own_page_is_ever_opened():
    from pet.client import own_page
    assert own_page("http://127.0.0.1:8765/#open=INBOX/7/me_163", 8765)
    assert own_page("http://127.0.0.1:8765/", 8765)
    for bad in ("http://127.0.0.1:1@evil.io/", "http://127.0.0.1:9999/", "https://127.0.0.1:8765/",
                "http://127.0.0.1:8765/../x", "http://127.0.0.1:8765/?q=1", "file:///C:/x", "http://127.0.0.1:8765@x/"):
        assert not own_page(bad, 8765), bad


def test_only_the_letter_opens_kol_mail_and_a_touch_gets_a_reaction():
    b, clock, wall = make_brain()
    b.pick = lambda lines: lines[0]
    poll(b, wall)
    assert b.on_touch("head") is None
    assert b.state() == "pat" and b.bubble().say and b.bubble().title == brain.REACTIONS["head"][1][0]
    clock.t += brain.SAY_FOR + 0.1
    assert b.bubble() is None
    for part, state in (("face", "poke"), ("body", "giggle"), ("tail", "spout"), ("feet", "hop")):
        assert b.on_touch(part) is None and b.state() == state, part
    assert b.on_touch("") is None  # nothing there
    assert b.on_touch("mail") == "http://127.0.0.1:8765/"
    poll(b, wall, events=[mail(3, wall.t), mail(4, wall.t, link="")])  # the newest has no link: the app at least
    assert b.on_touch("mail") == "http://127.0.0.1:8765/"


def test_small_talk_never_covers_a_new_mail_bubble():
    b, clock, wall = make_brain()
    poll(b, wall)
    b.on_touch("head")
    poll(b, wall, events=[mail(1, wall.t)])
    assert not b.bubble().say  # the mail took the bubble over
    b.on_touch("face")
    assert b.bubble().title == "新邮件 · Ana" and b.state() == "poke"
    assert b.on_touch("mail").endswith("/#open=INBOX/1")
    b.on_touch("body")
    assert b.bubble().say and b.on_bubble_click() is None and b.bubble() is None  # a click just closes it


def test_muted_she_still_reacts_but_says_nothing():
    b, clock, wall = make_brain()
    poll(b, wall, settings={"enabled": True, "muted": True})
    b.on_touch("tail")
    assert b.state() == "spout" and b.bubble() is None


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_dragging_works_again_after_a_click(monkeypatch):
    """A first click once left the drag direction as None, and every later drag failed."""
    from pet import win
    w = win.PetWindow.__new__(win.PetWindow)
    w.brain = brain.Brain(lambda s: 1000, "b")
    w.sprites = sprites.Sprites((10, 10), {}, "x")  # no part map: a click anywhere opens KOL Mail
    w.press, w.dragging, w.last_x, w.pos = None, False, None, (500, 500)
    w.hwnd = None  # no window: nothing to remember in physical pixels
    w.client = type("C", (), {"save": lambda self, **k: None})()
    moved, cursor = [], [0, 0]
    monkeypatch.setattr(win, "GetCursorPos", lambda p: (setattr(p._obj, "x", cursor[0]), setattr(p._obj, "y", cursor[1])))
    monkeypatch.setattr(win, "SetCapture", lambda h: None)
    monkeypatch.setattr(win, "ReleaseCapture", lambda: None)
    w.move = lambda x, y: (moved.append((x, y)), setattr(w, "pos", (x, y)))
    w.render = w.update_bubble = lambda *a, **k: None
    w.dpi = lambda: 1.0
    w.open = lambda url: None
    for drag in range(3):  # click, drag, drag again
        cursor[:] = [100, 100]
        w._handle(None, win.WM_LBUTTONDOWN, 0, 0)
        if drag:
            cursor[:] = [140, 110]
            w._handle(None, win.WM_MOUSEMOVE, win.MK_LBUTTON, 0)
        w._handle(None, win.WM_LBUTTONUP, 0, 0)
    assert len(moved) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_a_click_counts_what_was_under_the_cursor_when_pressed(monkeypatch):
    """At 60 frames a second her tail may have swung away by the time the button comes up."""
    from pet import win
    w = win.PetWindow.__new__(win.PetWindow)
    w.brain = brain.Brain(lambda s: 1000, "b")
    w.sprites = sprites.load(PET_DIR)
    w.press, w.dragging, w.last_x, w.pos = None, False, None, (0, 0)
    w.render = w.update_bubble = lambda *a, **k: None
    w.open = lambda url: None
    under = iter(["tail", ""])  # the tail at the press, nothing a moment later
    w.part_at = lambda x, y: next(under)
    monkeypatch.setattr(win, "GetCursorPos", lambda p: None)
    monkeypatch.setattr(win, "SetCapture", lambda h: None)
    monkeypatch.setattr(win, "ReleaseCapture", lambda: None)
    w._handle(None, win.WM_LBUTTONDOWN, 0, 0)
    w._handle(None, win.WM_LBUTTONUP, 0, 0)
    assert w.brain.state() == "spout"


class Screens:
    """Monitors as MonitorFromPoint and GetMonitorInfoW see them: (screen, work area) rectangles."""

    def __init__(self, *screens):
        self.screens = screens

    def from_point(self, pt, flags):
        from pet import win

        def gap(r):  # how far the point is from the rectangle
            return max(r[0] - pt.x, 0, pt.x - r[2] + 1) + max(r[1] - pt.y, 0, pt.y - r[3] + 1)
        nearest = min(range(len(self.screens)), key=lambda i: gap(self.screens[i][0]))
        if gap(self.screens[nearest][0]) and flags == win.MONITOR_DEFAULTTONULL:
            return None
        return nearest + 1

    def info(self, mon, p):
        from pet import win
        p._obj.rcWork = win.W.RECT(*self.screens[mon - 1][1])
        return True


LAPTOP = ((0, 0, 1536, 864), (0, 0, 1536, 816))
SECOND = ((1536, 0, 3456, 1080), (1536, 0, 3456, 1040))


def pet_on(monkeypatch, screens, pos=(0, 0)):
    from pet import win
    monkeypatch.setattr(win, "MonitorFromPoint", screens.from_point)
    monkeypatch.setattr(win, "GetMonitorInfoW", screens.info)
    w = win.PetWindow.__new__(win.PetWindow)
    w.size, w.pos, w.scale = (100, 200), pos, 1.0
    w.press, w.place, w.data, w.legacy_target = None, None, None, None
    return w


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_she_starts_where_she_was_left_unless_no_screen_shows_that_place(monkeypatch):
    """A place saved on a monitor since unplugged once kept her off every screen, still running, out of sight."""
    w = pet_on(monkeypatch, Screens(LAPTOP))
    for saved, start in (((200, 300), (200, 300)),
                         ((1480, 300), (1480, 300)),  # partly past the edge, her middle still on it: left alone
                         ((1685, 715), (1436, 616)),  # past the right edge: just inside, above the taskbar
                         ((None, None), (1412, 616))):  # never moved: bottom right
        w.settings = {"x": saved[0], "y": saved[1]}
        w.place_initially()
        assert w.pos == start


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_when_her_screen_goes_away_she_comes_back_onto_the_nearest_one(monkeypatch):
    from pet import win
    screens = Screens(LAPTOP, SECOND)
    w = pet_on(monkeypatch, screens, pos=(2000, 500))
    moved = []
    w.resize = lambda scale: None
    w.move = lambda x, y: (moved.append((x, y)), setattr(w, "pos", (x, y)))
    w._handle(None, win.WM_DISPLAYCHANGE, 0, 0)
    assert moved == []  # still on the second screen
    screens.screens = (LAPTOP,)  # unplugged
    w._handle(None, win.WM_DISPLAYCHANGE, 0, 0)
    assert moved == [(1436, 500)]
    screens.screens = (LAPTOP, SECOND)
    w.pos = (2000, 1500)  # below both: the second one is nearer
    w._handle(None, win.WM_DISPLAYCHANGE, 0, 0)
    assert moved[-1] == (2000, 840)


# ---------------------------------------------------------------- her place in physical pixels (pet/place.py)

# A laptop at 150 % with an external monitor at 100 % to its right, in physical pixels.
PANEL = place.Screen(r"\\.\DISPLAY1", (0, 0, 2560, 1440), (0, 0, 2560, 1380), 144, True)
DESK = place.Screen(r"\\.\DISPLAY2", (2560, 0, 4480, 1080), (2560, 0, 4480, 1040), 96)


def at_96(screen):
    """Her size there: 100 x 200 at 96 DPI, enlarged by the screen's scaling."""
    return round(100 * screen.dpi / 96), round(200 * screen.dpi / 96)


def test_a_place_on_the_second_screen_is_the_same_place_after_a_restart():
    """Saved in 96-DPI coordinates, a place on a screen scaled otherwise meant somewhere else at the next start."""
    saved = place.remember((3000, 600, 3100, 800), [PANEL, DESK], legacy=(2000, 400))
    assert saved["device"] == DESK.device and saved["feet"] == [3050, 800]
    x, y, screen, how = place.target(saved, [PANEL, DESK], at_96)
    assert (x, y, screen, how) == (3000, 600, DESK, "her screen")
    x, y, screen, _ = place.target(place.remember((900, 900, 1050, 1200), [PANEL, DESK], (600, 600)), [PANEL, DESK],
                                   at_96)
    assert (x, y, screen) == (900, 900, PANEL)  # on the 150 % panel she is 150 x 300: her feet stay where they were


def test_before_her_screen_is_up_she_waits_on_the_nearest_one_and_goes_back_when_it_comes():
    saved = place.remember((4300, 800, 4400, 1000), [PANEL, DESK], (0, 0))
    x, y, screen, how = place.target(saved, [PANEL], at_96)  # logon: the external monitor is not there yet
    assert screen == PANEL and how.startswith("the nearest") and (x, y) == (2410, 700)
    assert place.target(saved, [PANEL, DESK], at_96)[:2] == (4300, 800)
    assert place.target(saved, [], at_96) is None


def test_her_screen_at_another_resolution_keeps_her_at_the_same_spot_of_it():
    saved = place.remember((4300, 800, 4400, 1000), [PANEL, DESK], (0, 0))
    bigger = place.Screen(DESK.device, (2560, 0, 5120, 1440), (2560, 0, 5120, 1400), 96)
    x, y, screen, how = place.target(saved, [PANEL, bigger], at_96)
    assert how == "her screen, resized or moved" and screen == bigger
    assert (x + 50, y + 200) == (2560 + round((4350 - 2560) / 1920 * 2560), 1000 * 1400 // 1040)


def test_screens_that_swapped_names_are_told_apart_by_where_they_are():
    """\\\\.\\DISPLAY1 and 2 may swap after a restart: the name alone once put her, scaled, on the other screen."""
    saved = place.remember((3000, 600, 3100, 800), [PANEL, DESK], legacy=(0, 0))
    swapped = [place.Screen(DESK.device, PANEL.rect, PANEL.work, 144, True),
               place.Screen(PANEL.device, DESK.rect, DESK.work, 96)]
    x, y, screen, how = place.target(saved, swapped, at_96)
    assert (x, y, screen.rect) == (3000, 600, DESK.rect) and "now named" in how
    # her name went to a screen on the left, and a new screen stands where hers was: she goes to that one
    left = place.Screen(DESK.device, (-1920, 0, 0, 1080), (-1920, 0, 0, 1040), 96)
    new = place.Screen(r"\\.\DISPLAY3", (2560, 0, 4480, 1080), (2560, 0, 4480, 1040), 120)
    x, y, screen, how = place.target(saved, [PANEL, left, new], at_96)
    assert screen == new and "another screen now" in how and (x + 125 // 2, y + 250) == (3050, 800)


def test_a_place_file_counts_only_while_pet_json_still_has_its_x_and_y(tmp_path):
    saved = place.remember((3000, 600, 3100, 800), [PANEL, DESK], legacy=(2000, 400))
    place.save(tmp_path, saved)
    assert place.valid(place.load(tmp_path), {"x": 2000, "y": 400}) == saved
    assert place.valid(place.load(tmp_path), {"x": 10, "y": 400}) is None  # an older version moved her since
    newer = place.remember((900, 900, 1050, 1200), [PANEL, DESK], legacy=(600, 600), previous=(2000, 400))
    place.save(tmp_path, newer)
    assert place.valid(place.load(tmp_path), {"x": 2000, "y": 400}) == newer  # the app missed the last x, y
    assert place.valid(place.load(tmp_path), {"x": 600, "y": 600}) == newer
    assert place.valid({**newer, "previous": "x"}, {"x": 600, "y": 600}) is None
    for broken in ({**saved, "feet": [1]}, {**saved, "version": 2}, {**saved, "device": None}, [], None):
        assert place.valid(broken, {"x": 2000, "y": 400}) is None
    (tmp_path / place.FILE).write_text("{not json", encoding="utf-8")
    assert place.load(tmp_path) is None and place.load(None) is None


# A Mac's screens in Tk's points: the built-in one with the menu bar and the Dock, a monitor to its right that sits a
# little higher (its own menu bar), and one above the built-in one.
BUILT_IN = place.Screen("screen 0", (0, 0, 1512, 982), (0, 33, 1512, 920), primary=True)
RIGHT = place.Screen("screen 1", (1512, -200, 3432, 880), (1512, -175, 3432, 880))
ABOVE = place.Screen("screen 2", (0, -900, 1440, 0), (0, -875, 1440, 0))


def test_on_a_mac_her_bubble_stays_on_the_screen_she_stands_on():
    """Tk knows the first screen only: the bubble was kept inside it, so it came up on screen 1 while she stood on
    screen 2."""
    mac_screens = [BUILT_IN, RIGHT, ABOVE]
    assert place.bubble_at(mac_screens, (2000, 300, 2200, 500), (300, 80), 1950, 244) == (1950, 244)
    assert place.bubble_at(mac_screens, (500, -600, 700, -400), (300, 80), 450, -656) == (450, -656)
    assert place.bubble_at(mac_screens, (3300, 300, 3500, 500), (300, 80), 3250, 244) == (3132, 244)  # her right edge
    assert place.bubble_at(mac_screens, (2000, -190, 2200, 10), (300, 80), 1950, -246) == (1950, -175)  # under its bar
    assert place.bubble_at(mac_screens, (1450, 300, 1650, 500), (300, 80), 1400, 244) == (1512, 244)  # her middle's
    assert place.bubble_at(mac_screens, (5000, 300, 5200, 500), (300, 80), 4950, 244) == (3132, 244)  # nearest one
    assert place.bubble_at([BUILT_IN], (1300, 600, 1500, 800), (300, 80), 1250, 544) == (1212, 544)


def placed_pet(monkeypatch, tmp_path, screens, settings):
    from pet import win
    w = win.PetWindow.__new__(win.PetWindow)
    w.size, w.pos, w.scale, w.settings, w.data, w.hwnd = (100, 200), (0, 0), 1.0, settings, tmp_path, 1
    w.press, w.place, w.legacy_target, w.screens_seen = None, None, None, ()
    w.found = list(screens)
    w.screens = lambda: list(w.found)
    moves = []

    def move_physical(x, y):  # what Windows would report back in 96 DPI on that screen
        s = place.holder(w.found, x, y) or w.found[0]
        moves.append((x, y))
        w.pos = (round(x * 96 / s.dpi), round(y * 96 / s.dpi))
    w.move_physical = move_physical
    w.move = lambda x, y: setattr(w, "pos", (x, y))
    monkeypatch.setattr(win, "SetThreadDpiAwarenessContext", lambda ctx: None)  # truthy check only
    return w, moves


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_she_starts_on_her_place_and_follows_it_when_screens_come_late_but_not_after_a_drag(monkeypatch, tmp_path):
    from pet import win
    place.save(tmp_path, place.remember((3000, 600, 3100, 800), [PANEL, DESK], legacy=(2000, 400)))
    w, moves = placed_pet(monkeypatch, tmp_path, [PANEL], {"x": 2000, "y": 400})
    w.place_initially()  # logon: only the panel so far
    assert moves[-1] == (2410, 500)
    w.found = [PANEL, DESK]
    w.check_screens()  # no message came: the next TIMER_CHECK notices
    assert moves[-1] == (3000, 600)
    w.check_screens()
    assert len(moves) == 2  # nothing changed, nothing moved
    # she is dragged onto the panel: that is her place from now on
    w.window_rect = lambda: win.W.RECT(900, 900, 1050, 1200)
    w.remember()
    assert place.load(tmp_path)["device"] == PANEL.device
    w._handle(None, win.WM_SETTINGCHANGE, win.SPI_SETWORKAREA, 0)  # an app bar: nothing of hers changed
    assert len(moves) == 2
    w.resize = lambda scale: None
    w.found = [PANEL]  # the monitor unplugged and plugged back: she stays on the panel, where she was put last
    w._handle(None, win.WM_DISPLAYCHANGE, 0, 0)
    w.found = [PANEL, DESK]
    w._handle(None, win.WM_DISPLAYCHANGE, 0, 0)
    assert moves[-1] == (900, 900) and (3000, 600) not in moves[2:]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_a_drag_cut_short_by_a_lost_capture_is_still_her_new_place(monkeypatch, tmp_path):
    from pet import win
    w, moves = placed_pet(monkeypatch, tmp_path, [PANEL, DESK], {"x": 2000, "y": 400})
    w.brain = brain.Brain(lambda s: 1000, "b")
    saved = []
    w.client = type("C", (), {"save": lambda self, **k: saved.append(k)})()
    w.press, w.dragging, w.pos = (0, 0, 600, 600), True, (600, 600)
    w.window_rect = lambda: win.W.RECT(900, 900, 1050, 1200)
    w._handle(None, win.WM_MOUSEMOVE, 0, 0)  # Alt+Tab mid-drag: the button is up without a WM_LBUTTONUP
    assert saved == [{"x": 600, "y": 600}] and not w.dragging and w.press is None
    assert place.load(tmp_path)["feet"] == [975, 1200] and place.load(tmp_path)["previous"] == [2000, 400]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_a_dead_polling_thread_is_noticed_even_when_the_screen_check_fails(monkeypatch, tmp_path):
    from pet import win
    w, _ = placed_pet(monkeypatch, tmp_path, [PANEL], {})
    w.client = type("C", (), {"is_alive": lambda self: False})()
    w.check_screens = lambda: 1 / 0
    destroyed = []
    monkeypatch.setattr(win, "DestroyWindow", lambda h: destroyed.append(h))
    w._handle(None, win.WM_TIMER, win.TIMER_CHECK, 0)
    assert destroyed == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_an_outdated_place_file_leaves_her_at_pet_json_s_x_and_y(monkeypatch, tmp_path):
    place.save(tmp_path, place.remember((3000, 600, 3100, 800), [PANEL, DESK], legacy=(2000, 400)))
    w, moves = placed_pet(monkeypatch, tmp_path, [PANEL, DESK], {"x": 300, "y": 200})
    monkeypatch.setattr("pet.win.MonitorFromPoint", lambda pt, flags: 1)  # (300, 200) is on a screen
    w.place_initially()
    assert moves == [] and w.pos == (300, 200) and w.place is None and w.legacy_target == (300, 200)


# ---------------------------------------------------------------- sprites

def png_size(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    assert raw.startswith(b"\x89PNG"), path.name
    return struct.unpack(">II", raw[16:24])


def test_whale_girl_has_every_state_at_60_frames_a_second_inside_her_pictures():
    s = sprites.load(PET_DIR)
    assert set(KOL_STATES + TOUCH_STATES) <= set(s.states)
    assert s.get("idle").durations[0] == pytest.approx(1000 / 60) and s.touchable
    cw, ch = s.cell
    pw, ph = cw // s.part_scale, ch // s.part_scale
    assert ch % s.part_scale == 0  # no half rows in the part maps
    for name, st in s.states.items():
        assert st.loop_ms > 0 and st.durations[0] <= 34, name  # never below 30 frames a second
        layers = [(st.layer, [st.base_frame(i) for i in range(st.frames)])]
        if st.overlay:
            layers.append((st.overlay, [st.over_frame(i) for i in range(st.frames)]))
        for layer, frames in layers:
            for i in frames:
                band, j = divmod(i, layer.per)
                w, h = png_size(layer.bands[band])
                assert w == cw and (j + 1) * ch <= h, name
                w, h = png_size(layer.parts[band])
                assert w == pw and (j + 1) * ph <= h, name
        if st.overlay:
            x0, y0, x1, y1 = st.overlay.box
            assert 0 <= x0 < x1 <= cw and 0 <= y0 < y1 <= ch, name
    pictures = list(PET_DIR.glob("*.png"))
    for p in pictures:
        w, h = png_size(p)
        assert h <= 5 * ch, p.name  # small enough for Tk to decode quickly
    assert sum(p.stat().st_size for p in pictures) < 10 * 1024 * 1024


def v2_pet(folder: Path, **state) -> Path:
    """A made-up pet: a 6-frame clip in bands of 3, and an idle state of 4 steps with 2 frames of props."""
    for name in ("c.0.png", "c.0.parts.png", "c.1.png", "c.1.parts.png", "o.0.png", "o.0.parts.png"):
        (folder / name).write_bytes(b"")
    idle = {"clip": "sway", "frames": 4, "fps": 60, "pick": [0, 2, 4, 5],
            "overlay": {"bands": [["o.0.png", "o.0.parts.png"]], "per": 5, "frames": 2, "pick": [0, 0, 1, 1],
                        "box": [10, 20, 30, 40]}}
    meta = {"kolmail": {"version": 2, "cell": [100, 200],
                        "clips": {"sway": {"bands": [["c.0.png", "c.0.parts.png"], ["c.1.png", "c.1.parts.png"]],
                                           "per": 3, "frames": 6}},
                        "states": {"idle": {**idle, **state}}, "parts": {"scale": 2, "names": ["", "head", "mail"]}}}
    (folder / "pet.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def test_a_click_reads_the_props_on_top_first_then_the_clip_frame_shown(tmp_path):
    s = sprites.load(v2_pet(tmp_path))
    st = s.get("idle")
    assert [st.base_frame(i) for i in range(4)] == [0, 2, 4, 5] and st.loop_ms == pytest.approx(4000 / 60)
    assert st.layer.at(4, s.cell) == (tmp_path.resolve() / "c.1.png", 0, 200) and st.overlay.box == (10, 20, 30, 40)
    (over, ox, oy), (clip, cx, cy) = s.part_lookups("idle", 2, 31, 41)
    assert over.name == "o.0.parts.png" and (ox, oy) == (15, 1 * 100 + 20)  # props frame 1
    assert clip.name == "c.1.parts.png" and (cx, cy) == (15, 1 * 100 + 20)  # clip frame 4: band 1, second down
    assert s.part_lookups("idle", 0, 50, 199.5)[0][2] == 99  # the last row stays inside its frame
    assert s.part_lookups("idle", 0, 150, 10) == []  # outside her cell
    assert s.part_name(80) == "mail" and s.part_name(40) == "head" and s.part_name(0) == ""


@pytest.mark.parametrize("change", [
    {"pick": [0, 2, 4, 6]}, {"pick": [0, 1]}, {"pick": None, "frames": 7}, {"frames": 0}, {"frames": -3},
    {"frames": True}, {"fps": 0.5}, {"fps": "60"}, {"clip": "nowhere"},
    {"overlay": {"bands": [["o.0.png"]], "per": 5, "frames": 9, "pick": [0, 0, 0, 0]}},
    {"overlay": {"bands": [["gone.png"]], "per": 5, "frames": 1, "pick": [0, 0, 0, 0]}},
    {"overlay": {"bands": [["../x.png"]], "per": 5, "frames": 1, "pick": [0, 0, 0, 0]}},
    {"overlay": {"bands": [["o.0.png"]], "per": 5, "frames": 1, "pick": [0, 0, 0, 0], "box": [1, 2]}},
    {"overlay": "props"},
])
def test_a_pet_json_that_does_not_add_up_is_refused_at_once(tmp_path, change):
    state = {k: v for k, v in change.items() if v is not None}
    folder = v2_pet(tmp_path, **state)
    if change.get("pick", 0) is None:
        meta = json.loads((folder / "pet.json").read_text(encoding="utf-8"))
        meta["kolmail"]["states"]["idle"].pop("pick")
        (folder / "pet.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError):
        sprites.load(folder)


def test_a_newer_or_broken_pet_format_is_refused(tmp_path):
    for meta in ({"kolmail": {"version": 3}}, {"kolmail": {"version": 2}}, {"kolmail": {"version": 2, "cell": [0, 9],
                 "states": {}}}, {"kolmail": "v2"}):
        (tmp_path / "pet.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(ValueError):
            sprites.load(tmp_path)


def test_a_plain_codex_pet_maps_our_states_onto_its_rows(tmp_path):
    (tmp_path / "spritesheet.webp").write_bytes(b"")
    (tmp_path / "pet.json").write_text('{"id": "x", "displayName": "X", "spritesheetPath": "spritesheet.webp"}',
                                       encoding="utf-8")
    s = sprites.load(tmp_path)
    assert s.cell == (192, 208) and not s.touchable
    assert s.resolve("mail") == "jumping" and s.resolve("sleeping") == "idle" and s.resolve("drag-left") == "running-left"
    assert s.resolve("thinking") == "review" and s.resolve("whatever") == "idle" and s.resolve("pat") == "waving"
    assert s.get("drag-left").layer.at(2, s.cell) == (tmp_path.resolve() / "spritesheet.webp", 384, 2 * 208)


def test_a_sheet_outside_the_pet_folder_is_refused(tmp_path):
    (tmp_path / "pet.json").write_text('{"spritesheetPath": "../../secret.png"}', encoding="utf-8")
    with pytest.raises(ValueError):
        sprites.load(tmp_path)


@pytest.fixture
def gdiplus():
    from pet import win
    token = ctypes.c_size_t()
    win.check(win.GdiplusStartup(ctypes.byref(token), ctypes.byref(win.GdiplusStartupInput(1, None, False, False)),
                                 None), "Startup")
    return win


def stub_window(win, size=None):
    s = sprites.load(PET_DIR)
    w = win.PetWindow.__new__(win.PetWindow)
    w.sprites, w.size, w.mix, w.scale, w.own_dpi = s, size or s.cell, None, 1.0, lambda: 1.0
    w.strips = win.Cache(lambda path: win.Strip(path, s.cell, w.size), budget=win.STRIP_BUDGET)
    w.part_maps = win.Cache(win.PartMap, budget=16 << 20)
    return w


def pixel(bits, width: int, x: int, y: int) -> int:
    return ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint32))[y * width + x]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows drawing code")
def test_her_letter_her_head_her_tail_and_the_air_around_her(gdiplus):
    w = stub_window(gdiplus)
    w.shown_state, w.frame = "idle", 0
    try:
        assert w.part_at(191, 220) == "mail"   # the letter in her right hand
        assert w.part_at(110, 115) == "head"   # her hat
        assert w.part_at(3, 3) == ""           # the air: nothing to touch
        cw, ch = w.sprites.cell
        seen = {w.part_at(x, y) for x in range(0, cw, 4) for y in range(0, ch, 4)}
        assert {"head", "face", "body", "tail", "feet", "mail"} <= seen
        w.shown_state = "sleeping"  # facing front: the tail's fin to her left, below the skirt
        assert {w.part_at(x, y) for x in range(150, 200, 2) for y in range(290, 315, 2)} & {"tail"}
    finally:
        w.part_maps.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows drawing code")
def test_the_letter_is_blended_over_her_turn(gdiplus):
    win = gdiplus
    w = stub_window(win)
    cw, ch = w.sprites.cell
    st = w.sprites.get("idle")
    try:
        dc, x, y = w.picture(st, 0)
        assert (dc, x, y) == (w.mix.dc, 0, 0)
        base = w.strips.get(st.layer.at(0, (cw, ch))[0])
        over = w.strips.get(st.overlay.at(0, (cw, ch))[0])
        assert pixel(w.mix.bits, cw, 191, 220) == pixel(over.bits, cw, 191, 220) != 0  # the letter, on top
        assert pixel(w.mix.bits, cw, 110, 115) == pixel(base.bits, cw, 110, 115) != 0  # her hat, from the clip
        assert pixel(w.mix.bits, cw, 3, 3) == 0  # still see-through
        assert w.picture(w.sprites.get("pat"), 7)[1:] == (0, 2 * ch)  # her own frames: straight from the band
    finally:
        w.strips.close()
        w.mix.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows drawing code")
def test_at_the_small_and_big_sizes_each_frame_is_scaled_once_and_alone(gdiplus):
    win = gdiplus
    s = sprites.load(PET_DIR)
    cw, ch = s.cell
    for size in ((165, 268), (275, 448)):
        strip = win.Strip(s.get("pat").layer.bands[0], s.cell, size)
        try:
            assert (strip.w, strip.h) == (size[0], 5 * size[1])
            assert pixel(strip.bits, strip.w, size[0] // 2, size[1] * 115 // ch) >> 24 >= 250  # her hat, opaque
            for frame in range(1, 5):  # the air above her head: nothing of the frame before seeps in
                assert all(pixel(strip.bits, strip.w, x, frame * size[1]) == 0 for x in range(size[0]))
        finally:
            strip.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows drawing code")
def test_a_picture_that_cannot_be_read_is_said_once_and_not_retried_every_frame(gdiplus, capsys):
    win = gdiplus
    calls = []

    def broken(path):
        calls.append(path)
        raise OSError("locked")

    cache = win.Cache(broken, budget=1)
    for _ in range(3):
        with pytest.raises(OSError):
            cache.get(PET_DIR / "pat.0.png")
    assert len(calls) == 1 and capsys.readouterr().out.count("pat.0.png") == 1
    w = stub_window(win)
    w.strips, w.part_maps = cache, cache
    w.shown_state, w.frame, w.started, w.shown_key, w.hidden_for_fullscreen = "idle", 7, 0.0, ("idle", None), False
    w.brain = brain.Brain(lambda s: 1000, "b")
    w.render(force=True)  # nothing to show yet: the window keeps its last frame, no exception
    assert w.frame == 7 and w.part_at(110, 115) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_the_same_reaction_twice_plays_from_the_start_again(monkeypatch):
    from pet import win
    clock, tick = Clock(), [0.0]
    monkeypatch.setattr(win.time, "perf_counter", lambda: tick[0])
    w = win.PetWindow.__new__(win.PetWindow)
    w.sprites = sprites.load(PET_DIR)
    w.brain = brain.Brain(lambda s: w.sprites.get(s).loop_ms, "b", now=clock)
    w.shown_state, w.frame, w.started, w.shown_key = "", -1, 0.0, None
    w.brain.on_touch("head")
    assert w.current()[0] == "pat" and w.current()[2] == 0
    tick[0] += 1.0
    clock.t += 1.0
    assert w.current()[2] > 50
    w.brain.on_touch("head")  # patted again before the first one ended
    assert w.current()[0] == "pat" and w.current()[2] == 0


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window code")
def test_a_codex_pet_s_beat_keeps_up_with_its_shortest_frame(tmp_path):
    from pet import win
    (tmp_path / "spritesheet.png").write_bytes(b"")
    (tmp_path / "pet.json").write_text("{}", encoding="utf-8")
    w = win.PetWindow.__new__(win.PetWindow)
    w.sprites = sprites.load(tmp_path)
    w.brain = brain.Brain(lambda s: 1000, "b")
    w.shown_state, w.frame, w.started, w.shown_key, w.hidden_for_fullscreen = "", 0, 0.0, None, False
    w.picture = lambda st, step: (_ for _ in ()).throw(OSError("not drawn here"))
    w.render()
    assert w.tick_hz == pytest.approx(2000 / 110)  # idle's shortest frame, not its first (280 ms)


# ---------------------------------------------------------------- the Mac's layers (pet/macdraw.py)

def png_alpha(path: Path) -> bytes:
    """The alpha channel of an 8-bit PNG, paletted (as the pet's pictures are) or RGBA, decoded here without any
    image library."""
    raw, pos, idat, trns = path.read_bytes(), 8, b"", b""
    while pos < len(raw):
        n, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            w, h, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and colour in (3, 6) and interlace == 0
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
    bpp = 1 if colour == 3 else 4
    data, stride = zlib.decompress(idat), w * bpp
    prev, alpha = bytearray(stride), bytearray()
    for r in range(h):
        start = r * (stride + 1)
        kind, line = data[start], bytearray(data[start + 1:start + 1 + stride])
        for i in range(stride):
            a, b, c = (line[i - bpp] if i >= bpp else 0), prev[i], (prev[i - bpp] if i >= bpp else 0)
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        alpha += bytes(trns[k] if k < len(trns) else 255 for k in line) if colour == 3 else line[3::4]
        prev = line
    return bytes(alpha)


def test_macdraw_loads_nothing_until_it_is_used():
    """Importable anywhere (the other platforms import the pet package too): no library is loaded at import. In a
    process of its own, as another test may have used it already."""
    import subprocess
    script = "import sys; sys.path.insert(0, sys.argv[1]); from pet import macdraw; print(macdraw._rt)"
    out = subprocess.run([sys.executable, "-I", "-c", script, str(PET_DIR.parents[1])], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "None", out.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac drawing code")
def test_a_mac_strip_keeps_her_alpha_row_for_row_and_cuts_whole_cells():
    from pet import macdraw
    s = sprites.load(PET_DIR)
    cw, ch = s.cell
    band = s.get("pat").layer.bands[0]
    strip = macdraw.Strip(band, s.cell)
    try:
        assert strip.size == png_size(band) == (cw, 5 * ch)
        assert strip.alpha_rows == png_alpha(band)  # top row first, nothing lost to premultiplying
        assert strip.alpha(cw // 2, 115) >= 250 and strip.alpha(3, 3) == 0  # her hat; the air
        assert strip.alpha(-1, 0) == strip.alpha(cw, 0) == strip.alpha(0, 5 * ch) == 0  # off the picture
        cg = macdraw.rt().cg
        img = strip.frame(0, 2 * ch)
        assert (cg.CGImageGetWidth(img), cg.CGImageGetHeight(img)) == (cw, ch)
        assert strip.frame(0, 2 * ch) == img  # cut once, then reused
    finally:
        strip.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac window code")
def test_on_a_mac_tk_s_own_picture_is_hidden_and_the_layers_show_her():
    import tkinter as tk
    from ctypes import c_bool, c_float, c_ulong
    from pet import macdraw
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no screen to open a window on: {e}")
    s = sprites.load(PET_DIR)
    cw, ch = s.cell
    strip = None
    try:
        root.withdraw()
        root.overrideredirect(True)
        root.wm_attributes("-transparent", True)
        root.config(bg="systemTransparent")
        root.geometry(f"{cw}x{ch}+40+40")
        root.wm_attributes("-alpha", 0.0)
        root.deiconify()
        root.update()
        root.title("KOL Mail pet test")
        layers = macdraw.Layers("KOL Mail pet test", s.cell)
        send = macdraw.send
        content = send(layers.window, "contentView")
        assert send(send(content, "layer"), "opacity", restype=c_float) == 0.0  # Tk's black box: out of sight
        assert send(content, "alphaValue", restype=ctypes.c_double) == 0.0  # and AppKit will not sync it back
        views = send(send(content, "superview"), "subviews")
        order = [send(views, "objectAtIndex:", i, argtypes=(c_ulong,)) for i in range(send(views, "count", restype=c_ulong))]
        assert order.index(layers.view) < order.index(content)  # under Tk's view, which keeps the mouse
        strip = macdraw.Strip(s.get("pat").layer.bands[0], s.cell)
        layers.show(strip.frame(0, 0), strip.frame(0, ch))
        layers.show(strip.frame(0, ch), None)  # the next frame replaces the last whole, props gone
        assert send(layers.base, "contents") == strip.frame(0, ch) and send(layers.over, "contents") is None
        for ignore in (True, False, True):
            layers.click_through(ignore)
            assert send(layers.window, "ignoresMouseEvents", restype=c_bool) is ignore
    finally:
        if strip:
            strip.close()
        root.destroy()


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac window code")
def test_on_a_mac_the_window_takes_clicks_only_where_she_is():
    from pet import mac, macdraw
    s = sprites.load(PET_DIR)
    p = mac.PetApp.__new__(mac.PetApp)
    p.sprites, p.pos, p.press, p.frame, p.shown_strips = s, (100, 200), None, -1, ()
    calls, pointer, held = [], [(0, 0)], [True]
    p.layers = type("Layers", (), {"show": lambda self, base, over: None, "button_down": lambda self: held[0],
                                   "click_through": lambda self, ignore: calls.append(ignore)})()
    p.root = type("Root", (), {"winfo_pointerxy": lambda self: pointer[0]})()
    p.strips = mac.Pictures(64 << 20, load=lambda path: macdraw.Strip(path, s.cell),
                            weight=lambda strip: strip.nbytes, drop=lambda strip: strip.close())
    p.show_on_layers(s.get("idle"), 0)
    assert p.frame == 0 and len(p.shown_strips) == 2  # her turn and the letter in her hand
    for where in ((110, 115), (3, 3), (191, 220), (-50, -50)):  # her hat, the air, the letter, off the window
        pointer[0] = (100 + where[0], 200 + where[1])
        p.update_click_through()
    p.press = (0, 0, 100, 200)
    pointer[0] = (103, 203)
    p.update_click_through()  # mid-drag over the air: the window keeps the mouse
    assert calls == [False, True, False]
    held[0] = False
    p.update_click_through()  # the release never came, but no button is held: the air lets clicks through again
    assert calls == [False, True, False, True]


def mac_pet(settings: dict):
    import tkinter as tk
    from pet import mac
    try:
        return mac.PetApp(sprites.load(PET_DIR), 9, "b", {"enabled": True, **settings})
    except tk.TclError as e:
        pytest.skip(f"no screen to open a window on: {e}")


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac window code")
def test_on_a_mac_the_screens_are_read_in_tk_s_coordinates():
    from pet import macdraw
    p = mac_pet({})
    try:
        (first, first_work), *others = macdraw.screens()
        assert first == (0, 0, p.root.winfo_screenwidth(), p.root.winfo_screenheight())
        assert first[0] <= first_work[0] < first_work[2] <= first[2] and first[1] < first_work[1] < first_work[3]
        assert first_work[3] <= first[3]  # the menu bar at the top, the Dock (if at the bottom) under the work area
        for rect, work in others:
            assert rect[0] < rect[2] and rect[1] < rect[3] and rect[0] <= work[0] and work[2] <= rect[2]
    finally:
        p.root.destroy()


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac window code")
def test_on_a_mac_she_starts_on_the_second_screen_where_she_was_left(monkeypatch):
    """Her saved place counted only when it lay on the first screen: on the second one she was sent back to it."""
    from pet import mac
    monkeypatch.setattr(mac.macdraw, "screens", lambda: [(BUILT_IN.rect, BUILT_IN.work), (RIGHT.rect, RIGHT.work)])
    for saved, kept in (((2000, 300), True), ((2000, -150), True), ((5000, 300), False), ((300, 400), True)):
        p = mac_pet({"x": saved[0], "y": saved[1]})
        try:
            assert (p.pos == saved) is kept, saved
        finally:
            p.root.destroy()


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac window code")
def test_on_a_mac_she_and_her_bubble_stay_over_every_window_on_every_space(monkeypatch):
    """Tk clears -topmost as it makes the NSWindow of a window without a frame: she came out a normal window that
    other windows covered, on one desktop only."""
    from ctypes import c_long, c_ulong
    from pet import mac, macdraw
    p = mac_pet({})
    send = macdraw.send
    try:
        assert p.attach_layers()
        window = macdraw.find_window(mac.WINDOW_TITLE)
        assert send(window, "level", restype=c_long) == macdraw.TOPMOST
        assert send(window, "collectionBehavior", restype=c_ulong) & macdraw.EVERY_SPACE == macdraw.EVERY_SPACE
        assert int(p.root.wm_attributes("-topmost")) == 1
        app_ = send(macdraw.cls("NSApplication"), "sharedApplication")
        assert send(app_, "activationPolicy", restype=c_long) == macdraw.ACCESSORY
        send(app_, "setActivationPolicy:", 0, restype=ctypes.c_bool, argtypes=(c_long,))  # what Tk does once launched
        p.stay_accessory()  # every poll
        assert send(app_, "activationPolicy", restype=c_long) == macdraw.ACCESSORY and p.made_regular == 1
        # AppKit's rectangles flipped into Tk's coordinates: her window is where Tk put her
        cw, ch = p.sprites.cell
        first_height = macdraw.screens()[0][0][3]
        assert macdraw.tk_rect(macdraw._rect(window, b"frame"), first_height) == (*p.pos, p.pos[0] + cw, p.pos[1] + ch)
        for n, text in enumerate(("新邮件 · Ana", "新邮件 · Bo"), 1):  # each bubble is a window of its own
            monkeypatch.setattr(p.brain, "bubble", lambda text=text: brain.Bubble(text, "合作", "", ""))
            p.update_bubble()
            bubble = macdraw.find_window(f"{mac.BUBBLE_TITLE} {n}")
            assert send(bubble, "level", restype=c_long) == macdraw.TOPMOST + 1  # over her head, not under it
            assert send(bubble, "collectionBehavior", restype=c_ulong) & macdraw.EVERY_SPACE == macdraw.EVERY_SPACE
        assert not p.said  # nothing went wrong on the way
        p.root.wm_attributes("-topmost", False)
        p.root.wm_attributes("-topmost", True)
        assert send(window, "level", restype=c_long) == macdraw.TOPMOST  # the very level Tk's own -topmost sets
    finally:
        p.root.destroy()


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac window code")
def test_on_a_mac_her_bubble_comes_up_on_the_screen_she_stands_on(monkeypatch):
    import re
    from pet import mac
    monkeypatch.setattr(mac.macdraw, "screens", lambda: [(BUILT_IN.rect, BUILT_IN.work), (RIGHT.rect, RIGHT.work)])
    placed, geometry = [], mac.tk.Toplevel.wm_geometry
    monkeypatch.setattr(mac.tk.Toplevel, "geometry",
                        lambda self, spec=None: (placed.append(spec), geometry(self, spec))[1])
    p = mac_pet({"x": 3300, "y": 300})  # at the right edge of the screen on the right
    try:
        monkeypatch.setattr(p.brain, "bubble", lambda: brain.Bubble("新邮件 · Ana", "合作", "", ""))
        p.update_bubble()
        bx, by = map(int, re.fullmatch(r"\+(-?\d+)\+(-?\d+)", placed[-1]).groups())
        bw = p.bubble_win.winfo_reqwidth()
        assert RIGHT.work[0] <= bx and bx + bw <= RIGHT.work[2] and RIGHT.work[1] <= by < 300
        monkeypatch.setattr(mac.macdraw, "screens", lambda: (_ for _ in ()).throw(OSError("no AppKit")))
        first = (0, 0, p.root.winfo_screenwidth(), p.root.winfo_screenheight())
        assert p.screens() == [place.Screen("screen 0", first, first, primary=True)]  # Tk's first screen
        assert p.said == {"screens"}
    finally:
        p.root.destroy()


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac window code")
def test_on_a_mac_with_tk_9_and_no_layers_there_is_no_pet(monkeypatch):
    import tkinter as tk
    from pet import mac
    monkeypatch.setattr(mac.macdraw, "Layers", lambda title, cell: (_ for _ in ()).throw(OSError("no layers")))
    monkeypatch.setattr(mac.tk, "TkVersion", 9.0)
    try:
        p = mac.PetApp(sprites.load(PET_DIR), 9, "b", {"enabled": True})
    except tk.TclError as e:
        pytest.skip(f"no screen to open a window on: {e}")
    assert p.run() == mac.UNSUPPORTED  # rather Notification Center than a black box with trails


# ---------------------------------------------------------------- petfeed

def test_settings_are_cleaned_and_only_known_keys_kept(tmp_path):
    path = tmp_path / "pet.json"
    assert petfeed.load_settings(path) == petfeed.DEFAULTS
    s = petfeed.save_settings(path, {"scale": 1.3, "muted": "yes", "x": 120.7, "y": True, "evil": 1,
                                     "pet": "../x"})
    assert s["scale"] == 1.25 and s["muted"] is False and s["x"] == 120 and s["y"] is None
    assert "evil" not in petfeed.load_settings(path) and s["pet"] == "whale-girl"
    assert petfeed.save_settings(path, {"enabled": False})["scale"] == 1.25  # the rest is kept
    path.write_text("not json", encoding="utf-8")
    assert petfeed.load_settings(path) == petfeed.DEFAULTS


def test_feed_keeps_recent_events_and_counts_busy_work(monkeypatch):
    f = petfeed.Feed()
    first = f.push("mail", title="a")
    f.push("mail", title="b")
    assert [e["title"] for e in f.since(0)] == ["a", "b"]
    assert [e["title"] for e in f.since(first)] == ["b"]
    f.events[0]["at"] -= petfeed.EVENT_TTL + 1
    assert [e["title"] for e in f.since(0)] == ["b"]
    assert f.current_busy() is None
    with f.working("thinking"):
        with f.working("sending"):
            assert f.current_busy() == "sending"
        assert f.current_busy() == "thinking"
    assert f.current_busy() is None
    assert not f.alive()
    f.polled()
    assert f.alive()


def test_keeper_starts_the_pet_with_this_app_s_boot_id(tmp_path):
    k = petfeed.Keeper(Path("/app"), tmp_path, 8765, tmp_path / "pet.json", print)
    cmd = k.command()
    assert cmd[-6:] == ["--port", "8765", "--data", str(tmp_path), "--boot", petfeed.BOOT]
    assert cmd[1:4] == ["-I", "-X", "utf8"]


# ---------------------------------------------------------------- the app's side

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "PET_SETTINGS", tmp_path / "pet.json")
    monkeypatch.setattr(app, "feed", petfeed.Feed())
    monkeypatch.setattr(app, "MACOS", False)
    return TestClient(app.starlette_app)


def test_a_notice_waits_while_the_pet_is_on_but_not_running(client, tmp_path, monkeypatch):
    assert asyncio.run(app.deliver_notice("t", "a", "", "INBOX", "1")) is False and app.feed.seq == 0
    import petfeed as pf
    pf.save_settings(app.PET_SETTINGS, {"enabled": False})  # switched off on purpose: nothing to wait for
    assert asyncio.run(app.deliver_notice("t", "a", "", "INBOX", "1")) is True


def test_a_new_mail_reaches_the_pet(client):
    app.feed.polled()
    asyncio.run(app.deliver_notice("新邮件 · Ana", "合作 <b>", "", "INBOX", "42"))
    d = client.get("/api/pet?after=0").json()
    assert d["boot"] == petfeed.BOOT and d["seq"] == 1 and d["settings"]["enabled"] is True
    (e,) = d["events"]
    assert e["link"].endswith("/#open=INBOX/42") and e["line1"] == "合作 <b>"
    assert client.get("/api/pet?after=1").json()["events"] == []
    assert client.get("/api/pet?after=junk").json()["seq"] == 1


def test_a_mac_without_a_running_pet_still_gets_notification_center(monkeypatch):
    monkeypatch.setattr(app, "MACOS", True)
    monkeypatch.setattr(app, "feed", petfeed.Feed())
    shown = []

    async def banner(*args):
        shown.append(args)
        return True

    monkeypatch.setattr(app, "show_mac_notification", banner)
    assert asyncio.run(app.deliver_notice("t", "a", "b", "INBOX", "1"))
    assert shown == [("t", "a", "b")] and app.feed.seq == 0


def test_pet_settings_from_the_page_and_the_demo(client):
    d = client.post("/api/pet/settings", json={"scale": 0.75, "muted": True, "unknown": 1}).json()
    assert d["scale"] == 0.75 and d["muted"] is True and "unknown" not in d and d["running"] is False
    assert client.get("/api/pet/settings").json()["scale"] == 0.75
    assert client.post("/api/pet/settings", json=["no"]).status_code == 400
    assert client.post("/api/pet/demo", json={}).json()["ok"] is True
    assert client.get("/api/pet").json()["events"][0]["title"].startswith("新邮件")


def test_new_mail_notices_never_show_a_windows_toast():
    assert not hasattr(app, "show_toast") and not hasattr(app, "TOAST_APP_ID")


# ---------------------------------------------------------------- publishing the pictures

def test_publish_lets_only_bare_pictures_through():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import publish

    def chunk(kind, body):
        import struct
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    head, end = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\0" * 13), chunk(b"IEND", b"")
    assert publish.png_problem(head + chunk(b"IDAT", b"x") + end) is None
    assert "tEXt" in publish.png_problem(head + chunk(b"tEXt", b"Author\0C:\\Users\\alex") + end)
    assert "eXIf" in publish.png_problem(head + chunk(b"eXIf", b"MM") + end)
    assert "后面" in publish.png_problem(head + end + b"PK\3\4 hidden zip")
    broken = bytearray(head + end)
    broken[-1] ^= 1
    assert "校验" in publish.png_problem(bytes(broken))
    assert publish.png_problem(b"GIF89a") == "不是 PNG"
    for picture in PET_DIR.glob("*.png"):
        assert publish.png_problem(picture.read_bytes()) is None, picture.name
