"""KOL 总表的 Google 表格后端（Mac 版用；Windows 版继续用 xlsx + Excel COM）。

kol.py 把总表当成几张“表格”来读写（表头、整表的值、改一个格子、在底部加一行）。这里用 Google Sheets API 实现同一
套接口，记账规则（待办方、下一步、阶段、沟通记录去重）全在 kol.py 里，两个平台记出来的账一样。设置里的总表写成
“gsheet:表格ID”时 kol.py 才走这里。

- 认证：每个同事各自用 OAuth 授权一次，权限只有 drive.file，也就是只能碰 KOL Mail 自己建的表。凭证存在
  KOL_SHEET_STATE/google-token.json（权限 600）。
- 一次 kol.py 调用打开一次表：读出几个分页的全部值，所有改动先记在本地，save() 时一次 batchUpdate 写进去。写之前
  再读一遍被改动的分页，和打开时不一样（同事正好在网页上排序、删行、加行）就不写，交给 kol.py 排队，下次重来。
- 网络、授权、Google 繁忙这类问题都报 Unavailable，改动由 kol.py 排队，之后自动补写。

命令行（安装器用）：
    python gsheet.py auth --state DIR --client-id ID --client-secret SECRET   浏览器里授权一次
    python gsheet.py create --state DIR [--title 标题]                         新建一张空总表，最后一行打印表格 ID
    python gsheet.py check --state DIR --id ID                                 确认能读写这张表
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = os.environ.get("KOL_GSHEET_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URL = os.environ.get("KOL_GSHEET_TOKEN_URL", "https://oauth2.googleapis.com/token")
API = os.environ.get("KOL_GSHEET_API", "https://sheets.googleapis.com").rstrip("/")
TOKEN_FILE = "google-token.json"
TIMEOUT = httpx.Timeout(60, connect=15)
GROW_ROWS = 500
# kol.py names its tables (sheet, table) after the Excel ListObjects; in Google every table is a whole tab.
TAB_FOR = {"PROJ": "项目"}
DATE_COLUMNS = {"首次发信": "yyyy-mm-dd", "更新时间": "yyyy-mm-dd hh:mm", "时间": "yyyy-mm-dd hh:mm", "日期": "yyyy-mm-dd"}

# Tests swap this for an httpx.MockTransport-backed client.
client_factory = lambda: httpx.Client(timeout=TIMEOUT, follow_redirects=True)  # noqa: E731


class GSheetError(Exception):
    """Used when the caller did not pass its own exception classes."""


def sheet_id_of(value: str) -> str | None:
    """The spreadsheet id in a "gsheet:<id>" setting, else None."""
    value = (value or "").strip()
    return value[len("gsheet:"):].strip() or None if value.startswith("gsheet:") else None


def quote_tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def strip_text_mark(value):
    """kol.py marks text for Excel with a leading apostrophe; Google stores a stringValue as text anyway."""
    return value[1:] if isinstance(value, str) and value.startswith("'") else value


def normalized(rows: list[list]) -> list[list]:
    """Trailing empty cells and rows dropped, the way the values API reports them, so two reads compare equal."""
    out = []
    for row in rows:
        row = list(row)
        while row and row[-1] in (None, ""):
            row.pop()
        out.append(row)
    while out and not out[-1]:
        out.pop()
    return out


# ---------------------------------------------------------------- credentials

class Credentials:
    """`token_file` and `reauth` (what to do to authorise again, for the messages) default to kol.py's: the installer's
    google-token.json. googlegrid.py keeps its own token, authorised from the settings page."""

    def __init__(self, state_dir: Path, unavailable=GSheetError, token_file: str = TOKEN_FILE, reauth: str = ""):
        self.path = Path(state_dir) / token_file
        self.unavailable = unavailable
        self.reauth = reauth

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            raise self.unavailable(f"还没授权 KOL Mail 访问 Google 表格，{self.reauth or '请重新运行安装命令完成授权'}") from None
        except (OSError, ValueError) as e:
            raise self.unavailable(f"Google 授权文件读不出来：{e}") from None
        if not data.get("refresh_token") or not data.get("client_id"):
            raise self.unavailable(f"Google 授权文件不完整，{self.reauth or '请重新运行安装命令完成授权'}")
        return data

    def save(self, data: dict):
        """Atomic, private, and safe when two kol.py processes refresh at once (each has its own temp file)."""
        import tempfile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".google-token-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def access_token(self, client: httpx.Client) -> str:
        data = self.load()
        if data.get("access_token") and float(data.get("expires_at") or 0) > time.time() + 120:
            return data["access_token"]
        try:
            r = client.post(TOKEN_URL, data={"client_id": data["client_id"], "client_secret": data.get("client_secret", ""),
                                             "refresh_token": data["refresh_token"], "grant_type": "refresh_token"},
                            follow_redirects=False)  # the refresh token and secret go to Google, never on
        except httpx.HTTPError as e:
            raise self.unavailable(f"连不上 Google（检查网络或代理）：{type(e).__name__}") from None
        if r.status_code in (400, 401):
            raise self.unavailable(f"Google 授权已失效或被撤销，{self.reauth or '请重新运行安装命令重新授权'}")
        try:
            body = r.json() if r.status_code == 200 else {}
            token = str(body["access_token"])
        except (ValueError, KeyError, TypeError):
            raise self.unavailable(f"Google 登录服务暂时不可用（HTTP {r.status_code}，可能是网络登录页或代理）") from None
        data["access_token"] = token
        data["expires_at"] = time.time() + int(body.get("expires_in") or 3600)
        with contextlib.suppress(OSError):  # only a cache: the next run refreshes again
            self.save(data)
        return token


class Api:
    def __init__(self, state_dir: Path, unavailable=GSheetError, usage=GSheetError, client: httpx.Client | None = None):
        self.creds = Credentials(state_dir, unavailable)
        self.unavailable, self.usage = unavailable, usage
        self.client = client or client_factory()
        self.token = None

    def close(self):
        self.client.close()

    def call(self, method: str, path: str, **kw) -> dict:
        for attempt in (1, 2):
            if self.token is None:
                self.token = self.creds.access_token(self.client)
            try:
                r = self.client.request(method, API + path, headers={"Authorization": f"Bearer {self.token}"}, **kw)
            except httpx.HTTPError as e:
                raise self.unavailable(f"连不上 Google（检查网络或代理）：{type(e).__name__}") from None
            if r.status_code == 401 and attempt == 1:  # access token expired early: refresh once
                data = self.creds.load()
                data.pop("access_token", None)
                with contextlib.suppress(OSError):
                    self.creds.save(data)
                self.token = None
                continue
            break
        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:  # e.g. a Wi-Fi login page answered instead of Google
                raise self.unavailable("Google 返回的不是表格数据（可能是网络登录页或代理），稍后自动补写") from None
            if not isinstance(body, dict):
                raise self.unavailable("Google 返回的数据格式不对，稍后自动补写")
            return body
        detail = ""
        with contextlib.suppress(ValueError, AttributeError):
            detail = str((r.json().get("error") or {}).get("message") or "")[:160]
        detail = f"：{detail}" if detail else ""
        # Everything becomes Unavailable: kol.py queues the changes and the page shows this text until it is fixed,
        # so nothing is lost and nobody has to dig the batch out of a failed-queue file.
        if r.status_code == 401:
            raise self.unavailable(f"Google 授权失效，请重新运行安装命令重新授权{detail}")
        if r.status_code == 403:
            raise self.unavailable(f"Google 拒绝访问这张表格（HTTP 403{detail}）。没开 Sheets API、配额用完或授权被撤销都会这样")
        if r.status_code == 404:
            raise self.unavailable("找不到这张 Google 表格（被删除了？），请重新运行安装命令新建一张")
        if r.status_code == 429 or r.status_code >= 500:
            raise self.unavailable(f"Google 表格暂时忙（HTTP {r.status_code}），稍后自动补写")
        raise self.unavailable(f"Google 表格拒绝了这次写入（HTTP {r.status_code}{detail}），表格结构被改过的话请改回去")

    def meta(self, sid: str) -> dict:
        return self.call("GET", f"/v4/spreadsheets/{sid}",
                         params={"fields": "spreadsheetId,properties.title,"
                                           "sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))"})

    def read(self, sid: str, tabs: list[str]) -> dict[str, list[list]]:
        params = [("ranges", quote_tab(t)) for t in tabs] + [
            ("valueRenderOption", "UNFORMATTED_VALUE"), ("dateTimeRenderOption", "SERIAL_NUMBER"),
            ("majorDimension", "ROWS")]
        body = self.call("GET", f"/v4/spreadsheets/{sid}/values:batchGet", params=params)
        ranges = body.get("valueRanges") or []
        return {t: (ranges[i].get("values") if i < len(ranges) else None) or [] for i, t in enumerate(tabs)}

    def update(self, sid: str, requests: list[dict]) -> dict:
        return self.call("POST", f"/v4/spreadsheets/{sid}:batchUpdate", json={"requests": requests})


# ---------------------------------------------------------------- the book and its tables (kol.py's interface)

def cell_data(value, number_format: str | None = None) -> tuple[dict, str]:
    """(CellData, fields mask) for one value as kol.py hands it over."""
    value = strip_text_mark(value)
    cell: dict = {}
    fields = ["userEnteredValue"]
    if isinstance(value, bool):
        cell["userEnteredValue"] = {"boolValue": value}
    elif isinstance(value, (int, float)):
        cell["userEnteredValue"] = {"numberValue": value}
    elif value not in (None, ""):
        cell["userEnteredValue"] = {"stringValue": str(value)}
    if number_format:
        kind = "DATE_TIME" if "h" in number_format else "DATE"
        cell["userEnteredFormat"] = {"numberFormat": {"type": kind, "pattern": number_format}}
        fields.append("userEnteredFormat.numberFormat")
    return cell, ",".join(fields)


class GTable:
    def __init__(self, book: "GBook", tab: str):
        self.book, self.tab, self.name = book, tab, tab
        rows = book.grid[tab]
        header = rows[0] if rows else []
        self.headers = [str(h) if h is not None else "" for h in header]
        self.col = {h: i + 1 for i, h in enumerate(self.headers)}
        width = len(self.headers)
        # body rows padded to the header width; rows 1.. of the tab. The 频道 column's ARRAYFORMULA reports "" down
        # to the bottom of the grid, so trailing rows with nothing but formula output are not rows at all.
        self.rows = [list(r[:width]) + [None] * (width - len(r[:width])) for r in rows[1:]]
        while self.rows and self.blank(self.rows[-1]):
            self.rows.pop()

    def need(self, headers: list[str]):
        missing = [h for h in headers if h not in self.col]
        if missing:
            raise self.book.usage(f"Google 表格的“{self.tab}”页缺少列：{', '.join(missing)}（列名被改过？改回去再试）")

    def values(self) -> list[list]:
        return [list(r) for r in self.rows]

    def get(self, row: list, header: str):
        return row[self.col[header] - 1]

    def cell(self, r: int, header: str):
        return self.rows[r - 1][self.col[header] - 1]

    def _put(self, r: int, c: int, value, number_format: str | None = None):
        while len(self.rows) < r:
            self.rows.append([None] * len(self.headers))
        self.rows[r - 1][c - 1] = strip_text_mark(value) if value != "" else None
        if number_format is None and isinstance(value, (int, float)) and not isinstance(value, bool):
            number_format = DATE_COLUMNS.get(self.headers[c - 1])  # dates are serial numbers: give them their format
        cell, fields = cell_data(value, number_format)
        self.book.write_cell(self.tab, r, c - 1, cell, fields)

    def set(self, r: int, header: str, value, number_format: str | None = None):
        self._put(r, self.col[header], value, number_format)

    def write_row(self, r: int, values: dict):
        """Only the given cells; formula columns are never touched (Excel rewrote them from .Formula)."""
        for h, v in values.items():
            self._put(r, self.col[h], v)

    def blank(self, row: list) -> bool:
        return all(v in (None, "") for h, v in zip(self.headers, row) if h not in self.book.formula_columns)

    def append(self) -> int:
        """Index of a fresh row right under the last real one (trailing formula-only rows were dropped on load)."""
        if self.rows and self.blank(self.rows[-1]):
            return len(self.rows)
        self.rows.append([None] * len(self.headers))
        return len(self.rows)

    def records(self) -> list[list]:
        return [r for r in self.values() if not self.blank(r)]


class GBook:
    """One kol.py command's view of the Google spreadsheet: read on enter, written in one batch on save()."""

    def __init__(self, spreadsheet_id: str, state_dir: Path, write: bool = True, usage=GSheetError,
                 unavailable=GSheetError, formula_columns=frozenset(), client: httpx.Client | None = None):
        self.sid, self.state_dir, self.write = spreadsheet_id, Path(state_dir), write
        self.usage, self.unavailable = usage, unavailable
        self.formula_columns = set(formula_columns)
        self.client = client
        self.mode = "gsheet"
        self.api = None
        self.grid: dict[str, list[list]] = {}
        self.original: dict[str, list[list]] = {}
        self.sheet_ids: dict[str, int] = {}
        self.row_counts: dict[str, int] = {}
        self.tables: dict[str, GTable] = {}
        self.requests: list[dict] = []
        self.touched: set[str] = set()

    def __enter__(self):
        self.api = Api(self.state_dir, self.unavailable, self.usage, self.client)
        try:
            meta = self.api.meta(self.sid)
            for s in meta.get("sheets") or []:
                p = s.get("properties") or {}
                self.sheet_ids[p.get("title")] = p.get("sheetId")
                self.row_counts[p.get("title")] = int((p.get("gridProperties") or {}).get("rowCount") or 0)
            tabs = list(self.sheet_ids)
            self.grid = self.api.read(self.sid, tabs) if tabs else {}
            self.original = copy.deepcopy(self.grid)
        except BaseException:
            self.api.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.api is not None:
            self.api.close()

    def table(self, sheet: str, name: str) -> GTable:
        tab = TAB_FOR.get(name, sheet)
        if tab not in self.sheet_ids:
            raise self.usage(f"Google 表格里找不到“{tab}”页（被改名或删掉了？）")
        if tab not in self.tables:
            self.tables[tab] = GTable(self, tab)
        return self.tables[tab]

    def write_cell(self, tab: str, r: int, c: int, cell: dict, fields: str):
        if not self.write:
            raise self.usage("只读打开的总表不能写")
        need = r + 1  # header row + r body rows
        if need > self.row_counts[tab]:
            grow = max(GROW_ROWS, need - self.row_counts[tab])
            sheet = self.sheet_ids[tab]
            old = self.row_counts[tab]
            self.requests.append({"appendDimension": {"sheetId": sheet, "dimension": "ROWS", "length": grow}})
            if old >= 2:  # new grid rows start bare: copy the dropdowns and date formats of the first body row
                self.requests.append({"copyPaste": {
                    "source": {"sheetId": sheet, "startRowIndex": 1, "endRowIndex": 2},
                    "destination": {"sheetId": sheet, "startRowIndex": old, "endRowIndex": old + grow},
                    "pasteType": "PASTE_FORMAT"}})
            self.row_counts[tab] += grow
        self.requests.append({"updateCells": {
            "start": {"sheetId": self.sheet_ids[tab], "rowIndex": r, "columnIndex": c},
            "rows": [{"values": [cell]}], "fields": fields}})
        self.touched.add(tab)

    def save(self):
        if not self.requests:
            return
        # Optimistic check: the tabs about to be written must still look the way they did when we read them, or a
        # row index computed then could now point at a different KOL.
        now = self.api.read(self.sid, sorted(self.touched))
        for tab in self.touched:
            if normalized(now.get(tab) or []) != normalized(self.original.get(tab) or []):
                raise self.unavailable(f"总表的“{tab}”页在写入的同时被人改了（排序、删行或加行），这批改动稍后自动重写")
        self.api.update(self.sid, self.requests)
        self.requests = []
        self.original = copy.deepcopy(self.grid)
        self.touched.clear()

    def backup_once_a_day(self):
        """Google keeps the version history; nothing to do."""


# ---------------------------------------------------------------- creating a new sheet (installer)

def col_letter(n: int) -> str:
    s = ""
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def layout():
    """(tabs, formats, dropdowns) for a new spreadsheet, taken from kol.py's own column lists."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import kol
    tabs = [
        (kol.MAIN_SHEET, kol.MAIN_HEADERS),
        (kol.LOG_SHEET, kol.LOG_HEADERS),
        (kol.BATCH_SHEET, kol.BATCH_HEADERS),
        (TAB_FOR[kol.PROJECT_TABLE], ["项目"]),
    ]
    formats = {kol.MAIN_SHEET: {"首次发信": "yyyy-mm-dd", "更新时间": "yyyy-mm-dd hh:mm"},
               kol.LOG_SHEET: {"时间": "yyyy-mm-dd hh:mm"}, kol.BATCH_SHEET: {"日期": "yyyy-mm-dd"}}
    dropdowns = {kol.MAIN_SHEET: {"阶段": kol.STAGES, "意向": kol.INTENTS, "待办方": kol.OWNERS},
                 kol.LOG_SHEET: {"方向": ["收", "发"]}}
    return kol, tabs, formats, dropdowns


def create(api: Api, title: str) -> str:
    kol, tabs, formats, dropdowns = layout()
    main = kol.MAIN_HEADERS
    link = col_letter(main.index("频道链接") + 1)
    sheets = []
    for i, (tab, headers) in enumerate(tabs):
        cells = []
        for h in headers:
            if tab == kol.MAIN_SHEET and h == "频道":
                # The whole column from one formula in the header cell: rows added through the API need nothing.
                value = {"formulaValue": f'={{"频道";ARRAYFORMULA(IF({link}2:{link}="","",HYPERLINK({link}2:{link},"打开 ↗")))}}'}
            else:
                value = {"stringValue": h}
            cells.append({"userEnteredValue": value, "userEnteredFormat": {"textFormat": {"bold": True}}})
        sheets.append({"properties": {"sheetId": i + 1, "title": tab, "index": i,
                                      "gridProperties": {"rowCount": 1000, "columnCount": max(len(headers), 1),
                                                         "frozenRowCount": 1}},
                       "data": [{"startRow": 0, "startColumn": 0, "rowData": [{"values": cells}]}]})
    if kol.DEFAULT_PROJECT:
        sheets[-1]["data"][0]["rowData"].append({"values": [{"userEnteredValue": {"stringValue": kol.DEFAULT_PROJECT}}]})
    body = api.call("POST", "/v4/spreadsheets", json={"properties": {"title": title, "locale": "en_US"},
                                                      "sheets": sheets})
    sid = body["spreadsheetId"]
    requests = []
    for i, (tab, headers) in enumerate(tabs):
        for h, pattern in formats.get(tab, {}).items():
            c = headers.index(h)
            kind = "DATE_TIME" if "h" in pattern else "DATE"
            requests.append({"repeatCell": {
                "range": {"sheetId": i + 1, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat"}})
        for h, choices in dropdowns.get(tab, {}).items():
            c = headers.index(h)
            requests.append({"setDataValidation": {
                "range": {"sheetId": i + 1, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in choices]},
                         "showCustomUi": True, "strict": False}}})
    if requests:
        try:
            api.update(sid, requests)
        except Exception as e:  # noqa: BLE001  the sheet exists and works; only formats and dropdowns are missing
            print(f"表格已建好，但日期格式和下拉选项没设置成功：{e}", file=sys.stderr)
    return sid


def check(api: Api, sid: str) -> list[str]:
    """Problems with an existing sheet: missing tabs or columns. Columns added later (聊天账号) are optional: a sheet
    made before them still works."""
    kol, tabs, _, _ = layout()
    optional = set(kol.OPTIONAL_HEADERS)
    meta = api.meta(sid)
    titles = {(s.get("properties") or {}).get("title") for s in meta.get("sheets") or []}
    problems = [f"缺少“{t}”页" for t, _ in tabs if t not in titles]
    present = [t for t, _ in tabs if t in titles]
    grid = api.read(sid, present) if present else {}
    for tab, headers in tabs:
        if tab in titles:
            have = [str(h) for h in (grid.get(tab) or [[]])[0]] if grid.get(tab) else []
            missing = [h for h in headers if h not in have and h not in optional]
            if missing:
                problems.append(f"“{tab}”页缺少列：{', '.join(missing)}")
    return problems


# ---------------------------------------------------------------- one-time authorisation (installer)

def authorize(state_dir: Path, client_id: str, client_secret: str, open_browser: bool = True,
              timeout: float = 600, token_file: str = TOKEN_FILE, scope: str = SCOPE,
              done_page: str = "授权完成，可以关掉这个页面，回到终端。",
              failed_page: str = "没有完成授权，回到终端重新运行安装命令即可。", on_url=None) -> None:
    """Desktop OAuth with a loopback redirect and PKCE; stores the refresh token. The defaults are the installer's
    (kol.py's token and scope); googlegrid.py passes its own token file, scope and page texts, and `on_url` gets the
    consent address as soon as it is known, so the settings page can open it itself."""
    import http.server
    import webbrowser

    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            result["code"] = q.get("code", [""])[0]
            result["error"] = q.get("error", [""])[0]
            ok = bool(result["code"])
            page = f"<p>{done_page if ok else failed_page}</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<!doctype html><meta charset=utf-8><title>KOL Mail</title>{page}".encode("utf-8"))

        def log_message(self, *args):
            pass

    Handler.timeout = 30  # an idle pre-connection from the browser gives up instead of blocking the real redirect
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": scope,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("请在打开的浏览器页面里登录你工作用的 Google 账号，点“继续”或“允许”。没有自动打开的话，复制下面的地址到浏览器：")
    print(url, flush=True)
    if on_url is not None:
        try:
            on_url(url)
        except BaseException:
            server.server_close()
            raise
    if open_browser:
        webbrowser.open(url)
    server.timeout = 1  # handle_request returns every second, so the loop notices the code as soon as it arrives
    deadline = time.monotonic() + timeout
    while "code" not in result and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if not result.get("code"):
        raise SystemExit("没有拿到 Google 授权" + (f"（{result['error']}）" if result.get("error") else "（超时）"))
    with client_factory() as client:
        r = client.post(TOKEN_URL, data={"code": result["code"], "client_id": client_id, "client_secret": client_secret,
                                         "redirect_uri": redirect, "grant_type": "authorization_code",
                                         "code_verifier": verifier})
    if r.status_code != 200 or not r.json().get("refresh_token"):
        raise SystemExit(f"换取 Google 授权失败（HTTP {r.status_code}）：{r.text[:200]}")
    body = r.json()
    Credentials(state_dir, token_file=token_file).save({
        "client_id": client_id, "client_secret": client_secret, "refresh_token": body["refresh_token"],
        "scope": body.get("scope", scope), "access_token": body.get("access_token"),
        "expires_at": time.time() + int(body.get("expires_in") or 3600)})


def main(argv=None):
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="gsheet.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("auth")
    p.add_argument("--state", required=True)
    p.add_argument("--client-id", required=True)
    p.add_argument("--client-secret", default="")
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("create")
    p.add_argument("--state", required=True)
    p.add_argument("--title", default="KOL总表（KOL Mail）")
    p = sub.add_parser("check")
    p.add_argument("--state", required=True)
    p.add_argument("--id", required=True)
    a = ap.parse_args(argv)
    state = Path(a.state)
    try:
        if a.cmd == "auth":
            authorize(state, a.client_id, a.client_secret, open_browser=not a.no_browser)
            print("Google 授权完成")
        elif a.cmd == "create":
            api = Api(state)
            try:
                sid = create(api, a.title)
            finally:
                api.close()
            print(f"已新建 Google 表格：https://docs.google.com/spreadsheets/d/{sid}/edit")
            print(sid)
        elif a.cmd == "check":
            api = Api(state)
            try:
                problems = check(api, a.id)
            finally:
                api.close()
            if problems:
                raise SystemExit("这张表不能用：" + "；".join(problems))
            print("ok")
    except (GSheetError, RuntimeError) as e:
        raise SystemExit(str(e)) from None


if __name__ == "__main__":
    main()
