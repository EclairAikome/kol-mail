"""A POPO 在线表格 as the KOL sheet (sheet/popogrid.py), against a stand-in for popo-cli that answers the way the
real one does (several nested "data" layers, sparse cells, rich text headers)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sheet"))

import popogrid as pg  # noqa: E402
import usersheet as us  # noqa: E402

DOC = "0123456789abcdef0123456789abcdef"
SHEET_ID = "1768464310994995"


def wrap(inner_status, inner_data=None, message="成功"):
    body = {"status": inner_status, "message": message}
    if inner_data is not None:
        body["data"] = inner_data
    return json.dumps({"ok": True, "code": "FABRIC_OK", "data": {"code": "FABRIC_OK", "data": {
        "code": "FABRIC_OK", "hint": "Forwarded.", "message": "操作成功", "data": body}}})


class FakePopo:
    def __init__(self, cells: dict, row_count=200):
        self.cells = dict(cells)  # (row, col) 0-based -> value
        self.row_count = row_count
        self.commands: list[dict] = []
        self.args: list[list] = []

    def __call__(self, args):
        self.args.append(args)
        if args[1] == "doc_get_folder_path":
            return wrap(1, {"folderId": DOC, "teamSpaceId": "space123"})
        path = next(a for a in args if a.startswith("command=@file:"))[len("command=@file:"):]
        cmd = json.loads(Path(path).read_text(encoding="utf-8"))
        self.commands.append(cmd)
        t, pl = cmd["type"], cmd["payload"]
        if t == "workbook.getFullData":
            cells = [{"row": r, "col": c, "value": v} for (r, c), v in sorted(self.cells.items())]
            sheet = {"sheetId": SHEET_ID, "title": "KOLs", "rowCount": self.row_count, "colCount": 10,
                     "cells": cells, "hiddenRows": [], "hidden": False}
            other = {"sheetId": "2", "title": "说明", "rowCount": 10, "colCount": 2, "cells": [], "hidden": False}
            if pl.get("sheetId") == SHEET_ID:
                return wrap(1, {"tabs": [SHEET_ID, "2"], "sheets": [sheet]})
            return wrap(1, {"tabs": [SHEET_ID, "2"], "sheets": [sheet, other]})
        if t == "sheet.batchSetCells":
            for c in pl["cells"]:
                v = c.get("value")
                if "richText" in c:
                    v = "".join(p["text"] for p in c["richText"])
                self.cells[(c["row"], c["col"])] = v
            return wrap(1, {"version": 2})
        if t == "sheet.insertRows":
            self.row_count += pl["count"]
            return wrap(1, {"version": 3})
        raise AssertionError(t)


def colleague_sheet():
    return {(0, 0): {"richText": [{"text": "KOL-"}, {"text": "handle"}]}, (0, 1): "邮箱", (0, 2): "频道链接",
            (0, 3): "最近联系", (0, 4): "进展",
            (1, 0): "Ana", (1, 1): "ana@gmail.com", (1, 2): "https://www.youtube.com/@ana", (1, 3): "2026-10-01 08:00",
            (1, 4): "我方回信：brief",
            (2, 0): "Bo", (2, 1): "bo@x.com", (2, 4): "my own note"}


MAP = {"sheet": "KOLs", "sheet_id": SHEET_ID, "header_row": 1,
       "columns": {"name": "KOL-handle", "email": "邮箱", "channel": "频道链接", "last_contact": "最近联系",
                   "summary": "进展"}}


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    for name, value in (("STATE_DIR", tmp_path / "state"), ("PENDING", tmp_path / "state" / "pending.jsonl"),
                        ("STATE_FILE", tmp_path / "state" / "usersheet_state.json")):
        monkeypatch.setattr(us, name, value)


def grid(fake, tmp_path=None):
    return pg.PopoGrid(f"popo:{DOC}", us.Unavailable, us.UsageError, run=fake, sheet_ids={"KOLs": SHEET_ID},
                       backups=tmp_path / "backups" if tmp_path else None)


def test_events_reach_the_live_sheet(tmp_path):
    fake = FakePopo(colleague_sheet(), row_count=3)
    ops = [us.norm_op(o) for o in (
        {"op": "event", "dir": "in", "email": "ana@gmail.com", "subject": "Re", "time": "2026-10-20T09:00:00"},
        {"op": "event", "dir": "out", "email": "bo@x.com", "summary": "=我方回信：报价", "time": "2026-10-20T10:00:00"},
        {"op": "event", "dir": "in", "email": "new@example.com", "name": "New", "subject": "hi",
         "time": "2026-10-20T11:00:00", "channel_url": "https://www.twitch.tv/new"})]
    res = us.run(ops, "test", MAP, grid_factory=lambda: grid(fake, tmp_path))
    assert res["status"] == "ok" and res["mode"] == "popo", res
    c = fake.cells
    assert c[(1, 3)] == "2026-10-20 09:00" and c[(1, 4)] == "对方来信：Re"
    assert c[(2, 4)] == "my own note" and c[(2, 3)] == "2026-10-20 10:00"  # their note stays
    assert c[(3, 1)] == "new@example.com" and c[(3, 0)] == "New" and c[(3, 2)] == "https://www.twitch.tv/new"
    assert fake.row_count == 4  # the sheet had 3 rows: one was added before writing row 4
    rich = [cell for cmd in fake.commands if cmd["type"] == "sheet.batchSetCells" for cell in cmd["payload"]["cells"]
            if "richText" in cell]
    assert rich == []  # Bo's "=..." summary was not written: his own note stays
    writes = [cmd for cmd in fake.commands if cmd["type"] == "sheet.batchSetCells"]
    assert len(writes) == 1  # every cell of the sync in one call
    assert list((tmp_path / "backups").glob("popo_*.json"))


def test_text_that_looks_like_a_formula_goes_in_as_rich_text(tmp_path):
    fake = FakePopo(colleague_sheet())
    g = grid(fake)
    g.set_text("KOLs", 3, 5, "'=我方回信：报价")
    g.save()
    cell = fake.commands[-1]["payload"]["cells"][0]
    assert cell == {"row": 2, "col": 4, "richText": [{"text": "=我方回信：报价"}]}


def test_headers_in_rich_text_and_sheet_list(tmp_path):
    fake = FakePopo(colleague_sheet())
    g = pg.PopoGrid(f"popo:{DOC}", us.Unavailable, us.UsageError, run=fake)
    assert g.sheet_names() == ["KOLs", "说明"] and g.ids["KOLs"] == SHEET_ID
    assert g.rows("KOLs")[0][:2] == ["KOL-handle", "邮箱"]


def test_team_space_documents_carry_the_space_id(tmp_path):
    fake = FakePopo(colleague_sheet())
    setting = pg.resolve_link(f"https://docs.popo.example.com/team/pc/abc/pageDetail/{DOC}?tab=0", "cli", run=fake)
    assert setting == f"popo:{DOC}:space123"
    g = pg.PopoGrid(setting, us.Unavailable, us.UsageError, run=fake, sheet_ids={"KOLs": SHEET_ID})
    g.rows("KOLs")
    assert "teamSpaceId=space123" in fake.args[-1] and f"docId={DOC}" in fake.args[-1]
    assert pg.resolve_link(f"https://docs.popo.example.com/lingxi/{DOC}", "cli", run=fake) == f"popo:{DOC}"


@pytest.mark.parametrize("link", ["https://example.com/x", f"http://evil.com/{DOC}", "not a link", ""])
def test_only_popo_links(link):
    with pytest.raises(pg.PopoError):
        pg.resolve_link(link, "cli", run=lambda a: "")


def test_errors_say_what_to_do():
    with pytest.raises(pg.PopoError, match="没有权限"):
        pg.unwrap(wrap(403, message="暂无权限查看，请联系文档管理员"))
    with pytest.raises(pg.PopoError, match="auth login"):
        pg.unwrap(json.dumps({"ok": False, "code": "FABRIC_EXPIRED"}))
    with pytest.raises(pg.PopoError, match="看不懂|能读"):
        pg.unwrap("not json")
    assert pg.unwrap(wrap(1, {"x": 1})) == {"x": 1}


def test_failures_queue_instead_of_losing_changes(tmp_path):
    def broken(args):
        return wrap(403, message="暂无权限")
    g = lambda: pg.PopoGrid(f"popo:{DOC}", us.Unavailable, us.UsageError, run=broken, sheet_ids={"KOLs": SHEET_ID})
    op = us.norm_op({"op": "event", "dir": "in", "email": "ana@gmail.com", "subject": "x", "time": "2026-10-20T09:00:00"})
    res = us.run([op], "test", MAP, grid_factory=g)
    assert res["status"] == "queued" and "权限" in res["reason"] and us.PENDING.exists()


def test_the_command_file_is_removed(tmp_path):
    fake = FakePopo(colleague_sheet())
    grid(fake).rows("KOLs")
    path = next(a for a in fake.args[-1] if a.startswith("command=@file:"))[len("command=@file:"):]
    assert not Path(path).exists()


def test_a_failed_sync_writes_nothing(tmp_path):
    fake = FakePopo(colleague_sheet())
    g = grid(fake)
    g.set_text("KOLs", 2, 5, "x")
    assert not [c for c in fake.commands if c["type"] == "sheet.batchSetCells"]  # nothing sent before save()
