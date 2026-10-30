"""macOS: the pet's frames shown by Core Animation, so she is see-through with Tk 8.6 and Tk 9 alike.

Tk 9 draws a whole window into one bitmap that has no alpha channel (TKContentView's tkLayerBitmapContext, made with
kCGImageAlphaNoneSkipLast) and hands it to the view's layer. Whatever Tk draws is therefore opaque: a see-through
pixel comes out black, and a new frame never wipes the last one (systemTransparent paints nothing over it), which
leaves trails when she sways. Tk 8.6 had no such bitmap. So the Tk window keeps doing what Tk does well (the mouse,
the menu, the bubble), its own picture is hidden (its content view's layer at opacity 0; views take the mouse by
where they are, not by what they show), and the frames go to two layers in a view of ours right under it: the
clip's frame and the props on top. The window server blends them with their alpha, as UpdateLayeredWindow does on
Windows, and each frame replaces the last one whole. The whale girl's pictures hold two pixels a point (pet.json's
density): on a Retina screen each of her pixels is one of the screen's, not one stretched over four; on a plain screen
and at 小 or 大 Core Animation scales them on the graphics card.

Everything goes through the Objective-C runtime and Core Graphics with ctypes, as win.py does with GDI+: nothing to
install. No call here returns a struct (objc_msgSend would need its _stret twin on Intel Macs for that): a screen's
rectangle is read boxed in an NSValue (key-value coding) and copied out of it.

Tk's own "-topmost" does not last on her: each time Tk maps an overrideredirect toplevel it clears the flag
(ApplyContainerOverrideChanges in tkMacOSXWm.c, ApplyMasterOverrideChanges in older Tk 8.6), so she and her bubble
came out normal windows that any other window covered. Tk does put such windows on every desktop, but a normal app's
windows are kept off other apps' full-screen Spaces, where a swipe to a full-screen app lost her: only an accessory
app's are let on. keep_in_front() sets the level and the Spaces once Tk has mapped the window; mapping it again
(wm deiconify) would undo both."""

from __future__ import annotations

import ctypes
import os
from ctypes import c_bool, c_char_p, c_double, c_float, c_long, c_size_t, c_uint32, c_ulong, c_void_p
from pathlib import Path


class CGRect(ctypes.Structure):
    _fields_ = [("x", c_double), ("y", c_double), ("w", c_double), ("h", c_double)]


BGRA = 2 | (2 << 12)   # kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little: what Core Animation shows as is
NS_WINDOW_BELOW = -1   # NSWindowOrderingMode for addSubview:positioned:relativeTo:
NS_VIEW_SIZABLE = 2 | 16  # NSViewWidthSizable | NSViewHeightSizable
TOPMOST = 19           # kCGUtilityWindowLevel, the level Tk's -topmost sets: over every app's windows, under the Dock
                       # (her bubble goes one higher, the Dock's own level: over her head and never under the Dock)
# NSWindowCollectionBehavior CanJoinAllSpaces | Stationary | IgnoresCycle | FullScreenAuxiliary: on every desktop
# and beside other apps in full screen, left where she is by Mission Control and out of Cmd-` window cycling
EVERY_SPACE = 1 | 16 | 64 | 256
ACCESSORY = 1          # NSApplicationActivationPolicyAccessory: no Dock icon or menu bar of its own


def _sig(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, list(argtypes)


class _Runtime:
    """libobjc and Core Graphics, loaded on first use (the module itself imports anywhere, for the tests)."""

    def __init__(self):
        self.objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        # The classes used below live in these; Tk has loaded both already, this only makes sure.
        ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        ctypes.CDLL("/System/Library/Frameworks/QuartzCore.framework/QuartzCore")
        self.cg = cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        o = self.objc
        _sig(o.objc_getClass, c_void_p, c_char_p)
        _sig(o.sel_registerName, c_void_p, c_char_p)
        _sig(o.objc_autoreleasePoolPush, c_void_p)
        _sig(o.objc_autoreleasePoolPop, None, c_void_p)
        self.msg_send = ctypes.cast(o.objc_msgSend, c_void_p).value
        _sig(cg.CGDataProviderCreateWithFilename, c_void_p, c_char_p)
        _sig(cg.CGDataProviderRelease, None, c_void_p)
        _sig(cg.CGImageCreateWithPNGDataProvider, c_void_p, c_void_p, c_void_p, c_bool, ctypes.c_int)
        _sig(cg.CGImageGetWidth, c_size_t, c_void_p)
        _sig(cg.CGImageGetHeight, c_size_t, c_void_p)
        _sig(cg.CGImageCreateWithImageInRect, c_void_p, c_void_p, CGRect)
        _sig(cg.CGImageRelease, None, c_void_p)
        _sig(cg.CGColorSpaceCreateWithName, c_void_p, c_void_p)
        _sig(cg.CGBitmapContextCreate, c_void_p, c_void_p, c_size_t, c_size_t, c_size_t, c_size_t, c_void_p, c_uint32)
        _sig(cg.CGBitmapContextGetData, c_void_p, c_void_p)
        _sig(cg.CGBitmapContextGetBytesPerRow, c_size_t, c_void_p)
        _sig(cg.CGBitmapContextCreateImage, c_void_p, c_void_p)
        _sig(cg.CGContextDrawImage, None, c_void_p, CGRect, c_void_p)
        _sig(cg.CGContextRelease, None, c_void_p)
        self.srgb = cg.CGColorSpaceCreateWithName(c_void_p.in_dll(cg, "kCGColorSpaceSRGB"))  # kept for good
        self.protos: dict = {}
        self.sels: dict = {}
        self.classes: dict = {}


_rt: _Runtime | None = None


def rt() -> _Runtime:
    global _rt
    if _rt is None:
        _rt = _Runtime()
    return _rt


def cls(name: str) -> int:
    r = rt()
    if name not in r.classes:
        found = r.objc.objc_getClass(name.encode())
        if not found:
            raise OSError(f"no Objective-C class {name}")
        r.classes[name] = found
    return r.classes[name]


def send(obj, selector: str, *args, restype=c_void_p, argtypes: tuple = ()):
    """[obj selector:args...] with the method's exact C signature (arm64 passes variadic arguments differently, so
    objc_msgSend is always called through a prototype of its own)."""
    r = rt()
    fn = r.protos.get((restype, argtypes))
    if fn is None:
        fn = r.protos[(restype, argtypes)] = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)(r.msg_send)
    sel = r.sels.get(selector)
    if sel is None:
        sel = r.sels[selector] = r.objc.sel_registerName(selector.encode())
    return fn(obj, sel, *args)


class Pool:
    """An autorelease pool around a few calls, so objects AppKit hands back autoreleased are freed here, whatever
    runs the loop at that moment (the window search runs before Tk's main loop)."""

    def __enter__(self):
        self.pool = rt().objc.objc_autoreleasePoolPush()

    def __exit__(self, *exc):
        rt().objc.objc_autoreleasePoolPop(self.pool)


class Strip:
    """A picture of frames decoded once into a premultiplied bitmap, like win.py's Strip. A frame is a CGImage cut
    out of it that shares its pixels; the alpha channel is kept here to tell which pixels are see-through."""

    def __init__(self, path: Path, cell: tuple[int, int]):
        cg = rt().cg
        self.cell = cell
        self.frames: dict[tuple[int, int], int] = {}  # where the cell starts in the picture -> its CGImage
        provider = cg.CGDataProviderCreateWithFilename(os.fsencode(path))
        png = cg.CGImageCreateWithPNGDataProvider(provider, None, True, 0) if provider else None
        if provider:
            cg.CGDataProviderRelease(provider)
        if not png:
            raise OSError(f"{Path(path).name} could not be read")
        try:
            w, h = cg.CGImageGetWidth(png), cg.CGImageGetHeight(png)
            ctx = cg.CGBitmapContextCreate(None, w, h, 8, w * 4, rt().srgb, BGRA)
            if not ctx:
                raise OSError(f"no {w}x{h} bitmap for {Path(path).name}")
            try:
                cg.CGContextDrawImage(ctx, CGRect(0, 0, w, h), png)
                # rows top to bottom, B G R A each: every fourth byte from the fourth is the alpha
                stride = cg.CGBitmapContextGetBytesPerRow(ctx)
                data = ctypes.string_at(cg.CGBitmapContextGetData(ctx), stride * h)
                self.alpha_rows = b"".join(data[r * stride:r * stride + w * 4][3::4] for r in range(h))
                self.image = cg.CGBitmapContextCreateImage(ctx)  # its own copy of the pixels
            finally:
                cg.CGContextRelease(ctx)
        finally:
            cg.CGImageRelease(png)
        if not self.image:
            raise OSError(f"{Path(path).name} could not be decoded")
        self.size = (w, h)

    @property
    def nbytes(self) -> int:
        return self.size[0] * self.size[1] * 5  # the bitmap and its alpha channel

    def frame(self, x: int, y: int) -> int:
        """The cell whose top left corner is (x, y), as a CGImage (top-left based, as the pictures are)."""
        img = self.frames.get((x, y))
        if img is None:
            cw, ch = self.cell
            img = rt().cg.CGImageCreateWithImageInRect(self.image, CGRect(x, y, cw, ch))
            if not img:
                raise OSError(f"no cell at {x},{y}")
            self.frames[(x, y)] = img
        return img

    def alpha(self, x: int, y: int) -> int:
        w, h = self.size
        return self.alpha_rows[y * w + x] if 0 <= x < w and 0 <= y < h else 0

    def close(self):
        """Core Animation keeps its own reference to a frame it shows, so this is safe at any time."""
        cg = rt().cg
        for img in self.frames.values():
            cg.CGImageRelease(img)
        self.frames.clear()
        if self.image:
            cg.CGImageRelease(self.image)
            self.image = None


def find_window(title: str) -> int:
    """The NSWindow Tk made for the toplevel titled `title` (Tk sets the title once the window has been mapped)."""
    with Pool():
        windows = send(send(cls("NSApplication"), "sharedApplication"), "windows")
        for i in range(send(windows, "count", restype=c_ulong) if windows else 0):
            window = send(windows, "objectAtIndex:", i, argtypes=(c_ulong,))
            name = send(window, "title")
            if name and send(name, "UTF8String", restype=c_char_p) == title.encode("utf-8"):
                return window
    raise OSError(f"no window titled {title!r}")


def without_dock_icon() -> bool:
    """An accessory app, as a menu bar helper is: only such an app's windows are let onto another app's full-screen
    Space, and the pet's Python then shows no rocket in the Dock either. Tk makes the app a regular one when AppKit
    says it has finished launching, which comes whenever the event loop gets to it: so this is asked again later.
    True when it had to change the policy."""
    app = send(cls("NSApplication"), "sharedApplication")
    if send(app, "activationPolicy", restype=c_long) == ACCESSORY:
        return False
    return bool(send(app, "setActivationPolicy:", ACCESSORY, restype=c_bool, argtypes=(c_long,)))


def keep_in_front(window: int, level: int = TOPMOST):
    """Over every app's windows and on every Space, set after Tk has mapped the window (see the top)."""
    send(window, "setLevel:", level, restype=None, argtypes=(c_long,))
    send(window, "setCollectionBehavior:", EVERY_SPACE, restype=None, argtypes=(c_ulong,))
    send(window, "setHidesOnDeactivate:", False, restype=None, argtypes=(c_bool,))


def _rect(obj: int, key: bytes) -> CGRect:
    """obj.key for an NSRect property, through key-value coding: AppKit boxes it in an NSValue."""
    name = send(cls("NSString"), "stringWithUTF8String:", key, argtypes=(c_char_p,))
    value = send(obj, "valueForKey:", name, argtypes=(c_void_p,))
    if not value:
        raise OSError(f"no {key.decode()}")
    r = CGRect()
    send(value, "getValue:size:", ctypes.addressof(r), ctypes.sizeof(r), restype=None, argtypes=(c_void_p, c_size_t))
    return r


def screens() -> list[tuple[tuple[int, int, int, int], tuple[int, int, int, int]]]:
    """Each screen's rectangle and the part of it without the menu bar and the Dock, as left, top, right, bottom in
    Tk's coordinates: points from the top left of the first screen (the one with the menu bar), y downwards. AppKit
    counts from that screen's bottom left upwards. The first screen comes first."""
    rects = []
    with Pool():
        found = send(cls("NSScreen"), "screens")
        for i in range(send(found, "count", restype=c_ulong) if found else 0):
            screen = send(found, "objectAtIndex:", i, argtypes=(c_ulong,))
            rects.append((_rect(screen, b"frame"), _rect(screen, b"visibleFrame")))
    if not rects:
        return []
    height = rects[0][0].h
    return [(tk_rect(frame, height), tk_rect(visible, height)) for frame, visible in rects]


def tk_rect(r: CGRect, first_height: float) -> tuple[int, int, int, int]:
    """An AppKit rectangle as left, top, right, bottom in Tk's coordinates, flipped about the first screen's height
    as Tk itself does (TkMacOSXZeroScreenHeight)."""
    return round(r.x), round(first_height - r.y - r.h), round(r.x + r.w), round(first_height - r.y)


class Layers:
    """Tk's own picture of the window hidden, and in its place a view of ours with two layers: the frame of the
    clip (base) and the props over it (over)."""

    def __init__(self, title: str, size: tuple[int, int], density: int = 1):
        rect = CGRect(0, 0, *size)
        with Pool():
            self.window = find_window(title)
            content = send(self.window, "contentView")
            frame_view = send(content, "superview") if content else None
            if not frame_view:
                raise OSError("the pet window has no content view")
            send(frame_view, "setWantsLayer:", True, restype=None, argtypes=(c_bool,))  # Tk 9 has it already
            tk_layer = send(content, "layer")
            if not tk_layer:
                raise OSError("the pet window's content view has no layer")
            # owned (+1) and kept for the life of the process
            self.host = send(send(cls("CALayer"), "alloc"), "init")
            self.base = send(send(cls("CALayer"), "alloc"), "init")
            self.over = send(send(cls("CALayer"), "alloc"), "init")
            for layer in (self.base, self.over):  # the props last: on top
                send(layer, "setFrame:", rect, restype=None, argtypes=(CGRect,))
                send(layer, "setContentsScale:", float(density), restype=None, argtypes=(c_double,))
                send(self.host, "addSublayer:", layer, restype=None, argtypes=(c_void_p,))
            self.view = send(send(cls("NSView"), "alloc"), "initWithFrame:", rect, argtypes=(CGRect,))
            send(self.view, "setLayer:", self.host, restype=None, argtypes=(c_void_p,))  # layer first: we own its tree
            send(self.view, "setWantsLayer:", True, restype=None, argtypes=(c_bool,))
            send(self.view, "setAutoresizingMask:", NS_VIEW_SIZABLE, restype=None, argtypes=(c_ulong,))
            send(frame_view, "addSubview:positioned:relativeTo:", self.view, NS_WINDOW_BELOW, content,
                 restype=None, argtypes=(c_void_p, c_long, c_void_p))
            # Tk's picture out of sight. Tk 9's view is layer-backed, and AppKit sets such a layer's opacity from
            # the view's alphaValue whenever it syncs the two: both, or a move to another screen brings the box back.
            # Hit-testing ignores both, so Tk keeps the mouse.
            transaction = cls("CATransaction")
            send(transaction, "begin", restype=None)
            send(transaction, "setDisableActions:", True, restype=None, argtypes=(c_bool,))
            send(content, "setAlphaValue:", 0.0, restype=None, argtypes=(c_double,))
            send(tk_layer, "setOpacity:", 0.0, restype=None, argtypes=(c_float,))
            send(transaction, "commit", restype=None)
        self.ignoring: bool | None = None

    def show(self, base: int, over: int | None):
        """One frame, all at once. Layers fade a new picture in over a quarter of a second unless told not to,
        which would be trails of another kind."""
        transaction = cls("CATransaction")
        with Pool():
            send(transaction, "begin", restype=None)
            send(transaction, "setDisableActions:", True, restype=None, argtypes=(c_bool,))
            send(self.base, "setContents:", base, restype=None, argtypes=(c_void_p,))
            send(self.over, "setContents:", over, restype=None, argtypes=(c_void_p,))
            send(transaction, "commit", restype=None)

    def resize(self, size: tuple[int, int]):
        """小 中 大. The view follows the window by itself, by its autoresizing mask (were its frame set here too, it
        would grow twice over should Tk resize the window later); the layers are told, and a frame fills its layer."""
        transaction = cls("CATransaction")
        rect = CGRect(0, 0, *size)
        with Pool():
            send(transaction, "begin", restype=None)
            send(transaction, "setDisableActions:", True, restype=None, argtypes=(c_bool,))
            for layer in (self.base, self.over):
                send(layer, "setFrame:", rect, restype=None, argtypes=(CGRect,))
            send(transaction, "commit", restype=None)

    def click_through(self, ignore: bool):
        """Clicks on a see-through pixel go to whatever is under her; set from the pixel under the pointer."""
        if ignore != self.ignoring:
            self.ignoring = ignore
            send(self.window, "setIgnoresMouseEvents:", ignore, restype=None, argtypes=(c_bool,))

    @staticmethod
    def button_down() -> bool:
        """Whether a mouse button is held right now, whichever window got the press."""
        return bool(send(cls("NSEvent"), "pressedMouseButtons", restype=c_ulong))
