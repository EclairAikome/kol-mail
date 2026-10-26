"""A stand-in for the CodeMaker CLI, for tests and for CI's fresh installs (no login, no network).

    python fake_codemaker.py install <folder> [--version 1.15.11] [--log calls.jsonl] [--delete-seconds 0]

writes <folder>/codemaker (codemaker.cmd on Windows), a launcher with its settings baked in, so it also works where
no environment reaches it (the launchd service on a Mac). Every call is appended to the log as {"argv": [...]}.

It answers what KOL Mail and its installers ask: --version, quota, models, run (one JSON text event with a session
id) and session delete. Like CodeMaker before 1.16, an old version crashes on `run --agent` with "No context found
for instance"; a run whose agent leaves tools on answers something other than {"ok": "ok"}."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

MODELS = ["codemaker/gpt-5.6-terra", "codemaker/gpt-6.1-sol", "openai/gpt-6.1-sol"]


def install(folder: Path, version: str, log: Path, delete_seconds: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    # a copy next to the launcher: cmd.exe reads a .cmd in the console's code page, so a checkout path with
    # Chinese characters written into it would come out garbled
    me, py = folder / "fake_codemaker.py", Path(sys.executable).resolve()
    shutil.copy2(__file__, me)
    settings = {"FAKE_CM_LOG": str(log), "FAKE_CM_VERSION": version, "FAKE_CM_DELETE_SECONDS": str(delete_seconds)}
    if sys.platform == "win32":
        exe = folder / "codemaker.cmd"
        sets = "".join(f'@set "{k}={v}"\r\n' for k, v in settings.items())
        exe.write_text(f'@setlocal\r\n{sets}@"{py}" -I "{me}" %*\r\n', encoding="utf-8", newline="")
    else:
        exe = folder / "codemaker"
        sets = " ".join(f"{k}='{v}'" for k, v in settings.items())
        exe.write_text(f'#!/bin/sh\nexport {sets}\nexec "{py}" -I "{me}" "$@"\n', encoding="utf-8", newline="")
        exe.chmod(0o755)
    return exe


def main(args: list[str]) -> int:
    if args[:1] == ["install"]:
        opts = dict(zip(args[2::2], args[3::2]))
        exe = install(Path(args[1]), opts.get("--version", "1.18.31"),
                      Path(opts.get("--log", str(Path(args[1]) / "calls.jsonl"))), float(opts.get("--delete-seconds", 0)))
        print(exe)
        return 0
    version = os.environ.get("FAKE_CM_VERSION", "1.18.31")
    with open(os.environ["FAKE_CM_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args}) + "\n")
    if args[:1] == ["--version"]:
        print(version)
    elif args[:1] == ["quota"]:
        print("quota: plenty")
    elif args[:1] == ["models"]:
        print("\n".join(MODELS))
    elif args[:2] == ["session", "delete"]:
        time.sleep(float(os.environ.get("FAKE_CM_DELETE_SECONDS", "0")))  # still running when the caller moves on
    elif args[:1] == ["run"]:
        if "--agent" in args and tuple(int(p) for p in version.split(".")[:2]) < (1, 16):
            sys.stderr.write("Error: Unexpected error\nNo context found for instance\n"
                             "    at runLoop (B:/~BUN/root/codemaker.js:25:2045)\n")
            return 1
        run_dir = Path(args[args.index("--dir") + 1])
        config = json.loads((run_dir / "codemaker.json").read_text(encoding="utf-8"))
        agent = config.get("agent", {}).get(config.get("default_agent", "build"), {})
        sys.stdin.buffer.read()
        answer = {"ok": "ok"} if agent.get("tools") == {"*": False} else {"ok": "tools were left on"}
        print(json.dumps({"type": "text", "sessionID": "ses_fake1", "part": {"text": json.dumps(answer)}}), flush=True)
    else:
        sys.stderr.write(f"unknown command: {' '.join(args)}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
