"""The page's look: a colour preset, light or dark, and an optional wallpaper of the user's own.

Kept in data/skin.json; the wallpaper itself in data/skin/ (one file, replaced on upload). The page reads the
settings from GET /api/skin and the picture from /skin/wallpaper, outside /api/ because a CSS background request
cannot carry the X-KOL-Mail header. Only JPEG, PNG and WebP are accepted, recognised by their first bytes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PRESETS = ("classic", "ocean", "sakura", "matcha")
MODES = ("auto", "light", "dark")
MAX_WALLPAPER = 10 * 1024 * 1024
TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
DEFAULTS = {"preset": "classic", "mode": "auto", "show": 35, "focus_x": 50, "focus_y": 50, "glass": False,
            "auto_accent": False, "wallpaper": None}


class SkinError(ValueError):
    pass


def kind_of(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _int(value, lo: int, hi: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(lo, min(hi, int(round(value))))


def clean(raw: dict) -> dict:
    out = dict(DEFAULTS)
    out["preset"] = raw.get("preset") if raw.get("preset") in PRESETS else DEFAULTS["preset"]
    out["mode"] = raw.get("mode") if raw.get("mode") in MODES else DEFAULTS["mode"]
    out["show"] = _int(raw.get("show"), 0, 80, DEFAULTS["show"])
    out["focus_x"] = _int(raw.get("focus_x"), 0, 100, 50)
    out["focus_y"] = _int(raw.get("focus_y"), 0, 100, 50)
    out["glass"] = raw.get("glass") is True
    out["auto_accent"] = raw.get("auto_accent") is True
    w = raw.get("wallpaper")
    if isinstance(w, dict) and w.get("ext") in TYPES and isinstance(w.get("version"), str) and w["version"].isalnum():
        out["wallpaper"] = {"ext": w["ext"], "version": w["version"][:16]}
    return out


class Skin:
    def __init__(self, data: Path):
        self.file = data / "skin.json"
        self.folder = data / "skin"

    def load(self) -> dict:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        s = clean(raw if isinstance(raw, dict) else {})
        if s["wallpaper"] and not self.wallpaper_path(s).exists():
            s["wallpaper"] = None
        return s

    def _write(self, s: dict) -> dict:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, self.file)
        return s

    def save(self, changes: dict) -> dict:
        """Everything but the wallpaper itself, which only changes through set_wallpaper/remove_wallpaper."""
        current = self.load()
        changes = {k: v for k, v in changes.items() if k in DEFAULTS and k != "wallpaper"}
        return self._write(clean({**current, **changes}))

    def wallpaper_path(self, s: dict) -> Path:
        return self.folder / f"wallpaper.{s['wallpaper']['ext']}"

    def set_wallpaper(self, data: bytes) -> dict:
        if len(data) > MAX_WALLPAPER:
            raise SkinError(f"壁纸最大 {MAX_WALLPAPER // 1024 // 1024} MB")
        ext = kind_of(data)
        if not ext:
            raise SkinError("只能用 JPG、PNG 或 WebP 图片做壁纸")
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"wallpaper.{ext}"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        for other in self.folder.glob("wallpaper.*"):
            if other != target:
                other.unlink(missing_ok=True)
        s = self.load()
        s["wallpaper"] = {"ext": ext, "version": hashlib.sha256(data).hexdigest()[:16]}
        return self._write(clean({**s, "focus_x": 50, "focus_y": 50}))

    def remove_wallpaper(self) -> dict:
        for other in self.folder.glob("wallpaper.*"):
            other.unlink(missing_ok=True)
        s = self.load()
        s["wallpaper"] = None
        return self._write(s)
