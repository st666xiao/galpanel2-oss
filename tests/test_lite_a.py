"""lite-a regressions: startup, welcome page, header, frameless window (T5)."""
import json
import os
import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QObject
from PySide6.QtWidgets import QApplication, QAbstractButton, QCheckBox, QLabel, QMenu

from galpanel2.core import store
from galpanel2.ui.settings import Settings, default_library_path
from galpanel2.ui.theme import DAY, NIGHT, load_fonts, stylesheet
from galpanel2.ui.window import Window


@pytest.fixture(scope="session")
def app():
    application = QApplication.instance() or QApplication([])
    load_fonts()
    yield application


def wait(app, window):
    import time
    deadline = time.monotonic() + 20
    while window.pending_jobs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    for _ in range(5):
        app.processEvents()
    assert window.pending_jobs == 0
    assert window.fatal_error is None, window.fatal_error


def settings_dir(tmp_path, tag, **data):
    d = tmp_path / tag
    (d / "settings").mkdir(parents=True, exist_ok=True)
    (d / "settings" / "settings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return d


def new_window(app, monkeypatch, d, screenshot=False):
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(d / "settings"))
    w = Window(screenshot_mode=screenshot)
    w.notify = lambda text: setattr(w, "last_message", text)
    w.show()
    app.processEvents()
    return w


def make_library(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with store.create_library(str(path)):
        pass
    return str(path)


def button(parent, name):
    obj = parent.findChild(QAbstractButton, name)
    assert obj is not None, f"no button {name}"
    return obj


def label_of(b):
    return (b.accessibleName() or b.text() or "").strip()


# ------------------------------------------------------------------ T1 startup

def test_auto_open_remembered_library(app, tmp_path, monkeypatch):
    d = settings_dir(tmp_path, "recent", recent=[])
    lib = make_library(d / "library.galdb")
    (d / "settings" / "settings.json").write_text(json.dumps({"recent": [lib]}), encoding="utf-8")
    w = new_window(app, monkeypatch, d)
    try:
        assert w.lib is None and w.screen == "title", "constructing Window must not open a library"
        w.auto_open()
        wait(app, w)
        assert w.screen == "library" and w.lib is not None
        assert os.path.normcase(w.lib.path) == os.path.normcase(lib)
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_auto_open_missing_library_shows_welcome_message(app, tmp_path, monkeypatch):
    gone = str(tmp_path / "gone" / "old.galdb")
    d = settings_dir(tmp_path, "missing", recent=[gone])
    w = new_window(app, monkeypatch, d)
    try:
        w.auto_open()
        wait(app, w)
        assert w.screen == "title" and w.lib is None
        msg = w.title_page.findChild(QLabel, "welcomeMessage")
        assert msg.isVisible() and "找不到" in msg.text() and gone in msg.text()
        assert label_of(button(w.title_page, "welcomePrimary")) == "重新找到它…"
        assert button(w.title_page, "welcomeNew").isVisible()
        assert button(w.title_page, "welcomeOpen").isVisible()
        assert not w.title_page.hint.isVisible()
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_auto_open_no_recent_shows_start_button(app, tmp_path, monkeypatch):
    d = settings_dir(tmp_path, "empty", recent=[])
    w = new_window(app, monkeypatch, d)
    try:
        w.auto_open()
        wait(app, w)
        assert w.screen == "title" and w.lib is None
        assert label_of(button(w.title_page, "welcomePrimary")) == "开始使用"
        hint = w.title_page.findChild(QLabel, "welcomeHint")
        assert hint is not None and hint.isVisible() and "库放在" in hint.text()
        assert not button(w.title_page, "welcomeNew").isVisible()
        assert not w.title_page.message.isVisible()
        assert button(w.title_page, "welcomeImport").isVisible()
        assert not w.header.isVisible()
        assert w.title_page.welcome_controls.winClose.isVisible()
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_start_creates_default_library_and_remembers_it(app, tmp_path, monkeypatch):
    d = settings_dir(tmp_path, "create", recent=[])
    target = d / "deep" / "dir" / "library.galdb"
    monkeypatch.setenv("GALPANEL2_DEFAULT_LIBRARY", str(target))
    w = new_window(app, monkeypatch, d)
    try:
        w.auto_open()
        wait(app, w)
        button(w.title_page, "welcomePrimary").click()
        wait(app, w)
        assert target.is_file()
        assert w.screen == "library" and w.lib is not None
        assert os.path.normcase(w.lib.path) == os.path.normcase(str(target))
        saved = json.loads((d / "settings" / "settings.json").read_text(encoding="utf-8"))
        assert saved["recent"][0] == w.lib.path
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_start_opens_existing_default_without_creating(app, tmp_path, monkeypatch):
    d = settings_dir(tmp_path, "existing", recent=[])
    target = d / "lib" / "library.galdb"
    make_library(target)
    before = target.read_bytes()
    monkeypatch.setenv("GALPANEL2_DEFAULT_LIBRARY", str(target))
    w = new_window(app, monkeypatch, d)
    calls = []
    from galpanel2.ui import window as window_ui
    real = window_ui.store.create_library
    monkeypatch.setattr(window_ui.store, "create_library",
                        lambda path: calls.append(path) or real(path))
    try:
        w.auto_open()
        wait(app, w)
        button(w.title_page, "welcomePrimary").click()
        wait(app, w)
        assert calls == [], "an existing default library must never be recreated"
        assert w.screen == "library" and w.lib is not None
        assert target.read_bytes() == before
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_default_library_path_environment_wins(tmp_path, monkeypatch):
    d = tmp_path / "settings"
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(d))
    monkeypatch.delenv("GALPANEL2_DEFAULT_LIBRARY", raising=False)
    fallback = default_library_path()
    # On Windows with a real D:\ the documented default is the D: path;
    # elsewhere (or with D:\ patched away) it is the settings directory.
    if os.path.isdir("D:\\"):
        assert fallback == r"D:\GalPanel2\library.galdb"
    else:
        assert os.path.normcase(fallback) == os.path.normcase(str(d / "library.galdb"))
    override = str(tmp_path / "elsewhere" / "custom.galdb")
    monkeypatch.setenv("GALPANEL2_DEFAULT_LIBRARY", override)
    assert default_library_path() == override


def test_native_frame_setting_only_accepts_booleans(tmp_path, monkeypatch):
    d = settings_dir(tmp_path, "bools", recent=[], native_frame="yes")
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(d / "settings"))
    assert Settings().data["native_frame"] is False
    (d / "settings" / "settings.json").write_text(json.dumps({"native_frame": 1}), encoding="utf-8")
    assert Settings().data["native_frame"] is False
    (d / "settings" / "settings.json").write_text(json.dumps({"native_frame": True}), encoding="utf-8")
    assert Settings().data["native_frame"] is True


def test_screenshot_mode_does_not_write_settings(app, tmp_path, monkeypatch):
    d = settings_dir(tmp_path, "shot", recent=[])
    before = (d / "settings" / "settings.json").read_bytes()
    w = new_window(app, monkeypatch, d, screenshot=True)
    try:
        w.title_page.refresh()
        assert w.lib is None and w.screen == "title"
        dlg = w.make_settings_dialog()
        dlg.native_frame.setChecked(True)
        dlg.save()
        assert (d / "settings" / "settings.json").read_bytes() == before
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


# -------------------------------------------------------------------- T2 header

def open_library_window(app, tmp_path, monkeypatch, tag):
    d = settings_dir(tmp_path, tag, recent=[])
    lib = make_library(d / "library.galdb")
    (d / "settings" / "settings.json").write_text(json.dumps({"recent": [lib]}), encoding="utf-8")
    w = new_window(app, monkeypatch, d)
    w.auto_open()
    wait(app, w)
    assert w.screen == "library"
    return w, lib


def test_header_only_two_tabs_and_six_menu_actions(app, tmp_path, monkeypatch):
    w, lib = open_library_window(app, tmp_path, monkeypatch, "header")
    try:
        assert w.header.height() == 56
        assert label_of(button(w.header, "navLibrary")) == "游戏库"
        assert label_of(button(w.header, "navTidy")) == "整理"
        texts = {label_of(b) for b in w.header.findChildren(QAbstractButton) if b.isVisible()}
        assert not (texts & {"検収", "出所", "記録", "書架", "設定"})
        assert w.header.logo.toolTip() == w.lib.path
        assert isinstance(w.header.settings_menu, QMenu)
        assert [a.text() for a in w.header.settings_menu.actions()] == [
            "设定…", "游玩记录", "换库…", "新建库…", "从旧版导入…", "两库只读对比"]
        assert button(w.header, "addButton").accessibleName() == "添加"
        assert button(w.header, "themeButton").accessibleName() == "主题"
        assert button(w.header, "menuButton").accessibleName() == "菜单"
        w.header.navTidy.click()
        wait(app, w)
        assert w.screen == "tidy" and w.header.navTidy.selected and not w.header.navLibrary.selected
        w.header.navLibrary.click()
        wait(app, w)
        assert w.screen == "library" and w.header.navLibrary.selected
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_add_button_routes_to_sources_or_pending_scan(app, tmp_path, monkeypatch):
    w, _ = open_library_window(app, tmp_path, monkeypatch, "add")
    try:
        add = button(w.header, "addButton")
        add.click()
        wait(app, w)
        assert w.screen == "source"
        w.show_library()
        w.scan_page.plan = object()
        w.header.set_page("library")
        assert w.header.addButton.dot
        add.click()
        wait(app, w)
        assert w.screen == "scan"
        w.scan_page.plan = None
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_chrome_removed_and_path_in_tooltip(app, tmp_path, monkeypatch):
    w, lib = open_library_window(app, tmp_path, monkeypatch, "chrome")
    try:
        bad = [l.text() for l in w.findChildren(QLabel)
               if l.isVisible() and l.text().startswith("GalPanel 2  ·")]
        assert not bad
        assert getattr(w, "chrome", None) is None
        assert w.header.logo.toolTip() == w.lib.path
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


# --------------------------------------------------------------------- T3 frame

def test_frameless_default_and_window_buttons(app, tmp_path, monkeypatch):
    from galpanel2.ui.frame import EdgeGrip
    w, _ = open_library_window(app, tmp_path, monkeypatch, "frameless")
    try:
        assert w.windowFlags() & Qt.WindowType.FramelessWindowHint
        for name in ("winMin", "winMax", "winClose"):
            assert button(w.header, name).isVisible()
        assert w.frame_helper is not None
        grips = w.findChildren(EdgeGrip)
        assert len(grips) == 8
        assert all(g.isVisible() for g in grips)
        assert all(g.parent() is w for g in grips)
        max_button = button(w.header, "winMax")
        max_button.click()
        app.processEvents()
        assert w.windowState() & Qt.WindowState.WindowMaximized
        assert all(not g.isVisible() for g in grips)
        assert max_button.text() == "❐" and max_button.accessibleName() == "还原"
        max_button.click()
        app.processEvents()
        assert not (w.windowState() & Qt.WindowState.WindowMaximized)
        assert all(g.isVisible() for g in grips)
        assert max_button.text() == "☐" and max_button.accessibleName() == "最大化"
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_no_frame_event_filter_on_application(app, tmp_path, monkeypatch):
    """fix-1 R1: the frameless shell must not install an app-wide filter."""
    from galpanel2.ui.frame import EdgeGrip
    from galpanel2.ui import frame as frame_mod
    source = Path(frame_mod.__file__).read_text(encoding="utf-8")
    assert "installEventFilter" not in source
    w, _ = open_library_window(app, tmp_path, monkeypatch, "no-app-filter")
    try:
        assert len(w.findChildren(EdgeGrip)) == 8
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def test_native_frame_hides_window_buttons_and_saves_checkbox(app, tmp_path, monkeypatch):
    from galpanel2.ui.frame import EdgeGrip
    d = settings_dir(tmp_path, "native", recent=[], native_frame=True)
    w = new_window(app, monkeypatch, d)
    try:
        assert not (w.windowFlags() & Qt.WindowType.FramelessWindowHint)
        assert w.frame_helper is None
        assert not w.findChildren(EdgeGrip)
        for name in ("winMin", "winMax", "winClose"):
            b = w.header.findChild(QObject, name)
            assert b is None or not b.isVisible()
        dlg = w.make_settings_dialog()
        boxes = [c for c in dlg.findChildren(QCheckBox)
                 if c.text() == "使用系统标题栏（重启后生效）"]
        assert len(boxes) == 1 and boxes[0].isChecked()
        boxes[0].setChecked(False)
        dlg.save()
        saved = json.loads((d / "settings" / "settings.json").read_text(encoding="utf-8"))
        assert saved["native_frame"] is False
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


# --------------------------------------------------------------------- T4 styles

def test_stylesheet_widgets_and_icons_exist():
    for theme in (DAY, NIGHT):
        css = stylesheet(theme)
        for selector in ("QComboBox::drop-down", "QComboBox::down-arrow", "QComboBox QAbstractItemView",
                         "QCheckBox::indicator", "QCheckBox::indicator:checked", "QMenu::item:selected",
                         "QPushButton", "QMessageBox", "QPushButton:default"):
            assert selector in css, f"{theme.name} missing {selector}"
        for url in re.findall(r"url\(([^)]+)\)", css):
            assert Path(url.strip("'\"")).is_file(), url


# --------------------------------------------------------------- T1 transfer

def test_transfer_back_goes_to_library_or_welcome(app, tmp_path, monkeypatch):
    w, _ = open_library_window(app, tmp_path, monkeypatch, "transfer")
    try:
        w.show_transfer()
        wait(app, w)
        backs = [b for b in w.transfer_page.findChildren(QAbstractButton)
                 if label_of(b) == "返回" and b.isVisible()]
        assert backs
        backs[0].click()
        wait(app, w)
        assert w.screen == "library" and w.lib is not None
        w.show_title()
        wait(app, w)
        w.show_transfer()
        wait(app, w)
        backs = [b for b in w.transfer_page.findChildren(QAbstractButton)
                 if label_of(b) == "返回" and b.isVisible()]
        backs[0].click()
        wait(app, w)
        assert w.screen == "title" and w.lib is None
    finally:
        wait(app, w)
        w.close()
        app.processEvents()
