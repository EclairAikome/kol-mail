"""The Google Sheets backend of kol.py (the Mac build's KOL 总表), against an in-memory fake of the Sheets API: a new
sheet gets the right tabs and columns, mail events keep the books the same way as with Excel, a sheet edited while we
write is never written blindly, and network or authorisation trouble queues the changes instead of losing them."""

import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sheet"))

import gsheet  # noqa: E402


class FakeSheets:
    """Enough of the Sheets API v4 and the OAuth token endpoint for gsheet.py."""

    def __init__(self):
        self.books: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.token_status = 200
        self.down = False
        self.updates: list[list[dict]] = []

    # -- helpers
    def tab(self, sid, title):
        return self.books[sid]["tabs"][title]

    def rows(self, sid, title):
        """Like values.get: trailing empty cells and rows dropped, but a formula's "" is a value. The header's
        ARRAYFORMULA in KOL总表 fills 频道 down to the last grid row, "" where 频道链接 is empty."""
        t = self.tab(sid, title)
        out = [list(r) for r in t["cells"]]
        if title == "KOL总表" and out and "频道" in out[0] and "频道链接" in out[0]:
            ci, li = out[0].index("频道"), out[0].index("频道链接")
            while len(out) < t["rowCount"]:
                out.append([])
            for r in out[1:]:
                while len(r) <= max(ci, li):
                    r.append(None)
                r[ci] = "打开 ↗" if r[li] not in (None, "") else ""
        for r in out:
            while r and r[-1] is None:
                r.pop()
        while out and not out[-1]:
            out.pop()
        return out

    def put(self, sid, title, r, c, value):
        cells = self.tab(sid, title)["cells"]
        while len(cells) <= r:
            cells.append([])
        row = cells[r]
        while len(row) <= c:
            row.append(None)
        row[c] = value

    # -- transport
    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("offline", request=request)
        if getattr(self, "portal", False):  # a Wi-Fi login page answers every request
            return httpx.Response(200, text="<html>Please log in to the hotel Wi-Fi</html>")
        url, path = request.url, request.url.path
        self.calls.append((request.method, path))
        if str(url).startswith(gsheet.TOKEN_URL):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600, "refresh_token": "r1"})
        if request.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401, json={"error": {"message": "no token"}})
        if request.method == "POST" and path == "/v4/spreadsheets":
            return self.create(json.loads(request.content))
        sid = path.split("/")[3].split(":")[0]
        if sid not in self.books:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        book = self.books[sid]
        if request.method == "GET" and path.endswith("values:batchGet"):
            ranges = url.params.get_list("ranges")
            out = []
            for rng in ranges:
                title = rng.strip("'").replace("''", "'")
                out.append({"range": rng, "majorDimension": "ROWS", "values": self.rows(sid, title)})
            return httpx.Response(200, json={"spreadsheetId": sid, "valueRanges": out})
        if request.method == "GET":
            return httpx.Response(200, json={"spreadsheetId": sid, "sheets": [
                {"properties": {"sheetId": t["id"], "title": title,
                                "gridProperties": {"rowCount": t["rowCount"], "columnCount": 26}}}
                for title, t in book["tabs"].items()]})
        if request.method == "POST" and path.endswith(":batchUpdate"):
            requests = json.loads(request.content)["requests"]
            self.updates.append(requests)
            by_id = {t["id"]: title for title, t in book["tabs"].items()}
            for req in requests:
                if "appendDimension" in req:
                    a = req["appendDimension"]
                    book["tabs"][by_id[a["sheetId"]]]["rowCount"] += a["length"]
                elif "updateCells" in req:
                    u = req["updateCells"]
                    title = by_id[u["start"]["sheetId"]]
                    r, c = u["start"]["rowIndex"], u["start"]["columnIndex"]
                    assert r < book["tabs"][title]["rowCount"], "write outside the grid"
                    v = u["rows"][0]["values"][0].get("userEnteredValue") or {}
                    value = v.get("stringValue", v.get("numberValue", v.get("boolValue")))
                    assert "formulaValue" not in v
                    self.put(sid, title, r, c, value)
            return httpx.Response(200, json={"spreadsheetId": sid, "replies": [{} for _ in requests]})
        return httpx.Response(400, json={"error": {"message": f"unsupported {request.method} {path}"}})

    def create(self, body):
        sid = f"sheet{len(self.books) + 1}"
        tabs = {}
        for s in body["sheets"]:
            p = s["properties"]
            cells = []
            for row in s["data"][0]["rowData"]:
                vals = []
                for cell in row["values"]:
                    v = cell.get("userEnteredValue") or {}
                    if "formulaValue" in v:  # only the 频道 header formula is used
                        assert v["formulaValue"].startswith('={"频道";ARRAYFORMULA(')
                        vals.append("频道")
                    else:
                        vals.append(v.get("stringValue"))
                cells.append(vals)
            tabs[p["title"]] = {"id": p["sheetId"], "rowCount": p["gridProperties"]["rowCount"], "cells": cells}
        self.books[sid] = {"title": body["properties"]["title"], "tabs": tabs}
        return httpx.Response(200, json={"spreadsheetId": sid})


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeSheets()
    monkeypatch.setattr(gsheet, "client_factory", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    gsheet.Credentials(tmp_path).save({"client_id": "cid", "client_secret": "cs", "refresh_token": "r1"})
    return f


@pytest.fixture
def sheet(fake, tmp_path, monkeypatch):
    api = gsheet.Api(tmp_path)
    sid = gsheet.create(api, "KOL总表（KOL Mail）")
    api.close()
    monkeypatch.setenv("KOL_SHEET_PATH", f"gsheet:{sid}")
    monkeypatch.setenv("KOL_SHEET_STATE", str(tmp_path))
    monkeypatch.syspath_prepend(str(ROOT / "sheet"))
    kol = importlib.reload(importlib.import_module("kol"))
    monkeypatch.setattr(kol, "UNAVAILABLE_WAIT_SECONDS", 0)
    return kol, sid


def kol_rows(fake, sid):
    rows = fake.rows(sid, "KOL总表")
    found = [dict(zip(rows[0], r)) for r in rows[1:]]
    return [r for r in found if any(v not in (None, "") for k, v in r.items() if k != "频道")]


def log_rows(fake, sid):
    rows = fake.rows(sid, "沟通记录")
    return [dict(zip(rows[0], r)) for r in rows[1:]]


def test_new_sheet_has_the_xlsx_tabs_columns_and_dropdowns(fake, sheet):
    kol, sid = sheet
    tabs = fake.books[sid]["tabs"]
    assert list(tabs) == ["KOL总表", "沟通记录", "发信批次", "项目"]
    assert fake.rows(sid, "KOL总表")[0] == kol.MAIN_HEADERS
    assert fake.rows(sid, "沟通记录")[0] == kol.LOG_HEADERS
    assert fake.rows(sid, "发信批次")[0] == kol.BATCH_HEADERS
    rules = [r["setDataValidation"] for r in fake.updates[0] if "setDataValidation" in r]
    stage = [r for r in rules if r["range"]["startColumnIndex"] == kol.MAIN_HEADERS.index("阶段")][0]
    assert [v["userEnteredValue"] for v in stage["rule"]["condition"]["values"]] == kol.STAGES
    api = gsheet.Api(Path(kol.STATE_DIR))
    assert gsheet.check(api, sid) == []
    api.close()


def test_mail_events_keep_the_books_like_excel(fake, sheet):
    kol, sid = sheet
    ops = [kol.norm_op({"op": "upsert", "email": "ana@gmail.com", "kol": "Ana Gamer",
                        "channel": "https://youtube.com/@ana", "first": "2026-10-10"})]
    res = kol.run(ops, "test")
    assert res["status"] == "ok" and res["mode"] == "gsheet" and res["results"][0]["status"] == "created"
    row = kol_rows(fake, sid)[0]
    assert row["ID"] == "K0001" and row["KOL"] == "Ana Gamer" and row["阶段"] == "已发信"
    assert row["平台"] == "YouTube" and row["频道"] == "打开 ↗"
    assert isinstance(row["首次发信"], (int, float))  # a real date serial, not text

    incoming = kol.norm_op({"op": "event", "dir": "in", "email": "ana@gmail.com", "subject": "Re: collab",
                            "time": "2026-10-18T10:00:00+08:00", "message_id": "<m1@x.com>"})
    res = kol.run([incoming], "kol-mail")
    assert res["status"] == "ok", res
    row = kol_rows(fake, sid)[0]
    assert row["待办方"] == "我方" and row["阶段"] == "洽谈中" and "收到新回信" in row["下一步"]
    logs = log_rows(fake, sid)
    assert len(logs) == 1 and logs[0]["方向"] == "收" and logs[0]["ID"] == "K0001" and logs[0]["键"] == "<m1@x.com>"

    # the same mail again (a rescan): no second log row; the gist replaces the bare subject
    again = kol.norm_op({"op": "event", "dir": "in", "email": "ana@gmail.com", "summary": "对方回信：想要报价",
                         "time": "2026-10-18T10:00:00+08:00", "message_id": "<m1@x.com>"})
    assert kol.run([again], "kol-mail")["results"][0]["status"] == "summary_updated"
    logs = log_rows(fake, sid)
    assert len(logs) == 1 and logs[0]["摘要"] == "对方回信：想要报价"

    out = kol.norm_op({"op": "event", "dir": "out", "email": "ana@gmail.com", "summary": "我方回信：报价 150",
                       "time": "2026-10-18T11:00:00+08:00", "message_id": "<m2@x.com>"})
    kol.run([out], "kol-mail")
    row = kol_rows(fake, sid)[0]
    assert row["待办方"] == "对方" and len(log_rows(fake, sid)) == 2

    kols, logs = kol.snapshot()
    assert kols[0]["邮箱"] == "ana@gmail.com" and kols[0]["首次发信"] == "2026-10-10"
    assert logs[1]["时间"] == "2026-10-18 11:00" or logs[1]["时间"].startswith("2026-10-18")


def test_text_that_looks_like_a_formula_stays_text(fake, sheet):
    kol, sid = sheet
    kol.run([kol.norm_op({"op": "upsert", "email": "x@y.com", "note": "=IMPORTXML(\"http://evil\")"})], "t")
    for req in fake.updates[-1]:
        v = req.get("updateCells", {}).get("rows", [{"values": [{}]}])[0]["values"][0].get("userEnteredValue", {})
        assert "formulaValue" not in v


def test_rows_go_right_under_the_last_kol_despite_the_formula_column(fake, sheet, capsys):
    kol, sid = sheet
    for name in ("Ana", "Bob"):
        kol.run([kol.norm_op({"op": "upsert", "email": f"{name.lower()}@gmail.com", "kol": name})], "t")
    cells = fake.tab(sid, "KOL总表")["cells"]
    assert [r[1] for r in cells[1:3]] == ["Ana", "Bob"]  # rows 2 and 3 of the sheet, not 1000 and 1001
    assert fake.tab(sid, "KOL总表")["rowCount"] == 1000
    with pytest.raises(SystemExit):
        kol.main(["stats"])
    assert json.loads(capsys.readouterr().out)["total"] == 2


def test_concurrent_edit_queues_then_flush_writes(fake, sheet):
    kol, sid = sheet
    kol.run([kol.norm_op({"op": "upsert", "email": "ana@gmail.com", "kol": "Ana"})], "t")
    real_read = gsheet.Api.read
    state = {"n": 0}

    def read_then_edit(self, s, tabs):
        out = real_read(self, s, tabs)
        state["n"] += 1
        if state["n"] == 1:  # right after kol.py's first read, a row appears in the browser
            fake.put(sid, "KOL总表", 2, 1, "Bob")
        return out

    gsheet.Api.read = read_then_edit
    try:
        res = kol.run([kol.norm_op({"op": "event", "dir": "in", "email": "ana@gmail.com", "subject": "hi",
                                    "time": "2026-10-18T10:00:00+08:00", "message_id": "<m9@x.com>"})], "kol-mail")
    finally:
        gsheet.Api.read = real_read
    assert res["status"] == "queued" and "同时被人改了" in res["reason"]
    assert log_rows(fake, sid) == []
    assert kol.PENDING.exists()
    res = kol.run([], "补写队列")  # what kol-mail's sheet loop does every few minutes
    assert res["status"] == "ok" and res["flushed_from_queue"] == 1
    assert [r["KOL"] for r in kol_rows(fake, sid)] == ["Ana", "Bob"]
    assert kol_rows(fake, sid)[0]["待办方"] == "我方" and len(log_rows(fake, sid)) == 1
    assert not kol.PENDING.exists()


def test_offline_queues_and_nothing_is_lost(fake, sheet):
    kol, sid = sheet
    fake.down = True
    res = kol.run([kol.norm_op({"op": "upsert", "email": "ana@gmail.com", "kol": "Ana"})], "t")
    assert res["status"] == "queued" and "连不上 Google" in res["reason"]
    fake.down = False
    assert kol.run([], "补写队列")["status"] == "ok"
    assert kol_rows(fake, sid)[0]["KOL"] == "Ana"


def test_revoked_authorisation_says_so_and_queues(fake, sheet, tmp_path):
    kol, sid = sheet
    data = json.loads((tmp_path / gsheet.TOKEN_FILE).read_text(encoding="utf-8"))
    data.pop("access_token", None)
    (tmp_path / gsheet.TOKEN_FILE).write_text(json.dumps(data), encoding="utf-8")
    fake.token_status = 400
    res = kol.run([kol.norm_op({"op": "upsert", "email": "ana@gmail.com"})], "t")
    assert res["status"] == "queued" and "重新授权" in res["reason"]


def test_missing_authorisation(fake, sheet, tmp_path):
    kol, sid = sheet
    (tmp_path / gsheet.TOKEN_FILE).unlink()
    res = kol.run([kol.norm_op({"op": "upsert", "email": "ana@gmail.com"})], "t")
    assert res["status"] == "queued" and "授权" in res["reason"]


def test_grid_grows_when_full(fake, sheet):
    kol, sid = sheet
    fake.books[sid]["tabs"]["沟通记录"]["rowCount"] = 2  # header + one row
    for n in range(3):
        kol.run([kol.norm_op({"op": "log", "dir": "in", "email": f"k{n}@gmail.com", "summary": f"mail {n}",
                              "time": f"2026-10-1{n}T10:00:00", "message_id": f"<{n}@x.com>"})], "t")
    assert len(log_rows(fake, sid)) == 3
    assert fake.books[sid]["tabs"]["沟通记录"]["rowCount"] >= 4


def test_token_file_is_private(tmp_path):
    gsheet.Credentials(tmp_path).save({"client_id": "a", "refresh_token": "b"})
    if sys.platform != "win32":
        assert (tmp_path / gsheet.TOKEN_FILE).stat().st_mode & 0o077 == 0


def test_setting_parser():
    assert gsheet.sheet_id_of("gsheet:abc123") == "abc123"
    assert gsheet.sheet_id_of(r"C:\Users\alex\KOL总表.xlsx") is None
    assert gsheet.sheet_id_of("gsheet:") is None


def test_authorize_with_loopback_redirect(fake, tmp_path, monkeypatch):
    import threading
    import urllib.parse
    import urllib.request
    import webbrowser
    seen = {}

    def browser(url):  # what the user's browser does after they click "allow"
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        seen.update(q)
        target = q["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "c0de", "state": q["state"][0]})
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(), daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", browser)
    state = tmp_path / "auth"
    gsheet.authorize(state, "cid", "csecret", timeout=20)
    saved = json.loads((state / gsheet.TOKEN_FILE).read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "r1" and saved["client_id"] == "cid"
    assert seen["scope"] == [gsheet.SCOPE] and seen["code_challenge_method"] == ["S256"]
    assert seen["redirect_uri"][0].startswith("http://127.0.0.1:")


def test_read_commands_offline_exit_3_with_a_reason(fake, sheet, capsys):
    kol, sid = sheet
    fake.down = True
    with pytest.raises(SystemExit) as stop:
        kol.main(["list", "--fields", "邮箱,频道链接,阶段,意向"])
    assert stop.value.code == 3
    assert "连不上 Google" in json.loads(capsys.readouterr().out)["reason"]


def test_list_reads_the_google_sheet(fake, sheet, capsys):
    kol, sid = sheet
    kol.run([kol.norm_op({"op": "upsert", "email": "ana@gmail.com", "kol": "Ana", "channel": "https://youtube.com/@ana"})], "t")
    with pytest.raises(SystemExit) as stop:
        kol.main(["list", "--fields", "邮箱,频道链接,阶段,意向"])
    assert stop.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"] == [{"邮箱": "ana@gmail.com", "频道链接": "https://youtube.com/@ana", "阶段": "未联系", "意向": ""}]


def test_wifi_login_page_queues_instead_of_crashing(fake, sheet):
    kol, sid = sheet
    fake.portal = True
    res = kol.run([kol.norm_op({"op": "upsert", "email": "ana@gmail.com", "kol": "Ana"})], "t")
    assert res["status"] == "queued" and "网络登录页" in res["reason"]
    fake.portal = False
    assert kol.run([], "补写队列")["status"] == "ok"
    assert kol_rows(fake, sid)[0]["KOL"] == "Ana"
