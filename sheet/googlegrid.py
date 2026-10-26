"""A colleague's own Google Sheet as usersheet.py's Grid, through the Google Sheets API v4 over plain HTTPS: no Excel,
so Windows and the Mac work the same.

The settings keep `google:<spreadsheetId>` (`gsheet:<id>` is something else: kol.py's fixed-layout sheet on the Mac,
sheet/gsheet.py). Each colleague authorises KOL Mail once from the settings page with the `spreadsheets` scope, which
reaches the sheets they already have (drive.file only reaches sheets KOL Mail created). The token is
google-sheets-token.json in the token folder, separate from kol.py's google-token.json; refreshing it and the browser
sign-in are gsheet.py's.

One sync reads the mapped tab, collects its writes and sends them at save(): new rows, copied formats and every text
cell in one batchUpdate (text as a stringValue, which Google never reads as a formula), then the dates in one
values.batchUpdate as USER_ENTERED "YYYY-MM-DD HH:MM", so they become real date-times shown in the sheet's own date
format. Before writing, the tab is read again: if someone sorted, added or deleted rows in the browser meanwhile, the
row numbers could point at another KOL, so the sync is queued and redone instead (as gsheet.py does).
Network trouble, Google being busy, lost authorisation, missing permission and a sheet that is gone are Unavailable
(usersheet queues the changes and writes them once that is fixed); a wrong link and a tab that is gone are
UsageError, saying what to do. A cell that holds a formula is never written, whatever it shows.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

try:
    from . import gsheet  # imported as sheet.googlegrid (app.py)
except ImportError:
    import gsheet  # run from the sheet folder (usersheet.py, tests)

PREFIX = "google:"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TOKEN_FILE = "google-sheets-token.json"
REAUTH = "请在 设置 → KOL 总表 里重新授权 Google"
SHEET_ID = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
LINK_PATH = re.compile(r"^/spreadsheets(?:/u/\d+)?/d/([A-Za-z0-9_-]+)")
META_FIELDS = "sheets.properties(sheetId,title,index,hidden,sheetType,gridProperties(rowCount))"
EPOCH = dt.datetime(1899, 12, 30)  # Google's date serials count from here, like Excel's
LAST_SERIAL = 2958466  # 9999-12-31, as in usersheet.from_serial
NOT_A_LINK = ("不是 Google 表格的链接：在浏览器里打开那张表，复制地址栏里以 https://docs.google.com/spreadsheets/d/ "
              "开头的链接")
XLSX_IN_DRIVE = ("这是存在 Google 云端硬盘里的 Excel 文件（.xlsx），Google 不让程序直接改它：在表格里点 文件 → "
                 "另存为 Google 表格，再复制新表格的链接")
SCOPE_MISSING = "Google 授权里没有读写表格的权限：请重新授权，并在 Google 的页面上勾选读写 Google 表格的那一项"


class AuthError(Exception):
    """The browser sign-in gave no usable token; the message says why, in words for the settings page."""


def is_google(value) -> bool:
    return str(value or "").startswith(PREFIX)


def parse_setting(value: str) -> str:
    """google:<spreadsheetId> -> spreadsheetId ("" when it is not one)."""
    return str(value)[len(PREFIX):].strip() if is_google(value) else ""


def spreadsheet_id_from_link(link: str) -> str:
    """The spreadsheet id in a Google Sheets link (…/spreadsheets/d/<id>/edit#gid=…, …/d/<id>), a bare id or a
    google:<id> setting. Raises ValueError with what to paste instead."""
    value = str(link or "").strip().strip('"').strip("'")
    if is_google(value):
        value = parse_setting(value)
    if SHEET_ID.fullmatch(value):
        return value
    if value.lower().startswith("docs.google.com/"):
        value = "https://" + value
    u = urlparse(value)
    if u.scheme in ("http", "https") and u.netloc.lower() == "docs.google.com":
        if u.path.startswith("/spreadsheets/d/e/"):
            raise ValueError("这是“发布到网络”的只读链接：请在浏览器里打开表格本身，复制地址栏里的链接")
        m = LINK_PATH.match(u.path)
        if m and SHEET_ID.fullmatch(m.group(1)):
            if "rtpof=true" in u.query:
                raise ValueError(XLSX_IN_DRIVE)
            return m.group(1)
    raise ValueError(NOT_A_LINK)


# ---------------------------------------------------------------- authorisation (settings page)

def credentials(token_dir, unavailable=gsheet.GSheetError) -> gsheet.Credentials:
    return gsheet.Credentials(Path(token_dir), unavailable, token_file=TOKEN_FILE, reauth=REAUTH)


def authorize(token_dir, client_id: str, client_secret: str, open_browser: bool = True, timeout: float = 600,
              on_url=None) -> None:
    """Sign in with Google in the browser and keep the token in `token_dir`. Blocks until Google redirects back (or
    `timeout` seconds pass). `on_url(url)` gets the consent address before the wait, for the page to open it itself
    (with open_browser=False). Raises AuthError when the colleague declined, time ran out, or the permission to edit
    sheets was left unticked (a token that worked before is then kept)."""
    try:
        before = (Path(token_dir) / TOKEN_FILE).read_bytes()
    except OSError:
        before = None
    try:
        gsheet.authorize(Path(token_dir), client_id, client_secret, open_browser=open_browser, timeout=timeout,
                         token_file=TOKEN_FILE, scope=SCOPE, on_url=on_url,
                         done_page="授权完成，可以关掉这个页面，回到 KOL Mail。",
                         failed_page="没有完成授权：回到 KOL Mail 的 设置 → KOL 总表 里再点一次授权即可。")
    except SystemExit as e:  # gsheet.py's installer wording ends the process; here it must not end the app
        raise AuthError(f"{e}，可以在 设置 → KOL 总表 里再试一次") from None
    except httpx.HTTPError as e:
        raise AuthError(f"连不上 Google（检查网络或代理）：{type(e).__name__}") from None
    except Exception as e:  # noqa: BLE001  the page shows it; nothing raised here may end the app
        raise AuthError(f"Google 授权没有完成：{type(e).__name__}: {e}") from None
    status = auth_status(token_dir)
    if not status["authorized"]:
        if before is not None:
            with contextlib.suppress(OSError, ValueError):
                credentials(token_dir).save(json.loads(before))
        raise AuthError(status["error"])


def auth_status(token_dir) -> dict:
    """{"authorized": bool, "error": str | None} from the token file alone (no network). A token Google revoked
    since still counts as authorised here; the next sync says so."""
    try:
        data = credentials(token_dir).load()
        granted = str(data.get("scope") or "").split()
    except gsheet.GSheetError as e:
        return {"authorized": False, "error": str(e)}
    except (AttributeError, TypeError):  # valid JSON, but not an object
        return {"authorized": False, "error": f"Google 授权文件读不出来，{REAUTH}"}
    if SCOPE not in granted:
        return {"authorized": False, "error": SCOPE_MISSING}
    return {"authorized": True, "error": None}


# ---------------------------------------------------------------- the grid

def cell_value(value, shown):
    """One cell as usersheet expects it: None when empty, a datetime for a date (`shown`, the same cell read with
    dates as text, is a string only for date and time cells), else the value itself."""
    if value is None or value == "":
        return None
    if (isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(shown, str)
            and 1 <= value < LAST_SERIAL):
        return EPOCH + dt.timedelta(seconds=round(value * 86400))
    return value


class GoogleGrid:
    """usersheet.Grid for a Google Sheet. `client` is an httpx.Client (tests pass one on a fake transport)."""

    mode = "google"

    def __init__(self, setting: str, unavailable, usage, state_dir, sheet_ids: dict | None = None,
                 backups: Path | None = None, keep_backups: int = 14, client: httpx.Client | None = None):
        self.sid = parse_setting(setting)
        self.Unavailable, self.UsageError = unavailable, usage
        self.creds = credentials(state_dir, unavailable)
        self.ids = {k: str(v) for k, v in (sheet_ids or {}).items()}  # sheet title -> sheetId (gid), as text
        self.backups, self.keep_backups = backups, keep_backups
        self.client, self._own_client = client, False
        self.token: str | None = None
        self._meta: dict[str, dict] | None = None  # sheetId -> {"title", "hidden", "index", "rowCount"}
        self._seen: dict[str, list[list]] = {}  # sheetId -> its values as rows() read them, checked before writing
        self.requests: list[dict] = []  # rows added and formats copied, in order, sent at save()
        self.cells: dict[tuple[str, int, int], tuple[str, str]] = {}  # (sheetId, r, c) -> ("text" | "date", value)

    def __enter__(self):
        if not SHEET_ID.fullmatch(self.sid):
            raise self.UsageError("Google 表格的链接不对，请在 设置 → KOL 总表 里重新粘贴一次")
        return self

    def __exit__(self, *exc):
        if self._own_client and self.client is not None:
            self.client.close()
            self.client, self._own_client = None, False
        return False

    # -- plumbing

    def _call(self, method: str, path: str, **kw) -> dict:
        if self.client is None:
            try:
                self.client = gsheet.client_factory()
            except (ImportError, ValueError, httpx.InvalidURL) as e:  # e.g. a SOCKS proxy set, without socksio
                raise self.Unavailable(f"连不上 Google：这台电脑的代理设置用不了（{type(e).__name__}: {e}）") from None
            self._own_client = True
        for attempt in (1, 2):
            if self.token is None:
                self.token = self.creds.access_token(self.client)
            try:
                r = self.client.request(method, gsheet.API + path, headers={"Authorization": f"Bearer {self.token}"},
                                        **kw)
            except httpx.HTTPError as e:
                raise self.Unavailable(f"连不上 Google（检查网络或代理）：{type(e).__name__}") from None
            if r.status_code != 401 or attempt == 2:
                break
            self._forget_token()  # the access token ran out early: refresh once and try again
        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):  # e.g. a Wi-Fi login page answered instead of Google
                raise self.Unavailable("Google 返回的不是表格数据（可能是网络登录页或代理），稍后自动重试")
            return body
        raise self._failure(r)

    def _forget_token(self):
        self.token = None
        with contextlib.suppress(self.Unavailable, OSError):  # a missing file is reported by the refresh itself
            data = self.creds.load()
            data.pop("access_token", None)
            self.creds.save(data)

    def _failure(self, r: httpx.Response) -> Exception:
        code, body = r.status_code, None
        with contextlib.suppress(ValueError):
            body = r.json()
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):  # not Google's own answer: a proxy, a firewall or a Wi-Fi login page
            return self.Unavailable(f"Google 没有正常回应（HTTP {code}，可能是代理、防火墙或网络登录页），稍后自动重试")
        detail = str(error.get("message") or "")[:200]
        low, shown = detail.lower(), f"：{detail}" if detail else ""
        if code == 401:
            return self.Unavailable(f"Google 授权失效，{REAUTH}")
        if code == 429 or code >= 500 or (code == 403 and ("rate limit" in low or "quota" in low)):
            return self.Unavailable(f"Google 表格暂时忙（HTTP {code}），稍后自动重试")
        if code == 403 and "scope" in low:
            return self.Unavailable(SCOPE_MISSING)
        if code == 403 and "has not been used" in low:
            return self.Unavailable(f"KOL Mail 用的 Google 项目没有开启 Sheets API{shown}。请管理员开启，开启后自动补写")
        # No permission or no sheet: queued like an outage rather than given up on, so nothing is lost while the
        # sharing is fixed or the sheet is relinked (a relinked sheet starts a queue of its own).
        if code == 403:
            return self.Unavailable(f"Google 说你没有权限读写这张表格（HTTP 403{shown}）。确认授权时登录的 Google 账号能编辑"
                                    "这张表，或者请表格的主人把这个账号加为编辑者；改好后排队的改动会自动补写")
        if code == 404:
            return self.Unavailable("找不到这张 Google 表格（链接不对，或者表格被删除了）：请在 设置 → KOL 总表 里重新粘贴表格的链接")
        if code == 400 and "not supported for this document" in low:
            return self.UsageError(XLSX_IN_DRIVE)
        return self.Unavailable(f"Google 表格拒绝了这次请求（HTTP {code}{shown}），稍后自动重试")

    def _load_meta(self) -> dict[str, dict]:
        if self._meta is None:
            body = self._call("GET", f"/v4/spreadsheets/{self.sid}", params={"fields": META_FIELDS})
            meta = {}
            for s in body.get("sheets") or []:
                p = s.get("properties") or {}
                if p.get("sheetId") is None or (p.get("sheetType") or "GRID") != "GRID":  # charts have no cells
                    continue
                meta[str(p["sheetId"])] = {"title": str(p.get("title") or ""), "hidden": bool(p.get("hidden")),
                                           "index": int(p.get("index") or 0),
                                           "rowCount": int((p.get("gridProperties") or {}).get("rowCount") or 0)}
            self._meta = dict(sorted(meta.items(), key=lambda kv: kv[1]["index"]))
        return self._meta

    def _sheet(self, sheet: str) -> tuple[str, dict]:
        """(sheetId, its properties) of the tab mapped as `sheet`: by its id when the mapping has one (the tab may
        have been renamed since), else by its title."""
        meta = self._load_meta()
        sid = self.ids.get(sheet)
        if sid not in meta:
            sid = next((k for k, p in meta.items() if p["title"] == sheet), None)
        if sid is None:
            raise self.UsageError(f"Google 表格里没有叫“{sheet}”的工作表了（被改名或删掉了？），请在设置里重新对应")
        return sid, meta[sid]

    def _read(self, ranges: list[str], dates: str, render: str = "UNFORMATTED_VALUE") -> list[list[list]]:
        """values.batchGet: numbers unformatted, dates as SERIAL_NUMBER or FORMATTED_STRING (render FORMULA: the
        formulas as typed)."""
        params = [("ranges", r) for r in ranges] + [
            ("valueRenderOption", render), ("dateTimeRenderOption", dates), ("majorDimension", "ROWS")]
        got = self._call("GET", f"/v4/spreadsheets/{self.sid}/values:batchGet", params=params).get("valueRanges") or []
        return [(got[i].get("values") if i < len(got) else None) or [] for i in range(len(ranges))]

    # -- Grid

    def sheet_names(self) -> list[str]:
        names = []
        for sid, p in self._load_meta().items():
            if not p["hidden"]:
                self.ids[p["title"]] = sid
                names.append(p["title"])
        return names

    def rows(self, sheet: str) -> list[list]:
        """Read twice, dates as serial numbers and as the text the sheet shows: the cells that are a number in one and
        text in the other are the dates, returned as datetimes (as openpyxl does), whatever date format the sheet
        uses. Other numbers stay numbers, text stays text."""
        sid, p = self._sheet(sheet)
        rng = [gsheet.quote_tab(p["title"])]
        raw, shown = self._read(rng, "SERIAL_NUMBER")[0], self._read(rng, "FORMATTED_STRING")[0]
        self._seen[sid] = gsheet.normalized(raw)
        out = []
        for i, row in enumerate(raw):
            seen = shown[i] if i < len(shown) else []
            out.append([cell_value(v, seen[j] if j < len(seen) else None) for j, v in enumerate(row)])
        out = gsheet.normalized(out)
        width = max((len(r) for r in out), default=0)
        return [r + [None] * (width - len(r)) for r in out]

    def _set(self, sheet: str, r: int, c: int, what: tuple[str, str]):
        key = (self._sheet(sheet)[0], r, c)
        self.cells.pop(key, None)  # the last write of a cell wins
        self.cells[key] = what

    def set_text(self, sheet: str, r: int, c: int, value: str):
        self._set(sheet, r, c, ("text", value[1:] if value.startswith("'") else value))  # usersheet's Excel text mark

    def set_date(self, sheet: str, r: int, c: int, value: dt.datetime, header_row: int):
        self._set(sheet, r, c, ("date", f"{value:%Y-%m-%d %H:%M}"))

    def append_row(self, sheet: str, header_row: int, last_row: int, free_row: int) -> int:
        """free_row is empty in every column; past the sheet's end, rows are added first. The new row gets the
        formats and dropdowns of last_row (not its values or formulas)."""
        sid, p = self._sheet(sheet)
        if free_row > p["rowCount"]:
            grow = free_row - p["rowCount"]
            self.requests.append({"appendDimension": {"sheetId": int(sid), "dimension": "ROWS", "length": grow}})
            p["rowCount"] += grow
        if last_row > header_row:
            self.requests.append({"copyPaste": {
                "source": {"sheetId": int(sid), "startRowIndex": last_row - 1, "endRowIndex": last_row},
                "destination": {"sheetId": int(sid), "startRowIndex": free_row - 1, "endRowIndex": free_row},
                "pasteType": "PASTE_FORMAT"}})
        return free_row

    def _check_unchanged(self):
        """Rows found by e-mail are written by number. If a tab changed since rows() read it (a sort, a row added or
        deleted in the browser meanwhile), those numbers may point at another KOL: queue the sync and redo it."""
        meta = self._load_meta()
        sids = [sid for sid in dict.fromkeys(sid for sid, _, _ in self.cells) if sid in self._seen]
        now = self._read([gsheet.quote_tab(meta[sid]["title"]) for sid in sids], "SERIAL_NUMBER") if sids else []
        for sid, values in zip(sids, now):
            if gsheet.normalized(values) != self._seen[sid]:
                raise self.Unavailable(f"总表的“{meta[sid]['title']}”页在写入的同时被人改了（排序、删行或加行），"
                                       "这批改动稍后自动重写")
        # A cell that holds a formula is the colleague's own, even when what it shows looks empty or like one of
        # our words (an ARRAYFORMULA spills into the cells below it): never overwritten.
        formulas = self._read([gsheet.quote_tab(meta[sid]["title"]) for sid in sids], "SERIAL_NUMBER", "FORMULA") \
            if sids else []
        for sid, rows in zip(sids, formulas):
            for key in [k for k in self.cells if k[0] == sid]:
                r, c = key[1] - 1, key[2] - 1
                v = rows[r][c] if r < len(rows) and c < len(rows[r]) else None
                if isinstance(v, str) and v.startswith("="):
                    del self.cells[key]
        for sid in sids:
            del self._seen[sid]  # what follows is our own writing

    def save(self):
        """Rows, formats and text in one batchUpdate; then the dates in one values.batchUpdate (they need Google to
        read them as typed, which batchUpdate cannot do). A failure in the second leaves the first written: the queued
        retry finds those rows by e-mail and writes the rest."""
        if self.cells:
            self._check_unchanged()
        texts = [(key, v) for key, (kind, v) in self.cells.items() if kind == "text"]
        requests = self.requests + [{"updateCells": {
            "start": {"sheetId": int(sid), "rowIndex": r - 1, "columnIndex": c - 1},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": v}} if v else {}]}],
            "fields": "userEnteredValue"}} for (sid, r, c), v in texts]
        if requests:
            self._call("POST", f"/v4/spreadsheets/{self.sid}:batchUpdate", json={"requests": requests})
            self.requests = []
            for key, _ in texts:
                del self.cells[key]
        if self.cells:  # only dates are left
            meta = self._load_meta()
            data = [{"range": f"{gsheet.quote_tab(meta[sid]['title'])}!{gsheet.col_letter(c)}{r}", "values": [[v]]}
                    for (sid, r, c), (_, v) in self.cells.items()]
            self._call("POST", f"/v4/spreadsheets/{self.sid}/values:batchUpdate",
                       json={"valueInputOption": "USER_ENTERED", "data": data})
            self.cells.clear()

    def backup_once_a_day(self):
        """The mapped tab's values as JSON (every tab when none is mapped yet), before today's first change; Google
        also keeps its own version history."""
        if not self.backups:
            return
        self.backups.mkdir(parents=True, exist_ok=True)
        target = self.backups / f"google_{self.sid}_{dt.date.today():%Y%m%d}.json"
        if target.exists():
            return
        meta = self._load_meta()
        mapped = {sid: p for sid, p in meta.items() if sid in self.ids.values() or p["title"] in self.ids}
        meta = mapped or meta
        values = self._read([gsheet.quote_tab(p["title"]) for p in meta.values()], "FORMATTED_STRING") if meta else []
        sheets = [{"sheetId": sid, "title": p["title"], "values": v} for (sid, p), v in zip(meta.items(), values)]
        tmp = target.with_suffix(".tmp")  # a half-written file must not pass for today's backup
        tmp.write_text(json.dumps({"spreadsheetId": self.sid, "sheets": sheets}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        for f in sorted(self.backups.glob(f"google_{self.sid}_*.json"))[:-self.keep_backups]:
            with contextlib.suppress(OSError):
                f.unlink()
