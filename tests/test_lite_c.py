"""Find dialog interactions using the existing M2 fake site rules."""
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QListView

from galpanel2.core import binding as B, records as R, store
from test_m2_core import data
from test_m2_ui import app, fake_sites, find, m2_library, open_window, selected, wait, window


def open_find(window, app, library):
    window.resize(1440, 860)
    open_window(window, app, library)
    return find(window, app)


def test_find_geometry_and_veil(window, app, m2_library):
    dialog = open_find(window, app, m2_library)
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    owner = window.frameGeometry()
    assert dialog.frameGeometry() == owner.adjusted(150, 56, -150, -40)
    veil = window.find_veil
    assert veil.isVisible()
    assert veil.geometry().top() == window.header.geometry().bottom() + 1
    dialog.saving = True
    QTest.mouseClick(veil, Qt.MouseButton.LeftButton)
    assert dialog.isVisible()
    dialog.saving = False
    QTest.mouseClick(veil, Qt.MouseButton.LeftButton)
    assert not dialog.isVisible() and not veil.isVisible()


def test_find_pasted_link_debounces_and_can_return(window, app, m2_library, monkeypatch):
    dialog = open_find(window, app, m2_library)
    calls = []
    run_net = dialog._run_net

    def record(label, *args):
        if label == '搜索资料':
            calls.append(dialog.query.text())
        return run_net(label, *args)

    monkeypatch.setattr(dialog, '_run_net', record)
    assert dialog.query_hint.isVisible()
    dialog.query.setText('v123')
    QTest.qWait(100)
    dialog.query.setText('https://vndb.org/v55281')
    assert 'VNDB v55281' in dialog.ref_badge.text()
    assert dialog.ref_badge.isVisible() and not dialog.query_hint.isVisible()
    assert calls == []
    QTest.qWait(450)
    wait(app, window)
    assert calls == ['https://vndb.org/v55281']
    assert [c.record.site_id for c in dialog.candidates()] == ['v55281']
    assert dialog.back_link.isVisible() and '按链接取到 1 个' in dialog.work_label.text()
    dialog.back_link.linkActivated.emit('back')
    wait(app, window)
    assert dialog.query.text() == R.search_text(dialog._work_title)
    assert '找到' in dialog.work_label.text() and not dialog.back_link.isVisible()


def test_find_wall_five_columns(window, app, m2_library):
    dialog = open_find(window, app, m2_library)
    wall = dialog.list
    assert wall.viewMode() == QListView.ViewMode.IconMode
    assert wall.uniformItemSizes()
    assert wall.count() >= 6
    rects = [wall.visualRect(wall.model().index(row, 0)) for row in range(6)]
    assert all(r.top() == rects[0].top() for r in rects[:5]), (rects, wall.viewport().size(), wall.gridSize())
    assert rects[5].top() > rects[0].top()
    rect = rects[0]
    QTest.mouseClick(wall.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    wait(app, window)
    assert wall.currentRow() == 0 and dialog.current_plan is not None


def test_find_weak_poster_opacity(window, app, m2_library):
    dialog = open_find(window, app, m2_library)
    record = dialog.candidates()[0].record
    delegate = dialog.list.itemDelegate()
    assert delegate.opacity(R.Candidate(record, 'no', .1)) == .45
    assert delegate.opacity(R.Candidate(record, 'ok', 1)) == 1


def test_find_sidebar_previews_additions(window, app, m2_library):
    dialog = open_find(window, app, m2_library)
    assert '点一张海报看看' in dialog.side_title.toolTip()
    assert not dialog.will.isVisible()
    selected(window, app, dialog)
    assert dialog.side_title.toolTip() == dialog.current_record.title
    assert 'VNDB 页面' in dialog.side_by.text()
    assert '绑定后会加上' in dialog.will.text() and '封面' in dialog.will.text()
    assert dialog.side_cover.isVisible() and dialog.bind_button.isEnabled()


def test_find_replacement_action(window, app, m2_library):
    with store.open_library(str(m2_library[0])) as lib:
        record = R.parse_vndb(data('vndb_vn.json'))[0]
        B.apply_bind(lib, 1, 1, replace(record, site_id='v999'))
    dialog = open_find(window, app, m2_library)
    assert 'v999' in dialog.bound_label.text()
    assert selected(window, app, dialog).replaces == 'v999'
    assert dialog.bind_button.text() == '替换绑定'
    assert '会替换已绑的 v999' in dialog.will.text()


def test_find_site_actions(window, app, m2_library):
    dialog = open_find(window, app, m2_library)
    assert dialog.site_button.text() == '全部站点'
    action = dialog.switches['dlsite']
    assert isinstance(action, QAction) and action.isCheckable()
    action.trigger()
    assert dialog.sites() == ('vndb', 'bangumi', 'steam', 'f95')
    assert dialog.site_button.text() == 'VNDB · Bangumi · Steam · F95zone'


def test_find_escape_and_poster_return(window, app, m2_library):
    dialog = open_find(window, app, m2_library)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible() and not window.find_veil.isVisible()
    dialog = find(window, app)
    selected(window, app, dialog)
    dialog.list.setFocus()
    QTest.keyClick(dialog.list, Qt.Key.Key_Right)
    wait(app, window)
    assert dialog.list.currentRow() == 1
    QTest.keyClick(dialog.list, Qt.Key.Key_Left)
    wait(app, window)
    assert dialog.list.currentRow() == 0 and dialog.bind_button.isEnabled()
    QTest.keyClick(dialog.list, Qt.Key.Key_Return)
    wait(app, window)
    assert not dialog.isVisible() and not window.find_veil.isVisible()
    assert B.get_info(window.lib, 1).links == {'vndb': 'v55281'}


def test_open_find_updates_theme_without_resetting_preview(window, app, m2_library):
    dialog = open_find(window, app, m2_library)
    plan = selected(window, app, dialog)
    dialog.title_choice.setCurrentIndex(dialog.title_choice.count() - 1)
    title = dialog.title_choice.currentData()
    old = window.theme.roles['card']
    assert old in dialog.query.styleSheet()

    window.toggle_theme()

    new = window.theme.roles['card']
    assert new != old and new in dialog.query.styleSheet()
    assert window.theme.roles['sub'] in dialog.will.styleSheet()
    assert dialog.current_plan is plan and dialog.title_choice.currentData() == title
    assert dialog.bind_button.isEnabled() and not dialog._ref_timer.isActive()
