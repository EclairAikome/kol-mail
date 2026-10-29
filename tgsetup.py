"""Telegram from the settings page, so a colleague never needs a command line: install chigwell/telegram-mcp (the
Telegram backend app.py runs as a child process) and log the colleague's own Telegram account in.

Where the component lives: settings paths.telegram_mcp when set (the owner's hand-made checkout), else
data/telegram-mcp, which KOL Mail's self-update never touches. The install downloads the one commit the owner tested
and checks every file against a digest of that commit, so neither GitHub, a mirror nor anyone in between can change
telegram-mcp's own code. Its dependencies come from PyPI (or settings update.pip_index), the direct ones at the owner's
versions, the same trust KOL Mail's own installer gives its requirements. A venv is built next to it with the Python
KOL Mail runs on, and only then is the whole folder moved into place: a half-made install never shows up as installed.

The login runs in tglogin.py, a helper started with the component's Python (Telethon lives there, not in KOL Mail's
venv), because Telethon keeps the code's hash on its client between "send me a code" and "here is the code". On
success data/telegram/.env gets TELEGRAM_SESSION_STRING (the login app.py hands telegram-mcp) and this account's
TELEGRAM_API_ID / TELEGRAM_API_HASH, which telegram-mcp needs and a fresh install has nowhere else; backend_env()
passes those to the child. The owner's checkout keeps its own .env for them, untouched. The code, the 2FA password,
the API hash and the session string are never logged and never sent back to the page."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
import zlib
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import accounts
import config

BASE = Path(__file__).resolve().parent
HELPER = BASE / "tglogin.py"
COMMIT = "6b925e5ed86435b4285f063a561f9ea284b3e36e"  # the owner's tested checkout
ZIP_URL = f"https://codeload.github.com/chigwell/telegram-mcp/zip/{COMMIT}"
# tree_digest() of that commit's files: codeload's zip and `git archive` of the owner's checkout give the same value
TREE_SHA256 = "96876a2f9dd82ac1f45f5d60137461d3807b302dc2430e9ca6c54f54a59cb36e"
MAX_ZIP = 10 * 1024 * 1024  # the commit's zip is 1.3 MB
MAX_UNPACKED = 40 * 1024 * 1024  # 2.6 MB unpacked
MAX_ENTRIES = 2000  # 129 entries
# Its requirements.txt has lower bounds only (a Telethon 2 would break it); these are the owner's venv's versions.
PINS = ("telethon==1.45.0", "mcp==1.30.0", "pillow==12.3.0", "pypdf==6.19.0", "python-dotenv==1.2.2", "dotenv==0.9.9",
        "httpx==0.28.1", "python-json-logger==4.1.0", "qrcode==8.2")
PIP_TIMEOUT = 20 * 60
LOGIN_IDLE = 10 * 60  # a login helper nobody talked to for this long is stopped
HELPER_GRACE = 5  # seconds a helper gets to disconnect from Telegram on its own before it is ended
ME_RETRY = 10 * 60  # how often a missing name is looked up again

# all used with fullmatch; ASCII so a full-width or Arabic-Indic digit is not a "digit"
API_ID = re.compile(r"\d{1,12}", re.ASCII)
API_HASH = re.compile(r"[0-9a-f]{32}", re.ASCII)
PHONE = re.compile(r"\+\d{7,15}", re.ASCII)
CODE = re.compile(r"\d{4,8}", re.ASCII)
SESSION = re.compile(r"[A-Za-z0-9_\-+/=]{20,4000}", re.ASCII)
RETRYABLE = {"code_invalid", "password_invalid"}  # tglogin.py keeps the login going after these
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}

STAGES = {"downloading": "正在下载 Telegram 组件", "unpacking": "正在核对并解压", "venv": "正在创建 Python 环境",
          "pip": "正在安装依赖，第一次要几分钟", "finishing": "正在收尾", "done": "装好了", "failed": "安装失败"}


class SetupError(Exception):
    """A refusal the page shows as it is: {"error": message, **extra} with this HTTP status."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


class InstallError(Exception):
    pass


class HelperFailed(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def venv_python(repo: Path) -> Path:
    return repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def base_python() -> str:
    """The Python KOL Mail runs on (its own venv's; venv builds the new one on that venv's base Python)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))  # pythonw has no console streams to read progress from
    return str(exe)


def mask_phone(phone: str) -> str:
    digits = phone.lstrip("+")
    if len(digits) <= 4:
        return "+" + "*" * len(digits)
    return "+" + digits[:2] + "*" * (len(digits) - 4) + digits[-2:]


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines (the first one of a key wins, as app.py's telegram_session() reads it); comments, `export`
    and quotes as python-dotenv allows them in telegram-mcp's own .env."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out.setdefault(key, value)
    return out


def write_private(path: Path, text: str) -> None:
    """Atomic, and readable by this user only where the OS lets a file say so (POSIX modes). On Windows the file
    inherits the user profile's ACL, which already keeps other users out."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):  # Windows refuses the replace while another process has the file open for a moment
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def move(src: Path, dst: Path) -> None:
    """os.replace, retried for a few seconds: on Windows a virus scanner or the search indexer often holds a file of
    a venv that was just made."""
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


def write_env(path: Path, changes: dict[str, str | None]) -> None:
    """Set the given keys (None removes them), keeping every other line and the file's line endings. A key that
    appears twice keeps one line, so app.py's first-match reader and this file agree."""
    for value in changes.values():
        if value is not None and ("\n" in value or "\r" in value):
            raise ValueError("a .env value cannot hold a line break")
    try:
        text = path.read_bytes().decode("utf-8-sig")  # bytes: read_text would turn CRLF into LF before we see it
    except FileNotFoundError:
        text = ""
    newline = "\r\n" if "\r\n" in text else "\n"
    out, seen = [], set()
    for line in text.splitlines():
        key = line.split("=", 1)[0] if "=" in line else None
        if key in changes:
            if key not in seen and changes[key] is not None:
                out.append(f"{key}={changes[key]}")
            seen.add(key)
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in changes.items() if k not in seen and v is not None]
    write_private(path, newline.join(out) + newline if out else "")


def clean_me(doc) -> dict | None:
    """{id, name, username} from tglogin.py or telegram-mcp's get_me (which also carries type and phone: dropped)."""
    if not isinstance(doc, dict):
        return None
    try:
        user_id = int(doc.get("id"))
    except (TypeError, ValueError):
        return None
    name = _CONTROL.sub("", str(doc.get("name") or "")).strip()[:64]
    username = _CONTROL.sub("", str(doc.get("username") or "")).strip().lstrip("@")[:64] or None
    return {"id": user_id, "name": name, "username": username}


# ---------------------------------------------------------------- the component's zip

def tree_digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(files[rel]).hexdigest().encode() + b"\n")
    return h.hexdigest()


def _unsafe(name: str) -> bool:
    parts = name.rstrip("/").split("/")
    return (not name or "\\" in name or ":" in name or "\0" in name or name.startswith("/")
            or any(p in ("", ".", "..") for p in parts))


def unpack(blob: bytes, dest: Path, expect: str | None) -> None:
    """Every check happens before anything is written: a real zip with one top folder, no absolute, "..", drive or
    backslash path and no link, sane sizes, and (expect) exactly the tested commit's files."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise InstallError("下载的文件不是完整的压缩包，请再试一次") from e
    with z:
        infos = z.infolist()
        if not infos or len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise InstallError("压缩包大小不正常，没有安装")
        for info in infos:
            if _unsafe(info.filename) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise InstallError(f"压缩包里有不安全的路径，没有安装：{info.filename[:80]}")
        tops = {i.filename.split("/", 1)[0] for i in infos}
        if len(tops) != 1:
            raise InstallError("压缩包结构不对，没有安装")
        files, total = {}, 0
        for info in infos:
            if info.is_dir():
                continue
            rel = info.filename.split("/", 1)[1] if "/" in info.filename else ""
            if not rel:
                raise InstallError("压缩包结构不对，没有安装")
            try:
                data = z.read(info)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as e:  # cut short or damaged
                raise InstallError("下载的文件不是完整的压缩包，请再试一次") from e
            total += len(data)
            if total > MAX_UNPACKED:
                raise InstallError("压缩包大小不正常，没有安装")
            files[rel] = data
    if expect and tree_digest(files) != expect:
        raise InstallError("下载的组件和测试过的版本对不上，没有安装")
    if "main.py" not in files or "requirements.txt" not in files:
        raise InstallError("压缩包里没有 main.py 或 requirements.txt，没有安装")
    root = dest.resolve()
    for rel, data in files.items():
        target = (dest / rel).resolve()
        if root not in target.parents:  # the name checks above already rule this out
            raise InstallError(f"压缩包里有不安全的路径，没有安装：{rel[:80]}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


# ---------------------------------------------------------------- the login helper process

class Helper:
    """A running tglogin.py: a JSON line in, a JSON line out."""

    def __init__(self, proc):
        self.proc = proc

    async def ask(self, msg: dict | None, timeout: float) -> dict:
        """Send msg (None: only read, for the one-shot modes) and return the reply."""
        try:
            if msg is not None:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        except asyncio.TimeoutError as e:
            raise HelperFailed("Telegram 太久没有回应，请检查这台电脑能不能上 Telegram，再重新发送验证码") from e
        except (BrokenPipeError, ConnectionResetError):
            line = b""
        if not line:
            raise HelperFailed("登录助手意外退出了，请重新发送验证码；一直这样的话请重新安装 Telegram 组件")
        try:
            doc = json.loads(line)
        except ValueError as e:
            raise HelperFailed("登录助手的回复看不懂，请重新安装 Telegram 组件") from e
        if not isinstance(doc, dict):
            raise HelperFailed("登录助手的回复看不懂，请重新安装 Telegram 组件")
        return doc

    async def close(self):
        """Let it disconnect from Telegram on its own (end of stdin), else end it."""
        if self.proc.returncode is not None:
            return
        with contextlib.suppress(Exception):
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), HELPER_GRACE)
        except asyncio.TimeoutError:
            await accounts.kill_tree(self.proc)


# ---------------------------------------------------------------- the setup itself

class Setup:
    def __init__(self, data: Path):
        self.data = data
        self.dir = data / "telegram"
        self.env_file = self.dir / ".env"  # app.py's TG_SESSION_FILE
        self.me_file = self.dir / "me.json"
        self.home = data / "telegram-mcp"
        self.python = base_python()
        self.job = {"stage": None, "text": "", "line": "", "error": None, "started": None, "finished": None}
        self.job_task: asyncio.Task | None = None
        self.helper: Helper | None = None
        self.step: str | None = None  # "code" or "password" while a login waits for the person
        self.phone_masked = ""
        self.api: tuple[str, str] | None = None  # the login's API ID and hash, kept until it is written to .env
        self.seen = 0.0  # monotonic time of the login's last request
        self.lock = asyncio.Lock()  # one login request at a time
        self.me_tried = -ME_RETRY
        self.tasks: set[asyncio.Task] = set()
        self.on_change = None
        self.get_me = None
        self.log = lambda *args: print(time.strftime("%H:%M:%S"), *args, flush=True)

    # ------------------------------------------------ where things are

    @staticmethod
    def settings() -> dict:
        try:
            return config.load()  # read fresh: an install in this process may just have set paths.telegram_mcp
        except (OSError, ValueError):
            return config.S

    def location(self) -> Path:
        configured = os.environ.get("KOL_TELEGRAM_MCP") or self.settings()["paths"].get("telegram_mcp") or ""
        return Path(configured) if configured else self.home

    def repo_path(self) -> Path | None:
        """The telegram-mcp folder app.py should run, or None while it is not installed."""
        loc = self.location()
        return loc if (loc / "main.py").is_file() and venv_python(loc).is_file() else None

    def session(self) -> str | None:
        return read_env(self.env_file).get("TELEGRAM_SESSION_STRING") or None

    def backend_env(self) -> dict[str, str]:
        """This account's API ID and hash for the telegram-mcp child (process env beats its own .env). Empty for the
        owner's setup, whose keys live in the checkout's .env."""
        values = read_env(self.env_file)
        api_id, api_hash = values.get("TELEGRAM_API_ID", ""), values.get("TELEGRAM_API_HASH", "")
        if API_ID.fullmatch(api_id) and API_HASH.fullmatch(api_hash.lower()):
            return {"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": api_hash}
        return {}

    def saved_api(self) -> tuple[str, str] | None:
        """API ID and hash already on this machine: KOL Mail's own, else the owner's checkout's."""
        env = self.backend_env()
        if env:
            return env["TELEGRAM_API_ID"], env["TELEGRAM_API_HASH"]
        values = read_env(self.location() / ".env")
        api_id, api_hash = values.get("TELEGRAM_API_ID", ""), values.get("TELEGRAM_API_HASH", "").lower()
        return (api_id, api_hash) if API_ID.fullmatch(api_id) and API_HASH.fullmatch(api_hash) else None

    def me(self) -> dict | None:
        try:
            return clean_me(json.loads(self.me_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def component(self) -> str:
        if self.job_task is not None and not self.job_task.done():
            return "installing"
        if self.repo_path() is not None:
            return "ok"
        return "failed" if self.job["stage"] == "failed" else "missing"

    def status(self) -> dict:
        logged_in = bool(self.session())
        return {"component": self.component(), "install": dict(self.job), "logged_in": logged_in,
                "me": self.me() if logged_in else None, "api_ready": self.saved_api() is not None,
                "login": {"step": self.step, "phone": self.phone_masked} if self.helper else {"step": None}}

    # ------------------------------------------------ install

    def _stage(self, stage: str, line: str = ""):
        self.job.update(stage=stage, text=STAGES.get(stage, ""), line=line)

    def start_install(self):
        if self.job_task is not None and not self.job_task.done():
            return  # already running: the page just polls
        if self.repo_path() is not None:
            raise SetupError("Telegram 组件已经装好了", 409)
        if self.location() != self.home:
            # A hand-made checkout (the owner's) that is broken right now: installing here and repointing settings
            # would leave its .env, with the API keys, behind.
            raise SetupError(f"设置里的 Telegram 组件（{self.location()}）不完整，请先修好它，"
                             "或者清空 settings.json 里的 paths.telegram_mcp 再安装", 409)
        self.job = {"stage": None, "text": "", "line": "", "error": None, "started": time.time(), "finished": None}
        self._stage("downloading")
        self.job_task = asyncio.create_task(self._install())

    async def _install(self):
        # No longer than "telegram-mcp": pip's deepest files must stay under Windows' 260-character path limit here
        # as well as in the final place.
        staging = self.data / f".tgnew-{secrets.token_hex(2)}"
        self.log("telegram component install started")
        try:
            await asyncio.to_thread(self._clean_leftovers)
            await self._fetch(staging)
            self._stage("venv")
            code, out = await self._run([self.python, "-I", "-X", "utf8", "-m", "venv", str(staging / ".venv")], 600)
            py = venv_python(staging)
            if code != 0 or not py.is_file():
                raise InstallError("创建 Python 环境失败：" + (out[-300:] or f"退出码 {code}"))
            self._stage("pip")
            await self._pip(py, staging)
            self._stage("finishing")
            await asyncio.to_thread(self._swap, staging)
            try:
                config.update(["paths", "telegram_mcp"], str(self.home))
            except (OSError, ValueError) as e:
                raise InstallError(f"组件装好了，但 settings.json 写不进去：{e}") from e
        except Exception as e:  # noqa: BLE001  any failure ends the job with its reason on the page
            error = str(e) if isinstance(e, InstallError) else f"{type(e).__name__}: {str(e)[:200]}"
            self.job.update(stage="failed", text=STAGES["failed"], error=error, finished=time.time())
            self.log("telegram component install failed", error)
        else:
            self.job.update(stage="done", text=STAGES["done"], line="", finished=time.time())
            self.log("telegram component installed", self.home)
            await self._changed()
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)

    def _clean_leftovers(self):
        """Folders of an install that was cut off (KOL Mail restarted in the middle)."""
        for pattern in (".tgnew-*", ".tgold-*"):
            for p in self.data.glob(pattern):
                shutil.rmtree(p, ignore_errors=True)

    async def _fetch(self, staging: Path):
        """The pinned zip unpacked into staging: straight from GitHub, then through the mirror KOL Mail's updates use
        (update.mirror) when one is set. The digest check in unpack() makes the source irrelevant to what gets
        installed, so a page of HTML or a cut-off zip from one source just moves on to the next."""
        mirror = str(self.settings().get("update", {}).get("mirror") or "").rstrip("/")
        urls = [ZIP_URL] + ([f"{mirror}/{ZIP_URL}"] if mirror.startswith("https://") else [])
        error = None
        for url in urls:
            self._stage("downloading")
            try:
                blob = await self._download(url)
                self._stage("unpacking")
                await asyncio.to_thread(unpack, blob, staging, TREE_SHA256)
                return
            except InstallError as e:
                error = e
                await asyncio.to_thread(shutil.rmtree, staging, True)
        raise error

    async def _download(self, url: str) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), follow_redirects=True) as client:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        raise InstallError(f"下载 Telegram 组件失败：HTTP {r.status_code}")
                    chunks, size = [], 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_ZIP:
                            raise InstallError("下载的压缩包大得不正常，没有安装")
                        chunks.append(chunk)
        except httpx.HTTPError as e:
            raise InstallError(f"连不上 GitHub，下载 Telegram 组件失败（{type(e).__name__}），换个网络再试") from e
        return b"".join(chunks)

    def _pip_index(self) -> str:
        """settings update.pip_index (https only: pip ignores a plain-http index without --trusted-host). Without it
        pip uses its own configuration (pip.ini, PIP_INDEX_URL), as KOL Mail's installer does."""
        index = str(self.settings().get("update", {}).get("pip_index") or "").strip()
        return index if index.startswith("https://") else ""

    async def _pip(self, py: Path, staging: Path):
        pins = staging / ".kol-mail-pins.txt"
        pins.write_text("\n".join(PINS) + "\n", encoding="utf-8")
        index = self._pip_index()
        # -X utf8: -I drops PYTHONIOENCODING, and pip's progress lines would reach the page in the ANSI code page
        args = [str(py), "-I", "-X", "utf8", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                "--progress-bar", "off", "-r", str(staging / "requirements.txt"), "-c", str(pins)]
        code, out = await self._run(args + (["-i", index] if index else []), PIP_TIMEOUT)
        pins.unlink(missing_ok=True)
        if code != 0:
            raise InstallError("安装依赖失败：" + (out[-400:] or f"退出码 {code}"))
        code, out = await self._run([str(py), "-I", "-X", "utf8", "-c", "import telethon, mcp, dotenv, PIL, pypdf"],
                                    120)
        if code != 0:
            raise InstallError("依赖装完了，但是导入失败：" + out[-300:])

    async def _run(self, args: list[str], timeout: float) -> tuple[int, str]:
        """Exit code and the last lines of a command's output; each line shows on the page as it comes."""
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
        env.update(PYTHONIOENCODING="utf-8", PIP_NO_INPUT="1")
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env, limit=1024 * 1024, **_NO_WINDOW)
        tail: collections.deque[str] = collections.deque(maxlen=40)

        async def pump():
            while line := await proc.stdout.readline():
                text = line.decode("utf-8", "replace").strip()
                if text:
                    tail.append(text)
                    self.job["line"] = text[:200]
            await proc.wait()

        try:
            await asyncio.wait_for(pump(), timeout)
        except asyncio.TimeoutError:
            await accounts.kill_tree(proc)
            return -1, f"{timeout / 60:.0f} 分钟还没完成，停下了"
        return proc.returncode, "\n".join(tail)

    def _swap(self, staging: Path):
        old = None
        if self.home.exists():  # a broken earlier install (a working one is never replaced)
            old = self.data / f".tgold-{secrets.token_hex(2)}"
            try:
                move(self.home, old)
            except OSError as e:
                raise InstallError("旧的 Telegram 组件文件正被占用，重启 KOL Mail 后再安装") from e
        try:
            move(staging, self.home)
        except OSError as e:
            if old is not None:
                move(old, self.home)
            raise InstallError(f"放置 Telegram 组件失败：{e}") from e
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)

    # ------------------------------------------------ login

    async def _start_helper(self, mode: str, env: dict[str, str] | None = None) -> Helper:
        repo = self.repo_path()
        if repo is None:
            raise SetupError("先安装 Telegram 组件", 409)
        # none of KOL Mail's own TELEGRAM_* settings, only what this run is about
        child_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("TELEGRAM_") and k not in ("PYTHONHOME", "PYTHONPATH")}
        child_env.update(env or {})
        child_env["PYTHONIOENCODING"] = "utf-8"
        proc = await asyncio.create_subprocess_exec(
            str(venv_python(repo)), "-I", str(HELPER), mode, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=child_env, **_NO_WINDOW)
        return Helper(proc)

    async def _end_login(self):
        helper, self.helper, self.step, self.phone_masked, self.api = self.helper, None, None, "", None
        if helper is not None:
            await helper.close()

    async def _expire_idle(self):
        if self.helper is not None and time.monotonic() - self.seen > LOGIN_IDLE:
            await self._end_login()
            self.log("telegram login helper idle, stopped")
            raise SetupError("等太久了，验证码已经作废，请重新发送验证码", 409)

    async def _reap(self, helper: Helper):
        """Stop a login helper nobody talked to for LOGIN_IDLE, so it never holds a half-done login for long."""
        while self.helper is helper:
            left = self.seen + LOGIN_IDLE - time.monotonic()
            if left > 0:
                await asyncio.sleep(min(left + 0.05, 60))
                continue
            async with self.lock:
                if self.helper is not helper:
                    return
                if time.monotonic() - self.seen < LOGIN_IDLE:  # a request came in while we waited for the lock
                    continue
                await self._end_login()
                self.log("telegram login helper idle, stopped")
            return

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _ask(self, msg: dict, timeout: float) -> dict:
        """One request to the login helper. A refusal Telegram lets the person retry keeps the helper; any other
        ends the login."""
        try:
            doc = await self.helper.ask(msg, timeout)
        except HelperFailed as e:
            await self._end_login()
            raise SetupError(str(e), 502) from e
        self.seen = time.monotonic()
        if doc.get("ok"):
            return doc
        reason = str(doc.get("code") or "")
        if reason not in RETRYABLE:
            await self._end_login()
        extra = {"reason": reason}
        if reason == "flood":
            if isinstance(doc.get("wait"), int):
                extra["wait"] = doc["wait"]
            raise SetupError(str(doc.get("error") or "Telegram 暂时不让这个号再请求，请晚点再试"), 429, **extra)
        status = 502 if reason in ("network", "telegram", "no_telethon", "") else 400
        raise SetupError(str(doc.get("error") or "Telegram 登录失败"), status, **extra)

    async def login_start(self, body: dict) -> dict:
        api_id = str(body.get("api_id") or "").strip()
        api_hash = str(body.get("api_hash") or "").strip().lower()
        phone = re.sub(r"[\s\-()]", "", str(body.get("phone") or ""))
        if not api_id and not api_hash:
            saved = self.saved_api()
            if saved is None:
                raise SetupError("先填 API ID 和 API Hash")
            api_id, api_hash = saved
        elif not API_ID.fullmatch(api_id):
            raise SetupError("API ID 应该是一串数字，从 my.telegram.org 的 api_id 那一栏复制")
        elif not API_HASH.fullmatch(api_hash):
            raise SetupError("API Hash 应该是 32 位的字母和数字，从 my.telegram.org 的 api_hash 那一栏复制")
        if not PHONE.fullmatch(phone):
            raise SetupError("手机号要带国家区号，以 + 开头，比如 +8613812345678")
        if self.session():
            raise SetupError("已经登录了，要换号请先退出登录", 409)
        async with self.lock:
            await self._end_login()
            self.helper = await self._start_helper("login")
            self.seen = time.monotonic()
            self._spawn(self._reap(self.helper))
            doc = await self._ask({"cmd": "start", "api_id": int(api_id), "api_hash": api_hash, "phone": phone}, 100)
            self.step, self.phone_masked, self.api = "code", mask_phone(phone), (api_id, api_hash)
            self.log("telegram login: code sent to", self.phone_masked, f"(via {doc.get('via')})")
            return {"ok": True, "step": "code", "via": doc.get("via"), "phone": self.phone_masked}

    async def login_code(self, body: dict) -> dict:
        code = re.sub(r"[\s\-]", "", str(body.get("code") or ""))
        if not CODE.fullmatch(code):
            raise SetupError("验证码是几位数字，照 Telegram 里收到的填")
        async with self.lock:
            await self._expire_idle()
            if self.helper is None or self.step != "code":
                raise SetupError("请先发送验证码", 409)
            doc = await self._ask({"cmd": "code", "code": code}, 60)
            if doc.get("step") == "password":
                self.step = "password"
                return {"ok": True, "step": "password"}
            return await self._finish(doc)

    async def login_password(self, body: dict) -> dict:
        password = body.get("password")
        if not isinstance(password, str) or not password or len(password) > 256:
            raise SetupError("请填两步验证密码")
        async with self.lock:
            await self._expire_idle()
            if self.helper is None or self.step != "password":
                raise SetupError("请先发送验证码", 409)
            return await self._finish(await self._ask({"cmd": "password", "password": password}, 60))

    async def _finish(self, doc: dict) -> dict:
        session, api = doc.get("session"), self.api
        await self._end_login()
        if doc.get("step") != "done" or not isinstance(session, str) or not SESSION.fullmatch(session) or api is None:
            raise SetupError("登录助手的回复看不懂，请重新发送验证码", 502)
        me = clean_me(doc.get("me"))
        try:
            write_env(self.env_file, {"TELEGRAM_SESSION_STRING": session, "TELEGRAM_API_ID": api[0],
                                      "TELEGRAM_API_HASH": api[1]})
        except (OSError, ValueError, UnicodeDecodeError) as e:
            self.log("telegram login could not be saved", type(e).__name__)
            await self._end_remote(session, api)  # never an unused "KOL Mail" login left on the account
            raise SetupError(f"登录成功了，但是登录信息存不进 {self.env_file}（{type(e).__name__}），请再登录一次",
                             500) from e
        with contextlib.suppress(OSError):  # only the name for the page
            if me:
                write_private(self.me_file, json.dumps(me, ensure_ascii=False))
            else:
                self.me_file.unlink(missing_ok=True)  # never an earlier account's name next to this login
        self.log("telegram login done, user id", me["id"] if me else "?")
        await self._changed()
        return {"ok": True, "step": "done", "me": me}

    async def logout(self) -> dict:
        """Forget the login here, then end it on Telegram's side too (so it leaves the account's device list). The
        backend is told first, so it lets go of the session before the helper uses it one last time."""
        async with self.lock:
            await self._end_login()
        session, api = self.session(), self.saved_api()
        if session is None:
            self.me_file.unlink(missing_ok=True)
            return {"ok": True, "remote": True, "note": ""}
        write_env(self.env_file, {"TELEGRAM_SESSION_STRING": None})
        self.me_file.unlink(missing_ok=True)
        self.log("telegram logged out here")
        await self._changed()
        remote = await self._end_remote(session, api)
        note = "" if remote else "Telegram 那边没能自动结束这个登录，请在手机 Telegram 的“设置 → 设备”里把 KOL Mail 结束掉"
        return {"ok": True, "remote": remote, "note": note}

    async def _end_remote(self, session: str, api: tuple[str, str] | None) -> bool:
        """End a session on Telegram's side through the helper; False when that could not be done."""
        remote, helper = False, None
        if api is not None and self.repo_path() is not None:
            try:
                helper = await self._start_helper("logout", {"TELEGRAM_API_ID": api[0], "TELEGRAM_API_HASH": api[1],
                                                             "TELEGRAM_SESSION_STRING": session})
                doc = await helper.ask(None, 90)
                remote = bool(doc.get("ok") and doc.get("logged_out"))
            except (SetupError, HelperFailed, OSError):
                remote = False
            finally:
                if helper is not None:
                    await helper.close()
        self.log("telegram session ended on Telegram's side" if remote else "telegram session not ended remotely")
        return remote

    # ------------------------------------------------ the account's name for the page

    def maybe_fetch_me(self):
        """A login made before this page existed (the owner's) has no me.json: look the name up once in a while,
        through the running backend when app.py passed get_me (no second connection with the live session),
        else through the helper."""
        if self.helper is not None or not self.session() or self.me() is not None:
            return
        if time.monotonic() - self.me_tried < ME_RETRY:
            return
        self.me_tried = time.monotonic()
        self._spawn(self._fetch_me())

    async def _fetch_me(self):
        session, helper = self.session(), None
        try:
            if self.get_me is not None:
                me = clean_me(await asyncio.wait_for(self.get_me(), 40))
            else:
                api = self.saved_api()
                if api is None or session is None or self.repo_path() is None:
                    return
                helper = await self._start_helper("whoami", {"TELEGRAM_API_ID": api[0], "TELEGRAM_API_HASH": api[1],
                                                             "TELEGRAM_SESSION_STRING": session})
                doc = await helper.ask(None, 90)
                me = clean_me(doc.get("me")) if doc.get("ok") else None
        except Exception as e:  # noqa: BLE001  only a name for the page; tried again later
            self.log("telegram name lookup failed", type(e).__name__)
            return
        finally:
            if helper is not None:
                await helper.close()
        if me and self.session() == session:  # still the same login (not logged out meanwhile)
            write_private(self.me_file, json.dumps(me, ensure_ascii=False))

    # ------------------------------------------------ wiring

    async def _changed(self):
        if self.on_change is None:
            return
        try:
            result = self.on_change()
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001  the files are written; the backend picks them up on its next try
            self.log("telegram on_change failed", type(e).__name__, str(e)[:200])

    def routes(self, on_change=None, get_me=None, log=None) -> list[Route]:
        """on_change(): called (and awaited when it returns an awaitable) after an install, a login or a logout, so
        app.py restarts the Telegram backend. get_me(): optional coroutine function returning telegram-mcp's get_me
        result through the running backend. log(*args): app.py's log."""
        self.on_change, self.get_me = on_change, get_me
        if log is not None:
            self.log = log

        async def status(_body):
            self.maybe_fetch_me()
            return self.status()

        async def install(_body):
            self.start_install()
            return {"ok": True, "status": self.status()}

        async def logout(_body):
            return await self.logout()

        return [Route("/api/telegram/status", _endpoint(status), methods=["GET"]),
                Route("/api/telegram/install", _endpoint(install), methods=["POST"]),
                Route("/api/telegram/login/start", _endpoint(self.login_start), methods=["POST"]),
                Route("/api/telegram/login/code", _endpoint(self.login_code), methods=["POST"]),
                Route("/api/telegram/login/password", _endpoint(self.login_password), methods=["POST"]),
                Route("/api/telegram/logout", _endpoint(logout), methods=["POST"])]


def _endpoint(fn):
    async def endpoint(request: Request):
        body: dict = {}
        if request.method == "POST":
            raw = await request.body()
            try:
                body = json.loads(raw) if raw.strip() else {}
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return JSONResponse({"error": "请求格式不对"}, status_code=400)
        try:
            return JSONResponse(await fn(body))
        except SetupError as e:
            return JSONResponse({"error": str(e), **e.extra}, status_code=e.status)
    return endpoint


# ---------------------------------------------------------------- module API (app.py)

_default: Setup | None = None


def default() -> Setup:
    global _default
    if _default is None:
        _default = Setup(config.DATA)
    return _default


def repo_path() -> Path | None:
    return default().repo_path()


def backend_env() -> dict[str, str]:
    return default().backend_env()


def status() -> dict:
    return default().status()


async def logout() -> dict:
    return await default().logout()


def routes(on_change=None, get_me=None, log=None) -> list[Route]:
    return default().routes(on_change=on_change, get_me=get_me, log=log)
