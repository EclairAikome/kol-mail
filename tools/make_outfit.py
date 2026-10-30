"""Dress the whale-girl desktop pet in another outfit, from a GIF of her in it (developer tool, never run on users'
machines).

    uv run --no-project --with pillow --with numpy --with scipy --with imagequant --with onnxruntime \\
        --with opencv-python-headless python -I tools/make_outfit.py snow 扔雪球.gif pet/whale-girl-snow \\
        --esrgan realesrgan-ncnn-vulkan.exe --rife rife-ncnn-vulkan.exe --seg isnetis.onnx --work <folder> \\
        [--measure] [--previews <folder>]

The GIFs are short clips of her on a photographed background: snow and dark bushes (240 pixels square, and the
camera zooms), or a parquet floor with her hard shadow on it. Each frame is enlarged four times by Real-ESRGAN
(realesrgan-x4plus-anime, github.com/xinntao/Real-ESRGAN), held still against her standing frame by matching the
background (OpenCV: ORB features, a similarity found with RANSAC) and scaled so that she stands as tall as the whale
girl, her feet where hers are. RIFE (as in make_pet.py) puts frames in between, for 60 a second, and the anime
segmentation model (isnetis.onnx, huggingface.co/skytnt/anime-seg) cuts her out of each one; dark grey bushes are
painted over in the picture the model sees first, as it takes them for her hood otherwise. The far bushes do not
move with the ground she stands on (and she drifts across the GIF while she throws or dances): each frame of her
move is put back so that her body stays over her feet and her soles on the ground, smoothed over a quarter of a
second. Her soft shadow is drawn afresh under her feet.

States: make_pet's everyday ones (the letter in her hand, the hop with the envelope, sleeping, the pat and the poke...)
over a breathing "sway" clip of her standing frame (her eyes closed, happy or squinting are drawn on her face after
her own are filled in with the skin round them, smoothly: blank_eyes), and "show": the GIF's own move, from her
standing frame round to it again, which the pet plays now and then and when her body is touched (pet/brain.py).
GIF frames too fast for RIFE (Outfit.hold) are shown as they are, cut out of the GIF's own picture. The face and
the hand are measured on her standing figure: --measure writes it with a grid into the work folder and stops.

Everything slow is kept in the work folder (enlarged frames, RIFE's, the cut-outs): a second run starts where the
first one stopped. What depends on the GIF, the standing frame, her height and place or the canvas is made again
when one of those changed (params.json there)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageSequence
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_pet as mp  # noqa: E402
from make_pet import BASE_Y, CW, FEET_Y, FH, FPS, Cell, Face, Figure, Plan  # noqa: E402

FIG = (CW, BASE_Y + FH)       # a figure fills the cell down to the GIF frame's bottom: put() leaves it where it is
TALL = 537                    # the whale girl from her hat to her soles, at full scale
SOLES = FEET_Y + 4            # where her soles stand (her shadow's middle is FEET_Y)
MX, MT, MB = 160, 120, 80     # room around the figure while she is worked on: her move reaches beyond it, and she
                              # is moved back over her feet (anchors)
ANCHOR = 15                   # frames over which her drift is smoothed before she is moved back: her own sway stays
CANVAS = (FIG[0] + 2 * MX, FIG[1] + MT + MB)
FADE = 40                     # pixels over which she fades out at the cell's top and sides and the GIF's edge
WARP = 2                      # how frames are laid on the canvas (params.json: a new way makes them again)
CRUMB = 1500                  # pixels: a piece apart from her smaller than this is snow on the ground


@dataclass(frozen=True)
class Outfit:
    id: str
    outfit: str                            # her menu and the settings page call it this
    description: str
    stand: int                             # the GIF frame she stands in when not showing off
    whiten: bool                           # dark bushes behind her, hidden from the segmentation model first
    tall: int = TALL                       # from the top of her head to her soles: less for a wide one
    shift: int = 0                         # sideways in the cell: the middle of her feet is not always hers
    floor: bool = False                    # a wooden floor: brown bits at her edge (her shadow on it) cut off
    crumbs: bool = False                   # snow on the ground: small pieces apart from her dropped
    face: Face | None = None               # in her standing figure (FIG coordinates), measured with --measure;
                                           # its eyes are the boxes of the ellipses round each eye, lashes and all
    hold: tuple[int, int] | None = None    # GIF frames too fast for RIFE (a snowball across her face): shown as
                                           # they are, each for as long as the GIF shows it
    hand: tuple[int, int] | None = None    # where she holds the little letter
    feet: int = CW // 2                    # the middle between her feet, where her shadow goes
    neck: int = 0                          # rows of the figure: above the neck her head...
    hem: int = 0                           # ...below the hem her feet, her body in between


OUTFITS = {
    "snow": Outfit("whale-girl-snow", "雪地斗篷", "鲸鱼帽斗篷、毛绒边和小靴子，会团雪球扔出去。", stand=28, whiten=True,
                   crumbs=True, tall=500, hand=(218, 505), neck=405, hem=600, hold=(15, 18),
                   face=Face(eyes=((143, 342, 197, 384), (238, 339, 282, 382)), skin=(198, 360, 236, 385),
                             lashes=((147, 193, 367), (242, 278, 365)),
                             blush=((145, 382, 185, 396), (243, 380, 283, 394)))),
    "maid": Outfit("whale-girl-maid", "女仆装", "白色头饰和围裙的女仆装，会挥着丝带跳舞。", stand=0, whiten=False,
                   floor=True, shift=-65, hand=(118, 545), feet=165, neck=468, hem=650,
                   face=Face(eyes=((94, 377, 141, 433), (185, 389, 251, 443)), skin=(152, 432, 186, 455),
                             lashes=((97, 138, 410), (189, 247, 419)),
                             blush=((92, 432, 132, 448), (205, 441, 247, 457)))),
}


# ---------------------------------------------------------------- the GIF, enlarged

def gif_frames(path: Path) -> tuple[list[np.ndarray], list[int]]:
    im = Image.open(path)
    frames, durations = [], []
    for f in ImageSequence.Iterator(im):
        frames.append(np.asarray(f.convert("RGB")))
        durations.append(int(f.info.get("duration") or 100))
    return frames, durations


def run(cmd: list[str]):
    done = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if done.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({done.returncode}): {done.stderr[-2000:]}")


def enlarged(frames: list[np.ndarray], esrgan: Path, work: Path) -> list[np.ndarray]:
    """Four times the size, by Real-ESRGAN's anime model (kept in work/up)."""
    src, dst = work / "gif", work / "up"
    for d in (src, dst):
        d.mkdir(parents=True, exist_ok=True)
    if len(list(dst.glob("*.png"))) != len(frames):
        for i, f in enumerate(frames):
            Image.fromarray(f).save(src / f"{i:03d}.png")
        run([str(esrgan), "-i", str(src), "-o", str(dst), "-n", "realesrgan-x4plus-anime", "-s", "4", "-f", "png"])
    return [np.asarray(Image.open(dst / f"{i:03d}.png").convert("RGB")) for i in range(len(frames))]


# ---------------------------------------------------------------- cutting her out

class Segmenter:
    def __init__(self, model: Path, whiten: bool):
        import onnxruntime as ort
        self.session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        self.whiten = whiten

    def alpha(self, rgb: np.ndarray, size: int = 1024) -> np.ndarray:
        """Her alpha, 0 to 1. isnetis takes a square picture of 1024, the frame letterboxed into it."""
        seen = self.whitened(rgb) if self.whiten else rgb
        h0, w0 = seen.shape[:2]
        h, w = (size, round(size * w0 / h0)) if h0 > w0 else (round(size * h0 / w0), size)
        ph, pw = (size - h) // 2, (size - w) // 2
        inp = np.zeros((size, size, 3), np.float32)
        inp[ph:ph + h, pw:pw + w] = np.asarray(Image.fromarray(seen).resize((w, h), Image.BICUBIC), np.float32) / 255
        out = self.session.run(None, {"img": inp.transpose(2, 0, 1)[None]})[0][0][0][ph:ph + h, pw:pw + w]
        small = Image.fromarray((np.clip(out, 0, 1) * 65535).astype(np.uint16))
        return np.asarray(small.resize((w0, h0), Image.BICUBIC), np.float32) / 65535

    @staticmethod
    def whitened(rgb: np.ndarray) -> np.ndarray:
        """The dark grey bushes painted snow white; her navy and her thin dark lines stay."""
        f = rgb.astype(float)
        lum, chroma, blue = f.mean(2), f.max(2) - f.min(2), f[..., 2] - f[..., 0]
        bush = ndimage.binary_opening((lum < 150) & (blue < 20) & (chroma < 45), iterations=3)
        out = rgb.copy()
        out[bush] = (238, 241, 247)
        return out


# ---------------------------------------------------------------- holding the camera still, placing her

def held_still(ups: list[np.ndarray], masks: list[np.ndarray], ref: int) -> list[np.ndarray]:
    """For each frame, the similarity (2x3) that lays its background over the standing frame's."""
    import cv2
    orb = cv2.ORB_create(4000)
    grey = [cv2.cvtColor(u, cv2.COLOR_RGB2GRAY) for u in ups]
    away = [(ndimage.binary_dilation(m > 0.1, iterations=24) == 0).astype(np.uint8) * 255 for m in masks]
    kp_ref, des_ref = orb.detectAndCompute(grey[ref], away[ref])
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    out = []
    for i, g in enumerate(grey):
        if i == ref:
            out.append(np.float32([[1, 0, 0], [0, 1, 0]]))
            continue
        kp, des = orb.detectAndCompute(g, away[i])
        m = None
        if des is not None and des_ref is not None:
            pairs = matcher.match(des, des_ref)
            if len(pairs) >= 12:
                a = np.float32([kp[p.queryIdx].pt for p in pairs])
                b = np.float32([kp_ref[p.trainIdx].pt for p in pairs])
                m, inliers = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=3.0)
                if m is None or inliers.sum() < 12:
                    m = None
        if m is None:
            print(f"  frame {i}: its background matches nothing of the standing frame's, left as it is", flush=True)
        out.append(np.float32(m) if m is not None else np.float32([[1, 0, 0], [0, 1, 0]]))
        k = math.hypot(out[-1][0, 0], out[-1][1, 0])
        print(f"  frame {i}: zoom {k:.3f}, shift {out[-1][0, 2]:.1f},{out[-1][1, 2]:.1f}", flush=True)
    return out


def stance(alpha: np.ndarray) -> tuple[float, float, float]:
    """The top of her head, her soles and the middle between her feet (pixels of the frame), on the biggest solid
    piece only: footprints in the snow come out of the model as bits of her."""
    lab, n = ndimage.label(alpha > 0.5)
    if not n:
        raise ValueError("the model finds nobody in her standing frame")
    biggest = 1 + int(np.argmax(ndimage.sum(np.ones_like(alpha), lab, range(1, n + 1))))
    ys, xs = np.nonzero(lab == biggest)
    top, bottom = ys.min(), ys.max()
    low = xs[ys > bottom - 0.1 * (bottom - top)]  # both feet, the one stepping forward lower down
    return float(top), float(bottom), (low.min() + low.max()) / 2


def placing(alpha: np.ndarray, tall: int, shift: int) -> np.ndarray:
    """The similarity from the standing frame onto the canvas: `tall` high, her soles on the whale girl's."""
    top, bottom, cx = stance(alpha)
    k = tall / (bottom - top)
    tx, ty = MX + CW / 2 + shift - k * cx, MT + SOLES - k * bottom
    return np.float32([[k, 0, tx], [0, k, ty]])


def compose(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a after b, both 2x3."""
    return (np.vstack([a, [0, 0, 1]]) @ np.vstack([b, [0, 0, 1]]))[:2].astype(np.float32)


def laid(picture: np.ndarray, m: np.ndarray, flags: int, border: int) -> np.ndarray:
    """A picture of the size of the enlarged frames, laid on the canvas by m. warpAffine has no area filter
    (INTER_AREA there is bilinear): a picture made smaller is shrunk by resize first, which has."""
    import cv2
    k = math.hypot(m[0, 0], m[1, 0])
    if k < 1:
        small = cv2.resize(picture, (max(1, round(picture.shape[1] * k)), max(1, round(picture.shape[0] * k))),
                           interpolation=cv2.INTER_AREA)
        sx, sy = small.shape[1] / picture.shape[1], small.shape[0] / picture.shape[0]
        m = np.float32([[m[0, 0] / sx, m[0, 1] / sy, m[0, 2]], [m[1, 0] / sx, m[1, 1] / sy, m[1, 2]]])
        picture = small
    return cv2.warpAffine(picture, m, CANVAS, flags=flags, borderMode=border, borderValue=0)


def on_canvas(up: np.ndarray, m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The enlarged frame on the canvas, and where the GIF's picture reaches on it (1 inside, 0 outside)."""
    import cv2
    rgb = laid(up, m, cv2.INTER_CUBIC, cv2.BORDER_REPLICATE)
    inside = laid(np.full(up.shape[:2], 255, np.uint8), m, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT)
    return rgb, inside.astype(np.float32) / 255


# ---------------------------------------------------------------- her move, 60 frames a second

def show_order(n: int, stand: int) -> list[int]:
    """The GIF from her standing frame round to it again."""
    return [(stand + i) % n for i in range(n + 1)]


def in_between(keys: list[np.ndarray], frames: int, rife: Path, work: Path) -> list[np.ndarray]:
    """`frames` evenly timed frames from the first key to the last (which is left out), by RIFE (kept in work)."""
    src, dst = work / "keys", work / "rife"
    for d in (src, dst):
        d.mkdir(parents=True, exist_ok=True)
    if len(list(dst.glob("*.png"))) != frames + 1:
        for old in [*src.glob("*.png"), *dst.glob("*.png")]:
            old.unlink()
        for i, k in enumerate(keys):
            Image.fromarray(k).save(src / f"{i:04d}.png")
        run([str(rife), "-i", str(src), "-o", str(dst), "-n", str(frames + 1), "-m", str(rife.parent / "rife-v4.6"),
             "-x"])
    out = sorted(dst.glob("*.png"))
    assert len(out) == frames + 1, len(out)
    return [np.asarray(Image.open(p).convert("RGB")) for p in out[:-1]]


def alpha_of(seg: Segmenter, rgb: np.ndarray, o: Outfit, cache: Path | None = None,
             alpha: np.ndarray | None = None) -> np.ndarray:
    """Her alpha on the canvas (0 to 1): the model's answer kept in `cache` (or the one given), then cleaned up for
    the outfit."""
    if alpha is not None:
        pass
    elif cache and cache.exists():
        alpha = np.asarray(Image.open(cache), np.float32) / 65535
    else:
        alpha = seg.alpha(rgb)
        if cache:
            Image.fromarray((alpha * 65535).astype(np.uint16)).save(cache)
    if o.floor:
        alpha = off_the_floor(rgb.astype(np.float32), alpha)
    if o.crumbs:
        alpha = without_crumbs(alpha)
    return alpha


def spot(alpha: np.ndarray) -> tuple[float, float]:
    """Where she stands on the canvas: the middle of her body (from her waist down to her knees, roughly) and her
    soles, on the biggest solid piece of her."""
    lab, n = ndimage.label(alpha > 0.5)
    if not n:
        return CANVAS[0] / 2, MT + SOLES
    biggest = 1 + int(np.argmax(ndimage.sum(np.ones_like(alpha), lab, range(1, n + 1))))
    ys, xs = np.nonzero(lab == biggest)
    top, bottom = ys.min(), ys.max()
    body = (ys > top + 0.45 * (bottom - top)) & (ys < top + 0.8 * (bottom - top))
    return float(np.median(xs[body])), float(bottom)


def anchors(spots: list[tuple[float, float]], home: tuple[float, float]) -> list[tuple[int, int]]:
    """How far to move each frame of her move to keep her where she stands (`home`, her standing frame's spot):
    her drift smoothed over ANCHOR frames, so that only the drift goes and her own sway and steps stay."""
    pts = np.array(spots, dtype=float)
    pad = ANCHOR // 2
    smooth = [np.convolve(np.pad(pts[:, i], pad, mode="edge"), np.ones(ANCHOR) / ANCHOR, mode="valid") for i in (0, 1)]
    out = []
    for x, y in zip(*smooth, strict=True):
        dx = int(np.clip(round(home[0] - x), -MX, MX))
        dy = int(np.clip(round(home[1] - y), -MB, MT))
        out.append((dx, dy))
    return out


def figure(rgb: np.ndarray, alpha: np.ndarray, inside: np.ndarray, move: tuple[int, int] = (0, 0)) -> Image.Image:
    """Her figure (FIG, RGBA) out of a canvas frame, moved by `move`: faded where the cell ends above her and
    beside her and where the GIF's picture ends (not under her feet, where the cell ends 15 pixels below her soles),
    the edge pulled in by half a pixel and given the colour of the nearest solid pixel (no rim of the backdrop)."""
    x0, y0 = MX - move[0], MT - move[1]
    window = (slice(y0, y0 + FIG[1]), slice(x0, x0 + FIG[0]))
    f, a = rgb[window].astype(np.float32), alpha[window]
    edge = np.pad(inside[window] > 0.99, ((1, 0), (1, 1)))  # outside the cell above her and beside her...
    edge = np.vstack([edge, edge[-1:]])  # ...but the ground under her goes on
    room = ndimage.distance_transform_edt(edge)[1:-1, 1:-1]
    a = a * np.clip(room / FADE, 0, 1)
    a[a < 0.02] = 0
    a = np.minimum(a, 0.5 * a + 0.5 * ndimage.minimum_filter(a, size=3))
    solid = a >= 0.98
    if solid.any():
        _, (iy, ix) = ndimage.distance_transform_edt(~solid, return_indices=True)
        f = np.where(((a > 0) & ~solid)[..., None], f[iy, ix], f)
    return Image.fromarray(np.dstack([f, a * 255]).round().astype(np.uint8), "RGBA")


def without_crumbs(a: np.ndarray) -> np.ndarray:
    """Her and what is big enough to be her snowball; the snow on the ground comes out of the model in crumbs."""
    lab, n = ndimage.label(a > 0.25)
    if n < 2:
        return a
    sizes = ndimage.sum(np.ones_like(a), lab, range(1, n + 1))
    big = np.isin(lab, 1 + np.nonzero(sizes >= max(CRUMB, 0.03 * sizes.max()))[0])
    return np.where(ndimage.binary_dilation(big, iterations=2), a, 0.0)  # with her soft edge


def off_the_floor(f: np.ndarray, a: np.ndarray) -> np.ndarray:
    """Brown or orange pieces reaching her edge are the floor and her shadow on it (she is navy, white and pink;
    the gold trim of her skirt lies inside it and stays)."""
    lum, warm = f.mean(2), f[..., 0] - f[..., 2]
    brown = (a > 0.02) & (warm > 20) & (f[..., 0] >= f[..., 1]) & (lum < 150)
    lab, n = ndimage.label(ndimage.binary_closing(brown, iterations=1))
    if not n:
        return a
    edge = ndimage.binary_dilation(a < 0.5, iterations=2)
    touching = np.unique(lab[edge & (lab > 0)])
    floor = ndimage.binary_dilation(np.isin(lab, touching), iterations=1)
    return np.where(floor, 0.0, a)


# ---------------------------------------------------------------- body parts, the states

def blank_eyes(stand: Image.Image, face: Face) -> Image.Image:
    """Her standing figure with her eyes filled in by the skin round them: everything inside each eye's ellipse
    becomes the smooth surface that meets the skin at its edge (Laplace's equation, solved by relaxing it), where
    hair beside an eye gives no colour. Covering a box with one skin colour showed the box on a shaded face."""
    rgba = np.asarray(stand).copy()
    rgb = rgba[..., :3].astype(float)
    sx0, sy0, sx1, sy1 = face.skin
    ref = np.median(rgb[sy0:sy1, sx0:sx1].reshape(-1, 3), 0)
    skin = (np.abs(rgb - ref).max(2) < 34) & (rgba[..., 3] > 250)
    yy, xx = np.mgrid[0:rgb.shape[0], 0:rgb.shape[1]]
    hole = np.zeros(skin.shape, bool)
    for x0, y0, x1, y1 in face.eyes:
        cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2 + 1.5, (y1 - y0) / 2 + 1.5
        hole |= ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1
    ys, xs = np.nonzero(ndimage.binary_dilation(hole, iterations=2))
    box = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
    out, inner = rgb[box].copy(), hole[box]
    gives = (inner | skin[box]).astype(float)
    out[inner] = ref
    for _ in range(800):
        total, weight = np.zeros_like(out), np.zeros(inner.shape)
        for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            total += np.roll(out * gives[..., None], step, (0, 1))
            weight += np.roll(gives, step, (0, 1))
        out[inner] = (total / np.maximum(weight, 1e-6)[..., None])[inner]
    filled = rgb.copy()
    filled[box] = out
    soft = np.where(hole, 1.0, np.clip(ndimage.gaussian_filter(hole.astype(float), 0.9) * 2, 0, 1))[..., None]
    rgba[..., :3] = np.clip(rgb * (1 - soft) + filled * soft, 0, 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def parts_of(fig: Image.Image, o: Outfit) -> np.ndarray:
    """Head above her neck, feet below her hem, body between, the face as an ellipse round her eyes."""
    a = np.asarray(fig.getchannel("A")) > 40
    h, w = a.shape
    yy, xx = np.mgrid[0:h, 0:w]
    out = np.zeros((h, w), np.uint8)
    out[a & (yy < o.neck)] = mp.HEAD
    out[a & (yy >= o.neck) & (yy < o.hem)] = mp.BODY
    out[a & (yy >= o.hem)] = mp.FEET
    if o.face:
        (l0, t0, l1, b0), (r0, t1, r1, b1) = o.face.eyes
        cx, cy = (l0 + r1) / 2, (min(t0, t1) + max(b0, b1)) / 2
        rx, ry = (r1 - l0) / 2 + 14, (max(b0, b1) - min(t0, t1)) / 2 + 22
        out[a & (((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1)] = mp.FACE
    return out


def lift_of(fig: Image.Image) -> float:
    """How far above the ground her soles are in this frame of her move (she may jump): her shadow shrinks."""
    ys = np.nonzero((np.asarray(fig.getchannel("A")) > 128).any(1))[0]
    return max(0.0, SOLES - float(ys[-1])) if len(ys) else 0.0


def head_middle(fig: Image.Image) -> float:
    """The middle of the top of her head in her figure: where she hangs from the pointer when dragged."""
    a = np.asarray(fig.getchannel("A")) > 128
    top = int(np.nonzero(a.any(1))[0][0])
    return float(np.nonzero(a[top:top + 60])[1].mean())


def build(o: Outfit, stand: Image.Image, show: list[Image.Image]) -> Plan:
    plan = Plan()
    front = Figure(stand, Image.fromarray(parts_of(stand, o), "L"))
    eyes = {"front": front}
    blank = blank_eyes(stand, o.face)
    for style in ("closed", "happy", "squint"):
        eyes[style] = Figure(mp.drawn_eyes(blank.copy(), style, o.face), front.parts)
    Cell.hand, Cell.feet_x, Cell.head_x = o.hand, o.feet, head_middle(stand)

    # One breath and a little sway, 2.4 s there and back at 30 frames a second (a slow move looks the same and costs
    # half). The way back is the way there backwards, so only that is stored: a state over it plays fewer pictures,
    # which a Mac keeps decoded at 7.9 MB each.
    def sway_clip(c, t):  # t: 0 to 1 on the way there
        k = math.cos(math.pi * t)
        c.shadow(0, 1 + 0.01 * k)
        c.put(front, 2.5 * k, 0, 1 + 0.006 * k, 1 - 0.011 * k)

    plan.add_clip("sway", 1.2, sway_clip, fps=30)
    n = plan.clip_frames["sway"]
    there_and_back = [*range(n), *range(n - 2, 0, -1)]
    stand_pick = [there_and_back[i // 2] for i in range(2 * len(there_and_back))]  # 60 steps a second
    mp.everyday(plan, eyes, stand_pick, stand_pick, beats=2)  # 2.3 s a sway: the props keep the whale girl's pace

    figures = [Figure(f, Image.fromarray(parts_of(f, o), "L")) for f in show]
    lifts = [lift_of(f) for f in show]

    def show_frame(c, t):  # she is kept over her feet (anchors): her shadow stays where it is between them
        i = min(len(figures) - 1, round(t * len(figures)))
        c.shadow(lifts[i])
        c.put(figures[i])

    plan.own("show", len(figures) / FPS)(show_frame)
    return plan


# ---------------------------------------------------------------- the whole run

def gridded(fig: Image.Image) -> Image.Image:
    """Her standing figure, twice the size, on a 10-pixel grid with numbers every 50: to read the face from."""
    k = 2
    big = Image.new("RGBA", (fig.width * k, fig.height * k), (255, 255, 255, 255))
    big.alpha_composite(fig.resize(big.size, Image.NEAREST))
    d = ImageDraw.Draw(big)
    for v in range(0, max(fig.size), 10):
        color = (255, 0, 0, 255) if v % 50 == 0 else (220, 220, 220, 255)
        if v < fig.width:
            d.line((v * k, 0, v * k, big.height), fill=color, width=1)
        if v < fig.height:
            d.line((0, v * k, big.width, v * k), fill=color, width=1)
        if v % 50 == 0:
            d.text((v * k + 2, 2), str(v), fill=(255, 0, 0, 255))
            d.text((2, v * k + 2), str(v), fill=(255, 0, 0, 255))
    return big


def fresh(work: Path, gif: Path, o: Outfit):
    """Throw away what other settings made: the enlarged frames go with the GIF, the model's first look with the
    painting over of the bushes, and RIFE's frames and the cut-outs with any of these and the canvas."""
    now = {"gif": hashlib.sha256(gif.read_bytes()).hexdigest(), "whiten": o.whiten, "stand": o.stand, "tall": o.tall,
           "shift": o.shift, "canvas": [MX, MT, MB], "warp": WARP}
    f = work / "params.json"
    try:
        before = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    if before.get("gif") != now["gif"]:
        shutil.rmtree(work / "up", ignore_errors=True)
    if before.get("gif") != now["gif"] or before.get("whiten") != now["whiten"]:
        (work / "coarse.npy").unlink(missing_ok=True)
    if before != now:
        for d in ("keys", "rife", "cut"):
            shutil.rmtree(work / d, ignore_errors=True)
        (work / "stand.alpha.png").unlink(missing_ok=True)
    f.write_text(json.dumps(now), encoding="utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("outfit", choices=sorted(OUTFITS))
    p.add_argument("gif", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--esrgan", type=Path, required=True)
    p.add_argument("--rife", type=Path, required=True)
    p.add_argument("--seg", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--measure", action="store_true")
    p.add_argument("--previews", type=Path)
    args = p.parse_args()
    o = OUTFITS[args.outfit]
    work = args.work / args.outfit
    work.mkdir(parents=True, exist_ok=True)
    fresh(work, args.gif, o)
    seg = Segmenter(args.seg, o.whiten)

    frames, durations = gif_frames(args.gif)
    print(f"{len(frames)} frames of {frames[0].shape[1]}x{frames[0].shape[0]}, {sum(durations)} ms", flush=True)
    ups = enlarged(frames, args.esrgan, work)
    coarse = work / "coarse.npy"
    if coarse.exists():
        masks = list(np.load(coarse).astype(np.float32))
    else:
        masks = [seg.alpha(u) for u in ups]
        np.save(coarse, np.stack(masks).astype(np.float16))
    still = held_still(ups, masks, o.stand)
    place = placing(masks[o.stand], o.tall, o.shift)
    moves = [compose(place, m) for m in still]

    rgb, inside = on_canvas(ups[o.stand], moves[o.stand])
    stand_alpha = alpha_of(seg, rgb, o, work / "stand.alpha.png")
    stand = figure(rgb, stand_alpha, inside)
    stand.save(work / "stand.png")
    gridded(stand).save(work / "stand-grid.png")
    if args.measure or not (o.face and o.hand and o.neck and o.hem):
        print("measure her face, hand, neck and hem on", work / "stand-grid.png")
        return

    import cv2
    order = show_order(len(frames), o.stand)
    keys = [on_canvas(ups[i], moves[i]) for i in order]
    count = round(sum(durations) / 1000 * FPS)
    between = in_between([k[0] for k in keys], count, args.rife, work)
    cuts = work / "cut"
    cuts.mkdir(exist_ok=True)
    held = [k for k, i in enumerate(order) if o.hold and o.hold[0] <= i <= o.hold[1]]
    frames_shown, alphas = [], []
    for j, rgb in enumerate(between):
        # where between the keys this frame is: RIFE's -n puts frame j at j * keys / (frames asked), the last few
        # on the last key; with the GIF's picture of the keys round it, for her fading at its edge
        at = min(len(keys) - 1, j * len(keys) / (count + 1))
        near = range(max(0, math.floor(at) - 1), min(len(keys), math.ceil(at) + 2))
        inside = np.minimum.reduce([keys[i][1] for i in near])
        if held and held[0] <= at <= held[-1]:  # too fast for RIFE: the GIF's own frame, cut out of the GIF's
            # picture itself and laid on the canvas with it (the canvas's stretched edges fool the model)
            k = round(at)
            rgb, inside = keys[k]
            alpha = alpha_of(seg, rgb, o, alpha=laid(masks[order[k]], moves[order[k]], cv2.INTER_LINEAR,
                                                     cv2.BORDER_CONSTANT))
        else:
            alpha = alpha_of(seg, rgb, o, cuts / f"{j:04d}.alpha.png")
        frames_shown.append((rgb, inside))
        alphas.append(alpha)
        if j % 20 == 0:
            print(f"  cut {j}/{count}", flush=True)
    show = []
    moves_back = anchors([spot(a) for a in alphas], spot(stand_alpha))
    for (rgb, inside), alpha, move in zip(frames_shown, alphas, moves_back, strict=True):
        show.append(figure(rgb, alpha, inside, move))
    plan = build(o, stand, show)
    mp.write_pet(args.out, plan, {"id": o.id, "displayName": "鲸鱼娘", "outfit": o.outfit,
                                  "description": o.description},
                 str(args.previews) if args.previews else None)
    print(json.dumps({"outfit": o.outfit, "show_frames": count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
