"""macOS: the pet in a borderless, transparent, always-on-top Tk window.

Tk takes the mouse, the menu and the bubble; the frames are shown by Core Animation layers under Tk's own picture
(macdraw.py), because Tk 9 cannot draw anything see-through on a Mac: a transparent pixel comes out black and the
last frame stays behind the new one. uv's and Homebrew's Pythons come with Tk 9, python.org's with Tk 8.6. Should the
layers not come up, Tk 8.6 still draws her itself (below); Tk 9 gives up (exit code 4) and the app keeps using
Notification Center, as when Tk is missing altogether and main.py's import fails.

On a Mac the pet always has its normal size. Which frame shows follows the clock, as on Windows, about 60 times a
second. Pictures hold five frames each: a tall one takes long to decode."""

from __future__ import annotations

import collections
import fcntl
import os
import time
import tkinter as tk
import webbrowser
from pathlib import Path

from . import macdraw
from .brain import Brain
from .client import Client, own_page
from .sprites import Sprites

CREAM, NAVY, INK, MUTED = "#FFFBF4", "#283070", "#1E2233", "#5A6075"
DRAG_THRESHOLD = 4
UNSUPPORTED = 4  # main.UNSUPPORTED: this Mac cannot show the pet, the app uses Notification Center
WINDOW_TITLE = f"KOL Mail pet {os.getpid()}"  # how macdraw finds Tk's window; never shown (no title bar)


RETRY_AFTER = 30.0  # seconds before a picture that could not be read is tried again


class Pictures:
    """The pictures used last, up to `budget` bytes in memory (Tk keeps 4 bytes a pixel; five frames of her are
    1.6 MB). The two newest always stay, as a frame may need a clip and the props on top of it. A picture that cannot
    be read is said once and tried again RETRY_AFTER later; until then asking for it fails at once (OSError).
    Tk photo images by default; macdraw strips with their own loader, size and release."""

    def __init__(self, budget: int, load=lambda path: tk.PhotoImage(file=str(path)),
                 weight=lambda img: img.width() * img.height() * 4, drop=lambda img: None):
        self.budget, self.load, self.weight, self.drop = budget, load, weight, drop
        self.items: collections.OrderedDict = collections.OrderedDict()  # path -> picture
        self.failed: dict = {}  # path -> when to try it again

    def get(self, path):
        img = self.items.get(path)
        if img is None:
            if time.monotonic() < self.failed.get(path, 0.0):
                raise OSError(f"{path.name} could not be read a moment ago")
            try:
                img = self.load(path)
            except (tk.TclError, OSError) as e:
                if path not in self.failed:
                    print("pet picture:", path.name, e, flush=True)
                self.failed[path] = time.monotonic() + RETRY_AFTER
                raise
            self.failed.pop(path, None)
            self.items[path] = img
            while len(self.items) > 2 and sum(self.weight(i) for i in self.items.values()) > self.budget:
                self.drop(self.items.popitem(last=False)[1])
        self.items.move_to_end(path)
        return img


def single_instance(data: Path, port: int) -> bool:
    handle = open(data / f"pet-{port}.lock", "w")  # kept open (and locked) for the life of the process
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    single_instance.handle = handle
    return True


class PetApp:
    def __init__(self, sprites: Sprites, port: int, boot: str, settings: dict):
        self.sprites, self.settings = sprites, dict(settings)
        self.brain = Brain(lambda s: sprites.get(s).loop_ms, boot)
        self.client = Client(port)
        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-transparent", True)
        root.config(bg="systemTransparent")
        self.layers: macdraw.Layers | None = None  # set in run(); None: Tk draws her (Tk 8.6 only)
        self.strips = Pictures(64 << 20, load=lambda path: macdraw.Strip(path, sprites.cell),
                               weight=lambda strip: strip.nbytes, drop=lambda strip: strip.close())
        self.shown_strips = ()  # (strip, x, y) of the frame and the props on screen: which pixels are see-through
        self.pictures = Pictures(64 << 20)
        self.part_maps = Pictures(16 << 20)
        cw, ch = sprites.cell
        self.image = tk.PhotoImage(width=cw, height=ch)
        self.label = tk.Label(root, image=self.image, bg="systemTransparent", bd=0, highlightthickness=0)
        self.label.pack()
        x, y = self.settings.get("x"), self.settings.get("y")
        if x is None or y is None or not (0 <= x < root.winfo_screenwidth() and 0 <= y < root.winfo_screenheight()):
            x, y = root.winfo_screenwidth() - cw - 40, root.winfo_screenheight() - ch - 90  # above the Dock
        self.pos = (int(x), int(y))
        root.geometry(f"{cw}x{ch}+{self.pos[0]}+{self.pos[1]}")
        self.bubble_win: tk.Toplevel | None = None
        self.bubble_key = None
        self.shown, self.frame, self.started = "", -1, 0.0
        self.shown_key = None  # the state and the one-off move it belongs to
        self.press = None
        self.press_part = ""  # what the press touched, as she looked right then (60 frames a second: a tail moves)
        self.dragging = False
        self.last_x = None
        self.label.bind("<ButtonPress-1>", self.on_press)
        self.label.bind("<B1-Motion>", self.on_motion)
        self.label.bind("<ButtonRelease-1>", self.on_release)
        self.label.bind("<Motion>", self.on_hover)
        for button in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.label.bind(button, self.on_menu)

    # ---------------------------------------------------------------- drawing

    def draw(self, force: bool = False):
        wanted = self.brain.state()
        name = self.sprites.resolve(wanted)
        key = (name, self.brain.oneshot)  # a new one-off, even the same reaction again, starts from its first frame
        if key != self.shown_key:
            self.shown_key, self.shown, self.started, self.frame = key, name, time.perf_counter(), -1
        st = self.sprites.states[name]
        once = bool(self.brain.oneshot) and self.brain.oneshot[0] == wanted
        step = st.frame_at((time.perf_counter() - self.started) * 1000, loop=not once)
        if step != self.frame or force:
            if self.layers:
                self.show_on_layers(st, step)
                return min(st.durations)
            cell = cw, ch = self.sprites.cell
            try:
                path, x, y = st.layer.at(st.base_frame(step), cell)
                base = self.pictures.get(path)
                over = None
                if st.overlay:
                    path, ox, oy = st.overlay.at(st.over_frame(step), cell)
                    over = self.pictures.get(path)
            except (tk.TclError, OSError):  # a picture that cannot be read right now: the last frame stays
                return min(st.durations)
            self.frame = step
            # "set": the clip's frame replaces everything, transparency included; then the props go over it
            self.image.tk.call(self.image, "copy", base, "-from", x, y, x + cw, y + ch, "-to", 0, 0,
                               "-compositingrule", "set")
            if over is not None:
                x0, y0, x1, y1 = st.overlay.box or (0, 0, cw, ch)
                if x1 > x0 and y1 > y0:
                    self.image.tk.call(self.image, "copy", over, "-from", ox + x0, oy + y0, ox + x1, oy + y1,
                                       "-to", x0, y0, "-compositingrule", "overlay")
        return min(st.durations)

    def show_on_layers(self, st, step: int):
        """The frame on the Core Animation layers: the clip's cell, and the props' cell over it (no box needed, the
        layers blend on the graphics card)."""
        cell = self.sprites.cell
        try:
            path, x, y = st.layer.at(st.base_frame(step), cell)
            shown = [(self.strips.get(path), x, y)]
            if st.overlay:
                path, ox, oy = st.overlay.at(st.over_frame(step), cell)
                shown.append((self.strips.get(path), ox, oy))
            images = [strip.frame(sx, sy) for strip, sx, sy in shown]
        except OSError:  # a picture that cannot be read right now: the last frame stays
            return
        self.layers.show(images[0], images[1] if len(images) > 1 else None)
        self.frame, self.shown_strips = step, tuple(shown)

    def solid_at(self, x: int, y: int) -> bool:
        """Whether the pixel (x, y) of the window shows any of her or her props right now."""
        return any(strip.alpha(sx + x, sy + y) for strip, sx, sy in self.shown_strips)

    def update_click_through(self):
        """With the layers, the window takes clicks only where she is: from the pixel under the pointer. Never in
        the middle of a press, so a drag that slips over a see-through pixel keeps going; a press whose release
        never came (Mission Control mid-drag) stops counting once no button is held."""
        if not self.layers or (self.press and self.layers.button_down()):
            return
        px, py = self.root.winfo_pointerxy()
        x, y = px - self.pos[0], py - self.pos[1]
        cw, ch = self.sprites.cell
        if 0 <= x < cw and 0 <= y < ch:
            self.layers.click_through(not self.solid_at(x, y))

    def tick(self):
        delay = 16
        try:
            delay = self.draw()
            self.update_click_through()
            self.update_bubble()
        except Exception as e:  # noqa: BLE001  without the next after() the pet would stand still for good
            print("pet tick error:", repr(e), flush=True)
        finally:
            self.root.after(max(8, min(int(delay / 2), 16)), self.tick)  # twice a frame: a late wake-up skips none

    def update_bubble(self):
        b = self.brain.bubble()
        key = (b.title, b.line1, b.line2, b.more, b.say) if b else None
        if key == self.bubble_key:
            return
        self.bubble_key = key
        if self.bubble_win:
            self.bubble_win.destroy()
            self.bubble_win = None
        if not b:
            return
        w = self.bubble_win = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.wm_attributes("-topmost", True)
        frame = tk.Frame(w, bg=CREAM, highlightbackground=NAVY, highlightthickness=2, padx=12, pady=9,
                         cursor="arrow" if b.say else "pointinghand")
        frame.pack()
        title = b.title + (f"（又来了 {b.more} 封）" if b.more else "")
        rows = ((title, NAVY, ("PingFang SC", 13, "bold" if not b.say else "normal")),
                (b.line1, INK, ("PingFang SC", 13)), (b.line2, MUTED, ("PingFang SC", 12)))
        for text, color, font in rows:
            if text:
                lbl = tk.Label(frame, text=text, fg=color, bg=CREAM, font=font, wraplength=280, justify="left",
                               anchor="w")
                lbl.pack(fill="x")
                lbl.bind("<ButtonRelease-1>", self.on_bubble)
        frame.bind("<ButtonRelease-1>", self.on_bubble)
        w.update_idletasks()
        self.place_bubble()

    def place_bubble(self):
        w = self.bubble_win
        if not w:
            return
        bw, bh = w.winfo_reqwidth(), w.winfo_reqheight()
        cw, ch = self.sprites.cell
        bx = max(0, min(self.pos[0] + cw // 2 - bw // 2, self.root.winfo_screenwidth() - bw))
        by = max(0, self.pos[1] + int(ch * 0.12) - bh)
        w.geometry(f"+{bx}+{by}")

    # ---------------------------------------------------------------- what a click touched

    def part_at(self, x: int, y: int) -> str:
        for path, px, py in self.sprites.part_lookups(self.shown, max(0, self.frame), x, y):
            try:
                red = int(self.part_maps.get(path).get(px, py)[0])
            except (tk.TclError, OSError, IndexError, ValueError, TypeError):
                continue
            if red:  # the props on top first: the letter wins over the hand holding it
                return self.sprites.part_name(red)
        return ""

    # ---------------------------------------------------------------- events

    def poll(self):
        while not self.client.answers.empty():
            answer = self.client.answers.get_nowait()
            if answer is None:
                self.brain.on_offline()
            else:
                self.brain.on_poll(answer)
                self.settings = self.brain.settings or self.settings
        if not self.client.is_alive() and not self.brain.quit_reason:  # it would never hear "leave" again
            self.brain.quit_reason = "the polling thread died"
        if self.brain.quit_reason:
            self.root.after(2500 if self.brain.quit_reason == "hidden from the menu" else 0, self.root.destroy)
            return
        self.root.after(250, self.poll)

    def open(self, url: str | None):
        if url and own_page(url, self.client.port):  # only ever the app's own page
            webbrowser.open(url)

    def on_hover(self, e):
        self.brain.on_hover()
        letter = not self.sprites.touchable or self.part_at(e.x, e.y) == "mail"
        self.label.config(cursor="pointinghand" if letter else "arrow")

    def on_press(self, e):
        self.press = (e.x_root, e.y_root, *self.pos)
        self.press_part = self.part_at(e.x, e.y) if self.sprites.touchable else ""
        self.dragging = False

    def on_motion(self, e):
        if not self.press:
            return
        dx, dy = e.x_root - self.press[0], e.y_root - self.press[1]
        if not self.dragging and max(abs(dx), abs(dy)) >= DRAG_THRESHOLD:
            self.dragging = True
        if self.dragging:
            self.brain.on_drag(e.x_root - (self.last_x if self.last_x is not None else e.x_root))
            self.last_x = e.x_root
            self.pos = (self.press[2] + dx, self.press[3] + dy)
            self.root.geometry(f"+{self.pos[0]}+{self.pos[1]}")
            self.place_bubble()  # the bubble follows the pet

    def on_release(self, e):
        if not self.press:
            return
        self.press, self.last_x = None, None
        if self.dragging:
            self.dragging = False
            self.brain.on_drop()
            self.client.save(x=self.pos[0], y=self.pos[1])
        elif not self.sprites.touchable:
            self.open(self.brain.on_click())
        else:
            self.open(self.brain.on_touch(self.press_part))
        self.update_bubble()

    def on_bubble(self, e):
        self.open(self.brain.on_bubble_click())
        self.update_bubble()

    def on_menu(self, e):
        if getattr(self, "menu", None):
            self.menu.destroy()
        m = self.menu = tk.Menu(self.root, tearoff=0)
        m.add_command(label="打开 KOL Mail", command=lambda: self.open(self.brain.url))
        muted = tk.BooleanVar(value=bool(self.settings.get("muted")))
        m.add_checkbutton(label="静音（来信只换动作，不弹气泡）", variable=muted, command=lambda: self.set_muted(muted.get()))
        m.add_separator()
        m.add_command(label="隐藏桌宠（在 KOL Mail 的设置里可以再打开）", command=self.hide)
        m.tk_popup(e.x_root, e.y_root)

    def set_muted(self, value: bool):
        self.settings["muted"] = value
        self.client.save(muted=value)
        if value:
            self.brain.dismiss_bubble()
            self.update_bubble()

    def hide(self):
        self.client.save(enabled=False)
        self.brain.quit_reason = "hidden from the menu"
        self.root.withdraw()
        if self.bubble_win:
            self.bubble_win.destroy()
            self.bubble_win = None

    def attach_layers(self) -> bool:
        """Map the window unseen (alpha 0: Tk 9 would flash a black box) and put the layers in it. Tk gives the
        NSWindow its title only once mapped, and the title is how macdraw finds it. False: no pet on this Mac."""
        self.root.wm_attributes("-alpha", 0.0)
        self.root.deiconify()
        self.root.update()
        self.root.title(WINDOW_TITLE)
        try:
            self.layers = macdraw.Layers(WINDOW_TITLE, self.sprites.cell)
        except Exception as e:  # noqa: BLE001  ctypes and AppKit fail in many ways; each one means "no layers"
            print("pet layers:", repr(e), "Tk", tk.TkVersion, flush=True)
            if tk.TkVersion >= 9:  # Tk 9 by itself shows a black box with trails: rather no pet at all
                return False
        return True

    def run(self) -> int:
        if not self.attach_layers():
            self.root.destroy()
            return UNSUPPORTED
        self.draw(force=True)
        self.root.wm_attributes("-alpha", 1.0)
        self.client.start()
        self.root.after(16, self.tick)
        self.root.after(250, self.poll)
        self.root.mainloop()
        self.client.stopped.set()
        return 0


def run(port: int, boot: str, settings: dict, sprites: Sprites) -> int:
    app = PetApp(sprites, port, boot, settings)
    code = app.run()
    print("pet stopped:", app.brain.quit_reason if code == 0 else "this Mac cannot show it", flush=True)
    return code
