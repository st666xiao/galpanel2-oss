"""lite-b regressions: segments, cards, detail and empty states."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt, QItemSelectionModel
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractButton, QMenu, QWidget

from galpanel2.core import store, meta as M
from galpanel2.ui import library as L
from galpanel2.ui.theme import DAY, NIGHT
from test_ui import app, window, wait

NOW = datetime.now(timezone.utc)
WORKS = {
    1: ("Alpha", 0, {"vndb": "v1"}, 3, [("folder", "ok")]),
    2: ("Bravo", 0, {}, 60, [("folder", "ok"), ("folder", "ok")]),
    3: ("Charlie", 0, {}, None, [("archive", "ok")]),
    4: ("Delta", 0, {"dlsite": "RJ01"}, 1, [("folder", "ok")]),
    5: ("Echo", 1, {}, None, [("folder", "ok")]),
    6: ("Foxtrot", 0, {}, None, [("folder", "missing")]),
}
TAGS = [f"标签{i:02d}" for i in range(1, 13)]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def lite_b_library(tmp_path):
    path = tmp_path / "lite-b.galdb"
    with store.create_library(str(path)) as lib:
        tag_ids = [M.create_tag(lib, n, 1) for n in TAGS]
        with lib.transaction() as c:
            cid = 0
            for wid, (title, hidden, links, played, copies) in WORKS.items():
                added = "2026-01-01T00:00:00Z"
                c.execute("INSERT INTO works(id,uuid,title,developer,notes,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          (wid, str(uuid.uuid4()), title, "", "memo-" + title if wid == 1 else "", hidden, added, added))
                for n, (kind, avail) in enumerate(copies):
                    cid += 1
                    p = tmp_path / (f"{title}-{n}" + (".zip" if kind == "archive" else ""))
                    if avail == "ok":
                        if kind == "archive":
                            p.write_bytes(b"PK")
                        else:
                            p.mkdir(parents=True, exist_ok=True)
                            (p / "game.exe").write_bytes(b"x")
                    c.execute("INSERT INTO copies(id,work_id,source_id,kind,path,path_key,exe_relpath,availability,checked_at,origin,added_at)"
                              " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (cid, wid, None, kind, str(p), str(p).casefold(), "game.exe" if kind == "folder" else None,
                               avail, iso(NOW), "scan", added))
                for site, sid in links.items():
                    c.execute("INSERT INTO external_links(work_id,site,site_id,bound_at) VALUES(?,?,?,?)",
                              (wid, site, sid, iso(NOW)))
                if played is not None:
                    end = NOW - timedelta(days=played)
                    c.execute("INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,started_at,last_beat_at,ended_at,seconds,end_reason)"
                              " VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (wid, None, "tracked", "closed", end.date().isoformat(), iso(end - timedelta(hours=1)),
                               iso(end), iso(end), 3600, "exited"))
        M.set_work_meta(lib, 1, 1, [], tag_ids)
    return path


def opened(app, window, path):
    window.open_library_path(str(path))
    wait(app, window)
    window.show_library()
    wait(app, window)
    assert window.screen == "library"
    return window.library_page


def find(parent, cls, name):
    obj = parent.findChild(cls, name)
    assert obj is not None, f"missing {name}"
    return obj


def btn(parent, name):
    return find(parent, QAbstractButton, name)


def click(widget, pos=None):
    if pos is None:
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
    else:
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    app = QApplication.instance()
    for _ in range(3):
        app.processEvents()


def test_segments_counts_and_recent_sort(app, window, lite_b_library):
    p = opened(app, window, lite_b_library)
    assert tuple(L.SEGMENTS) == ("全部", "未绑定", "最近玩")
    assert p.segment_counts == {"全部": 5, "未绑定": 3, "最近玩": 2}
    for name, label, count in (("segAll", "全部", 5), ("segUnbound", "未绑定", 3), ("segRecent", "最近玩", 2)):
        b = btn(p, name)
        display = "缺资料" if label == "未绑定" else label
        assert b.accessibleName() == label and display in b.text() and str(count) in b.text()
    click(btn(p, "segUnbound"))
    assert sorted(p.visible_work_ids()) == [2, 3, 6]
    click(btn(p, "segRecent"))
    assert p.visible_work_ids() == [4, 1]
    assert not p.order.isEnabled()
    click(btn(p, "segAll"))
    assert p.order.isEnabled() and sorted(p.visible_work_ids()) == [1, 2, 3, 4, 6]
    assert [p.order.itemText(i) for i in range(p.order.count())] == ["加入顺序", "标题", "最近游玩"]
    p.set_filter("隐藏")
    assert p.visible_work_ids() == [5]
    assert not any(btn(p, n).isChecked() for n in ("segAll", "segUnbound", "segRecent"))


def test_recent_sort_uses_time_within_same_day(app, window, lite_b_library):
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    with store.open_library(str(lite_b_library)) as lib:
        with lib.transaction() as c:
            for work_id, hour in ((1, 1), (4, 0)):
                played_at = (noon + timedelta(hours=hour)).astimezone(timezone.utc)
                c.execute("UPDATE play_sessions SET local_day=?, ended_at=? WHERE work_id=?",
                          (noon.date().isoformat(), iso(played_at), work_id))
    page = opened(app, window, lite_b_library)
    page.set_filter("最近玩")
    assert page.visible_work_ids() == [1, 4]


def test_filter_row_state_buttons(app, window, lite_b_library):
    p = opened(app, window, lite_b_library)
    row = find(p, QWidget, "filterRow")
    assert row is p.filter_row and not row.isVisible()
    assert row.isAncestorOf(p.brand_button) and row.isAncestorOf(p.tag_button)
    click(btn(p, "filterButton"))
    assert row.isVisible()
    for text in ("压缩包", "不可访问", "待整理", "最近加入", "隐藏"):
        assert any(b.isVisible() and b.text() == text for b in row.findChildren(QAbstractButton)), text
    archive = next(b for b in row.findChildren(QAbstractButton) if b.text() == "压缩包")
    click(archive)
    assert p.filter_name == "压缩包" and p.visible_work_ids() == [3] and archive.highlight
    click(archive)
    assert p.filter_name == "全部" and not archive.highlight
    p.brand_button.click()
    app.processEvents()
    assert p.brand_popover.isVisible()
    p.brand_popover.hide()
    click(btn(p, "filterButton"))
    assert not row.isVisible()


def test_filter_button_condition_count(app, window, lite_b_library):
    p = opened(app, window, lite_b_library)
    button = btn(p, "filterButton")
    assert button.text() == "筛选"
    tid = next(i for i, t in p.tags.items() if t.name == TAGS[0])
    p.set_meta_state("tag", tid, "in")
    assert button.text().replace(" ", "") == "筛选·1"
    p.set_filter("隐藏")
    assert button.text().replace(" ", "") == "筛选·2"
    p.clear_meta_filter()
    p.set_filter("全部")
    assert button.text() == "筛选" and p.cond_bar.isHidden()


def test_header_search_only_on_library(app, window, lite_b_library):
    p = opened(app, window, lite_b_library)
    assert p.search is window.header.search
    assert window.header.isAncestorOf(p.search) and p.search.objectName() == "headerSearch"
    assert p.search.width() in (220, 340) and p.search.height() == 34
    assert "搜索" in p.search.placeholderText() and p.search.isClearButtonEnabled()
    p.search.setText("Charlie")
    assert p.visible_work_ids() == [3]
    p.search.clear()
    window.show_tidy()
    wait(app, window)
    assert not p.search.isVisible()
    window.show_library()
    wait(app, window)
    assert p.search.isVisible()


def test_find_rect_click_and_normal_card_click(app, window, lite_b_library):
    p = opened(app, window, lite_b_library)
    calls = []
    window.open_find = lambda wid, *a, **k: calls.append(wid)
    p.select_work(1)
    delegate = p.view.itemDelegate()
    def rect_for(wid):
        for i, work in enumerate(p.model.works):
            if work.id == wid:
                return p.view.visualRect(p.model.index(i, 0))
        raise AssertionError(wid)
    r3 = delegate.find_rect(rect_for(3))
    r1 = delegate.find_rect(rect_for(1))
    assert r3 is not None and r1 is None
    assert rect_for(3).contains(r3.toRect())
    assert hasattr(delegate, "find_rect") and delegate.hover_row == -1
    before = p.selected_ids()
    click(p.view.viewport(), r3.center().toPoint())
    assert calls == [3] and p.selected_ids() == before
    other = rect_for(1).topLeft() + (r3.center() - rect_for(3).topLeft()).toPoint()
    click(p.view.viewport(), other)
    assert calls == [3] and p.selected_ids() == [1]


def test_grid_is_six_columns_at_1440(app, window, lite_b_library):
    with store.open_library(str(lite_b_library)) as lib:
        with lib.transaction() as c:
            c.execute("UPDATE works SET hidden=0")
    p = opened(app, window, lite_b_library)
    window.resize(1440, 860)
    app.processEvents()
    grid = p.view.gridSize()
    # The wall is narrower while the legacy 360px detail panel is still docked
    # (E2) and full-width once it is removed (E3); both are valid.  Card width
    # now follows the 2:3 poster spec (176..240) instead of 150..210.
    cols = (p.view.viewport().width() + 18) // grid.width()
    assert 4 <= cols <= 6
    assert 176 <= p.view.itemDelegate().card_width <= 240
    assert p.view.uniformItemSizes()
    assert p.model.rowCount() == 6
    positions = [p.view.visualRect(p.model.index(row, 0)).top() for row in range(min(cols, 6))]
    assert len(set(positions)) == 1, positions
    window.resize(1100, 800)
    app.processEvents()
    cols2 = (p.view.viewport().width() + 18) // p.view.gridSize().width()
    assert 3 <= cols2 <= 6


def test_detail_find_row_more_menu_and_facts(app, window, lite_b_library):
    p = opened(app, window, lite_b_library)
    d = p.detail
    calls = []
    window.open_find = lambda wid, *a, **k: calls.append(wid)
    # The detail panel is now a full page (no fixed 360 width) reached via
    # open_detail(); find_row is hidden and the find action moved to a button.
    p.open_detail(3)
    assert d.width() >= 360
    row = find(d, QWidget, "detailFindRow")
    assert not row.isVisible()
    assert btn(d, "detailFind").isVisible() and btn(d, "detailFind").isEnabled()
    click(btn(d, "detailFind"))
    assert calls == [3]
    assert isinstance(d.more_menu, QMenu)
    assert [a.text() for a in d.more_menu.actions() if not a.isSeparator()] == [
        "编辑…", "启动设置…", "找资料…", "隐藏", "检查文件",
        "冷藏到机械盘…", "搬回常用盘…", "移到其他游戏目录…", "删除游戏文件…", "从书架移除…"]
    p.open_detail(2)
    assert any(a.text().startswith("切换副本") for a in d.more_menu.actions())
    next(a for a in d.more_menu.actions() if a.text().startswith("切换副本")).trigger()
    assert d.copy_index == 1
    p.open_detail(1)
    text = d.facts.text()
    assert not row.isVisible() and "<" not in text
    for word in ("玩过", "最近", "加入", "memo-Alpha", *TAGS, "v1", "找资料"):
        assert word in text, (word, text)
    assert any(isinstance(x, QAbstractButton) and x.isVisible() and x.text() == "+2"
               for x in d.findChildren(QAbstractButton))
    p.open_detail(3)
    assert "未绑定" in d.facts.text() and "还没有标签" in d.facts.text()
    p.open_detail(6)
    assert "文件夹找不到了" in d.facts.text()
    assert d.path.isVisible() and "文件夹找不到了" in d.path.text()
    assert d.current_copy().path in d.path.toolTip()


def test_empty_states_and_selection_bar(app, window, lite_b_library, tmp_path):
    empty = tmp_path / "empty.galdb"
    store.create_library(str(empty)).close()
    window.open_library_path(str(empty))
    wait(app, window)
    p = window.library_page
    calls = []
    window.show_sources = lambda *a, **k: calls.append(1)
    add = btn(p, "emptyAdd")
    assert add.isVisible() and add.text() == "添加游戏目录"
    click(add)
    assert calls == [1]
    window.open_library_path(str(lite_b_library))
    wait(app, window)
    p.search.setText("zzzz-none")
    assert not p.visible_work_ids()
    clear = btn(p, "emptyClear")
    assert clear.isVisible()
    click(clear)
    assert p.search.text() == "" and p.filter_name == "全部" and p.meta_filter.is_empty()
    assert sorted(p.visible_work_ids()) == [1, 2, 3, 4, 6]
    bar = find(p, QWidget, "selectionBar")
    assert bar is p.selection_bar and not bar.isVisible()
    sm = p.view.selectionModel()
    sm.select(p.model.index(0, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect)
    sm.select(p.model.index(1, 0), QItemSelectionModel.SelectionFlag.Select)
    assert bar.isVisible() and "已选 2 部" in bar.label.text()
    batches = []
    window.start_batch = lambda ids, *a, **k: batches.append(sorted(ids) if ids is not None else None)
    texts = [b.text() for b in bar.findChildren(QAbstractButton) if b.isVisible()]
    for word in ("找资料", "隐藏", "检查文件", "取消"):
        assert word in texts
    assert "批量搜刮" not in texts
    bar.buttons["找资料"].click()
    assert batches == [sorted(p.visible_work_ids()[:2])]


def test_theme_roles_and_status_light(app, window):
    expected = {"day": {"chip": "#F0ECEA", "faint": "#B9B2BD", "placeholder": "#EFEBE8"},
                "night": {"chip": "#2E3042", "faint": "#6E6878", "placeholder": "#2B2D3D"}}
    for theme in (DAY, NIGHT):
        for key, value in expected[theme.name].items():
            assert theme.roles[key].upper() == value
    window.resize(1440, 860)
    for theme in (DAY, NIGHT):
        window.theme = theme
        window.apply_theme()
        app.processEvents()
        assert window.status.height() <= 28
        from PySide6.QtGui import QColor
        c = QColor(window.status.grab().toImage().pixel(3, window.status.height() // 2))
        paper = theme.color("paper")
        assert sum(abs(getattr(c, k)() - getattr(paper, k)()) for k in ("red", "green", "blue")) < 40
