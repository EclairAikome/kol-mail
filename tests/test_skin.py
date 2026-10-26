"""The page's look (skin.py): settings are cleaned, only real pictures become wallpapers, and the wallpaper is only
served to the page itself."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import skin  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 40
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 40


def test_settings_are_cleaned():
    s = skin.clean({"preset": "neon", "mode": "night", "show": 300, "focus_x": -5, "focus_y": "50", "glass": 1,
                    "auto_accent": True, "wallpaper": {"ext": "svg", "version": "x"}})
    assert s == {**skin.DEFAULTS, "show": 80, "focus_x": 0, "auto_accent": True}
    assert skin.clean({"preset": "ocean", "mode": "dark"})["preset"] == "ocean"


@pytest.mark.parametrize("data, kind", [(PNG, "png"), (JPG, "jpg"), (WEBP, "webp"), (b"<svg onload=x>", None),
                                        (b"GIF89a", None)])
def test_only_jpeg_png_and_webp_are_pictures(data, kind):
    assert skin.kind_of(data) == kind


def test_wallpaper_is_replaced_and_removed(tmp_path):
    s = skin.Skin(tmp_path)
    s.save({"preset": "sakura", "focus_x": 10})
    first = s.set_wallpaper(PNG)
    assert first["wallpaper"]["ext"] == "png" and first["preset"] == "sakura" and first["focus_x"] == 50
    second = s.set_wallpaper(JPG)
    assert [p.name for p in (tmp_path / "skin").iterdir()] == ["wallpaper.jpg"]
    assert second["wallpaper"]["version"] != first["wallpaper"]["version"]
    assert s.save({"wallpaper": None, "show": 60})["wallpaper"] == second["wallpaper"]  # only the upload changes it
    assert s.remove_wallpaper()["wallpaper"] is None and not list((tmp_path / "skin").iterdir())
    with pytest.raises(skin.SkinError):
        s.set_wallpaper(b"not a picture")
    with pytest.raises(skin.SkinError):
        s.set_wallpaper(PNG + b"\0" * skin.MAX_WALLPAPER)


def test_a_wallpaper_file_that_disappeared_is_forgotten(tmp_path):
    s = skin.Skin(tmp_path)
    s.set_wallpaper(PNG)
    (tmp_path / "skin" / "wallpaper.png").unlink()
    assert s.load()["wallpaper"] is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "looks", skin.Skin(tmp_path))
    return TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")


def local(**extra):
    return {"X-KOL-Mail": "1", "Origin": f"http://127.0.0.1:{app.PORT}", **extra}


def test_the_page_changes_its_look_and_wallpaper(client):
    assert client.get("/api/skin", headers=local()).json()["preset"] == "classic"
    assert client.post("/api/skin", headers=local(), json={"preset": "matcha", "mode": "dark"}).json()["mode"] == "dark"
    assert client.get("/skin/wallpaper").status_code == 404
    r = client.post("/api/skin/wallpaper", headers=local(**{"Content-Type": "application/octet-stream"}), content=WEBP)
    assert r.json()["wallpaper"]["ext"] == "webp"
    pic = client.get("/skin/wallpaper", headers={"Sec-Fetch-Site": "same-origin"})
    assert pic.status_code == 200 and pic.headers["content-type"] == "image/webp" and pic.content == WEBP
    assert pic.headers["x-content-type-options"] == "nosniff"
    bad = client.post("/api/skin/wallpaper", headers=local(), content=b"<html>")
    assert bad.status_code == 400
    assert client.post("/api/skin/wallpaper", headers=local(), content=b"").json()["wallpaper"] is None


def test_another_site_cannot_show_or_change_the_wallpaper(client):
    client.post("/api/skin/wallpaper", headers=local(), content=PNG)
    assert client.get("/skin/wallpaper", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    r = client.post("/api/skin/wallpaper", headers={"X-KOL-Mail": "1", "Origin": "https://evil.io"}, content=JPG)
    assert r.status_code == 403
