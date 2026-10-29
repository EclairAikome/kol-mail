"""Telegram login helper for the settings page (tgsetup.py starts it). Runs with the Python of telegram-mcp's venv,
which has Telethon; KOL Mail's own venv does not.

    python -I tglogin.py login     JSON lines on stdin and stdout, one reply per request:
        {"cmd": "start", "api_id": 123, "api_hash": "...", "phone": "+..."} -> {"ok": true, "step": "code", "via": ...}
        {"cmd": "code", "code": "12345"}       -> {"ok": true, "step": "password"} or the "done" reply below
        {"cmd": "password", "password": "..."} -> {"ok": true, "step": "done", "session": "...", "me": {...}}
        {"cmd": "cancel"} or end of stdin      -> disconnect and exit
    python -I tglogin.py whoami    TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION_STRING from the environment
                                   -> {"ok": true, "me": {"id", "name", "username"}}
    python -I tglogin.py logout    the same environment -> {"ok": true, "logged_out": bool}: the session is ended on
                                   Telegram's side, so it leaves the account's device list

A failure is {"ok": false, "error": "<Chinese message for the page>", "code": "...", "wait": seconds (flood only)}.
Nothing secret is ever printed except the session string in the one "done" reply, which the parent stores and never
logs. The login lives in this process because Telethon keeps the code's hash on the client between the two steps."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

try:
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    from telethon.tl import types
except ImportError:  # the venv is incomplete: say so in the protocol, not as a traceback
    sys.stdout.write(json.dumps({"ok": False, "code": "no_telethon",
                                 "error": "Telegram 组件里缺少 Telethon，请在设置里重新安装 Telegram 组件"}) + "\n")
    sys.stdout.flush()
    sys.exit(2)

DEVICE = "KOL Mail"  # what the account's Settings > Devices shows
CONNECT_TIMEOUT = 70  # Telethon retries on its own below this; the parent gives up a little later
RETRYABLE = {"code_invalid", "password_invalid"}  # the same login continues; anything else ends it
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def reply(doc: dict) -> None:
    sys.stdout.write(json.dumps(doc, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def wait_text(seconds: int) -> str:
    if seconds >= 3600:
        return f"{(seconds + 3599) // 3600} 小时"
    if seconds >= 60:
        return f"{(seconds + 59) // 60} 分钟"
    return f"{max(seconds, 1)} 秒"


def me_of(user) -> dict:
    name = " ".join(x for x in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if x)
    return {"id": int(user.id), "name": _CONTROL.sub("", name).strip()[:64],
            "username": getattr(user, "username", None) or None}


def failure(e: BaseException, secrets: tuple[str, ...]) -> dict:
    """The page's message for an error. Only the type and Telegram's own error name ever reach it, scrubbed of
    anything the user typed in secret."""
    if isinstance(e, errors.FloodWaitError):
        return {"ok": False, "code": "flood", "wait": int(e.seconds),
                "error": f"Telegram 暂时不让这个号再请求，请 {wait_text(int(e.seconds))}后再试"}
    known = (
        (errors.PhoneCodeInvalidError, "code_invalid", "验证码不对，请对照 Telegram 里收到的验证码再输一次"),
        (errors.PhoneCodeEmptyError, "code_invalid", "请填验证码"),
        (errors.PhoneCodeExpiredError, "code_expired", "验证码过期了，请重新发送验证码"),
        (errors.PasswordHashInvalidError, "password_invalid", "两步验证密码不对，请再输一次"),
        (errors.PhoneNumberInvalidError, "phone_invalid", "手机号不对，要带国家区号，比如 +8613812345678"),
        (errors.PhoneNumberBannedError, "phone_banned", "这个手机号被 Telegram 封禁了"),
        (errors.PhoneNumberUnoccupiedError, "phone_unregistered", "这个手机号还没注册 Telegram，先在手机上注册"),
        (errors.PhoneNumberFloodError, "flood", "这个手机号请求验证码太频繁了，请过几个小时再试"),
        (errors.PhonePasswordFloodError, "flood", "两步验证密码试错太多次了，请过几个小时再试"),
        (errors.ApiIdInvalidError, "api_invalid", "API ID 和 API Hash 对不上，请回 my.telegram.org 重新复制"),
        (errors.ApiIdPublishedFloodError, "api_invalid", "这个 API ID 被 Telegram 限制了，请用自己在 my.telegram.org 申请的"),
        (errors.SendCodeUnavailableError, "send_unavailable", "Telegram 现在发不了验证码，请过一会儿再试"),
        (errors.AuthKeyUnregisteredError, "unauthorized", "这个登录已经失效了"),
        (errors.SessionRevokedError, "unauthorized", "这个登录已经在别处被退出了"),
        (errors.UserDeactivatedError, "unauthorized", "这个 Telegram 号已经注销"),
    )
    for kind, code, text in known:
        if isinstance(e, kind):
            return {"ok": False, "code": code, "error": text}
    if isinstance(e, (ConnectionError, OSError, asyncio.TimeoutError)):
        return {"ok": False, "code": "network", "error": "连不上 Telegram 服务器，请检查这台电脑能不能上 Telegram"}
    if isinstance(e, errors.RPCError):
        detail = str(getattr(e, "message", "") or type(e).__name__)
    else:
        detail = type(e).__name__
    for s in secrets:
        if s:
            detail = detail.replace(s, "***")
    return {"ok": False, "code": "telegram", "error": f"Telegram 拒绝了这次登录（{detail[:120]}）"}


def client_for(session: str, api_id: int, api_hash: str) -> TelegramClient:
    # flood_sleep_threshold=0: a flood wait comes back to the page with its seconds instead of a silent sleep
    return TelegramClient(StringSession(session), api_id, api_hash, device_model=DEVICE, timeout=15,
                          connection_retries=3, retry_delay=2, flood_sleep_threshold=0)


def code_channel(sent) -> str:
    kind = getattr(sent, "type", None)
    if isinstance(kind, types.auth.SentCodeTypeApp):
        return "app"
    if isinstance(kind, (types.auth.SentCodeTypeSms, types.auth.SentCodeTypeSmsWord, types.auth.SentCodeTypeSmsPhrase,
                         types.auth.SentCodeTypeFirebaseSms)):
        return "sms"
    if isinstance(kind, types.auth.SentCodeTypeSetUpEmailRequired):
        return "email_setup"
    return "other"


async def read_message() -> dict | None:
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        return None
    try:
        doc = json.loads(line)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


async def login() -> int:
    client: TelegramClient | None = None
    phone, secrets = "", ()
    try:
        while True:
            msg = await read_message()
            if msg is None or msg.get("cmd") == "cancel":
                return 0
            cmd = msg.get("cmd")
            try:
                if cmd == "start":
                    if client is not None:
                        await client.disconnect()
                    api_hash, phone = str(msg["api_hash"]), str(msg["phone"])
                    secrets = (api_hash,)
                    client = client_for("", int(msg["api_id"]), api_hash)
                    await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
                    sent = await client.send_code_request(phone)
                    via = code_channel(sent)
                    if via == "email_setup":
                        reply({"ok": False, "code": "email_setup",
                               "error": "Telegram 要求这个号先在手机上设置登录邮箱，设置好再来发送验证码"})
                        return 1
                    reply({"ok": True, "step": "code", "via": via})
                    continue
                if client is None:
                    reply({"ok": False, "code": "no_login", "error": "请先发送验证码"})
                    continue
                if cmd == "code":
                    code = str(msg.get("code") or "")
                    secrets = (*secrets[:1], code)
                    try:
                        user = await client.sign_in(phone=phone, code=code)
                    except errors.SessionPasswordNeededError:
                        reply({"ok": True, "step": "password"})
                        continue
                elif cmd == "password":
                    password = str(msg.get("password") or "")
                    secrets = (*secrets[:1], password)
                    user = await client.sign_in(password=password)
                else:
                    reply({"ok": False, "code": "bad_request", "error": "登录助手看不懂这个请求"})
                    continue
                reply({"ok": True, "step": "done", "session": StringSession.save(client.session), "me": me_of(user)})
                return 0
            except Exception as e:  # noqa: BLE001  every failure becomes one protocol line
                doc = failure(e, secrets)
                reply(doc)
                if doc["code"] not in RETRYABLE:
                    return 1
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001  exiting anyway
                pass


async def with_session(mode: str) -> int:
    api_id, api_hash = os.environ.get("TELEGRAM_API_ID", ""), os.environ.get("TELEGRAM_API_HASH", "")
    session = os.environ.get("TELEGRAM_SESSION_STRING", "")
    if not (api_id.isdigit() and api_hash and session):
        reply({"ok": False, "code": "bad_request", "error": "缺少 Telegram 的 API ID、API Hash 或登录信息"})
        return 2
    client = client_for(session, int(api_id), api_hash)
    try:
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            if mode == "logout":  # already ended on Telegram's side: nothing left to log out
                reply({"ok": True, "logged_out": True})
                return 0
            reply({"ok": False, "code": "unauthorized", "error": "这个登录已经失效了"})
            return 1
        if mode == "whoami":
            reply({"ok": True, "me": me_of(await client.get_me())})
        else:
            reply({"ok": True, "logged_out": bool(await client.log_out())})
        return 0
    except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError) as e:
        if mode == "logout":
            reply({"ok": True, "logged_out": True})
            return 0
        reply(failure(e, (api_hash, session)))
        return 1
    except Exception as e:  # noqa: BLE001
        reply(failure(e, (api_hash, session)))
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "login":
        return asyncio.run(login())
    if mode in ("whoami", "logout"):
        return asyncio.run(with_session(mode))
    reply({"ok": False, "code": "bad_request", "error": "用法：tglogin.py login | whoami | logout"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
