"""Self-update from the GitHub releases of the public repository (settings.json update.repo = "owner/name").

The page asks once per day, on the first open. When a newer release exists, its source zip is downloaded and
checked before anything is replaced:
- MANIFEST.json lists every file with its SHA-256 and the version; MANIFEST.sig is an Ed25519 signature over it,
  made by tools/publish.py with a private key that only the maintainer's machine has. The public key is pinned
  below, so neither GitHub, a mirror nor anyone in between can slip in code: an unsigned or altered release is refused.
- The archive must hold exactly the signed files, no absolute or ".." paths, nothing under data/.
- Every .py file must compile.
Then dependencies are installed if requirements.txt changed, the old code is backed up, and the files are swapped
in place (restored from the backup if the swap fails). data/ is never touched. The process exits and the service
wrapper (service/run.ps1) starts the new code.

A checkout with a .git folder is a developer copy and is never updated by this."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

import httpx

PUBLIC_KEY = "r6rtHPMD2rAmovmVExDFPcHi0G8lFE/Z4/sQMrkNTj0="  # Ed25519, raw 32 bytes, base64
MANIFEST, SIGNATURE = "MANIFEST.json", "MANIFEST.sig"
KEEP = {"data", ".git", "__pycache__", ".venv", "venv"}  # never replaced or removed (compared case-insensitively)
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
LATEST_TAG = re.compile(r"/releases/tag/(v\d+\.\d+\.\d+)/?$")  # where github.com/<repo>/releases/latest redirects
MAX_ZIP = 20 * 1024 * 1024
MAX_UNPACKED = 60 * 1024 * 1024


class UpdateError(Exception):
    pass


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = VERSION_RE.match((text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(remote: str, local: str) -> bool:
    r, l = parse_version(remote), parse_version(local)
    return bool(r and l and r > l)


def is_dev_copy(base: Path) -> bool:
    return (base / ".git").exists()


def kept(rel: Path) -> bool:
    return rel.parts[0].lower() in KEEP


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- manifest (shared with tools/publish.py)

def content_digest(path: Path) -> str:
    """SHA-256 with CRLF read as LF: git may hand out text files with either line ending, the content is the same."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def build_manifest(root: Path, files: list[str], version: str) -> bytes:
    entries = {rel: content_digest(root / rel) for rel in sorted(files) if rel not in (MANIFEST, SIGNATURE)}
    return json.dumps({"version": version, "files": entries}, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")


def verify_manifest(manifest: bytes, signature_b64: bytes, public_key_b64: str) -> dict:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as e:
        raise UpdateError("缺少 cryptography，无法验证新版本的签名，没有安装") from e
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    manifest = manifest.replace(b"\r\n", b"\n")  # signed with LF; git may have checked it out with CRLF
    try:
        key.verify(base64.b64decode(signature_b64.strip()), manifest)
    except (InvalidSignature, ValueError) as e:
        raise UpdateError("新版本的签名对不上，可能被篡改过，没有安装") from e
    data = json.loads(manifest.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise UpdateError("新版本的文件清单格式不对")
    return data


class Updater:
    def __init__(self, base: Path, data: Path, repo: str, current: str, enabled: bool = True, mirror: str = ""):
        self.base, self.data, self.repo, self.current = base, data, repo.strip(), current
        self.mirror = mirror.rstrip("/") if mirror.startswith("https://") else ""
        self.enabled = enabled and bool(self.repo) and not is_dev_copy(base)
        self.state_file = data / "update.json"
        self.lock = asyncio.Lock()
        self.status = {"current": current, "latest": None, "state": "idle", "error": None, "checked": None}

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, **fields):
        state = {**self._load(), **fields}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def due_today(self) -> bool:
        return self._load().get("checked_on") != date.today().isoformat()

    def _url(self, url: str) -> str:
        return f"{self.mirror}/{url}" if self.mirror else url

    async def latest(self, client: httpx.AsyncClient) -> dict:
        """The newest release's tag, read from where github.com/<repo>/releases/latest redirects to. The REST API
        allows 60 anonymous calls an hour per IP, and a company network shares one IP among everybody, so it is
        only the fallback when the redirect gives no tag."""
        tag, notes, api_error = await self._latest_by_redirect(client), "", None
        if not tag:
            try:
                tag, notes = await self._latest_by_api(client)
            except UpdateError as e:
                api_error = e
        if not tag:
            raise api_error or UpdateError("查不到最新版本")
        if not parse_version(tag):
            raise UpdateError(f"最新版本号看不懂：{tag[:30]}")
        return {"tag": tag, "version": tag.lstrip("v"), "zip": f"https://codeload.github.com/{self.repo}/zip/refs/tags/{tag}",
                "notes": notes}

    async def _latest_by_redirect(self, client: httpx.AsyncClient) -> str:
        try:
            r = await client.head(self._url(f"https://github.com/{self.repo}/releases/latest"), follow_redirects=False)
        except httpx.HTTPError:
            return ""
        m = LATEST_TAG.search(r.headers.get("location", "")) if r.is_redirect else None
        return m.group(1) if m else ""

    async def _latest_by_api(self, client: httpx.AsyncClient) -> tuple[str, str]:
        r = await client.get(self._url(f"https://api.github.com/repos/{self.repo}/releases/latest"),
                             headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            raise UpdateError("仓库还没有发布任何版本")
        if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
            raise UpdateError("GitHub 限制了这个网络的查询次数，过一会儿会自动再试")
        if r.status_code != 200:
            raise UpdateError(f"GitHub 返回 HTTP {r.status_code}")
        data = r.json()
        return str(data.get("tag_name") or ""), str(data.get("body") or "")[:2000]

    async def check(self, force: bool = False, install: bool = True) -> dict:
        """Called on page open. Does nothing unless enabled and not yet checked today (or forced)."""
        if not self.enabled:
            self.status["state"] = "disabled"
            return self.status
        if self.lock.locked() or (not force and not self.due_today()):
            return self.status
        async with self.lock:
            self.status.update(state="checking", error=None)
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), follow_redirects=True) as client:
                    rel = await self.latest(client)
                    self._save(checked_on=date.today().isoformat(), latest=rel["version"])
                    self.status.update(latest=rel["version"], checked=time.strftime("%H:%M"), notes=rel["notes"])
                    if not newer(rel["version"], self.current):
                        self.status["state"] = "current"
                        return self.status
                    if not install:
                        self.status["state"] = "available"
                        return self.status
                    self.status["state"] = "downloading"
                    blob = await self.download(client, rel["zip"])
                self.status["state"] = "installing"
                await asyncio.to_thread(self.install, blob, rel["version"])
                self.status["state"] = "restarting"
                self._save(installed=rel["version"], installed_at=time.time())
            except UpdateError as e:
                self.status.update(state="error", error=str(e))
            except Exception as e:  # noqa: BLE001  network, disk, pip: try again tomorrow, keep working today
                self.status.update(state="error", error=f"{type(e).__name__}: {str(e)[:150]}")
        return self.status

    async def download(self, client: httpx.AsyncClient, url: str) -> bytes:
        r = await client.get(self._url(url))
        if r.status_code != 200:
            raise UpdateError(f"下载新版本失败：HTTP {r.status_code}")
        if len(r.content) > MAX_ZIP:
            raise UpdateError("新版本压缩包大得不正常，没有安装")
        return r.content

    def install(self, blob: bytes, version: str):
        staging = self.data / "update" / version
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            self.unpack(blob, staging)
            self.verify(staging, version)
            self.install_requirements(staging)
            self.swap(staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def unpack(self, blob: bytes, staging: Path):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise UpdateError("新版本解压后大得不正常，没有安装")
            tops = {i.filename.split("/", 1)[0] for i in z.infolist()}
            if len(tops) != 1:
                raise UpdateError("压缩包结构不对")
            top = tops.pop()
            for info in infos:
                rel = info.filename[len(top) + 1:]
                if not rel:
                    continue
                parts = Path(rel).parts
                if Path(rel).is_absolute() or ".." in parts or ":" in rel or "\\" in rel or kept(Path(rel)):
                    raise UpdateError(f"压缩包里有不安全的路径：{rel[:60]}")
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    def verify(self, staging: Path, version: str):
        try:
            manifest = (staging / MANIFEST).read_bytes()
            signature = (staging / SIGNATURE).read_bytes()
        except OSError as e:
            raise UpdateError("新版本没有签名清单，没有安装") from e
        data = verify_manifest(manifest, signature, PUBLIC_KEY)
        if data.get("version") != version:
            raise UpdateError("签名清单里的版本号和发布版本对不上")
        present = {p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file()} - {MANIFEST, SIGNATURE}
        listed = set(data["files"])
        if present != listed:
            raise UpdateError("压缩包里的文件和签名清单对不上：" + ", ".join(sorted(present ^ listed))[:150])
        for rel, want in data["files"].items():
            if content_digest(staging / rel) != want:
                raise UpdateError(f"文件内容和签名清单对不上：{rel}")
        if (staging / "VERSION").read_text(encoding="utf-8").strip() != version:
            raise UpdateError("压缩包里的版本号和发布版本对不上")
        if not (staging / "app.py").exists():
            raise UpdateError("压缩包里没有 app.py")
        for py in staging.rglob("*.py"):
            try:
                compile(py.read_bytes(), str(py), "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as e:
                raise UpdateError(f"新版本的 {py.name} 有语法错误，没有安装") from e

    def install_requirements(self, staging: Path):
        new, old = staging / "requirements.txt", self.base / "requirements.txt"
        if not new.exists() or (old.exists() and digest(old) == digest(new)):
            return
        import subprocess
        result = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                                 "-r", str(new)], capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            raise UpdateError("安装新依赖失败：" + (result.stderr or result.stdout)[-300:])

    def code_files(self, root: Path) -> set[Path]:
        return {p.relative_to(root) for p in root.rglob("*") if p.is_file() and not kept(p.relative_to(root))}

    def swap(self, staging: Path):
        """Back up the current code, copy every new file over the old one, remove old files the new version no
        longer has. Any failure puts the backup back, so a half-updated mix never runs."""
        new_files = self.code_files(staging)
        old_files = self.code_files(self.base)
        backup = self.data / "update" / "backup"
        shutil.rmtree(backup, ignore_errors=True)
        for rel in old_files:
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.base / rel, backup / rel)
        try:
            for rel in sorted(new_files):
                target = self.base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".new")
                shutil.copyfile(staging / rel, tmp)
                os.replace(tmp, target)
            for rel in old_files - new_files:
                if rel.name != "autoreply.json":
                    (self.base / rel).unlink(missing_ok=True)
        except Exception:
            for rel in new_files - old_files:
                (self.base / rel).unlink(missing_ok=True)
            for rel in old_files:
                shutil.copy2(backup / rel, self.base / rel)
            raise
