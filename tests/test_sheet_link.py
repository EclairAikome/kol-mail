"""Linking a colleague's own KOL sheet from the settings page: which sheet tool runs (configure_sheet), and the
/api/sheet endpoints that read, map, create and unlink a sheet."""

import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import config  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PAGE = {"x-kol-mail": "1", "origin": "http://127.0.0.1:8765"}


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    """Every test starts unlinked and leaves settings.json and the app's sheet state as it found them."""
    saved = config.SETTINGS_FILE.read_bytes() if config.SETTINGS_FILE.exists() else None
    monkeypatch.setitem(app.CFG, "paths", {**app.CFG["paths"], "sheet_tool": "", "sheet_workbook": "", "sheet_python": ""})
    monkeypatch.setitem(app.CFG, "sheet_map", {})
    for env in ("KOL_SHEET_TOOL", "KOL_SHEET_PATH", "KOL_SHEET_PYTHON"):
        monkeypatch.delenv(env, raising=False)
    app.configure_sheet()
    yield
    if saved is not None:
        config.SETTINGS_FILE.write_bytes(saved)
    monkeypatch.undo()
    app.configure_sheet()


def client():
    return TestClient(app.app, base_url="http://127.0.0.1:8765")


def sample(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Creators"
    ws.append(["Name", "Channel Link", "Email", "Last Contact", "Progress"])
    ws.append(["Ana", "https://www.youtube.com/@ana", "ana@gmail.com", None, "sent brief"])
    wb.save(path)
    return path


def link(tool="", workbook="", sheet_map=None):
    app.CFG["paths"].update(sheet_tool=str(tool), sheet_workbook=str(workbook))
    app.CFG["sheet_map"] = sheet_map or {}
    app.configure_sheet()
    return app.SHEET_MODE


def test_which_tool_runs(tmp_path):
    owners = tmp_path / "kol-sheet" / "kol.py"
    ours = app.BASE / "sheet" / "kol.py"
    assert link() == ""
    assert link(owners) == "legacy" and app.SHEET_TOOL == owners  # the owner's fixed layout, untouched
    assert link(workbook=tmp_path / "x.xlsx") == "mapped" and app.SHEET_TOOL == app.USER_SHEET_TOOL
    assert link(ours, tmp_path / "x.xlsx") == "legacy"  # an earlier installer built kol.py's template for it
    assert link(ours, "gsheet:abc123") == "legacy"  # the Mac Google sheet is kol.py's layout
    assert link(app.USER_SHEET_TOOL) == ""  # unlinked: the tool stays in the settings, nothing runs
    m = {"sheet": "S", "header_row": 1, "columns": {"email": "Email"}}
    link(workbook=tmp_path / "x.xlsx", sheet_map=m)
    assert '"Email"' in app.SHEET_ENV["KOL_SHEET_MAP"]


def test_linked_but_not_mapped_does_not_sync(tmp_path):
    link(workbook=sample(tmp_path / "a.xlsx"))
    assert not app.sheet_enabled()
    s = client().get("/api/status", headers=PAGE).json()["sheet"]
    assert s == {"mode": "mapped", "needs_map": True}
    link(workbook=tmp_path / "a.xlsx", sheet_map={"sheet": "Creators", "header_row": 1, "columns": {"email": "Email"}})
    assert app.sheet_enabled()


def test_inspect_save_and_unlink(tmp_path):
    path = sample(tmp_path / "a.xlsx")
    c = client()
    d = c.post("/api/sheet/inspect", json={"path": str(path)}, headers=PAGE).json()
    assert d["sheet"] == "Creators" and d["guess"] == {"name": "Name", "channel": "Channel Link", "email": "Email",
                                                       "last_contact": "Last Contact", "summary": "Progress"}
    r = c.post("/api/sheet/save", json={"path": str(path), "sheet": "Creators", "header_row": 1, "columns": d["guess"]},
               headers=PAGE)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] and app.SHEET_MODE == "mapped" and app.SHEET_MAP["columns"]["email"] == "Email"
    saved = config.load()
    assert saved["paths"]["sheet_workbook"] == str(path) and saved["sheet_map"]["sheet"] == "Creators"
    r = c.post("/api/sheet/unlink", json={}, headers=PAGE)
    assert r.status_code == 200 and not r.json()["path"] and app.SHEET_MODE == ""
    assert config.load()["sheet_map"] == {}
    assert path.exists()  # unlinking never touches the sheet itself


@pytest.mark.parametrize("body, error", [
    ({"columns": {"name": "Name"}}, "邮箱"),
    ({"columns": {"email": "Email", "name": "Email"}}, "同一列"),
    ({"columns": {"email": "E-mail"}}, "E-mail"),
    ({"columns": {"email": "Email"}, "sheet": "Nope"}, "Nope"),
])
def test_save_refuses_a_mapping_that_does_not_fit(tmp_path, body, error):
    path = sample(tmp_path / "a.xlsx")
    r = client().post("/api/sheet/save", json={"path": str(path), "sheet": "Creators", "header_row": 1, **body},
                      headers=PAGE)
    assert r.status_code == 400 and error in r.json()["error"]
    assert app.SHEET_MODE == ""


@pytest.mark.parametrize("path, error", [("", "先选"), ("KOL总表.xlsx", "完整路径"), ("C:/x/a.xls", ".xlsx")])
def test_paths_are_checked(path, error):
    r = client().post("/api/sheet/inspect", json={"path": path}, headers=PAGE)
    assert r.status_code == 400 and error in r.json()["error"]


def test_create_links_a_new_sheet(tmp_path):
    path = tmp_path / "新的" / "KOL总表.xlsx"
    r = client().post("/api/sheet/create", json={"path": str(path)}, headers=PAGE)
    assert r.status_code == 200, r.text
    assert path.exists() and r.json()["enabled"] and app.SHEET_MAP["columns"]["email"] == "邮箱"
    again = client().post("/api/sheet/create", json={"path": str(path)}, headers=PAGE)
    assert again.status_code == 400 and "已经有文件" in again.json()["error"]


def test_channel_links_come_from_the_mapped_columns(tmp_path):
    """What sheet_channels_loop runs: usersheet.py list, through the app's own environment."""
    import json
    import subprocess
    link(workbook=sample(tmp_path / "a.xlsx"),
         sheet_map={"sheet": "Creators", "header_row": 1, "columns": {"email": "Email", "channel": "Channel Link"}})
    out = subprocess.run([app.SHEET_PYTHON, str(app.SHEET_TOOL), "list", "--fields", "邮箱,频道链接,阶段,意向"],
                         env=app.SHEET_ENV, capture_output=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout.decode("utf-8"))["rows"] == [
        {"邮箱": "ana@gmail.com", "频道链接": "https://www.youtube.com/@ana", "阶段": "", "意向": ""}]


def test_an_earlier_install_can_move_to_a_mapping_from_the_page(tmp_path):
    link(app.BASE / "sheet" / "kol.py", tmp_path / "old.xlsx")
    assert app.SHEET_MODE == "legacy" and not app.SHEET_OWNER_FIXED
    path = sample(tmp_path / "a.xlsx")
    r = client().post("/api/sheet/save", json={"path": str(path), "sheet": "Creators", "header_row": 1,
                                               "columns": {"email": "Email"}}, headers=PAGE)
    assert r.status_code == 200, r.text
    assert app.SHEET_MODE == "mapped" and app.SHEET_TOOL == app.USER_SHEET_TOOL


def test_each_linked_file_keeps_its_own_queue_and_memory(tmp_path):
    link(workbook=tmp_path / "a.xlsx", sheet_map={"sheet": "S", "header_row": 1, "columns": {"email": "Email"}})
    first = app.SHEET_STATE
    link(workbook=tmp_path / "b.xlsx", sheet_map={"sheet": "S", "header_row": 1, "columns": {"email": "Email"}})
    assert app.SHEET_STATE != first and app.SHEET_PENDING.parent == app.SHEET_STATE
    assert app.SHEET_ENV["KOL_SHEET_STATE"] == str(app.SHEET_STATE)


def test_the_owners_sheet_cannot_be_changed_from_the_page(tmp_path):
    link(tmp_path / "kol-sheet" / "kol.py")
    path = sample(tmp_path / "a.xlsx")
    for url, body in (("/api/sheet/save", {"path": str(path), "sheet": "Creators", "columns": {"email": "Email"}}),
                      ("/api/sheet/create", {"path": str(tmp_path / "b.xlsx")}), ("/api/sheet/unlink", {})):
        r = client().post(url, json=body, headers=PAGE)
        assert r.status_code == 400 and "kol.py" in r.json()["error"]
    assert app.SHEET_MODE == "legacy" and not (tmp_path / "b.xlsx").exists()
