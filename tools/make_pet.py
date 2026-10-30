"""Build the whale-girl desktop pet from one GIF (developer tool, never run on users' machines).

    uv run --no-project --with pillow --with numpy --with scipy --with imagequant python -I tools/make_pet.py \
        鲸鱼娘.gif pet/whale-girl [path/to/rife-ncnn-vulkan.exe] [previews]

The GIF is a chibi girl turning on a light grey studio backdrop, 15 frames. RIFE (github.com/nihui/rife-ncnn-vulkan,
model v4.6) puts four frames between each two, so one turn is 75 frames at 60 a second. Each frame is cut out (the
backdrop and the floor shadow become transparent) and its body parts are mapped (head, face, body, tail, feet).
The states are made from those frames: a hop, a floating envelope, closed eyes and Z's, a shake with a sweat drop, a
thought bubble, a paper plane, a pendulum swing while dragged, and the reactions to a touch (a head pat, a poke in
the face, a tickle, the tail). She holds a little letter in most of them; clicking it opens KOL Mail.

Out come the shared clips (the turn, the hop), the states' own frames and the props some states draw over a clip,
each as pictures of five frames stacked top to bottom (<name>.<k>.png), each with its part map (<name>.<k>.parts.png:
a quarter of the size, the part number in the red channel), and pet.json, which pet/sprites.py reads. The frames
keep the GIF's own size and are shown at half of it (density 2: sharp on a Retina screen); libimagequant puts them in
256 colours with dithering, which keeps the soft shadow soft. With a fourth argument, a preview GIF per state is
written there as well. tools/make_outfit.py dresses her in other outfits with the same states (Plan, everyday)."""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageSequence
from scipy import ndimage

# ---------------------------------------------------------------- cut-out

def backdrop(f: np.ndarray) -> np.ndarray:
    """The studio backdrop is a smooth gradient: fit a quadratic surface to the outermost pixels."""
    h, w, _ = f.shape
    band = np.zeros((h, w), bool)
    band[:3, :] = band[-3:, :] = band[:, :3] = band[:, -3:] = True
    ys, xs = np.nonzero(band)

    def feats(x, y):
        return np.stack([np.ones_like(x), x / w, y / h, (x / w) ** 2, (y / h) ** 2, x * y / (w * h)], -1)

    X = feats(xs.astype(float), ys.astype(float))
    yy, xx = np.mgrid[0:h, 0:w].astype(float)
    G = feats(xx, yy)
    out = np.empty((h, w, 3))
    for c in range(3):
        coef, *_ = np.linalg.lstsq(X, f[ys, xs, c], rcond=None)
        out[..., c] = G @ coef
    return out


def touching_edge(mask: np.ndarray) -> np.ndarray:
    lab, _ = ndimage.label(mask)
    edge = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    return np.isin(lab, edge[edge > 0])


def cut_out(rgb: np.ndarray, tol: float = 9) -> np.ndarray:
    """RGBA with the backdrop and floor shadow removed. Line art stops the flood, so the cream sweater (almost the
    backdrop's colour) survives; edge pixels get a partial alpha and their backdrop share taken out of the colour."""
    f = rgb.astype(float)
    h, w, _ = f.shape
    bg = backdrop(f)
    diff = np.abs(f - bg).max(2)
    chroma = f.max(2) - f.min(2)
    lum, blum = f.mean(2), bg.mean(2)
    warm = f[..., 0] - f[..., 2]
    yy = np.mgrid[0:h, 0:w][0]
    plain = (diff < tol) & (chroma < 9)
    shadow = (yy > 0.55 * h) & (((chroma < 11) & (lum < blum + 4) & (lum > 110))
                                | ((lum > 150) & (chroma < 22) & (warm > -22) & (warm < 4)))
    background = touching_edge(plain | shadow)
    # backdrop boxed in between the legs: the shoes touch, so it never reaches the frame edge
    lab, _ = ndimage.label((plain | shadow) & ~background & (yy > 0.75 * h))
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        part = lab[sl] == i
        if part.sum() >= 150 and sl[0].stop - sl[0].start >= 20 and chroma[sl][part].mean() < 9:
            background[sl] |= part
    background = ndimage.binary_opening(background, iterations=1) | touching_edge(plain)
    band = ndimage.binary_dilation(background, iterations=3) & ~background
    shadowy = ndimage.binary_dilation(background & ~plain, iterations=3)
    alpha = np.where(background, 0.0, 1.0)
    a_plain = np.clip((diff - 6) / 55.0, 0, 1)
    a_shadow = np.clip(np.maximum((chroma - 8) / 25.0, (blum - lum - 45) / 50.0), 0, 1)
    alpha[band] = np.where(shadowy, a_shadow, a_plain)[band]
    lab, n = ndimage.label(alpha > 0.02)  # specks left over from the GIF's dithering
    if n > 1:
        sizes = ndimage.sum(np.ones_like(alpha), lab, range(1, n + 1))
        alpha[np.isin(lab, 1 + np.nonzero(sizes < 40)[0])] = 0
    # Edge pixels take the colour of the nearest fully opaque pixel and the edge is pulled in by half a pixel: no
    # light rim from the grey backdrop shows when the pet stands on a dark desktop.
    alpha = np.minimum(alpha, 0.5 * alpha + 0.5 * ndimage.minimum_filter(alpha, size=3))
    solid = alpha >= 0.999
    _, (iy, ix) = ndimage.distance_transform_edt(~solid, return_indices=True)
    fg = np.where((band & ~solid)[..., None], f[iy, ix], f)
    return np.dstack([fg, alpha * 255]).round().astype(np.uint8)


# ---------------------------------------------------------------- drawing helpers (everything at full GIF scale)

NAVY = (40, 48, 112, 255)
CREAM = (255, 249, 240, 255)
CORAL = (240, 92, 104, 255)
SKY = (160, 205, 255, 255)
SS = 4  # props are drawn 4x larger and scaled down, for smooth edges


def prop_layer(size, draw_fn):
    """Draw with draw_fn(draw, s) at SS times the size, return the downscaled RGBA layer."""
    w, h = size
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(big), SS)
    return big.resize((w, h), Image.LANCZOS)


def font(px: int) -> ImageFont.FreeTypeFont:
    for name in ("seguibl.ttf", "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def envelope(d: ImageDraw.ImageDraw, cx: float, cy: float, w: float, s: int, seal=True):
    w *= s
    cx, cy = cx * s, cy * s
    h = w * 0.68
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    lw = max(1, round(w * 0.065))
    d.rounded_rectangle((x0, y0, x1, y1), radius=w * 0.09, fill=CREAM, outline=NAVY, width=lw)
    d.line([(x0 + lw, y0 + lw), (cx, cy + h * 0.08), (x1 - lw, y0 + lw)], fill=NAVY, width=lw, joint="curve")
    if seal:
        r = w * 0.12
        heart(d, cx, cy + h * 0.1, r, CORAL)


def heart(d, cx, cy, r, fill):
    d.ellipse((cx - r, cy - r * 0.9, cx, cy + r * 0.1), fill=fill)
    d.ellipse((cx, cy - r * 0.9, cx + r, cy + r * 0.1), fill=fill)
    d.polygon([(cx - r * 0.98, cy - r * 0.25), (cx + r * 0.98, cy - r * 0.25), (cx, cy + r * 1.05)], fill=fill)


def alert_badge(d, cx, cy, r, s):
    cx, cy, r = cx * s, cy * s, r * s
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=CORAL, outline=NAVY, width=max(1, round(r * 0.14)))
    bar_w = r * 0.32
    d.rounded_rectangle((cx - bar_w / 2, cy - r * 0.62, cx + bar_w / 2, cy + r * 0.2), radius=bar_w / 2, fill=CREAM)
    d.ellipse((cx - bar_w * 0.6, cy + r * 0.36, cx + bar_w * 0.6, cy + r * 0.36 + bar_w * 1.2), fill=CREAM)


def sweat(d, cx, cy, r, s):
    cx, cy, r = cx * s, cy * s, r * s
    d.polygon([(cx, cy - r * 2.1), (cx - r * 0.86, cy - r * 0.45), (cx + r * 0.86, cy - r * 0.45)], fill=SKY)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SKY)
    lw = max(1, round(r * 0.18))
    d.line([(cx, cy - r * 2.1), (cx - r * 0.9, cy - r * 0.4)], fill=(70, 110, 190, 255), width=lw)
    d.line([(cx, cy - r * 2.1), (cx + r * 0.9, cy - r * 0.4)], fill=(70, 110, 190, 255), width=lw)
    d.arc((cx - r, cy - r, cx + r, cy + r), 330, 210, fill=(70, 110, 190, 255), width=lw)
    d.ellipse((cx - r * 0.45, cy - r * 0.55, cx - r * 0.05, cy - r * 0.05), fill=(255, 255, 255, 230))


def cloud(d, cx, cy, w, s, dots: int):
    cx, cy, w = cx * s, cy * s, w * s
    h = w * 0.62
    lw = max(1, round(w * 0.04))
    puffs = [(-0.28, 0.05, 0.3), (0.0, -0.12, 0.34), (0.28, 0.02, 0.3), (-0.05, 0.16, 0.3), (0.2, 0.18, 0.24),
             (-0.3, 0.2, 0.2)]
    for px, py, pr in puffs:  # outline pass, then fill pass, so inner seams disappear
        r = pr * w
        d.ellipse((cx + px * w - r - lw, cy + py * h - r - lw, cx + px * w + r + lw, cy + py * h + r + lw), fill=NAVY)
    for px, py, pr in puffs:
        r = pr * w
        d.ellipse((cx + px * w - r, cy + py * h - r, cx + px * w + r, cy + py * h + r), fill=CREAM)
    for i in range(3):
        r = w * 0.055
        x = cx + (i - 1) * w * 0.2
        on = i < dots
        d.ellipse((x - r, cy + h * 0.02 - r, x + r, cy + h * 0.02 + r), fill=NAVY if on else (190, 196, 225, 255))
    for k, (ox, oy, rr) in enumerate([(-0.42, 0.62, 0.075), (-0.55, 0.85, 0.05)]):
        r = rr * w
        x, y = cx + ox * w, cy + oy * h
        d.ellipse((x - r - lw, y - r - lw, x + r + lw, y + r + lw), fill=NAVY)
        d.ellipse((x - r, y - r, x + r, y + r), fill=CREAM)


def plane(d, cx, cy, w, s, angle_deg=-18):
    a = math.radians(angle_deg)
    pts = [(0.5, 0), (-0.5, -0.32), (-0.18, 0.02), (-0.5, 0.3)]
    ca, sa = math.cos(a), math.sin(a)
    P = [((cx + (x * ca - y * sa) * w) * s, (cy + (x * sa + y * ca) * w) * s) for x, y in pts]
    lw = max(1, round(w * s * 0.05))
    d.polygon(P, fill=CREAM, outline=NAVY, width=lw)
    d.line([P[0], P[2]], fill=NAVY, width=lw)


def letter_z(d, cx, cy, px, s, alpha):
    f = font(int(px * s))
    fill = (SKY[0], SKY[1], SKY[2], int(255 * alpha))
    stroke = (NAVY[0], NAVY[1], NAVY[2], int(255 * alpha))
    d.text((cx * s, cy * s), "Z", font=f, fill=fill, stroke_width=max(1, int(px * s * 0.09)), stroke_fill=stroke,
           anchor="mm")


def note(d, cx, cy, px, s, alpha):
    try:
        f = ImageFont.truetype("seguisym.ttf", int(px * s))  # Segoe UI Symbol has the note
    except OSError:
        f = font(int(px * s))
    d.text((cx * s, cy * s), "♪", font=f, fill=(NAVY[0], NAVY[1], NAVY[2], int(255 * alpha)), anchor="mm")


def droplet(d, cx, cy, r, s, alpha):
    cx, cy, r = cx * s, cy * s, r * s
    a = int(255 * alpha)
    d.polygon([(cx, cy - r * 2), (cx - r * 0.86, cy - r * 0.4), (cx + r * 0.86, cy - r * 0.4)], fill=(150, 205, 255, a))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(150, 205, 255, a), outline=(70, 110, 190, a),
              width=max(1, round(r * 0.18)))


# ---------------------------------------------------------------- the character

@dataclass(frozen=True)
class Face:
    """Where a character's face is in her front figure: the open eyes (boxes painted over), bare skin between them
    for the colour, the lashes drawn instead ((x0, x1, y) for each eye) and the blush under them."""
    eyes: tuple[tuple[int, int, int, int], ...]
    skin: tuple[int, int, int, int]
    lashes: tuple[tuple[int, int, int], ...]
    blush: tuple[tuple[int, int, int, int], ...]


WHALE_FACE = Face(eyes=((139, 185, 176, 232), (212, 182, 263, 227)), skin=(180, 205, 210, 228),
                  lashes=((142, 175, 212), (218, 257, 208)), blush=((134, 226, 166, 240), (226, 224, 258, 238)))
LASH = (43, 36, 80, 255)


def painted_eyes(frame: np.ndarray, style: str, face_at: Face = WHALE_FACE) -> Image.Image:
    """The front figure with the eyes painted over: each eye is covered with the skin colour found between the eyes
    (a touch darker at the top, like a lid), then drawn again as `style`: "closed" (asleep, ∪), "happy" (^ ^) or
    "squint" (> <)."""
    im = Image.fromarray(frame, "RGBA")
    sx0, sy0, sx1, sy1 = face_at.skin
    face = frame[sy0:sy1, sx0:sx1, :3].reshape(-1, 3).astype(int)  # bare skin between the eyes
    bare = face[(face[:, 0] > 235) & (face[:, 0] - face[:, 2] > 12)]
    base = np.median(bare if len(bare) else face, 0)
    skin = frame.copy()
    keep = np.zeros(frame.shape[:2], bool)
    rgb = frame[..., :3].astype(int)
    hairlike = (rgb[..., 2] - rgb[..., 0] > 50) & (rgb.mean(2) < 150)
    for x0, y0, x1, y1 in face_at.eyes:
        for y in range(y0, y1 + 1):
            shade = 0.965 + 0.035 * (y - y0) / (y1 - y0)
            skin[y, x0:x1 + 1, :3] = np.clip(base * shade, 0, 255).astype(np.uint8)
        # the fringe hangs over the top of the eye: leave alone the strands that come in from outside the patch,
        # but not the lashes, which are as dark as hair and sit wholly inside it
        top = y0 + round(0.3 * (y1 - y0))
        region = hairlike[y0 - 6:top, x0 - 6:x1 + 7]
        lab, _ = ndimage.label(region)
        outside = np.unique(np.concatenate([lab[0], lab[:, 0], lab[:, -1]]))
        strands = np.isin(lab, outside[outside > 0])
        keep[y0:top, x0:x1 + 1] = strands[6:, 6:-6]
    mask = prop_layer(im.size, lambda d, s: [d.rounded_rectangle((x0 * s, y0 * s, x1 * s, y1 * s), radius=9 * s,
                                                                 fill=(255, 255, 255, 255))
                                                for x0, y0, x1, y1 in face_at.eyes])
    m = np.asarray(mask.filter(ImageFilter.GaussianBlur(1.0)).getchannel("A")).copy()
    m[ndimage.binary_dilation(keep, iterations=1)] = 0
    im = Image.composite(Image.fromarray(skin, "RGBA"), im, Image.fromarray(m, "L").filter(ImageFilter.GaussianBlur(0.6)))
    return drawn_eyes(im, style, face_at)


def drawn_eyes(im: Image.Image, style: str, face_at: Face) -> Image.Image:
    """Eyes drawn as `style` on a face whose own eyes are gone (painted_eyes; make_outfit.py has its own way), with
    a blush under them."""
    def lashes(d, s):
        lw = 4 * s
        for side, (x0, x1, y) in enumerate(face_at.lashes):
            if style == "closed":
                d.arc((x0 * s, (y - 14) * s, x1 * s, (y + 9) * s), 20, 160, fill=LASH, width=lw)
                d.line([(x1 * s - 2 * s, y * s - 1 * s), (x1 * s + 5 * s, y * s - 6 * s)], fill=LASH, width=3 * s)
            elif style == "happy":
                d.arc((x0 * s, (y - 6) * s, x1 * s, (y + 20) * s), 200, 340, fill=LASH, width=lw)
            else:  # squint: > for the left eye, < for the right one
                tip, back = (x1 - 4, x0 + 4) if side == 0 else (x0 + 4, x1 - 4)
                d.line([(back * s, (y - 9) * s), (tip * s, (y - 1) * s), (back * s, (y + 7) * s)], fill=LASH,
                       width=lw, joint="curve")

    im.alpha_composite(prop_layer(im.size, lashes))
    strength = 70 if style == "closed" else 120
    blush = prop_layer(im.size, lambda d, s: [d.ellipse((x0 * s, y0 * s, x1 * s, y1 * s), fill=(244, 140, 150, strength))
                                              for x0, y0, x1, y1 in face_at.blush])
    im.alpha_composite(blush.filter(ImageFilter.GaussianBlur(2)))
    return im


def tinted(im: Image.Image, amount: float) -> Image.Image:
    """Greyer and a little blue: how the pet looks while something is broken."""
    a = np.asarray(im).astype(float)
    grey = a[..., :3].mean(2, keepdims=True) * np.array([0.92, 0.96, 1.08])
    a[..., :3] = a[..., :3] * (1 - amount) + np.clip(grey, 0, 255) * amount
    return Image.fromarray(a.round().astype(np.uint8), "RGBA")


# ---------------------------------------------------------------- body parts (what a click touched)

PARTS = ["", "head", "face", "body", "tail", "feet", "mail"]
HEAD, FACE, BODY, TAIL, FEET, MAIL = range(1, 7)


def parts_of(f: np.ndarray) -> np.ndarray:
    """Which part each pixel of a cut-out frame belongs to. Measured on this character: the head (hat, hair, the
    long hair beside the body) above the neck, the face as an ellipse around the cheeks, the tail as the blue to
    the sides of the legs, the feet below the skirt."""
    h, w, _ = f.shape
    a = f[..., 3] > 40
    r, g, b = (f[..., i].astype(int) for i in range(3))
    yy, xx = np.mgrid[0:h, 0:w]
    lum = (r + g + b) / 3
    warm = (r - b > 12) & (r > 190)
    skin = (r > 200) & (r - b > 25) & (g < 228)
    bluish = b > r + 35
    low = a & warm & (yy > 440) & (yy < 530)
    cx = xx[low].mean() if low.any() else w / 2  # the legs' centre: she turns, the legs stay put
    out = np.zeros((h, w), np.uint8)
    out[a & (yy < 262)] = HEAD
    cheeks = a & skin & (yy > 200) & (yy < 255) & (np.abs(xx - cx) < 90)
    if cheeks.sum() > 50:
        xs = xx[cheeks]
        lo, hi = np.percentile(xs, 3), np.percentile(xs, 97)
        ellipse = ((xx - (lo + hi) / 2) / ((hi - lo) / 2 + 8)) ** 2 + ((yy - 218) / 46.0) ** 2 <= 1
        out[a & ellipse] = FACE
    out[a & (yy >= 262) & (yy < 445)] = BODY
    out[a & (yy >= 445)] = FEET
    side = np.abs(xx - cx) > 72
    out[a & bluish & (yy >= 262) & (yy < 380) & side & (lum < 150)] = HEAD  # the long hair, down to the skirt
    out[a & bluish & (yy >= 380) & side] = TAIL  # where it comes out from under the skirt
    out[a & bluish & (yy >= 445) & (lum >= 80)] = TAIL  # below the skirt, blue but not the dark shoes and bows
    return out


# ---------------------------------------------------------------- figures and cells

FW, FH = 402, 570           # GIF frame
CW, CH = 440, 716           # a cell at full scale: room above the head for the hop and the props, below the feet
                            # for the soft shadow; even at a quarter, as the part maps are
BASE_X, BASE_Y = 19, 140    # where an unmoved frame sits in the cell
FEET_Y = BASE_Y + 551
SCALE = 1.0                 # stored at the GIF's own size...
DENSITY = 2                 # ...and shown at half of it: two pixels a point, one a pixel on a Retina screen
FPS = 60
LOOP = 75                   # frames in one turn of the character (the 15 GIF frames, four more after each)
HAND = (BASE_X + 363, BASE_Y + 300)  # where she holds her little letter
HAT_TOP = 14                # the top of her hat in the GIF frame: another outfit's head hangs there when dragged


class Figure:
    """The character in one pose: the picture and its body-part map, transformed together."""

    def __init__(self, img: Image.Image, parts: Image.Image):
        self.img, self.parts = img, parts

    @classmethod
    def of(cls, rgba: np.ndarray, parts: np.ndarray):
        return cls(Image.fromarray(rgba, "RGBA"), Image.fromarray(parts, "L"))

    def resized(self, sx: float, sy: float):
        size = (round(self.img.width * sx), round(self.img.height * sy))
        return Figure(self.img.resize(size, Image.LANCZOS), self.parts.resize(size, Image.NEAREST))

    def with_img(self, img: Image.Image):
        return Figure(img, self.parts)

    @property
    def top(self) -> int:
        """The first row with anything of her: the top of her head."""
        rows = np.nonzero((np.asarray(self.img.getchannel("A")) > 40).any(1))[0]
        return int(rows[0]) if len(rows) else 0


class Cell:
    """One frame of a state at full scale, with its body-part map (0 where nothing can be clicked)."""

    hand = HAND  # where she holds her little letter (another outfit holds it elsewhere)
    feet_x = CW / 2  # the middle between her feet, where her shadow goes
    head_x: float | None = None  # the middle of the top of her head in her figure, if not its middle

    def __init__(self):
        self.img = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
        self.parts = Image.new("L", (CW, CH), 0)

    def shadow(self, lift: float = 0.0, squash: float = 1.0, x: float | None = None):
        x = self.feet_x if x is None else x
        k = max(0.45, 1 - lift / 160)
        rx, ry = 118 * k * squash, 17 * k
        layer = prop_layer((CW, CH), lambda d, s: d.ellipse(((x - rx) * s, (FEET_Y - ry) * s, (x + rx) * s,
                                                              (FEET_Y + ry) * s), fill=(28, 34, 80, int(70 * k))))
        self.img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(5)))

    def put(self, fig: Figure, dx=0.0, dy=0.0, sx=1.0, sy=1.0):
        """The figure scaled about its feet (bottom centre) and moved by dx, dy."""
        if sx != 1.0 or sy != 1.0:
            fig = fig.resized(sx, sy)
        x = round(BASE_X + FW / 2 - fig.img.width / 2 + dx)
        y = round(BASE_Y + FH - fig.img.height + dy)
        self._paste(fig, x, y)

    def _paste(self, fig: Figure, x: int, y: int):
        src = (max(0, -x), max(0, -y))
        self.img.alpha_composite(fig.img, (max(0, x), max(0, y)), src)
        box = (src[0], src[1], fig.img.width, fig.img.height)
        mask = fig.parts.crop(box).point(lambda v: 255 if v else 0)
        self.parts.paste(fig.parts.crop(box), (max(0, x), max(0, y)), mask)

    def put_swung(self, fig: Figure, angle: float):
        """Smaller (picked up) and rotated about the top of the head, as when it hangs from the mouse."""
        small = fig.resized(0.85, 0.85)
        ox, oy = BASE_X + FW / 2 - fig.img.width / 2, BASE_Y + FH - fig.img.height  # where it stands unmoved
        top = round(BASE_Y + 40 + 0.85 * (HAT_TOP - fig.top))  # any outfit's head where the whale girl's hat hangs
        head_x = small.img.width / 2 if self.head_x is None else 0.85 * self.head_x
        x = round(CW / 2 - head_x)  # the top of her head under the pointer
        big = Cell()
        big._paste(small, x, top)
        centre = (CW / 2, BASE_Y + 55)
        self.img.alpha_composite(big.img.rotate(angle, resample=Image.BICUBIC, center=centre))
        rotated = big.parts.rotate(angle, resample=Image.NEAREST, center=centre)
        self.parts.paste(rotated, (0, 0), rotated.point(lambda v: 255 if v else 0))
        # her hand, carried through the same shrinking and turning, keeps the letter
        hx, hy = x + (self.hand[0] - ox) * 0.85 - centre[0], top + (self.hand[1] - oy) * 0.85 - centre[1]
        a = math.radians(angle)  # counter-clockwise on screen, as Image.rotate
        self.letter(at=(centre[0] + hx * math.cos(a) + hy * math.sin(a),
                        centre[1] - hx * math.sin(a) + hy * math.cos(a)), size=54 * 0.85)

    def prop(self, draw_fn, part: int = 0):
        """A drawn prop; with a part, clicking it counts as that part (the letter: MAIL)."""
        layer = prop_layer((CW, CH), draw_fn)
        self.img.alpha_composite(layer)
        if part:
            self.parts.paste(part, (0, 0), layer.getchannel("A").point(lambda v: 255 if v > 90 else 0))

    def letter(self, bob: float = 0.0, dot: bool = False, size: float = 54, dx: float = 0.0, sx: float = 1.0,
               sy: float = 1.0, at: tuple[float, float] | None = None):
        """The little letter in her hand; dx, bob, sx, sy as given to put(), so it moves with her (or `at`)."""
        bottom = BASE_Y + FH
        x, y = at or (CW / 2 + (self.hand[0] - CW / 2) * sx + dx, bottom + (self.hand[1] - bottom) * sy)
        y += bob
        self.prop(lambda d, s: envelope(d, x, y, size, s), MAIL)
        if dot:  # unread mail
            self.prop(lambda d, s: d.ellipse(((x + 22 - 9) * s, (y - 20 - 9) * s, (x + 22 + 9) * s, (y - 20 + 9) * s),
                                             fill=CORAL, outline=NAVY, width=3 * s), MAIL)


def frames_for(seconds: float, fps: int = FPS) -> list[float]:
    """Times t in [0, 1) for one run of a state."""
    n = max(2, round(seconds * fps))
    return [i / n for i in range(n)]


def hop_curve(t: float):
    """(lift, squash x, squash y) at t in [0, 1): crouch, take off, apex, land, settle."""
    if t < 0.12:
        k = t / 0.12
        return 0.0, 1 + 0.05 * k, 1 - 0.07 * k
    if t < 0.78:
        k = (t - 0.12) / 0.66
        return 78 * math.sin(math.pi * k), 0.98, 1.04 - 0.04 * abs(0.5 - k) * 2
    if t < 0.9:
        k = (t - 0.78) / 0.12
        return 0.0, 1.05 - 0.02 * k, 0.93 + 0.03 * k
    return 0.0, 1.0, 1.0


Draw = Callable[[], list[Cell]]  # draws a run of frames when called: one state at a time is held in memory


class Built:
    """One state's pictures: its own frames, or frames of a shared clip (picked by `pick`, None for 0, 1, 2...),
    with a layer of props drawn on top (`overlay`: one Cell per frame; identical ones are stored once)."""

    def __init__(self, frames: int, fps: int = FPS, own: Draw | None = None, clip: str | None = None,
                 pick: list[int] | None = None, overlay: Draw | None = None):
        self.frames, self.fps, self.own, self.clip, self.pick, self.overlay = frames, fps, own, clip, pick, overlay


class Plan:
    """A pet's shared clips and states, each drawn only when saved: one state at a time is held in memory."""

    def __init__(self):
        self.clips: dict[str, Draw] = {}
        self.clip_frames: dict[str, int] = {}
        self.states: dict[str, Built] = {}

    def add_clip(self, name, seconds, fn, fps=FPS):
        self.clips[name] = lambda: make(seconds, fps, fn)
        self.clip_frames[name] = len(frames_for(seconds, fps))

    def own(self, name, seconds, fps=FPS):
        """A state with frames of its own; fn(c, t) draws the frame at t in [0, 1)."""
        def wrap(fn):
            self.states[name] = Built(len(frames_for(seconds, fps)), fps, own=lambda: make(seconds, fps, fn))
        return wrap

    def on_clip(self, name, clip, pick=None):
        """Props drawn over a clip; fn(c, t) draws them (t in [0, 1))."""
        n = len(pick) if pick else self.clip_frames[clip]

        def wrap(fn):
            self.states[name] = Built(n, clip=clip, pick=pick, overlay=lambda: make(n / FPS, FPS, fn))
        return wrap


def make(seconds, fps, fn) -> list[Cell]:
    cells = []
    for t in frames_for(seconds, fps):
        c = Cell()
        fn(c, t)
        cells.append(c)
    return cells


def everyday(plan: Plan, eyes: dict[str, Figure], stand: list[int] | None, rock: list[int], beats: int = 1):
    """What every outfit does: the states over her "sway" clip (`stand` picks its frames for 60 a second, None: all
    of them; `rock` is a gentler choice of them for holding up the envelope), the hop, sleeping, failing, being
    dragged, and the reactions to a pat on the head and a poke in the face. eyes: her front figure ("front") and
    the same with painted eyes (closed, happy, squint). beats: how many times the props over the sway go round
    while it does (a sway twice as long as the whale girl's turn: 2, so that they keep her pace)."""
    front = eyes["front"]

    def hop_clip(c, t):
        lift, sx, sy = hop_curve(t)
        c.shadow(lift, sx); c.put(front, 0, -lift, sx, sy)

    plan.add_clip("hop", 0.9, hop_clip)

    @plan.on_clip("idle", "sway", stand)
    def _(c, t):
        c.letter()

    @plan.on_clip("waiting", "sway", stand)
    def _(c, t):
        c.letter(bob=round(7 * math.sin(2 * math.pi * beats * t)), dot=True)

    @plan.on_clip("thinking", "sway", stand)
    def _(c, t):
        c.letter()
        dots = int(t * 4 * beats) % 4
        c.prop(lambda d, s: cloud(d, CW / 2 + 120, BASE_Y - 40, 110, s, dots))

    @plan.on_clip("notify", "sway", pick=rock)
    def _(c, t):
        bob = round(6 * math.sin(2 * math.pi * beats * t))
        c.prop(lambda d, s: envelope(d, CW / 2 + 6, BASE_Y - 30 + bob, 64, s), MAIL)
        c.prop(lambda d, s: alert_badge(d, CW / 2 + 48, BASE_Y - 58 + bob, 17, s), MAIL)

    def flight(t):  # from beside the right hand up past the head, off the top right
        return CW / 2 + 150 + 45 * t + 18 * math.sin(math.pi * t), BASE_Y + 330 - 440 * t

    steps = 60 * beats  # one reply a second

    @plan.on_clip("sending", "sway", pick=[rock[round(i * len(rock) / steps) % len(rock)] for i in range(steps)])
    def _(c, t):
        c.letter()  # hers stays in her hand; the reply flies off
        t = t * beats % 1.0
        x, y = flight(t)

        def fly(d, s):
            for k in range(1, 4):  # a dotted trail
                xx, yy = flight(max(0.0, t - k * 0.07))
                r = 4.5 - k
                d.ellipse(((xx - r) * s, (yy - r) * s, (xx + r) * s, (yy + r) * s), fill=(150, 160, 210, 200))
            if 0.05 < t < 0.98:
                plane(d, x, y, 66, s, -70 + 15 * t)
        c.prop(fly)

    @plan.on_clip("hop", "hop")  # the letter goes up and down with her
    def _(c, t):
        lift, sx, sy = hop_curve(t)
        c.letter(-lift, sx=sx, sy=sy)

    @plan.on_clip("mail", "hop")
    def _(c, t):
        if t > 0.15:
            lift = hop_curve(t)[0]
            k = min(1.0, (t - 0.15) / 0.25)
            top = BASE_Y + 8 - lift
            c.prop(lambda d, s: envelope(d, CW / 2 + 6, top - 36 * k, 64 * (0.6 + 0.4 * k), s), MAIL)

    @plan.own("sleeping", 1.92, fps=30)  # slow breathing: 30 frames a second look the same and cost half
    def _(c, t):
        c.shadow()
        breath = math.sin(2 * math.pi * t)
        sx, sy = 1.0 + 0.008 * breath, 1.0 - 0.014 * breath
        c.put(eyes["closed"], 0, 0, sx, sy)
        c.letter(sx=sx, sy=sy)

        def zs(d, s):
            for j in range(3):
                p = (t + j / 3) % 1.0
                letter_z(d, CW / 2 + 92 + 46 * p, BASE_Y + 95 - 120 * p, 22 + 16 * p, s, math.sin(math.pi * p))
        c.prop(zs)

    grey = front.with_img(tinted(front.img, 0.45))

    @plan.own("failed", 1.0)
    def _(c, t):
        c.shadow()
        shake = 6 * math.sin(2 * math.pi * 3 * t) * max(0.0, 1 - t * 1.6)
        c.put(grey, shake, 0); c.letter(dx=shake)
        c.prop(lambda d, s: sweat(d, CW / 2 + 110 + shake, BASE_Y + 150 + 4 * math.sin(2 * math.pi * t), 13, s))

    for name, lean in (("drag-left", 7.0), ("drag-right", -7.0)):
        plan.own(name, 0.6)(lambda c, t, lean=lean: c.put_swung(front, lean + 3 * math.sin(2 * math.pi * t)))

    # ---------------------------------------------------------------- reactions to a touch

    @plan.own("pat", 1.2)  # the head: eyes ^ ^, two little bobs, hearts rising
    def _(c, t):
        bob = abs(math.sin(2 * math.pi * t)) * 0.035
        c.shadow(); c.put(eyes["happy"], 0, 0, 1 + bob * 0.4, 1 - bob)
        c.letter(sx=1 + bob * 0.4, sy=1 - bob)

        def hearts(d, s):
            for j, (ox, delay) in enumerate(((-60, 0.0), (55, 0.2), (5, 0.4))):
                p = (t - delay) / 0.6
                if 0 <= p <= 1:
                    r = 10 + 6 * p
                    x, y = CW / 2 + ox + 10 * math.sin(6 * p + j), BASE_Y + 20 - 90 * p
                    heart(d, x * s, y * s, r * s, (240, 92, 104, int(255 * math.sin(math.pi * p))))
        c.prop(hearts)

    @plan.own("poke", 0.9)  # the face: eyes > <, a squish, a little "!"
    def _(c, t):
        k = math.sin(math.pi * min(1.0, t / 0.35)) if t < 0.35 else 0.0
        c.shadow(); c.put(eyes["squint"], 0, 0, 1 + 0.05 * k, 1 - 0.05 * k)
        c.letter(sx=1 + 0.05 * k, sy=1 - 0.05 * k)
        if t < 0.75:
            c.prop(lambda d, s: alert_badge(d, CW / 2 + 95, BASE_Y + 120 - 10 * min(1.0, t * 4), 14, s))


def build_states(base: list[Figure], eyes: dict[str, Figure]) -> Plan:
    """The whale girl's clips (the turn, the hop: character and shadow only) and states. base: the 75 frames of one
    turn. The states that only add props to a clip store just the props: the turn is not stored five times over."""
    plan = Plan()
    own = plan.own

    def sway_clip(c, t):
        c.shadow(); c.put(base[round(t * LOOP) % LOOP])  # round: int(55 / 75 * 75) is 54

    plan.add_clip("sway", LOOP / FPS, sway_clip)
    rock = [round(12 * math.sin(2 * math.pi * i / LOOP)) % LOOP for i in range(LOOP)]  # a gentle rock about the front
    everyday(plan, eyes, None, rock)

    @own("giggle", 1.0)  # the body: eyes ^ ^, wiggling, notes
    def _(c, t):
        c.shadow()
        dx, sy = 6 * math.sin(2 * math.pi * 2 * t), 1.0 - 0.02 * abs(math.sin(4 * math.pi * t))
        c.put(eyes["happy"], dx, 0, 1.0, sy); c.letter(dx=dx, sy=sy)

        def notes(d, s):
            for j, (ox, delay) in enumerate(((-130, 0.0), (120, 0.25), (-100, 0.5))):
                p = (t - delay) / 0.5
                if 0 <= p <= 1:
                    note(d, CW / 2 + ox + 12 * math.sin(5 * p), BASE_Y + 200 - 80 * p, 30, s, math.sin(math.pi * p))
        c.prop(notes)

    @own("spout", 1.3)  # the tail: eyes > <, a little jump, a whale's spout from the head
    def _(c, t):
        lift = 30 * math.sin(math.pi * min(1.0, t / 0.4)) if t < 0.4 else 0.0
        c.shadow(lift); c.put(eyes["squint"], 0, -lift); c.letter(-lift)

        def spout(d, s):
            if t < 0.15:
                return
            p = (t - 0.15) / 0.85
            top = BASE_Y - 10 - lift
            height = 110 * math.sin(math.pi * min(1.0, p * 1.6) / 2)
            fade = 1.0 if p < 0.7 else max(0.0, 1 - (p - 0.7) / 0.3)
            for j in range(7):  # the column
                y = top - height * j / 6
                droplet(d, CW / 2 + 8 + 3 * math.sin(j + 10 * p), y, 9 - j * 0.6, s, fade)
            for j, side in enumerate((-1, 1, -1, 1)):  # drops falling to either side
                q = min(1.0, p * 1.3 - j * 0.08)
                if q > 0:
                    x = CW / 2 + 8 + side * (40 + 25 * j) * q
                    y = top - height + 140 * q * q - 60 * q
                    droplet(d, x, y, 7, s, fade)
        c.prop(spout)

    return plan


# ---------------------------------------------------------------- the GIF in, RIFE in between

def gif_frames(gif: str) -> list[np.ndarray]:
    return [np.asarray(f.convert("RGB")) for f in ImageSequence.Iterator(Image.open(gif))]


def interpolate(frames: list[np.ndarray], rife: str | None, work: Path) -> list[np.ndarray]:
    """One turn as LOOP frames: RIFE (rife-ncnn-vulkan with its v4.6 model) puts four frames between each two of
    the GIF's, the last one back to the first, so the loop has no seam. Without RIFE every frame is repeated."""
    per = LOOP // len(frames)
    if not rife:
        return [f for f in frames for _ in range(per)]
    src, dst = work / "in", work / "out"
    for d in (src, dst):
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.png"):
            old.unlink()
    for i, f in enumerate(frames + frames[:1]):
        Image.fromarray(f).save(src / f"{i:04d}.png")
    exe = Path(rife)
    cmd = [str(exe), "-i", str(src), "-o", str(dst), "-n", str(LOOP + 1), "-m", str(exe.parent / "rife-v4.6"), "-x"]
    import subprocess
    subprocess.run(cmd, check=True, capture_output=True)
    out = sorted(dst.glob("*.png"))
    assert len(out) == LOOP + 1, len(out)
    return [np.asarray(Image.open(p).convert("RGB")) for p in out[:-1]]


# ---------------------------------------------------------------- output

PARTS_SCALE = 4   # the part maps are a quarter of the pictures' size: half of hers on the screen
BAND = 5          # frames per picture: Tk takes ~10 ms to decode one frame, but seconds for 75 at once (it slows
                  # down with the picture's size); GDI+ does not mind either way


def save_frames(out: Path, name: str, cells: list[Cell]) -> dict:
    """The frames in bands of BAND, top to bottom: <name>.<k>.png, and beside each its part map <name>.<k>.parts.png
    (PARTS_SCALE times smaller, the part number times 40 in the red channel). One palette for all bands of a run, so
    the colours do not jump between them."""
    import imagequant
    cw, ch = round(CW * SCALE), round(CH * SCALE)
    strip = Image.new("RGBA", (cw, ch * len(cells)), (0, 0, 0, 0))
    for i, c in enumerate(cells):
        strip.alpha_composite(c.img if c.img.size == (cw, ch) else c.img.resize((cw, ch), Image.LANCZOS), (0, i * ch))
    strip = imagequant.quantize_pil_image(strip, dithering_level=1.0, max_colors=256)
    pw, ph = cw // PARTS_SCALE, ch // PARTS_SCALE
    bands = []
    for k, first in enumerate(range(0, len(cells), BAND)):
        run = cells[first:first + BAND]
        picture, parts = f"{name}.{k}.png", f"{name}.{k}.parts.png"
        strip.crop((0, first * ch, cw, (first + len(run)) * ch)).save(out / picture, optimize=True)
        pmap = Image.new("L", (pw, ph * len(run)), 0)
        for i, c in enumerate(run):
            pmap.paste(c.parts.resize((pw, ph), Image.NEAREST), (0, i * ph))
        pmap.point(lambda v: v * 40).convert("RGB").quantize(colors=8, method=Image.Quantize.MEDIANCUT).save(
            out / parts, optimize=True)
        bands.append([picture, parts])
    return {"bands": bands, "per": BAND, "frames": len(cells)}


def unique(cells: list[Cell]) -> tuple[list[Cell], list[int]]:
    """Identical overlay frames stored once (the letter in her hand is the same in all 75 idle frames)."""
    seen: dict[bytes, int] = {}
    kept, index = [], []
    for c in cells:
        key = c.img.tobytes() + c.parts.tobytes()
        if key not in seen:
            seen[key] = len(kept)
            kept.append(c)
        index.append(seen[key])
    return kept, index


def box_of(cells: list[Cell]) -> list[int]:
    """Where in the cell (stored scale) any of the props is drawn: the pet only blends that much over the clip."""
    x0 = y0 = 10 ** 6
    x1 = y1 = 0
    for c in cells:
        b = c.img.getchannel("A").getbbox()
        if b:
            x0, y0, x1, y1 = min(x0, b[0]), min(y0, b[1]), max(x1, b[2]), max(y1, b[3])
    if x1 <= x0:
        return [0, 0, 0, 0]
    cw, ch = round(CW * SCALE), round(CH * SCALE)
    return [max(0, math.floor(x0 * SCALE) - 1), max(0, math.floor(y0 * SCALE) - 1),
            min(cw, math.ceil(x1 * SCALE) + 1), min(ch, math.ceil(y1 * SCALE) + 1)]


CODEX = {"idle": "idle", "mail": "jumping", "notify": "waving", "waiting": "waiting", "sleeping": "idle",
         "failed": "failed", "thinking": "review", "sending": "running", "drag-left": "running-left",
         "drag-right": "running-right", "hop": "jumping", "pat": "waving", "poke": "waving", "giggle": "jumping",
         "spout": "jumping", "show": "jumping"}


def main(gif: str, out_dir: str, rife: str | None = None, preview_dir: str | None = None):
    out = Path(out_dir)
    raw = gif_frames(gif)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="kolmail-rife-") as work:
        turn = interpolate(raw, rife, Path(work))
    cut = [cut_out(f) for f in turn]
    base = [Figure.of(f, parts_of(f)) for f in cut]
    eyes = {"front": base[0]}
    for style in ("closed", "happy", "squint"):
        eyes[style] = Figure(painted_eyes(cut[0], style), base[0].parts)
    plan = build_states(base, eyes)
    write_pet(out, plan, {"id": "whale-girl", "displayName": "鲸鱼娘", "outfit": "毛衣",
                          "description": "蓝发鲸鱼娘，奶白毛衣、格子裙和鲸鱼尾巴。"}, preview_dir)


def write_pet(out: Path, plan: Plan, about: dict, preview_dir: str | None = None):
    """Draw every clip and state and save their pictures and pet.json into `out`."""
    import shutil
    out.mkdir(parents=True, exist_ok=True)
    clips, states = plan.clips, plan.states
    new = out.parent / f".{out.name}.building"  # the old pictures stay until the new ones are all there
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    meta_clips = {name: save_frames(new, f"clip-{name}", draw()) for name, draw in clips.items()}
    meta = {}
    for name, b in states.items():
        if b.own:
            m = {**save_frames(new, name, b.own()), "fps": b.fps}
        else:
            m = {"clip": b.clip, "frames": b.frames, "fps": b.fps}
            if b.pick:
                m["pick"] = b.pick
            if b.overlay:
                kept, index = unique(b.overlay())
                m["overlay"] = {**save_frames(new, f"{name}.overlay", kept), "pick": index, "box": box_of(kept)}
        meta[name] = {**m, "codex": CODEX[name]}
    cw, ch = round(CW * SCALE), round(CH * SCALE)
    pet = {**about,
           "kolmail": {"version": 2, "cell": [cw, ch], "density": DENSITY, "feet": [cw // 2, round(FEET_Y * SCALE)],
                       "clips": meta_clips, "states": meta, "parts": {"scale": PARTS_SCALE, "names": PARTS}}}
    # bytes: write_text would end it with CRLF on Windows, and the repository is all LF
    (new / "pet.json").write_bytes((json.dumps(pet, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    for old in [*out.glob("*.png"), out / "pet.json"]:
        old.unlink(missing_ok=True)
    for f in new.iterdir():
        f.replace(out / f.name)
    new.rmdir()
    pictures = list(out.glob("*.png"))
    print(f"{len(states)} states, {sum(b.frames for b in states.values())} frames shown, {len(pictures)} pictures, "
          f"{sum(p.stat().st_size for p in pictures) // 1024} KB")
    if preview_dir:
        write_previews(out, pet, Path(preview_dir))


def compose(out: Path, pet: dict, name: str) -> list[Image.Image]:
    """A state's frames as the pet shows them: its own, or the clip's with the overlay on top."""
    cw, ch = pet["kolmail"]["cell"]
    m = pet["kolmail"]["states"][name]

    def frames_of(spec: dict) -> list[Image.Image]:
        got = []
        for picture, _ in spec["bands"]:
            band = Image.open(out / picture).convert("RGBA")
            got += [band.crop((0, i * ch, cw, (i + 1) * ch)) for i in range(band.height // ch)]
        return got

    if "bands" in m:
        return frames_of(m)
    clip = frames_of(pet["kolmail"]["clips"][m["clip"]])
    over = frames_of(m["overlay"]) if "overlay" in m else None
    frames = []
    for i in range(m["frames"]):
        f = clip[m["pick"][i] if "pick" in m else i].copy()
        if over is not None:
            f.alpha_composite(over[m["overlay"]["pick"][i]])
        frames.append(f)
    return frames


def write_previews(out: Path, pet: dict, pv: Path):
    """One GIF per state, at 50 frames a second (as fast as GIF goes)."""
    pv.mkdir(parents=True, exist_ok=True)
    for name, m in pet["kolmail"]["states"].items():
        step = max(1, round(m["fps"] / 50))
        frames = []
        for f in compose(out, pet, name)[::step]:
            bg = Image.new("RGBA", f.size, (236, 240, 247, 255))
            bg.alpha_composite(f)
            frames.append(bg.convert("RGB"))
        frames[0].save(pv / f"{name}.gif", save_all=True, append_images=frames[1:],
                       duration=round(1000 * step / m["fps"]), loop=0)


if __name__ == "__main__":
    main(*sys.argv[1:5])
