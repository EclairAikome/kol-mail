"""How a reply looks in the KOL's mail program: the font, and a signature with a handwritten name and a banner.

Replies are written as plain text (the drafts, the side-by-side, the compliance check all work on it). Only when a
mail goes out is it turned into HTML here: the text in the chosen font (Arial unless the owner picked another), the
handwritten name in place of the signature's first line when that line is the name, and the banner under the
signature. The pictures go inline (mailserver.py), so they show without the reader allowing "external images".
The plain text goes along as the mail's text version.

Kept in data/signature.json; the pictures in data/signature/. The handwritten name is drawn by the page (a canvas
with one of the handwriting fonts the computer has) and uploaded as a PNG at twice its shown size; the banner is the
team's (templates/kolmail-inline-banner.png) unless the owner uploads another. The page shows the pictures from
/signature/<kind>.png, outside /api/ because an <img> cannot carry the X-KOL-Mail header."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import struct
from pathlib import Path

import mailserver

FONTS = {  # what the owner picks from, and the stack the mail asks for (the reader may not have the first one)
    "Arial": "Arial, Helvetica, sans-serif",
    "Calibri": "Calibri, Carlito, Arial, sans-serif",
    "Helvetica": "Helvetica, Arial, sans-serif",
    "Verdana": "Verdana, Geneva, sans-serif",
    "Tahoma": "Tahoma, Verdana, sans-serif",
    "Segoe UI": "'Segoe UI', Arial, sans-serif",
    "Georgia": "Georgia, 'Times New Roman', serif",
    "Times New Roman": "'Times New Roman', Times, serif",
}
DEFAULT_FONT = "Arial"
KINDS = ("name", "banner")
MAX_PICTURE = 1024 * 1024
MAX_SIDE = 1600              # pixels of an uploaded picture (twice its shown size)
DEFAULT_BANNER = Path(__file__).resolve().parent / "templates" / f"{mailserver.INLINE_PREFIX}banner.png"
DEFAULTS = {"font": DEFAULT_FONT, "name_on": False, "name_text": "", "name_font": "", "banner_on": True,
            "versions": {}}
URL = re.compile(r"https?://[^\s<>\"'\u0080-￿]*[^\s<>\"'.,;:!?)\]\u0080-￿]")  # ends at 。 too


class SignatureError(ValueError):
    pass


def png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG, read from its header; None for anything else."""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def clean(raw: dict) -> dict:
    out = {**DEFAULTS, "versions": {}}
    if raw.get("font") in FONTS:
        out["font"] = raw["font"]
    out["name_on"] = raw.get("name_on") is True
    out["banner_on"] = raw.get("banner_on") is not False
    out["name_text"] = " ".join(str(raw.get("name_text") or "").split())[:40]
    out["name_font"] = re.sub(r"[^\w .'-]", "", str(raw.get("name_font") or ""))[:60]
    versions = raw.get("versions") if isinstance(raw.get("versions"), dict) else {}
    out["versions"] = {k: v[:16] for k, v in versions.items() if k in KINDS and isinstance(v, str) and v.isalnum()}
    return out


class Signature:
    def __init__(self, data: Path):
        self.file = data / "signature.json"
        self.folder = data / "signature"

    def load(self) -> dict:
        try:
            return clean(json.loads(self.file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return clean({})

    def _write(self, s: dict):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    def save(self, raw: dict) -> dict:
        """The settings the page sends; the pictures' versions are kept, they change only with the pictures."""
        current = self.load()
        s = clean({**current, **{k: v for k, v in raw.items() if k != "versions"}, "versions": current["versions"]})
        self._write(s)
        return s

    def own_picture(self, kind: str) -> Path:
        return self.folder / f"{mailserver.INLINE_PREFIX}{kind}.png"

    def picture(self, kind: str) -> Path | None:
        """The file a picture is sent from: the owner's own, or the team banner shipped with the app."""
        own = self.own_picture(kind)
        if own.is_file():
            return own
        return DEFAULT_BANNER if kind == "banner" and DEFAULT_BANNER.is_file() else None

    def set_picture(self, kind: str, data: bytes) -> dict:
        if kind not in KINDS:
            raise SignatureError("不认识的图片")
        size = png_size(data)
        if not size or len(data) > MAX_PICTURE:
            raise SignatureError("只能是 1 MB 以内的 PNG 图片")
        if not all(0 < side <= MAX_SIDE for side in size):
            raise SignatureError(f"图片太大了，长宽都要在 {MAX_SIDE} 像素以内")
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.own_picture(kind)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        s = self.load()
        s["versions"][kind] = hashlib.sha256(data).hexdigest()[:12]
        self._write(s)
        return s

    def remove_picture(self, kind: str) -> dict:
        """The owner's own picture goes; the banner then falls back to the team's."""
        if kind not in KINDS:
            raise SignatureError("不认识的图片")
        self.own_picture(kind).unlink(missing_ok=True)
        s = self.load()
        s["versions"].pop(kind, None)
        self._write(s)
        return s

    def shown(self, kind: str) -> dict | None:
        """{"width", "height", "version", "own"} of a picture as the mail shows it (half its pixels), or None."""
        path = self.picture(kind)
        try:
            size = png_size(path.read_bytes()) if path else None
        except OSError:
            size = None
        if not size:
            return None
        own = path == self.own_picture(kind)
        version = self.load()["versions"].get(kind, "") if own else "team"
        return {"width": max(1, size[0] // 2), "height": max(1, size[1] // 2), "version": version or "0", "own": own}

    def pictures(self, s: dict | None = None) -> dict[str, Path]:
        """The pictures that go with a mail now, by kind."""
        s = s or self.load()
        out = {}
        if s["name_on"] and s["name_text"] and self.own_picture("name").is_file():
            out["name"] = self.own_picture("name")
        if s["banner_on"] and self.picture("banner"):
            out["banner"] = self.picture("banner")
        return out

    def outgoing(self, text: str, signature: str) -> dict:
        """send_email arguments for a reply: its HTML with the plain text carried along, and the inline pictures."""
        s = self.load()
        pictures = self.pictures(s)
        sizes = {k: self.shown(k) for k in pictures}
        markup = render(text, signature, s, {k: f"cid:{mailserver.cid(k)}" for k in pictures}, sizes)
        return {"body": mailserver.text_mark(text) + markup, "html": True,
                "attachments": [str(p) for p in pictures.values()]}


def _lines(text: str) -> str:
    """Escaped text with its links clickable and its line breaks kept."""
    out, last = [], 0
    for m in URL.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        url = html.escape(m.group(0), quote=True)
        out.append(f'<a href="{url}" style="color:#1a5fb4">{url}</a>')
        last = m.end()
    out.append(html.escape(text[last:]))
    lines = "".join(out).split("\n")
    # HTML folds runs of spaces: an indented list keeps its indent only as non-breaking spaces
    lines = [re.sub(r"^ +", lambda m: "&nbsp;" * len(m.group(0)), line).replace("  ", " &nbsp;") for line in lines]
    return "<br>\n".join(lines)


def _img(src: str, size: dict | None, alt: str) -> str:
    dims = f' width="{size["width"]}" height="{size["height"]}"' if size else ""
    return (f'<img src="{html.escape(src, quote=True)}"{dims} alt="{html.escape(alt, quote=True)}" '
            f'style="display:block;border:0;outline:none">')


def render(text: str, signature: str, s: dict, sources: dict[str, str], sizes: dict[str, dict | None]) -> str:
    """The HTML of a reply. sources maps the pictures that go along to their src (cid: in a mail, a URL in the
    page's preview); sizes gives their shown size."""
    text = (text or "").replace("\r\n", "\n").strip()
    sig = (signature or "").replace("\r\n", "\n").strip()
    body, sig_lines = text, []
    if sig and text.endswith(sig):
        body, sig_lines = text[:-len(sig)].rstrip(), sig.split("\n")
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    parts = [f'<p style="margin:0 0 1em 0">{_lines(p.strip())}</p>' for p in paragraphs]
    block = []
    if "name" in sources:
        block.append(f'<div style="margin:0 0 4px 0">{_img(sources["name"], sizes.get("name"), s["name_text"])}</div>')
        if sig_lines and sig_lines[0].strip().casefold() == s["name_text"].casefold():
            sig_lines = sig_lines[1:]  # the handwriting is the name: the same name typed under it would be twice
    if sig_lines:
        block.append(f'<div>{_lines(chr(10).join(sig_lines))}</div>')
    if "banner" in sources:
        block.append(f'<div style="margin:12px 0 0 0">{_img(sources["banner"], sizes.get("banner"), "")}</div>')
    if block:
        parts.append(f'<div style="margin:1.5em 0 0 0">{"".join(block)}</div>')
    stack = FONTS.get(s.get("font"), FONTS[DEFAULT_FONT])
    return (f'<div style="font-family:{html.escape(stack, quote=True)};font-size:14px;line-height:1.5;'
            f'color:#222222">\n' + "\n".join(parts) + "\n</div>")
