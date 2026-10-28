"""Automatic error reports from colleagues' machines to the maintainer's POPO feedback group.

Whatever goes wrong on a colleague's PC reaches the group on its own, through the same robot webhook as the
feedback button (settings.json feedback.popo_webhook, handed out with the installer): failed requests, background
tasks that die, the errors the status bar shows (AI, mail, KOL sheet, notifications, phone, updates), and an
app that exits with an error (the service runners call report_exit). The installers send their own reports
(install.ps1, install.sh) in the same format.

A report says, in Chinese: where it happened, the error, a likely cause when the error is a known one (diagnose),
whose machine and what it runs (KOL Mail and Python version, system, AI backend), the traceback and the last lines
of the log. Every report contains "KOL Mail", the robot's security keyword, and ends with (版本 x.y.z) like a
feedback message, so scripts/triage_report.py reads it too.

The same error from one machine is sent once every 6 hours, and one machine sends at most 30 an hour: a broken loop
must not use up the robot's sending limit and crowd out the next new error. A status-bar error the app is already
retrying on its own waits half an hour first (StateWatcher): most are gone by then. What was sent when is kept in
data/autoreport.json, so a crash-and-restart loop does not resend either; an error counts as sent only once the
robot took it, so one that met no network goes again next time. Secrets (tokens, webhooks, keys) are
masked; nothing is sent from a dev copy (KOL_DEV=1) or when no webhook is configured. Sending never raises.

Standard library only: the service runners use it when the app's own dependencies are what broke."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import platform
import queue
import re
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

COOLDOWN = 6 * 3600   # seconds between two reports of the same error from one machine
COOLDOWN = 6 * 3600   # seconds between two reports of the same error from one machine
PER_HOUR = 30         # reports one machine sends in an hour at most
MAX_CHARS = 3800      # a POPO robot message is cut off somewhere above this
LOG_LINES = 25        # last log lines in a report

# What an error most likely means, first match wins; scope "mail" or "ai" hints are skipped for reports from the
# other side (an AI "Authentication failed" is not a mailbox password). Checked against the error and the last
# lines of its traceback, not the whole traceback: source lines and paths would match anything.
HINTS = [
    (r"No context found for instance", "CodeMaker 版本太旧：在同事电脑上运行 codemaker upgrade", "ai"),
    (r"or newer is required", "Claude Code 版本太旧：运行 claude update，或重新运行安装命令", "ai"),
    (r"POPO 授权过期|没有 popo-cli|popo-cli auth login",  # before the AI's "auth login"
     "popo-cli 没装好、没登录或 POPO 授权过期：让同事在终端运行 popo-cli auth login，并在 POPO 里确认授权卡片", None),
    (r"not logged in|API Error: 401|/login\b|oauth (access )?token|auth login|没登录|登录已过期",
     "AI 没登录或登录过期：让同事运行 codemaker auth login（或 claude 里 /login）", "ai"),
    (r"provider policy rejected|LOGIN failed|Authentication failed|AUTHENTICATIONFAILED|Credential is unavailable",
     "邮箱服务器不接受密码：密码或授权码不对，或者邮箱没开 IMAP/SMTP；让同事在登录页重新登录", "mail"),
    (r"spend limit|用量到上限", "AI 账号的用量到上限了（比如 Claude 的月度花费上限）：账号主人调高上限，或者等它重置", "ai"),
    (r"\bquota\b|usage limit|额度|credit balance", "AI 额度用完或被限流：等额度恢复，或换 AI 后端", "ai"),
    (r"rate limit exceeded", "GitHub 匿名接口限流（公司网络共用出口 IP）：稍后再试，或换手机热点", None),
    (r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLCertVerificationError",
     "HTTPS 证书验证失败：多半是公司网络或代理软件拦截了 HTTPS", None),
    (r"getaddrinfo|Name or service not known|nodename nor servname|Temporary failure in name resolution|"
     r"ConnectTimeout|ConnectError|ReadTimeout|WriteTimeout|PoolTimeout|RemoteProtocolError|timed out|"
     r"WinError 10060|WinError 10061|Connection refused|Network is unreachable",
     "网络不通：服务器连不上（断网、VPN、代理或防火墙）", None),
    (r"Bad Gateway|Gateway Time-?out|Service Unavailable|HTTP 50[0234]\b|下游服务|FABRIC_ENDPOINT_TIMEOUT|"
     r"POPO 返回错误 (0|5\d\d)：",
     "对方服务器临时出错（Telegram、POPO 这类服务端）：程序会自己重试，一直不好才需要处理", None),
    (r"WinError 206|filename or extension is too long|File name too long", "路径太长：安装目录或文件名太深", None),
    (r"No space left|WinError 112|磁盘空间", "磁盘满了", None),
    (r"database is locked", "数据库被占用：可能同时开着两个 KOL Mail，或者杀毒软件在扫描 data 目录", None),
    (r"PermissionError|Access is denied|WinError 5\b|Operation not permitted|被拒绝",
     "没有权限：文件被别的程序占用（Excel、杀毒软件）或目录没有写权限", None),
    (r"RPC_E_CALL_REJECTED|0x80010001|0x800AC472|Excel 正忙", "Excel 正忙：同事正在编辑单元格或开着对话框", None),
    (r"ModuleNotFoundError|ImportError|DLL load failed", "依赖缺失或损坏：让同事重新运行安装命令", None),
    (r"Smart App Control|智能应用控制|WinError 4551|blocked by your organization",
     "Windows 智能应用控制拦住了程序：在 Windows 安全中心关掉它", None),
]
MAIL_PLACES = ("邮箱", "mail", "登录")

# Never let these leave the machine (the release scan's token rules, a few more, and the webhook itself).
SECRETS = [
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),                       # Telegram bot token
    re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9-]{20,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}"),                         # Google OAuth client secret
    re.compile(r"https?://\S*(?:open-apis[/]robots|[/]hook[/])\S*"),  # webhook addresses ([/]: or the release scan
                                                                       # would take this line for one)
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),       # Authorization headers
    re.compile(r"(?i)(cookie|set-cookie)\s*:\s*[^\n]+"),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)(['\"]?\s*[=:]\s*['\"]?|\s+)[^\s\"',;]{4,}"),
    re.compile(r"TELEGRAM_SESSION_STRING\s*=\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]

_lock = threading.Lock()
_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_recent_log: collections.deque = collections.deque(maxlen=LOG_LINES)
_cfg: dict = {"webhook": "", "body_key": "message", "version": "?", "state_file": None, "enabled": False,
              "who": "", "context": None}


def configure(webhook: str, version: str, state_file: Path | None, *, body_key: str = "message", who: str = "",
              enabled: bool = True, context=None):
    """context: a function returning extra facts for the 环境 line (AI backend ...). The webhook is also kept in the
    state file: a settings.json broken badly enough to crash the app can then still be reported (report_exit)."""
    _cfg.update(webhook=webhook or "", body_key=body_key or "message", version=version, state_file=state_file,
                who=who, context=context,
                enabled=bool(enabled and str(webhook or "").startswith("https://") and os.environ.get("KOL_DEV") != "1"))
    if _cfg["enabled"]:
        with _lock:
            data = _load()
            if data.get("cfg") != (cfg := {"webhook": webhook, "body_key": _cfg["body_key"], "who": who}):
                _save({**data, "cfg": cfg})


def enabled() -> bool:
    return _cfg["enabled"]


def note_log(line: str):
    """The app's log() feeds this: the last lines go along with a report (masked before they are cut short)."""
    try:
        _recent_log.append(mask(line)[:300])
    except Exception:  # noqa: BLE001  logging must never fail because of this
        pass


def mask(text: str) -> str:
    for rule in SECRETS:
        text = rule.sub(lambda m: (m.group(1) + m.group(2) + "***") if m.re.groups >= 2 and m.group(2) else "***", text)
    if _cfg["webhook"]:  # after the rules: they mask the whole address, this only the configured part of it
        text = text.replace(_cfg["webhook"], "***")
    return text


def diagnose(text: str, where: str = "") -> str:
    mail_side = any(p in where.lower() for p in MAIL_PLACES)
    for pattern, hint, scope in HINTS:
        if where and ((scope == "mail" and not mail_side) or (scope == "ai" and mail_side)):
            continue
        if re.search(pattern, text, re.IGNORECASE):
            return hint
    return ""


def signature(where: str, error: str) -> str:
    """Where plus the error without what changes from one run to the next (numbers, ids, paths, quoted values)."""
    norm = error.lower()
    norm = re.sub(r"[a-z]:[\\/][^\s'\"]*|/[\w.-]+(?:/[\w.-]+)+", "<path>", norm)
    norm = re.sub(r"(['\"]).*?\1", "<value>", norm)
    norm = re.sub(r"\b(?=[a-z_-]*\d)(?=\d*[a-z])[a-z0-9_-]{6,}\b", "<id>", norm)  # letters and digits: an id
    norm = re.sub(r"0x[0-9a-f]+|\d+(?:\.\d+)*", "<n>", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha1(f"{where}|{norm}".encode("utf-8", "replace")).hexdigest()[:12]


def environment() -> str:
    facts = [f"KOL Mail {_cfg['version']}", f"{platform.system()} {platform.release()} ({platform.version()})",
             f"Python {platform.python_version()}"]
    if _cfg["who"]:
        facts.append(_cfg["who"])
    if _cfg["context"]:
        try:
            facts += [str(f) for f in _cfg["context"]() if f]
        except Exception as e:  # noqa: BLE001  a report must go out even when a fact cannot be read
            facts.append(f"(环境读取失败：{type(e).__name__})")
    return " · ".join(facts)


def message(where: str, error: str, detail: str = "", hint: str | None = None) -> str:
    error, detail = mask(error), mask(detail)  # before anything is cut short: half a token is not recognised
    last = "\n".join(detail.strip().splitlines()[-3:])
    hint = diagnose(f"{error}\n{last}", where) if hint is None else hint
    parts = [f"【KOL Mail 自动报错 · {where}】", f"错误：{error.strip()[:600]}",
             f"可能原因：{hint or '没认出来，看下面的详情和日志'}", f"环境：{environment()}"]
    if detail.strip():
        parts.append("详情：\n" + detail.strip()[-1500:])
    if _recent_log:
        parts.append("最近日志：\n" + "\n".join(_recent_log))
    tail = f"\n\n(版本 {_cfg['version']})"
    text = "\n".join(parts)
    if len(text) + len(tail) > MAX_CHARS:
        text = text[:MAX_CHARS - len(tail) - 12] + "\n…（截断）"
    return text + tail


def _load() -> dict:
    path = _cfg["state_file"]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict):
    path = _cfg["state_file"]
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(path).with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _claim(sig: str, now: float) -> bool:
    """True when this error may be sent now; it is then held as sent until _settle says how the sending went."""
    with _lock:
        data = _load()
        sent = {k: v for k, v in (data.get("sent") or {}).items() if isinstance(v, (int, float)) and now - v < COOLDOWN}
        hour = [t for t in (data.get("hour") or []) if isinstance(t, (int, float)) and now - t < 3600]
        if sig in sent or len(hour) >= PER_HOUR:
            return False
        sent[sig] = now
        hour.append(now)
        _save({**data, "sent": sent, "hour": hour})
        return True


def _settle(sig: str, now: float, delivered: bool):
    """A report the robot did not take is not counted: the next time the error happens it is sent again."""
    if delivered:
        return
    with _lock:
        data = _load()
        sent = data.get("sent") or {}
        if sent.get(sig) == now:
            del sent[sig]
        hour = [t for t in (data.get("hour") or []) if t != now]
        _save({**data, "sent": sent, "hour": hour})


def post(text: str, timeout: float = 15) -> bool:
    try:
        body = json.dumps({_cfg["body_key"]: text}, ensure_ascii=False).encode("utf-8", "replace")
        req = urllib.request.Request(_cfg["webhook"], data=body, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310  the configured https webhook
            if r.status != 200:
                return False
            try:
                data = json.loads(r.read() or b"{}")
            except ValueError:
                return True
            code = data.get("errcode", data.get("code", 0)) if isinstance(data, dict) else 0
            return code in (0, "0", 200, "200", None)
    except Exception:  # noqa: BLE001  a report that cannot be sent must never cause another error
        return False


def _run_worker():
    while True:
        sig, now, text = _queue.get()
        try:
            _settle(sig, now, post(text))
        except Exception:  # noqa: BLE001  the worker must outlive any one report
            pass
        finally:
            _queue.task_done()


def report(where: str, error: str, detail: str = "", exc: BaseException | None = None, *, wait: bool = False) -> bool:
    """Send one report in the background (wait=True: send now, for a process about to exit). False when it was not
    sent: reporting off, already sent recently, the hourly limit reached, or (wait=True) the robot did not take it.
    Never raises."""
    global _worker
    try:
        if not _cfg["enabled"] or not error:
            return False
        if exc is not None and not detail:
            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        sig, now = signature(where, error), time.time()
        if not _claim(sig, now):
            return False
        text = message(where, error, detail)
        if wait:
            delivered = post(text)
            _settle(sig, now, delivered)
            return delivered
        with _lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run_worker, name="autoreport", daemon=True)
                _worker.start()
        _queue.put((sig, now, text))
        return True
    except Exception:  # noqa: BLE001  an odd character in an error must not turn one error into two
        return False


def flush(timeout: float = 20):
    """Wait (at most timeout seconds) until queued reports are out."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.1)


def report_exit(code: int, log_file: Path, app_dir: Path = Path(__file__).resolve().parent) -> bool:
    """For the service runners (service/run.ps1, run.sh) after the app exited with an error: a crash at startup,
    a dependency missing after an update, anything the app could not report itself. The error is the traceback's
    last line in the log, else the log's last line. Without a readable settings.json the webhook the app last ran
    with (kept in data/autoreport.json) is used."""
    state_file = app_dir / "data" / "autoreport.json"
    try:
        settings = json.loads((app_dir / "data" / "settings.json").read_text(encoding="utf-8-sig"))
        settings = settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        settings = {}
    fb, ident, mail = ((settings.get(k) or {}) if isinstance(settings.get(k), dict) else {}
                       for k in ("feedback", "identity", "mail"))
    try:
        saved = json.loads(state_file.read_text(encoding="utf-8")).get("cfg") or {}
    except (OSError, ValueError, AttributeError):
        saved = {}
    try:
        version = (app_dir / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "?"
    who = " ".join(x for x in (ident.get("name") or "", f"<{mail['address']}>" if mail.get("address") else "") if x)
    configure(fb.get("popo_webhook") or saved.get("webhook") or "", version, state_file,
              body_key=fb.get("body_key") or saved.get("body_key") or "message", who=who or saved.get("who") or "")
    try:
        lines = Path(log_file).read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError:
        lines = []
    tb = max((i for i, l in enumerate(lines) if l.startswith("Traceback (most recent call last)")), default=None)
    last_line = next((l.strip() for l in reversed(lines) if l.strip() and "exited with code" not in l), "")
    error = f"KOL Mail 退出了，退出码 {code}" + (f"：{last_line[:200]}" if last_line else "")
    if tb is not None:
        last = next((l.strip() for l in lines[tb + 1:] if re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Exit)\b", l)), "")
        error = last or error
    detail = "\n".join(lines[tb:] if tb is not None else lines[-40:])
    return report("程序退出", error[:600], detail[-2500:], wait=True)


# Errors the app gets over by itself, which only matter when they last: its own words for a retry in progress
# (mobile_loop, tell_owner, a mail session reconnecting), and a sheet change queued because a server or the network
# had a moment. A change queued for something a person has to fix (POPO or Google authorisation, no popo-cli, a
# read-only workbook) is not one of them: that goes at once.
SELF_HEALING = re.compile(
    r"自动重试中|自动补发|正在重连|"
    r"(POPO 返回错误 (0|5\d\d)：|下游服务|FABRIC_ENDPOINT_TIMEOUT|popo-cli 两分钟没有返回|暂时忙|连不上 Google（检查网络|"
    r"ConnectError|ConnectTimeout|ReadTimeout|Bad Gateway|Gateway Time-?out|Service Unavailable).*改动已排队")
PATIENCE = 30 * 60  # seconds such an error must last before it is reported
CLEAR_CHECKS = 3    # checks in a row without an error before an outage counts as over (a minute between two failures
                    # is still the same outage)
SLEEP_GAP = 5 * 60  # a gap this long between two checks: the computer slept, so nothing has "lasted" across it


def self_healing(value: str) -> bool:
    """Every part of it (the mail scan joins one error per mailbox with "；") is an error the app gets over itself."""
    parts = [p for p in value.split("；") if p.strip()]
    return bool(parts) and all(SELF_HEALING.search(p) for p in parts)


class StateWatcher:
    """Reports each new value of the app's error fields (state["claude_error"], mail errors ...) once: the same
    error stays on the status bar for hours, a changed one is a new report. A value that could not go out yet (the
    hourly limit, no network) is tried again on the next check.

    An error the app gets over by itself (self_healing) is reported only once the field has shown errors for
    PATIENCE, and then once per likely cause until the outage is over: one night of a colleague's patchy VPN sent five
    Telegram reports and three POPO ones on 2026-10-28, each gone a minute later."""

    def __init__(self, names: dict[str, str], clock=time.monotonic):
        self.names = names  # field -> where, e.g. {"claude_error": "AI"}
        self.seen: dict[str, str] = {}
        self.since: dict[str, float] = {}  # field -> when this outage began
        self.clear: dict[str, int] = {}    # field -> checks in a row without an error
        self.told: dict[str, str] = {}     # field -> likely cause (diagnose) of the lasting outage already reported
        self.last: float | None = None
        self.clock = clock

    def check(self, values: dict) -> list[str]:
        sent, now = [], self.clock()
        if self.last is not None and now - self.last > SLEEP_GAP:
            self.since.clear()
        self.last = now
        for field, where in self.names.items():
            value = str(values.get(field) or "")
            if not value:
                self.clear[field] = self.clear.get(field, 0) + 1
                if self.clear[field] >= CLEAR_CHECKS:
                    self.seen.pop(field, None)
                    self.since.pop(field, None)
                    self.told.pop(field, None)
                continue
            self.clear[field] = 0
            since = self.since.setdefault(field, now)
            if value == self.seen.get(field):
                continue
            if not self_healing(value):
                if report(where, value):
                    sent.append(field)
                    self.seen[field] = value
                continue
            cause = diagnose(value, where)
            if now - since < PATIENCE or self.told.get(field) == cause:
                continue
            if report(where, f"{value}（已经持续 {int(now - since) // 60} 分钟）"):
                sent.append(field)
                self.seen[field] = value
                self.told[field] = cause
        return sent


if __name__ == "__main__":  # python autoreport.py exited <exit code> <log file>   (the service runners)
    if len(sys.argv) == 4 and sys.argv[1] == "exited":
        report_exit(int(sys.argv[2]), Path(sys.argv[3]))
