"""A pet's pictures, what each state is, and which body part a pixel belongs to.

Two layouts are read from pet/<name>/pet.json:
- KOL Mail's own ("kolmail": {"version": 2}): frames in pictures of a few each ("bands", "per" frames to a band),
  stacked top to bottom. A state either has its own frames, or plays frames of a shared clip ("clips": the turn, the
  hop; "pick" says which frame when) with a layer of props on top ("overlay": the letter in her hand, a thought
  bubble...; "box" is where in the cell they can be), so the turn is stored once and not once per state. Beside each
  picture is its part map ("scale" times smaller, the part number times 40 in the red channel), which says what a
  click touched.
  Small pictures because Tk (the Mac) takes seconds to decode a tall one, and only a fraction of that for its parts.
  "density" pixels of the pictures make one point on the screen (2: sharp on a Mac's Retina screen, where one point
  is two pixels); the cell, the boxes and the part maps' scale are in the pictures' pixels.
- A plain Codex pet: one sprite sheet, 192x208 cells, rows idle / running-right / running-left / waving / jumping /
  failed / waiting / running / review. No part map: a click anywhere opens KOL Mail.
KOL Mail's state names are mapped onto whatever the pet has (FALLBACK). A pet.json that does not add up is refused
here, with a ValueError, rather than failing later in the window."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CODEX_ROWS = [  # (state, frames, durations) of the Codex v1 atlas
    ("idle", 6, [280, 110, 110, 140, 140, 320]),
    ("running-right", 8, [120] * 7 + [220]),
    ("running-left", 8, [120] * 7 + [220]),
    ("waving", 4, [140] * 3 + [280]),
    ("jumping", 5, [140] * 4 + [280]),
    ("failed", 8, [140] * 7 + [240]),
    ("waiting", 6, [150] * 5 + [260]),
    ("running", 6, [120] * 5 + [220]),
    ("review", 6, [150] * 5 + [280]),
]
MAX_FRAMES = 3600  # a minute at 60 a second: far more than any state needs

# what to show when a pet lacks a state, tried in order
FALLBACK = {
    "mail": ["jumping", "hop", "waving"],
    "notify": ["waving", "waiting"],
    "waiting": ["waiting"],
    "sleeping": ["idle"],
    "failed": ["failed"],
    "thinking": ["review", "waiting"],
    "sending": ["running"],
    "drag-left": ["running-left"],
    "drag-right": ["running-right"],
    "hop": ["jumping", "waving"],
    "pat": ["waving", "hop", "jumping"],
    "poke": ["waving", "hop", "jumping"],
    "giggle": ["jumping", "hop", "waving"],
    "spout": ["jumping", "hop", "waving"],
}


@dataclass(frozen=True)
class Layer:
    """Frames in one or more pictures: frame i is in bands[i // per], the (i % per)-th down (KOL Mail's) or across
    from `origin` (a Codex row); parts[k] is band k's part map."""
    bands: tuple[Path, ...]
    parts: tuple[Path, ...] = ()
    per: int = MAX_FRAMES
    origin: tuple[int, int] = (0, 0)
    across: bool = False
    box: tuple[int, int, int, int] | None = None  # props: where in the cell they can be

    def at(self, i: int, cell: tuple[int, int]) -> tuple[Path, int, int]:
        """The picture holding frame i, and the frame's top left corner in it (a whole number of cells)."""
        band, j = divmod(i, self.per)
        x, y = self.origin
        return self.bands[band], *((x + j * cell[0], y) if self.across else (x, y + j * cell[1]))


@dataclass(frozen=True)
class State:
    layer: Layer                              # the character (its own frames, or a shared clip)
    durations: tuple[float, ...]              # milliseconds per frame
    pick: tuple[int, ...] | None = None       # which frame of the layer at each step (None: 0, 1, 2...)
    overlay: Layer | None = None              # props drawn on top
    overlay_pick: tuple[int, ...] | None = None

    @property
    def frames(self) -> int:
        return len(self.durations)

    @property
    def loop_ms(self) -> float:
        return sum(self.durations)

    def frame_at(self, ms: float, loop: bool = True) -> int:
        """The step showing `ms` milliseconds after the state started (the last one, once a one-off has ended)."""
        if loop:
            ms %= max(1, self.loop_ms)
        if len(set(self.durations)) == 1:  # the usual case: an even frame rate
            return min(self.frames - 1, int(ms // self.durations[0]))
        for i, d in enumerate(self.durations):
            if ms < d:
                return i
            ms -= d
        return self.frames - 1

    def base_frame(self, i: int) -> int:
        return self.pick[i] if self.pick else i

    def over_frame(self, i: int) -> int:
        return self.overlay_pick[i] if self.overlay_pick else i


@dataclass(frozen=True)
class Sprites:
    cell: tuple[int, int]                     # a frame in the pictures, in pixels
    states: dict[str, State]
    name: str
    part_names: tuple[str, ...] = ()
    part_scale: int = 1
    density: int = 1                          # pixels of the pictures a point on the screen

    @property
    def size(self) -> tuple[int, int]:
        """Her size on the screen at the normal size, in points (96-DPI pixels on Windows)."""
        return self.cell[0] // self.density, self.cell[1] // self.density

    def resolve(self, state: str) -> str:
        """The state name this pet actually has for `state` (idle as the last resort)."""
        if state in self.states:
            return state
        for alt in FALLBACK.get(state, []):
            if alt in self.states:
                return alt
        return "idle"

    def get(self, state: str) -> State:
        return self.states[self.resolve(state)]

    @property
    def touchable(self) -> bool:
        return bool(self.part_names)

    def pictures_at_once(self, skip=()) -> int:
        """The most pictures one state plays, its clip's and its props' (the states in `skip` left out): a cache
        that holds that many keeps any of them decoded while it plays round and round."""
        most = 0
        for name, st in self.states.items():
            if name in skip:
                continue
            used = {st.layer.at(st.base_frame(i), self.cell)[0] for i in range(st.frames)}
            if st.overlay:
                used |= {st.overlay.at(st.over_frame(i), self.cell)[0] for i in range(st.frames)}
            most = max(most, len(used))
        return most

    def part_lookups(self, state: str, step: int, x: float, y: float) -> list[tuple[Path, int, int]]:
        """Where to read the part at (x, y) of a state's step (cell coordinates): the props on top first, then the
        character. Empty when the pet has no part map there."""
        st = self.get(state)
        if not (0 <= x < self.cell[0] and 0 <= y < self.cell[1]):
            return []
        pw, ph = self.cell[0] // self.part_scale, self.cell[1] // self.part_scale
        px, py = min(pw - 1, int(x // self.part_scale)), min(ph - 1, int(y // self.part_scale))
        out = []
        for layer, frame in ((st.overlay, st.over_frame(step) if st.overlay else 0), (st.layer, st.base_frame(step))):
            if layer and layer.parts:
                band, j = divmod(frame, layer.per)
                out.append((layer.parts[band], px, j * ph + py))
        return out

    def part_name(self, red: int) -> str:
        i = round(red / 40)
        return self.part_names[i] if 0 <= i < len(self.part_names) else ""


def _inside(folder: Path, name) -> Path:
    if not isinstance(name, str) or not name:
        raise ValueError("a picture's name must be text")
    path = (folder / name).resolve()
    if folder.resolve() not in path.parents:
        raise ValueError("a pet's pictures must stay inside its folder")
    if not path.is_file():
        raise ValueError(f"{name} is missing")
    return path


def _count(value, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{what} must be a whole number from {low} to {high}")
    return value


def _layer(folder: Path, spec: dict) -> tuple[Layer, int]:
    """A run of frames from pet.json, and how many frames it has."""
    frames = _count(spec.get("frames"), 1, MAX_FRAMES, "frames")
    per = _count(spec.get("per"), 1, MAX_FRAMES, "per")
    bands = spec.get("bands")
    if not isinstance(bands, list) or len(bands) != -(-frames // per):
        raise ValueError("bands do not fit the frames")
    pictures, parts = [], []
    for band in bands:
        if not isinstance(band, list) or len(band) not in (1, 2):
            raise ValueError("a band is [picture] or [picture, part map]")
        pictures.append(_inside(folder, band[0]))
        if len(band) == 2:
            parts.append(_inside(folder, band[1]))
    if parts and len(parts) != len(pictures):
        raise ValueError("part maps for some bands only")
    box = spec.get("box")
    if box is not None:
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, int) for v in box)):
            raise ValueError("box is [x0, y0, x1, y1]")
        box = tuple(box)
    return Layer(tuple(pictures), tuple(parts), per, box=box), frames


def _picks(spec: dict, steps: int, limit: int) -> tuple[int, ...] | None:
    """Which frame of a run of `limit` frames each of a state's `steps` shows (None: 0, 1, 2...)."""
    pick = spec.get("pick")
    if pick is None:
        if steps > limit:
            raise ValueError("more steps than frames, and no pick")
        return None
    if not isinstance(pick, list) or len(pick) != steps:
        raise ValueError("pick does not fit the steps")
    return tuple(_count(i, 0, limit - 1, "a picked frame") for i in pick)


def _kolmail(folder: Path, km: dict, name: str) -> Sprites:
    cell = (_count(km["cell"][0], 8, 2048, "cell width"), _count(km["cell"][1], 8, 2048, "cell height"))
    density = _count(km.get("density", 1), 1, 4, "density")
    if cell[0] % density or cell[1] % density:
        raise ValueError("the cell is not a whole number of points")
    clips = {key: _layer(folder, spec) for key, spec in (km.get("clips") or {}).items()}
    states = {}
    for key, s in km["states"].items():
        steps = _count(s.get("frames"), 1, MAX_FRAMES, "frames")
        fps = s.get("fps") or 60
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 1 <= fps <= 120:
            raise ValueError("fps must be from 1 to 120")
        durations = (1000.0 / fps,) * steps  # not rounded: 17 ms would be 58.8 a second
        if "bands" in s:
            layer, limit = _layer(folder, s)
        elif s.get("clip") in clips:
            layer, limit = clips[s["clip"]]
        else:
            raise ValueError(f"{key}: neither frames of its own nor a known clip")
        overlay = over_pick = None
        if "overlay" in s:
            overlay, over_frames = _layer(folder, s["overlay"])
            over_pick = _picks(s["overlay"], steps, over_frames)
        states[key] = State(layer, durations, _picks(s, steps, limit), overlay, over_pick)
    parts = km.get("parts") or {}
    names = tuple(str(n) for n in parts.get("names") or ())
    return Sprites(cell, states, name, names, _count(parts.get("scale", 1), 1, 8, "part scale"), density)


def _codex(folder: Path, meta: dict, name: str) -> Sprites:
    sheet = _inside(folder, str(meta.get("spritesheetPath") or "spritesheet.png"))
    cell = (192, 208)
    states = {key: State(Layer((sheet,), origin=(0, row * cell[1]), across=True), tuple(d))
              for row, (key, _, d) in enumerate(CODEX_ROWS)}
    return Sprites(cell, states, name)


def outfits(root: Path, first: str) -> list[tuple[str, str]]:
    """The pets in root (pet/) as (folder name, the outfit's name), `first` first: what her menu and the settings page
    offer. Only pet.json's names are read; a folder without a readable one is left out."""
    found = []
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        try:
            meta = json.loads((folder / "pet.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and folder.name.replace("-", "").isalnum():
            found.append((folder.name, str(meta.get("outfit") or meta.get("displayName") or folder.name)))
    return sorted(found, key=lambda o: o[0] != first)


def load(folder: Path) -> Sprites:
    try:
        meta = json.loads((folder / "pet.json").read_text(encoding="utf-8"))
        name = str(meta.get("displayName") or folder.name)
        km = meta.get("kolmail")
        if km is None:
            sprites = _codex(folder, meta, name)
        elif isinstance(km, dict) and km.get("version") == 2:
            sprites = _kolmail(folder, km, name)
        else:
            raise ValueError("a newer pet format than this KOL Mail knows")
    except (KeyError, TypeError, AttributeError, IndexError) as e:
        raise ValueError(f"pet.json does not add up: {e!r}") from e
    if "idle" not in sprites.states:
        raise ValueError("a pet needs an idle state")
    return sprites
