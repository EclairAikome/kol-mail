"""A colleague's own Google Sheet as the KOL sheet (sheet/googlegrid.py), against an in-memory fake of the Sheets API
and Google's token endpoint: links, reading values and dates whatever their display format, text that stays text,
dates that become dates, cells with a formula left alone, new rows that grow the sheet and copy the formats, one batch
per sync, and errors split into "queue and retry" (network, Google busy, lost authorisation, no permission, a sheet gone)
and "tell the user" (a wrong link, a tab gone)."""

import datetime as dt
import inspect
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sheet"))

import googlegrid as gg  # noqa: E402
import gsheet  # noqa: E402
import usersheet as us  # noqa: E402

SID = "1aBcDeFgHiJkLmNoPqRsTuVwXyZ_0123456789-AbCd"
T0 = dt.datetime(2026, 10, 20, 9, 0)
EPOCH = dt.datetime(1899, 12, 30)
AUTO = "yyyy-mm-dd h:mm:ss"  # what Google shows for a typed ISO date-time in a cell without a format: 9:00, not 09:00


class Date:
    """A date cell: Google keeps a serial number and shows it through the cell's number format."""

    def __init__(self, when: dt.datetime):
        self.when = when
        self.serial = (when - EPOCH).total_seconds() / 86400

    def shown(self, pattern):
        if pattern == "dd/mm/yyyy":
            return f"{self.when:%d/%m/%Y}"
        return f"{self.when:%Y-%m-%d} {self.when.hour}:{self.when:%M:%S}"


def cells_of(rows):
    return {(r, c): v for r, row in enumerate(rows) for c, v in enumerate(row) if v is not None}


def colleague_book():
    """A colleague's sheet: English headers, their own words for whose turn it is, a dd/mm/yyyy date column, a
    yellow last row, only three grid rows (a new KOL needs a fourth), a notes tab and a hidden tab."""
    kols = {"id": 0, "title": "KOLs", "index": 0, "hidden": False, "rowCount": 3,
            "cells": cells_of([
                ["KOL", "Email", "Channel", "Last contact", "Ball", "Progress", "Fee"],
                ["Ana", "ana@gmail.com", "https://www.youtube.com/@ana", Date(dt.datetime(2026, 10, 1, 8, 0)), "Them",
                 "我方回信：sent brief", 300],
                ["Bo", "bo@x.com", None, None, "Me", None, None]]),
            "formats": {(1, 3): "dd/mm/yyyy", (2, 3): "dd/mm/yyyy",
                        **{(2, c): "fill:FFF2CC" for c in (0, 1, 2, 4, 5, 6)}}}
    notes = {"id": 1234, "title": "说明", "index": 1, "hidden": False, "rowCount": 10,
             "cells": {(0, 0): "Read me"}, "formats": {}}
    old = {"id": 99, "title": "old", "index": 2, "hidden": True, "rowCount": 5,
           "cells": {(0, 0): "Email", (1, 0): "x@y.com"}, "formats": {}}
    return {t["id"]: t for t in (kols, notes, old)}


def column(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


class FakeGoogle:
    """Enough of the Sheets API v4 and the OAuth token endpoint for googlegrid.py."""

    def __init__(self):
        self.books = {SID: colleague_book()}
        self.calls: list[tuple[str, str]] = []
        self.updates: list[list[dict]] = []  # spreadsheets.batchUpdate requests
        self.value_writes: list[dict] = []  # values.batchUpdate bodies
        self.token_calls = 0
        self.token_status = 200
        self.granted = gg.SCOPE
        self.fail: tuple[int, str] | None = None  # every API call answers with this Google error
        self.fail_raw: tuple[int, str] | None = None  # every API call answers with this page (a proxy, a firewall)
        self.fail_next: dict[str, int] = {}  # path suffix -> status, once
        self.down = False

    def tab(self, sid, sheet_id):
        return self.books[sid][sheet_id]

    def insert_row(self, sid, sheet_id, at, values):
        """A colleague inserts a row in the browser: everything from `at` (0-based) moves down."""
        tab = self.tab(sid, sheet_id)
        for key in ("cells", "formats"):
            tab[key] = {((r + 1 if r >= at else r), c): v for (r, c), v in tab[key].items()}
        tab["cells"].update({(at, c): v for c, v in enumerate(values) if v is not None})
        tab["rowCount"] += 1

    def by_title(self, sid, title):
        return next((t for t in self.books[sid].values() if t["title"] == title), None)

    def values(self, tab, dates, render="UNFORMATTED_VALUE"):
        """values.batchGet with UNFORMATTED_VALUE (FORMULA: a cell's formula instead of what it shows): "" for gaps,
        trailing empty cells and rows left out."""
        cells = {**tab["cells"], **(tab.get("formulas") or {})} if render == "FORMULA" else tab["cells"]
        out = []
        for r in range(max((r for r, _ in cells), default=-1) + 1):
            width = max((c for rr, c in cells if rr == r), default=-1) + 1
            row = []
            for c in range(width):
                v = cells.get((r, c))
                if isinstance(v, Date):
                    v = v.serial if dates == "SERIAL_NUMBER" else v.shown(tab["formats"].get((r, c)))
                row.append("" if v is None else v)
            out.append(row)
        return out

    def typed(self, tab, r, c, text):
        """USER_ENTERED: a plain-text cell keeps text, a date-time becomes a date (and gets a format if it had none)."""
        assert r < tab["rowCount"], "write outside the grid"
        if tab["formats"].get((r, c)) == "@":
            tab["cells"][(r, c)] = text
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", text):
            tab["cells"][(r, c)] = Date(dt.datetime.strptime(text, "%Y-%m-%d %H:%M"))
            tab["formats"].setdefault((r, c), AUTO)
        else:
            assert not text.startswith(("=", "+", "-", "@")), "a formula typed into the sheet"
            tab["cells"][(r, c)] = text

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("offline", request=request)
        url, path = request.url, request.url.path
        if str(url).startswith(gsheet.TOKEN_URL):
            self.token_calls += 1
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            body = {"access_token": "tok", "expires_in": 3600}
            if form.get("grant_type") == "authorization_code":
                body.update(refresh_token="r1", scope=self.granted)
            return httpx.Response(200, json=body)
        self.calls.append((request.method, path))
        if request.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401, json={"error": {"code": 401, "message": "Request had invalid credentials."}})
        if self.fail:
            code, message = self.fail
            return httpx.Response(code, json={"error": {"code": code, "message": message}})
        if self.fail_raw:
            return httpx.Response(self.fail_raw[0], text=self.fail_raw[1])
        for suffix, code in list(self.fail_next.items()):
            if path.endswith(suffix):
                del self.fail_next[suffix]
                return httpx.Response(code, json={"error": {"code": code, "message": "Try again later."}})
        sid = path.split("/")[3].split(":")[0]
        if sid not in self.books:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Requested entity was not found."}})
        book = self.books[sid]
        if request.method == "GET" and path.endswith("/values:batchGet"):
            render = url.params["valueRenderOption"]
            assert render in ("UNFORMATTED_VALUE", "FORMULA")
            out = []
            for rng in url.params.get_list("ranges"):
                tab = self.by_title(sid, rng[1:-1].replace("''", "'"))
                if tab is None:
                    return httpx.Response(400, json={"error": {"message": f"Unable to parse range: {rng}"}})
                values = self.values(tab, url.params["dateTimeRenderOption"], render)
                out.append({"range": rng, "majorDimension": "ROWS", **({"values": values} if values else {})})
            return httpx.Response(200, json={"spreadsheetId": sid, "valueRanges": out})
        if request.method == "POST" and path.endswith("/values:batchUpdate"):
            body = json.loads(request.content)
            self.value_writes.append(body)
            assert body["valueInputOption"] == "USER_ENTERED"
            for d in body["data"]:
                title, letters, row = re.fullmatch(r"'((?:[^']|'')*)'!([A-Z]+)(\d+)", d["range"]).groups()
                self.typed(self.by_title(sid, title.replace("''", "'")), int(row) - 1, column(letters) - 1,
                           d["values"][0][0])
            return httpx.Response(200, json={"spreadsheetId": sid})
        if request.method == "POST" and path.endswith(":batchUpdate"):
            requests = json.loads(request.content)["requests"]
            self.updates.append(requests)
            for req in requests:
                self.apply(book, req)
            return httpx.Response(200, json={"spreadsheetId": sid, "replies": [{} for _ in requests]})
        if request.method == "GET":
            return httpx.Response(200, json={"spreadsheetId": sid, "sheets": [
                {"properties": {"sheetId": t["id"], "title": t["title"], "index": t["index"], "hidden": t["hidden"],
                                "sheetType": "GRID", "gridProperties": {"rowCount": t["rowCount"]}}}
                for t in book.values()]})
        raise AssertionError(f"unsupported {request.method} {path}")

    def apply(self, book, req):
        if "appendDimension" in req:
            a = req["appendDimension"]
            assert a["dimension"] == "ROWS"
            book[a["sheetId"]]["rowCount"] += a["length"]
        elif "copyPaste" in req:
            p = req["copyPaste"]
            assert p["pasteType"] == "PASTE_FORMAT"
            tab = book[p["source"]["sheetId"]]
            src, dst = p["source"]["startRowIndex"], p["destination"]["startRowIndex"]
            assert dst < tab["rowCount"], "paste outside the grid"
            for (r, c), f in list(tab["formats"].items()):
                if r == src:
                    tab["formats"][(dst, c)] = f
        elif "updateCells" in req:
            u = req["updateCells"]
            assert u["fields"] == "userEnteredValue"
            tab = book[u["start"]["sheetId"]]
            r, c = u["start"]["rowIndex"], u["start"]["columnIndex"]
            assert r < tab["rowCount"], "write outside the grid"
            v = u["rows"][0]["values"][0].get("userEnteredValue") or {}
            assert set(v) <= {"stringValue"}, v  # never a formula, a number or a bool
            if v:
                tab["cells"][(r, c)] = v["stringValue"]
            else:
                tab["cells"].pop((r, c), None)
        else:
            raise AssertionError(f"unsupported request {req}")


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    for name, value in (("STATE_DIR", state), ("PENDING", state / "pending.jsonl"),
                        ("FAILED", state / "pending.failed.jsonl"), ("STATE_FILE", state / "usersheet_state.json"),
                        ("BACKUPS", state / "backups")):
        monkeypatch.setattr(us, name, value)
    monkeypatch.setattr(us, "UNAVAILABLE_WAIT_SECONDS", 0)
    monkeypatch.delenv("KOL_GOOGLE_TOKEN_DIR", raising=False)
    monkeypatch.delenv("KOL_SHEET_MAP", raising=False)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeGoogle()
    monkeypatch.setattr(gg.gsheet, "client_factory", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    f.tokens = tmp_path / "tokens"
    gg.credentials(f.tokens).save({"client_id": "cid", "client_secret": "cs", "refresh_token": "r1",
                                   "scope": gg.SCOPE})
    return f


def grid(fake, ids=None, backups=None):
    return gg.GoogleGrid(f"google:{SID}", us.Unavailable, us.UsageError, fake.tokens,
                         sheet_ids={"KOLs": "0"} if ids is None else ids, backups=backups)


MAP = {"sheet": "KOLs", "sheet_id": "0", "header_row": 1,
       "columns": {"name": "KOL", "email": "Email", "channel": "Channel", "last_contact": "Last contact",
                   "owner": "Ball", "summary": "Progress"}}


# ---------------------------------------------------------------- links

@pytest.mark.parametrize("link", [
    f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=0",
    f"https://docs.google.com/spreadsheets/d/{SID}/edit?usp=sharing",
    f"https://docs.google.com/spreadsheets/d/{SID}",
    f"https://docs.google.com/spreadsheets/u/1/d/{SID}/edit",
    f"docs.google.com/spreadsheets/d/{SID}/",
    f"  {SID} ",
    f"google:{SID}",
])
def test_links_give_the_spreadsheet_id(link):
    assert gg.spreadsheet_id_from_link(link) == SID


@pytest.mark.parametrize("link, says", [
    ("https://example.com/spreadsheets/d/" + SID, "不是 Google 表格的链接"),
    (f"https://docs.google.com/document/d/{SID}/edit", "不是 Google 表格的链接"),
    ("https://docs.google.com/spreadsheets/d/short/edit", "不是 Google 表格的链接"),
    ("https://docs.google.com/spreadsheets/d/e/2PACX-1vQabcdefghijklmnopqrstuvwxyz/pubhtml", "发布到网络"),
    (f"https://docs.google.com/spreadsheets/d/{SID}/edit?usp=drive_link&rtpof=true", "另存为 Google 表格"),
    ("not a link", "不是 Google 表格的链接"),
    ("", "不是 Google 表格的链接"),
    (f"gsheet:{SID}", "不是 Google 表格的链接"),
])
def test_other_links_are_refused_with_what_to_paste(link, says):
    with pytest.raises(ValueError, match=says):
        gg.spreadsheet_id_from_link(link)


def test_setting_format():
    assert gg.is_google(f"google:{SID}") and not gg.is_google(f"gsheet:{SID}") and not gg.is_google(None)
    assert gg.parse_setting(f"google:{SID}") == SID and gg.parse_setting("popo:abc") == ""
    with pytest.raises(us.UsageError, match="链接不对"):
        with gg.GoogleGrid("google:nope", us.Unavailable, us.UsageError, "."):
            pass


# ---------------------------------------------------------------- reading

def test_sheet_names_and_rows(fake):
    g = grid(fake, ids={})
    assert g.sheet_names() == ["KOLs", "说明"]  # the hidden tab is left out, like POPO's
    assert g.ids == {"KOLs": "0", "说明": "1234"}  # gids as text: gid 0 must survive `if sheet_id`
    rows = g.rows("KOLs")
    assert rows[0] == ["KOL", "Email", "Channel", "Last contact", "Ball", "Progress", "Fee"]
    assert rows[1] == ["Ana", "ana@gmail.com", "https://www.youtube.com/@ana", dt.datetime(2026, 10, 1, 8, 0), "Them",
                       "我方回信：sent brief", 300]  # the dd/mm/yyyy date comes back as a date, the fee as a number
    assert rows[2] == ["Bo", "bo@x.com", None, None, "Me", None, None]  # gaps and the trailing cells are None
    assert len(rows) == 3


# ---------------------------------------------------------------- writing

def test_text_that_looks_like_a_formula_stays_text(fake):
    g = grid(fake)
    g.set_text("KOLs", 3, 6, us.safe_text('=HYPERLINK("http://evil","x")'))
    g.set_text("KOLs", 3, 7, us.safe_text("00123"))
    g.set_text("KOLs", 3, 3, "+1 555 0100")
    g.save()
    cells = fake.tab(SID, 0)["cells"]
    assert cells[(2, 5)] == '=HYPERLINK("http://evil","x")'  # usersheet's ' mark dropped, the text kept as is
    assert cells[(2, 6)] == "00123" and cells[(2, 2)] == "+1 555 0100"
    assert fake.value_writes == []  # text is never "typed in", so Google never parses it


def test_a_cell_with_a_formula_is_never_overwritten(fake):
    tab = fake.tab(SID, 0)
    tab["formulas"] = {(2, 5): '=IF(B3="","","")'}  # Bo's progress: a formula that shows nothing
    op = us.norm_op({"op": "event", "dir": "in", "email": "bo@x.com", "subject": "rates",
                     "time": "2026-10-20T09:00:00"})
    assert us.run([op], "test", MAP, grid_factory=lambda: grid(fake))["status"] == "ok"
    assert (2, 5) not in tab["cells"]  # the formula's cell is left as it was
    assert isinstance(tab["cells"][(2, 3)], Date)  # the cell beside it, with no formula, is written


def test_dates_are_written_as_dates(fake):
    fake.tab(SID, 0)["formats"][(2, 6)] = "@"  # Bo's fee cell is formatted as plain text
    g = grid(fake)
    g.set_date("KOLs", 3, 4, T0, 1)  # Bo's empty dd/mm/yyyy cell
    g.set_date("KOLs", 3, 7, T0, 1)
    g.set_date("说明", 2, 2, T0, 1)  # a cell with no format at all
    g.save()
    assert fake.value_writes[-1] == {"valueInputOption": "USER_ENTERED", "data": [
        {"range": "'KOLs'!D3", "values": [["2026-10-20 09:00"]]},
        {"range": "'KOLs'!G3", "values": [["2026-10-20 09:00"]]},
        {"range": "'说明'!B2", "values": [["2026-10-20 09:00"]]}]}
    kols = fake.tab(SID, 0)
    assert kols["cells"][(2, 3)].when == T0 and kols["formats"][(2, 3)] == "dd/mm/yyyy"  # a real date, their format
    assert kols["cells"][(2, 6)] == "2026-10-20 09:00"  # a plain-text cell gets text, as when typed
    # read back as datetimes, though the sheet shows "20/10/2026" and "2026-10-20 9:00:00"
    fresh = grid(fake)
    assert fresh.rows("KOLs")[2][3] == T0 and fresh.rows("说明")[1][1] == T0


def test_a_new_row_past_the_end_grows_the_sheet_and_copies_the_format(fake):
    g = grid(fake)
    g.rows("KOLs")
    assert g.append_row("KOLs", 1, 3, 4) == 4
    g.set_text("KOLs", 4, 2, "new@example.com")
    g.set_date("KOLs", 4, 4, T0, 1)
    g.save()
    tab = fake.tab(SID, 0)
    assert tab["rowCount"] == 4
    assert [next(iter(r)) for r in fake.updates[-1]] == ["appendDimension", "copyPaste", "updateCells"]
    assert {c: f for (r, c), f in tab["formats"].items() if r == 3} == \
           {c: f for (r, c), f in tab["formats"].items() if r == 2}  # yellow, and the date column's format
    assert tab["cells"][(3, 1)] == "new@example.com" and tab["cells"][(3, 3)].when == T0
    assert (3, 0) not in tab["cells"]  # formats only: Bo's values are not copied


def test_a_row_inside_the_grid_needs_no_new_rows_and_a_header_is_never_copied(fake):
    tab = fake.tab(SID, 1234)
    g = grid(fake)
    assert g.append_row("说明", 1, 1, 2) == 2
    g.set_text("说明", 2, 1, "a@x.com")
    g.save()
    assert [next(iter(r)) for r in fake.updates[-1]] == ["updateCells"] and tab["rowCount"] == 10


def test_save_sends_everything_at_once_and_nothing_before(fake):
    g = grid(fake)
    g.rows("KOLs")
    before = len(fake.calls)
    g.set_text("KOLs", 2, 5, "Me")
    g.set_text("KOLs", 2, 6, "对方来信：first")
    g.set_text("KOLs", 2, 6, "对方来信：second")  # the last write of a cell wins
    g.set_date("KOLs", 2, 4, T0, 1)
    g.set_date("KOLs", 3, 4, T0, 1)
    assert len(fake.calls) == before  # a sync that fails half way writes nothing
    g.save()
    assert [m for m, _ in fake.calls[before:]] == ["GET", "GET", "POST", "POST"]  # the check, the formulas, the
    # text, the dates
    assert fake.calls[-2][1].endswith(f"{SID}:batchUpdate") and fake.calls[-1][1].endswith("/values:batchUpdate")
    assert len(fake.updates[-1]) == 2 and len(fake.value_writes[-1]["data"]) == 2
    assert fake.tab(SID, 0)["cells"][(1, 5)] == "对方来信：second"
    g.save()
    assert len(fake.calls) == before + 4  # nothing left to send


def test_a_row_inserted_in_the_browser_meanwhile_queues_the_sync(fake, monkeypatch):
    real_rows, edited = gg.GoogleGrid.rows, []

    def rows_then_someone_edits(self, sheet):
        out = real_rows(self, sheet)
        if not edited:  # once, right after the sync read the sheet
            edited.append(sheet)
            fake.insert_row(SID, 0, 1, ["Cy", "cy@x.com", None, None, "Them", "their note", None])
        return out

    monkeypatch.setattr(gg.GoogleGrid, "rows", rows_then_someone_edits)
    op = us.norm_op({"op": "event", "dir": "in", "email": "ana@gmail.com", "subject": "x",
                     "time": "2026-10-20T09:00:00"})
    res = us.run([op], "test", MAP, grid_factory=lambda: grid(fake))
    assert res["status"] == "queued" and "同时被人改了" in res["reason"]
    tab = fake.tab(SID, 0)
    assert fake.updates == [] and fake.value_writes == [] and tab["cells"][(1, 5)] == "their note"  # Cy untouched
    monkeypatch.setattr(gg.GoogleGrid, "rows", real_rows)
    res = us.run([], "补写队列", MAP, grid_factory=lambda: grid(fake))
    assert res["status"] == "ok" and res["flushed_from_queue"] == 1
    assert tab["cells"][(2, 5)] == "对方来信：x" and tab["cells"][(1, 5)] == "their note"  # Ana, now one row lower


def test_two_new_kols_in_one_sync_and_a_failure_between_the_two_writes(fake):
    ops = [us.norm_op({"op": "event", "dir": "in", "email": f"k{n}@example.com", "subject": "hi",
                       "time": "2026-10-20T09:00:00"}) for n in (1, 2)]
    fake.fail_next["/values:batchUpdate"] = 503
    res = us.run(ops, "test", MAP, grid_factory=lambda: grid(fake))
    assert res["status"] == "queued" and "暂时忙" in res["reason"]
    tab = fake.tab(SID, 0)
    assert tab["rowCount"] == 5 and tab["cells"][(3, 1)] == "k1@example.com" and tab["cells"][(4, 1)] == "k2@example.com"
    assert (3, 3) not in tab["cells"] and tab["formats"][(4, 0)] == "fill:FFF2CC"  # rows and text in, dates not yet
    res = us.run([], "补写队列", MAP, grid_factory=lambda: grid(fake))
    assert res["status"] == "ok" and res["flushed_from_queue"] == 2
    assert tab["rowCount"] == 5 and tab["cells"][(3, 3)].when == T0 and tab["cells"][(4, 3)].when == T0
    assert [v for (r, c), v in sorted(tab["cells"].items()) if c == 1 and r >= 3] == ["k1@example.com",
                                                                                      "k2@example.com"]


def test_a_renamed_tab_is_found_by_its_id(fake):
    fake.tab(SID, 0)["title"] = "KOL list"
    g = grid(fake)  # the mapping still says "KOLs", with gid 0
    assert g.rows("KOLs")[1][1] == "ana@gmail.com"
    g.set_date("KOLs", 3, 4, T0, 1)
    g.save()
    assert fake.value_writes[-1]["data"][0]["range"] == "'KOL list'!D3"
    with pytest.raises(us.UsageError, match="没有叫“KOLs”的工作表"):
        grid(fake, ids={}).rows("KOLs")


def test_backup_once_a_day(fake, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    for day in range(1, 16):
        (backups / f"google_{SID}_202001{day:02d}.json").write_text("{}", encoding="utf-8")
    g = grid(fake, backups=backups)
    g.backup_once_a_day()
    today = backups / f"google_{SID}_{dt.date.today():%Y%m%d}.json"
    saved = json.loads(today.read_text(encoding="utf-8"))
    kols = saved["sheets"][0]
    assert kols["title"] == "KOLs" and kols["values"][1][3] == "01/10/2026"  # dates as the sheet shows them
    assert [s["title"] for s in saved["sheets"]] == ["KOLs"]  # the mapped tab only, not the rest of the file
    assert len(list(backups.glob(f"google_{SID}_*.json"))) == 14
    calls = len(fake.calls)
    grid(fake, backups=backups).backup_once_a_day()
    assert len(fake.calls) == calls  # once a day
    unmapped = tmp_path / "unmapped"
    grid(fake, ids={}, backups=unmapped).backup_once_a_day()  # nothing mapped yet: every tab, hidden ones too
    saved = json.loads((unmapped / today.name).read_text(encoding="utf-8"))
    assert [s["title"] for s in saved["sheets"]] == ["KOLs", "说明", "old"]


# ---------------------------------------------------------------- errors

def test_an_expired_access_token_is_refreshed_once(fake):
    token = gg.credentials(fake.tokens)
    token.save({**token.load(), "access_token": "old", "expires_at": time.time() + 3600})
    assert grid(fake).rows("KOLs")[1][0] == "Ana"
    assert fake.token_calls == 1 and token.load()["access_token"] == "tok"
    assert fake.calls[0][0] == "GET" and len([c for c in fake.calls if c[1] == f"/v4/spreadsheets/{SID}"]) == 2


def test_revoked_authorisation_is_unavailable_and_points_to_the_page(fake):
    fake.token_status = 400
    with pytest.raises(us.Unavailable, match="已失效或被撤销，请在 设置 → KOL 总表 里重新授权 Google") as e:
        grid(fake).rows("KOLs")
    assert "安装命令" not in str(e.value)
    op = us.norm_op({"op": "event", "dir": "in", "email": "ana@gmail.com", "subject": "x",
                     "time": "2026-10-20T09:00:00"})
    res = us.run([op], "test", MAP, grid_factory=lambda: grid(fake))
    assert res["status"] == "queued" and "重新授权 Google" in res["reason"] and us.PENDING.exists()
    fake.token_status = 200  # authorised again on the page: the queue is written
    res = us.run([], "补写队列", MAP, grid_factory=lambda: grid(fake))
    assert res["status"] == "ok" and res["flushed_from_queue"] == 1
    assert fake.tab(SID, 0)["cells"][(1, 5)] == "对方来信：x"


def test_missing_authorisation(fake):
    (fake.tokens / gg.TOKEN_FILE).unlink()
    with pytest.raises(us.Unavailable, match="还没授权 KOL Mail 访问 Google 表格，请在 设置 → KOL 总表 里重新授权 Google"):
        grid(fake).rows("KOLs")
    status = gg.auth_status(fake.tokens)
    assert status["authorized"] is False and "设置 → KOL 总表" in status["error"]


def test_no_permission_or_a_sheet_gone_is_queued_and_a_missing_tab_is_a_usage_error(fake):
    fake.fail = (403, "The caller does not have permission")
    with pytest.raises(us.Unavailable, match="没有权限读写这张表格"):
        grid(fake).rows("KOLs")
    res = us.run([us.norm_op({"op": "event", "dir": "in", "email": "a@x.com", "subject": "s"})], "t", MAP,
                 grid_factory=lambda: grid(fake))
    assert res["status"] == "queued" and "没有权限" in res["reason"]  # kept until the sharing is fixed
    fake.fail = None
    other = gg.GoogleGrid("google:" + "z" * 44, us.Unavailable, us.UsageError, fake.tokens)
    with pytest.raises(us.Unavailable, match="找不到这张 Google 表格"):
        other.sheet_names()
    del fake.books[SID][0]
    with pytest.raises(us.UsageError, match="没有叫“KOLs”的工作表"):
        grid(fake).rows("KOLs")


@pytest.mark.parametrize("trouble, says", [
    ("down", "连不上 Google"),
    ((429, "Quota exceeded for quota metric 'Write requests'"), "暂时忙"),
    ((503, "The service is currently unavailable."), "暂时忙"),
    ((403, "Request had insufficient authentication scopes."), "没有读写表格的权限"),
])
def test_network_busy_and_scope_trouble_queue(fake, trouble, says):
    if trouble == "down":
        fake.down = True
    else:
        fake.fail = trouble
    with pytest.raises(us.Unavailable, match=says):
        grid(fake).rows("KOLs")


@pytest.mark.parametrize("code", [403, 404, 407])
def test_a_proxy_or_firewall_page_is_not_a_permission_problem(fake, code):
    fake.fail_raw = (code, "<html>Blocked by your company's web filter</html>")
    with pytest.raises(us.Unavailable, match="没有正常回应"):
        grid(fake).rows("KOLs")


def test_unusable_proxy_settings_queue(fake, monkeypatch):
    def no_client():
        raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")
    monkeypatch.setattr(gg.gsheet, "client_factory", no_client)
    with pytest.raises(us.Unavailable, match="代理设置"):
        grid(fake).rows("KOLs")


def test_numbers_that_are_not_dates_stay_numbers():
    serial = (T0 - EPOCH).total_seconds() / 86400
    assert gg.cell_value(serial, "20/10/2026") == T0
    assert gg.cell_value(3_000_000, "########") == 3_000_000 and gg.cell_value(0.375, "9:00:00") == 0.375
    assert gg.cell_value(serial, None) == serial and gg.cell_value(True, "TRUE") is True and gg.cell_value("", "") is None


def test_a_wifi_login_page_is_unavailable(fake):
    def portal(request):  # the token endpoint still answers; the sheet does not
        if str(request.url).startswith(gsheet.API):
            return httpx.Response(200, text="<html>Please log in to the hotel Wi-Fi</html>")
        return fake.handler(request)
    with httpx.Client(transport=httpx.MockTransport(portal)) as client:
        g = gg.GoogleGrid(f"google:{SID}", us.Unavailable, us.UsageError, fake.tokens, client=client)
        with pytest.raises(us.Unavailable, match="网络登录页"):
            g.rows("KOLs")


# ---------------------------------------------------------------- usersheet, end to end

def test_usersheet_inspects_then_syncs_a_google_sheet(fake, monkeypatch, capsys):
    setting = f"google:{SID}"
    monkeypatch.setattr(us, "RAW_WORKBOOK", setting)
    monkeypatch.setenv("KOL_GOOGLE_TOKEN_DIR", str(fake.tokens))  # not the sync's own state folder
    info = us.inspect(setting, None, None)
    assert info["sheets"] == ["KOLs", "说明"] and info["sheet"] == "KOLs" and info["sheet_id"] == "0"
    assert info["header_row"] == 1 and info["guess"] == MAP["columns"]
    dates = next(c for c in info["columns"] if c["header"] == "Last contact")
    assert dates["dates"] == 1 and dates["samples"] == ["2026-10-01 08:00:00"]
    mapping = {"sheet": info["sheet"], "sheet_id": info["sheet_id"], "header_row": info["header_row"],
               "columns": info["guess"]}
    monkeypatch.setenv("KOL_SHEET_MAP", json.dumps(mapping))

    new = us.norm_op({"op": "event", "dir": "in", "email": "new@example.com", "name": "New KOL", "subject": "hi",
                      "time": "2026-10-20T09:00:00", "channel_url": "https://www.twitch.tv/newkol"})
    res = us.run([new], "test", us.load_map())
    assert res["status"] == "ok" and res["mode"] == "google" and res["results"][0]["created"], res
    tab = fake.tab(SID, 0)
    row = [tab["cells"].get((3, c)) for c in range(7)]
    assert row[:3] == ["New KOL", "new@example.com", "https://www.twitch.tv/newkol"]
    assert row[3].when == T0 and row[4:] == ["Me", "对方来信：hi", None]  # the sheet's own word for our turn
    assert tab["rowCount"] == 4 and tab["formats"][(3, 0)] == "fill:FFF2CC" and tab["formats"][(3, 3)] == "dd/mm/yyyy"
    assert len(fake.updates) == 1 and len(fake.value_writes) == 1  # one sync, two writes
    assert list(us.BACKUPS.glob(f"google_{SID}_*.json"))

    # An older mail must not overwrite newer news, even with the sync's memory gone: the date in the sheet, shown
    # as 20/10/2026, has to read back as a date.
    us.STATE_FILE.unlink()
    old = us.norm_op({"op": "event", "dir": "out", "email": "new@example.com", "summary": "我方回信：old",
                      "time": "2026-10-19T09:00:00"})
    res = us.run([old], "test", us.load_map())
    assert "旧" in res["results"][0]["note"] and tab["cells"][(3, 5)] == "对方来信：hi"

    ana = us.norm_op({"op": "event", "dir": "in", "email": "Ana@Gmail.com", "subject": "Re: brief",
                      "time": "2026-10-20T10:00:00"})
    us.run([ana], "test", us.load_map())
    assert tab["cells"][(1, 3)].when == dt.datetime(2026, 10, 20, 10, 0) and tab["cells"][(1, 4)] == "Me"
    assert tab["cells"][(1, 5)] == "对方来信：Re: brief" and tab["cells"][(1, 6)] == 300

    rows = us.list_rows(us.load_map(), ["邮箱", "频道链接"])
    assert {"邮箱": "new@example.com", "频道链接": "https://www.twitch.tv/newkol"} in rows
    with pytest.raises(SystemExit) as stop:
        us.main(["status"])
    assert stop.value.code == 0 and json.loads(capsys.readouterr().out)["exists"] is True


def test_the_token_folder_defaults_to_the_state_folder(fake, monkeypatch):
    monkeypatch.setattr(us, "STATE_DIR", fake.tokens)
    with us.google_grid(f"google:{SID}", MAP) as g:
        assert g.rows("KOLs")[1][0] == "Ana"


# ---------------------------------------------------------------- authorisation

def redirect_to(url, **answer):
    """What the colleague's browser does after they answer Google's consent page."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    target = q["redirect_uri"][0] + "?" + urllib.parse.urlencode({**answer, "state": q["state"][0]})
    threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(), daemon=True).start()
    return q


def test_authorize_from_the_settings_page(fake, tmp_path, monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("the page opens the address, not the app"))
    seen = {}
    tokens = tmp_path / "page"
    gg.authorize(tokens, "cid", "csecret", open_browser=False, timeout=20,
                 on_url=lambda url: seen.update(redirect_to(url, code="c0de")))
    assert seen["scope"] == [gg.SCOPE] and seen["code_challenge_method"] == ["S256"]
    saved = json.loads((tokens / "google-sheets-token.json").read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "r1" and saved["client_id"] == "cid"
    assert not (tokens / gsheet.TOKEN_FILE).exists()  # kol.py's token is another file
    assert gg.auth_status(tokens) == {"authorized": True, "error": None}


def test_authorize_declined_timed_out_or_without_the_sheets_permission(fake, tmp_path):
    with pytest.raises(gg.AuthError, match="access_denied"):
        gg.authorize(tmp_path, "cid", "cs", open_browser=False, timeout=20,
                     on_url=lambda url: redirect_to(url, error="access_denied"))
    with pytest.raises(gg.AuthError, match="超时"):
        gg.authorize(tmp_path, "cid", "cs", open_browser=False, timeout=1, on_url=lambda url: None)
    fake.granted = "https://www.googleapis.com/auth/drive.file"  # the colleague unticked the sheets box
    with pytest.raises(gg.AuthError, match="勾选"):
        gg.authorize(tmp_path, "cid", "cs", open_browser=False, timeout=20,
                     on_url=lambda url: redirect_to(url, code="c0de"))
    assert gg.auth_status(tmp_path)["authorized"] is False


def test_refusing_the_sheets_permission_keeps_the_token_that_worked(fake):
    before = json.loads((fake.tokens / gg.TOKEN_FILE).read_text(encoding="utf-8"))
    fake.granted = "https://www.googleapis.com/auth/drive.file"
    with pytest.raises(gg.AuthError, match="勾选"):
        gg.authorize(fake.tokens, "cid", "cs", open_browser=False, timeout=20,
                     on_url=lambda url: redirect_to(url, code="c0de"))
    assert json.loads((fake.tokens / gg.TOKEN_FILE).read_text(encoding="utf-8")) == before
    assert gg.auth_status(fake.tokens) == {"authorized": True, "error": None}


def test_kol_py_google_token_and_wording_are_unchanged(tmp_path):
    assert gsheet.TOKEN_FILE == "google-token.json" and gsheet.SCOPE == "https://www.googleapis.com/auth/drive.file"
    params = inspect.signature(gsheet.authorize).parameters
    assert params["token_file"].default == gsheet.TOKEN_FILE and params["scope"].default == gsheet.SCOPE
    assert gsheet.Credentials(tmp_path).path == tmp_path / "google-token.json"
    with pytest.raises(gsheet.GSheetError, match="请重新运行安装命令完成授权"):
        gsheet.Credentials(tmp_path).load()
    gsheet.Credentials(tmp_path).save({"client_id": "cid", "refresh_token": "r1"})
    revoked = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={})))
    with pytest.raises(gsheet.GSheetError, match="请重新运行安装命令重新授权"):
        gsheet.Credentials(tmp_path).access_token(revoked)
    assert gg.TOKEN_FILE != gsheet.TOKEN_FILE and gg.SCOPE != gsheet.SCOPE
