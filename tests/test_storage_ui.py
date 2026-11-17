"""UI tests for cold storage / permanent delete (T2)."""
import os
from pathlib import Path
import uuid

from PySide6.QtWidgets import QLabel

from galpanel2.core import storage, store
from galpanel2.ui.settings import Settings, DEFAULTS
from galpanel2.ui import library as L
from test_ui import app, wait, window  # noqa: F401  (shared fixtures)


def _make_lib(tmp_path):
    cold = tmp_path / "G" / "冷藏" / "Games"
    cold.mkdir(parents=True)
    root = tmp_path / "D" / "Games"
    path = tmp_path / "cold-ui.galdb"
    rows = ((1, "Alpha", 0, [("日系", "Alpha")]),
            (2, "Bravo", 0, [("日系", "Bravo")]),
            (3, "Hidden", 1, [("日系", "Hidden")]),
            (5, "Multi", 0, [("同人", "Multi"), ("同人", "Multi 2")]))
    with store.create_library(str(path)) as lib:
        with lib.transaction() as c:
            for wid, title, hidden, _copies in rows:
                c.execute("INSERT INTO works(id,uuid,title,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?)",
                          (wid, str(uuid.uuid4()), title, hidden, "2020-01-01", "2020-01-01"))
            c.execute("INSERT INTO works(id,uuid,title,hidden,added_at,updated_at) VALUES(4,?,'NoCopy',0,?,?)",
                      (str(uuid.uuid4()), "2020-01-01", "2020-01-01"))
            for wid, _title, _hidden, copies in rows:
                for category, name in copies:
                    folder = root / category / name
                    folder.mkdir(parents=True)
                    (folder / "game.exe").write_bytes(b"MZ" + bytes([wid]) * 100)
                    c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,"
                              "availability,origin,added_at) VALUES(?,NULL,'folder',?,?,?,'ok','scan','2020-01-01')",
                              (wid, str(folder), os.path.normcase(os.path.abspath(str(folder))), "game.exe"))
    return path, cold, root


def _open(window, app, path, cold):
    window.settings.data["cold_root"] = str(cold)
    window.open_library_path(str(path))
    wait(app, window)
    window.show_library()
    wait(app, window)
    return window.library_page


def test_settings_cold_root_default_and_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("GALPANEL2_SETTINGS_DIR", str(tmp_path))
    assert DEFAULTS["cold_root"] == storage.DEFAULT_COLD_ROOT
    assert Settings().data["cold_root"] == storage.DEFAULT_COLD_ROOT
    (tmp_path / "settings.json").write_text('{"cold_root": ""}', encoding="utf-8")
    assert Settings().data["cold_root"] == storage.DEFAULT_COLD_ROOT
    (tmp_path / "settings.json").write_text('{"cold_root": "H:\\\\x"}', encoding="utf-8")
    assert Settings().data["cold_root"] == r"H:\x"


def test_settings_dialog_saves_cold_root(window, app, tmp_path):
    from galpanel2.ui.externaldialogs import SettingsDialog
    dialog = SettingsDialog(window)
    dialog.cold_root.setText(r"H:\冷藏2")
    dialog.save()
    app.processEvents()
    assert window.settings.data["cold_root"] == r"H:\冷藏2"
    import json
    saved = json.loads((tmp_path / "settings" / "settings.json").read_text(encoding="utf-8"))
    assert saved["cold_root"] == r"H:\冷藏2"


def test_location_tag_values():
    assert L.location_tag("冷藏") == "冷藏"
    assert L.location_tag("已删除") == "已删除"
    assert L.location_tag("常用") is None


def test_sidebar_buttons_and_location_filter(window, app, tmp_path):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    assert list(page.location_buttons) == ["全部", "常用", "冷藏", "已删除"]
    assert page.location_name == "全部"
    assert page.location_buttons["全部"].text() == "全部 4"
    assert page.location_buttons["常用"].text() == "常用 4"
    assert page.location_buttons["冷藏"].text() == "冷藏 0"
    assert page.location_buttons["已删除"].text() == "已删除 0"
    assert page.location_of(4) == "常用"  # no copies
    page.set_location_filter("冷藏")
    assert page.model.rowCount() == 0
    page.set_location_filter("常用")
    assert sorted(w.id for w in page.model.works) == [1, 2, 4, 5]
    page.set_location_filter("全部")
    assert 3 not in [w.id for w in page.model.works]


def test_cold_and_restore_flows(window, app, tmp_path):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    source = root / "日系" / "Alpha"
    asked = []

    def ask(action, items):
        asked.append((action, items))
        return [item["copy_id"] for item in items]

    window.ask_storage = ask
    window.cold_works([1])
    wait(app, window)
    target = cold / "日系" / "Alpha"
    assert asked[0][0] == "cold"
    assert {"copy_id", "work_id", "title", "path", "kind", "size_bytes", "location"} <= set(asked[0][1][0])
    assert not os.path.lexists(source) and (target / "game.exe").is_file()
    assert page.location_of(1) == "冷藏"
    assert page.location_buttons["冷藏"].text() == "冷藏 1"
    page.set_location_filter("冷藏")
    assert [w.id for w in page.model.works] == [1]

    window.restore_works([1])
    wait(app, window)
    assert asked[-1][0] == "restore"
    assert (source / "game.exe").is_file() and not os.path.lexists(target)
    assert page.location_of(1) == "常用"
    log = Path(path).with_name("storage-log.jsonl")
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_delete_flow_keeps_open_detail_deleted(window, app, tmp_path):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    source = root / "日系" / "Alpha"
    page.open_detail(1)
    app.processEvents()
    window.ask_storage = lambda action, items: [item["copy_id"] for item in items]
    window.delete_work_files([1])
    wait(app, window)
    assert not os.path.lexists(source)
    assert page.location_of(1) == "已删除"
    assert page.detail.work is not None and page.detail.work.id == 1
    label = page.detail.findChild(QLabel, "detailLocation")
    assert label is not None and label.text() == "游戏文件已删除"
    assert not page.detail.start_button.isEnabled()
    assert 1 not in [w.id for w in page.model.works]
    page.set_location_filter("已删除")
    assert [w.id for w in page.model.works] == [1]
    assert page.location_buttons["已删除"].text() == "已删除 1"


def test_delete_cancel_does_nothing(window, app, tmp_path):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    source = root / "日系" / "Alpha"
    window.ask_storage = lambda action, items: None
    window.delete_work_files([1])
    wait(app, window)
    assert (source / "game.exe").is_file()
    assert page.location_of(1) == "常用"


def test_delete_keeps_search_and_location_filters_intersected(window, app, tmp_path):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    window.ask_storage = lambda action, items: [item["copy_id"] for item in items]
    window.delete_work_files([1, 2])
    wait(app, window)
    page.set_location_filter("已删除")
    page.search.setText("Alpha")
    assert [w.id for w in page.model.works] == [1]
    page.search.clear()
    assert sorted(w.id for w in page.model.works) == [1, 2]


def test_more_menu_enabled_states(window, app, tmp_path):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    page.open_detail(1)
    app.processEvents()
    detail = page.detail
    detail._rebuild_more_menu()
    actions = {a.text(): a.isEnabled() for a in detail.more_menu.actions() if a.text()}
    assert actions["冷藏到机械盘…"] and not actions["搬回常用盘…"] and actions["删除游戏文件…"]
    window.ask_storage = lambda action, items: [item["copy_id"] for item in items]
    window.cold_works([1])
    wait(app, window)
    page.open_detail(1)
    detail._rebuild_more_menu()
    actions = {a.text(): a.isEnabled() for a in detail.more_menu.actions() if a.text()}
    assert not actions["冷藏到机械盘…"] and actions["搬回常用盘…"] and actions["删除游戏文件…"]
    window.tracker = object()
    try:
        detail._rebuild_more_menu()
        actions = {a.text(): a.isEnabled() for a in detail.more_menu.actions() if a.text()}
        assert not any(actions[name] for name in ("冷藏到机械盘…", "搬回常用盘…", "删除游戏文件…"))
    finally:
        window.tracker = None
    window.screenshot_mode = True
    try:
        detail._rebuild_more_menu()
        actions = {a.text(): a.isEnabled() for a in detail.more_menu.actions() if a.text()}
        assert not any(actions[name] for name in ("冷藏到机械盘…", "搬回常用盘…", "删除游戏文件…"))
    finally:
        window.screenshot_mode = False
    window.lib.readonly = True
    try:
        detail._rebuild_more_menu()
        actions = {a.text(): a.isEnabled() for a in detail.more_menu.actions() if a.text()}
        assert not any(actions[name] for name in ("冷藏到机械盘…", "搬回常用盘…", "删除游戏文件…"))
    finally:
        window.lib.readonly = False


def test_multiple_copies_are_all_offered(window, app, tmp_path):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    got = []

    def ask(action, items):
        got.append(items)
        return [items[1]["copy_id"]]

    window.ask_storage = ask
    window.cold_works([5])
    wait(app, window)
    assert len(got[0]) == 2
    state = storage.list_storage(window.lib)
    assert sorted(state[item["copy_id"]].location for item in got[0]) == ["冷藏", "常用"]
    page.set_location_filter("冷藏")
    assert [w.id for w in page.model.works] == [5]


def test_detail_location_is_hidden_for_common_copy(window, app, tmp_path):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    page.open_detail(1)
    app.processEvents()
    label = page.detail.findChild(QLabel, "detailLocation")
    assert label is not None and not label.isVisible()
    page.detail.start_button.isEnabled()


def test_storage_failure_reloads_and_reports_partial(window, app, tmp_path, monkeypatch):
    path, cold, root = _make_lib(tmp_path)
    _open(window, app, path, cold)
    window.ask_storage = lambda action, items: [items[0]["copy_id"]]
    reloads = []
    monkeypatch.setattr(window, "reload_library", lambda *a, **k: reloads.append((a, k)))
    monkeypatch.setattr(window, "_storage_done_count", lambda action, ids: 1)
    monkeypatch.setattr(storage, "plan_moves", lambda *a, **k: [storage.Move(1, "src", "dst", 0)])

    def boom(*args, **kwargs):
        raise storage.StorageError("模拟失败")

    monkeypatch.setattr(storage, "run_moves", boom)
    window.cold_works([1])
    wait(app, window)
    assert reloads
    assert "已完成 1 部，其余没动" in window.last_message
    assert "库没有改变" not in window.last_message


def test_storage_cancel_reports_partial(window, app, tmp_path, monkeypatch):
    path, cold, root = _make_lib(tmp_path)
    _open(window, app, path, cold)
    window.ask_storage = lambda action, items: [items[0]["copy_id"]]
    reloads = []
    monkeypatch.setattr(window, "reload_library", lambda *a, **k: reloads.append((a, k)))
    monkeypatch.setattr(window, "_storage_done_count", lambda action, ids: 2)
    monkeypatch.setattr(storage, "plan_moves", lambda *a, **k: [storage.Move(1, "src", "dst", 0)])

    def stop(*args, **kwargs):
        raise storage.Cancelled("用户取消")

    monkeypatch.setattr(storage, "run_moves", stop)
    window.cold_works([1])
    wait(app, window)
    assert reloads
    assert "已取消" in window.last_message and "已完成 2 部，其余没动" in window.last_message
    assert "库没有改变" not in window.last_message


def test_completed_message_reports_leftover_sources(window, app, tmp_path, monkeypatch):
    path, cold, root = _make_lib(tmp_path)
    _open(window, app, path, cold)
    window.ask_storage = lambda action, items: [item["copy_id"] for item in items]

    def fake_run(lib, moves, **kwargs):
        kwargs["left"].append(moves[0].src)
        return [move.copy_id for move in moves]

    monkeypatch.setattr(storage, "run_moves", fake_run)
    window.cold_works([1])
    wait(app, window)
    text = window.status.message.text()
    assert "有 1 部原文件没删干净，详见 storage-log.jsonl" in text


def test_ask_storage_ids_are_limited_to_items(window, app, tmp_path, monkeypatch):
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    from galpanel2.ui import window as window_ui
    warnings = []
    monkeypatch.setattr(window_ui.QMessageBox, "warning",
                        lambda *args, **kwargs: warnings.append(args))
    source = root / "日系" / "Alpha"
    target = cold / "日系" / "Alpha"
    valid = page.details[1].copies[0].id
    window.ask_storage = lambda action, items: [99999, valid]
    window.cold_works([1])
    wait(app, window)
    assert not warnings
    assert not os.path.lexists(source) and (target / "game.exe").is_file()

