"""The Mac writer (sheet/macexcel.py) against a stand-in for Excel: the Python side of every AppleScript call, the
open/close/quit choices, and that the sync rules of usersheet.py hold through it. The AppleScript itself only runs
on a real Mac (the colleague's self-test)."""

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sheet"))

import macexcel as mx  # noqa: E402
import usersheet as us  # noqa: E402

RS, US = mx.RS, mx.US


def cell_of(a: str) -> tuple[int, int]:
    letters, digits = "".join(ch for ch in a if ch.isalpha()), "".join(ch for ch in a if ch.isdigit())
    c = 0
    for ch in letters:
        c = c * 26 + ord(ch) - 64
    return int(digits), c


class FakeExcel:
    """Enough of Excel for Mac to answer the scripts in macexcel.py the way they are written to answer."""

    def __init__(self, path: Path, cells: dict, running=True, open_now=True, read_only=False):
        self.path, self.cells, self.running = path, dict(cells), running
        self.formats: dict = {}
        self.formulas: dict = {}
        self.open = open_now
        self.read_only = read_only
        self.calls: list[str] = []
        self.saved = 0

    def open_in_background(self, path):
        self.calls.append("open")
        self.running = self.open = True

    def osa(self, script, *args):
        name = next(k for k, v in vars(mx).items() if v is script)
        self.calls.append(name)
        if name == "LIST_BOOKS":
            return str(self.path) + US + self.path.name if self.open else ""
        if name == "SHEET_NAMES":
            return "Creators" + RS + "Notes"
        if name == "BOOK_READ_ONLY":
            return "true" if self.read_only else "false"
        if name == "READ_SHEET":
            used = [k for k, v in self.cells.items() if v not in (None, "")] or [(1, 1)]
            r0, c0 = min(r for r, _ in used), min(c for _, c in used)
            r1, c1 = max(r for r, _ in used), max(c for _, c in used)
            lines = [f"{r0}{US}{c0}"]
            for r in range(r0, r1 + 1):
                out = []
                for c in range(c0, c1 + 1):
                    v = self.cells.get((r, c))
                    out.append(v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, dt.datetime) else
                               "" if v is None else str(v))
                lines.append(US.join(out))
            return RS.join(lines)
        if name == "SET_TEXT":
            _, _, a, v = args
            self.cells[cell_of(a)] = v[1:] if v.startswith("'") else v  # Excel keeps the apostrophe as a text mark
            return "ok"
        if name == "SET_DATE":
            _, _, a, above, y, m, d, secs, fmt, as_text = [str(x) for x in args]
            rc = cell_of(a)
            when = dt.datetime(int(y), int(m), int(d)) + dt.timedelta(seconds=int(secs))
            if self.formats.get(rc) == "@":
                self.cells[rc] = as_text
                return "text"
            if self.formats.get(rc, "General") == "General":
                fa = self.formats.get(cell_of(above), "General") if above else "General"
                self.formats[rc] = fa if fa not in ("General", "@") else fmt
            self.cells[rc] = when
            return "date"
        if name == "APPEND_ROW":
            _, _, hr, last, new, cells = args
            hr, last, new = int(hr), int(last), int(new)
            targets = [cell_of(a) for a in cells.split(",")]
            assert all(r == new for r, _ in targets)
            if last > hr:
                for _, c in targets:
                    if (last, c) in self.formats:
                        self.formats[(new, c)] = self.formats[(last, c)]
                    if (last, c) in self.formulas:
                        self.formulas[(new, c)] = self.formulas[(last, c)]
            return "no"
        if name == "SAVE":
            self.saved += 1
            return "ok"
        if name == "CLOSE":
            self.open = False
            return "ok"
        if name == "QUIT":
            if self.open:
                return "kept"
            self.running = False
            return "quit"
        raise AssertionError(name)


def sheet_cells():
    return {(1, 1): "Q4 outreach",
            (2, 1): "Name", (2, 2): "Email", (2, 3): "Last Contact", (2, 4): "Ball", (2, 5): "Progress", (2, 6): "Fee",
            (3, 1): "Ana", (3, 2): "ana@gmail.com", (3, 3): dt.datetime(2026, 10, 1, 8), (3, 4): "Them",
            (3, 5): "我方回信：brief", (3, 6): 300,
            (4, 1): "Bo", (4, 2): "bo@x.com", (4, 4): "Me", (4, 6): 100,
            (6, 6): "=SUM(F3:F4)"}


MAP = {"sheet": "Creators", "header_row": 2,
       "columns": {"name": "Name", "email": "Email", "last_contact": "Last Contact", "owner": "Ball",
                   "summary": "Progress"}}


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mx, "OPEN_WAIT_SECONDS", 3)
    monkeypatch.setattr(mx.time, "sleep", lambda s: None)
    for name, value in (("STATE_DIR", tmp_path / "state"), ("PENDING", tmp_path / "state" / "pending.jsonl"),
                        ("STATE_FILE", tmp_path / "state" / "usersheet_state.json")):
        monkeypatch.setattr(us, name, value)


def run(fake, ops, write=True):
    grid = lambda: mx.MacExcelGrid(fake.path, write, us.Unavailable, us.UsageError, fake.path.parent / "backups",
                                   osa=fake.osa, excel_running=lambda: fake.running,
                                   open_in_background=fake.open_in_background)
    return us.run([us.norm_op(o) for o in ops], "test", MAP, grid_factory=grid)


@pytest.fixture
def book(tmp_path):
    p = tmp_path / "KOL 表.xlsx"
    p.write_bytes(b"xlsx")
    return p


OPS = [{"op": "event", "dir": "in", "email": "ana@gmail.com", "subject": "Re", "time": "2026-10-20T09:00:00"},
       {"op": "event", "dir": "out", "email": "bo@x.com", "summary": "=我方回信：报价", "time": "2026-10-20T10:00:00"},
       {"op": "event", "dir": "in", "email": "new@example.com", "name": "New", "subject": "hi", "time": "2026-10-20T11:00:00"}]


def test_open_workbook_is_written_live(book):
    fake = FakeExcel(book, sheet_cells())
    res = run(fake, OPS)
    assert res["status"] == "ok" and res["mode"] == "live", res
    c = fake.cells
    assert c[(3, 3)] == dt.datetime(2026, 10, 20, 9) and c[(3, 4)] == "Me" and c[(3, 5)] == "对方来信：Re"
    assert c[(4, 4)] == "Them" and c[(4, 5)] == "=我方回信：报价"  # the apostrophe made it text
    assert c[(5, 2)] == "new@example.com" and c[(5, 1)] == "New" and c[(6, 6)] == "=SUM(F3:F4)"
    assert "open" not in fake.calls and "CLOSE" not in fake.calls and fake.saved == 1
    assert (book.parent / "backups").exists()


def test_closed_workbook_opens_in_the_background_then_closes_and_quits(book):
    fake = FakeExcel(book, sheet_cells(), running=False, open_now=False)
    res = run(fake, OPS[:1])
    assert res["mode"] == "headless" and fake.calls.count("open") == 1
    assert fake.calls[-2:] == ["CLOSE", "QUIT"] and not fake.running


def test_excel_already_running_is_left_running(book):
    fake = FakeExcel(book, sheet_cells(), running=True, open_now=False)
    run(fake, OPS[:1])
    assert "CLOSE" in fake.calls and "QUIT" not in fake.calls and fake.running


def test_a_book_we_opened_is_closed_again_when_it_cannot_be_written(book):
    fake = FakeExcel(book, sheet_cells(), running=False, open_now=False, read_only=True)
    res = run(fake, OPS[:1])
    assert res["status"] == "queued" and not fake.open and not fake.running


def test_excel_that_never_opens_the_book_is_quit(book, monkeypatch):
    fake = FakeExcel(book, sheet_cells(), running=False, open_now=False)
    monkeypatch.setattr(fake, "open_in_background", lambda path: setattr(fake, "running", True))  # opens nothing
    res = run(fake, OPS[:1])
    assert res["status"] == "queued" and "一分半钟" in res["reason"] and not fake.running


def test_read_only_workbook_queues(book):
    fake = FakeExcel(book, sheet_cells(), read_only=True)
    res = run(fake, OPS[:1])
    assert res["status"] == "queued" and "只读" in res["reason"] and us.PENDING.exists()


def test_date_takes_the_format_of_the_cell_above_or_text(book):
    fake = FakeExcel(book, sheet_cells())
    fake.formats[(3, 3)] = "dd/mm/yyyy"
    fake.formats[(4, 3)] = "@"
    run(fake, OPS[:2])
    assert fake.cells[(4, 3)] == "2026-10-20 10:00"  # a text column gets text
    assert fake.formats[(3, 3)] == "dd/mm/yyyy"


def test_numbers_and_dates_come_back_usable(book):
    fake = FakeExcel(book, sheet_cells())
    grid = mx.MacExcelGrid(book, False, us.Unavailable, us.UsageError, osa=fake.osa, excel_running=lambda: True,
                           open_in_background=fake.open_in_background)
    with grid as g:
        rows = g.rows("Creators")
    assert rows[2][:6] == ["Ana", "ana@gmail.com", "2026-10-01 08:00:00", "Them", "我方回信：brief", 300.0]
    assert us.from_serial(rows[2][2]) == dt.datetime(2026, 10, 1, 8) and rows[0][0] == "Q4 outreach"


def test_addresses_and_paths():
    assert [mx.addr(1, 1), mx.addr(5, 26), mx.addr(7, 27), mx.addr(2, 703)] == ["A1", "Z5", "AA7", "AAA2"]
    assert mx.posix_of("Macintosh HD:Users:alex:Documents:KOL.xlsx") == "/Users/alex/Documents/KOL.xlsx"
    assert mx.posix_of("/Users/alex/KOL.xlsx") == "/Users/alex/KOL.xlsx"


def test_permission_refusal_is_explained(book, monkeypatch):
    grid = mx.MacExcelGrid(book, True, us.Unavailable, us.UsageError)

    def refused(*a, **k):
        return subprocess.CompletedProcess(a, 1, b"", b"execution error: Not authorized to send Apple events to Microsoft Excel. (-1743)")
    monkeypatch.setattr(mx.subprocess, "run", refused)
    with pytest.raises(us.Unavailable, match="自动化"):
        grid._osascript(mx.SAVE, "x")


def test_cell_text_reaches_applescript_only_as_an_argument(book, monkeypatch):
    seen = {}

    def fake_run(cmd, input=None, **k):
        seen["cmd"], seen["script"] = cmd, input.decode("utf-8")
        return subprocess.CompletedProcess(cmd, 0, b"ok\n", b"")
    monkeypatch.setattr(mx.subprocess, "run", fake_run)
    grid = mx.MacExcelGrid(book, True, us.Unavailable, us.UsageError)
    grid.book = "KOL 表.xlsx"
    evil = '" & (do shell script "rm -rf ~") & "'
    grid.set_text("Creators", 3, 5, evil)
    assert seen["cmd"][:2] == ["osascript", "-"] and seen["cmd"][-1] == evil and evil not in seen["script"]
