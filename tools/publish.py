"""Export this developer checkout to the public repository, sign it and tag a release.

    python tools/publish.py <public-clone-dir> --author "Name <id+user@users.noreply.github.com>" [--push]

1. Copies every git-tracked file (never data/, never anything untracked) into the clone of the public repo.
2. Scans the copy for anything private: the patterns in data/publish-denylist.txt (one regex per line; the list
   itself stays out of git because it names the private strings), plus built-in checks for tokens, webhooks,
   Windows user folders, e-mail addresses that are not example ones and binary files. Any hit stops the release.
3. Writes MANIFEST.json (SHA-256 of every file) and MANIFEST.sig, signed with data/release-signing-key.pem. The
   updater on colleagues' machines only installs releases whose signature matches the pinned public key.
4. Commits as "Release vX.Y.Z" (version from VERSION) with the given author and, with --push, pushes and tags it.
   An existing tag is never moved. Creating the GitHub release (what the updater looks for) is
   `gh release create vX.Y.Z --repo owner/name --notes "..."`.
"""

from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402

BUILTIN = [
    r"\d{6,12}:[A-Za-z0-9_-]{30,}",                    # Telegram bot token
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
    r"sk-[A-Za-z0-9-]{20,}",                             # API keys
    r"open-apis/robots|/hook/[A-Za-z0-9-]{16,}",         # webhook addresses
    r"[A-Za-z]:[\\/]+Users[\\/]+(?!alex\b)[A-Za-z0-9._-]+",  # someone's Windows profile, either slash
    r"TELEGRAM_SESSION_STRING\s*=\s*\S{20,}",
    r"BEGIN [A-Z ]*PRIVATE KEY",
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL = re.compile(
    r"@([a-z0-9-]+\.)*example\.(com|org|net)$"          # documentation and test addresses
    r"|@(x\.com|y\.com|evil\.io|example-team\.com)$"       # test fixtures
    r"|^(mailer-daemon|postmaster|no-reply|noreply|no_reply|do_not_reply)@"  # machine senders
    r"|^(ana\.gamer|ana|bob|busy|done|fee|mixed|no|gone|someone|stranger)@gmail\.com$"
    r"|@users\.noreply\.github\.com$|^noreply@anthropic\.com$", re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".html", ".js", ".css", ".ps1", ".cmd", ".toml", ".cfg", ".ini", ""}
SKIP_SELF = {"tools/publish.py"}  # its own patterns would match themselves


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p and not p.startswith("data/")]


def scan(base: Path, files: list[str]) -> list[str]:
    deny_file = ROOT / "data" / "publish-denylist.txt"
    if not deny_file.exists():
        sys.exit(f"缺少 {deny_file}（每行一个不能公开的正则，比如你的名字、邮箱、内部域名）。没有它不发布。")
    patterns = [l.strip() for l in deny_file.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    rules = [re.compile(p, re.IGNORECASE) for p in patterns + BUILTIN]
    hits = []
    for rel in files:
        path = base / rel
        if path.suffix.lower() not in TEXT_SUFFIXES and rel not in (".gitignore", ".gitattributes"):
            hits.append(f"{rel}: 不是文本文件，扫描不了，不能放进公开仓库")
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            hits.append(f"{rel}: 含二进制内容，扫描不了")
            continue
        if rel in SKIP_SELF:
            continue
        for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            for rule in rules:
                if rule.search(line):
                    hits.append(f"{rel}:{n}: 命中 {rule.pattern[:40]} -> {line.strip()[:100]}")
            for m in EMAIL.finditer(line):
                if not ALLOWED_EMAIL.search(m.group(0)):
                    hits.append(f"{rel}:{n}: 邮箱 {m.group(0)}")
    return hits


def sign(clone: Path, files: list[str], version: str):
    key_file = ROOT / "data" / "release-signing-key.pem"
    if not key_file.exists():
        sys.exit(f"缺少签名私钥 {key_file}，不能发布")
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    manifest = updater.build_manifest(clone, files, version)
    signature = base64.b64encode(key.sign(manifest))
    updater.verify_manifest(manifest, signature, updater.PUBLIC_KEY)  # the pinned key must match this private key
    (clone / updater.MANIFEST).write_bytes(manifest)
    (clone / updater.SIGNATURE).write_bytes(signature + b"\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clone", type=Path)
    ap.add_argument("--author", required=True, help='"Name <id+user@users.noreply.github.com>"')
    ap.add_argument("--push", action="store_true")
    a = ap.parse_args()
    clone = a.clone.resolve()
    if not (clone / ".git").exists():
        sys.exit(f"{clone} 不是公开仓库的 git 克隆")
    if not re.fullmatch(r"[^<>]+ <[^<>@\s]+@users\.noreply\.github\.com>", a.author.strip()):
        sys.exit("--author 要用 GitHub 的 noreply 邮箱，公开仓库的提交记录里不能有真实邮箱")
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
        sys.exit("开发仓库还有没提交的改动，先提交再发布")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not updater.parse_version(version):
        sys.exit(f"VERSION 写的 {version} 不是 X.Y.Z")
    tags = subprocess.run(["git", "tag", "-l", f"v{version}"], cwd=clone, capture_output=True, text=True).stdout.strip()
    if tags:
        sys.exit(f"v{version} 已经发布过，发布新版本先改 VERSION")
    files = tracked()
    for item in clone.iterdir():  # the export is exactly the tracked files: removed files disappear too
        if item.name != ".git":
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    for rel in files:
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    hits = scan(clone, files)
    if hits:
        print("\n".join(hits))
        sys.exit(f"发现 {len(hits)} 处可能不能公开的内容，没有发布")
    sign(clone, files, version)
    name, email = re.fullmatch(r"([^<>]+) <([^<>]+)>", a.author.strip()).groups()
    git = lambda *args: subprocess.run(["git", "-c", f"user.name={name}", "-c", f"user.email={email}", *args],
                                       cwd=clone, check=True)
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=clone).returncode == 0:
        print("没有变化")
    else:
        git("commit", "-q", "-m", f"Release v{version}")
    if a.push:
        git("tag", f"v{version}")
        git("push", "-q", "origin", "HEAD")
        git("push", "-q", "origin", f"v{version}")
    print(f"导出完成：v{version}，{len(files)} 个文件，扫描干净，已签名")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
