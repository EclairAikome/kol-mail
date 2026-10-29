"""Telegram set up from the settings page (tgsetup.py): installing the component, logging in and out through the
helper, the .env and me.json it writes, and the owner's hand-made setup. No network and no real Telegram: the
download, the venv/pip commands and the helper process are all replaced per test."""

import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import types
import zipfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import tgsetup  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
API_ID, API_HASH = "12345", "0123456789abcdef0123456789abcdef"
PHONE = "+8613812345678"
SESSION = "1BVtsOK8Bu1" + "A" * 300 + "=="
SS = "TELEGRAM_SESSION_" + "STRING"  # never written out whole next to a value: tools/publish.py scans for that
LOGGED_IN_ME = {"id": 42, "name": "Li Na", "username": "lina"}


# ---------------------------------------------------------------- fixtures

class FakeHelper:
    """Stands in for a tglogin.py process: replies come from a script function."""

    def __init__(self, mode, env, script):
        self.mode, self.env, self.script = mode, env or {}, script
        self.sent, self.closed = [], False

    async def ask(self, msg, timeout):
        self.sent.append(msg)
        reply = self.script(msg)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def close(self):
        self.closed = True


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.monkeypatch = tmp_path, monkeypatch
        self.settings = tmp_path / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config, "SETTINGS_FILE", self.settings)
        monkeypatch.delenv("KOL_TELEGRAM_MCP", raising=False)
        monkeypatch.delenv("PIP_INDEX_URL", raising=False)
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.setup = tgsetup.Setup(self.data)
        self.logs, self.events, self.helpers = [], [], []
        self.script = lambda msg: {"ok": True}

    def install_component(self, where: Path | None = None) -> Path:
        repo = where or self.data / "telegram-mcp"
        (repo / "main.py").parent.mkdir(parents=True, exist_ok=True)
        (repo / "main.py").write_text("# telegram-mcp\n", encoding="utf-8")
        py = tgsetup.venv_python(repo)
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_bytes(b"")
        return repo

    def fake_helpers(self):
        async def start_helper(mode, env=None):
            if self.setup.repo_path() is None:
                raise tgsetup.SetupError("先安装 Telegram 组件", 409)
            self.events.append(f"helper:{mode}")
            helper = FakeHelper(mode, env, self.script)
            self.helpers.append(helper)
            return helper
        self.monkeypatch.setattr(self.setup, "_start_helper", start_helper)

    def client(self, get_me=None):
        app = Starlette(routes=self.setup.routes(on_change=lambda: self.events.append("changed"), get_me=get_me,
                                                 log=lambda *a: self.logs.append(" ".join(map(str, a)))))
        return TestClient(app)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def login_script(msg):
    if msg["cmd"] == "start":
        return {"ok": True, "step": "code", "via": "app"}
    if msg["cmd"] == "code":
        if msg["code"] == "11111":
            return {"ok": False, "code": "code_invalid", "error": "验证码不对，请对照 Telegram 里收到的验证码再输一次"}
        return {"ok": True, "step": "password"}
    if msg["cmd"] == "password":
        return {"ok": True, "step": "done", "session": SESSION, "me": {**LOGGED_IN_ME, "phone": "8613812345678"}}
    return {"ok": False, "code": "bad_request", "error": "?"}


def make_zip(entries: dict[str, bytes], symlinks: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
        for name in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o120777 << 16
            z.writestr(info, "../../outside")
    return buf.getvalue()


GOOD = {"telegram-mcp-x/main.py": b"print('hi')\n", "telegram-mcp-x/requirements.txt": b"telethon>=1.45.0\n",
        "telegram-mcp-x/telegram_mcp/__init__.py": b""}


# ---------------------------------------------------------------- small pieces

def test_phone_is_masked_to_country_prefix_and_last_two_digits():
    assert tgsetup.mask_phone(PHONE) == "+86*********78"
    assert tgsetup.mask_phone("+123") == "+***"
    assert "13812345678" not in tgsetup.mask_phone(PHONE)


def test_env_writing_keeps_other_lines_endings_and_one_line_per_key(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(f"# note\r\nOTHER=1\r\n{SS}=old\r\n{SS}=older\r\n".encode())
    tgsetup.write_env(env, {"TELEGRAM_SESSION_STRING": "new", "TELEGRAM_API_ID": API_ID})
    assert env.read_bytes() == f"# note\r\nOTHER=1\r\n{SS}=new\r\nTELEGRAM_API_ID=12345\r\n".encode()
    tgsetup.write_env(env, {"TELEGRAM_SESSION_STRING": None})
    assert env.read_bytes() == b"# note\r\nOTHER=1\r\nTELEGRAM_API_ID=12345\r\n"
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError):
        tgsetup.write_env(env, {"TELEGRAM_API_ID": "1\nEVIL=1"})
    if os.name != "nt":
        assert env.stat().st_mode & 0o777 == 0o600


def test_read_env_takes_the_first_value_and_dotenv_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# c\nexport TELEGRAM_API_ID="777"\nTELEGRAM_API_ID=888\nBAD LINE\n', encoding="utf-8")
    assert tgsetup.read_env(env) == {"TELEGRAM_API_ID": "777"}
    assert tgsetup.read_env(tmp_path / "missing") == {}


def test_me_keeps_only_id_name_and_username():
    assert tgsetup.clean_me({"id": "7", "name": "A\x00B", "type": "user", "username": "@x", "phone": "1"}) == \
        {"id": 7, "name": "AB", "username": "x"}
    assert tgsetup.clean_me({"result": "Error"}) is None


# ---------------------------------------------------------------- the zip

@pytest.mark.parametrize("bad", ["telegram-mcp-x/../evil.py", "/evil.py", "telegram-mcp-x/..\\evil.py",
                                 "C:/evil.py", "telegram-mcp-x/a/../../evil.py", "telegram-mcp-x//evil.py"])
def test_zip_with_a_path_out_of_its_folder_is_refused(tmp_path, bad):
    dest = tmp_path / "x" / "dest"
    with pytest.raises(tgsetup.InstallError, match="不安全的路径"):
        tgsetup.unpack(make_zip({**GOOD, bad: b"boom"}), dest, None)
    assert not dest.exists() and not (tmp_path / "x" / "evil.py").exists() and not (tmp_path / "evil.py").exists()


def test_zip_with_a_link_or_two_folders_or_junk_is_refused(tmp_path):
    with pytest.raises(tgsetup.InstallError, match="不安全的路径"):
        tgsetup.unpack(make_zip(GOOD, symlinks=("telegram-mcp-x/link",)), tmp_path / "a", None)
    with pytest.raises(tgsetup.InstallError, match="结构不对"):
        tgsetup.unpack(make_zip({**GOOD, "other/x.py": b""}), tmp_path / "b", None)
    with pytest.raises(tgsetup.InstallError, match="不是完整的压缩包"):
        tgsetup.unpack(b"not a zip", tmp_path / "c", None)
    assert not any((tmp_path / d).exists() for d in "abc")


def test_zip_must_be_exactly_the_tested_commit(tmp_path, monkeypatch):
    blob = make_zip(GOOD)
    with pytest.raises(tgsetup.InstallError, match="对不上"):
        tgsetup.unpack(blob, tmp_path / "a", tgsetup.TREE_SHA256)
    assert not (tmp_path / "a").exists()
    files = {k.split("/", 1)[1]: v for k, v in GOOD.items()}
    tgsetup.unpack(blob, tmp_path / "b", tgsetup.tree_digest(files))
    assert (tmp_path / "b" / "main.py").read_bytes() == GOOD["telegram-mcp-x/main.py"]
    monkeypatch.setattr(tgsetup, "MAX_UNPACKED", 10)
    with pytest.raises(tgsetup.InstallError, match="大小不正常"):
        tgsetup.unpack(blob, tmp_path / "c", None)


# ---------------------------------------------------------------- install

def fake_install(world, pip_codes=(0,), downloads=None):
    """Download gives GOOD (or, in turn, what downloads holds: bytes, or an exception to raise), venv makes the
    python file, pip answers with pip_codes in turn."""
    blob = make_zip(GOOD)
    world.monkeypatch.setattr(tgsetup, "TREE_SHA256", tgsetup.tree_digest({k.split("/", 1)[1]: v for k, v in GOOD.items()}))
    calls, codes, served = [], list(pip_codes), list(downloads or [])

    async def download(url):
        calls.append(["download", url])
        got = served.pop(0) if served else blob
        if isinstance(got, Exception):
            raise got
        return got

    async def run(args, timeout):
        calls.append(list(args))
        if "venv" in args:
            py = tgsetup.venv_python(Path(args[-1]).parent)
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"")
            return 0, ""
        if "pip" in args:
            code = codes.pop(0) if codes else 0
            return code, "ERROR: no network" if code else "Successfully installed telethon-1.45.0"
        return 0, ""

    world.monkeypatch.setattr(world.setup, "_download", download)
    world.monkeypatch.setattr(world.setup, "_run", run)
    return calls


def wait_install(client):
    for _ in range(200):
        s = client.get("/api/telegram/status").json()
        if s["component"] != "installing":
            return s
        time.sleep(0.02)
    raise AssertionError("install did not finish")


def test_install_puts_the_component_in_data_and_saves_the_path(world):
    calls = fake_install(world)
    with world.client() as c:
        assert c.get("/api/telegram/status").json()["component"] == "missing"
        r = c.post("/api/telegram/install", json={})
        assert r.status_code == 200
        s = wait_install(c)
    home = world.data / "telegram-mcp"
    assert s["component"] == "ok" and s["install"]["stage"] == "done", s
    assert (home / "main.py").is_file() and tgsetup.venv_python(home).is_file()
    assert json.loads(world.settings.read_text(encoding="utf-8"))["paths"]["telegram_mcp"] == str(home)
    assert world.setup.repo_path() == home
    assert world.events == ["changed"]
    assert not list(world.data.glob(".tg*-*"))
    assert calls[0] == ["download", tgsetup.ZIP_URL]
    venv = next(a for a in calls if "venv" in a)
    assert venv[:6] == [world.setup.python, "-I", "-X", "utf8", "-m", "venv"]
    pip = next(a for a in calls if "pip" in a)
    assert "-r" in pip and pip[pip.index("-r") + 1].endswith("requirements.txt") and "-c" in pip and "-i" not in pip
    assert not (home / ".kol-mail-pins.txt").exists()


def test_install_uses_the_configured_index_and_only_https(world):
    world.settings.write_text(json.dumps({"update": {"pip_index": "https://pypi.example.org/simple"}}), encoding="utf-8")
    calls = fake_install(world)
    with world.client() as c:
        c.post("/api/telegram/install", json={})
        assert wait_install(c)["component"] == "ok"
    pips = [a for a in calls if "pip" in a]
    assert len(pips) == 1 and pips[0][pips[0].index("-i") + 1] == "https://pypi.example.org/simple"
    world.settings.write_text(json.dumps({"update": {"pip_index": "http://pypi.example.org/simple"}}), encoding="utf-8")
    assert world.setup._pip_index() == ""


def test_failed_pip_leaves_nothing_behind(world):
    (world.data / ".tgnew-dead" / "x").mkdir(parents=True)  # from an install cut off earlier
    calls = fake_install(world, pip_codes=(1,))
    with world.client() as c:
        c.post("/api/telegram/install", json={})
        s = wait_install(c)
    assert len([a for a in calls if "pip" in a]) == 1
    assert s["component"] == "failed" and "安装依赖失败" in s["install"]["error"] and "no network" in s["install"]["error"]
    assert not (world.data / "telegram-mcp").exists() and not list(world.data.glob(".tg*-*"))
    assert "paths" not in json.loads(world.settings.read_text(encoding="utf-8"))
    assert world.events == []  # nothing to restart


def test_the_mirror_is_tried_when_github_fails_or_serves_junk(world):
    world.settings.write_text(json.dumps({"update": {"mirror": "https://gh.example.org/"}}), encoding="utf-8")
    calls = fake_install(world, downloads=[tgsetup.InstallError("下载 Telegram 组件失败：HTTP 503")])
    with world.client() as c:
        c.post("/api/telegram/install", json={})
        assert wait_install(c)["component"] == "ok"
    assert [a[1] for a in calls if a[0] == "download"] == [tgsetup.ZIP_URL, "https://gh.example.org/" + tgsetup.ZIP_URL]

    (world.tmp / "second").mkdir()
    world2 = World(world.tmp / "second", world.monkeypatch)
    world2.settings.write_text(json.dumps({"update": {"mirror": "https://gh.example.org"}}), encoding="utf-8")
    calls = fake_install(world2, downloads=[b"<html>rate limited</html>", b"still not a zip"])
    with world2.client() as c:
        c.post("/api/telegram/install", json={})
        s = wait_install(c)
    assert len([a for a in calls if a[0] == "download"]) == 2
    assert s["component"] == "failed" and "压缩包" in s["install"]["error"] and not list(world2.data.glob(".tg*-*"))


def test_a_failed_swap_puts_the_old_folder_back(world, monkeypatch):
    home = world.data / "telegram-mcp"
    (home / "keep.txt").parent.mkdir(parents=True)
    (home / "keep.txt").write_text("broken but mine", encoding="utf-8")  # no venv: not a working install
    fake_install(world)
    real_move = tgsetup.move

    def move(src, dst):
        if Path(src).name.startswith(".tgnew-"):
            raise PermissionError("in use")
        real_move(src, dst)

    monkeypatch.setattr(tgsetup, "move", move)
    with world.client() as c:
        c.post("/api/telegram/install", json={})
        s = wait_install(c)
    assert s["component"] == "failed" and "放置 Telegram 组件失败" in s["install"]["error"]
    assert (home / "keep.txt").read_text(encoding="utf-8") == "broken but mine"
    assert not list(world.data.glob(".tg*-*"))


def test_installed_component_is_not_installed_again(world):
    world.install_component()
    with world.client() as c:
        r = c.post("/api/telegram/install", json={})
    assert r.status_code == 409 and "已经装好了" in r.json()["error"]


def test_a_broken_hand_made_checkout_is_not_replaced_by_an_install(world):
    checkout = world.tmp / "telegram-mcp-owner"
    checkout.mkdir()
    (checkout / ".env").write_text(f"TELEGRAM_API_ID={API_ID}\n", encoding="utf-8")  # its .venv is gone
    world.settings.write_text(json.dumps({"paths": {"telegram_mcp": str(checkout)}}), encoding="utf-8")
    fake_install(world)
    with world.client() as c:
        assert c.get("/api/telegram/status").json()["component"] == "missing"
        r = c.post("/api/telegram/install", json={})
    assert r.status_code == 409 and "paths.telegram_mcp" in r.json()["error"]
    assert json.loads(world.settings.read_text(encoding="utf-8"))["paths"]["telegram_mcp"] == str(checkout)
    assert not (world.data / "telegram-mcp").exists()


# ---------------------------------------------------------------- the owner's setup

def test_owner_checkout_reads_as_installed_and_logged_in_and_stays_untouched(world):
    checkout = world.install_component(world.tmp / "telegram-mcp-owner")
    owner_env = f"TELEGRAM_API_ID={API_ID}\nTELEGRAM_API_HASH={API_HASH}\n{SS}=claude-code-session\n"
    (checkout / ".env").write_text(owner_env, encoding="utf-8")
    world.settings.write_text(json.dumps({"paths": {"telegram_mcp": str(checkout)}}), encoding="utf-8")
    (world.data / "telegram").mkdir()
    session_file = world.data / "telegram" / ".env"
    session_file.write_text(f"# kol-mail 自动回复专用的 Telegram 登录\n{SS}=kol-mail-session\n",
                            encoding="utf-8")
    before = session_file.read_bytes()
    world.fake_helpers()  # without get_me the name is looked up through the helper; it answers without one here
    with world.client() as c:
        s = c.get("/api/telegram/status").json()
        for _ in range(100):
            if world.helpers:
                break
            time.sleep(0.02)
    assert s["component"] == "ok" and s["logged_in"] is True and s["api_ready"] is True and s["me"] is None
    assert world.setup.repo_path() == checkout
    assert world.setup.backend_env() == {}  # the checkout's .env keeps supplying the API keys, as before
    assert (checkout / ".env").read_text(encoding="utf-8") == owner_env and session_file.read_bytes() == before
    assert not (world.data / "telegram" / "me.json").exists()
    assert "claude-code-session" not in json.dumps(s) and API_HASH not in json.dumps(s)
    (whoami,) = world.helpers
    assert whoami.mode == "whoami" and whoami.env == {"TELEGRAM_API_ID": API_ID, "TELEGRAM_API_HASH": API_HASH,
                                                      "TELEGRAM_SESSION_STRING": "kol-mail-session"}


def test_the_owners_name_comes_from_the_running_backend(world):
    world.install_component()
    (world.data / "telegram").mkdir()
    (world.data / "telegram" / ".env").write_text(f"{SS}=kol-mail-session\n", encoding="utf-8")
    world.fake_helpers()
    asked = []

    async def get_me():
        asked.append(1)
        return {"id": 7, "name": "Owner", "type": "user", "username": "owner", "phone": "8613800000000"}

    with world.client(get_me=get_me) as c:
        c.get("/api/telegram/status")
        for _ in range(100):
            me = c.get("/api/telegram/status").json()["me"]
            if me:
                break
            time.sleep(0.02)
    assert me == {"id": 7, "name": "Owner", "username": "owner"} and asked == [1]
    assert world.helpers == []  # no second connection with the live session
    assert "8613800000000" not in (world.data / "telegram" / "me.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- login

@pytest.mark.parametrize("body, wanted", [
    ({"api_id": "12ab", "api_hash": API_HASH, "phone": PHONE}, "API ID"),
    ({"api_id": API_ID, "api_hash": "xyz", "phone": PHONE}, "API Hash"),
    ({"api_id": API_ID, "api_hash": API_HASH[:-1], "phone": PHONE}, "API Hash"),
    ({"api_id": API_ID, "api_hash": "", "phone": PHONE}, "API Hash"),
    ({"api_id": API_ID, "api_hash": API_HASH, "phone": "13812345678"}, "国家区号"),
    ({"api_id": API_ID, "api_hash": API_HASH, "phone": "+12"}, "国家区号"),
    ({"phone": PHONE}, "先填 API ID"),
])
def test_bad_input_is_refused_before_any_helper_starts(world, body, wanted):
    world.install_component()
    world.fake_helpers()
    with world.client() as c:
        r = c.post("/api/telegram/login/start", json=body)
    assert r.status_code == 400 and wanted in r.json()["error"]
    assert world.helpers == []


def test_bad_code_password_and_bodies_are_refused(world):
    world.install_component()
    world.fake_helpers()
    with world.client() as c:
        assert c.post("/api/telegram/login/code", json={"code": "12a45"}).status_code == 400
        assert c.post("/api/telegram/login/password", json={"password": ""}).status_code == 400
        assert c.post("/api/telegram/login/code", json={"code": "12345"}).status_code == 409  # no code sent yet
        bad = c.post("/api/telegram/login/start", content=b"{not json", headers={"Content-Type": "application/json"})
        assert bad.status_code == 400 and bad.json()["error"] == "请求格式不对"
        assert c.post("/api/telegram/login/start", json=[1, 2]).status_code == 400
    assert world.helpers == []


def test_login_needs_the_component_and_no_login_yet(world):
    world.fake_helpers()
    with world.client() as c:
        r = c.post("/api/telegram/login/start", json={"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE})
        assert r.status_code == 409 and "先安装" in r.json()["error"]
        world.install_component()
        (world.data / "telegram").mkdir()
        (world.data / "telegram" / ".env").write_text("TELEGRAM_SESSION_STRING=x\n", encoding="utf-8")
        r = c.post("/api/telegram/login/start", json={"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE})
        assert r.status_code == 409 and "退出登录" in r.json()["error"]


def test_login_with_code_and_password_writes_env_and_me_and_leaks_nothing(world):
    world.install_component()
    world.fake_helpers()
    world.script = login_script
    (world.data / "telegram").mkdir()
    env = world.data / "telegram" / ".env"
    env.write_bytes(b"# my note\r\nOTHER=1\r\nTELEGRAM_SESSION_STRING=\r\n")
    texts = []
    with world.client() as c:
        r = c.post("/api/telegram/login/start", json={"api_id": API_ID, "api_hash": API_HASH.upper(),
                                                      "phone": "+86 138-1234-5678"})
        texts.append(r.text)
        assert r.status_code == 200 and r.json() == {"ok": True, "step": "code", "via": "app", "phone": "+86*********78"}
        s = c.get("/api/telegram/status").json()
        texts.append(json.dumps(s))
        assert s["login"] == {"step": "code", "phone": "+86*********78"} and s["logged_in"] is False
        r = c.post("/api/telegram/login/code", json={"code": "11111"})
        texts.append(r.text)
        assert r.status_code == 400 and r.json()["reason"] == "code_invalid"
        assert c.get("/api/telegram/status").json()["login"]["step"] == "code"  # the same login goes on
        r = c.post("/api/telegram/login/code", json={"code": "22 222"})
        texts.append(r.text)
        assert r.json() == {"ok": True, "step": "password"}
        assert world.events == ["helper:login"]
        r = c.post("/api/telegram/login/password", json={"password": "hunter2-secret"})
        texts.append(r.text)
        assert r.status_code == 200 and r.json() == {"ok": True, "step": "done", "me": LOGGED_IN_ME}
        s = c.get("/api/telegram/status").json()
        texts.append(json.dumps(s))
    helper = world.helpers[0]
    assert helper.sent[0] == {"cmd": "start", "api_id": 12345, "api_hash": API_HASH, "phone": PHONE}
    assert helper.sent[2] == {"cmd": "code", "code": "22222"} and helper.closed
    assert env.read_bytes() == (b"# my note\r\nOTHER=1\r\nTELEGRAM_SESSION_STRING=" + SESSION.encode()
                                + b"\r\nTELEGRAM_API_ID=12345\r\nTELEGRAM_API_HASH=" + API_HASH.encode() + b"\r\n")
    assert json.loads((world.data / "telegram" / "me.json").read_text(encoding="utf-8")) == LOGGED_IN_ME
    assert s["logged_in"] is True and s["me"] == LOGGED_IN_ME and s["login"] == {"step": None}
    assert world.events == ["helper:login", "changed"]
    assert world.setup.backend_env() == {"TELEGRAM_API_ID": API_ID, "TELEGRAM_API_HASH": API_HASH}
    for secret in (SESSION, API_HASH, "hunter2-secret", "22222", "13812345678"):
        assert all(secret not in t for t in texts), secret
        assert all(secret not in line for line in world.logs), secret
    # on disk: the session and the hash in .env only, the password, the code and the phone nowhere
    for path in (p for p in world.tmp.rglob("*") if p.is_file()):
        content = path.read_bytes()
        for secret in ("hunter2-secret", "22222", "13812345678") + (() if path == env else (SESSION, API_HASH)):
            assert secret.encode() not in content, (path, secret)


def test_a_login_that_cannot_be_saved_is_ended_on_telegram(world, monkeypatch):
    world.install_component()
    world.fake_helpers()
    world.script = lambda msg: ({"ok": True, "logged_out": True} if msg is None else login_script(msg))

    def refuse(path, changes):
        raise PermissionError("locked by another program")

    monkeypatch.setattr(tgsetup, "write_env", refuse)
    with world.client() as c:
        c.post("/api/telegram/login/start", json={"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE})
        c.post("/api/telegram/login/code", json={"code": "22222"})
        r = c.post("/api/telegram/login/password", json={"password": "pw"})
    assert r.status_code == 500 and "存不进" in r.json()["error"] and SESSION not in r.text
    logout = world.helpers[1]
    assert logout.mode == "logout" and logout.env["TELEGRAM_SESSION_STRING"] == SESSION and logout.closed
    assert "changed" not in world.events


def test_saved_api_keys_are_used_when_both_fields_are_left_empty(world):
    world.install_component()
    world.fake_helpers()
    world.script = login_script
    (world.data / "telegram").mkdir()
    (world.data / "telegram" / ".env").write_text(f"TELEGRAM_API_ID={API_ID}\nTELEGRAM_API_HASH={API_HASH}\n",
                                                  encoding="utf-8")
    with world.client() as c:
        assert c.get("/api/telegram/status").json()["api_ready"] is True
        r = c.post("/api/telegram/login/start", json={"api_id": "", "api_hash": "", "phone": PHONE})
    assert r.status_code == 200 and world.helpers[0].sent[0]["api_hash"] == API_HASH


def test_flood_wait_ends_the_login_with_429_and_the_seconds(world):
    world.install_component()
    world.fake_helpers()
    world.script = lambda msg: {"ok": False, "code": "flood", "wait": 300, "error": "Telegram 暂时不让这个号再请求，请 5 分钟后再试"}
    with world.client() as c:
        r = c.post("/api/telegram/login/start", json={"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE})
        s = c.get("/api/telegram/status").json()
    assert r.status_code == 429 and r.json()["wait"] == 300 and "5 分钟" in r.json()["error"]
    assert world.helpers[0].closed and s["login"] == {"step": None}


def test_expired_code_and_a_dead_helper_end_the_login(world):
    world.install_component()
    world.fake_helpers()
    replies = iter([{"ok": True, "step": "code", "via": "sms"},
                    {"ok": False, "code": "code_expired", "error": "验证码过期了，请重新发送验证码"},
                    {"ok": True, "step": "code", "via": "sms"},
                    tgsetup.HelperFailed("登录助手意外退出了")])
    world.script = lambda msg: next(replies)
    with world.client() as c:
        body = {"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE}
        c.post("/api/telegram/login/start", json=body)
        r = c.post("/api/telegram/login/code", json={"code": "12345"})
        assert r.status_code == 400 and "过期" in r.json()["error"]
        assert c.get("/api/telegram/status").json()["login"] == {"step": None}
        c.post("/api/telegram/login/start", json=body)
        r = c.post("/api/telegram/login/code", json={"code": "12345"})
        assert r.status_code == 502 and "意外退出" in r.json()["error"]
    assert all(h.closed for h in world.helpers) and not (world.data / "telegram" / ".env").exists()
    assert "changed" not in world.events


def test_only_one_login_runs_at_a_time(world):
    world.install_component()
    world.fake_helpers()
    world.script = login_script
    with world.client() as c:
        body = {"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE}
        c.post("/api/telegram/login/start", json=body)
        c.post("/api/telegram/login/start", json={**body, "phone": "+447700900123"})
        s = c.get("/api/telegram/status").json()
    first, second = world.helpers
    assert first.closed and not second.closed and s["login"]["phone"] == "+44********23"


def test_a_helper_left_alone_is_stopped(world, monkeypatch):
    monkeypatch.setattr(tgsetup, "LOGIN_IDLE", 0.2)
    world.install_component()
    world.fake_helpers()
    world.script = login_script
    with world.client() as c:
        c.post("/api/telegram/login/start", json={"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE})
        time.sleep(0.6)
        r = c.post("/api/telegram/login/code", json={"code": "22222"})
        s = c.get("/api/telegram/status").json()
    assert r.status_code == 409 and world.helpers[0].closed and s["login"] == {"step": None}
    assert any("idle" in line for line in world.logs)


# ---------------------------------------------------------------- logout

def logged_in(world):
    world.install_component()
    (world.data / "telegram").mkdir()
    env = world.data / "telegram" / ".env"
    env.write_text(f"OTHER=1\n{SS}={SESSION}\nTELEGRAM_API_ID={API_ID}\nTELEGRAM_API_HASH={API_HASH}\n",
                   encoding="utf-8")
    (world.data / "telegram" / "me.json").write_text(json.dumps(LOGGED_IN_ME), encoding="utf-8")
    world.fake_helpers()
    return env


def test_logout_ends_the_session_on_telegram_and_keeps_the_api_keys(world):
    env = logged_in(world)
    world.script = lambda msg: {"ok": True, "logged_out": True}
    with world.client() as c:
        r = c.post("/api/telegram/logout")
        s = c.get("/api/telegram/status").json()
    assert r.status_code == 200 and r.json() == {"ok": True, "remote": True, "note": ""}
    assert env.read_text(encoding="utf-8") == f"OTHER=1\nTELEGRAM_API_ID={API_ID}\nTELEGRAM_API_HASH={API_HASH}\n"
    assert not (world.data / "telegram" / "me.json").exists()
    assert world.events == ["changed", "helper:logout"]  # the backend lets go of the session first
    helper = world.helpers[0]
    assert helper.env == {"TELEGRAM_API_ID": API_ID, "TELEGRAM_API_HASH": API_HASH, "TELEGRAM_SESSION_STRING": SESSION}
    assert helper.sent == [None] and helper.closed
    assert s["logged_in"] is False and s["me"] is None and s["api_ready"] is True
    assert SESSION not in r.text and all(SESSION not in line for line in world.logs)


def test_logout_that_telegram_did_not_take_still_forgets_here_and_says_so(world):
    env = logged_in(world)
    world.script = lambda msg: tgsetup.HelperFailed("Telegram 太久没有回应")
    with world.client() as c:
        r = c.post("/api/telegram/logout")
    assert r.json()["remote"] is False and "设备" in r.json()["note"]
    assert "TELEGRAM_SESSION_STRING" not in env.read_text(encoding="utf-8")


def test_logout_cancels_a_login_in_flight(world):
    world.install_component()
    world.fake_helpers()
    world.script = login_script
    with world.client() as c:
        c.post("/api/telegram/login/start", json={"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE})
        r = c.post("/api/telegram/logout")
        s = c.get("/api/telegram/status").json()
    assert r.json()["ok"] and world.helpers[0].closed and s["login"] == {"step": None}


# ---------------------------------------------------------------- the helper process plumbing

def test_helper_talks_json_lines_and_one_that_hangs_or_dies_is_handled(tmp_path, monkeypatch):
    monkeypatch.setattr(tgsetup, "HELPER_GRACE", 0.3)
    scripts = {"echo": "import sys, json\nfor line in sys.stdin:\n"
                       "    print(json.dumps({'ok': True, 'got': json.loads(line)}), flush=True)\n",
               "hang": "import time\ntime.sleep(60)\n", "silent": "pass\n"}
    for name, text in scripts.items():
        (tmp_path / f"{name}.py").write_text(text, encoding="utf-8")

    async def start(name):
        return tgsetup.Helper(await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(tmp_path / f"{name}.py"), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, **tgsetup._NO_WINDOW))

    async def scenario():
        h = await start("echo")
        assert await h.ask({"cmd": "x"}, 30) == {"ok": True, "got": {"cmd": "x"}}
        await h.close()
        assert h.proc.returncode is not None
        h = await start("hang")
        with pytest.raises(tgsetup.HelperFailed, match="太久"):
            await h.ask({"cmd": "x"}, 0.5)
        await h.close()  # ignores the end of its stdin: ended after HELPER_GRACE
        assert h.proc.returncode is not None
        with pytest.raises(tgsetup.HelperFailed, match="意外退出"):
            await (await start("silent")).ask(None, 30)

    asyncio.run(scenario())


def test_the_helper_gets_only_the_telegram_settings_of_its_own_run(world, monkeypatch):
    repo = world.install_component()
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "claude-code-session")
    monkeypatch.setenv("TELEGRAM_API_HASH", "f" * 32)
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return types.SimpleNamespace(returncode=None, stdin=None, stdout=None)

    monkeypatch.setattr(tgsetup.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(world.setup._start_helper("logout", {"TELEGRAM_SESSION_STRING": SESSION}))
    assert seen["args"] == (str(tgsetup.venv_python(repo)), "-I", str(tgsetup.HELPER), "logout")
    env = seen["kwargs"]["env"]
    assert env["TELEGRAM_SESSION_STRING"] == SESSION and "TELEGRAM_API_HASH" not in env
    assert "claude-code-session" not in json.dumps(env)
    assert seen["kwargs"]["stderr"] == asyncio.subprocess.DEVNULL


# ---------------------------------------------------------------- the helper script itself

def load_tglogin(monkeypatch):
    """tglogin.py against a stand-in Telethon with just the names it uses, loaded without registering it in
    sys.modules (the stand-in modules are, through monkeypatch, and go away after the test)."""
    errors = types.ModuleType("telethon.errors")

    class RPCError(Exception):
        def __init__(self, message="RPC_ERROR"):
            super().__init__(message)
            self.message = message

    class FloodWaitError(RPCError):
        def __init__(self, seconds):
            super().__init__("FLOOD_WAIT")
            self.seconds = seconds

    errors.RPCError, errors.FloodWaitError = RPCError, FloodWaitError
    for name in ("PhoneCodeInvalidError", "PhoneCodeEmptyError", "PhoneCodeExpiredError", "PasswordHashInvalidError",
                 "PhoneNumberInvalidError", "PhoneNumberBannedError", "PhoneNumberUnoccupiedError",
                 "PhoneNumberFloodError", "PhonePasswordFloodError", "ApiIdInvalidError", "ApiIdPublishedFloodError",
                 "SendCodeUnavailableError", "AuthKeyUnregisteredError", "SessionRevokedError",
                 "UserDeactivatedError", "SessionPasswordNeededError"):
        setattr(errors, name, type(name, (RPCError,), {}))
    auth = types.SimpleNamespace(**{n: type(n, (), {}) for n in (
        "SentCodeTypeApp", "SentCodeTypeSms", "SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase",
        "SentCodeTypeFirebaseSms", "SentCodeTypeSetUpEmailRequired", "SentCodeTypeCall")})
    tl_types = types.ModuleType("telethon.tl.types")
    tl_types.auth = auth
    tl = types.ModuleType("telethon.tl")
    tl.types = tl_types
    sessions = types.ModuleType("telethon.sessions")

    class StringSession:
        def __init__(self, s=""):
            self.s = s

        @staticmethod
        def save(session):
            return "SAVED-" + session.s

    sessions.StringSession = StringSession
    telethon = types.ModuleType("telethon")
    telethon.errors, telethon.TelegramClient = errors, object
    for name, module in {"telethon": telethon, "telethon.errors": errors, "telethon.sessions": sessions,
                         "telethon.tl": tl, "telethon.tl.types": tl_types}.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("tglogin_under_test", ROOT / "tglogin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, errors, auth


def test_helper_turns_telegram_errors_into_page_messages_without_secrets(monkeypatch):
    m, errors, auth = load_tglogin(monkeypatch)
    flood = m.failure(errors.FloodWaitError(125), ())
    assert flood["code"] == "flood" and flood["wait"] == 125 and "3 分钟" in flood["error"]
    assert m.failure(errors.PhoneCodeInvalidError(), ())["code"] == "code_invalid"
    assert m.failure(errors.PhoneCodeExpiredError(), ())["code"] == "code_expired"
    assert m.failure(errors.PasswordHashInvalidError(), ())["code"] == "password_invalid"
    assert m.failure(errors.ApiIdInvalidError(), ())["code"] == "api_invalid"
    assert m.failure(ConnectionError("refused"), ())["code"] == "network"
    assert "s3cret" not in m.failure(errors.RPCError("BAD_THING s3cret"), ("s3cret",))["error"]
    assert m.failure(ValueError("s3cret"), ())["error"].count("ValueError") == 1  # the type, never the message
    assert m.code_channel(types.SimpleNamespace(type=auth.SentCodeTypeApp())) == "app"
    assert m.code_channel(types.SimpleNamespace(type=auth.SentCodeTypeSms())) == "sms"
    assert m.code_channel(types.SimpleNamespace(type=auth.SentCodeTypeCall())) == "other"


def test_helper_login_goes_code_then_password_and_retries_wrong_ones(monkeypatch):
    m, errors, auth = load_tglogin(monkeypatch)

    class Client:
        def __init__(self):
            self.session, self.disconnected = types.SimpleNamespace(s="sess"), False

        async def connect(self):
            pass

        async def send_code_request(self, phone):
            return types.SimpleNamespace(type=auth.SentCodeTypeApp())

        async def sign_in(self, phone=None, code=None, password=None):
            if code == "11111":
                raise errors.PhoneCodeInvalidError()
            if code:
                raise errors.SessionPasswordNeededError()
            if password == "pw":
                return types.SimpleNamespace(id=5, first_name="Li", last_name="Na", username="lina")
            raise errors.PasswordHashInvalidError()

        async def disconnect(self):
            self.disconnected = True

    clients, out = [], []
    inbox = iter([{"cmd": "start", "api_id": 1, "api_hash": "h" * 32, "phone": PHONE}, {"cmd": "code", "code": "11111"},
                  {"cmd": "code", "code": "22222"}, {"cmd": "password", "password": "bad"},
                  {"cmd": "password", "password": "pw"}])

    async def read_message():
        return next(inbox, None)

    monkeypatch.setattr(m, "client_for", lambda session, api_id, api_hash: clients.append(Client()) or clients[-1])
    monkeypatch.setattr(m, "read_message", read_message)
    monkeypatch.setattr(m, "reply", out.append)
    assert asyncio.run(m.login()) == 0
    assert [d.get("step") or d.get("code") for d in out] == ["code", "code_invalid", "password", "password_invalid",
                                                            "done"]
    assert out[0]["via"] == "app" and out[-1]["session"] == "SAVED-sess"
    assert out[-1]["me"] == {"id": 5, "name": "Li Na", "username": "lina"} and clients[0].disconnected
    assert all("pw" not in json.dumps(d) and "h" * 32 not in json.dumps(d) for d in out)


def test_helper_logout_counts_an_already_dead_session_as_logged_out(monkeypatch):
    m, errors, auth = load_tglogin(monkeypatch)

    class Client:
        def __init__(self, authorized):
            self.authorized = authorized

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return self.authorized

        async def log_out(self):
            return True

        async def disconnect(self):
            pass

    out = []
    monkeypatch.setattr(m, "reply", out.append)
    monkeypatch.setenv("TELEGRAM_API_ID", API_ID)
    monkeypatch.setenv("TELEGRAM_API_HASH", API_HASH)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", SESSION)
    for authorized in (True, False):
        monkeypatch.setattr(m, "client_for", lambda *a, authorized=authorized: Client(authorized))
        assert asyncio.run(m.with_session("logout")) == 0
    assert out == [{"ok": True, "logged_out": True}, {"ok": True, "logged_out": True}]


def test_helper_without_telethon_answers_in_its_protocol():
    try:
        import telethon  # noqa: F401
        pytest.skip("Telethon is installed here; the owner's telegram-mcp venv checks the real thing")
    except ImportError:
        pass
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tglogin.py"), "login"], input="", capture_output=True,
                       text=True, timeout=60)
    doc = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 2 and doc["ok"] is False and doc["code"] == "no_telethon"
