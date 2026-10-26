"""Refuse a commit that changes any file's line endings.

    python tools/check_line_endings.py          staged changes (the pre-commit hook, .githooks/pre-commit)
    python tools/check_line_endings.py --all    every tracked text file must be LF in the repository (CI)

A file's line endings are LF, CRLF, mixed or none (no line break at all). A staged file whose endings differ from
the committed version fails, and so does a new text file that is not LF: the repository is all LF (.gitattributes
makes Windows scripts CRLF on disk only). Files git treats as binary (-text, NUL bytes) are skipped. A whole file
turned CRLF by an editor or by core.autocrlf once slipped into a release this way and made every line a change.

Turn the hook on in a clone with:  git config core.hooksPath .githooks"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, input=data, capture_output=True, check=True).stdout


def endings(blob: bytes) -> str:
    crlf = blob.count(b"\r\n")
    kinds = [k for k, n in (("crlf", crlf), ("lf", blob.count(b"\n") - crlf), ("cr", blob.count(b"\r") - crlf)) if n]
    return "mixed" if len(kinds) > 1 else kinds[0] if kinds else "none"


def binary_paths(paths: list[str]) -> set[str]:
    """Paths whose `text` attribute is unset (-text, binary), by the .gitattributes being committed."""
    if not paths:
        return set()
    out = git("check-attr", "--cached", "-z", "--stdin", "text", data="\0".join(paths).encode("utf-8") + b"\0")
    fields = out.decode("utf-8").split("\0")
    return {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == "unset"}


def blob(spec: str) -> bytes | None:
    try:
        return git("cat-file", "blob", spec)
    except subprocess.CalledProcessError:
        return None


def staged_problems() -> list[str]:
    # (old path, new path) for every staged addition, change, rename and copy; old path None for a new file
    out = git("diff", "--cached", "--name-status", "-z", "--diff-filter=ACMR", "-M").decode("utf-8").split("\0")
    changes, i = [], 0
    while i < len(out) - 1:
        status = out[i]
        if status[0] in "RC":
            changes.append((out[i + 1], out[i + 2]))
            i += 3
        else:
            changes.append((None if status == "A" else out[i + 1], out[i + 1]))
            i += 2
    skip = binary_paths([new for _, new in changes])
    problems = []
    if any(new == ".gitattributes" for _, new in changes):  # e.g. "* -text" would switch this check off
        problems.append(".gitattributes: the line-ending rules themselves change (check them, then commit with "
                        "--no-verify)")
    for old, new in changes:
        if new in skip:
            continue
        now = blob(f":{new}")
        if now is None or b"\0" in now:
            continue
        before = blob(f"HEAD:{old}") if old else None  # None too in a repository without commits
        was = endings(before) if before is not None and b"\0" not in before else None
        is_ = endings(now)
        if was is None:
            if is_ not in ("lf", "none"):
                problems.append(f"{new}: new file with {is_.upper()} line endings (the repository is LF)")
        elif "none" not in (was, is_) and was != is_:
            problems.append(f"{new}: line endings change from {was.upper()} to {is_.upper()}")
        elif was == "none" and is_ not in ("lf", "none"):
            problems.append(f"{new}: line endings change to {is_.upper()} (the repository is LF)")
    return problems


def tree_problems() -> list[str]:
    paths = [p for p in git("ls-files", "-z").decode("utf-8").split("\0") if p]
    skip = binary_paths(paths)
    problems = []
    for path in paths:
        if path in skip:
            continue
        data = blob(f":{path}")
        if data is None or b"\0" in data:
            continue
        kind = endings(data)
        if kind not in ("lf", "none"):
            problems.append(f"{path}: {kind.upper()} line endings in the repository (it is LF)")
    return problems


def main(argv: list[str]) -> int:
    problems = tree_problems() if "--all" in argv else staged_problems()
    if not problems:
        return 0
    print("Line endings changed, nothing committed:", file=sys.stderr)
    for p in problems:
        print("  " + p, file=sys.stderr)
    print("Convert the file back (LF), e.g. `git add --renormalize <file>` after fixing the editor setting; "
          "a deliberate change needs `git commit --no-verify` and a word in the commit message.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
