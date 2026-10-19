"""Merge the installer's answers into data/settings.json without dropping anything else the user set.

    python -I tools/merge_settings.py <settings.json> <answers.json>

Only the keys present in answers are written (nested objects merge key by key); everything else stays as it was."""

import json
import os
import sys
from pathlib import Path


def merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def main():
    target, answers = Path(sys.argv[1]), Path(sys.argv[2])
    current = json.loads(target.read_text(encoding="utf-8-sig")) if target.exists() else {}
    new = merge(current, json.loads(answers.read_text(encoding="utf-8-sig")))
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


if __name__ == "__main__":
    main()
