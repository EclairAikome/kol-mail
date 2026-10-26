"""macOS: the pet in a borderless, transparent, always-on-top Tk window (Tk ships with Python from python.org).

Tk can only scale images by whole numbers, so on a Mac the pet always has its normal size. When Tk is missing (some
Homebrew Pythons), main.py's import fails, the pet exits with an error and the app keeps using Notification Center.
Which frame shows follows the clock, as on Windows; Tk redraws about 60 times a second. A state made of a shared clip
and props on top gets the clip's frame copied first, then the props over it, only where they can be (their box): Tk
copies about 3 ms for a whole frame. Pictures hold five frames each, as Tk decodes one in some 60 ms, but takes
seconds for a tall one."""

from __future__ import annotations

import collections
import fcntl
import time
import tkinter as tk
import webbrowser
from pathlib import Path

from .brain import Brain
from .client import Client, own_page
from .sprites import Sprites

CREAM, NAVY, INK, MUTED = "#FFFBF4", "#283070", "#1E2233", "#5A6075"
DRAG_THRESHOLD = 4


RETRY_AFTER = 30.0  # seconds before a picture that could not be read is tried again


class Pictures:
    """The pictures used last, up to `budget` bytes in memory (Tk keeps 4 bytes a pixel; five frames of her are
    1.6 MB). The two newest always stay, as a frame may need a clip and the props on top of it. A picture that cannot
    be read is said once and tried again RETRY_AFTER later; until then asking for it fails at once."""

    def __init__(self, budget: int):
        self.budget = budget
        self.items: collections.OrderedDict = collections.OrderedDict()  # path -> tk.PhotoImage
        self.failed: dict = {}  # path -> when to try it again

    def get(self, path) -> tk.PhotoImage:
        img = self.items.get(path)
        if img is None:
            if time.monotonic() < self.failed.get(path, 0.0):
                raise tk.TclError(f"{path.name} could not be read a moment ago")
            try:
                img = tk.PhotoImage(file=str(path))
            except tk.TclError as e:
                if path not in self.failed:
                    print("pet picture:", path.name, e, flush=True)
                self.failed[path] = time.monotonic() + RETRY_AFTER
                raise
            self.failed.pop(path, None)
            self.items[path] = img
            while len(self.items) > 2 and sum(i.width() * i.height() * 4 for i in self.items.values()) > self.budget:
                self.items.popitem(last=False)
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
            cell = cw, ch = self.sprites.cell
            try:
                path, x, y = st.layer.at(st.base_frame(step), cell)
                base = self.pictures.get(path)
                over = None
                if st.overlay:
                    path, ox, oy = st.overlay.at(st.over_frame(step), cell)
                    over = self.pictures.get(path)
            except tk.TclError:  # a picture that cannot be read right now: the last frame stays
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

    def tick(self):
        delay = 16
        try:
            delay = self.draw()
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
            except (tk.TclError, IndexError, ValueError, TypeError):
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

    def run(self):
        self.draw(force=True)
        self.root.deiconify()
        self.client.start()
        self.root.after(16, self.tick)
        self.root.after(250, self.poll)
        self.root.mainloop()
        self.client.stopped.set()


def run(port: int, boot: str, settings: dict, sprites: Sprites) -> int:
    app = PetApp(sprites, port, boot, settings)
    app.run()
    print("pet stopped:", app.brain.quit_reason, flush=True)
    return 0
