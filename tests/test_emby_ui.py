"""emby UI regressions: media theme, sidebar, poster cards, full-page detail."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QComboBox, QStackedWidget, QWidget

from galpanel2.core import shelf, store
from galpanel2.ui import library as L
from galpanel2.ui.settings import Settings
from galpanel2.ui.theme import MEDIA, THEMES, font
from galpanel2.ui.window import Window
from test_ui import app, wait, window  # noqa: F401  (shared fixtures)

ROOT = Path(__file__).resolve().parents[1]


def _make_lib(tmp_path):
    path = tmp_path / "emby.galdb"
    with store.create_library(str(path)) as lib:
        with lib.transaction() as c:
            for wid, title, hidden in ((1, "Alpha", 0), (2, "Bravo", 0), (3, "Charlie", 0)):
                c.execute("INSERT INTO works(id,uuid,title,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?)",
                          (wid, str(uuid.uuid4()), title, hidden, "2020-01-01", "2020-01-01"))
                p = tmp_path / f"game-{wid}"
                p.mkdir(exist_ok=True)
                (p / "game.exe").write_bytes(b"MZ")
                c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,origin,added_at) "
                          "VALUES(?,NULL,'folder',?,?,?,'scan','2020-01-01')",
                          (wid, str(p), str(p).casefold(), "game.exe"))
        shelf.set_category(lib, 1, "日系")
        shelf.set_status(lib, 1, "在玩")
        shelf.set_status(lib, 2, "想玩")
    return path


def _opened(window, app, path):
    window.open_library_path(str(path))
    wait(app, window)
    window.show_library()
    wait(app, window)
    window.resize(1440, 860)
    app.processEvents()
    return window.library_page


# ------------------------------------------------------------------ E2 core
def test_default_theme_is_media(tmp_path, monkeypatch):
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(tmp_path / "settings"))
    assert Settings().data["theme"] == "media"
    assert THEMES == {"day": THEMES["day"], "night": THEMES["night"], "media": MEDIA}
    assert MEDIA.name == "media" and MEDIA.roles["on_accent"] == "#14110B"


def test_toggle_theme_cycles_media_day_night(window, app):
    assert window.theme.name == "media"
    names = []
    for _ in range(3):
        window.toggle_theme()
        names.append(window.theme.name)
    assert names == ["day", "night", "media"]


def test_media_title_font_is_body_font(window, app):
    assert window.theme.name == "media"
    assert font("title", 20).family() == font("body", 20).family()


def test_poster_mode_function():
    assert L.poster_mode(600, 900) == "cover"
    assert L.poster_mode(55, 100) == "cover"
    assert L.poster_mode(85, 100) == "cover"
    assert L.poster_mode(1920, 1080) == "contain"
    assert L.poster_mode(500, 500) == "contain"
    assert L.poster_mode(86, 100) == "contain"
    assert L.poster_mode(0, 100) == "contain"
    assert L.poster_mode(100, 0) == "contain"


def test_file_tag_function(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    by = {w.id: w for w in page.works}
    assert L.file_tag(by[1]) == ""
    with window.lib.transaction() as c:
        c.execute("UPDATE copies SET availability='missing' WHERE work_id=2")
    window.reload_library()
    wait(app, window)
    by = {w.id: w for w in page.works}
    assert L.file_tag(by[2]) == "已失效"


def test_ambient_color_function():
    assert L.ambient_color(QImage()) is None
    img = QImage(60, 90, QImage.Format_RGB32)
    img.fill(QColor("#C03060"))
    assert L.ambient_color(img).name().lower() == "#c03060"
    bw = QImage(2, 1, QImage.Format_RGB32)
    bw.setPixelColor(0, 0, QColor("#000000"))
    bw.setPixelColor(1, 0, QColor("#FFFFFF"))
    c = L.ambient_color(bw)
    assert 90 <= c.red() <= 165


def test_sidebar_counts_and_filters(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    side = page.findChild(QWidget, "wallSidebar")
    assert side is page.sidebar and abs(side.width() - 200) <= 2
    assert list(page.category_buttons) == ["全部", "日系", "同人", "欧美", "其他", "未分类"]
    assert list(page.status_buttons) == ["想玩", "在玩", "玩过", "不玩了", "未标记"]
    assert page.category_of(1) == "日系"
    assert page.status_of(1) == "在玩"
    page.set_category_filter("日系")
    assert sorted(w.id for w in page.model.works) == [1]
    page.set_category_filter("全部")
    page.set_status_filter("想玩")
    assert sorted(w.id for w in page.model.works) == [2]


def test_status_button_click_twice_clears(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    QTest.mouseClick(page.status_buttons["想玩"], Qt.MouseButton.LeftButton)
    assert page.status_name == "想玩"
    QTest.mouseClick(page.status_buttons["想玩"], Qt.MouseButton.LeftButton)
    assert page.status_name == "全部"


# ------------------------------------------------------------------ E3 detail
def test_wall_is_stacked_and_full_width(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    assert isinstance(page.pages, QStackedWidget)
    assert page.pages.objectName() == "libraryPages"
    assert page.pages.currentIndex() == 0 and page.pages.widget(1) is page.detail
    assert not page.detail_open() and not page.detail.isVisible()
    assert page.view.viewport().width() >= 1100
    assert page.view.itemDelegate().card_width >= 176


def test_open_and_close_detail_keeps_selection(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    page.select_work(2)
    assert not page.detail_open()
    page.open_detail()
    assert page.detail_open() and page.detail.work.id == 2
    page.close_detail()
    assert not page.detail_open()
    cur = page.view.currentIndex().data(Qt.ItemDataRole.UserRole)
    assert cur is not None and cur.id == 2
    page.open_detail(3)
    assert page.detail.work.id == 3


def test_click_and_back_and_escape(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    index = page.model.index(0, 0)
    page.view.scrollTo(index)
    app.processEvents()
    art = page.view.itemDelegate()._art_rect(page.view.visualRect(index))
    from PySide6.QtCore import QPoint
    spot = QPoint(int(art.center().x()), int(art.bottom() - 40))
    QTest.mouseClick(page.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, spot)
    assert page.detail_open()
    back = page.detail.findChild(QAbstractButton, "detailBack")
    assert back is not None and "返回" in back.text()
    QTest.mouseClick(back, Qt.MouseButton.LeftButton)
    assert not page.detail_open()
    page.open_detail(1)
    QTest.keyClick(page.detail, Qt.Key.Key_Escape)
    assert not page.detail_open()


def test_detail_buttons_and_controls(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    page.open_detail(1)
    app.processEvents()
    d = page.detail
    want = {"detailStart": "开始游戏", "detailOpen": "打开文件夹", "detailFind": "找资料",
            "detailEdit": "修改资料", "detailMore": "更多"}
    for name, text in want.items():
        b = d.findChild(QAbstractButton, name)
        assert b is not None and text in b.text(), name
    assert d.start_button.height() >= 44 and d.start_button.width() >= 180
    assert list(d.status_buttons) == list(shelf.STATUSES)
    combo = d.findChild(QComboBox, "detailCategory")
    assert combo is d.category_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["未分类", *shelf.CATEGORIES]
    assert combo.currentText() == "日系"
    assert d.summary.max_lines >= 12
    assert d.hero.width() >= 280 and d.hero.height() >= 420


def test_edit_status_writes_db_and_stays_open(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    page.open_detail(1)
    app.processEvents()
    d = page.detail
    QTest.mouseClick(d.status_buttons["玩过"], Qt.MouseButton.LeftButton)
    wait(app, window)
    assert shelf.list_shelf(window.lib)[1].status == "玩过"
    assert shelf.list_shelf(window.lib)[1].category == "日系"
    assert page.detail_open() and d.work.id == 1
    assert d.status_buttons["玩过"].isChecked()
    QTest.mouseClick(d.status_buttons["玩过"], Qt.MouseButton.LeftButton)
    wait(app, window)
    assert shelf.list_shelf(window.lib).get(1, shelf.Shelf(None, None)).status is None
    assert not any(b.isChecked() for b in d.status_buttons.values())


def test_edit_category_writes_db(window, app, tmp_path):
    page = _opened(window, app, _make_lib(tmp_path))
    page.open_detail(3)
    d = page.detail
    assert d.category_combo.currentText() == "未分类"
    d.set_category("欧美")
    wait(app, window)
    assert shelf.list_shelf(window.lib)[3].category == "欧美"
    assert d.category_combo.currentText() == "欧美"
    d.category_combo.setCurrentIndex(d.category_combo.findText("同人"))
    d.category_combo.activated.emit(d.category_combo.currentIndex())
    wait(app, window)
    assert shelf.list_shelf(window.lib)[3].category == "同人"
    before = dict(shelf.list_shelf(window.lib))
    page.open_detail(1)
    assert dict(shelf.list_shelf(window.lib)) == before, "switching works must not write"


def test_cli_detail_screenshot(tmp_path):
    path = _make_lib(tmp_path)
    settings = tmp_path / "settings"
    settings.mkdir(exist_ok=True)
    (settings / "settings.json").write_text(json.dumps({"recent": []}), encoding="utf-8")
    out = tmp_path / "detail.png"
    env = dict(os.environ, GALPANEL2_SETTINGS_DIR=str(settings), QT_QPA_PLATFORM="offscreen",
               PYTHONUTF8="1", PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "galpanel2", "--screen", "detail", "--library", str(path),
                        "--select", "1", "--screenshot", str(out)], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0 and out.is_file(), (r.stderr or r.stdout)[-600:]
    img = QImage(str(out))
    assert (img.width(), img.height()) == (1440, 860)
    r2 = subprocess.run([sys.executable, "-m", "galpanel2", "--screen", "detail", "--library", str(path),
                         "--screenshot", str(tmp_path / "x.png")], cwd=ROOT, env=env,
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r2.returncode != 0, "--screen detail without --select must fail"


def test_detail_with_cover_paints(window, app, tmp_path):
    from galpanel2.core import repository as repo
    page = _opened(window, app, _make_lib(tmp_path))
    window.theme = MEDIA
    window.apply_theme()
    cover = tmp_path / "cover.png"
    img = QImage(300, 450, QImage.Format_RGB32)
    img.fill(QColor("#3366AA"))
    assert img.save(str(cover), "PNG")
    repo.set_cover(window.lib, 1, repo.get_work(window.lib, 1).revision, str(cover))
    window.reload_library()
    wait(app, window)
    page.open_detail(1)
    app.processEvents()
    assert page.detail.work.id == 1 and page.detail.work.cover_path is not None
    shot = page.detail.grab()
    assert not shot.isNull() and shot.width() > 0 and shot.height() > 0


def test_settings_save_keeps_media_theme(window, app, tmp_path):
    from galpanel2.ui.externaldialogs import SettingsDialog
    window.theme = MEDIA
    window.apply_theme()
    dialog = SettingsDialog(window)
    assert dialog.theme.currentData() == "media"
    assert str(window.settings.path).startswith(str(tmp_path)), "settings must go to the temp dir"
    dialog.save()
    assert window.theme.name == "media"
