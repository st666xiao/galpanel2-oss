from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from galpanel2.core import store, repository as r, scanning as s, domain as d
from galpanel2.ui.theme import load_fonts, DAY, NIGHT
from galpanel2.ui.window import Window, CompareDialog
from galpanel2.ui.library import Editor, filter_counts
from galpanel2.ui.settings import Settings


@pytest.fixture(scope="session")
def app():
    application = QApplication.instance() or QApplication([])
    load_fonts()
    yield application


def wait(app, window):
    deadline = time.monotonic() + 20
    while window.pending_jobs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    app.processEvents()
    assert window.pending_jobs == 0
    assert window.fatal_error is None, window.fatal_error


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(tmp_path / "settings"))
    w = Window()
    monkeypatch.setattr(w, "notify", lambda text: setattr(w, "last_message", text))
    w.show()
    app.processEvents()
    yield w
    wait(app, w)
    w.close()
    app.processEvents()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "games"
    for name in ("First story", "Second story", "Hidden story"):
        p = root / name
        p.mkdir(parents=True)
        (p / "game.exe").write_bytes(b"game")
    (root / "Archive story.7z").write_bytes(b"archive")
    target = tmp_path / "library.galdb"
    with store.create_library(str(target)) as lib:
        sid = r.add_source(lib, str(root))
        s.commit_scan(lib, s.plan_scan(lib, sid, min_size_bytes=1))
        for w in r.list_works(lib):
            detail = r.get_work(lib, w.id)
            r.edit_work(lib, w.id, detail.revision, hidden=w.title == "Hidden story", developer="Dev" if w.title == "First story" else "",
                        notes="Searchable MEMO" if w.title == "Second story" else "")
        with lib.transaction() as c:
            c.execute("UPDATE works SET added_at=? WHERE title='First story'", ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),))
            c.execute("UPDATE copies SET availability='missing' WHERE work_id=(SELECT id FROM works WHERE title='Second story')")
    return target, root, sid


def test_startup_does_not_open_or_create_library(window, tmp_path):
    assert window.lib is None and window.screen == "title"
    assert not list(tmp_path.rglob("*.galdb"))
    # The old three recent-file slots are gone; the welcome page shows 「开始使用」.
    assert window.title_page.primary.text() == "开始使用"
    assert not window.title_page.message.isVisible()
    assert window.title_page.hint.isVisible()


def test_all_five_fonts_load(app):
    loaded = load_fonts()
    assert set(loaded) == {"SourceHanSerifCN-SemiBold.otf", "ResourceHanRoundedCN-Regular.ttf",
                           "ResourceHanRoundedCN-Bold.ttf", "JetBrainsMono-Regular.ttf", "JetBrainsMono-Italic.ttf"}


@pytest.mark.parametrize("theme", [DAY, NIGHT])
@pytest.mark.parametrize("screen", ["title", "library", "scan", "transfer"])
def test_each_screen_constructs_and_grabs(window, app, library, theme, screen):
    window.theme = theme
    window.apply_theme()
    if screen in ("library", "scan"):
        window.open_library_path(str(library[0]))
        wait(app, window)
        if screen == "scan":
            window.start_scan(library[2])
            wait(app, window)
    elif screen == "transfer":
        window.show_transfer()
    assert window.screen == screen
    window.resize(1440, 860)
    app.processEvents()
    shot = window.grab()
    assert (shot.width(), shot.height()) == (1440, 860)
    # 記録 is now a ⚙ menu action, not a header button; the logo carries the path.
    assert window.header.logo.toolTip() == (window.lib.path if window.lib else "")
    assert [a.text() for a in window.header.settings_menu.actions()][1] == "游玩记录"


def test_filters_search_sort_and_details(window, app, library):
    window.open_library_path(str(library[0]))
    wait(app, window)
    page = window.library_page
    assert page.counts == {"全部": 3, "最近": 2, "待整理": 3, "压缩包": 1, "不可访问": 1, "隐藏": 1}
    page.set_filter("隐藏")
    assert page.model.rowCount() == 1 and page.model.works[0].hidden
    page.set_filter("全部")
    page.search.setText("memo")
    assert [w.title for w in page.model.works] == ["Second story"]
    page.search.clear()
    page.order.setCurrentIndex(1)
    assert [w.title for w in page.model.works] == ["Archive story", "First story", "Second story"]
    assert all(not w.hidden for w in page.model.works)
    page.set_filter("不可访问")
    assert page.detail.aggregate == "missing"


def test_editor_conflict_reloads_without_overwriting(window, app, library):
    window.open_library_path(str(library[0]))
    wait(app, window)
    work = r.get_work(window.lib, r.list_works(window.lib)[0].id)
    editor = Editor(window, work)
    latest = r.edit_work(window.lib, work.id, work.revision, notes="Concurrent change")
    editor.notes.setPlainText("Stale edit")
    editor.save()
    wait(app, window)
    assert editor.message.text() == "这部作品刚被别处改过，已重新载入"
    assert editor.notes.toPlainText() == "Concurrent change"
    assert r.get_work(window.lib, work.id).revision == latest.revision
    editor.close()


def test_scan_discard_no_writes_and_title_decision(window, app, library, tmp_path):
    window.open_library_path(str(library[0]))
    wait(app, window)
    pending = tmp_path / "pending"
    (pending / "First story").mkdir(parents=True)
    with (pending / "First story/game.exe").open("wb") as f:
        f.truncate(21 * 1024 * 1024)
    sid = r.add_source(window.lib, str(pending))
    before = library[0].read_bytes()
    window.start_scan(sid)
    wait(app, window)
    page = window.scan_page
    assert page.plan.items[0].status == "same_name"
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert page.prepare_commit()[1][page.plan.items[0].key] == "skip"
    page.choose(page.plan.items[0].key, "new")
    page.table.item(0, 3).setText("User corrected title")
    plan, decisions = page.prepare_commit()
    assert plan.items[0].title == "User corrected title" and decisions[plan.items[0].key] == "new"
    page.discard()
    assert library[0].read_bytes() == before
    assert page.plan is None and window.screen == "library"


def test_settings_atomic_allowed_fields_and_recent_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(tmp_path / "settings"))
    settings = Settings()
    for i in range(10):
        settings.remember(f"C:\\selected\\library{i}.galdb")
    saved = json.loads(settings.path.read_text(encoding="utf-8"))
    assert set(saved) == {"recent", "theme", "show_mascot", "window_position", "native_frame", "cold_root"}
    assert saved["native_frame"] is False
    assert len(saved["recent"]) == 8
    assert [p.name for p in settings.path.parent.iterdir()] == ["settings.json"]
    settings.data["theme"] = "night"
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("injected replace failure")))
    before = settings.path.read_bytes()
    with pytest.raises(OSError):
        settings.save()
    assert settings.path.read_bytes() == before
    assert [p.name for p in settings.path.parent.iterdir()] == ["settings.json"]


def test_worker_keeps_event_loop_responsive_and_cancels(window, app):
    started = threading.Event()
    ticks = []
    def task(cancel):
        started.set()
        while not cancel():
            time.sleep(.01)
        raise d.Cancelled()
    window.run_job("test cancellable task", task, lambda _: None)
    deadline = time.monotonic() + 3
    while not started.is_set() and time.monotonic() < deadline:
        app.processEvents()
    assert started.is_set() and window.busy
    from PySide6.QtCore import QTimer
    QTimer.singleShot(10, lambda: ticks.append(1))
    until = time.monotonic() + .1
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(.005)
    assert ticks == [1]
    window.cancel_job()
    wait(app, window)
    assert not window.busy


def test_same_library_second_window_returns_to_title(window, app, library, tmp_path):
    window.open_library_path(str(library[0]))
    wait(app, window)
    other = Window()
    other.notify = lambda text: setattr(other, "last_message", text)
    other.open_library_path(str(library[0]))
    wait(app, other)
    assert other.screen == "title" and other.lib is None
    assert other.last_message == "这个库已经在另一个窗口里打开了"
    other.close()


def test_mascot_setting_keeps_dialogue(window, app):
    window.show_mascot = False
    window.apply_theme()
    window.show_transfer()
    app.processEvents()
    assert not window.transfer_page.mascot.isVisible()
    assert window.transfer_page.adv.isVisible()
    assert window.transfer_page.adv.text.startswith("嘿咻")


def test_compare_dialog_reads_two_libraries(window, app, library, tmp_path):
    b = tmp_path / "empty.galdb"
    store.create_library(str(b)).close()
    dialog = CompareDialog(window)
    dialog.paths = [str(library[0]), str(b)]
    before = library[0].read_bytes(), b.read_bytes()
    dialog.run()
    wait(app, window)
    assert "共同路径：0" in dialog.paths_text.toPlainText()
    assert before == (library[0].read_bytes(), b.read_bytes())
    dialog.close()


def test_transfer_background_inspect_import_and_open(window, app, tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE games(id INTEGER PRIMARY KEY,title TEXT,kind TEXT,path TEXT,notes TEXT,bindings TEXT)")
        c.execute("INSERT INTO games VALUES(1,'Legacy story','folder','D:\\fixture\\Story','a note','{}')")
    before = old.read_bytes()
    window.show_transfer()
    page = window.transfer_page
    page.inspect(str(old))
    wait(app, window)
    assert page.info.games == page.info.notes_nonempty == 1 and page.report is None
    target = tmp_path / "new.galdb"
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    page.transfer()
    wait(app, window)
    assert page.report.imported == 1 and page.report.conservation_errors == ()
    assert page.open_button.isEnabled() and not page.transfer_button.isEnabled()
    assert old.read_bytes() == before
    page.open_new()
    wait(app, window)
    assert window.screen == "library" and window.library_page.model.rowCount() == 1


def test_331_work_virtual_grid_scroll(window, app, tmp_path):
    root = tmp_path / "large fixture"
    root.mkdir()
    for i in range(331):
        (root / f"Story {i:03d}.zip").write_bytes(b"fixture")
    target = tmp_path / "331.galdb"
    with store.create_library(str(target)) as lib:
        sid = r.add_source(lib, str(root))
        s.commit_scan(lib, s.plan_scan(lib, sid))
    window.open_library_path(str(target))
    wait(app, window)
    page = window.library_page
    assert page.model.rowCount() == 331
    assert page.view.uniformItemSizes()
    scrollbar = page.view.verticalScrollBar()
    assert scrollbar.maximum() > 0
    timings = []
    for i in range(12):
        start = time.monotonic()
        scrollbar.setValue(scrollbar.maximum() * i // 11)
        app.processEvents()
        timings.append(time.monotonic() - start)
    assert max(timings) < 1.0
    assert len(page.cache.images) == 331
