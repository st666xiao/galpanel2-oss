"""Fix-1 regressions: real UI/task transitions and temporary filesystem fixtures."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QFontInfo
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from galpanel2.core import domain as d, store, repository as repo, scanning, legacy, compare
from galpanel2.ui import window as window_ui, pages as pages_ui
from galpanel2.ui.library import Editor
from galpanel2.ui.settings import Settings
from galpanel2.ui.theme import load_fonts, font, FAMILIES
from galpanel2.ui.window import Window, CompareDialog
from galpanel2.ui.workers import Job


@pytest.fixture(scope="session")
def app():
    application = QApplication.instance() or QApplication([])
    load_fonts()
    yield application


def until(app, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    app.processEvents()
    assert predicate(), "background operation did not finish"


def wait(app, window):
    until(app, lambda: not window.pending_jobs)
    assert window.fatal_error is None, window.fatal_error


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(tmp_path / "settings"))
    w = Window()
    monkeypatch.setattr(w, "notify", lambda message: setattr(w, "last_message", message))
    w.show()
    app.processEvents()
    yield w
    wait(app, w)
    w.close()
    app.processEvents()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "games"
    root.mkdir()
    (root / "Existing story.zip").write_bytes(b"fixture")
    path = tmp_path / "library.galdb"
    with store.create_library(str(path)) as lib:
        sid = repo.add_source(lib, str(root))
        scanning.commit_scan(lib, scanning.plan_scan(lib, sid))
    return path, root, sid


def opened(app, window, library):
    window.open_library_path(str(library[0]))
    wait(app, window)
    return repo.list_works(window.lib)[0].id


def pending_scan(app, window, library):
    (library[1] / "New story.zip").write_bytes(b"fixture")
    window.start_scan(library[2])
    wait(app, window)
    return window.scan_page


def test_r1_reload_after_commit_not_cancellable(window, app, library, monkeypatch):
    opened(app, window, library)
    page = pending_scan(app, window, library)
    entered, release = threading.Event(), threading.Event()
    projection = window_ui.load_projection
    def slow_projection(lib, cancel):
        entered.set()
        assert release.wait(10)
        return projection(lib, cancel)
    monkeypatch.setattr(window_ui, "load_projection", slow_projection)
    page.commit()
    try:
        until(app, entered.is_set)
        assert window.busy and window.screen == "library"
        assert not window.status.cancel.isVisible() and not window.status.cancel.isEnabled()
        job = window.active_job
        window.status.cancel.click()
        window.cancel_job()
        assert not job.cancel_event.is_set()
    finally:
        release.set()
        wait(app, window)
    assert "New story" in [w.title for w in window.library_page.model.works]


def test_r1_cancel_message_never_says_not_committed_after_commit(window, app, library, monkeypatch):
    wid = opened(app, window, library)
    removed = library[1] / "Removed story.zip"
    removed.write_bytes(b"fixture")
    scanning.commit_scan(window.lib, scanning.plan_scan(window.lib, library[2]))
    removed.unlink()
    extra = library[1] / "another copy"
    extra.mkdir()
    (extra / "Existing story.zip").write_bytes(b"fixture")
    page = pending_scan(app, window, library)
    same = next(it for it in page.plan.items if it.status == "same_name")
    page.choose(same.key, wid)
    entered, release = threading.Event(), threading.Event()
    projection = window_ui.load_projection
    def slow_projection(lib, cancel):
        entered.set()
        assert release.wait(10)
        return projection(lib, cancel)
    monkeypatch.setattr(window_ui, "load_projection", slow_projection)
    page.commit()
    expected = "已入库：新作品 1 部，挂接 1 份，确认 1 份，标记不在 1 份"
    try:
        until(app, entered.is_set)
        assert expected in window.status.message.text()
        window.cancel_job()
        assert "未提交" not in window.status.message.text()
    finally:
        release.set()
        wait(app, window)
    assert window.status.message.text() == expected
    assert any(w.title == "New story" for w in repo.list_works(window.lib))


def test_r2_second_save_after_conflict_dialog_closable(window, app, library, monkeypatch):
    wid = opened(app, window, library)
    editor = Editor(window, repo.get_work(window.lib, wid))
    editor.show()
    reloads = []
    reload_library = window.reload_library
    def track_reload(*args, **kwargs):
        reloads.append(editor.isVisible())
        return reload_library(*args, **kwargs)
    monkeypatch.setattr(window, "reload_library", track_reload)
    latest = repo.edit_work(window.lib, wid, editor.work.revision, notes="Concurrent change")
    editor.notes.setPlainText("Stale note")
    editor.save()
    wait(app, window)
    assert editor.message.text() == "这部作品刚被别处改过，已重新载入"
    assert editor.work.revision == latest.revision and editor.notes.toPlainText() == "Concurrent change"
    assert not window.busy and not editor.saving and not reloads
    editor.notes.setPlainText("Retry using latest revision")
    editor.save()
    wait(app, window)
    assert repo.get_work(window.lib, wid).notes == "Retry using latest revision"
    assert not editor.saving and not editor.isVisible()
    assert reloads == [False]
    editor.close()
    assert not editor.isVisible()


def test_r2_save_while_busy_keeps_dialog_closable(window, app, library):
    wid = opened(app, window, library)
    editor = Editor(window, repo.get_work(window.lib, wid))
    editor.show()
    before = library[0].read_bytes()
    release = threading.Event()
    window.run_job("Temporary background task", lambda cancel: release.wait(10), lambda _: None)
    try:
        editor.save()
        assert editor.message.text() == "后台任务还在进行，请稍后再保存"
        assert not editor.saving and editor.save_button.isEnabled() and editor.cancel_button.isEnabled()
        editor.close()
        assert not editor.isVisible()
    finally:
        release.set()
        wait(app, window)
    assert library[0].read_bytes() == before


def test_r3_commit_returns_to_library(window, app, library):
    opened(app, window, library)
    page = pending_scan(app, window, library)
    assert window.screen == "scan"
    page.commit()
    wait(app, window)
    assert window.screen == "library"
    assert "New story" in [w.title for w in window.library_page.model.works]


@pytest.mark.parametrize("depth", [0, 2, 6])
def test_r4_generic_subdir_exe_at_max_depth(tmp_path, depth):
    root = tmp_path / "root"
    game = root.joinpath(*[f"level{i}" for i in range(depth)])
    (game / "bin").mkdir(parents=True)
    (game / "bin/play.exe").write_bytes(b"fixture")
    if depth:
        too_deep = root.joinpath(*[f"other{i}" for i in range(depth + 1)])
        (too_deep / "bin").mkdir(parents=True)
        (too_deep / "bin/deep.exe").write_bytes(b"fixture")
    with store.create_library(str(tmp_path / "depth.galdb")) as lib:
        sid = repo.add_source(lib, str(root))
        p = scanning.plan_scan(lib, sid, max_depth=depth, min_size_bytes=1)
        assert len(p.items) == 1
        assert p.items[0].key == d.path_key(str(game))
        assert p.items[0].status == "new" and p.items[0].exe_relpath == "bin\\play.exe"


def test_r5_known_overrides_too_small(tmp_path):
    root = tmp_path / "root"
    game = root / "Small story"
    game.mkdir(parents=True)
    (game / "game.exe").write_bytes(b"fixture")
    path = tmp_path / "small.galdb"
    with store.create_library(str(path)) as lib:
        sid = repo.add_source(lib, str(root))
        scanning.commit_scan(lib, scanning.plan_scan(lib, sid, min_size_bytes=1))
        cid = repo.get_work(lib, repo.list_works(lib)[0].id).copies[0].id
        with lib.transaction() as c:
            c.execute("UPDATE copies SET availability='missing',checked_at=NULL,size_bytes=999,exe_relpath='old.exe'")
        (game / "inside.zip").write_bytes(b"not another discovery")
        before = path.read_bytes()
        plan = scanning.plan_scan(lib, sid, min_size_bytes=10**12)
        assert path.read_bytes() == before and len(plan.items) == 1
        item = plan.items[0]
        assert (item.status, item.copy_id, item.reason) == ("known", cid, "")
        result = scanning.commit_scan(lib, plan)
        cp = repo.get_work(lib, repo.list_works(lib)[0].id).copies[0]
        assert result.marked_known == 1 and result.marked_missing == 0
        assert cp.availability == "ok" and cp.checked_at and cp.size_bytes == item.size_bytes
        assert cp.exe_relpath == "game.exe"


def test_r5_known_overrides_skip_mark(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    arc = root / "Foo_patch.zip"
    arc.write_bytes(b"fixture")
    with store.create_library(str(tmp_path / "archive.galdb")) as lib:
        sid = repo.add_source(lib, str(root))
        key = d.path_key(str(arc))
        item = d.PlanItem(key, str(arc), "archive", "Foo", "", None, 7, "new", (), None, "")
        scanning.commit_scan(lib, d.ScanPlan(sid, str(root), d.utc_now(), (item,), ()))
        wid = repo.list_works(lib)[0].id
        cid = repo.get_work(lib, wid).copies[0].id
        with lib.transaction() as c:
            c.execute("UPDATE copies SET availability='missing',checked_at=NULL,size_bytes=999,exe_relpath='old.exe'")
        (root / "Bar_patch.zip").write_bytes(b"fixture")
        plan = scanning.plan_scan(lib, sid)
        found = next(i for i in plan.items if i.key == key)
        skipped = next(i for i in plan.items if i.key != key)
        assert (found.status, found.copy_id, found.reason) == ("known", cid, "")
        assert skipped.status == "skipped" and skipped.reason == "skip_mark"
        result = scanning.commit_scan(lib, plan)
        cp = repo.get_work(lib, wid).copies[0]
        assert result.marked_known == 1 and result.added_works == 0
        assert cp.availability == "ok" and cp.checked_at and cp.size_bytes == arc.stat().st_size
        assert cp.exe_relpath is None


def test_r6_same_name_needs_explicit_choice(window, app, library, tmp_path):
    wid = opened(app, window, library)
    root = tmp_path / "incoming"
    root.mkdir()
    (root / "Existing story.zip").write_bytes(b"fixture")
    (root / "New pending.zip").write_bytes(b"fixture")
    sid = repo.add_source(window.lib, str(root))
    before = library[0].read_bytes()
    window.start_scan(sid)
    wait(app, window)
    page = window.scan_page
    row, item = next((i, it) for i, it in enumerate(page.plan.items) if it.status == "same_name")
    check = page.table.item(row, 0)
    assert not (check.flags() & Qt.ItemFlag.ItemIsEnabled)
    assert check.toolTip() == "先在右侧选择怎么处理"
    QTest.mouseClick(page.table.viewport(), Qt.MouseButton.LeftButton, pos=page.table.visualItemRect(check).center())
    assert check.checkState() == Qt.CheckState.Unchecked
    assert page.prepare_commit()[1][item.key] == "skip" and page.commit_button.text() == "入库 1 部"
    page.choose(item.key, "new")
    assert check.flags() & Qt.ItemFlag.ItemIsEnabled
    assert check.checkState() == Qt.CheckState.Checked
    assert page.prepare_commit()[1][item.key] == "new" and page.commit_button.text() == "入库 2 部"
    check.setCheckState(Qt.CheckState.Unchecked)
    assert page.prepare_commit()[1][item.key] == "skip" and page.commit_button.text() == "入库 1 部"
    page.choose(item.key, wid)
    assert check.checkState() == Qt.CheckState.Checked and page.prepare_commit()[1][item.key] == wid
    page.choose(item.key, "skip")
    assert check.checkState() == Qt.CheckState.Unchecked and not (check.flags() & Qt.ItemFlag.ItemIsEnabled)
    assert page.commit_button.text() == "入库 1 部" and library[0].read_bytes() == before
    page.choose(item.key, "new")
    page.commit()
    wait(app, window)
    works = repo.list_works(window.lib)
    assert len(works) == 3 and sum(w.title == "Existing story" for w in works) == 2


def test_r7_welcome_uses_recent_without_async_checks(app, tmp_path, monkeypatch):
    """The old title page probed three recent slots in a background job.
    The lite welcome page has no slots: it reads recent[0] synchronously."""
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(tmp_path / "settings"))
    existing = tmp_path / "existing.galdb"
    existing.write_bytes(b"file existence fixture, never opened")
    settings = Settings()
    settings.remember(str(existing))
    real = os.path.isfile
    calls = []
    def counted_isfile(path):
        calls.append((path, threading.get_ident()))
        return real(path)
    monkeypatch.setattr(pages_ui.os.path, "isfile", counted_isfile)
    start = time.monotonic()
    w = Window(screenshot_mode=True)
    w.show()
    app.processEvents()
    try:
        assert time.monotonic() - start < 1
        assert not w.busy and w.pending_jobs == 0
        assert w.title_page.state == "empty"
        assert not w.title_page.message.isVisible()
        assert w.title_page.primary.text() == "开始使用"
        w.title_page.refresh()
        assert all(t == threading.get_ident() for _, t in calls)
        assert w.lib is None and existing.read_bytes() == b"file existence fixture, never opened"
    finally:
        wait(app, w)
        w.close()
        app.processEvents()


def old_fixture(tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE games(id INTEGER PRIMARY KEY,title TEXT,kind TEXT,path TEXT,developer TEXT,notes TEXT)")
        c.execute("INSERT INTO games VALUES(1,'Legacy story','folder',?,'','')", (str(tmp_path / "legacy game"),))
    return old


def test_r8_leftover_covers_dir_message(window, app, tmp_path, monkeypatch):
    old = old_fixture(tmp_path)
    before = old.read_bytes()
    target = tmp_path / "leftover.galdb"
    covers = Path(str(target) + ".covers")
    covers.mkdir()
    sentinel = covers / "keep.txt"
    sentinel.write_text("never remove", encoding="utf-8")
    expected = (f"{covers} 已存在，但库文件不在；这可能是上次没完成的导入留下的。"
                "请换一个保存位置，或自己确认后移走这个目录。")
    with pytest.raises(d.TargetExists) as caught:
        legacy.import_legacy(str(old), str(target))
    assert str(caught.value) == expected
    window.show_transfer()
    page = window.transfer_page
    page.inspect(str(old))
    wait(app, window)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    page.transfer()
    wait(app, window)
    assert page.details.text() == expected and expected in page.adv.text
    assert window.status.message.text() == expected
    assert not target.exists() and not Path(str(target) + ".partial").exists()
    assert sentinel.read_text(encoding="utf-8") == "never remove" and old.read_bytes() == before


def test_r9_font_weights(app):
    registered = load_fonts()
    normal = QFontInfo(font("body", 13))
    bold = QFontInfo(font("body", 13, bold=True))
    assert normal.family() == registered["ResourceHanRoundedCN-Regular.ttf"] == FAMILIES["body"]
    assert bold.family() == registered["ResourceHanRoundedCN-Bold.ttf"] == FAMILIES["body_bold"]
    assert normal.weight() == QFont.Weight.Normal and bold.weight() == QFont.Weight.Bold
    assert normal.styleName() == "Regular" and bold.styleName() == "Bold"
    assert "Regular" in QFontDatabase.styles(normal.family()) and "Bold" in QFontDatabase.styles(bold.family())


def test_r10_all_error_capsule(window, app, library):
    wid = opened(app, window, library)
    extra = library[1] / "Another copy.zip"
    extra.write_bytes(b"fixture")
    plan = scanning.plan_scan(window.lib, library[2])
    scanning.commit_scan(window.lib, plan, {d.path_key(str(extra)): wid})
    with window.lib.transaction() as c:
        c.execute("UPDATE copies SET availability='error'")
    window.reload_library(wid)
    wait(app, window)
    assert repo.list_works(window.lib)[0].availability == "unknown"
    detail = window.library_page.detail
    assert detail.availability_text() == "检查出错"
    assert "检查错误" in detail.path.toolTip() and "未检查" not in detail.path.toolTip()
    with window.lib.transaction() as c:
        c.execute("UPDATE copies SET availability='unknown' WHERE id=(SELECT min(id) FROM copies)")
    window.reload_library(wid)
    wait(app, window)
    detail = window.library_page.detail
    assert detail.availability_text() == "未检查"
    assert "未检查" in detail.path.toolTip() and "检查错误" not in detail.path.toolTip()


@pytest.mark.parametrize("operation", ["check", "import", "scan", "inspect", "compare"])
def test_cancel_messages_follow_actual_operation(window, app, library, tmp_path, monkeypatch, operation):
    opened(app, window, library)
    def cancelled(*a, **k):
        raise d.Cancelled("cancelled fixture")
    if operation == "check":
        monkeypatch.setattr(scanning, "check_availability", cancelled)
        trigger = window.check_files
    elif operation == "scan":
        monkeypatch.setattr(scanning, "plan_scan", cancelled)
        trigger = lambda: window.start_scan(library[2])
    elif operation in ("import", "inspect"):
        old = old_fixture(tmp_path)
        window.show_transfer()
        page = window.transfer_page
        if operation == "import":
            page.inspect(str(old))
            wait(app, window)
            target = tmp_path / "cancelled.galdb"
            monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
            monkeypatch.setattr(legacy, "import_legacy", cancelled)
            trigger = page.transfer
        else:
            monkeypatch.setattr(legacy, "inspect_legacy", cancelled)
            trigger = lambda: page.inspect(str(old))
    else:
        dialog = CompareDialog(window)
        dialog.paths = [str(library[0]), str(library[0])]
        monkeypatch.setattr(compare, "compare_libraries", cancelled)
        trigger = dialog.run
    before = library[0].read_bytes()
    trigger()
    wait(app, window)
    expected = "已取消，库没有改变" if operation in ("check", "import") else "已取消"
    assert window.status.message.text() == expected and "未提交" not in window.status.message.text()
    assert library[0].read_bytes() == before


def test_editor_job_start_failure_restores_dialog(window, app, library, monkeypatch):
    wid = opened(app, window, library)
    editor = Editor(window, repo.get_work(window.lib, wid))
    editor.show()
    def failed_start(self):
        raise RuntimeError("injected job start failure")
    monkeypatch.setattr(Job, "start", failed_start)
    editor.save()
    assert not editor.saving and not window.busy and window.pending_jobs == 0
    assert editor.save_button.isEnabled() and editor.cancel_button.isEnabled()
    assert editor.message.text() == "injected job start failure"
    assert window.status.message.text() == "无法启动任务：injected job start failure"
    assert not window.status.progress.isVisible()
    editor.close()
    assert not editor.isVisible()


def test_commit_success_stays_explicit_when_projection_fails(window, app, library, monkeypatch):
    opened(app, window, library)
    page = pending_scan(app, window, library)
    def failed_projection(*args, **kwargs):
        raise OSError("injected projection failure")
    monkeypatch.setattr(window_ui, "load_projection", failed_projection)
    page.commit()
    wait(app, window)
    assert "New story" in [w.title for w in repo.list_works(window.lib)]
    text = window.status.message.text()
    assert "已入库" in text and "书架载入失败" in text and "未提交" not in text
    assert window.screen == "library" and not window.busy


def test_conflict_cancel_defers_one_refresh_until_background_finishes(window, app, library, monkeypatch):
    wid = opened(app, window, library)
    editor = Editor(window, repo.get_work(window.lib, wid))
    editor.show()
    repo.edit_work(window.lib, wid, editor.work.revision, notes="Concurrent change")
    editor.save()
    wait(app, window)
    release = threading.Event()
    refreshes = []
    reload_library = window.reload_library
    def tracked(*args, **kwargs):
        refreshes.append(1)
        return reload_library(*args, **kwargs)
    monkeypatch.setattr(window, "reload_library", tracked)
    window.run_job("Unrelated task", lambda cancel: release.wait(10), lambda _: None)
    editor.reject()
    assert not editor.isVisible() and not refreshes
    release.set()
    wait(app, window)
    assert refreshes == [1] and window.library_page.details[wid].notes == "Concurrent change"


def test_welcome_refresh_does_not_unlock_foreground_task(window, app, tmp_path, monkeypatch):
    """Refreshing the welcome page must not add a job or unlock a running one."""
    file = tmp_path / "recent.galdb"
    file.write_bytes(b"fixture")
    window.settings.data["recent"] = [str(file)]
    foreground_release = threading.Event()
    window.title_page.refresh()
    job = window.run_job("Foreground operation", lambda cancel: foreground_release.wait(10), lambda _: None)
    try:
        until(app, lambda: window.pending_jobs == 1)
        assert window.active_job is job and window.busy and not window.pages.isEnabled()
        assert window.status.message.text() == "Foreground operation"
    finally:
        foreground_release.set()
        wait(app, window)


def test_title_screenshot_leaves_settings_untouched(tmp_path):
    """--screen title --screenshot must not auto-open a library or write settings."""
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    recent = tmp_path / "recent.galdb"
    recent.write_bytes(b"existence only")
    (settings_dir / "settings.json").write_text(json.dumps({"recent": [str(recent)]}), encoding="utf-8")
    output = tmp_path / "title.png"
    wrapper = tmp_path / "ready_title.py"
    wrapper.write_text("""from galpanel2.ui import window as ui
class ReadyWindow(ui.Window):
    def grab(self, *a, **k):
        assert self.pending_jobs == 0
        assert self.lib is None and self.screen == "title"
        assert self.title_page.primary.text() == "开始使用"
        print("RECENT_READY", flush=True)
        return super().grab(*a, **k)
ui.Window = ReadyWindow
from galpanel2.__main__ import main
raise SystemExit(main())
""", encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", GALPANEL2_SETTINGS_DIR=str(settings_dir), PYTHONUTF8="1")
    before = (settings_dir / "settings.json").read_bytes()
    result = subprocess.run([sys.executable, str(wrapper), "--screen", "title", "--screenshot", str(output)],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RECENT_READY" in result.stdout and output.is_file()
    assert (settings_dir / "settings.json").read_bytes() == before


def test_n1_welcome_page_does_not_block_close(app, tmp_path, monkeypatch):
    """A frameless welcome window closes via the ordinary closeEvent."""
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(tmp_path / "settings"))
    existing = tmp_path / "existing.galdb"
    existing.write_bytes(b"file existence fixture, never opened")
    w = Window(screenshot_mode=True)
    w.show()
    app.processEvents()
    try:
        assert w.screen == "title" and w.pending_jobs == 0 and not w.jobs and not w.closing
        assert w.title_page.welcome_controls.winClose.isVisible()
        assert w.close() is True
        app.processEvents()
        assert not w.isVisible() and not w.closing and w.pending_jobs == 0
        assert existing.read_bytes() == b"file existence fixture, never opened"
    finally:
        for _ in range(20):
            app.processEvents()


def test_n1_screenshot_timeout_quits_if_close_refused(tmp_path):
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "title.png"
    wrapper = tmp_path / "timeout_title.py"
    wrapper.write_text("""import galpanel2.__main__ as entry
entry.SCREENSHOT_TIMEOUT_MS = 200
from galpanel2.ui import window as ui
class RefuseClose(ui.Window):
    @property
    def pending_jobs(self):
        return 1
    def closeEvent(self, event):
        event.ignore()
ui.Window = RefuseClose
raise SystemExit(entry.main())
""", encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", GALPANEL2_SETTINGS_DIR=str(settings_dir), PYTHONUTF8="1")
    result = subprocess.run([sys.executable, str(wrapper), "--screen", "title", "--screenshot", str(output)],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Screenshot loading timed out" in result.stderr


def test_n2_welcome_refresh_leaves_no_pending_job(app, tmp_path, monkeypatch):
    """No slot worker can fail here; a refresh never leaves a pending job."""
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(tmp_path / "settings"))
    w = Window(screenshot_mode=True)
    w.show()
    app.processEvents()
    recent = tmp_path / "recent.galdb"
    recent.write_bytes(b"x")
    w.settings.remember(str(recent))
    def boom(*args, **kwargs):
        raise RuntimeError("start failed")
    monkeypatch.setattr(Job, "start", boom)
    try:
        w.title_page.refresh()
        assert w.pending_jobs == 0 and not w.slot_jobs and not w.jobs
        assert w.title_page.primary.text() == "开始使用"
    finally:
        w.close()
        app.processEvents()


def test_n3_existing_target_says_choose_another_path(tmp_path):
    old = old_fixture(tmp_path)
    target = tmp_path / "exists.galdb"
    target.write_bytes(b"already")
    with pytest.raises(d.TargetExists) as caught:
        legacy.import_legacy(str(old), str(target))
    assert str(caught.value) == "目标已存在，请换一个保存位置"
    assert target.read_bytes() == b"already"
    assert not Path(str(target) + ".partial").exists()
    assert not Path(str(target) + ".covers").exists()


def test_n3_existing_target_message_on_transfer_page(window, app, tmp_path, monkeypatch):
    old = old_fixture(tmp_path)
    target = tmp_path / "exists.galdb"
    target.write_bytes(b"already")
    expected = "目标已存在，请换一个保存位置"
    window.show_transfer()
    page = window.transfer_page
    page.inspect(str(old))
    wait(app, window)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    page.transfer()
    wait(app, window)
    assert page.details.text() == expected and expected in page.adv.text
    assert window.status.message.text() == expected
    assert target.read_bytes() == b"already"
