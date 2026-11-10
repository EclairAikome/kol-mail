"""Draws KOL Mail's icon (static/kolmail.ico, static/kolmail.png) from the pet's own art: the whale girl's head on
a rounded square in the 深海鲸鱼 colours, with a small envelope. Run by a maintainer when the art changes (needs
Pillow, which the app itself never uses); the two files it writes are committed.

    python tools/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

BASE = Path(__file__).resolve().parents[1]
FRAME = BASE / "pet" / "whale-girl" / "clip-sway.0.png"
CELL = (0, 0, 440, 716)          # the first frame of the band
HEAD = (52, 128, 388, 464)       # hat, hair and face, square
SIZE = 1024                      # drawn big, scaled down for each size of the .ico
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
TOP, BOTTOM = (64, 104, 200), (30, 52, 120)   # the square's colours, top to bottom
CREAM, NAVY = (255, 250, 240), (28, 35, 64)


def background() -> Image.Image:
    grad = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))
    square = grad.resize((SIZE, SIZE)).convert("RGBA")
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=round(SIZE * 0.225), fill=255)
    square.putalpha(mask)
    return square


def envelope(width: int) -> Image.Image:
    """A cream envelope with a navy outline, drawn at its final size on the big canvas."""
    height = round(width * 0.7)
    pad = round(width * 0.06)
    im = Image.new("RGBA", (width + 2 * pad, height + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    line = max(2, round(width * 0.07))
    box = (pad, pad, pad + width, pad + height)
    d.rounded_rectangle(box, radius=round(width * 0.12), fill=CREAM, outline=NAVY, width=line)
    mid = (pad + width // 2, pad + round(height * 0.58))
    d.line([(pad + line, pad + line), mid, (pad + width - line, pad + line)], fill=NAVY, width=line, joint="curve")
    return im


def draw() -> Image.Image:
    icon = background()
    head = Image.open(FRAME).convert("RGBA").crop(CELL).crop(HEAD)
    side = round(SIZE * 0.84)
    head = head.resize((side, side), Image.LANCZOS)
    # a soft shadow under the head, so the cream hat does not melt into a light wallpaper behind small sizes
    shadow = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    at = ((SIZE - side) // 2 - round(SIZE * 0.05), round(SIZE * 0.06))
    shadow.paste((10, 16, 40, 120), (at[0], at[1] + round(SIZE * 0.02)), head.getchannel("A"))
    icon.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(SIZE * 0.02)))
    icon.alpha_composite(head, at)
    env = envelope(round(SIZE * 0.31))
    icon.alpha_composite(env, (SIZE - env.width - round(SIZE * 0.045), SIZE - env.height - round(SIZE * 0.05)))
    mask = background().getchannel("A")  # whatever stands out of the rounded square is cut off
    icon.putalpha(Image.composite(icon.getchannel("A"), Image.new("L", icon.size, 0), mask))
    return icon


def main() -> int:
    if not FRAME.is_file():
        print(f"missing {FRAME}", file=sys.stderr)
        return 1
    icon = draw()
    static = BASE / "static"
    icon.resize((256, 256), Image.LANCZOS).save(static / "kolmail.png", optimize=True)
    icon.save(static / "kolmail.ico", sizes=[(s, s) for s in SIZES])
    print("wrote", static / "kolmail.ico", (static / "kolmail.ico").stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
