"""The mailboxes KOL Mail shows: the primary one set up by the installer, plus any added in the page.

The primary account is settings.json mail.account_name / mail.address, as before. The others are listed in
mail.extra_accounts as {"name", "address", "label"}; "name" is the account's name inside mcp-email-server, which keeps
the server settings and stores the password in the system's credential store (Windows Credential Manager, Keychain).
Adding, testing and removing an account goes through mcp-email-server's own command line, with the password written
to its standard input, never on a command line."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import signal
import subprocess
from dataclasses import dataclass

NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# Conservative on purpose: the address goes on mcp-email-server's command line, and its argument parser expands
# %VAR%, $VAR, ~ and wildcards on Windows. Ordinary addresses never need those characters.
EMAIL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$")
MAX_ACCOUNTS = 8

# Well-known providers: (IMAP host, port, security, SMTP host, port, security). 163/126/QQ need the 授权码 from the
# mailbox's web settings, Gmail an app password; Outlook.com only allows OAuth sign-in now, so it is not offered.
PROVIDERS = {
    "163.com": ("imap.163.com", 993, "ssl", "smtp.163.com", 465, "ssl"),
    "126.com": ("imap.126.com", 993, "ssl", "smtp.126.com", 465, "ssl"),
    "yeah.net": ("imap.yeah.net", 993, "ssl", "smtp.yeah.net", 465, "ssl"),
    "qq.com": ("imap.qq.com", 993, "ssl", "smtp.qq.com", 465, "ssl"),
    "foxmail.com": ("imap.qq.com", 993, "ssl", "smtp.qq.com", 465, "ssl"),
    "sina.com": ("imap.sina.com", 993, "ssl", "smtp.sina.com", 465, "ssl"),
    "gmail.com": ("imap.gmail.com", 993, "ssl", "smtp.gmail.com", 465, "ssl"),
    "icloud.com": ("imap.mail.me.com", 993, "ssl", "smtp.mail.me.com", 587, "starttls"),
    "me.com": ("imap.mail.me.com", 993, "ssl", "smtp.mail.me.com", 587, "starttls"),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "ssl", "smtp.mail.yahoo.com", 465, "ssl"),
}
OAUTH_ONLY = ("outlook.com", "hotmail.com", "live.com", "msn.com")


ERRORS_ZH = {
    "account_limit_reached": "邮件组件里的邮箱数量已到上限",
    "account_name_exists": "邮件组件里已经有同名的邮箱",
    "authentication_or_provider_rejected": "邮箱服务器拒绝登录：密码（或授权码）不对，或者邮箱还没开通 IMAP/SMTP",
    "credential_store_unavailable": "系统的凭据管理器用不了，密码存不进去",
    "invalid_input": "填的内容格式不对",
    "revision_conflict": "邮箱设置刚被别的程序改过，请再试一次",
    "timeout": "邮箱服务器太久没回应",
    "tls_or_connection_failed": "连不上邮箱服务器：检查服务器地址、端口和网络",
}


class AccountError(ValueError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Account:
    name: str
    address: str
    label: str
    primary: bool

    @property
    def short(self) -> str:
        return self.label or self.address


def from_settings(mail: dict) -> list[Account]:
    primary = Account(str(mail.get("account_name") or ""), str(mail.get("address") or "").lower(),
                      str(mail.get("label") or "")[:40], True)
    out = [primary]
    for x in mail.get("extra_accounts") or []:
        if not isinstance(x, dict):
            continue
        name, address = str(x.get("name") or ""), str(x.get("address") or "").lower()
        if NAME.match(name) and EMAIL.match(address) and all(a.name != name for a in out):
            out.append(Account(name, address, str(x.get("label") or "")[:40], False))
    return out


def to_settings(accounts: list[Account]) -> list[dict]:
    """mail.extra_accounts for everything but the primary account."""
    return [{"name": a.name, "address": a.address, "label": a.label} for a in accounts if not a.primary]


def name_for(address: str, taken: set[str]) -> str:
    """An account name for mcp-email-server: letters of the address plus a random tail. The tail matters: a removed
    account keeps its name reserved there for good, and no listing shows it, so the same address added again
    (after a wrong password, or a logout) needs a name never used before."""
    local, _, domain = address.partition("@")
    base = (re.sub(r"[^A-Za-z0-9]", "", local)[:20] or "mail") + "_" + (re.sub(r"[^A-Za-z0-9]", "", domain.split(".")[0])[:12] or "x")
    lowered = {t.lower() for t in taken}
    while True:
        name = f"{base}_{secrets.token_hex(2)}"
        if name.lower() not in lowered:
            return name


def servers_for(address: str, given: dict, company: dict | None) -> dict:
    """IMAP/SMTP settings: what the user typed, else a known provider's, else those of the company mailbox when the
    address is on the same domain. Raises AccountError when none of these applies."""
    domain = address.rpartition("@")[2].lower()
    if domain in OAUTH_ONLY:
        raise AccountError("Outlook / Hotmail 邮箱现在只能用微软账号网页登录（OAuth），KOL Mail 暂时接不了")
    if given.get("imap_host"):
        imap_security = given.get("imap_security") if given.get("imap_security") in ("ssl", "starttls") else "ssl"
        smtp_security = given.get("smtp_security") if given.get("smtp_security") in ("ssl", "starttls") else "ssl"
        out = {"imap_host": str(given["imap_host"]).strip(), "imap_port": _port(given.get("imap_port"), 993),
               "imap_security": imap_security, "smtp_host": str(given.get("smtp_host") or "").strip(),
               "smtp_port": _port(given.get("smtp_port"), 465 if smtp_security == "ssl" else 587),
               "smtp_security": smtp_security}
    elif domain in PROVIDERS:
        ih, ip, isec, sh, sp, ssec = PROVIDERS[domain]
        out = {"imap_host": ih, "imap_port": ip, "imap_security": isec, "smtp_host": sh, "smtp_port": sp,
               "smtp_security": ssec}
    elif company and company.get("domain") == domain:
        out = {k: company[k] for k in ("imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port",
                                        "smtp_security")}
    else:
        raise AccountError("不认识这个邮箱的服务器，请在“服务器设置”里填收信（IMAP）和发信（SMTP）服务器")
    for key in ("imap_host", "smtp_host"):
        if not re.fullmatch(r"[A-Za-z0-9.-]{3,253}", out[key] or ""):
            raise AccountError("服务器地址不对，应该像 imap.example.com 这样")
    return out


def _port(value, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 0 < port < 65536 else default


def endpoint_security(ep: dict) -> str:
    return "starttls" if ep.get("start_ssl") or ep.get("starttls") else "ssl"


class Cli:
    """mcp-email-server's command line, run as a child process. Secrets go to its stdin as UTF-8 without a BOM."""

    def __init__(self, exe: str):
        self.exe = exe

    async def run(self, *args: str, secrets: tuple[str, ...] = (), timeout: float = 120) -> tuple[int, str]:
        data = "".join(f"{s}\n" for s in secrets).encode("utf-8")  # before starting: a bad text never leaves it waiting
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        # plain tracebacks: a crash report must never print the local variables (the password among them)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "_TYPER_STANDARD_TRACEBACK": "1"}
        proc = await asyncio.create_subprocess_exec(
            self.exe, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env, **extra)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(data), timeout)
        except asyncio.TimeoutError:
            await kill_tree(proc)
            return -1, f"邮件组件 {timeout:.0f} 秒没有响应"
        text = out.decode("utf-8", "replace").strip()
        for s in secrets:
            if s:
                text = text.replace(s, "***")
        return proc.returncode, text

    async def json(self, *args: str, secrets: tuple[str, ...] = ()) -> dict:
        code, out = await self.run(*args, "--json", secrets=secrets)
        doc = None
        for line in reversed(out.splitlines()):  # the JSON document is the last line; warnings may come first
            try:
                doc = json.loads(line)
                break
            except ValueError:
                continue
        if code != 0 or not isinstance(doc, dict) or not doc.get("ok"):
            err = doc.get("error") if isinstance(doc, dict) and isinstance(doc.get("error"), dict) else {}
            raise AccountError(ERRORS_ZH.get(err.get("code"), err.get("message") or out[-300:]
                                             or f"邮件组件出错（退出码 {code}）"), str(err.get("code") or ""))
        return doc.get("data") or {}

    async def show(self, name: str) -> dict:
        return await self.json("account", "show", name)

    async def names(self) -> set[str]:
        data = await self.json("account", "list")
        return {a["name"] for a in data.get("accounts", [])}

    async def add(self, name: str, address: str, full_name: str, servers: dict, password: str):
        args = ["account", "add", name, "--email", address, "--full-name", full_name or address,
                "--imap-host", servers["imap_host"], "--imap-port", str(servers["imap_port"]),
                "--imap-user", address, "--smtp-host", servers["smtp_host"], "--smtp-port", str(servers["smtp_port"]),
                "--smtp-user", address, "--password-stdin"]
        args += ["--imap-ssl", "--no-imap-starttls"] if servers["imap_security"] == "ssl" else ["--no-imap-ssl", "--imap-starttls"]
        args += ["--smtp-ssl", "--no-smtp-starttls"] if servers["smtp_security"] == "ssl" else ["--no-smtp-ssl", "--smtp-starttls"]
        await self.json(*args, secrets=(password, password))

    async def test(self, name: str, role: str) -> str | None:
        """None when the connection works, else the component's one-line reason."""
        code, out = await self.run("account", "test", name, role, timeout=150)
        return None if code == 0 else (out.splitlines() or ["连接失败"])[-1][:300]

    async def remove(self, name: str) -> bool:
        """Remove the account and its stored passwords from mcp-email-server. False when a password could not be
        deleted from the credential store (the account itself is gone either way). An account that is not there any
        more counts as removed: a logout that failed after this step can be tried again."""
        if name not in await self.names():
            return True
        revision = (await self.show(name))["revision"]
        data = await self.json("account", "remove", name, "--expected-revision", str(revision), "--confirm", name)
        return not data.get("cleanup_required")


async def kill_tree(proc):
    """A venv's python.exe on Windows is a launcher whose child does the work: end both, or the account could
    still be created after we reported a failure."""
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/T", "/F", "/PID", str(proc.pid), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


def password_problem(message: str) -> bool:
    return bool(re.search(r"Authentication|provider policy rejected|Credential is unavailable|LOGIN failed|password",
                          message or "", re.IGNORECASE))
