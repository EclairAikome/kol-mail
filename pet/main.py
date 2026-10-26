"""Start the desktop pet: python -I -X utf8 pet/main.py --port 8765 --data <app data folder> --boot <id>

Started by the app (petfeed.Keeper), never by hand in normal use. Exit codes: 0 the pet left on purpose (switched
off, or it belongs to an older run of the app), 3 another pet already runs for this app, 4 this computer cannot show
one (no Tk), anything else a crash."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path[0] = str(Path(__file__).resolve().parent.parent)  # import pet.* and petfeed from the app folder

import petfeed  # noqa: E402
from pet import sprites as sprites_mod  # noqa: E402

ANOTHER, UNSUPPORTED = 3, 4


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--boot", default="")
    args = p.parse_args()
    settings = petfeed.load_settings(args.data / "pet.json")
    folder = Path(__file__).resolve().parent / settings["pet"]
    if not (folder / "pet.json").exists():
        folder = Path(__file__).resolve().parent / petfeed.DEFAULTS["pet"]
    sprites = sprites_mod.load(folder)
    if sys.platform == "win32":
        from pet import win
        if not win.single_instance(args.port):
            return ANOTHER
        return win.run(args.port, args.boot, settings, sprites)
    if sys.platform == "darwin":
        try:
            from pet import mac
        except ImportError as e:  # a Python without Tk: the app keeps using Notification Center
            print("no desktop pet:", e)
            return UNSUPPORTED
        if not mac.single_instance(args.data, args.port):
            return ANOTHER
        return mac.run(args.port, args.boot, settings, sprites)
    print("no desktop pet on", sys.platform)
    return UNSUPPORTED


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001  the traceback goes to data/pet.log for whoever looks into it
        traceback.print_exc()
        sys.exit(1)
