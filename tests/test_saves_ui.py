"""Offline dialog tests; full Window integration follows test_ui's fixtures."""
import shiboken6
from dataclasses import replace
from pathlib import Path
import json
import shutil
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QWidget

from galpanel2.core import saves, store, twodfan
from galpanel2.core.domain import Copy
from galpanel2.ui import savedialog as SD
from galpanel2.ui.theme import NIGHT
from test_ui import app, wait, window  # noqa: F401

FIX = Path(__file__).parent / 'fixtures/saves'


def h(name):
    return (FIX / name).read_text(encoding='utf-8')


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.urls = pages, []
    def load(self, url, callback):
        self.urls.append(url)
        QTimer.singleShot(0, lambda: callback(url, self.pages.get(url, '<html></html>')))


def fake_downloader(url, directory):
    Path(directory).mkdir(parents=True, exist_ok=True)
    target = Path(directory) / 'save.zip'
    shutil.copy2(FIX / 'toshiuekanojo_cg_save.zip', target)
    return str(target)


def spin(app, condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.01)
    assert condition(), 'dialog did not reach the expected state'


def choose_online(dialog):
    row = next(i for i in range(dialog.sources.count()) if dialog.sources.item(i).text() == '从 2DFan 自动获取…')
    dialog.sources.setCurrentRow(row)


@pytest.fixture
def dialog_env(app, tmp_path):
    game = tmp_path / 'game'
    (game / 'savedata').mkdir(parents=True)
    (game / 'savedata/saveg.dat').write_bytes(b'old')
    (game / '全CG存档/savedata').mkdir(parents=True)
    (game / '全CG存档/savedata/saveg.dat').write_bytes(b'new')
    (game / '全CG存档/savedata/added.dat').write_bytes(b'added')
    (game / '全CG存档/unlock.reg').write_bytes(b'reg')
    copy = Copy(1, 'folder', str(game), 'game.exe', 10, 'ok', None, None, 'scan')
    with store.create_library(str(tmp_path / 'saves.galdb')) as lib:
        with lib.transaction() as c:
            c.execute('INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(1,?,?,?,?)', (str(uuid.uuid4()), '年上彼女', '2020', '2020'))
        owner = QWidget()
        owner.lib, owner.theme = lib, NIGHT
        dialogs = []
        def make(pages=None, downloader=None, fetcher=None):
            dialog = SD.SaveDialog(owner, 1, copy, fetcher=fetcher or FakeFetcher(pages or {}), downloader=downloader or fake_downloader)
            dialogs.append(dialog)
            dialog.show()
            return dialog
        yield SimpleNamespace(make=make, game=game, owner=owner, copy=copy, root=tmp_path)
        for dialog in dialogs:
            if not shiboken6.isValid(dialog):
                continue  # WA_DeleteOnClose already destroyed it
            spin(app, lambda: not dialog._writing)
            dialog.close()
        spin(app, lambda: not SD._RUNNING)
        owner.close()
        owner.deleteLater()
        app.processEvents()


def test_dialog_lists_bundled_pack_and_fixed_sources(dialog_env, app):
    dialog = dialog_env.make()
    spin(app, lambda: dialog.loaded is not None)
    assert dialog.objectName() == 'saveDialog'
    assert [dialog.sources.item(i).text() for i in range(dialog.sources.count())] == ['全CG存档', '从 2DFan 自动获取…', '选择文件或文件夹…']
    assert dialog.targets.item(0).data(Qt.ItemDataRole.UserRole) == str(dialog_env.game)
    assert dialog.apply_button.isEnabled() and '2 个文件' in dialog.summary.text()


def test_dialog_apply_history_and_restore(dialog_env, app, monkeypatch):
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog = dialog_env.make()
    spin(app, lambda: dialog.apply_button.isEnabled())
    dialog.apply_button.click()
    spin(app, lambda: dialog.history.count() == 1 and not dialog._writing)
    assert (dialog_env.game / 'savedata/saveg.dat').read_bytes() == b'new'
    assert '已应用，原存档备份在' in dialog.status.text()
    button = next(b for b in dialog.history.findChildren(QPushButton) if b.text() == '还原')
    button.click()
    spin(app, lambda: '已还原' in dialog.status.text())
    assert (dialog_env.game / 'savedata/saveg.dat').read_bytes() == b'old'
    assert not (dialog_env.game / 'savedata/added.dat').exists()
    assert saves.list_applied(dialog.backup_root, 1)[0].restored


def test_restore_message_keeps_changed_addition(dialog_env, app, monkeypatch):
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog = dialog_env.make()
    spin(app, lambda: dialog.apply_button.isEnabled())
    dialog.apply_button.click()
    spin(app, lambda: dialog.history.count() == 1 and not dialog._writing)
    (dialog_env.game / 'savedata/added.dat').write_bytes(b'played')
    next(b for b in dialog.history.findChildren(QPushButton) if b.text() == '还原').click()
    spin(app, lambda: '保留了被游戏修改' in dialog.status.text())
    assert 'added.dat' in dialog.status.text()
    assert (dialog_env.game / 'savedata/added.dat').read_bytes() == b'played'


def test_cancel_apply_does_not_write(dialog_env, app, monkeypatch):
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.No)
    dialog = dialog_env.make()
    spin(app, lambda: dialog.apply_button.isEnabled())
    dialog.apply_button.click()
    assert dialog.history.count() == 0
    assert (dialog_env.game / 'savedata/saveg.dat').read_bytes() == b'old'


def test_2dfan_flow_to_live_target_uses_worker(dialog_env, app):
    pages = {twodfan.search_url('年上彼女'): h('2dfan_search.html'),
             twodfan.subject_saves_url(18977): h('2dfan_subject_downloads.html'),
             twodfan.download_url(30930): h('2dfan_download_30930.html')}
    fetcher = FakeFetcher(pages)
    calls = []
    ui_thread = threading.get_ident()
    def downloader(url, directory):
        calls.append((url, directory, threading.get_ident()))
        return fake_downloader(url, directory)
    dialog = dialog_env.make(fetcher=fetcher, downloader=downloader)
    spin(app, lambda: dialog.loaded is not None)
    choose_online(dialog)
    spin(app, lambda: dialog.loaded is not None and dialog.source == '2dfan:30930')
    assert fetcher.urls == list(pages)
    assert calls[0][2] != ui_thread and calls[0][0].startswith('https://d1.acgxp.com/')
    assert '_downloads' in calls[0][1]
    assert dialog.targets.item(0).data(Qt.ItemDataRole.UserRole) == str(dialog_env.game / 'savedata')


def test_challenge_reveals_browser_area(dialog_env, app):
    dialog = dialog_env.make({twodfan.search_url('年上彼女'): h('2dfan_cloudflare.html')})
    spin(app, lambda: dialog.loaded is not None)
    choose_online(dialog)
    spin(app, lambda: '人机验证' in dialog.status.text())
    assert dialog.browser_area.isVisible() and dialog.browser_toggle.isChecked()


def test_challenge_then_success_resumes(dialog_env, app):
    class Fetcher(FakeFetcher):
        def load(self, url, callback):
            self.urls.append(url)
            page = h('2dfan_cloudflare.html') if len(self.urls) == 1 else '<html></html>'
            QTimer.singleShot(0, lambda: callback(url, page))
    fetcher = Fetcher({})
    dialog = dialog_env.make(fetcher=fetcher)
    choose_online(dialog)
    spin(app, lambda: '没搜到' in dialog.status.text())
    assert len(fetcher.urls) == 2


def test_search_no_result_message(dialog_env, app):
    dialog = dialog_env.make()
    choose_online(dialog)
    spin(app, lambda: '2DFan 上没搜到这部' in dialog.status.text())
    assert not dialog.apply_button.isEnabled()


def test_no_save_message(dialog_env, app):
    search = h('2dfan_search.html').replace('/downloads/30930">存档', '/downloads/30944">其他')
    dialog = dialog_env.make({twodfan.search_url('年上彼女'): search, twodfan.subject_saves_url(18977): '<html></html>'})
    choose_online(dialog)
    spin(app, lambda: '2DFan 上没有这部的存档' in dialog.status.text())


def test_reg_warning_is_explicit(dialog_env, app):
    dialog = dialog_env.make()
    spin(app, lambda: dialog.loaded is not None)
    assert dialog.reg_row.isVisible()
    assert 'unlock.reg' in dialog.reg_label.text() and '不会自动导入' in dialog.reg_label.text()
    assert any(b.text() == '打开所在文件夹' for b in dialog.reg_row.findChildren(QPushButton))


def test_manual_target_parses_description_and_generic(dialog_env, app):
    dialog = dialog_env.make()
    spin(app, lambda: dialog.loaded is not None)
    dialog.target_edit.setText('存档位置：%AppData%')
    dialog.add_typed_target()
    assert '这是大根目录，请确认' in dialog.targets.currentItem().text()
    assert dialog.targets.currentItem().data(Qt.ItemDataRole.UserRole) == dialog.env.appdata
    dialog.targets.setCurrentRow(-1)
    assert not dialog.apply_button.isEnabled()


def test_missing_direct_link_shows_browser_button(dialog_env, app):
    detail = h('2dfan_download_30930.html').replace('download-btn ', 'removed-btn ')
    dialog = dialog_env.make({twodfan.search_url('年上彼女'): h('2dfan_search.html'),
                             twodfan.subject_saves_url(18977): h('2dfan_subject_downloads.html'),
                             twodfan.download_url(30930): detail})
    choose_online(dialog)
    spin(app, lambda: '没有直连下载链接' in dialog.status.text())
    assert dialog.resource_button.isVisible() and not dialog.apply_button.isEnabled()


def test_closed_dialog_ignores_late_fetch_callback(dialog_env, app):
    class Delayed:
        def load(self, url, callback):
            self.callback = callback
    fetcher = Delayed()
    dialog = dialog_env.make(fetcher=fetcher)
    choose_online(dialog)
    dialog.close()
    fetcher.callback(twodfan.search_url('年上彼女'), h('2dfan_search.html'))
    app.processEvents()
    app.sendPostedEvents(None, 0)
    app.processEvents()
    assert not shiboken6.isValid(dialog) or dialog._closed


def test_subject_choice_requires_exact_match(dialog_env, app):
    search = h('2dfan_search.html').replace('>年上彼女</a>', '>完全不同的标题</a>')
    dialog = dialog_env.make({twodfan.search_url('年上彼女'): search})
    choose_online(dialog)
    spin(app, lambda: dialog.choice_list.isVisible())
    assert dialog.choice_list.count() == 3
    assert 'あざらしそふと' in dialog.choice_list.item(0).text()
    assert '2024-11-29' in dialog.choice_list.item(0).text()


def test_button_exists_and_archive_disabled(window, app, tmp_path):
    from test_storage_ui import _make_lib, _open
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    page.open_detail(1)
    detail = page.detail
    assert detail.saves_button.objectName() == 'detailSaves' and detail.saves_button.isEnabled()
    detail.work = replace(detail.work, copies=(replace(detail.current_copy(), kind='archive'),))
    detail.update_start()
    assert not detail.saves_button.isEnabled() and '先解压' in detail.saves_button.toolTip()


def test_button_missing_and_readonly_disabled(window, app, tmp_path):
    from test_storage_ui import _make_lib, _open
    path, cold, root = _make_lib(tmp_path)
    page = _open(window, app, path, cold)
    page.open_detail(1)
    detail = page.detail
    original = detail.work
    detail.work = replace(original, copies=(replace(detail.current_copy(), availability='missing'),))
    detail.update_start()
    assert not detail.saves_button.isEnabled() and '失效' in detail.saves_button.toolTip()
    detail.work = original
    window.lib.readonly = True
    try:
        detail.update_start()
        assert not detail.saves_button.isEnabled() and '只读' in detail.saves_button.toolTip()
    finally:
        window.lib.readonly = False


def test_default_fetcher_polls_challenge_and_continues(dialog_env, app, monkeypatch):
    from PySide6.QtCore import QUrl
    dialog = dialog_env.make()
    fetcher = SD.WebFetcher(dialog)
    fetcher.view = SimpleNamespace(url=lambda: QUrl(twodfan.search_url('年上彼女')))
    calls = []
    fetcher.callback = lambda url, html: calls.append((url, html))
    fetcher._load_ok = False  # A Cloudflare HTTP 403 is still a usable browser page.
    fetcher._html(fetcher._serial, h('2dfan_cloudflare.html'))
    assert not calls and fetcher.callback is not None
    fetcher._challenge_started = time.monotonic() - 21
    fetcher._html(fetcher._serial, h('2dfan_cloudflare.html'))
    assert dialog.browser_area.isVisible() and '人机验证' in dialog.status.text()
    fetcher._load_ok = True
    fetcher._html(fetcher._serial, h('2dfan_search.html'))
    assert len(calls) == 1 and fetcher.callback is None


def test_failed_download_reports_stage_and_has_no_target(dialog_env, app):
    pages = {twodfan.search_url('年上彼女'): h('2dfan_search.html'),
             twodfan.subject_saves_url(18977): h('2dfan_subject_downloads.html'),
             twodfan.download_url(30930): h('2dfan_download_30930.html')}
    def failed(url, directory):
        raise saves.SaveError('测试下载错误')
    dialog = dialog_env.make(pages, downloader=failed)
    choose_online(dialog)
    spin(app, lambda: '下载 2DFan 存档失败' in dialog.status.text())
    assert '测试下载错误' in dialog.status.text() and not dialog.apply_button.isEnabled()


def test_multiple_downloads_default_first_and_user_choice(dialog_env, app):
    downloads = h('2dfan_subject_downloads.html')
    start = downloads.index('<div class="download-card">', downloads.index('/downloads/30944'))
    extra = downloads[start:].replace('30930', '30931').replace('年上彼女全CG存档', '另一个存档')
    downloads = downloads.replace('</main>', extra + '</main>')
    dialog = dialog_env.make({twodfan.search_url('年上彼女'): h('2dfan_search.html'),
                             twodfan.subject_saves_url(18977): downloads})
    choose_online(dialog)
    spin(app, lambda: dialog.choice_list.isVisible())
    assert dialog.choice_list.count() == 2 and dialog.choice_list.currentRow() == 0
    dialog.choice_list.setCurrentRow(1)
    dialog.choice_button.click()
    spin(app, lambda: '没有直连下载链接' in dialog.status.text())
    assert dialog._resource_url == twodfan.download_url(30931)


def test_load_archive_is_background_and_late_result_ignored(dialog_env, app, monkeypatch):
    started, release = threading.Event(), threading.Event()
    real_load = SD.saves.load_pack
    thread_ids = []
    def delayed(*args, **kwargs):
        thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        return real_load(*args, **kwargs)
    monkeypatch.setattr(SD.saves, 'load_pack', delayed)
    dialog = dialog_env.make()
    spin(app, started.is_set)
    assert thread_ids[0] != threading.get_ident()
    dialog.close()
    release.set()
    spin(app, lambda: not SD._RUNNING)
    assert dialog._closed and dialog.loaded is None


def test_manual_archive_becomes_source_and_leaves_fixed_rows_last(dialog_env, app):
    dialog = dialog_env.make()
    spin(app, lambda: dialog.loaded is not None)
    dialog._manual_source(str(FIX / 'toshiuekanojo_cg_save.zip'))
    spin(app, lambda: dialog.loaded is not None and dialog.loaded.pack.kind == 'archive')
    assert dialog.source == 'file:' + str(FIX / 'toshiuekanojo_cg_save.zip')
    assert dialog.sources.item(dialog.sources.count() - 2).text() == '从 2DFan 自动获取…'
    assert dialog.sources.item(dialog.sources.count() - 1).text() == '选择文件或文件夹…'
    assert '（压缩包）' in dialog.sources.currentItem().text()


def _untracked_dialog(dialog_env):
    # These lifecycle tests intentionally destroy the dialog before fixture teardown.
    dialog = SD.SaveDialog(dialog_env.owner, 1, dialog_env.copy, fetcher=FakeFetcher({}))
    dialog.show()
    return dialog


def test_dialog_delete_on_close_releases_qt_children(dialog_env, app):
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid
    dialog = _untracked_dialog(dialog_env)
    spin(app, lambda: dialog.loaded is not None)
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    child = dialog.browser_area
    destroyed = []
    dialog.destroyed.connect(lambda: destroyed.append(True))
    dialog.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed and not isValid(dialog) and not isValid(child)


def test_dialog_close_removes_only_own_extract_dirs_and_keeps_downloads(dialog_env, app):
    dialog = _untracked_dialog(dialog_env)
    spin(app, lambda: dialog.loaded is not None)
    dialog._manual_source(str(FIX / 'toshiuekanojo_cg_save.zip'))
    spin(app, lambda: dialog.loaded is not None and dialog.loaded.pack.kind == 'archive')
    first = Path(dialog.loaded.root)
    dialog.sources.setCurrentRow(0)
    spin(app, lambda: dialog.loaded is not None and dialog.loaded.pack.kind == 'folder')
    dialog.sources.setCurrentRow(1)
    spin(app, lambda: dialog.loaded is not None and dialog.loaded.pack.kind == 'archive')
    second = Path(dialog.loaded.root)
    assert first != second and first.exists() and second.exists()
    extract_parent = Path(dialog.backup_root) / '_extract/1'
    other = extract_parent / 'other-dialog'
    other.mkdir(parents=True)
    (other / 'keep.dat').write_bytes(b'other')
    downloads = Path(dialog.backup_root) / '_downloads/1/99'
    downloads.mkdir(parents=True)
    (downloads / 'keep.zip').write_bytes(b'download')
    dialog.close()
    assert not first.exists() and not second.exists()
    assert sorted(p.name for p in extract_parent.iterdir()) == ['other-dialog']
    assert (other / 'keep.dat').read_bytes() == b'other'
    assert (downloads / 'keep.zip').read_bytes() == b'download'


def test_closed_destroyed_dialog_cleans_late_extraction(dialog_env, app, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid
    dialog = _untracked_dialog(dialog_env)
    spin(app, lambda: dialog.loaded is not None)
    real_load = SD.saves.load_pack
    started, release = threading.Event(), threading.Event()
    roots = []
    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(5)
        loaded = real_load(*args, **kwargs)
        roots.append(Path(loaded.root))
        return loaded
    monkeypatch.setattr(SD.saves, 'load_pack', delayed)
    dialog._manual_source(str(FIX / 'toshiuekanojo_cg_save.zip'))
    spin(app, started.is_set)
    extract_parent = Path(dialog.backup_root) / '_extract/1'
    try:
        dialog.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(dialog)
    finally:
        release.set()
    spin(app, lambda: not SD._RUNNING)
    assert roots and not roots[0].exists()
    assert not extract_parent.exists() or not list(extract_parent.iterdir())


def test_page_timers_removed_after_success_failure_timeout_and_source_change(dialog_env, app):
    class Pending:
        def load(self, url, callback):
            self.callback = callback
        def cancel(self):
            pass
    dialog = SD.SaveDialog(dialog_env.owner, 1, dialog_env.copy, fetcher=Pending())
    dialog.show()
    spin(app, lambda: dialog.loaded is not None)
    for _ in range(3):
        dialog._request('https://example.test', '测试加载', lambda html: None)
        assert len(dialog._page_timers) == 1
        dialog.fetcher.callback('https://example.test', '<html></html>')
        assert dialog._page_timers == []
    dialog._request('https://example.test', '测试加载', lambda html: None)
    dialog._fetch_failed('测试失败')
    assert dialog._page_timers == []
    dialog._request('https://example.test', '测试加载', lambda html: None)
    dialog._page_timers[0].timeout.emit()
    assert dialog._page_timers == [] and '加载超时' in dialog.status.text()
    dialog._request('https://example.test', '测试加载', lambda html: None)
    dialog.select_source(0)
    assert dialog._page_timers == []
    dialog._request('https://example.test', '测试加载', lambda html: None)
    dialog.close()
    assert dialog._page_timers == []


def test_failed_extraction_is_cleaned_when_dialog_closes(dialog_env, app, monkeypatch):
    dialog = _untracked_dialog(dialog_env)
    spin(app, lambda: dialog.loaded is not None)
    paths = []
    def broken_load(pack, game_dir, env, extract_dir, **kwargs):
        path = Path(extract_dir)
        path.mkdir(parents=True)
        (path / 'partial.dat').write_bytes(b'partial')
        paths.append(path)
        raise saves.SaveError('测试解压失败')
    monkeypatch.setattr(SD.saves, 'load_pack', broken_load)
    dialog._manual_source(str(FIX / 'toshiuekanojo_cg_save.zip'))
    spin(app, lambda: '读取存档包失败' in dialog.status.text())
    assert paths and paths[0].exists()
    dialog.close()
    assert not paths[0].exists()
