"""Offscreen tidy page: checks, writes, and the screenshot CLI."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QDialog, QLabel, QLineEdit,
                               QTableWidget, QWidget)

from galpanel2.core import store, tidy
from galpanel2.ui.tidy import LEGACY_HINT
from test_tidy import NOW, _leg, _work
from test_ui import app, window, wait

ROOT = r"C:\gp2-tidy-b"


def _offline_letter():
    for letter in "ZYXWVUTSRQPONMLKJIHGFEDCBA":
        if not os.path.exists(letter + ":\\"):
            return letter
    raise AssertionError("no offline drive letter")


def _play(lib, wid, seconds):
    with lib.transaction() as c:
        c.execute(
            "INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,started_at,last_beat_at,"
            "ended_at,seconds,end_reason,edited) VALUES(?,NULL,'manual','closed','2026-01-01',?,?,?,?,'manual',0)",
            (wid, NOW, NOW, NOW, seconds))


def _bind(lib, wid, site_id):
    with lib.transaction() as c:
        c.execute("INSERT INTO external_links(work_id,site,site_id,bound_at) VALUES(?,?,?,?)",
                  (wid, "vndb", site_id, NOW))


def _user_summary(lib, wid, text):
    with lib.transaction() as c:
        c.execute("INSERT INTO work_info(work_id,summary,summary_source,updated_at) VALUES(?,?,?,?)",
                  (wid, text, "user", NOW))


def _q(path, sql, *args):
    with sqlite3.connect(path) as c:
        return c.execute(sql, args).fetchall()


def build_shelf(tmp_path):
    path = tmp_path / "shelf.galdb"
    letter = _offline_letter()
    off = letter + r":\gp2-tidy-b\OfflineDrive\离线戊"
    assert not off.replace("/", "\\").lower().startswith("d:\\library")
    lib = store.create_library(str(path))
    ids = {}
    try:
        ids["long"], _ = _work(lib, "重复时长", [{"path": ROOT + r"\dup\play-long\重复时长", "kind": "folder"}])
        ids["short"], _ = _work(lib, "重复时长", [{"path": ROOT + r"\dup\play-short\重复时长", "kind": "folder"}])
        _play(lib, ids["long"], 7200)
        ids["plain"], _ = _work(lib, "重复绑定", [{"path": ROOT + r"\dup\bind-plain\重复绑定", "kind": "folder"}])
        ids["bound"], _ = _work(lib, "重复绑定", [{"path": ROOT + r"\dup\bind-bound\重复绑定", "kind": "folder"}])
        _bind(lib, ids["bound"], "v55501")
        ids["lo"], _ = _work(lib, "重复编号", [{"path": ROOT + r"\dup\id-lo\重复编号", "kind": "folder"}])
        ids["hi"], _ = _work(lib, "重复编号", [{"path": ROOT + r"\dup\id-hi\重复编号", "kind": "folder"}])
        ids["miss_a"], _ = _work(lib, "失踪甲", [{"path": ROOT + r"\miss\Alpha\失踪甲.zip", "kind": "archive",
                                               "availability": "missing", "size": 20}])
        ids["miss_b"], _ = _work(lib, "失踪乙", [{"path": ROOT + r"\miss\Alpha\失踪乙.zip", "kind": "archive",
                                               "availability": "missing", "size": 20}])
        ids["miss_c"], _ = _work(lib, "失踪丙", [{"path": ROOT + r"\miss\Beta\失踪丙", "kind": "folder",
                                               "availability": "missing"}])
        ids["miss_d"], _ = _work(lib, "失踪丁", [{"path": ROOT + r"\miss\Beta\失踪丁", "kind": "folder",
                                               "availability": "missing"}])
        ids["off"], _ = _work(lib, "离线戊", [{"path": off, "kind": "folder", "availability": "missing"}])
        ids["junk_hide"], _ = _work(lib, "补丁工具包", [{"path": ROOT + r"\junk\补丁工具包", "kind": "folder",
                                                    "size": 100}])
        ids["junk_game"], _ = _work(lib, "小文件样本", [{"path": ROOT + r"\junk\小文件样本", "kind": "folder",
                                                     "size": 100, "exe": "game.exe"}])
        ids["clean"], _ = _work(lib, "Eternum-0.9.5-pc", [{"path": ROOT + r"\titles\box\Eternum-0.9.5-pc",
                                                          "kind": "folder"}])
        ids["orig"], _ = _work(lib, "现用书名", [{"path": ROOT + r"\titles\box2\现用书名", "kind": "folder"}])
        _leg(lib, ids["orig"], "original_title", "text", "旧库原名作品")
        ids["parent"], _ = _work(lib, "v2", [{"path": ROOT + r"\titles\上层目录名\v2", "kind": "folder"}])
        ids["free"], _ = _work(lib, "可找资料", [{"path": ROOT + r"\legacy\可找资料", "kind": "folder"}])
        _leg(lib, ids["free"], "bindings", "text", json.dumps({"vndb": "v4242"}, ensure_ascii=False))
        _leg(lib, ids["free"], "description", "text", "可找资料的旧简介正文。")
        ids["free2"], _ = _work(lib, "也可找资料", [{"path": ROOT + r"\legacy\也可找资料", "kind": "folder"}])
        _leg(lib, ids["free2"], "bindings", "text", json.dumps({"vndb": "v4243"}, ensure_ascii=False))
        ids["holder"], _ = _work(lib, "占用编号的作品", [{"path": ROOT + r"\legacy\占用编号的作品", "kind": "folder"}])
        _bind(lib, ids["holder"], "v7777")
        ids["taken"], _ = _work(lib, "编号被占", [{"path": ROOT + r"\legacy\编号被占", "kind": "folder"}])
        _leg(lib, ids["taken"], "bindings", "text", json.dumps({"vndb": "v7777"}, ensure_ascii=False))
        ids["linked"], _ = _work(lib, "已经绑定", [{"path": ROOT + r"\legacy\已经绑定", "kind": "folder"}])
        _leg(lib, ids["linked"], "bindings", "text", json.dumps({"vndb": "v8888"}, ensure_ascii=False))
        _bind(lib, ids["linked"], "v8888")
        ids["sum"], _ = _work(lib, "待用简介", [{"path": ROOT + r"\legacy\待用简介", "kind": "folder"}])
        _leg(lib, ids["sum"], "description", "text", "旧简介正文甲")
        ids["user"], _ = _work(lib, "自写简介", [{"path": ROOT + r"\legacy\自写简介", "kind": "folder"}])
        _leg(lib, ids["user"], "description", "text", "旧简介不该覆盖")
        _user_summary(lib, ids["user"], "用户写的")
        tidy.add_ignored(lib, ROOT + r"\skip\exact-file", "exact", "user")
        tidy.add_ignored(lib, ROOT + r"\skip\tree-dir", "tree", "user")
        titles = {row["work_id"]: row for row in tidy.title_report(lib)}
        spec = {
            "ids": ids, "uuid": lib.uuid, "letter": letter,
            "clean": titles[ids["clean"]]["suggested"],
            "orig": titles[ids["orig"]]["suggested"],
        }
        assert titles[ids["clean"]]["reason"] == "cleaned"
        assert titles[ids["parent"]]["reason"] == "parent_folder"
        assert titles[ids["orig"]]["reason"] == "legacy_original"
    finally:
        lib.close()
    return path, spec


def plain(html):
    import re
    return re.sub(r"<[^>]+>", "", html or "")


def _labels(window):
    return [lab for lab in window.findChildren(QLabel) if lab.isVisible()]


def page_text(window):
    parts = [plain(lab.text()) for lab in _labels(window)]
    parts += [b.text() for b in window.findChildren(QAbstractButton) if b.isVisible() and b.text()]
    parts += [e.text() for e in window.findChildren(QLineEdit) if e.isVisible()]
    return "\n".join(parts)


def _pos(window, widget):
    return widget.mapTo(window, widget.rect().center())


def _activate(app, lab):
    import re
    raw = lab.text() or ""
    match = re.search(r"""href\s*=\s*['\"]([^'\"]+)['\"]""", raw)
    lab.linkActivated.emit(match.group(1) if match else "go")
    app.processEvents()


def _candidates(window, fragment):
    found = []
    for lab in _labels(window):
        if fragment in plain(lab.text()):
            found.append(lab)
    for btn in window.findChildren(QAbstractButton):
        if btn.isVisible() and fragment in (btn.text() or ""):
            found.append(btn)
    return found


def click_text(app, window, fragment, marker=None):
    found = _candidates(window, fragment)
    assert found, fragment
    if marker:
        anchors = [lab for lab in _labels(window) if marker in plain(lab.text())]
        assert anchors, marker
        ay = _pos(window, anchors[0]).y()
        found = sorted(found, key=lambda item: abs(_pos(window, item).y() - ay))
        assert abs(_pos(window, found[0]).y() - ay) <= 56, (fragment, marker)
    target = found[0]
    if isinstance(target, QLabel):
        _activate(app, target)
    else:
        target.click()
        app.processEvents()
    return target


def click_button(app, window, fragment):
    found = [b for b in window.findChildren(QAbstractButton) if b.isVisible() and fragment in (b.text() or "")]
    assert found, fragment
    found[0].click()
    app.processEvents()
    return found[0]


def _boxes(window):
    page = window.pages.currentWidget()
    found = list(page.findChildren(QCheckBox))
    return [b for b in found if b.isVisible()]


def _nearby(window, box):
    pos = _pos(window, box)
    bits = []
    for lab in _labels(window):
        lp = _pos(window, lab)
        if abs(lp.y() - pos.y()) <= 18 and -40 <= lp.x() - pos.x() <= 900:
            bits.append(plain(lab.text()))
    return " ".join(bits)


def box_near(window, marker):
    hits = [b for b in _boxes(window) if marker in _nearby(window, b)]
    assert len(hits) == 1, (marker, [(_nearby(window, b), b.isChecked()) for b in _boxes(window)])
    return hits[0]


def goto(app, window, token, marker):
    found = _candidates(window, token)
    assert found, token
    found.sort(key=lambda item: _pos(window, item).x())
    target = found[0]
    if isinstance(target, QLabel) and "href" in (target.text() or ""):
        _activate(app, target)
    else:
        target.click()
        app.processEvents()
    assert marker in page_text(window), (token, page_text(window)[:400])


def enter(app, window, path):
    window.resize(1440, 860)
    window.open_library_path(str(path))
    wait(app, window)
    window.header.navTidy.click()
    wait(app, window)
    assert window.screen == "tidy"
    return page_text(window)


def expand_until(app, window, marker, title):
    if any(title in plain(lab.text()) for lab in _labels(window)):
        return
    for _ in range(6):
        anchors = [lab for lab in _labels(window) if marker in plain(lab.text())]
        assert anchors, marker
        ay = _pos(window, anchors[0]).y()
        row = []
        for wdg in window.pages.currentWidget().findChildren(QWidget):
            if not wdg.isVisible() or not isinstance(wdg, (QLabel, QAbstractButton)) or isinstance(wdg, QCheckBox):
                continue
            if abs(_pos(window, wdg).y() - ay) > 24:
                continue
            label = plain(wdg.text())
            if "整组移除" in label or marker in label:
                continue
            if label in ("▸", "▶", "►", "▾", "▼", "") or "展开" in label:
                row.append(wdg)
        row.sort(key=lambda item: _pos(window, item).x())
        assert row, "no expand control"
        target = row[0]
        if isinstance(target, QLabel):
            _activate(app, target)
        else:
            target.click()
            app.processEvents()
        if any(title in plain(lab.text()) for lab in _labels(window)):
            return
    raise AssertionError("could not expand")


def line_edit(window, text):
    hits = [e for e in window.findChildren(QLineEdit) if e.isVisible() and e.text() == text]
    assert len(hits) == 1, (text, [e.text() for e in window.findChildren(QLineEdit) if e.isVisible()])
    return hits[0]


def hear(window):
    notes = []
    real = window.status.idle
    def wrapped(message):
        notes.append(str(message))
        real(message)
    window.status.idle = wrapped
    return notes


class _Dialogs:
    def __init__(self, monkeypatch):
        self.items = []
        self.mode = "accept"
        def fake_exec(dialog, *args, **kwargs):
            info = self._dump(dialog)
            self.items.append(info)
            blob = "\n".join([info["title"], *info["labels"], *info["buttons"]])
            if "确认移除" in blob or "备份并移除" in blob:
                target = "备份并移除" if self.mode == "accept" else "取消"
                for button in dialog.findChildren(QAbstractButton):
                    if target in (button.text() or ""):
                        button.click()
                        break
                code = QDialog.DialogCode.Accepted if self.mode == "accept" else QDialog.DialogCode.Rejected
            else:
                code = QDialog.DialogCode.Accepted
            try:
                dialog.setResult(code)
            except Exception:
                pass
            return int(code)
        monkeypatch.setattr(QDialog, "exec", fake_exec)

    @staticmethod
    def _dump(dialog):
        labels = [plain(lab.text()) for lab in dialog.findChildren(QLabel)]
        buttons = [b.text() for b in dialog.findChildren(QAbstractButton) if b.text()]
        tables = []
        for table in dialog.findChildren(QTableWidget):
            rows = []
            for r in range(table.rowCount()):
                rows.append([plain(table.item(r, c).text()) if table.item(r, c) else ""
                             for c in range(table.columnCount())])
            tables.append(rows)
        return {"title": dialog.windowTitle(), "labels": labels, "buttons": buttons, "tables": tables}


def _blob(info):
    return "\n".join([info.get("title") or "", *(info.get("labels") or []), *(info.get("buttons") or [])])


def test_tidy_link_opens_page(app, window, tmp_path):
    path = tmp_path / "empty.galdb"
    store.create_library(str(path)).close()
    window.resize(1440, 860)
    window.open_library_path(str(path))
    wait(app, window)
    page = window.library_page
    assert window.header.navTidy.isVisible() and window.header.navTidy.text() == "整理"
    assert page.batch_link.isHidden() and page.filter_button.text() == "筛选"
    assert page.filter_row.isHidden() and page.filter_row.isAncestorOf(page.brand_button)
    assert window.header.navLibrary.selected
    window.header.navTidy.click()
    wait(app, window)
    assert window.screen == "tidy"
    # lite-a header has only two tabs; on the tidy page 「整理」 is current.
    assert window.header.navTidy.selected
    assert not window.header.navLibrary.selected
    assert set(window.header.buttons) == {"library", "tidy", "scan", "source", "record"}


def test_tidy_readonly_disables_writes(app, window, tmp_path, monkeypatch):
    path, spec = build_shelf(tmp_path)
    import galpanel2.ui.window as winmod
    real = winmod.store.open_library
    monkeypatch.setattr(winmod.store, "open_library", lambda p, readonly=False, **k: real(p, readonly=True))
    enter(app, window, path)
    assert "只读打开的库只能看，不能整理" in page_text(window)
    goto(app, window, "NOT A GAME", "补丁工具包")
    assert _boxes(window)
    box_near(window, "补丁工具包").click()
    app.processEvents()
    hide = [b for b in window.findChildren(QAbstractButton) if b.isVisible() and "隐藏勾选" in (b.text() or "")]
    assert hide and not hide[0].isEnabled()
    goto(app, window, "DUPLICATES", "备份并合并")
    merge = [b for b in window.findChildren(QAbstractButton) if b.isVisible() and "备份并合并" in (b.text() or "")]
    assert merge and not merge[0].isEnabled()
    goto(app, window, "TITLES", "去版本号")
    rename = [b for b in window.findChildren(QAbstractButton) if b.isVisible() and "的书名" in (b.text() or "")]
    assert rename and not rename[0].isEnabled()
    goto(app, window, "LEGACY", "待确认")
    queue = [b for b in window.findChildren(QAbstractButton) if b.isVisible() and "全部放进" in (b.text() or "")]
    assert queue and not queue[0].isEnabled()
    goto(app, window, "MISSING", "找不到文件")
    box_near(window, "Alpha").click()
    app.processEvents()
    remove = [b for b in window.findChildren(QAbstractButton) if b.isVisible() and "移除勾选" in (b.text() or "")]
    assert remove and not remove[0].isEnabled()
    before = list(path.parent.glob("*.bak"))
    remove[0].click()
    app.processEvents()
    wait(app, window)
    assert list(path.parent.glob("*.bak")) == before
    assert _q(path, "SELECT count(*) FROM works")[0][0] > 0


def test_tidy_batch_running_blocks_writes(app, window, tmp_path, monkeypatch):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    dialogs = _Dialogs(monkeypatch)
    dialogs.mode = "reject"
    enter(app, window, path)
    hold = QThread()
    hold.start()
    try:
        deadline = time.monotonic() + 5
        while not hold.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert hold.isRunning()
        window.batch_runner = hold
        goto(app, window, "DUPLICATES", "备份并合并")
        button = click_button(app, window, "备份并合并")
        assert button.isEnabled()
        wait(app, window)
        blob = "\n".join(notes + [window.status.message.text() or ""])
        assert "后台找资料还在跑，先在待确认页暂停" in blob
        assert "重复时长" in page_text(window)
        assert _q(path, "SELECT count(*) FROM works WHERE title=?", "重复时长")[0][0] == 2
        assert not list(path.parent.glob("*.tidy-*.bak"))
    finally:
        window.batch_runner = None
        hold.quit()
        hold.wait(2000)


def test_missing_defaults_unchecked_and_group_check(app, window, tmp_path):
    path, spec = build_shelf(tmp_path)
    enter(app, window, path)
    assert "找不到文件" in page_text(window)
    boxes = _boxes(window)
    assert boxes and not any(box.isChecked() for box in boxes)
    remove = [b for b in window.findChildren(QAbstractButton) if b.isVisible() and "移除勾选" in (b.text() or "")]
    assert remove and not remove[0].isEnabled()
    assert "已勾选 0 部" in page_text(window)
    box_near(window, "Alpha").click()
    app.processEvents()
    shown = page_text(window)
    assert "已勾选 2 部" in shown and "1 个文件夹" in shown
    click_button(app, window, "全不选")
    cleared = page_text(window)
    assert "已勾选 0 部" in cleared
    assert not [b for b in window.findChildren(QAbstractButton)
                if b.isVisible() and "移除勾选" in (b.text() or "")][0].isEnabled()
    assert _q(path, "SELECT count(*) FROM works WHERE title LIKE '失踪%'")[0][0] == 4


def test_missing_remove_group_and_partial(app, window, tmp_path, monkeypatch):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    dialogs = _Dialogs(monkeypatch)
    enter(app, window, path)
    expand_until(app, window, "Beta", "失踪丙")
    click_button(app, window, "全不选")
    box_near(window, "失踪丙").click()
    app.processEvents()
    bar = page_text(window)
    assert "已勾选 1 部" in bar and "0 个文件夹" in bar
    dialogs.mode = "reject"
    dialogs.items.clear()
    click_button(app, window, "移除勾选")
    partial = _blob(dialogs.items[-1])
    dialogs.mode = "accept"
    click_button(app, window, "移除勾选")
    wait(app, window)
    dialogs.mode = "reject"
    dialogs.items.clear()
    click_text(app, window, "整组移除，以后扫描跳过这个文件夹", "Alpha")
    group = _blob(dialogs.items[-1])
    dialogs.mode = "accept"
    click_text(app, window, "整组移除，以后扫描跳过这个文件夹", "Alpha")
    wait(app, window)
    for blob in (partial, group):
        assert "确认移除" in blob
        assert "取消" in blob and "备份并移除" in blob
        assert "不动。只删书架记录、游玩记录和封面引用" in blob
        assert "shelf.galdb.tidy-" in blob and ".bak" in blob
        assert "后悔了可以用备份文件恢复整个库" in blob
    assert "全在" in partial and "Beta" in partial and "整个文件夹" not in partial
    assert "其余移除的路径各自跳过" in partial
    assert "整个文件夹" in group and "Alpha" in group and "全在" in group
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪丙") == []
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪丁")
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪甲") == []
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪乙") == []
    scopes = _q(path, "SELECT path, scope FROM ignored_paths")
    assert any(row[1] == "tree" and "Alpha" in row[0] for row in scopes)
    assert any(row[1] == "exact" and str(row[0]).endswith("失踪丙") for row in scopes)
    assert not any(row[1] == "tree" and "Beta" in row[0] for row in scopes)
    assert any("移除了" in line for line in notes)


def test_missing_drive_offline_message(app, window, tmp_path, monkeypatch):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    dialogs = _Dialogs(monkeypatch)
    dialogs.mode = "accept"
    enter(app, window, path)
    click_text(app, window, "整组移除，以后扫描跳过这个文件夹", "OfflineDrive")
    wait(app, window)
    blob = "\n".join(notes + [window.status.message.text() or ""])
    assert spec["letter"] in blob and "现在不在" in blob and "没有移除这一组" in blob
    assert _q(path, "SELECT id FROM works WHERE title=?", "离线戊")


def test_junk_hide_and_is_game(app, window, tmp_path, monkeypatch):
    path, spec = build_shelf(tmp_path)
    dialogs = _Dialogs(monkeypatch)
    dialogs.mode = "reject"
    enter(app, window, path)
    goto(app, window, "NOT A GAME", "是游戏")
    before = len(dialogs.items)
    baks = list(path.parent.glob("*.bak"))
    click_text(app, window, "隐藏", "补丁工具包")
    wait(app, window)
    click_text(app, window, "是游戏", "小文件样本")
    wait(app, window)
    assert len(dialogs.items) == before
    assert _q(path, "SELECT hidden FROM works WHERE title=?", "补丁工具包") == [(1,)]
    assert _q(path, "SELECT hidden FROM works WHERE title=?", "小文件样本") == [(0,)]
    assert list(path.parent.glob("*.bak")) == baks
    loaded = tidy.DismissStore(str(path), spec["uuid"]).load()
    assert spec["ids"]["junk_game"] in loaded["junk"]
    assert "小文件样本" not in page_text(window)


def test_dup_default_keep_and_merge(app, window, tmp_path, monkeypatch):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    short, long = spec["ids"]["short"], spec["ids"]["long"]
    side = tidy.DismissStore(str(path), spec["uuid"])
    side.add("junk", short)
    side.add("titles", short)
    side.add("duplicates", (short, 999999))
    dialogs = _Dialogs(monkeypatch)
    enter(app, window, path)
    goto(app, window, "DUPLICATES", "备份并合并")
    first = page_text(window)
    click_text(app, window, "重复绑定")
    bound = page_text(window)
    click_text(app, window, "重复编号")
    ident = page_text(window)
    click_button(app, window, "换主作品")
    rotated = page_text(window)
    click_text(app, window, "重复时长")
    dialogs.items.clear()
    click_button(app, window, "备份并合并")
    wait(app, window)
    assert f"并进 No.{long:03d}" in first
    assert f"并进 No.{spec['ids']['bound']:03d}" in bound
    assert f"No.{spec['ids']['plain']:03d}" in bound
    assert f"并进 No.{spec['ids']['lo']:03d}" in ident
    assert f"并进 No.{spec['ids']['lo']:03d}" not in rotated
    assert f"并进 No.{spec['ids']['hi']:03d}" in rotated
    assert not any("确认移除" in _blob(item) for item in dialogs.items)
    assert any("合并好了" in line and "重复时长" in line for line in notes)
    assert _q(path, "SELECT id FROM works WHERE id=?", short) == []
    assert _q(path, "SELECT id FROM works WHERE id=?", long) == [(long,)]
    assert _q(path, "SELECT count(*) FROM copies WHERE work_id=?", long)[0][0] == 2
    left = tidy.DismissStore(str(path), spec["uuid"]).load()
    assert short not in left["junk"] and short not in left["titles"]
    assert all(short not in item for item in left["duplicates"])


def test_dup_not_duplicate_persists(app, window, tmp_path):
    path, spec = build_shelf(tmp_path)
    lo, hi = spec["ids"]["lo"], spec["ids"]["hi"]
    enter(app, window, path)
    goto(app, window, "DUPLICATES", "不是重复")
    click_text(app, window, "不是重复", "重复编号")
    wait(app, window)
    assert "重复编号" not in page_text(window)
    window.show_tidy()
    wait(app, window)
    goto(app, window, "DUPLICATES", "备份并合并")
    assert "重复编号" not in page_text(window)
    assert "重复时长" in page_text(window)
    loaded = tidy.DismissStore(str(path), spec["uuid"]).load()
    assert (lo, hi) in loaded["duplicates"] or (hi, lo) in loaded["duplicates"]
    assert _q(path, "SELECT count(*) FROM works WHERE title=?", "重复编号")[0][0] == 2


def test_titles_default_check_and_empty_rejected(app, window, tmp_path):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    enter(app, window, path)
    goto(app, window, "TITLES", "上层文件夹")
    states = {key: box_near(window, marker).isChecked()
              for marker, key in (("去版本号", "clean"), ("旧库原名", "orig"), ("上层文件夹", "parent"))}
    assert states == {"clean": True, "orig": True, "parent": False}
    line_edit(window, spec["clean"]).setText("")
    click_button(app, window, "的书名")
    wait(app, window)
    blob = "\n".join(notes + [window.status.message.text() or ""])
    assert "第 1 行书名是空的" in blob
    assert _q(path, "SELECT title FROM works WHERE id=?", spec["ids"]["clean"]) == [("Eternum-0.9.5-pc",)]
    assert _q(path, "SELECT title FROM works WHERE id=?", spec["ids"]["orig"]) == [("现用书名",)]
    assert _q(path, "SELECT title FROM works WHERE id=?", spec["ids"]["parent"]) == [("v2",)]


def test_titles_retitle_uses_edited_text(app, window, tmp_path):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    enter(app, window, path)
    goto(app, window, "TITLES", "去版本号")
    line_edit(window, spec["clean"]).setText("  验收新书名  ")
    click_button(app, window, "的书名")
    wait(app, window)
    assert any("改好了" in line for line in notes)
    assert _q(path, "SELECT title FROM works WHERE id=?", spec["ids"]["clean"]) == [("验收新书名",)]
    assert _q(path, "SELECT title FROM works WHERE id=?", spec["ids"]["orig"]) == [(spec["orig"],)]
    assert _q(path, "SELECT title FROM works WHERE id=?", spec["ids"]["parent"]) == [("v2",)]


def test_legacy_links_and_queue_refs(app, window, tmp_path, monkeypatch):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    monkeypatch.setattr("galpanel2.ui.externaldialogs.FindDialog.search", lambda self: None)
    seen = []
    def fake_search(client, title, paths, sites=None, ref=None):
        seen.append(None if ref is None else [ref.site, ref.site_id])
        return {"status": "none", "candidates": [], "errors": {}, "sites": [], "unreachable": []}
    monkeypatch.setattr("galpanel2.ui.review.BT.search_one", fake_search)
    enter(app, window, path)
    goto(app, window, "LEGACY", "按编号找资料")
    shown = page_text(window)
    action_links = [lab for lab in _labels(window) if plain(lab.text()).strip() == "按编号找资料"]
    assert len(action_links) == 2
    assert LEGACY_HINT in shown
    assert "已绑 VNDB" in shown
    assert f"已绑在 No.{spec['ids']['holder']:03d}" in shown
    assert "v7777" in shown
    click_text(app, window, "按编号找资料", "可找资料")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        app.processEvents()
        if window.find_dialog is not None and "v4242" in (window.find_dialog.query.text() or ""):
            break
        time.sleep(0.02)
    assert window.find_dialog is not None
    assert window.find_dialog.query.text().strip() == "v4242"
    window.find_dialog.close()
    wait(app, window)
    click_text(app, window, "用旧简介", "待用简介")
    wait(app, window)
    assert any("已用旧简介" in line for line in notes)
    assert _q(path, "SELECT summary, summary_source FROM work_info WHERE work_id=?", spec["ids"]["sum"]) == [
        ("旧简介正文甲", "user")]
    assert _q(path, "SELECT summary FROM work_info WHERE work_id=?", spec["ids"]["user"]) == [("用户写的",)]
    hold = QThread()
    hold.start()
    try:
        window.batch_runner = hold
        from galpanel2.core.batch import QueueStore
        assert not os.path.exists(QueueStore(window.lib.path, window.lib.uuid).path)
        click_button(app, window, "全部放进")
        assert window.screen == "review"
        assert not os.path.exists(QueueStore(window.lib.path, window.lib.uuid).path)
    finally:
        window.batch_runner = None
        hold.quit()
        hold.wait(2000)
    window.show_tidy()
    wait(app, window)
    goto(app, window, "LEGACY", "全部放进")
    click_button(app, window, "全部放进")
    deadline = time.monotonic() + 30
    from galpanel2.core.batch import QueueStore
    queue = QueueStore(window.lib.path, window.lib.uuid)
    while time.monotonic() < deadline and not os.path.exists(queue.path):
        app.processEvents()
        time.sleep(0.02)
    wait(app, window)
    raw = json.loads(Path(queue.path).read_text(encoding="utf-8"))
    expect = {str(spec["ids"]["free"]): "vndb:v4242", str(spec["ids"]["free2"]): "vndb:v4243"}
    assert raw.get("refs") == expect
    assert ["vndb", "v4242"] in seen and ["vndb", "v4243"] in seen


def test_ignored_undo(app, window, tmp_path):
    path, spec = build_shelf(tmp_path)
    enter(app, window, path)
    goto(app, window, "IGNORED", "撤销")
    shown = page_text(window)
    assert "exact-file" in shown and "tree-dir" in shown
    assert "只这一个" in shown and "整个文件夹" in shown and "你加的" in shown
    click_text(app, window, "撤销", "exact-file")
    wait(app, window)
    click_text(app, window, "撤销", "tree-dir")
    wait(app, window)
    assert "还没有跳过的路径" in page_text(window)
    assert _q(path, "SELECT count(*) FROM ignored_paths")[0][0] == 0


def test_log_dialog_lists_actions(app, window, tmp_path, monkeypatch):
    path = tmp_path / "log.galdb"
    lib = store.create_library(str(path))
    try:
        gone, _ = _work(lib, "日志移除", [{"path": ROOT + r"\log\gone", "availability": "missing"}])
        keep, _ = _work(lib, "日志主作品", [{"path": ROOT + r"\log\keep"}])
        other, _ = _work(lib, "日志次作品", [{"path": ROOT + r"\log\other"}])
        named, _ = _work(lib, "日志旧名", [{"path": ROOT + r"\log\named"}])
        summed, _ = _work(lib, "日志简介", [{"path": ROOT + r"\log\sum"}])
        _leg(lib, summed, "description", "text", "日志用的旧简介")
        clock = {"n": 0}
        def tick():
            clock["n"] += 1
            return f"2026-01-01T00:{clock['n']:02d}:00Z"
        old = tidy.utc_now
        tidy.utc_now = tick
        try:
            rev = {row[0]: row[1] for row in _q(path, "SELECT id,revision FROM works")}
            removed = tidy.remove_works(lib, {gone: rev[gone]})
            tidy.merge_works(lib, keep, rev[keep], {other: rev[other]})
            rev[named] = _q(path, "SELECT revision FROM works WHERE id=?", named)[0][0]
            tidy.retitle(lib, {named: (rev[named], "日志新名")})
            tidy.add_ignored(lib, ROOT + r"\log\skip-me", "exact", "user")
            assert tidy.use_legacy_summary(lib, summed) is True
        finally:
            tidy.utc_now = old
        backup = Path(removed["backup"]).name
    finally:
        lib.close()
    dialogs = _Dialogs(monkeypatch)
    enter(app, window, path)
    dialogs.items.clear()
    click_text(app, window, "整理记录")
    app.processEvents()
    assert dialogs.items, "log dialog did not appear"
    info = dialogs.items[-1]
    rows = [row for table in info["tables"] for row in table]
    blob = _blob(info) + "\n" + "\n".join(" ".join(row) for row in rows)
    assert rows, info
    for word in ("移除", "合并", "改书名", "跳过路径", "用旧简介"):
        assert word in blob, (word, rows)
    assert rows and "用旧简介" in " ".join(rows[0])
    assert backup in blob
    assert __import__("re").search(r"\d{1,2}:\d{2}", blob)


def test_missing_two_hundred_toggle_stays_fast(app, window, tmp_path):
    path = tmp_path / "wide.galdb"
    lib = store.create_library(str(path))
    try:
        for n in range(200):
            _work(lib, f"大组作品{n:03d}", [{"path": ROOT + rf"\wide\Pile\w{n:03d}", "availability": "missing"}])
    finally:
        lib.close()
    enter(app, window, path)
    expand_until(app, window, "Pile", "大组作品000")
    assert "另外 150 部" in page_text(window)
    started = time.monotonic()
    click_text(app, window, "展开全部")
    app.processEvents()
    expand_s = time.monotonic() - started
    group_boxes = [b for b in _boxes(window) if "Pile" in _nearby(window, b) and "大组作品" not in _nearby(window, b)]
    assert len(group_boxes) == 1, len(group_boxes)
    started = time.monotonic()
    group_boxes[0].click()
    app.processEvents()
    toggle_s = time.monotonic() - started
    assert "已勾选 200 部" in page_text(window)
    assert toggle_s < 1.5, toggle_s
    assert expand_s < 3, expand_s


def _shot(path, out, theme, extra):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
               GALPANEL2_SETTINGS_DIR=str(path.parent / f"settings-{theme}-{out.stem}"))
    before = path.read_bytes()
    result = subprocess.run([sys.executable, "-m", "galpanel2", "--screen", "tidy", "--theme", theme,
                             "--library", str(path), "--screenshot", str(out), *extra],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.is_file() and out.stat().st_size > 1000
    assert path.read_bytes() == before


def test_tidy_screenshot_sections(app, tmp_path):
    path, spec = build_shelf(tmp_path)
    missing = subprocess.run([sys.executable, "-m", "galpanel2", "--screen", "tidy", "--screenshot",
                              str(tmp_path / "nope.png")],
                             env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
                                      GALPANEL2_SETTINGS_DIR=str(tmp_path / "shot-err")),
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
    blob = (missing.stderr or "") + (missing.stdout or "")
    assert missing.returncode != 0 and "library" in blob.lower()
    shots = [("missing", ["--tidy-section", "missing", "--tidy-expand", "--tidy-confirm"]),
             ("junk", ["--tidy-section", "junk"]),
             ("dup", ["--tidy-section", "dup"]),
             ("title", ["--tidy-section", "title"]),
             ("ignored", ["--tidy-section", "ignored"])]
    for name, extra in shots:
        _shot(path, tmp_path / f"{name}-day.png", "day", extra)
    _shot(path, tmp_path / "missing-night.png", "night", ["--tidy-section", "missing", "--tidy-expand"])


def _heard(notes, window):
    return "\n".join(notes + [window.status.message.text() or "", window.last_message or ""])


def _shelf(window):
    return {work.title: work for work in window.library_page.works}


def test_partial_remove_drops_finished_ids_and_reloads(app, window, tmp_path, monkeypatch):
    from galpanel2.core import play
    from galpanel2.core.batch import QueueStore
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    dialogs = _Dialogs(monkeypatch)
    dialogs.mode = "accept"
    enter(app, window, path)
    ids = spec["ids"]
    alpha = [ids["miss_a"], ids["miss_b"]]
    beta = [ids["miss_c"], ids["miss_d"]]
    QueueStore(window.lib.path, window.lib.uuid).start(alpha + beta)
    tidy.DismissStore(window.lib.path, window.lib.uuid).add("junk", ids["miss_a"])
    play.start_session(window.lib, ids["miss_c"], None)
    box_near(window, "Alpha").click()
    app.processEvents()
    box_near(window, "Beta").click()
    app.processEvents()
    window.library_page.works.clear()
    click_button(app, window, "移除勾选")
    wait(app, window)
    blob = _heard(notes, window)
    assert "正在游玩，先关掉游戏再整理" in blob
    assert "移除了 2 部" in blob
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪甲") == []
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪乙") == []
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪丙")
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪丁")
    queued = json.loads(Path(QueueStore(str(path), spec["uuid"]).path).read_text(encoding="utf-8"))
    assert ids["miss_a"] not in queued["todo"] and ids["miss_b"] not in queued["todo"]
    assert ids["miss_c"] in queued["todo"] and ids["miss_d"] in queued["todo"]
    left = tidy.DismissStore(str(path), spec["uuid"]).load()
    assert ids["miss_a"] not in left["junk"]
    shelf = _shelf(window)
    assert "失踪甲" not in shelf and "失踪乙" not in shelf
    assert "失踪丙" in shelf and "失踪丁" in shelf
    shown = page_text(window)
    assert "Alpha" not in shown and "Beta" in shown


def test_remove_failure_reloads_tidy_and_shelf(app, window, tmp_path, monkeypatch):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    dialogs = _Dialogs(monkeypatch)
    dialogs.mode = "accept"
    def boom(*_args, **_kwargs):
        raise RuntimeError("tidy-boom")
    monkeypatch.setattr("galpanel2.ui.tidy.tidy.remove_missing_group", boom)
    enter(app, window, path)
    box_near(window, "Alpha").click()
    app.processEvents()
    window.library_page.works.clear()
    click_button(app, window, "移除勾选")
    wait(app, window)
    assert "tidy-boom" in _heard(notes, window)
    assert _q(path, "SELECT id FROM works WHERE title=?", "失踪甲")
    assert "失踪甲" in _shelf(window)
    assert "Alpha" in page_text(window)


def test_partial_selection_leaves_files_that_came_back(app, window, tmp_path, monkeypatch):
    path = tmp_path / "back.galdb"
    folder = tmp_path / "CameBack"
    folder.mkdir()
    present = folder / "甲.zip"
    present.write_bytes(b"pk")
    absent = folder / "乙.zip"
    extra = folder / "甲-副.zip"
    lib = store.create_library(str(path))
    try:
        _work(lib, "又出现甲", [
            {"path": str(present), "kind": "archive", "availability": "missing"},
            {"path": str(extra), "kind": "archive", "availability": "missing"},
        ])
        _work(lib, "仍失踪乙", [{"path": str(absent), "kind": "archive", "availability": "missing"}])
    finally:
        lib.close()
    notes = hear(window)
    dialogs = _Dialogs(monkeypatch)
    dialogs.mode = "accept"
    enter(app, window, path)
    expand_until(app, window, "CameBack", "又出现甲")
    click_button(app, window, "全不选")
    box_near(window, "又出现甲").click()
    app.processEvents()
    click_button(app, window, "移除勾选")
    wait(app, window)
    blob = _heard(notes, window)
    assert "1 部文件又出现了，没有移除" in blob
    assert _q(path, "SELECT id FROM works WHERE title=?", "又出现甲")
    assert _q(path, "SELECT id FROM works WHERE title=?", "仍失踪乙")
    assert _q(path, "SELECT count(*) FROM ignored_paths WHERE path LIKE ?", "%甲.zip") == [(0,)]
    notes.clear()
    box_near(window, "仍失踪乙").click()
    app.processEvents()
    click_button(app, window, "移除勾选")
    wait(app, window)
    assert _q(path, "SELECT id FROM works WHERE title=?", "仍失踪乙") == []
    assert _q(path, "SELECT id FROM works WHERE title=?", "又出现甲")


def test_hide_conflict_keeps_earlier_hides_and_reloads(app, window, tmp_path, monkeypatch):
    from galpanel2.core.domain import WriteConflict
    from galpanel2.core import repository as repo
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    real = repo.edit_work
    state = {"n": 0}
    def wrapped(lib, wid, revision, **changes):
        state["n"] += 1
        if state["n"] >= 2:
            raise WriteConflict("Work changed or was removed")
        return real(lib, wid, revision, **changes)
    monkeypatch.setattr("galpanel2.ui.tidy.repo.edit_work", wrapped)
    enter(app, window, path)
    goto(app, window, "NOT A GAME", "是游戏")
    box_near(window, "补丁工具包").click()
    app.processEvents()
    box_near(window, "小文件样本").click()
    app.processEvents()
    window.library_page.works.clear()
    click_button(app, window, "隐藏勾选")
    wait(app, window)
    blob = _heard(notes, window)
    assert "已隐藏 1 部" in blob
    assert "有作品刚被改过" in blob
    assert _q(path, "SELECT hidden FROM works WHERE title=?", "补丁工具包") == [(1,)]
    assert _q(path, "SELECT hidden FROM works WHERE title=?", "小文件样本") == [(0,)]
    shelf = _shelf(window)
    assert shelf["补丁工具包"].hidden
    assert not shelf["小文件样本"].hidden


def test_playing_blocks_hide_retitle_summary_and_undo(app, window, tmp_path):
    from galpanel2.core import play
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    enter(app, window, path)
    play.start_session(window.lib, spec["ids"]["junk_hide"], None)
    goto(app, window, "NOT A GAME", "隐藏")
    click_text(app, window, "隐藏", "补丁工具包")
    wait(app, window)
    assert "正在游玩，先关掉游戏再整理" in _heard(notes, window)
    assert _q(path, "SELECT hidden FROM works WHERE title=?", "补丁工具包") == [(0,)]
    notes.clear()
    goto(app, window, "TITLES", "的书名")
    click_button(app, window, "的书名")
    wait(app, window)
    assert "正在游玩，先关掉游戏再整理" in _heard(notes, window)
    assert _q(path, "SELECT title FROM works WHERE id=?", spec["ids"]["clean"]) == [("Eternum-0.9.5-pc",)]
    notes.clear()
    goto(app, window, "LEGACY", "用旧简介")
    click_text(app, window, "用旧简介", "待用简介")
    wait(app, window)
    assert "正在游玩，先关掉游戏再整理" in _heard(notes, window)
    assert _q(path, "SELECT summary_source FROM work_info WHERE work_id=?", spec["ids"]["sum"]) == []
    notes.clear()
    goto(app, window, "IGNORED", "撤销")
    click_text(app, window, "撤销", "exact-file")
    wait(app, window)
    assert "正在游玩，先关掉游戏再整理" in _heard(notes, window)
    assert _q(path, "SELECT count(*) FROM ignored_paths WHERE path LIKE ?", "%exact-file") == [(1,)]


def test_empty_title_names_the_current_book(app, window, tmp_path):
    path, spec = build_shelf(tmp_path)
    notes = hear(window)
    enter(app, window, path)
    goto(app, window, "TITLES", "上层文件夹")
    click_button(app, window, "上层文件夹")
    window.pages.currentWidget().title_edits[spec["ids"]["clean"]] = "   "
    click_button(app, window, "的书名")
    app.processEvents()
    blob = _heard(notes, window)
    assert "「Eternum-0.9.5-pc」的新书名是空的" in blob
    assert "第 1 行书名是空的" not in blob
    assert _q(path, "SELECT title FROM works WHERE id=?", spec["ids"]["clean"]) == [("Eternum-0.9.5-pc",)]


def test_merge_note_matches_merge_rules(app, window, tmp_path):
    from galpanel2.ui.tidy import _merge_note, load_snapshot
    def work(wid, **kw):
        row = {"id": wid, "developer": "", "links": {}, "summary": None, "summary_source": None, "copies": [{}]}
        row.update(kw)
        return row
    filled = _merge_note([work(1), work(2, developer="Studio")], 1)
    assert "社团主作品没有 → 用 Studio" in filled
    neither = _merge_note([work(1), work(2)], 1)
    assert "社团：都没有" in neither
    kept = _merge_note([work(1, summary=None, summary_source="user"), work(2, summary="次简介")], 1)
    assert "简介不换" in kept and "用次作品的" not in kept
    blank = _merge_note([work(1, summary="", summary_source=None), work(2, summary="次简介")], 1)
    assert "简介不换" in blank
    borrowed = _merge_note([work(1), work(2, summary="次简介", summary_source="vndb")], 1)
    assert "简介用次作品的" in borrowed
    later = _merge_note([
        work(1),
        work(2, links={"vndb": "v1"}),
        work(3, links={"vndb": "v2"}),
    ], 1)
    assert "VNDB" in later and "丢弃" in later
    both = _merge_note([
        work(1, links={"vndb": "v0"}),
        work(2, links={"dlsite": "RJ1"}),
        work(3, links={"dlsite": "RJ2", "vndb": "v9"}),
    ], 1)
    assert "VNDB" in both and "DLsite" in both and "丢弃" in both
    path = tmp_path / "merge.galdb"
    lib = store.create_library(str(path))
    try:
        first, _ = _work(lib, "同名作品", [{"path": str(tmp_path / "dup-a")}])
        second, _ = _work(lib, "同名作品", [{"path": str(tmp_path / "dup-b")}])
        third, _ = _work(lib, "同名作品", [{"path": str(tmp_path / "dup-c")}])
        with lib.transaction() as conn:
            conn.execute("INSERT INTO external_links(work_id,site,site_id,bound_at) VALUES(?,?,?,?)",
                         (second, "vndb", "v1", NOW))
            conn.execute("INSERT INTO external_links(work_id,site,site_id,bound_at) VALUES(?,?,?,?)",
                         (third, "vndb", "v2", NOW))
            conn.execute("INSERT INTO work_info(work_id,summary,summary_source,updated_at) VALUES(?,?,?,?)",
                         (first, None, "user", NOW))
    finally:
        lib.close()
    enter(app, window, path)
    snap = load_snapshot(window.lib)
    rows = {item["id"]: item for group in snap["dups"] for item in group["works"]}
    assert rows[first]["summary_source"] == "user"
    assert rows[second]["summary_source"] is None
    page = window.pages.currentWidget()
    goto(app, window, "DUPLICATES", "备份并合并")
    page.keeps[tuple(sorted((first, second, third)))] = first
    page.dup_key = tuple(sorted((first, second, third)))
    page.rebuild()
    app.processEvents()
    shown = page_text(window)
    assert "社团：都没有" in shown
    assert "简介不换" in shown
    assert "VNDB" in shown and "丢弃" in shown


def test_card_styles_do_not_border_the_text(app, window, tmp_path):
    path, spec = build_shelf(tmp_path)
    enter(app, window, path)
    def bordered(root):
        bad = []
        for widget in root.findChildren(QWidget):
            sheet = (widget.styleSheet() or "").strip()
            if "border" not in sheet:
                continue
            if sheet.startswith("#") or sheet.startswith("Q"):
                continue
            bad.append(sheet)
        return bad
    page = window.pages.currentWidget()
    assert bordered(page) == []
    nav = [widget for widget in page.findChildren(QWidget)
           if "border-radius:12px" in (widget.styleSheet() or "") and "border:none" in (widget.styleSheet() or "")]
    assert nav and all("1px solid" not in widget.styleSheet() for widget in nav)
    goto(app, window, "DUPLICATES", "备份并合并")
    assert bordered(window.pages.currentWidget()) == []
    goto(app, window, "TITLES", "上层文件夹")
    assert bordered(window.pages.currentWidget()) == []


def _settle(app, window):
    window.resize(1440, 860)
    for _ in range(4):
        app.processEvents()


def _no_hscroll(window):
    from PySide6.QtWidgets import QScrollArea
    areas = [area for area in window.pages.currentWidget().findChildren(QScrollArea) if area.isVisible()]
    assert areas, "no scroll area"
    for area in areas:
        bar = area.horizontalScrollBar()
        assert bar.maximum() == 0, (bar.maximum(), area.widget().minimumSizeHint().width(), area.viewport().width())


def _text_band(edit):
    from PySide6.QtWidgets import QStyle, QStyleOptionFrame
    edit.ensurePolished()
    opt = QStyleOptionFrame()
    edit.initStyleOption(opt)
    styled = edit.style().subElementRect(QStyle.SubElement.SE_LineEditContents, opt, edit)
    return min(edit.contentsRect().height(), styled.height())


def _title_edits_fit(window):
    page = window.pages.currentWidget()
    edits = [edit for edit in page.findChildren(QLineEdit) if edit.isVisible()]
    assert edits, "no title edits"
    ink = window.theme.roles["ink"].lower()
    card = window.theme.roles["card"].lower()
    for edit in edits:
        sheet = (edit.styleSheet() or "").lower()
        assert ink in sheet and card in sheet, sheet
        metrics = edit.fontMetrics()
        need = metrics.height()
        tight = metrics.boundingRect("VIRTUESaegis国").height()
        if 0 < tight <= metrics.lineSpacing():
            need = max(need, tight)
        assert edit.height() >= metrics.lineSpacing() + 8, (edit.height(), metrics.lineSpacing())
        assert _text_band(edit) >= need, (edit.contentsRect(), edit.height(), need, edit.styleSheet())
    _no_hscroll(window)


def test_title_edits_fit_the_font_day_and_night(app, window, tmp_path):
    path, _spec = build_shelf(tmp_path)
    enter(app, window, path)
    goto(app, window, "TITLES", "去版本号")
    _settle(app, window)
    # Default theme is now "media"; the toggle cycles media -> day -> night.
    assert window.theme.name == "media"
    _title_edits_fit(window)
    window.toggle_theme()
    _settle(app, window)
    assert window.theme.name == "day"
    _title_edits_fit(window)
    window.toggle_theme()
    _settle(app, window)
    assert window.theme.name == "night"
    _title_edits_fit(window)


def test_long_titles_paths_and_legacy_cards_do_not_scroll_sideways(app, window, tmp_path):
    import html
    path = tmp_path / "long.galdb"
    lib = store.create_library(str(path))
    miss_title = "LONGTITLE" + ("书" * 80)
    miss_dir = "LONGMISS-" + ("directory-" * 40)
    junk_title = "LONGTITLE补丁" + ("名" * 80)
    junk_dir = "LONGJUNK-" + ("segment-" * 40)
    card_title = "LEGACYCARD" + ("卡" * 80)
    raw = 'She said &quot;hi&quot; &amp; left ' + ("word " * 30)
    try:
        _work(lib, miss_title, [{"path": ROOT + "\\" + miss_dir + "\\gone.zip", "kind": "archive",
                                 "availability": "missing", "size": 20}])
        junk_id, _ = _work(lib, junk_title, [{
            "path": ROOT + "\\" + junk_dir + "\\gamefolder", "kind": "folder",
            "exe": "game.exe", "size": 11 * 1024 * 1024}])
        card_id, _ = _work(lib, card_title, [{"path": ROOT + r"\legacy\card", "kind": "folder"}])
        _leg(lib, card_id, "description", "text", raw)
        _leg(lib, card_id, "release_date", "text", "2020/9/3")
        _leg(lib, card_id, "original_title", "text", "原" * 80)
    finally:
        lib.close()
    assert junk_id
    enter(app, window, path)
    _settle(app, window)
    assert "LONGMISS" in page_text(window)
    assert miss_dir not in page_text(window)
    _no_hscroll(window)
    expand_until(app, window, "LONGMISS", "LONGTITLE")
    _settle(app, window)
    shown = page_text(window)
    assert "LONGTITLE" in shown and miss_title not in shown
    assert "整组移除，以后扫描跳过这个文件夹" in shown
    assert "…" in shown
    _no_hscroll(window)
    goto(app, window, "NOT A GAME", "LONGTITLE")
    _settle(app, window)
    shown = page_text(window)
    assert "LONGTITLE" in shown and junk_title not in shown
    assert junk_dir not in shown
    assert "…" in shown
    _no_hscroll(window)
    goto(app, window, "LEGACY", "She said")
    _settle(app, window)
    shown = page_text(window)
    labels = "\n".join(plain(lab.text()) for lab in _labels(window))
    assert card_title not in labels and "LEGACYCARD" in labels
    assert "&quot;" not in shown and "&amp;" not in shown
    assert 'She said "hi" & left' in labels
    assert ("原" * 80) not in labels
    assert "…" in labels
    _no_hscroll(window)
    click_text(app, window, "用旧简介", "She said")
    wait(app, window)
    assert _q(path, "SELECT summary, summary_source FROM work_info WHERE work_id=?", card_id) == [
        (html.unescape(raw).strip(), "user")]
    field = _q(path, "SELECT value_json FROM legacy_fields WHERE work_id=? AND column_name='description'", card_id)
    assert "&quot;" in field[0][0] and "&amp;" in field[0][0]
