"""The release scan (tools/publish.py): what may go into the public repository."""

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import publish  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(extra: bytes = b"") -> bytes:
    pixels = zlib.compress(b"\0" + b"\0\0\0\0" * 2 + b"\0" + b"\0\0\0\0" * 2)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)) + extra
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


def ico(*pictures: bytes, gap: bytes = b"", tail: bytes = b"") -> bytes:
    head, body, offset = struct.pack("<HHH", 0, 1, len(pictures)), b"", 6 + 16 * len(pictures) + len(gap)
    for picture in pictures:
        head += struct.pack("<BBBBHHII", 2, 2, 0, 0, 1, 32, len(picture), offset)
        body += picture
        offset += len(picture)
    return head + gap + body + tail


BITMAP = struct.pack("<IiiHHIIiiII", 40, 2, 4, 1, 32, 0, 0, 0, 0, 0, 0) + b"\0" * 24


def test_an_icon_of_plain_pictures_passes():
    assert publish.ico_problem(ico(png(), png())) is None
    assert publish.ico_problem((ROOT / "static" / "kolmail.ico").read_bytes()) is None  # the one that ships


def test_an_icon_cannot_carry_anything_but_its_pictures():
    assert "tEXt" in publish.ico_problem(ico(png(chunk(b"tEXt", b"Author\0someone"))))
    assert publish.ico_problem(ico(png(), tail=b"hidden")) and publish.ico_problem(ico(png(), gap=b"hidden"))
    assert publish.ico_problem(ico(b"GIF89a" + b"\0" * 60)) and publish.ico_problem(b"\0\0\x01\0")
    assert publish.ico_problem(ico(png())[:-5]) and publish.ico_problem(b"not an icon at all")
    # Found in review: a bitmap picture can carry anything behind its pixels, and so can the directory's spare fields
    assert "PNG" in publish.ico_problem(ico(png(), BITMAP + b"hidden text" * 20))
    spare = bytearray(ico(png()))
    spare[9] = 0x41
    assert publish.ico_problem(bytes(spare))
    planes = bytearray(ico(png()))
    planes[10:12] = b"hi"
    assert publish.ico_problem(bytes(planes))


def test_the_scan_reads_an_icon_and_still_refuses_other_binaries(tmp_path, monkeypatch):
    deny = tmp_path / "root" / "data" / "publish-denylist.txt"
    deny.parent.mkdir(parents=True)
    deny.write_text("secretword\n", encoding="utf-8")
    monkeypatch.setattr(publish, "ROOT", tmp_path / "root")
    (tmp_path / "good.ico").write_bytes(ico(png()))
    (tmp_path / "bad.ico").write_bytes(ico(png(), tail=b"x"))
    (tmp_path / "font.ttf").write_bytes(b"\0\1\0\0")
    (tmp_path / "note.md").write_text("a secretword and someone@" + "realmail.net\n", encoding="utf-8")
    hits = publish.scan(tmp_path, ["good.ico", "bad.ico", "font.ttf", "note.md"])
    assert not any(h.startswith("good.ico") for h in hits)
    assert any(h.startswith("bad.ico") for h in hits) and any(h.startswith("font.ttf") for h in hits)
    assert sum(h.startswith("note.md") for h in hits) == 2  # the denied word and the address
