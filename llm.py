"""The AI backend: one call shape (system prompt, untrusted user text, JSON schema) over two command-line agents.

- claude:    Claude Code CLI in print mode, all tools off, structured output via --json-schema.
- codemaker: CodeMaker CLI (an opencode fork), `codemaker run --format json` with an agent whose tools are all off.
             CodeMaker has no schema flag, so the schema goes into the system prompt and the JSON is parsed from the
             reply text. Models are named without their provider here; the full provider/model id is looked up once
             in `codemaker models`.

The model sees text, plus the image or PDF files a caller passes (what a KOL sent, already checked by kol-mail),
and can only answer with text. Both CLIs add their own context to the user turn (Claude Code appends reminders such
as <ip_reminder>); answers are scrubbed of those tags, and callers that show model text as a translation must still
check it (see looks_chinese).
"""

from __future__ import annotations

import asyncio
import base64
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROLES = ("translate", "draft")
MODELS = {
    "claude": {"translate": "claude-sonnet-5-5", "draft": "claude-opus-5-5"},
    "codemaker": {"translate": "gpt-5.6-terra", "draft": "gpt-6.1-sol"},  # CodeMaker offers no Claude models
}
_codemaker_ids: dict[str, str] = {}  # bare model name -> provider/model, from `codemaker models`
UNAVAILABLE_MARKERS = ("log in", "login", "/login", "limit", "credit", "overloaded", "authenticat", "quota",
                       "登录", "额度", "版本太旧")
# What a colleague should do about the errors the CLIs commonly give; the raw message stays in brackets.
LOGIN_HINT = {
    "claude": "Claude Code 没登录或登录已过期：打开 PowerShell 运行 claude，输入 /login 登录，然后在页面上点“重新检测 AI”",
    "codemaker": "CodeMaker 没登录或登录已过期：打开 PowerShell 运行 codemaker auth login，然后在页面上点“重新检测 AI”",
}
OLD_CLI = re.compile(r"or newer is required|No context found for instance", re.IGNORECASE)  # the second: old CodeMaker
LOGGED_OUT = re.compile(r"authenticat|API Error: 401|oauth (access )?token|not logged in|\blog ?in\b|/login\b|"
                        r"unauthori[sz]ed", re.IGNORECASE)
OLD_CLI_HINT = {
    "claude": "Claude Code 版本太旧，不支持现在用的模型：打开 PowerShell 运行 claude update（不行就重新运行 KOL Mail 安装命令）",
    "codemaker": "CodeMaker 版本太旧：打开 PowerShell 运行 codemaker upgrade，更新好后在页面上点“重新检测 AI”（不行就按公司指引重装 CodeMaker）",
}
# CodeMaker may run git in the background (plugins, skill repositories): it must fail quietly, never pop up a
# Git Credential Manager window in front of a colleague.
NO_GIT_PROMPTS = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
QUOTA = re.compile(r"quota|credit|rate limit|usage limit|overloaded|额度", re.IGNORECASE)
# Claude Code on 2026-10-28: "You've hit your monthly spend limit · raise it at https://claude.ai/settings/usage ..."
SPEND_LIMIT = re.compile(r"spend limit|hit your (?:usage|session|daily|weekly|monthly) limit", re.IGNORECASE)
# Context the CLIs inject into the user turn. A model that copies its input (a lazy "translation") copies these too.
INJECTED_TAG = re.compile(r"<(system-reminder|ip_reminder|user-prompt-submit-hook|env|context)\b[^>]*>.*?</\1>\s*",
                          re.DOTALL | re.IGNORECASE)
INJECTED_OPEN = re.compile(r"</?(system-reminder|ip_reminder|user-prompt-submit-hook)\b[^>]*>", re.IGNORECASE)


class LLMError(Exception):
    """This particular call failed (bad output, timeout)."""


class LLMUnavailable(LLMError):
    """The backend itself cannot run right now (missing, logged out, out of quota)."""


# ---------------------------------------------------------------- settings

class Settings:
    """Which backend and models to use. Values come from the caller's config; the environment can override them."""

    def __init__(self, backend: str = "claude", models: dict | None = None, claude_bin: str = "",
                 codemaker_bin: str = "", workdir: Path | None = None):
        self.backend = os.environ.get("KOL_LLM_BACKEND", backend) or "claude"
        if self.backend not in MODELS:
            raise ValueError(f"未知的 AI 后端：{self.backend}（只支持 claude 或 codemaker）")
        chosen = dict(MODELS[self.backend])
        chosen.update({k: v for k, v in (models or {}).items() if k in ROLES and v})
        for role in ROLES:
            env = os.environ.get(f"KOL_{role.upper()}_MODEL")
            if env:
                chosen[role] = env
        self.models = chosen
        self.claude_bin = os.environ.get("KOL_CLAUDE_BIN", claude_bin)
        self.codemaker_bin = os.environ.get("KOL_CODEMAKER_BIN", codemaker_bin)
        self.workdir = workdir or Path(tempfile.gettempdir()) / "kol-mail-llm"

    def binary(self) -> str | None:
        return find_claude(self.claude_bin) if self.backend == "claude" else find_codemaker(self.codemaker_bin)

    def label(self) -> str:
        return "Claude Code" if self.backend == "claude" else "CodeMaker"


def find_claude(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    if sys.platform == "darwin":
        return _newest(MAC_CLAUDE_PATTERNS)
    # The desktop app ships its own CLI; the Store (MSIX) build keeps AppData under its package folder.
    patterns = [
        os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "*", "claude.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_*", "LocalCache", "Roaming",
                     "Claude", "claude-code", "*", "*", "claude.exe"),
    ]
    candidates = [p for pattern in patterns for p in glob.glob(pattern)]
    return max(candidates, key=os.path.getmtime) if candidates else None


def find_codemaker(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    on_path = shutil.which("codemaker")
    if on_path:
        return on_path
    if sys.platform == "darwin":  # the official install script puts it here and adds it to ~/.zshrc only
        default = Path.home() / ".codemaker" / "bin" / "codemaker"
        return str(default) if default.exists() else None
    default = Path(os.environ.get("USERPROFILE", "")) / ".codemaker" / "bin" / "codemaker.exe"
    return str(default) if default.exists() else None


# macOS: a launchd service gets no shell PATH, so the installer writes the full path into settings.json; these are
# only the fallbacks. Native installer, Homebrew (Apple silicon, Intel), npm's usual prefixes, the desktop app's CLI.
MAC_CLAUDE_PATTERNS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.nvm/versions/node/*/bin/claude",
    "~/Library/Application Support/Claude/claude-code/*/claude",
    "~/Library/Application Support/Claude/claude-code/*/*/claude",
    "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude",
)


def _newest(patterns) -> str | None:
    found = [p for pattern in patterns for p in glob.glob(os.path.expanduser(pattern))
             if os.path.isfile(p) and os.access(p, os.X_OK)]
    return max(found, key=os.path.getmtime) if found else None


# ---------------------------------------------------------------- output handling

def scrub(value):
    """Remove CLI-injected reminder blocks from every string in a model answer."""
    if isinstance(value, str):
        return INJECTED_OPEN.sub("", INJECTED_TAG.sub("", value))
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


def had_injection(value) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return bool(INJECTED_OPEN.search(text))


CJK = re.compile(r"[㐀-䶿一-鿿]")
LATIN_WORD = re.compile(r"[A-Za-zÀ-ÿĀ-žА-я]{2,}")


def looks_chinese(text: str) -> bool:
    """True when the text reads as Chinese: links, names and the signature may stay in Latin script, but most of
    the words must be Chinese. Guards against a 'translation' that is the original copied back."""
    plain = re.sub(r"https?://\S+|\S+@\S+", " ", text or "")
    han = len(CJK.findall(plain))
    words = len(LATIN_WORD.findall(plain))
    return han >= 4 and han >= words


def parse_json_text(text: str) -> dict:
    """The JSON object in a free-text answer: a bare object, one in a ``` fence, or the outermost {...}."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise LLMError("AI 没有按格式返回结果")


def check_required(obj: dict, schema: dict) -> dict:
    missing = [k for k in schema.get("required", []) if k not in obj]
    if missing:
        raise LLMError(f"AI 返回结果缺少字段：{', '.join(missing)}")
    return obj


def unavailable(message: str) -> bool:
    low = message.lower()
    return any(m in low for m in UNAVAILABLE_MARKERS)


def explain(message: str, backend: str) -> str:
    """The CLI's error with what to do about it in front, for the errors a colleague can fix themselves."""
    if not message:
        return message
    if OLD_CLI.search(message):
        hint = OLD_CLI_HINT.get(backend, OLD_CLI_HINT["claude"])
    elif LOGGED_OUT.search(message):
        hint = LOGIN_HINT.get(backend, LOGIN_HINT["claude"])
    elif SPEND_LIMIT.search(message):
        hint = "AI 账号的用量到上限了：在账号的用量设置里调高上限，或者等它重置（重置时间见原始信息）"
    elif QUOTA.search(message):
        hint = "AI 额度用完或服务繁忙，稍后再试"
    else:
        return message
    return f"{hint}（原始信息：{message}）"


MODEL_ID = re.compile(r"[\w.-]+/[\w.:-]+")


def _key(model: str) -> str:
    return re.sub(r"[\s_-]", "", model.lower())


def match_models(listing: str, model: str) -> list[str]:
    """provider/model ids from `codemaker models` whose model part is `model`, ignoring case, spaces, dashes and
    underscores ("gpt6.1 sol" finds "openai/gpt-6.1-sol", but "gpt-5.6" never "gpt-56"); CodeMaker's own provider
    first. Lines that are not a bare provider/model id are not ids."""
    want = _key(model)
    ids = [line.strip() for line in listing.splitlines()
           if MODEL_ID.fullmatch(line.strip()) and _key(line.strip().rsplit("/", 1)[1]) == want]
    return sorted(ids, key=lambda i: "codemaker" not in i.split("/", 1)[0])


# ---------------------------------------------------------------- the call

def _fail(message: str, backend: str):
    """Raise the CLI's error, explained; LLMUnavailable when the backend itself cannot run (logged out, too old)."""
    explained = explain(message, backend)
    raise (LLMUnavailable if unavailable(explained) else LLMError)(explained)


FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".pdf": "application/pdf"}


def file_type(path: Path) -> str:
    """The media type of a file the model may be shown; ValueError for anything else."""
    kind = FILE_TYPES.get(Path(path).suffix.lower())
    if not kind:
        raise ValueError(f"AI 只能看图片和 PDF：{Path(path).name}")
    return kind


def claude_message(user_text: str, files: list[Path]) -> bytes:
    """One stream-json user turn: the text, then each file as an image or a PDF document block."""
    content = [{"type": "text", "text": user_text}]
    for f in files:
        kind = file_type(f)
        data = base64.b64encode(Path(f).read_bytes()).decode("ascii")
        content.append({"type": "document" if kind == "application/pdf" else "image",
                        "source": {"type": "base64", "media_type": kind, "data": data}})
    line = {"type": "user", "message": {"role": "user", "content": content}}
    return (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")


async def complete(settings: Settings, system_prompt: str, user_text: str, schema: dict, role: str,
                   timeout: int, files: list[Path] | None = None) -> dict:
    """Run one call and return the JSON object the schema describes, scrubbed of injected reminder blocks.
    files: images or PDFs shown to the model along with user_text."""
    if role not in ROLES:
        raise ValueError(role)
    files = [Path(f) for f in files or []]
    for f in files:
        file_type(f)
    binary = settings.binary()
    if not binary:
        raise LLMUnavailable(f"找不到 {settings.label()} 命令行")
    model = settings.models[role]
    settings.workdir.mkdir(parents=True, exist_ok=True)
    if settings.backend == "claude":
        result = await _claude(binary, model, system_prompt, user_text, schema, settings.workdir, timeout, files)
    else:
        result = await _codemaker(binary, model, system_prompt, user_text, schema, settings.workdir, timeout, files)
    return check_required(scrub(result), schema)


async def _communicate(proc, data: bytes | None, timeout: int, label: str) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(proc.communicate(data), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise LLMError(f"{label} 超过 {timeout} 秒没有返回")
    except asyncio.CancelledError:
        proc.kill()
        raise


async def _claude(binary: str, model: str, system_prompt: str, user_text: str, schema: dict, cwd: Path,
                  timeout: int, files: list[Path] | None = None) -> dict:
    # The prompt (with the whole playbook) goes through a private file: a command line is readable by other local
    # processes and Windows caps it at 32K characters.
    prompt_file = cwd / f"prompt-{secrets.token_hex(6)}.md"
    prompt_file.write_text(system_prompt, encoding="utf-8")
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")  # mail text must never meet personal memories
    # Plain stdin carries text only; files go as content blocks of one stream-json turn, still with every tool off.
    streamed = ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"] if files else \
        ["--output-format", "json"]
    stdin = claude_message(user_text, files) if files else user_text.encode("utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-p",
            "--model", model,
            "--tools", "",
            "--disallowedTools", "mcp__*",
            "--strict-mcp-config",
            *streamed,
            "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--system-prompt-file", str(prompt_file),
            "--setting-sources", "local",
            "--disable-slash-commands",
            "--no-session-persistence",
            cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await _communicate(proc, stdin, timeout, "Claude Code")
    finally:
        try:
            prompt_file.unlink()
        except OSError:
            pass
    try:
        obj = stream_result(out) if files else json.loads(out.decode("utf-8", "replace"))
    except ValueError:  # not the CLI's JSON at all: a broken call, not proof the backend is unavailable
        raise LLMError(explain((err or out).decode("utf-8", "replace")[:300] or "Claude Code 输出无法解析", "claude"))
    if obj.get("is_error"):
        _fail(str(obj.get("result") or obj.get("terminal_reason") or "Claude Code 调用失败")[:300], "claude")
    structured = obj.get("structured_output")
    if isinstance(structured, dict):
        return structured
    return parse_json_text(str(obj.get("result", "")))


def stream_result(out: bytes) -> dict:
    """The final "result" event of a stream-json run (the same object --output-format json prints)."""
    for line in reversed(out.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        event = json.loads(line)
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    raise ValueError("no result event")


CODEMAKER_FORMAT = """

Answer with exactly one JSON object and nothing else (no prose, no code fence). It must follow this JSON Schema:
{schema}
"""


async def codemaker_model_id(binary: str, model: str) -> str:
    """CodeMaker wants provider/model. A bare name is matched against `codemaker models` (once per name), preferring
    the provider CodeMaker logs you in with; a name that already has a provider is used as it is."""
    if "/" in model:
        return model
    if model in _codemaker_ids:
        return _codemaker_ids[model]
    proc = await asyncio.create_subprocess_exec(binary, "models", stdin=asyncio.subprocess.DEVNULL,
                                                env={**os.environ, **NO_GIT_PROMPTS},
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    # None, not b"": stdin is not a pipe, and Python 3.11 writes even empty input to it ("'NoneType' object has no
    # attribute 'write'"), so this lookup, and with it every CodeMaker call, failed on 3.11
    out, err = await _communicate(proc, None, 120, "CodeMaker")
    ids = match_models(out.decode("utf-8", "replace"), model)
    if not ids:
        msg = (err or b"").decode("utf-8", "replace").strip()[-200:]
        if unavailable(msg):
            raise LLMUnavailable(explain(msg, "codemaker"))
        raise LLMError(f"CodeMaker 里没有模型 {model}（在 PowerShell 运行 codemaker models 看看有哪些，"
                       f"在 settings.json 的 llm.models 里改）" + (f"：{msg}" if msg else ""))
    _codemaker_ids[model] = ids[0]
    return ids[0]


async def _codemaker(binary: str, model: str, system_prompt: str, user_text: str, schema: dict, workdir: Path,
                     timeout: int, files: list[Path] | None = None) -> dict:
    model = await codemaker_model_id(binary, model)
    # The agent (system prompt, no tools) is defined in a codemaker.json of a fresh folder per call, so parallel
    # calls never share a prompt and nothing is left in a project folder. It replaces the default agent "build"
    # instead of being picked with --agent: CodeMaker before 0.4 (1.15.x) crashes on run --agent with
    # "No context found for instance", and colleagues do not always update CodeMaker.
    run_dir = workdir / f"cm-{secrets.token_hex(6)}"
    run_dir.mkdir(parents=True)
    agent = {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": "build",  # even when the colleague's own config names another default
        "agent": {"build": {
            "mode": "primary",
            "description": "kol-mail text task, no tools",
            "prompt": system_prompt + CODEMAKER_FORMAT.format(schema=json.dumps(schema, ensure_ascii=False)),
            "tools": {"*": False},
            "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        }},
    }
    (run_dir / "codemaker.json").write_text(json.dumps(agent, ensure_ascii=False), encoding="utf-8")
    session_id = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "run", "--pure", "-m", model, "--format", "json", "--dir", str(run_dir),
            # --file=PATH, never "--file PATH": the option takes several values and would swallow a message.
            *[f"--file={Path(f).resolve()}" for f in files or []],
            cwd=str(run_dir), env={**os.environ, **NO_GIT_PROMPTS},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await _communicate(proc, user_text.encode("utf-8"), timeout, "CodeMaker")
        texts, errors = [], []
        for line in out.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            session_id = session_id or event.get("sessionID")
            if event.get("type") == "text":
                texts.append(str((event.get("part") or {}).get("text") or ""))
            elif event.get("type") == "error":
                errors.append(json.dumps(event.get("error"), ensure_ascii=False))
        if errors:
            _fail(f"CodeMaker 调用失败：{errors[0][:300]}", "codemaker")
        if not texts:
            detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
            known = [line for line in detail if OLD_CLI.search(line)]  # a crash may end with a stack frame
            _fail((known or detail)[-1][:300] if detail else "CodeMaker 没有返回内容", "codemaker")
        return parse_json_text(texts[-1])
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        if session_id:  # CodeMaker keeps every session locally; mail text should not pile up there
            _forget(binary, session_id)


def _forget(binary: str, session_id: str):
    """Delete the session in a process of its own, never as a task of the event loop: the installer's AI check
    ends its asyncio.run right after the call, and cancelling a task that is starting or waiting for a subprocess
    hangs asyncio.run for good on Python 3.12 for Windows (seen on a colleague's PC)."""
    try:
        subprocess.Popen([binary, "session", "delete", session_id], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, **NO_GIT_PROMPTS},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:  # housekeeping only
        pass
