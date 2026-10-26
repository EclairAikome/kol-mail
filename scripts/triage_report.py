"""Turn a colleague's error report from the POPO feedback group into a failing regression test skeleton.

    python scripts/triage_report.py <pasted.txt | ->  [--name short-name] [--force] [--dry-run]

The feedback webhook only sends: nothing collects the reports, so the input is the text copied out of the "KOL Mail
Feedback" group (one or several messages, each starting 【KOL Mail 匿名反馈 · 问题】 and ending (版本 x.y.z)), or
any pasted installer output. For each report this script:

1. Masks what may not go public before anything is written: e-mail addresses that are not example ones, Windows and
   Mac user folders, tokens and webhook addresses (the built-in rules of tools/publish.py), and every pattern in
   data/publish-denylist.txt when it exists. The test lands in tests/ and is published with the code.
2. Reads what it can: version, Windows or Mac, Python, CodeMaker and Claude Code versions, the installer step
   ([5/8]) and the error line (a traceback's last line, else the first line that says error, 失败, 超时 ...).
3. Gives the error a signature (the error with numbers, paths and quoted values taken out). A report whose signature
   is already in tests/regressions/, or that holds the words of a test's "Matches:" line, is a known bug: the script
   names that test and writes nothing (--force writes a new one anyway).
4. Writes tests/regressions/test_feedback_<date>_<name>.py: the masked report, what was read from it, and a test
   that fails until someone writes the reproduction with the fixtures in tests/regressions/conftest.py and fixes
   the bug. It never guesses a reproduction itself."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from publish import ALLOWED_EMAIL, BUILTIN, EMAIL  # noqa: E402  the release scan's own rules

OUT_DIR = ROOT / "tests" / "regressions"
DENYLIST = ROOT / "data" / "publish-denylist.txt"
HEADER = re.compile(r"【KOL Mail 匿名反馈[^】]*】")
VERSION = re.compile(r"[(（]版本\s*v?(\d+\.\d+\.\d+)[)）]")
ERROR_WORDS = re.compile(r"error|exception|traceback|failed|timed? ?out|rate limit|denied|refused|not found|"
                         r"错误|失败|出错|超时|报错|卡住|没找到|找不到|拒绝|不能用", re.IGNORECASE)
EXCEPTION_LINE = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Expired))(?::\s*(.*))?$")


def deny_rules() -> list[re.Pattern]:
    patterns = []
    if DENYLIST.exists():
        patterns = [l.strip() for l in DENYLIST.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.startswith("#")]
    return [re.compile(p, re.IGNORECASE) for p in patterns + BUILTIN]


def mask(text: str) -> str:
    # user folders become alex's, the one name the release scan lets through
    text = re.sub(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s\"']+", r"\1alex", text)
    text = re.sub(r"(?<![\w.])(/Users/)(?!Shared\b)[^/\s\"']+", r"\1alex", text)
    text = EMAIL.sub(lambda m: m.group(0) if ALLOWED_EMAIL.search(m.group(0)) else "someone@example.com", text)
    for rule in deny_rules():
        text = rule.sub("<redacted>", text)
    return text


def leaks(text: str) -> list[str]:
    """What the release scan would still refuse in this text (must be nothing after mask)."""
    found = [r.pattern for r in deny_rules() if r.search(text)]
    found += [m.group(0) for m in EMAIL.finditer(text) if not ALLOWED_EMAIL.search(m.group(0))]
    return found


def split_reports(text: str) -> list[str]:
    starts = [m.start() for m in HEADER.finditer(text)]
    if len(starts) < 2:
        return [text.strip()] if text.strip() else []
    return [text[a:b].strip() for a, b in zip(starts, starts[1:] + [len(text)])]


def error_line(text: str) -> str:
    lines = [l.rstrip() for l in text.splitlines()]
    for i, line in enumerate(lines):  # a traceback: its exception line is the error
        if line.strip().startswith("Traceback (most recent call last)"):
            for after in lines[i + 1:]:
                if EXCEPTION_LINE.match(after):
                    return after.strip()
    for line in lines:
        if EXCEPTION_LINE.match(line) or (ERROR_WORDS.search(line) and not HEADER.search(line)):
            return line.strip()[:300]
    return ""


def facts(text: str) -> dict:
    def first(pattern: str, flags: int = re.IGNORECASE) -> str:
        m = re.search(pattern, text, flags)
        return m.group(1) if m else ""
    platform = ""
    if re.search(r"[A-Za-z]:\\|PowerShell|\.ps1\b|WinError|\.exe\b|chcp", text, re.IGNORECASE):
        platform = "Windows"
    elif re.search(r"/Users/|launchd|install\.sh|zsh|macOS|osascript", text):
        platform = "macOS"
    python = first(r"\bPython\s?(3\.\d+(?:\.\d+)?)")
    if not python and (m := re.search(r"Python3(1\d)\b", text)):  # a path like ...\Python\Python312\python.exe
        python = "3." + m.group(1)
    step = re.findall(r"\[(\d)/8\]", text)
    return {
        "kind": first(r"【KOL Mail 匿名反馈\s*·\s*([^】]+)】", 0),
        "version": (VERSION.search(text) or [None, ""])[1],
        "platform": platform,
        "python": python,
        "codemaker": first(r"codemaker\S*\s*(?:v|version\s*)?(\d+\.\d+\.\d+)"),
        "claude": first(r"claude(?: code)?\s*(?:v|version\s*)?(\d+\.\d+\.\d+)"),
        "step": f"{step[-1]}/8" if step else "",
        "error": error_line(text),
    }


def signature(error: str) -> str:
    """The error without what changes from one machine or run to the next."""
    norm = error.lower()
    norm = re.sub(r"[a-z]:[\\/][^\s'\"]*|/[\w.-]+(?:/[\w.-]+)+", "<path>", norm)
    norm = re.sub(r"(['\"]).*?\1", "<value>", norm)
    norm = re.sub(r"0x[0-9a-f]+|\d+(?:\.\d+)*", "<n>", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def known(sig: str, report: str, out_dir: Path) -> Path | None:
    """The regression test this report is already about: same signature, or a "Matches: <words>" line of the test
    whose words appear in the report (for errors worded differently from one report to the next)."""
    lowered = report.lower()
    for path in sorted(out_dir.glob("test_*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if f"Signature: {sig}" in text:
            return path
        words = re.findall(r"^Matches: (.+?)(?:\"\"\"|''')?\s*$", text, re.M)  # the docstring may end on that line
        if any(w.strip().lower() in lowered for w in words if w.strip()):
            return path
    return None


def slug(name: str, info: dict, sig: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or info["error"]).lower())
    return "_".join(words[:6])[:50] or sig


def render(report: str, info: dict, sig: str, name: str) -> str:
    about = " · ".join(f"{label} {info[key]}" for key, label in (
        ("version", "KOL Mail"), ("platform", "on"), ("python", "Python"), ("codemaker", "CodeMaker"),
        ("claude", "Claude Code"), ("step", "installer step")) if info[key])
    return f'''"""Colleague feedback triaged {date.today().isoformat()}, not reproduced yet ({info["kind"] or "report"}).

Signature: {sig}
Seen with: {about or "nothing the report says"}
Error: {info["error"] or "(the report names none: read REPORT)"}

To do: replace the pytest.fail below with the smallest steps that fail on the current code the way the report
does, using the fixtures in conftest.py (ai_check, fake_codemaker, run_ps_functions); then fix the code until it
passes, and put the commit in this docstring. Add a line "Matches: <words of the error that never change>" so
scripts/triage_report.py knows later reports of it. REPORT is the message as copied, private details masked."""

import pytest

REPORT = {report!r}


def test_{name}():
    pytest.fail("not reproduced yet: write the reproduction (see this file's docstring)")
'''


def triage(text: str, out_dir: Path = OUT_DIR, name: str = "", force: bool = False, dry_run: bool = False) -> list[dict]:
    results = []
    for raw in split_reports(text):
        report = mask(raw)
        left = leaks(report)
        if left:  # mask() and the release scan disagree: never write it
            raise SystemExit(f"could not mask everything private in this report: {left}")
        info = facts(report)
        sig = signature(info["error"] or report)
        existing = known(sig, report, out_dir)
        if existing and not force:
            results.append({"signature": sig, "known": existing, "path": None, "facts": info})
            continue
        base = slug(name, info, sig)
        path = out_dir / f"test_feedback_{date.today():%Y%m%d}_{base}.py"
        n = 2
        while path.exists():
            path = out_dir / f"test_feedback_{date.today():%Y%m%d}_{base}_{n}.py"
            n += 1
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(render(report, info, sig, path.stem[len("test_"):]), encoding="utf-8", newline="\n")
        results.append({"signature": sig, "known": None, "path": path, "facts": info})
    return results


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="file with the copied group messages, or - for stdin")
    ap.add_argument("--name", default="", help="a short English name for the test file")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--force", action="store_true", help="write a test even when the signature is known")
    ap.add_argument("--dry-run", action="store_true", help="say what would be written, write nothing")
    args = ap.parse_args(argv)
    if args.source == "-":
        text = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    else:
        text = Path(args.source).read_text(encoding="utf-8-sig", errors="replace")
    results = triage(text, Path(args.out_dir), args.name, args.force, args.dry_run)
    # the reports are Chinese: a console in another code page (cp1252, GBK) must not crash the summary
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not results:
        print("nothing to triage: the text is empty")
        return 1
    for r in results:
        about = ", ".join(f"{k} {v}" for k, v in r["facts"].items() if v and k != "error")
        if r["known"]:
            print(f"known bug {r['signature']}: {r['known']} ({about})")
        else:
            verb = "would write" if args.dry_run else "wrote"
            print(f"{verb} {r['path']} (signature {r['signature']}; {about}; error: {r['facts']['error'] or '-'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
