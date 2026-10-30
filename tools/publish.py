"""Export this developer checkout to the public repository, sign it and tag a release.

    python tools/publish.py <public-clone-dir> --author "Name <id+user@users.noreply.github.com>" [--push]

1. Copies every git-tracked file (never data/, never anything untracked) into the clone of the public repo.
2. Scans the copy for anything private: the patterns in data/publish-denylist.txt (one regex per line; the list
   itself stays out of git because it names the private strings), plus built-in checks for tokens, webhooks,
   Windows user folders, e-mail addresses that are not example ones and binary files (PNG pictures are allowed when
   they hold nothing but the picture: no text, EXIF or other metadata chunks). Any hit stops the release.
3. Writes MANIFEST.json (SHA-256 of every file) and MANIFEST.sig, signed with data/release-signing-key.pem. The
   updater on colleagues' machines only installs releases whose signature matches the pinned public key.
4. Commits as "Release vX.Y.Z" (version from VERSION) with the given author and, with --push, pushes and tags it.
   An existing tag is never moved. Creating the GitHub release (what the updater looks for) is
   `gh release create vX.Y.Z --repo owner/name --notes "..."`.
5. With --push, restarts the KOL Mail service running from this checkout (it never updates itself, so it would keep
   the old code in memory) through POST /api/restart, which waits until nothing is being sent, and waits until a new
   process answers. A service that is not running is left alone; a failed restart never fails the release.
"""

from __future__ import annotations

import argparse
import base64
import re
import shutil
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402

BUILTIN = [
    r"\d{6,12}:[A-Za-z0-9_-]{30,}",                    # Telegram bot token
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
    r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9-]{20,}",           # API keys (a word that merely ends in "sk" is none)
    r"open-apis/robots|/hook/[A-Za-z0-9-]{16,}",         # webhook addresses
    r"[A-Za-z]:[\\/]+Users[\\/]+(?!alex\b)[A-Za-z0-9._-]+",  # someone's Windows profile, either slash
    r"(?<![\w.])/Users/(?!alex\b|Shared\b)[A-Za-z0-9._-]+",  # someone's Mac home folder
    r"TELEGRAM_SESSION_STRING\s*=\s*\S{20,}",
    r"BEGIN [A-Z ]*PRIVATE KEY",
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL = re.compile(
    r"@([a-z0-9-]+\.)*example\.(com|org|net)$"          # documentation and test addresses
    r"|@(x\.com|y\.com|evil\.io|example-team\.com)$"       # test fixtures
    r"|^(mailer-daemon|postmaster|no-reply|noreply|no_reply|do_not_reply)@"  # machine senders
    r"|^(ana\.gamer|ana|bob|busy|done|fee|mixed|no|gone|someone|stranger)@gmail\.com$"
    # tests of the mailbox presets, and the add-mailbox placeholder, need a real provider's domain
    r"|^(a|li|li2|name)@163\.com$|^li@qq\.com$|^a@icloud\.com$|^x@outlook\.com$"
    # an address on a video site's domain must not be read as a link to it (tests/test_materials.py)
    r"|^gamer@youtube\.com$"
    r"|@users\.noreply\.github\.com$|^noreply@anthropic\.com$", re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".html", ".js", ".css", ".ps1", ".cmd", ".toml", ".cfg", ".ini", ".sh",
                 ".yml", ".yaml", ""}
MAX_PNG = 3 * 1024 * 1024  # the desktop pet's pictures: five frames each, under 0.5 MB
SKIP_SELF = {"tools/publish.py"}  # its own patterns would match themselves


PNG_CHUNKS = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"gAMA", b"cHRM", b"sRGB", b"pHYs", b"bKGD"}


def png_problem(raw: bytes) -> str | None:
    """Why this PNG may not go public, or None. Only the chunks that hold the picture itself are allowed: text,
    EXIF, colour profiles, provenance (C2PA) and editors' private chunks can all carry names, paths or places,
    and anything after the end chunk could carry anything at all."""
    import struct
    import zlib
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "不是 PNG"
    pos = 8
    while pos + 12 <= len(raw):
        length, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        end = pos + 12 + length
        if end > len(raw):
            return "文件被截断了"
        if struct.unpack(">I", raw[end - 4:end])[0] != zlib.crc32(raw[pos + 4:end - 4]):
            return f"{kind.decode('latin-1')} 块校验不对"
        if kind not in PNG_CHUNKS:
            return f"带有 {kind.decode('latin-1')} 块（文字、EXIF、配置文件之类的附加信息），先用工具去掉再发布"
        pos = end
        if kind == b"IEND":
            return None if pos == len(raw) else "图片结尾后面还藏着别的数据"
    return "没有结尾块"


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
        if path.suffix.lower() == ".png":
            raw = path.read_bytes()
            problem = png_problem(raw) or (f"超过 {MAX_PNG // 1024 // 1024} MB" if len(raw) > MAX_PNG else None)
            if problem:
                hits.append(f"{rel}: {problem}")
            continue
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


def restart_local_service(version: str, wait: float = 720, poll: float = 5):
    """The app waits up to 10 minutes for a send in progress before it exits; run.ps1 / launchd start it 15 s later."""
    import httpx
    import config
    port = int(os.environ.get("KOL_MAIL_PORT", config.S["port"]))
    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=base, headers={"x-kol-mail": "1", "origin": base}, timeout=10,
                          trust_env=False)  # never through a proxy from the environment

    def status() -> dict | None:
        try:
            r = client.get("/api/status")
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    with client:
        before = status()
        if before is None:
            print(f"本机 {base} 没有在运行的 KOL Mail，不用重启")
            return
        r = client.post("/api/restart", json={})
        if r.status_code != 200:
            print(f"本机服务没有重启（HTTP {r.status_code}：{r.text[:200]}），需要手动重启它才会用上 v{version}")
            return
        print("已请求本机服务重启，等它跑起新代码（有邮件正在发会先等发完）……")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(poll)
            now = status()
            if now and now.get("pid") != before.get("pid"):  # a new process, not a slow answer from the old one
                running = now.get("version")
                note = "" if running == version else f"（这个文件夹里的代码是 v{running}，发布的是 v{version}）"
                print(f"本机服务已重启，现在运行 v{running}{note}")
                return
        print(f"等了 {int(wait / 60)} 分钟本机服务还没重启完，去看一下 server.log")


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
    if a.push:
        try:
            restart_local_service(version)
        except Exception as e:  # noqa: BLE001  the release is out already; restarting is a convenience
            print(f"本机服务重启失败：{type(e).__name__}: {e}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
