"""Excel for Mac through AppleScript (osascript), the Mac side of sheet/usersheet.py's Grid.

Open in Excel: written into that window, which the colleague sees at once (we save too). Not open: Excel opens it in
the background (`open -g`), we write, save and close it, and quit Excel again if it was not running before. The file is
never written behind Excel's back, so nothing Excel knows about (charts, images, formats) is lost.

Every value goes to AppleScript as an argument of `on run argv`, never pasted into the script text, so a cell text can
not change what the script does. macOS asks once whether KOL Mail may control Microsoft Excel (系统设置 → 隐私与安全性
→ 自动化); until it is allowed every call fails with -1743 and the changes queue.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

RS, US = "\x1e", "\x1f"  # row and cell separators in what the read script returns
OPEN_WAIT_SECONDS = 90

LIST_BOOKS = r'''
on run argv
    set sepCell to character id 31
    set sepRow to character id 30
    tell application "Microsoft Excel"
        set fullNames to full name of every workbook
        set shortNames to name of every workbook
    end tell
    set out to {}
    repeat with i from 1 to count of fullNames
        set end of out to (item i of fullNames) & sepCell & (item i of shortNames)
    end repeat
    set AppleScript's text item delimiters to sepRow
    return out as text
end run
'''

SHEET_NAMES = r'''
on run argv
    set bk to item 1 of argv
    tell application "Microsoft Excel"
        set out to name of every worksheet of workbook bk
    end tell
    set AppleScript's text item delimiters to (character id 30)
    return out as text
end run
'''

# value of the used range, one row per RS, cells by US; dates as ISO text, numbers as plain digits
READ_SHEET = r'''
on iso(d)
    set y to year of d as integer
    set m to month of d as integer
    set dd to day of d as integer
    set s to time of d
    return (y as text) & "-" & my two(m) & "-" & my two(dd) & " " & my two(s div 3600) & ":" & my two((s mod 3600) div 60) & ":" & my two(s mod 60)
end iso
on two(n)
    return text -2 thru -1 of ("0" & (n as integer as text))
end two
on cellText(v)
    if v is missing value then return ""
    set k to class of v
    if k is date then return my iso(v)
    if k is real or k is integer then
        set t to v as text
        set AppleScript's text item delimiters to ","
        set parts to text items of t
        set AppleScript's text item delimiters to "."
        set t to parts as text
        return t
    end if
    if k is boolean then return v as text
    return v as text
end cellText
on run argv
    set bk to item 1 of argv
    set sh to item 2 of argv
    set sepCell to character id 31
    set sepRow to character id 30
    tell application "Microsoft Excel"
        set ws to worksheet sh of workbook bk
        set ur to used range of ws
        set r0 to first row index of ur
        set c0 to first column index of ur
        set nCols to count of columns of ur
        set vals to value of ur
    end tell
    if class of vals is not list then
        set vals to {{vals}}
    else if (count of vals) > 0 and class of item 1 of vals is not list then
        if nCols is 1 then
            set oneCol to {}
            repeat with v in vals
                set end of oneCol to {contents of v}
            end repeat
            set vals to oneCol
        else
            set vals to {vals}
        end if
    end if
    set rowsOut to {(r0 as text) & sepCell & (c0 as text)}
    repeat with rw in vals
        set cellList to {}
        repeat with v in rw
            set end of cellList to my cellText(contents of v)
        end repeat
        set AppleScript's text item delimiters to sepCell
        set end of rowsOut to cellList as text
    end repeat
    set AppleScript's text item delimiters to sepRow
    return rowsOut as text
end run
'''

SET_TEXT = r'''
on run argv
    set bk to item 1 of argv
    set sh to item 2 of argv
    set cellAddr to item 3 of argv
    set newValue to item 4 of argv
    tell application "Microsoft Excel"
        set value of range cellAddr of worksheet sh of workbook bk to newValue
    end tell
    return "ok"
end run
'''

# argv: book, sheet, address, address of the cell above (or ""), year, month, day, seconds, default format, the
# same moment as text ("2026-10-20 09:00") for a column formatted as text
SET_DATE = r'''
on isGeneral(nf)
    return nf is "General" or nf is "通用格式" or nf is "常规" or nf is "G/通用格式"
end isGeneral
on run argv
    set bk to item 1 of argv
    set sh to item 2 of argv
    set cellAddr to item 3 of argv
    set aboveAddr to item 4 of argv
    set theDate to current date
    set day of theDate to 1
    set year of theDate to (item 5 of argv) as integer
    set month of theDate to (item 6 of argv) as integer
    set day of theDate to (item 7 of argv) as integer
    set time of theDate to (item 8 of argv) as integer
    set defaultFormat to item 9 of argv
    set asText to item 10 of argv
    tell application "Microsoft Excel"
        set rg to range cellAddr of worksheet sh of workbook bk
        set nf to number format of rg
    end tell
    if nf is "@" then
        tell application "Microsoft Excel" to set value of rg to asText
        return "text"
    end if
    if my isGeneral(nf) then
        set newFormat to defaultFormat
        if aboveAddr is not "" then
            tell application "Microsoft Excel" to set nfAbove to number format of range aboveAddr of worksheet sh of workbook bk
            if not my isGeneral(nfAbove) and nfAbove is not "@" then set newFormat to nfAbove
        end if
        tell application "Microsoft Excel" to set number format of rg to newFormat
    end if
    tell application "Microsoft Excel" to set value of rg to theDate
    return "date"
end run
'''

# argv: book, sheet, header row, last data row, free row, the free row's cell addresses joined by commas. Copies the
# last data row (formats, formulas) into the free row and clears the copied constants; grows an Excel table that ends
# right above. Returns "table" when a table grew, "table-failed" when growing it failed, else "no".
APPEND_ROW = r'''
on run argv
    set bk to item 1 of argv
    set sh to item 2 of argv
    set hr to (item 3 of argv) as integer
    set lastRow to (item 4 of argv) as integer
    set newRow to (item 5 of argv) as integer
    set AppleScript's text item delimiters to ","
    set newCells to text items of (item 6 of argv)
    set AppleScript's text item delimiters to ""
    set grown to "no"
    if newRow is (lastRow + 1) then
        try
            tell application "Microsoft Excel"
                set ws to worksheet sh of workbook bk
                set tableList to list objects of ws
            end tell
            repeat with lo in tableList
                tell application "Microsoft Excel"
                    set rr to range object of lo
                    set firstRowIndex to first row index of rr
                    set rowCount to count of rows of rr
                end tell
                if firstRowIndex is hr and (firstRowIndex + rowCount - 1) is lastRow then
                    tell application "Microsoft Excel"
                        set biggerRange to get resize rr row size (rowCount + 1)
                        resize lo range biggerRange
                    end tell
                    set grown to "table"
                end if
            end repeat
        on error
            set grown to "table-failed"
        end try
    end if
    if lastRow > hr then
        tell application "Microsoft Excel"
            set ws to worksheet sh of workbook bk
            copy range (range ((lastRow as text) & ":" & (lastRow as text)) of ws) destination (range ((newRow as text) & ":" & (newRow as text)) of ws)
            repeat with cellAddr in newCells
                set oneCell to range (contents of cellAddr) of ws
                if not (has formula of oneCell) then clear contents oneCell
            end repeat
        end tell
    end if
    return grown
end run
'''

SAVE = r'''
on run argv
    tell application "Microsoft Excel" to save workbook (item 1 of argv)
    return "ok"
end run
'''

CLOSE = r'''
on run argv
    tell application "Microsoft Excel" to close workbook (item 1 of argv) saving no
    return "ok"
end run
'''

QUIT = r'''
on run argv
    tell application "Microsoft Excel"
        if (count of workbooks) is 0 then
            quit
            return "quit"
        end if
    end tell
    return "kept"
end run
'''

BOOK_READ_ONLY = r'''
on run argv
    tell application "Microsoft Excel" to return (read only of workbook (item 1 of argv)) as text
end run
'''


def col_letters(c: int) -> str:
    out = ""
    while c:
        c, rem = divmod(c - 1, 26)
        out = chr(65 + rem) + out
    return out


def addr(r: int, c: int) -> str:
    return f"{col_letters(c)}{r}"


def posix_of(full_name: str) -> str:
    """Excel reports a workbook's place as a POSIX path or, in some versions, an HFS path ("Macintosh HD:Users:...")."""
    if full_name.startswith("/") or re.match(r"[A-Za-z]:[\\/]", full_name):  # POSIX (or a Windows path in tests)
        return full_name
    if ":" in full_name:
        parts = full_name.split(":")
        return "/" + "/".join(parts[1:])
    return full_name


def same_file(a: str, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(str(b)))


def number(text: str):
    return float(text) if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text) else text


class MacExcelGrid:
    """usersheet.Grid for Excel for Mac. `osa` runs one AppleScript with arguments and returns its result (tests
    replace it); `unavailable` and `usage` are usersheet's exception classes."""

    def __init__(self, path: Path, write: bool, unavailable, usage, backups: Path | None = None, keep_backups: int = 14,
                 osa=None, excel_running=None, open_in_background=None):
        self.path, self.write = path, write
        self.Unavailable, self.UsageError = unavailable, usage
        self.backups, self.keep_backups = backups, keep_backups
        self.real = osa is None
        self.osa = osa or self._osascript
        self.excel_running = excel_running or self._excel_running
        self.open_in_background = open_in_background or self._open_in_background
        self.book = ""
        self.mode = ""
        self.quit_after = False

    # -- plumbing

    def _osascript(self, script: str, *args) -> str:
        try:
            r = subprocess.run(["osascript", "-", *[str(a) for a in args]], input=script.encode("utf-8"),
                               capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise self.Unavailable("Excel 两分钟没有响应（可能开着对话框或正在编辑单元格）") from None
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip()
            if "-1743" in err:
                raise self.Unavailable("macOS 还没允许 KOL Mail 控制 Excel：打开 系统设置 → 隐私与安全性 → 自动化，"
                                       "在 KOL Mail 用的 Python 下面勾上 Microsoft Excel")
            if "-1712" in err:
                raise self.Unavailable("Excel 正忙（可能开着对话框或正在编辑单元格）")
            raise self.Unavailable(f"Excel 没有完成操作：{err[-200:]}")
        return r.stdout.decode("utf-8", "replace").rstrip("\n")

    @staticmethod
    def _excel_running() -> bool:
        """Without an Apple Event: asking Excel anything would start it."""
        return subprocess.run(["pgrep", "-x", "Microsoft Excel"], capture_output=True).returncode == 0

    def _open_in_background(self, path: Path):
        try:
            r = subprocess.run(["open", "-g", "-a", "Microsoft Excel", str(path)], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise self.Unavailable(f"打不开总表：{type(e).__name__}") from None
        if r.returncode != 0:
            raise self.Unavailable(f"Excel 打不开总表：{r.stderr.decode('utf-8', 'replace').strip()[-200:]}")

    @staticmethod
    def _excel_installed() -> bool:
        return subprocess.run(["open", "-Ra", "Microsoft Excel"], capture_output=True, timeout=30).returncode == 0

    def _find_book(self) -> str:
        listing = self.osa(LIST_BOOKS)
        online = []
        for item in filter(None, listing.split(RS)):
            full, _, name = item.partition(US)
            if full.startswith(("http://", "https://")):  # a OneDrive / SharePoint copy reports its web address
                online.append(name)
            elif same_file(posix_of(full), self.path):
                return name
        same_name = [n for n in online if n == self.path.name]
        return same_name[0] if len(same_name) == 1 else ""

    # -- context manager

    def __enter__(self):
        try:
            return self._connect()
        except BaseException:
            self.__exit__(None, None, None)  # never leave a book we opened, or an Excel we started, behind
            raise

    def _connect(self):
        if self.real and not self._excel_installed():
            raise self.Unavailable("这台 Mac 上没有装 Microsoft Excel，写不进总表")
        try:
            exists = self.path.exists()
        except OSError as e:  # e.g. macOS has not allowed access to the folder
            raise self.Unavailable(f"读不到总表所在的文件夹：{e}") from None
        if not exists:
            raise self.UsageError(f"找不到总表：{self.path}")
        was_running = self.excel_running()
        self.book = self._find_book() if was_running else ""
        if self.book:
            self.mode = "live"
        else:
            self.mode = "opening"  # from here on, __exit__ closes what we opened
            self.quit_after = not was_running
            self.open_in_background(self.path)
            deadline = time.monotonic() + OPEN_WAIT_SECONDS
            while not self.book:
                if time.monotonic() > deadline:
                    raise self.Unavailable("Excel 一分半钟都没把总表打开（可能在等你登录或点对话框）")
                time.sleep(1.5)
                try:
                    self.book = self._find_book()
                except self.Unavailable:
                    if time.monotonic() > deadline:
                        raise
            self.mode = "headless"
        if self.write and self.osa(BOOK_READ_ONLY, self.book).strip().lower() == "true":
            raise self.Unavailable("总表在 Excel 里是以只读方式打开的，存不进去。请关掉后正常打开")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.mode not in ("headless", "opening"):
            return
        try:
            if not self.book:
                self.book = self._find_book()  # it may have opened after we gave up waiting
            if self.book:
                self.osa(CLOSE, self.book)
            if self.quit_after:
                self.osa(QUIT)
        except Exception:  # noqa: BLE001  leaving Excel open is better than failing the sync
            pass

    # -- Grid

    def sheet_names(self) -> list[str]:
        return [s for s in self.osa(SHEET_NAMES, self.book).split(RS) if s]

    def rows(self, sheet: str) -> list[list]:
        if sheet not in self.sheet_names():
            raise self.UsageError(f"总表里没有叫“{sheet}”的工作表了（被改名或删掉了？），请在设置里重新对应")
        lines = self.osa(READ_SHEET, self.book, sheet).split(RS)
        r0, _, c0 = lines[0].partition(US)
        r0, c0 = int(r0), int(c0)
        out: list[list] = [[] for _ in range(r0 - 1)]
        for line in lines[1:]:
            out.append([None] * (c0 - 1) + [number(v) if v else None for v in line.split(US)])
        self.width = max((len(r) for r in out), default=1)
        return out

    def set_text(self, sheet: str, r: int, c: int, value: str):
        self.osa(SET_TEXT, self.book, sheet, addr(r, c), value)

    def set_date(self, sheet: str, r: int, c: int, value: dt.datetime, header_row: int):
        above = addr(r - 1, c) if r - 1 > header_row else ""
        secs = value.hour * 3600 + value.minute * 60 + value.second
        self.osa(SET_DATE, self.book, sheet, addr(r, c), above, value.year, value.month, value.day, secs,
                 "yyyy-mm-dd hh:mm", f"{value:%Y-%m-%d %H:%M}")

    def append_row(self, sheet: str, header_row: int, last_row: int, free_row: int) -> int:
        width = getattr(self, "width", 0) or max((len(r) for r in self.rows(sheet)), default=1)
        cells = ",".join(addr(free_row, c) for c in range(1, width + 1))
        self.table_grown = self.osa(APPEND_ROW, self.book, sheet, header_row, last_row, free_row, cells)
        return free_row

    def save(self):
        self.osa(SAVE, self.book)

    def backup_once_a_day(self):
        """A copy of the file as last saved, before today's first change."""
        if not self.backups:
            return
        self.backups.mkdir(parents=True, exist_ok=True)
        target = self.backups / f"{self.path.stem}_{dt.date.today():%Y%m%d}{self.path.suffix}"
        if not target.exists():
            try:
                shutil.copy2(self.path, target)
            except OSError as e:
                raise self.Unavailable(f"备份总表失败：{e}") from None
        for f in sorted(self.backups.glob(f"{self.path.stem}_*{self.path.suffix}"))[:-self.keep_backups]:
            try:
                f.unlink()
            except OSError:
                pass
