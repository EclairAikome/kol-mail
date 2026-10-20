"""A POPO 在线表格 as usersheet.py's Grid, through popo-cli (`popo-cli popo doc_execute_table`).

The sheet is linked by its POPO docs link; the settings keep `popo:<docId>` or
`popo:<docId>:<teamSpaceId>` (team-space documents need the space id, looked up once from the link). Writes land in the
live document, like a colleague typing. Every popo-cli call takes seconds, so the cells of one sync are collected and
sent in one batchSetCells when the sync saves; a sync that fails half way writes nothing. POPO's API returns no cell
styles, so a new row cannot copy the formats of the row above: it gets the values only.

popo-cli has to be installed and logged in on this computer (`popo-cli auth login`, confirmed in POPO). Its path is
found once and kept in the settings, because the background service starts with a bare PATH.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

DOC_ID = re.compile(r"^[0-9a-f]{32}$")
CALL_TIMEOUT = 120
BATCH_CELLS = 1000  # batchSetCells takes at most this many cells


def is_popo(value) -> bool:
    return str(value or "").startswith("popo:")


def parse_setting(value: str) -> tuple[str, str]:
    """popo:<docId>[:<teamSpaceId>] -> (docId, teamSpaceId or "")."""
    parts = str(value)[len("popo:"):].split(":")
    return parts[0], parts[1] if len(parts) > 1 else ""


def doc_id_from_link(link: str) -> str:
    """The document id in a POPO docs link (the last 32-hex path segment), or "" when it is not one."""
    u = urlparse(link.strip())
    if u.scheme not in ("http", "https") or not u.netloc.startswith("docs.popo."):
        return ""
    for seg in reversed([s for s in u.path.split("/") if s]):
        if DOC_ID.match(seg.lower()):
            return seg.lower()
    return ""


def find_popo_cli(configured: str = "") -> str:
    """popo-cli's full path: the configured one, PATH, or where its installers put it."""
    candidates = [configured] if configured else []
    found = shutil.which("popo-cli")
    if found:
        candidates.append(found)
    home = Path.home()
    if sys.platform == "win32":
        for base in (os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates += [str(Path(base) / "npm" / "popo-cli.cmd"), str(Path(base) / "nodejs" / "popo-cli.cmd")]
    else:
        candidates += ["/usr/local/bin/popo-cli", "/opt/homebrew/bin/popo-cli", str(home / ".npm-global/bin/popo-cli"),
                       str(home / ".local/bin/popo-cli"), str(home / ".popo-cli/bin/popo-cli")]
        candidates += sorted(glob.glob(str(home / ".nvm/versions/node/*/bin/popo-cli")), reverse=True)
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return ""


def cli_env(cli: str) -> dict:
    """popo-cli is a node script: node usually sits next to it, which a background service's PATH lacks."""
    env = dict(os.environ)
    extra = [str(Path(cli).parent)]
    if sys.platform != "win32":
        extra += ["/usr/local/bin", "/opt/homebrew/bin"]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


class PopoError(Exception):
    pass


def unwrap(out: str) -> dict:
    """popo-cli wraps the document service's answer several times; return its innermost data, or raise PopoError with
    what to do."""
    try:
        obj = json.loads(out)
    except ValueError:
        raise PopoError(f"popo-cli 没有返回能读的结果：{out.strip()[-200:]}") from None
    code = str(obj.get("code") or "")
    if not obj.get("ok") or code not in ("FABRIC_OK", ""):
        if code in ("FABRIC_EXPIRED", "FABRIC_PENDING") or "auth" in code.lower() or "login" in code.lower():
            raise PopoError("POPO 授权过期或还没确认：在终端运行 popo-cli auth login，并在 POPO 里确认授权卡片")
        raise PopoError(f"popo-cli 出错：{code} {str(obj.get('message') or obj.get('data') or '')[:200]}")
    node = obj
    for _ in range(6):  # data.data.data...: down to the document service's {"status", "message", "data"}
        if isinstance(node, dict) and isinstance(node.get("status"), int):
            break
        inner = str(node.get("code") or "") if isinstance(node, dict) else ""
        if inner.startswith("FABRIC_") and inner != "FABRIC_OK":
            raise PopoError(f"POPO 服务暂时不可用（{inner}），稍后会自动重试")
        node = node.get("data") if isinstance(node, dict) else None
    if not isinstance(node, dict) or "status" not in node:
        raise PopoError(f"popo-cli 返回的结果看不懂：{out.strip()[:200]}")
    if node.get("status") != 1:
        msg = str(node.get("message") or "")
        if node.get("status") == 403 or "权限" in msg:
            raise PopoError(f"POPO 说没有权限读写这张表（{msg}）：确认登录 popo-cli 的是你自己的账号，并且这张表你能编辑")
        raise PopoError(f"POPO 返回错误 {node.get('status')}：{msg[:200]}")
    return node.get("data") or {}


def cell_text(value) -> str | float | int | None:
    if isinstance(value, dict) and isinstance(value.get("richText"), list):
        return "".join(str(p.get("text") or "") for p in value["richText"])
    return value


class PopoGrid:
    """usersheet.Grid for a POPO 在线表格. `run(args) -> stdout` runs popo-cli (tests replace it)."""

    mode = "popo"

    def __init__(self, setting: str, unavailable, usage, cli: str = "", run=None, sheet_ids: dict | None = None,
                 backups: Path | None = None, keep_backups: int = 14):
        self.doc, self.space = parse_setting(setting)
        self.Unavailable, self.UsageError = unavailable, usage
        self.cli = cli
        self.run = run or self._run
        self.ids = dict(sheet_ids or {})  # sheet title -> sheetId
        self.backups, self.keep_backups = backups, keep_backups
        self._rows_cache: dict[str, list[list]] = {}
        self._info: dict[str, dict] = {}  # sheetId -> {"rowCount", "hidden"} from the last read
        self.pending: dict[str, list[dict]] = {}  # sheetId -> cells to write at save()

    def __enter__(self):
        if not DOC_ID.match(self.doc):
            raise self.UsageError("POPO 表格的链接不对，请在设置里重新粘贴一次")
        return self

    def __exit__(self, *exc):
        return False

    # -- plumbing

    def _run(self, args: list[str]) -> str:
        cli = self.cli or find_popo_cli()
        if not cli:
            raise self.Unavailable("这台电脑上没有 popo-cli：装好并运行 popo-cli auth login 登录后，会自动补写排队的改动")
        try:
            r = subprocess.run([cli, *args], capture_output=True, timeout=CALL_TIMEOUT, env=cli_env(cli))
        except subprocess.TimeoutExpired:
            raise self.Unavailable("popo-cli 两分钟没有返回（网络慢或 POPO 服务忙）") from None
        except OSError as e:
            raise self.Unavailable(f"popo-cli 启动不了：{e}") from None
        out = r.stdout.decode("utf-8", "replace")
        if not out.strip():
            raise self.Unavailable(f"popo-cli 出错：{r.stderr.decode('utf-8', 'replace').strip()[-200:]}")
        return out

    def call(self, command: dict) -> dict:
        """One doc_execute_table command, passed as a JSON file (the only form that survives every shell)."""
        fd, path = tempfile.mkstemp(prefix="kolmail-popo-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(command, f, ensure_ascii=False)
            args = ["popo", "doc_execute_table", f"docId={self.doc}", f"command=@file:{path}"]
            if self.space:
                args.insert(3, f"teamSpaceId={self.space}")
            try:
                return unwrap(self.run(args))
            except PopoError as e:
                raise self.Unavailable(str(e)) from None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _id(self, sheet: str) -> str:
        if sheet not in self.ids:
            self.sheet_names()
        if sheet not in self.ids:
            raise self.UsageError(f"POPO 表格里没有叫“{sheet}”的工作表了（被改名或删掉了？），请在设置里重新对应")
        return self.ids[sheet]

    # -- Grid

    def sheet_names(self) -> list[str]:
        data = self.call({"type": "workbook.getFullData", "payload": {}})
        order = data.get("tabs") or []
        sheets = {s.get("sheetId"): s for s in data.get("sheets") or []}
        names = []
        for sid in order or list(sheets):
            s = sheets.get(sid)
            if not s or s.get("hidden"):
                continue
            title = str(s.get("title") or sid)
            self.ids[title] = sid
            self._rows_cache[title] = self._grid(s)
            self._remember(s)
            names.append(title)
        return names

    def _remember(self, sheet: dict):
        self._info[sheet.get("sheetId")] = {"rowCount": int(sheet.get("rowCount") or 0),
                                            "hidden": len(sheet.get("hiddenRows") or [])}

    @staticmethod
    def _grid(sheet: dict) -> list[list]:
        cells = sheet.get("cells") or []
        if not cells:
            return []
        nrows = max(c["row"] for c in cells) + 1
        ncols = max(c["col"] for c in cells) + 1
        grid: list[list] = [[None] * ncols for _ in range(nrows)]
        for c in cells:
            v = c.get("formula") if c.get("value") is None and c.get("formula") else c.get("value")
            grid[c["row"]][c["col"]] = cell_text(v)
        return grid

    def rows(self, sheet: str) -> list[list]:
        sid = self._id(sheet)
        if sheet in self._rows_cache:  # read moments ago by sheet_names
            return self._rows_cache.pop(sheet)
        data = self.call({"type": "workbook.getFullData", "payload": {"sheetId": sid}})
        found = [s for s in data.get("sheets") or [] if s.get("sheetId") == sid]
        if not found or (not found[0].get("title") and not found[0].get("cells")):
            raise self.UsageError(f"POPO 表格里找不到工作表“{sheet}”了，请在设置里重新对应")
        self._remember(found[0])
        return self._grid(found[0])

    def _set(self, sheet: str, r: int, c: int, cell: dict):
        cells = self.pending.setdefault(self._id(sheet), [])
        cells[:] = [x for x in cells if (x["row"], x["col"]) != (r - 1, c - 1)]  # the last write of a cell wins
        cells.append({"row": r - 1, "col": c - 1, **cell})

    def set_text(self, sheet: str, r: int, c: int, value: str):
        text = value[1:] if value.startswith("'") else value  # usersheet's Excel text mark
        if text.startswith(("=", "+", "-", "@")):
            self._set(sheet, r, c, {"richText": [{"text": text}]})  # rich text is never read as a formula
        else:
            self._set(sheet, r, c, {"value": text})

    def set_date(self, sheet: str, r: int, c: int, value: dt.datetime, header_row: int):
        self._set(sheet, r, c, {"value": f"{value:%Y-%m-%d %H:%M}"})  # POPO sheets keep dates as text

    def append_row(self, sheet: str, header_row: int, last_row: int, free_row: int) -> int:
        """free_row is empty in every column; past the sheet's end, rows are added first."""
        sid = self._id(sheet)
        if sid not in self._info:
            self.rows(sheet)
        info = self._info[sid]
        if free_row > info["rowCount"]:
            count = free_row - info["rowCount"]
            self.call({"type": "sheet.insertRows",
                       "payload": {"sheetId": sid, "startRowIndex": info["rowCount"] - info["hidden"], "count": count}})
            info["rowCount"] += count
        return free_row

    def save(self):
        """Send the collected cells, at most BATCH_CELLS per call."""
        for sid, cells in list(self.pending.items()):
            for i in range(0, len(cells), BATCH_CELLS):
                self.call({"type": "sheet.batchSetCells", "payload": {"sheetId": sid, "cells": cells[i:i + BATCH_CELLS]}})
            del self.pending[sid]

    def backup_once_a_day(self):
        """The sheet's values as JSON, before today's first change (POPO also keeps its own version history)."""
        if not self.backups:
            return
        self.backups.mkdir(parents=True, exist_ok=True)
        target = self.backups / f"popo_{self.doc}_{dt.date.today():%Y%m%d}.json"
        if target.exists():
            return
        data = self.call({"type": "workbook.getFullData", "payload": {}})
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for f in sorted(self.backups.glob(f"popo_{self.doc}_*.json"))[:-self.keep_backups]:
            try:
                f.unlink()
            except OSError:
                pass


def resolve_link(link: str, cli: str, run=None) -> str:
    """popo:<docId>[:<teamSpaceId>] for a POPO docs link; team-space documents get their space id from POPO."""
    doc = doc_id_from_link(link)
    if not doc:
        raise PopoError("不是 POPO 在线表格的链接（在 POPO 里打开那张表，复制浏览器地址栏里的链接）")
    if "/team/" not in urlparse(link).path:
        return f"popo:{doc}"
    runner = run or (lambda args: subprocess.run([cli, *args], capture_output=True, timeout=CALL_TIMEOUT,
                                                 env=cli_env(cli)).stdout.decode("utf-8", "replace"))
    u = urlparse(link.strip())
    clean = urlunparse((u.scheme, u.netloc, u.path, "", "", ""))  # no query: an "&" would split the command in cmd.exe
    data = unwrap(runner(["popo", "doc_get_folder_path", f"url={clean}"]))
    space = str(data.get("teamSpaceId") or "")
    return f"popo:{doc}:{space}" if space else f"popo:{doc}"


def auth_status(cli: str) -> dict:
    """{"installed", "logged_in", "user"} from `popo-cli auth status`."""
    if not cli:
        return {"installed": False, "logged_in": False, "user": ""}
    try:
        r = subprocess.run([cli, "auth", "status"], capture_output=True, timeout=60, env=cli_env(cli))
        obj = json.loads(r.stdout.decode("utf-8", "replace"))
        data = obj.get("data") or {}
        return {"installed": True, "logged_in": bool(obj.get("ok") and data.get("has_access_token")),
                "user": str(data.get("uid") or "")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"installed": True, "logged_in": False, "user": ""}
