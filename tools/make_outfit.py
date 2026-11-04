"""Dress the whale-girl desktop pet in another outfit, or teach her default one a move, from a short video of her on a
plain backdrop (developer tool, never run on users' machines).

    uv run --no-project --with pillow --with numpy --with scipy --with imagequant --with av \\
        --with opencv-python-headless python -I tools/make_outfit.py snow show-snow.mp4 pet/whale-girl-snow \\
        --rife rife-ncnn-vulkan.exe --work <folder> [--measure] [--previews <folder>]

The videos come from Seedance (即梦): a picture of her standing in the outfit, a clip of the move to copy, a camera that
does not move and a flat green backdrop (magenta where something of hers is green) with no floor and no shadow. She
stands at the start and at the end. Her standing frame is the video's first: she is scaled to stand as tall as the
whale girl, her feet where hers are, and the same is done to every frame (the camera stands still). RIFE (as in
make_pet.py) puts frames in between, for SHOW_FPS a second, and from the last frame back to the first. Each frame is
keyed against the backdrop as it is round her (keyed(): Seedance tints it near her): the backdrop's share of every
pixel is taken out of it, so a glowing snowball stays as see-through as it was, the soft shadow the video still draws
under her goes with the backdrop, and no rim of green is left on her. Her own soft shadow is drawn afresh under her
feet. Where Seedance raised the ground she sits on (Outfit.sit), she is put back on it.

States: make_pet's everyday ones (the letter in her hand, the hop with the envelope, sleeping, the pat and the poke...)
over a breathing "sway" clip of her standing frame (her eyes closed, happy or squinting are drawn on her face after
her own are filled in with the skin round them, smoothly: blank_eyes), and "show": the video's move, from her standing
frame round to it again, which the pet plays now and then and when her body is touched (pet/brain.py). The face and
the hand are measured on her standing figure: --measure writes it with a grid into the work folder and stops.

"sweater" is the default outfit (make_pet.py): only its move is made and added to the folder (show.<k>.png and its
entry in pet.json), nothing else there changes. The video's last frame is laid over her front figure; RIFE goes from
that figure to the video's first frame (she has her hands up already there) and from its last frame back. Running
make_pet.py again makes that folder afresh, without the move: run this one after it.

RIFE's frames are kept in the work folder: a second run starts where the first one stopped. They are made again when
the video, her height and place, the canvas or the way frames are laid on it changed (params.json there)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_pet as mp  # noqa: E402
from make_pet import BASE_X, BASE_Y, CW, FEET_Y, FH, HAND, WHALE_FACE, Cell, Face, Figure, Plan  # noqa: E402

FIG = (CW, BASE_Y + FH)       # a figure fills the cell down to the GIF frame's bottom: put() leaves it where it is
TALL = 537                    # the whale girl from her hat to her soles, at full scale
SOLES = FEET_Y + 4            # where her soles stand (her shadow's middle is FEET_Y)
MX, MT, MB = 160, 120, 80     # room around the figure while she is worked on: her move reaches beyond it
ANCHOR = 15                   # frames (odd) over which her sideways drift is smoothed before she is moved back
CANVAS = (FIG[0] + 2 * MX, FIG[1] + MT + MB)
FADE = 40                     # pixels over which she fades out at the cell's top and sides and the video's edge
SHOW_FPS = 30                 # her move: the video has 24 a second; 60 would cost twice the room for little more
LEAD = 0.4                    # seconds from the default outfit's front figure to the video's first frame...
BACK = 0.2                    # ...and from the video's last frame back to her standing one
KEY = (0.12, 0.72)            # the backdrop's share in a pixel: below the first she is solid, above the second she
                              # is gone (the noise on the backdrop, and the light her glow casts on it, stays above)
PLATE = 6.0                   # pixels (sigma) over which the backdrop round her is measured
SOLID = 0.98                  # alpha from which a pixel is solid...
NEAR = 4                      # ...and pixels from anything solid: a see-through edge further out is a shadow or a glow;
DIM = 110                     # a shadow if no channel of its own colour is brighter than this...
FAINT = 0.35                  # ...and a glow fainter than this is light on the backdrop, not hers: it fades out
EASE = 6                      # frames over which she is lowered onto the ground she sits on, and lifted off it again
WARP = 1                      # how frames are laid on the canvas (params.json: a new way makes RIFE's frames again)


@dataclass(frozen=True)
class Outfit:
    id: str
    outfit: str                            # her menu and the settings page call it this
    description: str
    tall: int = TALL                       # from the top of her head to her soles: less for a wide one
    shift: int = 0                         # sideways in the cell: the middle of her feet is not always hers
    face: Face | None = None               # in her standing figure (FIG coordinates), measured with --measure;
                                           # its eyes are the boxes of the ellipses round each eye, lashes and all
    hand: tuple[int, int] | None = None    # where she holds the little letter
    feet: int = CW // 2                    # the middle between her feet, where her shadow goes
    neck: int = 0                          # rows of the figure: above the neck her head...
    hem: int = 0                           # ...below the hem her feet, her body in between
    into: bool = False                     # the default outfit: only its move is made, into make_pet's folder
    glow: bool = True                      # light round her that touches nothing solid stays (keyed())
    sit: tuple[float, float] | None = None  # seconds of her move in which she sits on ground Seedance drew higher
                                           # than where she stands: she is lowered onto hers


def moved(face: Face, dx: int, dy: int) -> Face:
    """A face measured in the whale girl's GIF frame, where that frame sits in the cell."""
    return Face(eyes=tuple((x0 + dx, y0 + dy, x1 + dx, y1 + dy) for x0, y0, x1, y1 in face.eyes),
                skin=(face.skin[0] + dx, face.skin[1] + dy, face.skin[2] + dx, face.skin[3] + dy),
                lashes=tuple((x0 + dx, x1 + dx, y + dy) for x0, x1, y in face.lashes),
                blush=tuple((x0 + dx, y0 + dy, x1 + dx, y1 + dy) for x0, y0, x1, y1 in face.blush))


OUTFITS = {
    "snow": Outfit("whale-girl-snow", "雪地斗篷", "白毛绒帽、雪花发饰和鲸鱼斗篷，会变出雪球。", hand=(342, 495),
                   neck=397, hem=566, sit=(2.1, 4.1),
                   face=Face(eyes=((141, 335, 190, 377), (225, 335, 283, 375)), skin=(203, 360, 222, 374),
                             lashes=((145, 187, 359), (229, 279, 358)),
                             blush=((148, 375, 188, 389), (240, 373, 278, 387)))),
    "maid": Outfit("whale-girl-maid", "女仆装", "白色头饰和围裙的女仆装，会挥着小剑转圈跳舞。", hand=(318, 458),
                   neck=404, hem=575,
                   face=Face(eyes=((146, 335, 196, 379), (230, 334, 285, 374)), skin=(203, 364, 226, 378),
                             lashes=((150, 190, 360), (234, 280, 357)),
                             blush=((152, 379, 192, 393), (240, 375, 280, 389)))),
    "sweater": Outfit("whale-girl", "毛衣", "", face=moved(WHALE_FACE, BASE_X, BASE_Y), hand=HAND,
                      neck=BASE_Y + 262, hem=BASE_Y + 445, into=True,  # make_pet.parts_of's rows
                      glow=False),  # Seedance's lens flares round her come out as pink smears; the butterfly stays
}


# ---------------------------------------------------------------- the video

def run(cmd: list[str]):
    done = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if done.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({done.returncode}): {done.stderr[-2000:]}")


def video_frames(path: Path) -> Iterator[np.ndarray]:
    """The video's frames (RGB), one after another: a few seconds of 1080p do not fit in memory at once."""
    import av
    with av.open(str(path)) as c:
        for f in c.decode(video=0):
            yield f.to_ndarray(format="rgb24")


def video_rate(path: Path) -> float:
    """Frames a second, from the frames' own times (the stream's average rate is off: 24.12 for 24)."""
    import av
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        pts = sorted(p.pts for p in c.demux(s) if p.pts is not None)
        return (len(pts) - 1) / float((pts[-1] - pts[0]) * s.time_base)


# ---------------------------------------------------------------- keying her out

def backdrop_of(rgb: np.ndarray) -> np.ndarray:
    """The backdrop's colour: the middle of the picture's outer ring (her glow may reach an edge, never most of it).
    It drifts a little over a video, so every frame has its own."""
    b = 6
    ring = np.concatenate([rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
                           rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3)])
    return np.median(ring, 0)


def keyed(rgb: np.ndarray, bg: np.ndarray, glow: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Her colours and alpha (0 to 1) in a picture of her on a plain backdrop of colour `bg`, which she has none of:
    a colour difference key. The backdrop's own channels (green; red and blue for magenta) stand above its others by
    so much; a pixel's share of the backdrop is how far its own stand above its others against that (grey, white and
    her navy: not at all; a snowball half see-through: half as far). The backdrop is measured where it is plainly the
    backdrop and carried in over PLATE pixels: Seedance tints it round her, and against the colour at the picture's
    edge her outline would keep a rim of the tint. Her colour is what remains when that share of the backdrop is taken
    out of the pixel, with no more green than the mean of red and blue (magenta: no more red and blue than green): the
    backdrop's light on her edge, and in the glow round a snowball, which turns white-blue again. A see-through pixel
    more than NEAR pixels from anything solid is a shadow on the ground (which looks just like the edge of her dark
    hair) when it is dim, and goes; when it is bright it is a glow, of which only the stronger part stays (the light
    it throws on the backdrop goes), and none of it unless `glow`."""
    f = rgb.astype(np.float32)
    b = np.asarray(bg, np.float32)
    lead = b > b.mean()

    def above(x: np.ndarray) -> np.ndarray:
        return x[..., lead].min(-1) - x[..., ~lead].max(-1)

    plain = np.clip(above(f) / above(b), 0, 1) >= KEY[1]
    weight = ndimage.gaussian_filter(plain.astype(np.float32), PLATE)
    near = np.stack([ndimage.gaussian_filter(f[..., c] * plain, PLATE) for c in range(3)], -1)
    known = np.clip(weight / 0.05, 0, 1)[..., None]  # deep inside her the backdrop is not seen: bg there
    plate = near / np.maximum(weight, 1e-6)[..., None] * known + b * (1 - known)
    share = np.clip(above(f) / np.maximum(above(plate), 0.2 * above(b)), 0, 1)
    alpha = 1 - np.clip((share - KEY[0]) / (KEY[1] - KEY[0]), 0, 1)
    fg = (f - share[..., None] * plate) / np.clip(1 - share, 0.05, 1)[..., None]
    if lead.sum() == 1:
        fg[..., lead] = np.minimum(fg[..., lead], fg[..., ~lead].mean(-1, keepdims=True))
    else:
        fg[..., lead] -= np.maximum(above(fg), 0)[..., None]
    fg = np.clip(fg, 0, 255)
    far = (alpha < SOLID) & (ndimage.distance_transform_edt(alpha < SOLID) > NEAR)
    dim = (fg.max(2) < DIM) | (not glow)
    alpha[far & dim] = 0
    alpha[far & ~dim] = np.clip((alpha[far & ~dim] - FAINT) / (1 - FAINT), 0, 1)
    return fg, alpha


# ---------------------------------------------------------------- placing her

def stance(alpha: np.ndarray) -> tuple[float, float, float]:
    """The top of her head, her soles and the middle between her feet (pixels of the frame), on the biggest solid
    piece only: a snowball or a butterfly apart from her is not her."""
    lab, n = ndimage.label(alpha > 0.5)
    if not n:
        raise ValueError("the key leaves nobody in her standing frame")
    biggest = 1 + int(np.argmax(ndimage.sum(np.ones_like(alpha), lab, range(1, n + 1))))
    ys, xs = np.nonzero(lab == biggest)
    top, bottom = ys.min(), ys.max()
    low = xs[ys > bottom - 0.1 * (bottom - top)]  # both feet, the one stepping forward lower down
    return float(top), float(bottom), (low.min() + low.max()) / 2


def placing(alpha: np.ndarray, target: tuple[float, float, float]) -> np.ndarray:
    """The similarity from a video frame onto the canvas that puts her stance where `target` (top, soles, middle
    between the feet, in FIG coordinates) says."""
    top, bottom, cx = stance(alpha)
    t_top, t_bottom, t_cx = target
    k = (t_bottom - t_top) / (bottom - top)
    return np.float32([[k, 0, MX + t_cx - k * cx], [0, k, MT + t_bottom - k * bottom]])


def laid(picture: np.ndarray, m: np.ndarray, flags: int, border: int) -> np.ndarray:
    """A picture laid on the canvas by m. warpAffine has no area filter (INTER_AREA there is bilinear): a picture made
    smaller is shrunk by resize first, which has."""
    import cv2
    k = math.hypot(m[0, 0], m[1, 0])
    if k < 1:
        small = cv2.resize(picture, (max(1, round(picture.shape[1] * k)), max(1, round(picture.shape[0] * k))),
                           interpolation=cv2.INTER_AREA)
        sx, sy = small.shape[1] / picture.shape[1], small.shape[0] / picture.shape[0]
        m = np.float32([[m[0, 0] / sx, m[0, 1] / sy, m[0, 2]], [m[1, 0] / sx, m[1, 1] / sy, m[1, 2]]])
        picture = small
    return cv2.warpAffine(picture, m, CANVAS, flags=flags, borderMode=border, borderValue=0)


def on_canvas(frame: np.ndarray, m: np.ndarray) -> np.ndarray:
    """A video frame on the canvas, the backdrop carried on past its edges."""
    import cv2
    return laid(frame, m, cv2.INTER_CUBIC, cv2.BORDER_REPLICATE)


def reach_of(frame: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Where the video's picture reaches on the canvas: 1 inside, 0 outside."""
    import cv2
    inside = laid(np.full(frame.shape[:2], 255, np.uint8), m, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT)
    return inside.astype(np.float32) / 255


# ---------------------------------------------------------------- her move, SHOW_FPS frames a second

def in_between(src: Path, frames: int, rife: Path, dst: Path) -> list[Path]:
    """`frames` evenly timed frames from the first picture in `src` to the last (which is left out), by RIFE (its
    first ones: make_pet.rife_asks); kept in `dst`, made again when there should be another number of them."""
    n = mp.rife_asks(frames, len(list(src.glob("*.png"))))
    dst.mkdir(parents=True, exist_ok=True)
    if len(list(dst.glob("*.png"))) != n:
        for old in dst.glob("*.png"):
            old.unlink()
        run([str(rife), "-i", str(src), "-o", str(dst), "-n", str(n), "-m", str(rife.parent / "rife-v4.6"), "-x"])
    out = sorted(dst.glob("*.png"))
    if len(out) != n:
        raise RuntimeError(f"RIFE made {len(out)} frames in {dst}, not {n}")
    return out[:frames]


def keys_in(folder: Path, pictures: list[np.ndarray]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(pictures):
        Image.fromarray(p).save(folder / f"{i:04d}.png")
    return folder


def spot(alpha: np.ndarray) -> tuple[float, float]:
    """Where she stands on the canvas: the middle of her body (from her waist down to her knees, roughly) and her
    lowest point, on the biggest solid piece of her."""
    lab, n = ndimage.label(alpha > 0.5)
    if not n:
        return CANVAS[0] / 2, float(CANVAS[1])
    biggest = 1 + int(np.argmax(ndimage.sum(np.ones_like(alpha), lab, range(1, n + 1))))
    ys, xs = np.nonzero(lab == biggest)
    top, bottom = ys.min(), ys.max()
    body = (ys > top + 0.45 * (bottom - top)) & (ys < top + 0.8 * (bottom - top))
    return float(np.median(xs[body])), float(bottom)


def anchors(spots: list[tuple[float, float]], home: float, ground: float,
            sit: tuple[int, int] | None = None) -> list[tuple[int, int]]:
    """How far to move each frame of her move to keep her where she stands (`home`, her standing frame's spot
    across): her drift smoothed over ANCHOR frames, so that only the drift goes and her own sway and steps stay, and
    none at either end, where she stands. Up and down she is left alone (the camera stands still, so the ground does
    too, and she may jump), but for the frames `sit` (from, to) where she sits on ground drawn higher than hers: there
    she goes down onto `ground` by as much as she sits above it in most of them, easing in and out over EASE frames."""
    pts = np.array([x for x, _ in spots], dtype=float)
    pad = ANCHOR // 2
    smooth = np.convolve(np.pad(pts, pad, mode="edge"), np.ones(ANCHOR) / ANCHOR, mode="valid")
    i = np.arange(len(pts))
    ramp = np.clip(np.minimum(i, len(pts) - 1 - i) / ANCHOR, 0, 1)
    down = np.zeros(len(pts))
    if sit:
        s0, s1 = sit
        drop = np.clip(ground - np.median([b for _, b in spots[s0:s1]]), 0, MT)
        k = np.clip(np.minimum(i - s0, s1 - i) / EASE, 0, 1)
        down = drop * k * k * (3 - 2 * k)
    return [(int(np.clip(round((home - x) * r), -MX, MX)), round(d))
            for x, r, d in zip(smooth, ramp, down, strict=True)]


def figure(fg: np.ndarray, alpha: np.ndarray, inside: np.ndarray, move: tuple[int, int] = (0, 0)) -> Image.Image:
    """Her figure (FIG, RGBA) out of a keyed canvas frame, moved by `move`: faded where the cell ends above her and
    beside her and where the video's picture ends (not under her feet, where the cell ends 15 pixels below her
    soles)."""
    x0, y0 = MX - move[0], MT - move[1]
    window = (slice(y0, y0 + FIG[1]), slice(x0, x0 + FIG[0]))
    f, a = fg[window], alpha[window]
    edge = np.pad(inside[window] > 0.99, ((1, 0), (1, 1)))  # outside the cell above her and beside her...
    edge = np.vstack([edge, edge[-1:]])  # ...but the ground under her goes on
    room = ndimage.distance_transform_edt(edge)[1:-1, 1:-1]
    a = a * np.clip(room / FADE, 0, 1)
    a[a < 0.02] = 0
    return Image.fromarray(np.dstack([f, a * 255]).round().astype(np.uint8), "RGBA")


def front_of(folder: Path) -> Image.Image:
    """The default outfit's front figure (FIG, RGBA): the first frame of its sway clip, without the shadow the clip
    draws under her (the move draws its own)."""
    meta = json.loads((folder / "pet.json").read_text(encoding="utf-8"))["kolmail"]
    band = Image.open(folder / meta["clips"]["sway"]["bands"][0][0]).convert("RGBA")
    rgba = np.asarray(band.crop((0, 0, FIG[0], FIG[1]))).copy()
    a = rgba[..., 3]
    rgba[..., 3] = np.where((np.arange(FIG[1])[:, None] > FEET_Y - 40) & (a < 190), 0, a)
    return Image.fromarray(rgba, "RGBA")


def on_backdrop(fig: Image.Image, bg: np.ndarray) -> np.ndarray:
    """A figure on the canvas over a plain backdrop, where an unmoved frame of her move would be."""
    canvas = Image.new("RGBA", CANVAS, tuple(int(round(v)) for v in bg) + (255,))
    canvas.alpha_composite(fig, (MX, MT))
    return np.asarray(canvas.convert("RGB"))


# ---------------------------------------------------------------- body parts, the states

def eye_drawing(rgba: np.ndarray, face: Face) -> tuple[np.ndarray, ...]:
    """What of her face is her open eyes' drawing, to be taken out (the iris, its white, the lashes and the lids, the
    red-brown liner and the faint lower lid just outside the measured eye), what is hair there (it stays: her fringe
    hangs over her eyes, and her hair goes on beside them), what is of the skin's colour, that colour and the measured
    eyes themselves (skin wherever the hair is not, though the fringe above is nearer than the cheek below). Thin
    tips of the fringe under an eye go with the eye: alone on the cheek they looked like creases. Filling the whole
    measured ellipse with skin had left bits of the lashes round it, cut the fringe off and bitten into the hair
    beside."""
    rgb = rgba[..., :3].astype(float)
    yy, xx = np.mgrid[0:rgb.shape[0], 0:rgb.shape[1]]
    sx0, sy0, sx1, sy1 = face.skin
    ref = np.median(rgb[sy0:sy1, sx0:sx1].reshape(-1, 3), 0)
    skin = (np.abs(rgb - ref).max(2) < 34) & (rgba[..., 3] > 250)
    inside, near, iris, low = (np.zeros(skin.shape, bool) for _ in range(4))
    for x0, y0, x1, y1 in face.eyes:
        cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2 + 1.5, (y1 - y0) / 2 + 1.5
        e = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1
        inside |= e
        near |= ((xx - cx) / (rx + 9)) ** 2 + ((yy - cy) / (ry + 9)) ** 2 <= 1
        iris |= e & (yy > y0 + 0.4 * (y1 - y0))  # as blue as her hair: only the top of an eye can be fringe
        low |= (yy > cy) & (np.abs(xx - cx) < rx + 9)
    lum = rgb.mean(2)
    bluish = (rgb[..., 2] - rgb[..., 0] > 45) & (rgb[..., 2] - rgb[..., 1] > 25) & (lum < 150)
    lab, _ = ndimage.label(bluish & ~iris)
    rim = ndimage.binary_dilation(near, iterations=3) & ~near
    hair = np.isin(lab, np.unique(lab[rim & bluish])) & ~iris & near
    wisp = hair & low & ~ndimage.binary_opening(hair, iterations=2)
    hair &= ~wisp
    dark = (rgb.max(2) < 140) | ((rgb[..., 0] - rgb[..., 2] > 15) & (lum < 170))
    lid = low & (rgb[..., 2] > rgb[..., 0] + 10)
    eye = (inside & ~hair) | (near & ~inside & (dark | lid) & ~hair & ~skin) | wisp  # its white is all but skin
    lab, _ = ndimage.label(ndimage.binary_closing(eye, iterations=1))
    eye &= np.isin(lab, np.unique(lab[ndimage.binary_dilation(inside, iterations=4) & eye]))
    hole = ndimage.binary_dilation(eye, iterations=2) & near & ~(hair & ~ndimage.binary_dilation(eye, iterations=1))
    return hole, hair, skin, ref, inside


def relaxed(rgb: np.ndarray, hole: np.ndarray, gives: np.ndarray, start: np.ndarray, box) -> np.ndarray:
    """`rgb` with `hole` (inside `box`, the slices round it) made the smooth surface that meets the pixels of `gives`
    round it (Laplace's equation, solved by relaxing it from `start`, one colour)."""
    out, inner = rgb[box].copy(), hole[box]
    weights = (inner | gives[box]).astype(float)
    out[inner] = start
    for _ in range(1200):
        total, weight = np.zeros_like(out), np.zeros(inner.shape)
        for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            total += np.roll(out * weights[..., None], step, (0, 1))
            weight += np.roll(weights, step, (0, 1))
        out[inner] = (total / np.maximum(weight, 1e-6)[..., None])[inner]
    full = rgb.copy()
    full[box] = out
    return full


def blank_eyes(stand: Image.Image, face: Face) -> Image.Image:
    """Her standing figure with the drawing of her eyes taken out (eye_drawing): each pixel of it becomes skin, or
    hair where hair is nearer outside the eyes themselves (the liner over the hair beside them), smoothly from what is
    round it (one colour on a shaded face showed as a patch)."""
    rgba = np.asarray(stand).copy()
    rgb = rgba[..., :3].astype(float)
    hole, hair, skin, ref, eyes = eye_drawing(rgba, face)
    ys, xs = np.nonzero(ndimage.binary_dilation(hole, iterations=3))
    box = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
    bare, locks = skin & ~hair & ~hole, hair & ~hole
    by_skin = relaxed(rgb, hole, bare, ref, box)
    if not locks.any():  # no hair round her eyes at all: skin everywhere
        locks = np.zeros_like(hole)
        by_hair = by_skin
    else:
        by_hair = relaxed(rgb, hole, locks, np.median(rgb[locks], 0), box)
    hairy = ((ndimage.distance_transform_edt(~locks) < ndimage.distance_transform_edt(~bare)) & ~eyes).astype(float)
    hairy = ndimage.gaussian_filter(hairy, 0.7)[..., None]
    filled = by_hair * hairy + by_skin * (1 - hairy)
    rgba[..., :3] = np.where(hole[..., None], np.clip(filled, 0, 255), rgb).astype(np.uint8)
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


def lift_of(fig: Image.Image, soles: float) -> float:
    """How far above the ground (her soles' row when she stands) her soles are in this frame of her move (she may
    jump): her shadow shrinks. The biggest solid piece only: a snowball under her is not her foot."""
    lab, n = ndimage.label(np.asarray(fig.getchannel("A")) > 128)
    if not n:
        return 0.0
    biggest = 1 + int(np.argmax(ndimage.sum(np.ones(lab.shape), lab, range(1, n + 1))))
    ys = np.nonzero((lab == biggest).any(1))[0]
    return max(0.0, soles - float(ys[-1]))


def head_middle(fig: Image.Image) -> float:
    """The middle of the top of her head in her figure: where she hangs from the pointer when dragged."""
    a = np.asarray(fig.getchannel("A")) > 128
    top = int(np.nonzero(a.any(1))[0][0])
    return float(np.nonzero(a[top:top + 60])[1].mean())


def show_in(c: Cell, fig: Image.Image, o: Outfit, soles: float = SOLES) -> Cell:
    """A frame of her move in the cell: she is kept over her feet (anchors), so her shadow stays between them."""
    c.shadow(lift_of(fig, soles))
    c.put(Figure(fig, Image.fromarray(parts_of(fig, o), "L")))
    return c


def build(o: Outfit, stand: Image.Image, show: list[Image.Image]) -> Plan:
    plan = Plan()
    front = Figure(stand, Image.fromarray(parts_of(stand, o), "L"))
    eyes = {"front": front}
    blank = blank_eyes(stand, o.face)
    for style in ("closed", "happy", "squint"):
        eyes[style] = Figure(mp.drawn_eyes(blank.copy(), style, o.face), front.parts)
    Cell.hand, Cell.feet_x, Cell.head_x = o.hand, o.feet, head_middle(stand)

    # One breath and a little sway, 2.3 s there and back at 30 frames a second (a slow move looks the same and costs
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

    def show_frame(c, t):
        show_in(c, show[min(len(show) - 1, round(t * len(show)))], o)

    plan.own("show", len(show) / SHOW_FPS, fps=SHOW_FPS)(show_frame)
    return plan


def add_show(out: Path, o: Outfit, show: list[Image.Image], soles: float, previews: Path | None):
    """Her move added to the default outfit's folder (make_pet.py's): show.<k>.png and its entry in pet.json, made
    beside it first (as write_pet does): the old move stays until the new one is all there."""
    Cell.hand, Cell.feet_x = o.hand, o.feet
    new = out.parent / f".{out.name}.show"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    meta = mp.save_frames(new, "show", [show_in(Cell(), f, o, soles) for f in show])
    pet = json.loads((out / "pet.json").read_text(encoding="utf-8"))
    pet["kolmail"]["states"]["show"] = {**meta, "fps": SHOW_FPS, "codex": mp.CODEX["show"]}
    # bytes: write_text would end it with CRLF on Windows, and the repository is all LF
    (new / "pet.json").write_bytes((json.dumps(pet, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    for old in out.glob("show.*.png"):
        old.unlink()
    for f in new.iterdir():
        f.replace(out / f.name)
    new.rmdir()
    print(f"show: {meta['frames']} frames, {sum((out / b).stat().st_size for b, _ in meta['bands']) // 1024} KB")
    if previews:
        mp.write_previews(out, {"kolmail": {**pet["kolmail"], "states": {"show": pet["kolmail"]["states"]["show"]}}},
                          previews)


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


def fresh(work: Path, params: dict):
    """Throw away RIFE's frames when what they were made from changed (params.json in the work folder)."""
    f = work / "params.json"
    try:
        before = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    if before != params:
        for d in ("keys", "rife"):
            shutil.rmtree(work / d, ignore_errors=True)
    f.write_text(json.dumps(params), encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def laid_out(video: Path, o: Outfit, front: Image.Image | None) -> tuple[list[np.ndarray], np.ndarray, np.ndarray]:
    """The video's frames on the canvas, her standing frame where she should stand (laid over `front`, the default
    outfit's figure, when only her move is made), with the similarity used and where the picture reaches."""
    frames = list(video_frames(video))  # 1080p for a few seconds: dropped once she is on the canvas
    print(f"{len(frames)} frames of {frames[0].shape[1]}x{frames[0].shape[0]}", flush=True)
    if front is not None:  # the video's last frame: she stands there as she does in it
        target, ref = stance(np.asarray(front.getchannel("A"), np.float32) / 255), frames[-1]
    else:
        target, ref = (SOLES - o.tall, SOLES, CW / 2 + o.shift), frames[0]
    place = placing(keyed(ref, backdrop_of(ref))[1], target)
    first, last = (stance(keyed(f, backdrop_of(f))[1]) for f in (frames[0], frames[-1]))
    print(f"  her height first {first[1] - first[0]:.0f}, last {last[1] - last[0]:.0f} pixels "
          f"(a camera that moved: very different)", flush=True)
    return [on_canvas(f, place) for f in frames], place, reach_of(frames[0], place)


def her_move(on: list[np.ndarray], rate: float, o: Outfit, front: Image.Image | None, soles: float,
             inside: np.ndarray, rife: Path, work: Path) -> list[Image.Image]:
    """Her move's figures, SHOW_FPS a second: (from her front figure to the video's first frame,) the video, and
    from its last frame back to where she started; kept over her feet and on her ground (anchors)."""
    home = on_backdrop(front, backdrop_of(on[-1])) if front is not None else on[0]
    parts = [("main", on, round((len(on) - 1) / rate * SHOW_FPS)), ("back", [on[-1], home], round(BACK * SHOW_FPS))]
    if front is not None:
        parts.insert(0, ("lead", [home, on[0]], round(LEAD * SHOW_FPS)))
    shown = []
    for name, pictures, count in parts:
        shown += in_between(keys_in(work / "keys" / name, pictures), count, rife, work / "rife" / name)
        print(f"  {name}: {count} frames", flush=True)

    def cut(path: Path) -> tuple[np.ndarray, np.ndarray]:
        rgb = np.asarray(Image.open(path).convert("RGB"))
        return keyed(rgb, backdrop_of(rgb), o.glow)

    spots = [spot(cut(s)[1]) for s in shown]
    xs = [x for x, _ in spots]
    print(f"  her drift across: {min(xs) - xs[0]:+.0f} to {max(xs) - xs[0]:+.0f} pixels", flush=True)
    sit = tuple(round(t * SHOW_FPS) for t in o.sit) if o.sit else None
    moves = anchors(spots, xs[0], MT + soles, sit)
    return [figure(*cut(s), inside, move) for s, move in zip(shown, moves, strict=True)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("outfit", choices=sorted(OUTFITS))
    p.add_argument("video", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--rife", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--measure", action="store_true")
    p.add_argument("--previews", type=Path)
    args = p.parse_args()
    o = OUTFITS[args.outfit]
    if o.into and args.measure:
        p.error(f"{args.outfit} is make_pet.py's outfit, measured there: --measure is for a new one")
    work = args.work / args.outfit
    work.mkdir(parents=True, exist_ok=True)

    front = front_of(args.out) if o.into else None
    soles = stance(np.asarray(front.getchannel("A"), np.float32) / 255)[1] if front is not None else SOLES
    on, place, inside = laid_out(args.video, o, front)
    fresh(work, {"video": digest(args.video), "place": np.round(place, 4).tolist(), "canvas": [MX, MT, MB],
                 "warp": WARP, "fps": SHOW_FPS, "lead": LEAD, "back": BACK,
                 "front": digest(args.out / "clip-sway.0.png") if o.into else None})
    if not o.into:
        stand = figure(*keyed(on[0], backdrop_of(on[0]), o.glow), inside)
        stand.save(work / "stand.png")
        gridded(stand).save(work / "stand-grid.png")
        if args.measure or not (o.face and o.hand and o.neck and o.hem):
            print("measure her face, hand, feet, neck and hem on", work / "stand-grid.png")
            return

    show = her_move(on, video_rate(args.video), o, front, soles, inside, args.rife, work)
    if o.into:
        add_show(args.out, o, show, soles, args.previews)
        return
    plan = build(o, stand, show)
    mp.write_pet(args.out, plan, {"id": o.id, "displayName": "鲸鱼娘", "outfit": o.outfit,
                                  "description": o.description},
                 str(args.previews) if args.previews else None)
    print(json.dumps({"outfit": o.outfit, "show_frames": len(show)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
