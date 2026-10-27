"""Windows: the pet as a per-pixel transparent, always-on-top window, drawn with GDI+ through ctypes. Both ship with
every Windows, so the pet adds no package and no executable. Transparent pixels let clicks through to the desktop.

One window shows the pet, a second one the speech bubble. A background thread (client.py) polls the app and wakes
the window with WM_APP; another one beats 60 times a second (WM_TICK) while she moves. Everything else happens on the
window thread.

Playing 60 frames a second cheaply: a picture of frames is decoded once into one premultiplied bitmap (Strip), at
the size she is shown, and UpdateLayeredWindow shows a frame straight out of it, no copy, no scaling. A state made of
a shared clip and props on top first gets the props blended over the frame (BitBlt + AlphaBlend, a fraction of a
millisecond). The pet window is DPI-unaware, so on a
150 % screen the compositor (on the graphics card) does the enlarging; only the bubble is DPI-aware, for sharp text.
Which frame shows follows the clock, so a late beat never makes her speed up or slow down."""

from __future__ import annotations

import collections
import contextlib
import ctypes
import ctypes.wintypes as W
import os
import threading
import time

from . import brain as brain_mod
from .client import Client, own_page
from .sprites import Sprites

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdiplus = ctypes.WinDLL("gdiplus")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x8, 0x80, 0x08000000
WM_DESTROY, WM_TIMER, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE = 0x0002, 0x0113, 0x0201, 0x0202, 0x0200
WM_RBUTTONUP, WM_SETCURSOR, WM_COMMAND, WM_APP, WM_DISPLAYCHANGE, WM_DPICHANGED = 0x0205, 0x0020, 0x0111, 0x8000, 0x007E, 0x02E0
WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x0021, 3
MK_LBUTTON = 0x0001
QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE = 2, 3, 4
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 2, 0, 1
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
HWND_TOPMOST = W.HWND(-1)
MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_POPUP = 0x0, 0x800, 0x8, 0x10
TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x100, 0x2
IDC_HAND, IDC_ARROW = 32649, 32512
MONITOR_DEFAULTTONEAREST, MONITOR_DEFAULTTONULL = 2, 0
ERROR_ALREADY_EXISTS = 183
TIMER_CHECK, TIMER_QUIT = 2, 3
WM_TICK = 0x8001  # WM_APP + 1: the animation beat
DRAG_THRESHOLD = 4

PixelFormat32bppPARGB = 0x000E200B
UnitPixel = 2


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE),
                ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR),
                ("hIconSm", W.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", W.BOOL), ("SuppressExternalCodecs", W.BOOL)]


class RectF(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float), ("Width", ctypes.c_float), ("Height", ctypes.c_float)]


class GpRect(ctypes.Structure):
    _fields_ = [("X", ctypes.c_int), ("Y", ctypes.c_int), ("Width", ctypes.c_int), ("Height", ctypes.c_int)]


class BitmapData(ctypes.Structure):
    _fields_ = [("Width", W.UINT), ("Height", W.UINT), ("Stride", ctypes.c_int), ("PixelFormat", ctypes.c_int),
                ("Scan0", ctypes.c_void_p), ("Reserved", ctypes.c_size_t)]


def _sig(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, list(argtypes)
    return f


VP = ctypes.c_void_p
PVP = ctypes.POINTER(VP)
DefWindowProcW = _sig(user32, "DefWindowProcW", LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
RegisterClassExW = _sig(user32, "RegisterClassExW", W.ATOM, ctypes.POINTER(WNDCLASSEXW))
CreateWindowExW = _sig(user32, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, VP)
DestroyWindow = _sig(user32, "DestroyWindow", W.BOOL, W.HWND)
ShowWindow = _sig(user32, "ShowWindow", W.BOOL, W.HWND, ctypes.c_int)
UpdateLayeredWindow = _sig(user32, "UpdateLayeredWindow", W.BOOL, W.HWND, W.HDC, ctypes.POINTER(W.POINT),
                           ctypes.POINTER(W.SIZE), W.HDC, ctypes.POINTER(W.POINT), W.COLORREF,
                           ctypes.POINTER(BLENDFUNCTION), W.DWORD)
SetWindowPos = _sig(user32, "SetWindowPos", W.BOOL, W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, W.UINT)
GetWindowRect = _sig(user32, "GetWindowRect", W.BOOL, W.HWND, ctypes.POINTER(W.RECT))
GetDC = _sig(user32, "GetDC", W.HDC, W.HWND)
ReleaseDC = _sig(user32, "ReleaseDC", ctypes.c_int, W.HWND, W.HDC)
SetTimer = _sig(user32, "SetTimer", ctypes.c_size_t, W.HWND, ctypes.c_size_t, W.UINT, VP)
GetMessageW = _sig(user32, "GetMessageW", W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
TranslateMessage = _sig(user32, "TranslateMessage", W.BOOL, ctypes.POINTER(W.MSG))
DispatchMessageW = _sig(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(W.MSG))
PostMessageW = _sig(user32, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
PostQuitMessage = _sig(user32, "PostQuitMessage", None, ctypes.c_int)
SetCapture = _sig(user32, "SetCapture", W.HWND, W.HWND)
ReleaseCapture = _sig(user32, "ReleaseCapture", W.BOOL)
GetCursorPos = _sig(user32, "GetCursorPos", W.BOOL, ctypes.POINTER(W.POINT))
LoadCursorW = _sig(user32, "LoadCursorW", W.HANDLE, W.HINSTANCE, VP)
SetCursor = _sig(user32, "SetCursor", W.HANDLE, W.HANDLE)
CreatePopupMenu = _sig(user32, "CreatePopupMenu", W.HMENU)
AppendMenuW = _sig(user32, "AppendMenuW", W.BOOL, W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR)
TrackPopupMenu = _sig(user32, "TrackPopupMenu", W.BOOL, W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      W.HWND, VP)
DestroyMenu = _sig(user32, "DestroyMenu", W.BOOL, W.HMENU)
SetForegroundWindow = _sig(user32, "SetForegroundWindow", W.BOOL, W.HWND)
SHQueryUserNotificationState = _sig(ctypes.WinDLL("shell32"), "SHQueryUserNotificationState", ctypes.c_long,
                                    ctypes.POINTER(ctypes.c_int))
MonitorFromPoint = _sig(user32, "MonitorFromPoint", W.HMONITOR, W.POINT, W.DWORD)
GetMonitorInfoW = _sig(user32, "GetMonitorInfoW", W.BOOL, W.HMONITOR, ctypes.POINTER(MONITORINFO))
CreateCompatibleDC = _sig(gdi32, "CreateCompatibleDC", W.HDC, W.HDC)
DeleteDC = _sig(gdi32, "DeleteDC", W.BOOL, W.HDC)
CreateDIBSection = _sig(gdi32, "CreateDIBSection", W.HBITMAP, W.HDC, ctypes.POINTER(BITMAPINFOHEADER), W.UINT, PVP,
                        W.HANDLE, W.DWORD)
SelectObject = _sig(gdi32, "SelectObject", W.HGDIOBJ, W.HDC, W.HGDIOBJ)
DeleteObject = _sig(gdi32, "DeleteObject", W.BOOL, W.HGDIOBJ)
BitBlt = _sig(gdi32, "BitBlt", W.BOOL, W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC,
              ctypes.c_int, ctypes.c_int, W.DWORD)
GdiAlphaBlend = _sig(gdi32, "GdiAlphaBlend", W.BOOL, W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, BLENDFUNCTION)
GdiFlush = _sig(gdi32, "GdiFlush", W.BOOL)
SRCCOPY = 0x00CC0020
GetDeviceCaps = _sig(gdi32, "GetDeviceCaps", ctypes.c_int, W.HDC, ctypes.c_int)
CreateMutexW = _sig(kernel32, "CreateMutexW", W.HANDLE, VP, W.BOOL, W.LPCWSTR)
GetModuleHandleW = _sig(kernel32, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
try:
    GetDpiForWindow = _sig(user32, "GetDpiForWindow", W.UINT, W.HWND)
except AttributeError:  # before Windows 10 1607
    GetDpiForWindow = None

GdiplusStartup = _sig(gdiplus, "GdiplusStartup", ctypes.c_int, ctypes.POINTER(ctypes.c_size_t),
                      ctypes.POINTER(GdiplusStartupInput), VP)
GdipCreateBitmapFromStream = _sig(gdiplus, "GdipCreateBitmapFromStream", ctypes.c_int, VP, PVP)
SHCreateMemStream = _sig(ctypes.WinDLL("shlwapi"), "SHCreateMemStream", VP, ctypes.c_char_p, W.UINT)
GdipBitmapLockBits = _sig(gdiplus, "GdipBitmapLockBits", ctypes.c_int, VP, ctypes.POINTER(GpRect), W.UINT, ctypes.c_int,
                          ctypes.POINTER(BitmapData))
GdipBitmapUnlockBits = _sig(gdiplus, "GdipBitmapUnlockBits", ctypes.c_int, VP, ctypes.POINTER(BitmapData))
GdipBitmapGetPixel = _sig(gdiplus, "GdipBitmapGetPixel", ctypes.c_int, VP, ctypes.c_int, ctypes.c_int,
                          ctypes.POINTER(ctypes.c_uint32))
GdipGetImageHeight = _sig(gdiplus, "GdipGetImageHeight", ctypes.c_int, VP, ctypes.POINTER(W.UINT))
IStreamRelease = ctypes.WINFUNCTYPE(ctypes.c_ulong, VP)  # IUnknown::Release, slot 2 of the vtable
try:  # Windows 10 1607+: one window of the process DPI-unaware, another per-monitor aware
    SetThreadDpiAwarenessContext = _sig(user32, "SetThreadDpiAwarenessContext", VP, VP)
except AttributeError:
    SetThreadDpiAwarenessContext = None
DPI_UNAWARE, DPI_PER_MONITOR_V2 = VP(-1), VP(-4)
ImageLockModeRead = 1
GdipGetImageWidth = _sig(gdiplus, "GdipGetImageWidth", ctypes.c_int, VP, ctypes.POINTER(W.UINT))
GdipCreateBitmapFromScan0 = _sig(gdiplus, "GdipCreateBitmapFromScan0", ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, VP, PVP)
GdipGetImageGraphicsContext = _sig(gdiplus, "GdipGetImageGraphicsContext", ctypes.c_int, VP, PVP)
GdipDeleteGraphics = _sig(gdiplus, "GdipDeleteGraphics", ctypes.c_int, VP)
GdipDisposeImage = _sig(gdiplus, "GdipDisposeImage", ctypes.c_int, VP)
GdipGraphicsClear = _sig(gdiplus, "GdipGraphicsClear", ctypes.c_int, VP, ctypes.c_uint32)
GdipSetInterpolationMode = _sig(gdiplus, "GdipSetInterpolationMode", ctypes.c_int, VP, ctypes.c_int)
GdipSetPixelOffsetMode = _sig(gdiplus, "GdipSetPixelOffsetMode", ctypes.c_int, VP, ctypes.c_int)
GdipSetSmoothingMode = _sig(gdiplus, "GdipSetSmoothingMode", ctypes.c_int, VP, ctypes.c_int)
GdipSetTextRenderingHint = _sig(gdiplus, "GdipSetTextRenderingHint", ctypes.c_int, VP, ctypes.c_int)
GdipDrawImageRectRectI = _sig(gdiplus, "GdipDrawImageRectRectI", ctypes.c_int, VP, VP, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, VP, VP, VP)
GdipFlush = _sig(gdiplus, "GdipFlush", ctypes.c_int, VP, ctypes.c_int)
GdipSetCompositingMode = _sig(gdiplus, "GdipSetCompositingMode", ctypes.c_int, VP, ctypes.c_int)
GdipCloneBitmapAreaI = _sig(gdiplus, "GdipCloneBitmapAreaI", ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, VP, PVP)
GdipCreateImageAttributes = _sig(gdiplus, "GdipCreateImageAttributes", ctypes.c_int, PVP)
GdipSetImageAttributesWrapMode = _sig(gdiplus, "GdipSetImageAttributesWrapMode", ctypes.c_int, VP, ctypes.c_int,
                                      ctypes.c_uint32, W.BOOL)
GdipDisposeImageAttributes = _sig(gdiplus, "GdipDisposeImageAttributes", ctypes.c_int, VP)
GdipCreateFontFamilyFromName = _sig(gdiplus, "GdipCreateFontFamilyFromName", ctypes.c_int, W.LPCWSTR, VP, PVP)
GdipCreateFont = _sig(gdiplus, "GdipCreateFont", ctypes.c_int, VP, ctypes.c_float, ctypes.c_int, ctypes.c_int, PVP)
GdipDeleteFont = _sig(gdiplus, "GdipDeleteFont", ctypes.c_int, VP)
GdipDeleteFontFamily = _sig(gdiplus, "GdipDeleteFontFamily", ctypes.c_int, VP)
GdipCreateSolidFill = _sig(gdiplus, "GdipCreateSolidFill", ctypes.c_int, ctypes.c_uint32, PVP)
GdipDeleteBrush = _sig(gdiplus, "GdipDeleteBrush", ctypes.c_int, VP)
GdipCreatePen1 = _sig(gdiplus, "GdipCreatePen1", ctypes.c_int, ctypes.c_uint32, ctypes.c_float, ctypes.c_int, PVP)
GdipDeletePen = _sig(gdiplus, "GdipDeletePen", ctypes.c_int, VP)
GdipCreateStringFormat = _sig(gdiplus, "GdipCreateStringFormat", ctypes.c_int, ctypes.c_int, ctypes.c_ushort, PVP)
GdipSetStringFormatTrimming = _sig(gdiplus, "GdipSetStringFormatTrimming", ctypes.c_int, VP, ctypes.c_int)
GdipDeleteStringFormat = _sig(gdiplus, "GdipDeleteStringFormat", ctypes.c_int, VP)
GdipDrawString = _sig(gdiplus, "GdipDrawString", ctypes.c_int, VP, W.LPCWSTR, ctypes.c_int, VP,
                      ctypes.POINTER(RectF), VP, VP)
GdipMeasureString = _sig(gdiplus, "GdipMeasureString", ctypes.c_int, VP, W.LPCWSTR, ctypes.c_int, VP,
                         ctypes.POINTER(RectF), VP, ctypes.POINTER(RectF), VP, VP)
GdipCreatePath = _sig(gdiplus, "GdipCreatePath", ctypes.c_int, ctypes.c_int, PVP)
GdipDeletePath = _sig(gdiplus, "GdipDeletePath", ctypes.c_int, VP)
GdipAddPathArc = _sig(gdiplus, "GdipAddPathArc", ctypes.c_int, VP, ctypes.c_float, ctypes.c_float, ctypes.c_float,
                      ctypes.c_float, ctypes.c_float, ctypes.c_float)
GdipAddPathLine = _sig(gdiplus, "GdipAddPathLine", ctypes.c_int, VP, ctypes.c_float, ctypes.c_float, ctypes.c_float,
                       ctypes.c_float)
GdipClosePathFigure = _sig(gdiplus, "GdipClosePathFigure", ctypes.c_int, VP)
GdipFillPath = _sig(gdiplus, "GdipFillPath", ctypes.c_int, VP, VP, VP)
GdipDrawPath = _sig(gdiplus, "GdipDrawPath", ctypes.c_int, VP, VP, VP)


def check(status: int, what: str):
    if status != 0:
        raise OSError(f"GDI+ {what} failed with status {status}")


class Canvas:
    """A 32-bit premultiplied DIB that GDI+ draws into and UpdateLayeredWindow shows."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.dc = self.hbm = self.old = self.bitmap = self.g = None
        try:
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down
            self.bits = VP()
            self.dc = CreateCompatibleDC(None)
            self.hbm = CreateDIBSection(self.dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
            if not self.dc or not self.hbm:
                raise OSError("CreateDIBSection failed (out of memory?)")
            self.old = SelectObject(self.dc, self.hbm)
            bitmap, g = VP(), VP()
            check(GdipCreateBitmapFromScan0(w, h, w * 4, PixelFormat32bppPARGB, self.bits, ctypes.byref(bitmap)),
                  "CreateBitmapFromScan0")
            self.bitmap = bitmap
            check(GdipGetImageGraphicsContext(self.bitmap, ctypes.byref(g)), "GetImageGraphicsContext")
            self.g = g
        except Exception:
            self.close()
            raise
        GdipSetInterpolationMode(self.g, 7)   # high quality bicubic
        GdipSetPixelOffsetMode(self.g, 4)     # half pixel: no drift when scaling
        GdipSetSmoothingMode(self.g, 4)       # anti-aliased shapes
        GdipSetTextRenderingHint(self.g, 4)   # anti-aliased text (ClearType needs an opaque background)

    def show(self, hwnd, x: int, y: int):
        GdipFlush(self.g, 1)
        screen = GetDC(None)
        UpdateLayeredWindow(hwnd, screen, ctypes.byref(W.POINT(x, y)), ctypes.byref(W.SIZE(self.w, self.h)), self.dc,
                            ctypes.byref(W.POINT(0, 0)), 0, ctypes.byref(BLEND), ULW_ALPHA)
        ReleaseDC(None, screen)

    def close(self):
        """Also after a failed __init__: whatever was made by then."""
        if self.g:
            GdipDeleteGraphics(self.g)
        if self.bitmap:
            GdipDisposeImage(self.bitmap)
        if self.old:
            SelectObject(self.dc, self.old)
        if self.hbm:
            DeleteObject(self.hbm)
        if self.dc:
            DeleteDC(self.dc)
        self.dc = self.hbm = self.old = self.bitmap = self.g = None


BLEND = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)  # premultiplied pixels over what is behind
STRIP_BUDGET = 64 << 20  # bytes of decoded pictures kept at the normal size (more at 大)
RETRY_AFTER = 30.0       # seconds before a picture that could not be read is tried again


def argb(a: int, r: int, g: int, b: int) -> int:
    return (a << 24) | (r << 16) | (g << 8) | b


class Fonts:
    def __init__(self, px: float):
        self.family = VP()
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            if GdipCreateFontFamilyFromName(name, None, ctypes.byref(self.family)) == 0:
                break
        self.title, self.body, self.small = VP(), VP(), VP()
        check(GdipCreateFont(self.family, px * 1.0, 1, UnitPixel, ctypes.byref(self.title)), "CreateFont")  # bold
        check(GdipCreateFont(self.family, px * 1.05, 0, UnitPixel, ctypes.byref(self.body)), "CreateFont")
        check(GdipCreateFont(self.family, px * 0.9, 0, UnitPixel, ctypes.byref(self.small)), "CreateFont")
        self.fmt = VP()
        GdipCreateStringFormat(0, 0, ctypes.byref(self.fmt))
        GdipSetStringFormatTrimming(self.fmt, 3)  # ellipsis at the end of what fits

    def close(self):
        for f in (self.title, self.body, self.small):
            GdipDeleteFont(f)
        GdipDeleteFontFamily(self.family)
        GdipDeleteStringFormat(self.fmt)


@contextlib.contextmanager
def dpi_context(ctx):
    """Calls inside work in this DPI context (the bubble is per-monitor aware, the pet is not)."""
    old = SetThreadDpiAwarenessContext(ctx) if SetThreadDpiAwarenessContext else None
    try:
        yield
    finally:
        if old:
            SetThreadDpiAwarenessContext(old)


def load_bitmap(path) -> tuple[VP, int]:
    """A GDI+ bitmap from a PNG and the stream it reads from (released by the caller once the bitmap is gone). From
    memory, not from the file: GDI+ keeps a file it loaded open, and the updater must be able to replace it."""
    data = path.read_bytes()
    stream = SHCreateMemStream(data, len(data))  # copies the bytes
    if not stream:
        raise OSError("SHCreateMemStream failed")
    bitmap = VP()
    status = GdipCreateBitmapFromStream(stream, ctypes.byref(bitmap))
    if status != 0:
        release(stream)
        check(status, f"load {path.name}")
    return bitmap, stream


def release(stream):
    vtable = ctypes.cast(ctypes.cast(stream, ctypes.POINTER(VP))[0], ctypes.POINTER(VP))
    IStreamRelease(vtable[2])(stream)


class Strip:
    """A picture of frames decoded once into a premultiplied DIB, at the size the window shows them (at 小 and 大 each
    frame is scaled once, here): showing a frame is handing UpdateLayeredWindow a rectangle of it, never a copy."""

    def __init__(self, path, cell: tuple[int, int], size: tuple[int, int]):
        self.dc = self.hbm = self.old = None
        bitmap, stream = load_bitmap(path)
        try:
            w, h = W.UINT(), W.UINT()
            GdipGetImageWidth(bitmap, ctypes.byref(w))
            GdipGetImageHeight(bitmap, ctypes.byref(h))
            if size == cell:
                self._make(w.value, h.value)
                self._copy(bitmap)
            else:
                cols, rows = max(1, w.value // cell[0]), max(1, h.value // cell[1])
                self._make(cols * size[0], rows * size[1])
                self._scale(bitmap, cell, size, cols, rows)
        except Exception:
            self.close()
            raise
        finally:
            GdipDisposeImage(bitmap)
            release(stream)

    def _make(self, w: int, h: int):
        self.w, self.h, self.nbytes = w, h, w * h * 4
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        self.bits = VP()
        self.dc = CreateCompatibleDC(None)
        self.hbm = CreateDIBSection(self.dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        if not self.dc or not self.hbm:
            raise OSError("CreateDIBSection failed (out of memory?)")
        self.old = SelectObject(self.dc, self.hbm)

    def _copy(self, bitmap):
        data = BitmapData()
        check(GdipBitmapLockBits(bitmap, ctypes.byref(GpRect(0, 0, self.w, self.h)), ImageLockModeRead,
                                 PixelFormat32bppPARGB, ctypes.byref(data)), "lock bits")
        try:
            row = self.w * 4
            if data.Stride == row:  # always so for 32-bit pixels: one copy
                ctypes.memmove(self.bits, data.Scan0, row * self.h)
            else:
                for y in range(self.h):
                    ctypes.memmove(self.bits.value + y * row, data.Scan0 + y * data.Stride, row)
        finally:
            GdipBitmapUnlockBits(bitmap, ctypes.byref(data))

    def _scale(self, bitmap, cell, size, cols: int, rows: int):
        """Each frame on its own, so no colour of the neighbouring frame bleeds in at the edges."""
        (cw, ch), (sw, sh) = cell, size
        dst, g, attrs = VP(), VP(), VP()
        check(GdipCreateBitmapFromScan0(self.w, self.h, self.w * 4, PixelFormat32bppPARGB, self.bits,
                                        ctypes.byref(dst)), "wrap strip")
        try:
            check(GdipGetImageGraphicsContext(dst, ctypes.byref(g)), "GetImageGraphicsContext")
            GdipSetInterpolationMode(g, 7)  # high quality bicubic
            GdipSetPixelOffsetMode(g, 4)    # half pixel: no drift
            GdipSetCompositingMode(g, 1)    # source copy: the pixels as they are, alpha included
            check(GdipCreateImageAttributes(ctypes.byref(attrs)), "image attributes")
            GdipSetImageAttributesWrapMode(attrs, 3, 0, False)  # mirrored beyond the frame: no dark fringe
            for gy in range(rows):
                for gx in range(cols):
                    one = VP()
                    check(GdipCloneBitmapAreaI(gx * cw, gy * ch, cw, ch, PixelFormat32bppPARGB, bitmap,
                                               ctypes.byref(one)), "clone a frame")
                    try:
                        check(GdipDrawImageRectRectI(g, one, gx * sw, gy * sh, sw, sh, 0, 0, cw, ch, UnitPixel, attrs,
                                                     None, None), "scale a frame")
                    finally:
                        GdipDisposeImage(one)
            GdipFlush(g, 1)
        finally:
            if attrs:
                GdipDisposeImageAttributes(attrs)
            if g:
                GdipDeleteGraphics(g)
            GdipDisposeImage(dst)

    def close(self):
        """Also after a failed __init__: whatever was made by then."""
        if self.old:
            SelectObject(self.dc, self.old)
        if self.hbm:
            DeleteObject(self.hbm)
        if self.dc:
            DeleteDC(self.dc)
        self.dc = self.hbm = self.old = None


class PartMap:
    """A picture's part map: which body part a pixel is (its red channel / 40)."""

    def __init__(self, path):
        self.bitmap, stream = load_bitmap(path)
        self.stream = stream
        w, h = W.UINT(), W.UINT()
        GdipGetImageWidth(self.bitmap, ctypes.byref(w))
        GdipGetImageHeight(self.bitmap, ctypes.byref(h))
        self.nbytes = w.value * h.value * 4  # once GDI+ has decoded it

    def red(self, x: int, y: int) -> int:
        argb = ctypes.c_uint32()
        if GdipBitmapGetPixel(self.bitmap, x, y, ctypes.byref(argb)) != 0:
            return 0
        return (argb.value >> 16) & 0xFF

    def close(self):
        GdipDisposeImage(self.bitmap)
        release(self.stream)


class Cache:
    """The pictures used last, up to `budget` bytes once decoded (five frames of her are 1.6 MB): the oldest goes when
    a new one comes. The two newest always stay, as a frame may need a clip and the props on top of it. A picture that
    cannot be read (gone, locked by a virus scanner for a moment) is said once and tried again RETRY_AFTER later; until
    then asking for it fails at once, so the window just keeps its last frame."""

    def __init__(self, factory, budget: int):
        self.factory, self.budget = factory, budget
        self.items: collections.OrderedDict = collections.OrderedDict()
        self.failed: dict = {}  # path -> when to try it again

    def get(self, path):
        item = self.items.get(path)
        if item is None:
            if time.monotonic() < self.failed.get(path, 0.0):
                raise OSError(f"{path.name} could not be read a moment ago")
            try:
                item = self.factory(path)
            except OSError as e:
                if path not in self.failed:
                    print("pet picture:", path.name, repr(e), flush=True)
                self.failed[path] = time.monotonic() + RETRY_AFTER
                raise
            self.failed.pop(path, None)
            self.items[path] = item
            while len(self.items) > 2 and sum(i.nbytes for i in self.items.values()) > self.budget:
                self.items.popitem(last=False)[1].close()
        self.items.move_to_end(path)
        return item

    def close(self):
        for item in self.items.values():
            item.close()
        self.items.clear()


class PetWindow:
    def __init__(self, sprites: Sprites, port: int, boot: str, settings_hint: dict):
        self.sprites = sprites
        self.client = Client(port, wake=lambda: PostMessageW(self.hwnd, WM_APP, 0, 0))
        self.brain = brain_mod.Brain(lambda s: sprites.get(s).loop_ms, boot)
        self.settings = dict(settings_hint)
        self.hwnd = None
        self.bubble_hwnd = None
        self.strips = Cache(lambda path: Strip(path, sprites.cell, self.size), budget=STRIP_BUDGET)
        self.part_maps = Cache(PartMap, budget=16 << 20)
        self.mix: Canvas | None = None  # a clip's frame with the props on top
        self.bubble_canvas: Canvas | None = None
        self.bubble_key = None
        self.shown_state, self.frame, self.started = "", -1, 0.0
        self.shown_key = None  # the state and the one-off move it belongs to
        self.pos = (0, 0)
        self.size = (1, 1)
        self.scale = 1.0
        self.press: tuple[int, int, int, int] | None = None  # cursor and window position at mouse down
        self.press_part = ""  # what the press touched, as she looked right then (60 frames a second: a tail moves)
        self.dragging = False
        self.last_x: int | None = None  # cursor x at the previous move of a drag, for its direction
        self.hidden_for_fullscreen = False
        self.tick_hz = 60.0
        self._proc = WNDPROC(self._wndproc)  # kept referenced: Windows calls it for the life of the window
        self._bubble_proc = WNDPROC(self._bubble_wndproc)

    # ---------------------------------------------------------------- setup

    def create(self):
        token = ctypes.c_size_t()
        check(GdiplusStartup(ctypes.byref(token), ctypes.byref(GdiplusStartupInput(1, None, False, False)), None),
              "Startup")
        hinst = GetModuleHandleW(None)
        arrow = LoadCursorW(None, VP(IDC_ARROW))
        for name, proc in (("KOLMailPet", self._proc), ("KOLMailPetBubble", self._bubble_proc)):
            wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, proc, 0, 0, hinst, None, arrow, None, None, name, None)
            if not RegisterClassExW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        with dpi_context(DPI_UNAWARE):  # the compositor scales her: no per-frame enlarging on the CPU
            self.hwnd = CreateWindowExW(ex, "KOLMailPet", "KOL Mail 桌宠", WS_POPUP, 0, 0, 1, 1, None, None, hinst,
                                        None)
        self.bubble_hwnd = CreateWindowExW(ex, "KOLMailPetBubble", "KOL Mail 新邮件", WS_POPUP, 0, 0, 1, 1, None,
                                           None, hinst, None)
        if not self.hwnd or not self.bubble_hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hand = LoadCursorW(None, VP(IDC_HAND))
        self.arrow = arrow
        with dpi_context(DPI_UNAWARE):
            self.resize(self.settings.get("scale") or 1.0, keep_feet=False)
            self.place_initially()
            self.render(force=True)
            ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        SetTimer(self.hwnd, TIMER_CHECK, 2000, None)
        self.client.start()
        threading.Thread(target=self._beat, daemon=True, name="pet-beat").start()

    def _beat(self):
        """60 (or the state's rate) wake-ups a second, as WM_TICK; a few a second while she is hidden."""
        due = time.perf_counter()
        while not self.client.stopped.is_set():
            due += 1.0 / max(1.0, self.tick_hz)
            now = time.perf_counter()
            if now - due > 0.25:  # back from a long stall (sleep, a busy machine): start over, do not rush
                due = now
            time.sleep(max(0.0, due - now))  # against the clock, so late wake-ups do not add up
            PostMessageW(self.hwnd, WM_TICK, 0, 0)

    def own_dpi(self) -> float:
        """Without DWM scaling (Windows before 1607) the pet draws itself at the screen's DPI."""
        if SetThreadDpiAwarenessContext:
            return 1.0
        dc = GetDC(None)
        try:
            return GetDeviceCaps(dc, 88) / 96.0  # LOGPIXELSX
        finally:
            ReleaseDC(None, dc)

    def bubble_dpi(self) -> float:
        if self.bubble_hwnd and GetDpiForWindow:
            value = GetDpiForWindow(self.bubble_hwnd)
            if value:
                return value / 96.0
        dc = GetDC(None)
        try:
            return GetDeviceCaps(dc, 88) / 96.0
        finally:
            ReleaseDC(None, dc)

    def resize(self, scale: float, keep_feet: bool = True):
        cw, ch = self.sprites.cell
        k = scale * self.own_dpi()
        size = w, h = max(16, round(cw * k)), max(16, round(ch * k))
        old = self.window_rect() if keep_feet and self.size != (1, 1) else None
        if size != self.size:  # the pictures are kept at the size she is shown
            self.strips.close()
            self.strips.budget = round(STRIP_BUDGET * max(1.0, k * k))
            if self.mix:
                self.mix.close()
                self.mix = None
        self.size, self.scale = size, scale
        if old:  # the feet stay where they were
            cx, bottom = (old.left + old.right) // 2, old.bottom
            self.move(cx - w // 2, bottom - h)
        self.render(force=True)
        self.place_bubble()  # now that the window has its new size

    def window_rect(self) -> W.RECT:
        r = W.RECT()
        GetWindowRect(self.hwnd, ctypes.byref(r))
        return r

    def work_area(self, x: int, y: int) -> W.RECT:
        mon = MonitorFromPoint(W.POINT(x, y), MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO(ctypes.sizeof(MONITORINFO))
        GetMonitorInfoW(mon, ctypes.byref(info))
        return info.rcWork

    def on_screen(self, x: int, y: int) -> tuple[int, int]:
        """(x, y) while her middle is on a screen; otherwise as near it as the nearest screen's work area allows (a
        monitor unplugged, or a place saved at a desk with more screens)."""
        w, h = self.size
        cx, cy = x + w // 2, y + h // 2
        if MonitorFromPoint(W.POINT(cx, cy), MONITOR_DEFAULTTONULL):
            return x, y
        wa = self.work_area(cx, cy)
        return max(wa.left, min(x, wa.right - w)), max(wa.top, min(y, wa.bottom - h))

    def place_initially(self):
        x, y = self.settings.get("x"), self.settings.get("y")
        w, h = self.size
        if x is not None and y is not None:
            self.pos = self.on_screen(x, y)
            return
        wa = self.work_area(0, 0)  # primary monitor: bottom right, just above the taskbar
        self.pos = (wa.right - w - 24, wa.bottom - h)

    def move(self, x: int, y: int):
        self.pos = (x, y)
        SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        self.place_bubble()

    # ---------------------------------------------------------------- drawing

    def current(self):
        wanted = self.brain.state()
        name = self.sprites.resolve(wanted)
        key = (name, self.brain.oneshot)  # a new one-off, even the same reaction again, starts from its first frame
        if key != self.shown_key:
            self.shown_key, self.shown_state, self.started, self.frame = key, name, time.perf_counter(), -1
        st = self.sprites.states[name]
        once = bool(self.brain.oneshot) and self.brain.oneshot[0] == wanted  # a reaction ends on its last frame
        return name, st, st.frame_at((time.perf_counter() - self.started) * 1000, loop=not once)

    def picture(self, st, step: int) -> tuple[int, int, int]:
        """The frame to show, at the window's size: (DC, x, y) of it. For a clip's frame with props, the props are
        blended over a copy of it first, only where they can be (the overlay's box)."""
        (cw, ch), (sw, sh) = self.sprites.cell, self.size
        path, x, y = st.layer.at(st.base_frame(step), (cw, ch))
        base = self.strips.get(path)
        bx, by = x // cw * sw, y // ch * sh
        if not st.overlay:
            return base.dc, bx, by
        path, x, y = st.overlay.at(st.over_frame(step), (cw, ch))
        over = self.strips.get(path)  # base stays loaded: the two newest always do
        ox, oy = x // cw * sw, y // ch * sh
        if self.mix is None:
            self.mix = Canvas(sw, sh)
        m = self.mix
        BitBlt(m.dc, 0, 0, sw, sh, base.dc, bx, by, SRCCOPY)
        x0, y0, x1, y1 = st.overlay.box or (0, 0, cw, ch)
        x0, y0 = max(0, x0 * sw // cw - 1), max(0, y0 * sh // ch - 1)
        x1, y1 = min(sw, -(-x1 * sw // cw) + 1), min(sh, -(-y1 * sh // ch) + 1)
        if x1 > x0 and y1 > y0:
            GdiAlphaBlend(m.dc, x0, y0, x1 - x0, y1 - y0, over.dc, ox + x0, oy + y0, x1 - x0, y1 - y0, BLEND)
        GdiFlush()
        return m.dc, 0, 0

    def render(self, force: bool = False):
        _, st, step = self.current()
        # the beat follows the state, at twice its frame rate: a beat on time with the frames would drop one
        # whenever a wake-up came a little late (a beat with nothing new costs next to nothing). A trickle
        # while hidden.
        self.tick_hz = 4.0 if self.hidden_for_fullscreen else min(120.0, 2000.0 / min(st.durations))
        if step == self.frame and not force:
            return
        try:
            dc, x, y = self.picture(st, step)
        except OSError:  # a picture that cannot be read right now: the last frame stays (Cache tries again later)
            return
        self.frame = step
        screen = GetDC(None)
        try:
            UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(W.POINT(*self.pos)), ctypes.byref(W.SIZE(*self.size)),
                                dc, ctypes.byref(W.POINT(x, y)), 0, ctypes.byref(BLEND), ULW_ALPHA)
        finally:
            ReleaseDC(None, screen)

    def tick(self):
        self.render()
        self.update_bubble()

    def update_bubble(self):
        b = self.brain.bubble()
        key = (b.title, b.line1, b.line2, b.more, b.say) if b else None
        if key == self.bubble_key:
            return
        self.bubble_key = key
        if not b:
            ShowWindow(self.bubble_hwnd, SW_HIDE)
            return
        with dpi_context(DPI_PER_MONITOR_V2):
            self.draw_bubble(b)
            self.place_bubble()
        if not self.hidden_for_fullscreen:  # drawn now, shown when the full-screen app is gone
            ShowWindow(self.bubble_hwnd, SW_SHOWNOACTIVATE)

    def draw_bubble(self, b):
        px = 13 * self.bubble_dpi()
        pad = px * 0.9
        if b.say:  # a short line after a touch: a small bubble around it
            lines = [(b.title, "body", 0xFF283070)]
            heights = {"body": px * 1.6}
            width = round(min(px * 22, pad * 2 + px * 1.1 * len(b.title) + px))
        else:
            title = b.title + (f"（又来了 {b.more} 封）" if b.more else "")
            lines = [(title, "title", 0xFF283070), (b.line1, "body", 0xFF1E2233), (b.line2, "small", 0xFF5A6075)]
            lines = [x for x in lines if x[0]]
            heights = {"title": px * 1.45, "body": px * 2.9, "small": px * 2.6}
            width = round(px * 22)
        tail = px * 0.9
        h = round(pad * 2 + sum(heights[k] for _, k, _ in lines) + tail)
        if self.bubble_canvas:
            self.bubble_canvas.close()
        c = self.bubble_canvas = Canvas(width, h)
        fonts = Fonts(px)
        try:
            GdipGraphicsClear(c.g, 0)
            path = VP()
            GdipCreatePath(0, ctypes.byref(path))
            r, x0, y0, x1, y1 = px * 0.9, 1.5, 1.5, width - 1.5, h - tail - 1.5
            GdipAddPathArc(path, x0, y0, 2 * r, 2 * r, 180, 90)
            GdipAddPathArc(path, x1 - 2 * r, y0, 2 * r, 2 * r, 270, 90)
            GdipAddPathArc(path, x1 - 2 * r, y1 - 2 * r, 2 * r, 2 * r, 0, 90)
            GdipAddPathLine(path, width * 0.62, y1, width * 0.55, h - 1.5)  # the tail points down at the pet
            GdipAddPathLine(path, width * 0.55, h - 1.5, width * 0.5, y1)
            GdipAddPathArc(path, x0, y1 - 2 * r, 2 * r, 2 * r, 90, 90)
            GdipClosePathFigure(path)
            brush, pen = VP(), VP()
            GdipCreateSolidFill(argb(246, 255, 251, 244), ctypes.byref(brush))
            GdipCreatePen1(argb(255, 40, 48, 112), max(1.5, px * 0.13), UnitPixel, ctypes.byref(pen))
            GdipFillPath(c.g, brush, path)
            GdipDrawPath(c.g, pen, path)
            GdipDeleteBrush(brush)
            GdipDeletePen(pen)
            GdipDeletePath(path)
            y = pad
            for text, kind, color in lines:
                font = getattr(fonts, kind)
                rect = RectF(pad, y, width - 2 * pad, heights[kind])
                ink = VP()
                GdipCreateSolidFill(color, ctypes.byref(ink))
                GdipDrawString(c.g, text, -1, font, ctypes.byref(rect), fonts.fmt, ink)  # -1: up to the NUL
                GdipDeleteBrush(ink)
                y += heights[kind]
        finally:
            fonts.close()

    def place_bubble(self):
        """Above her head, in the bubble's own (per-monitor) coordinates: the pet's rectangle is asked for in
        that context too, so both agree whatever the screen's scaling."""
        c = self.bubble_canvas
        if not c or not self.bubble_key:
            return
        with dpi_context(DPI_PER_MONITOR_V2):
            r = self.window_rect()
            w, h = r.right - r.left, r.bottom - r.top
            bx = r.left + w // 2 - round(c.w * 0.55)
            by = r.top + round(h * 0.12) - c.h
            wa = self.work_area(r.left + w // 2, r.top + h // 2)
            bx = max(wa.left, min(bx, wa.right - c.w))
            by = max(wa.top, by)
            c.show(self.bubble_hwnd, bx, by)

    # ---------------------------------------------------------------- what a click touched

    def part_at(self, x: int, y: int) -> str:
        """The body part under (x, y) in window coordinates, "" for none."""
        k = self.scale * self.own_dpi()
        try:
            for path, px, py in self.sprites.part_lookups(self.shown_state, max(0, self.frame), x / k, y / k):
                red = self.part_maps.get(path).red(px, py)
                if red:  # the props on top first: the letter wins over the hand holding it
                    return self.sprites.part_name(red)
        except OSError:  # a part map that cannot be read right now: nothing touched
            pass
        return ""

    def cursor_part(self) -> str:
        pt = W.POINT()
        GetCursorPos(ctypes.byref(pt))
        return self.part_at(pt.x - self.pos[0], pt.y - self.pos[1])

    # ---------------------------------------------------------------- events

    def apply(self, answer):
        if answer is None:
            self.brain.on_offline()
        else:
            self.brain.on_poll(answer)
            new = self.brain.settings
            if new and new.get("scale") and new["scale"] != self.scale and not self.dragging:
                self.resize(new["scale"])
            self.settings = new or self.settings
        if self.brain.quit_reason:
            DestroyWindow(self.hwnd)
            return
        self.render()
        self.update_bubble()

    def fullscreen_check(self):
        """Hide while Windows itself says a full-screen app, a game or a presentation is in front (the state that
        also holds back its own notifications). A merely maximised window does not count."""
        state = ctypes.c_int(0)
        full = SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (
            QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)
        if full != self.hidden_for_fullscreen:
            self.hidden_for_fullscreen = full
            ShowWindow(self.hwnd, SW_HIDE if full else SW_SHOWNOACTIVATE)
            if self.bubble_key:
                ShowWindow(self.bubble_hwnd, SW_HIDE if full else SW_SHOWNOACTIVATE)
            self.render(force=True)

    def open(self, url: str | None):
        """Only ever the app's own page on this computer, whatever the answer from the port said."""
        if url and own_page(url, self.client.port):
            os.startfile(url)

    def click(self, part: str):
        if not self.sprites.touchable:  # a pet without a part map: anywhere opens KOL Mail
            self.open(self.brain.on_click())
        else:
            self.open(self.brain.on_touch(part))
        self.update_bubble()

    def menu(self):
        m = CreatePopupMenu()
        sizes = CreatePopupMenu()
        for i, (label, value) in enumerate((("小", 0.75), ("中", 1.0), ("大", 1.25))):
            AppendMenuW(sizes, MF_STRING | (MF_CHECKED if abs(self.scale - value) < 0.01 else 0), 11 + i, label)
        AppendMenuW(m, MF_STRING, 1, "打开 KOL Mail")
        AppendMenuW(m, MF_SEPARATOR, 0, None)
        AppendMenuW(m, MF_STRING | (MF_CHECKED if self.settings.get("muted") else 0), 2, "静音（来信只换动作，不弹气泡）")
        AppendMenuW(m, MF_POPUP, sizes, "大小")
        AppendMenuW(m, MF_SEPARATOR, 0, None)
        AppendMenuW(m, MF_STRING, 3, "隐藏桌宠（在 KOL Mail 的设置里可以再打开）")
        pt = W.POINT()
        GetCursorPos(ctypes.byref(pt))
        SetForegroundWindow(self.hwnd)  # without it the menu does not close when clicking elsewhere
        cmd = TrackPopupMenu(m, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.hwnd, None)
        DestroyMenu(m)  # also destroys the submenu
        if cmd == 1:
            self.open(self.brain.url)
        elif cmd == 2:
            self.settings["muted"] = not self.settings.get("muted")
            self.client.save(muted=self.settings["muted"])
            if self.settings["muted"]:
                self.brain.dismiss_bubble()
                self.update_bubble()
        elif cmd in (11, 12, 13):
            scale = (0.75, 1.0, 1.25)[cmd - 11]
            self.resize(scale)
            self.client.save(scale=scale, x=self.pos[0], y=self.pos[1])
        elif cmd == 3:
            self.client.save(enabled=False)
            self.brain.quit_reason = "hidden from the menu"
            ShowWindow(self.hwnd, SW_HIDE)
            ShowWindow(self.bubble_hwnd, SW_HIDE)
            # the save goes out on the client thread's next turn: give it that long before the process ends
            SetTimer(self.hwnd, TIMER_QUIT, 2500, None)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            return self._handle(hwnd, msg, wparam, lparam)
        except Exception as e:  # noqa: BLE001  an exception must never escape into Windows' callback
            print("pet window error:", repr(e), flush=True)
            return DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle(self, hwnd, msg, wparam, lparam):
        if msg == WM_TICK:
            self.tick()
            return 0
        if msg == WM_TIMER:
            if wparam == TIMER_CHECK:
                self.fullscreen_check()
                if not self.client.is_alive():  # without it the pet would never hear "leave" again
                    print("pet stopped: the polling thread died", flush=True)
                    DestroyWindow(self.hwnd)
            elif wparam == TIMER_QUIT:
                DestroyWindow(self.hwnd)
            return 0
        if msg == WM_APP:
            while not self.client.answers.empty():
                self.apply(self.client.answers.get_nowait())
            return 0
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if msg == WM_SETCURSOR:
            letter = not self.sprites.touchable or self.cursor_part() == "mail"
            SetCursor(self.hand if letter else self.arrow)  # the hand only where a click opens KOL Mail
            self.brain.on_hover()
            return 1
        if msg == WM_LBUTTONDOWN:
            pt = W.POINT()
            GetCursorPos(ctypes.byref(pt))
            part = self.part_at(pt.x - self.pos[0], pt.y - self.pos[1]) if self.sprites.touchable else ""
            self.press, self.press_part = (pt.x, pt.y, *self.pos), part
            self.dragging = False
            SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE and self.press:
            if not wparam & MK_LBUTTON:  # the capture was lost mid-drag (Alt+Tab, a dialog): forget the press
                self.press, self.dragging, self.last_x = None, False, None
                return 0
            pt = W.POINT()
            GetCursorPos(ctypes.byref(pt))
            dx, dy = pt.x - self.press[0], pt.y - self.press[1]
            if not self.dragging and max(abs(dx), abs(dy)) >= DRAG_THRESHOLD:
                self.dragging = True
            if self.dragging:
                self.brain.on_drag(0 if self.last_x is None else pt.x - self.last_x)
                self.last_x = pt.x
                self.move(self.press[2] + dx, self.press[3] + dy)
                self.render()
            return 0
        if msg == WM_LBUTTONUP and self.press:
            ReleaseCapture()
            self.press = None
            self.last_x = None
            if self.dragging:
                self.dragging = False
                self.brain.on_drop()
                self.client.save(x=self.pos[0], y=self.pos[1])
            else:
                self.click(self.press_part)
            self.render()
            return 0
        if msg == WM_RBUTTONUP:
            self.menu()
            return 0
        if msg in (WM_DISPLAYCHANGE, WM_DPICHANGED):
            self.resize(self.scale)
            pos = self.on_screen(*self.pos)  # her screen may have just been unplugged
            if pos != self.pos:
                self.move(*pos)
            return 0
        if msg == WM_DESTROY:
            self.client.stopped.set()
            DestroyWindow(self.bubble_hwnd)
            self.strips.close()
            self.part_maps.close()
            if self.mix:
                self.mix.close()
            PostQuitMessage(0)
            return 0
        return DefWindowProcW(hwnd, msg, wparam, lparam)

    def _bubble_wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_SETCURSOR:
                b = self.brain.bubble()
                SetCursor(self.arrow if b and b.say else self.hand)
                return 1
            if msg == WM_LBUTTONUP:
                self.open(self.brain.on_bubble_click())
                self.update_bubble()
                return 0
            if msg == WM_RBUTTONUP:  # right click: just close it
                self.brain.dismiss_bubble()
                self.update_bubble()
                return 0
        except Exception as e:  # noqa: BLE001
            print("bubble window error:", repr(e), flush=True)
        return DefWindowProcW(hwnd, msg, wparam, lparam)

    def loop(self):
        msg = W.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))


def single_instance(port: int) -> bool:
    """A named mutex per port: a second pet for the same app sees it and leaves."""
    single_instance.handle = CreateMutexW(None, False, f"Local\\kol-mail-pet-{port}")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext(VP(-4))  # per monitor v2: the bubble stays crisp on every screen
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


def run(port: int, boot: str, settings: dict, sprites: Sprites) -> int:
    dpi_aware()
    w = PetWindow(sprites, port, boot, settings)
    w.create()
    w.loop()
    print("pet stopped:", w.brain.quit_reason, flush=True)
    return 0
