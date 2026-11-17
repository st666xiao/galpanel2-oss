"""Save import UI. Web pages stay in Qt's browser; disk and downloads use jobs."""
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

from PySide6.QtCore import QObject, QStandardPaths, QSignalBlocker, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QToolButton,
    QVBoxLayout, QWidget)

from galpanel2.core import binding, saves, twodfan
from .workers import Job


# Jobs outlive a closed dialog. Their QObject receivers belong to the dialog,
# so Qt disconnects them on destruction; the thread itself belongs to the app.
_RUNNING = set()
_PROFILE = None


class _ExtractWorkspace:
    """Keep running readers' directories alive until their final disk access."""
    def __init__(self):
        self._lock = threading.Lock()
        self._paths = set()
        self._active = set()
        self._closed = False

    def add(self, path):
        with self._lock:
            self._paths.add(path)
            self._active.add(path)

    def finished(self, path):
        with self._lock:
            self._active.discard(path)
            remove = self._closed
            if remove:
                self._paths.discard(path)
        if remove:
            shutil.rmtree(path, ignore_errors=True)

    def close(self):
        with self._lock:
            self._closed = True
            paths = self._paths - self._active
            self._paths.difference_update(paths)
        for path in paths:
            shutil.rmtree(path, ignore_errors=True)


class _Delivery(QObject):
    def __init__(self, dialog, generation, done, failed):
        super().__init__(dialog)
        self.dialog, self.generation = dialog, generation
        self.done_callback, self.failed_callback = done, failed

    @Slot(object)
    def result(self, value):
        if not self.dialog._closed and self.dialog._generation == self.generation:
            self.done_callback(value)
        self.deleteLater()

    @Slot(object)
    def failed(self, error):
        if not self.dialog._closed and self.dialog._generation == self.generation:
            self.failed_callback(error)
        self.deleteLater()


class WebFetcher(QObject):
    """A persistent, unmodified browser session for 2DFan's normal challenge."""
    failed = Signal(str)

    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.callback = None
        self.view = None
        self._serial = 0
        self._started = 0
        self._challenge_started = None
        self._reading = False
        self._load_ok = None
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._poll)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(lambda: self._fail('加载 2DFan 页面超时（90 秒）'))

    def _create_view(self):
        global _PROFILE
        if self.view is not None:
            return
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
        if _PROFILE is None:
            directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)) / 'webprofile'
            directory.mkdir(parents=True, exist_ok=True)
            _PROFILE = QWebEngineProfile('galpanel2-2dfan', QApplication.instance())
            _PROFILE.setPersistentStoragePath(str(directory))
            _PROFILE.setCachePath(str(directory / 'cache'))
            _PROFILE.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.view = QWebEngineView(self.dialog.browser_area)
        self.view.setPage(QWebEnginePage(_PROFILE, self.view))
        self.view.setMinimumHeight(220)
        self.dialog.browser_area.layout().addWidget(self.view)
        self.view.loadFinished.connect(self._loaded)
        self.view.loadStarted.connect(self._loading)

    def load(self, url, callback):
        self.cancel()
        self._create_view()
        self.callback = callback
        self._started = time.monotonic()
        self._challenge_started = None
        self._reading = False
        self._load_ok = None
        self.deadline.start(90000)
        self.timer.start()
        self.view.load(QUrl(url))

    def cancel(self):
        self._serial += 1
        self.callback = None
        self.timer.stop()
        self.deadline.stop()
        if self.view is not None:
            self.view.stop()

    def _loading(self):
        self._load_ok = None

    def _loaded(self, ok):
        if not self.callback or self.view.page().isLoading():
            return
        # HTTP 403 may still contain the real Cloudflare verification page.
        # Inspect that HTML before deciding that a failed load is an error.
        self._load_ok = ok
        self._poll()

    def _poll(self):
        if not self.callback or self._reading:
            return
        if time.monotonic() - self._started >= 90:
            self._fail('加载 2DFan 页面超时（90 秒）')
            return
        # Do not parse the previous page while navigation is in progress.
        if self.view.page().isLoading() or self._load_ok is None:
            return
        self._reading = True
        serial = self._serial
        self.view.page().toHtml(lambda html: self._html(serial, html))

    def _html(self, serial, html):
        if serial != self._serial or not self.callback or self.dialog._closed:
            return
        self._reading = False
        if self._load_ok is None:
            return  # Navigation began while an older toHtml call was pending.
        if twodfan.is_challenge(html):
            if self._challenge_started is None:
                self._challenge_started = time.monotonic()
                self.dialog.status.setText('2DFan 正在进行人机验证…')
            if time.monotonic() - self._challenge_started >= 20:
                self.dialog.reveal_browser()
                self.dialog.status.setText('2DFan 要人机验证，请在下面的网页里点一下')
            return
        if self._load_ok is False:
            self._fail('2DFan 页面加载失败，请检查网络后重试')
            return
        callback = self.callback
        final_url = self.view.url().toString()
        self.callback = None
        self.timer.stop()
        self.deadline.stop()
        callback(final_url, html)

    def _fail(self, message):
        if self.callback:
            self.cancel()
            self.failed.emit(message)


class SaveDialog(QDialog):
    def __init__(self, owner, work_id, copy, *, fetcher=None, downloader=None):
        super().__init__(owner)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.owner, self.work_id, self.copy = owner, work_id, copy
        self.lib = owner.lib
        self.env = saves.SaveEnv.current()
        self.backup_root = saves.backup_root_for(self.lib.path)
        self.downloader = downloader or twodfan.fetch_file
        self.loaded = None
        self.source = ''
        self._closed = False
        self._writing = False
        self._generation = 0
        self._step = ''
        self._request_serial = 0
        self._page_timers = []
        self._extract_workspace = _ExtractWorkspace()
        self.setObjectName('saveDialog')
        self.setWindowTitle('导入解锁存档')
        self.resize(800, 850)
        self.setAcceptDrops(True)
        colors = owner.theme.roles
        self.setStyleSheet(f'''
            QDialog#saveDialog {{ background:{colors['paper']}; color:{colors['ink']}; }}
            QLabel, QToolButton {{ color:{colors['ink']}; }}
            QListWidget, QLineEdit {{ background:{colors['card']}; color:{colors['ink']};
                border:1px solid {colors['line']}; padding:5px; }}
            QPushButton {{ background:{colors['chip']}; color:{colors['ink']}; padding:7px 12px;
                border:1px solid {colors['line']}; border-radius:5px; }}
            QPushButton:disabled {{ color:{colors['faint']}; }}
        ''')
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('来源'))
        self.sources = QListWidget()
        self.sources.setObjectName('saveSources')
        self.sources.setMaximumHeight(135)
        layout.addWidget(self.sources)
        self.summary = QLabel('请选择存档来源')
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        self.hints_label = QLabel()
        self.hints_label.setWordWrap(True)
        self.hints_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.hints_label)
        layout.addWidget(QLabel('放到哪'))
        self.targets = QListWidget()
        self.targets.setObjectName('saveTargets')
        self.targets.setMaximumHeight(125)
        layout.addWidget(self.targets)
        target_row = QHBoxLayout()
        self.target_edit = QLineEdit()
        self.target_edit.setObjectName('saveTargetEdit')
        self.target_edit.setPlaceholderText('粘贴路径或存档位置说明，按回车加入候选')
        target_row.addWidget(self.target_edit, 1)
        self.browse_target = QPushButton('选择文件夹…')
        self.browse_target.clicked.connect(self.choose_target)
        target_row.addWidget(self.browse_target)
        layout.addLayout(target_row)
        self.apply_button = QPushButton('应用存档')
        self.apply_button.setObjectName('saveApply')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply)
        layout.addWidget(self.apply_button)
        layout.addWidget(QLabel('历史'))
        self.history = QListWidget()
        self.history.setObjectName('saveHistory')
        self.history.setMinimumHeight(100)
        layout.addWidget(self.history)
        self.reg_row = QWidget()
        reg_layout = QHBoxLayout(self.reg_row)
        reg_layout.setContentsMargins(0, 0, 0, 0)
        self.reg_label = QLabel()
        self.reg_label.setWordWrap(True)
        self.reg_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reg_label.setStyleSheet(f"color:{colors['yamabuki']}")
        reg_layout.addWidget(self.reg_label, 1)
        reg_open = QPushButton('打开所在文件夹')
        reg_open.clicked.connect(self.open_reg_folder)
        reg_layout.addWidget(reg_open)
        self.reg_row.hide()
        layout.addWidget(self.reg_row)
        self.status = QLabel()
        self.status.setObjectName('saveStatus')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.resource_button = QPushButton('在浏览器打开资源页')
        self.resource_button.hide()
        self._resource_url = ''
        self.resource_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._resource_url)))
        layout.addWidget(self.resource_button)
        self.choice_list = QListWidget()
        self.choice_list.setObjectName('saveOnlineChoices')
        self.choice_list.setMaximumHeight(95)
        self.choice_list.hide()
        self.choice_button = QPushButton('获取选中的存档')
        self.choice_button.hide()
        self._choice_callback = None
        self.choice_button.clicked.connect(self._choose_online)
        self.choice_list.itemDoubleClicked.connect(self._choose_online)
        layout.addWidget(self.choice_list)
        layout.addWidget(self.choice_button)
        self.browser_toggle = QToolButton()
        self.browser_toggle.setText('显示 2DFan 网页')
        self.browser_toggle.setCheckable(True)
        self.browser_toggle.toggled.connect(self._toggle_browser)
        layout.addWidget(self.browser_toggle)
        self.browser_area = QWidget()
        self.browser_area.setObjectName('saveBrowserArea')
        QVBoxLayout(self.browser_area).setContentsMargins(0, 0, 0, 0)
        self.browser_area.hide()
        layout.addWidget(self.browser_area)
        footer = QLabel('原存档备份目录：' + self.backup_root)
        footer.setWordWrap(True)
        footer.setTextFormat(Qt.TextFormat.PlainText)
        footer.setStyleSheet(f"color:{colors['sub']}; font-size:11px")
        layout.addWidget(footer)
        self.fetcher = fetcher or WebFetcher(self)
        if isinstance(self.fetcher, WebFetcher):
            self.fetcher.failed.connect(self._fetch_failed)
        self.titles = []
        try:
            with self.lib.connection() as connection:
                row = connection.execute('SELECT title FROM works WHERE id=?', (work_id,)).fetchone()
                if row:
                    self.titles.append(row[0])
            self.titles.extend(binding.list_titles(self.lib).get(work_id, ()))
            self.titles = list(dict.fromkeys(self.titles))
            for pack in saves.find_packs(copy.path):
                self._source_item(pack, 'bundled:' + pack.label)
        except Exception as error:
            self.status.setText('读取存档来源失败：' + str(error))
        self.sources.addItem('从 2DFan 自动获取…')
        self.sources.item(self.sources.count() - 1).setData(Qt.ItemDataRole.UserRole, '2dfan')
        self.sources.addItem('选择文件或文件夹…')
        self.sources.item(self.sources.count() - 1).setData(Qt.ItemDataRole.UserRole, 'file')
        self.sources.currentRowChanged.connect(self.select_source)
        self.targets.currentRowChanged.connect(self.update_apply)
        self.target_edit.returnPressed.connect(self.add_typed_target)
        self.reload_history()
        if self.sources.count() > 2:
            self.sources.setCurrentRow(0)

    def _source_item(self, pack, source, extra=''):
        item = QListWidgetItem(pack.label + ('（压缩包）' if pack.kind == 'archive' else ''))
        item.setData(Qt.ItemDataRole.UserRole, (pack, source, extra))
        index = self.sources.count()
        if index and self.sources.item(index - 1).data(Qt.ItemDataRole.UserRole) == 'file':
            index -= 2
        with QSignalBlocker(self.sources):
            self.sources.insertItem(index, item)
        return item

    def _job(self, task, done, step):
        delivery = _Delivery(self, self._generation, done, lambda error: self._failed(step, error))
        job = Job(lambda cancel: task(), QApplication.instance())
        _RUNNING.add(job)
        job.result.connect(delivery.result)
        job.failed.connect(delivery.failed)
        def finished():
            _RUNNING.discard(job)
            job.deleteLater()
        job.finished.connect(finished)
        job.start()

    def _failed(self, step, error):
        self._set_writing(False)
        self.status.setText(f'{step}失败：{error}')
        self.update_apply()

    def select_source(self, row):
        self._generation += 1
        self._request_serial += 1
        self._clear_page_timers()
        if hasattr(self.fetcher, 'cancel'):
            self.fetcher.cancel()
        self.loaded = None
        self.targets.clear()
        self.summary.setText('')
        self.hints_label.clear()
        self.reg_row.hide()
        self.resource_button.hide()
        self.choice_list.hide()
        self.choice_button.hide()
        self.update_apply()
        if row < 0 or self._closed:
            return
        item = self.sources.item(row)
        data = item.data(Qt.ItemDataRole.UserRole)
        if data == '2dfan':
            self._search(0)
        elif data == 'file':
            self.choose_source()
        else:
            pack, self.source, extra = data
            self.status.setText('解压中…' if pack.kind == 'archive' else '正在读取存档包…')
            extract_dir = os.path.join(self.backup_root, '_extract', str(self.work_id), uuid.uuid4().hex)
            game_dir, env = self.copy.path, self.env
            workspace = self._extract_workspace
            workspace.add(extract_dir)
            def read():
                try:
                    loaded = saves.load_pack(pack, game_dir, env, extract_dir, extra_text=extra)
                    return loaded, saves.rank_destinations(loaded, game_dir, env)
                finally:
                    workspace.finished(extract_dir)
            self._job(read, self._pack_ready, '读取存档包')

    def _pack_ready(self, result):
        self.loaded, destinations = result
        self.summary.setText(f'要拷 {len(self.loaded.files)} 个文件：\n' + '\n'.join(self.loaded.files[:5]))
        self.hints_label.setText('说明里找到的位置：\n' + '\n'.join(dict.fromkeys(h.line for h in self.loaded.hints)) if self.loaded.hints else '说明里没有找到可用位置，可手动选择目标文件夹。')
        if self.loaded.reg_files:
            self.reg_label.setText('包里有注册表文件 ' + '、'.join(Path(p).name for p in self.loaded.reg_files) + '，程序不会自动导入。需要的话点『打开所在文件夹』，自己双击导入。')
            self.reg_row.show()
        generic = {os.path.normcase(h.path) for h in self.loaded.hints if h.generic}
        for dest in destinations:
            self._add_target(dest.path, dest.reason, dest.score, os.path.normcase(dest.path) in generic)
        if self.targets.count():
            self.targets.setCurrentRow(0)
        self.status.setText('存档已载入，请确认目标目录。')
        self.update_apply()

    def _add_target(self, path, reason, score=2, generic=False):
        label = path + ' — ' + reason
        if score <= 1:
            label += '（目录还不存在）'
        if generic:
            label += '（这是大根目录，请确认）'
        for index in range(self.targets.count()):
            if os.path.normcase(self.targets.item(index).data(Qt.ItemDataRole.UserRole)) == os.path.normcase(path):
                return index
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, path)
        self.targets.addItem(item)
        return self.targets.count() - 1

    def add_typed_target(self):
        locations = saves.parse_locations(self.target_edit.text(), self.env, self.copy.path)
        if not locations:
            self.status.setText('没有识别出有效路径，请粘贴完整路径或选择文件夹。')
            return
        for loc in locations:
            row = self._add_target(loc.path, '手动输入', 2 if os.path.isdir(loc.path) else 1, loc.generic)
            self.targets.setCurrentRow(row)
        self.target_edit.clear()

    def choose_target(self):
        path = QFileDialog.getExistingDirectory(self, '选择存档目标目录', self.copy.path)
        if path:
            self.targets.setCurrentRow(self._add_target(path, '手动选择'))

    def choose_source(self):
        dialog = QFileDialog(self, '选择存档文件或文件夹', self.copy.path, '存档压缩包 (*.zip *.7z *.rar *.001)')
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        folder = QPushButton('选择当前文件夹', dialog)
        chosen = []
        def choose_folder():
            chosen.append(dialog.directory().absolutePath())
            dialog.reject()
        folder.clicked.connect(choose_folder)
        dialog.layout().addWidget(folder, dialog.layout().rowCount(), 0, 1, -1)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        path = chosen[0] if chosen else (dialog.selectedFiles()[0] if accepted and dialog.selectedFiles() else None)
        if path:
            self._manual_source(path)

    def _manual_source(self, path):
        if not os.path.isdir(path) and not saves._archive(path):
            self.status.setText('请选择存档文件夹或 zip / 7z / rar 压缩包。')
            return
        pack = saves.SavePack(path, 'folder' if os.path.isdir(path) else 'archive', Path(path).name)
        self.sources.setCurrentItem(self._source_item(pack, 'file:' + path))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self._writing:
            if any(u.isLocalFile() for u in event.mimeData().urls()):
                event.acceptProposedAction()

    def dropEvent(self, event):
        if self._writing:
            return
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self._manual_source(url.toLocalFile())
                event.acceptProposedAction()
                break

    def update_apply(self, *args):
        self.apply_button.setEnabled(bool(self.loaded and self.targets.currentItem() and not self._writing and not self.lib.readonly))

    def _set_writing(self, writing):
        self._writing = writing
        for widget in (self.sources, self.targets, self.history, self.target_edit, self.browse_target):
            widget.setEnabled(not writing)
        self.update_apply()

    def apply(self):
        item = self.targets.currentItem()
        if not self.loaded or item is None or self._writing or self.lib.readonly:
            return
        dest = item.data(Qt.ItemDataRole.UserRole)
        overwritten = sum(os.path.isfile(saves._ci_path(os.path.join(dest, *f.split('/')))) for f in self.loaded.files)
        message = (f'目标目录：{dest}\n会覆盖 {overwritten} 个文件，新增 {len(self.loaded.files) - overwritten} 个文件。\n'
                   f'原文件备份到：{self.backup_root}\n（自动创建 {self.work_id}-时间 子目录）\n确认应用存档？')
        if QMessageBox.question(self, '确认应用存档', message) != QMessageBox.StandardButton.Yes:
            return
        loaded, source, root, work_id = self.loaded, self.source, self.backup_root, self.work_id
        self._set_writing(True)
        self.status.setText('正在备份并应用存档…')
        self._job(lambda: saves.apply_pack(loaded, dest, root, work_id, source), self._applied, '应用存档')

    def _applied(self, record):
        self._set_writing(False)
        self.reload_history()
        self.status.setText('已应用，原存档备份在 ' + record.backup_dir)

    def reload_history(self):
        self.history.clear()
        try:
            for record in saves.list_applied(self.backup_root, self.work_id):
                item = QListWidgetItem()
                panel = QWidget()
                row = QHBoxLayout(panel)
                text = QLabel(f'{record.created} · {record.source}\n{record.dest}\n覆盖 {len(record.overwritten)}，新增 {len(record.added)}' + (' · 已还原' if record.restored else ''))
                text.setWordWrap(True)
                text.setTextFormat(Qt.TextFormat.PlainText)
                row.addWidget(text, 1)
                if not record.restored:
                    button = QPushButton('还原')
                    button.setEnabled(not self.lib.readonly)
                    button.clicked.connect(lambda checked=False, rec=record: self.restore_record(rec))
                    row.addWidget(button)
                item.setSizeHint(panel.sizeHint())
                self.history.addItem(item)
                self.history.setItemWidget(item, panel)
        except Exception as error:
            self.status.setText('读取存档历史失败：' + str(error))

    def restore_record(self, record):
        if self._writing or self.lib.readonly:
            return
        if QMessageBox.question(self, '确认还原', f'还原 {record.dest} 的原存档？\n新增文件只在未被游戏修改时删除。') != QMessageBox.StandardButton.Yes:
            return
        self._set_writing(True)
        self.status.setText('正在还原存档…')
        def restore():
            restored = saves.restore(record)
            data = json.loads((Path(restored.backup_dir) / 'manifest.json').read_text(encoding='utf-8'))
            return restored, data.get('kept_changed', [])
        self._job(restore, self._restored, '还原存档')

    def _restored(self, result):
        self._set_writing(False)
        self.reload_history()
        self.status.setText('已还原原存档。' + ('保留了被游戏修改的文件：' + '、'.join(result[1]) if result[1] else ''))

    def open_reg_folder(self):
        if self.loaded and self.loaded.reg_files:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.loaded.reg_files[0]).parent)))

    def _toggle_browser(self, checked):
        self.browser_area.setVisible(checked)
        self.browser_toggle.setText('收起 2DFan 网页' if checked else '显示 2DFan 网页')

    def reveal_browser(self):
        self.browser_toggle.setChecked(True)

    def _fetch_failed(self, error):
        self._request_serial += 1
        self._clear_page_timers()
        self.status.setText(self._step + '失败：' + str(error))

    def _release_page_timer(self, timer):
        if timer in self._page_timers:
            self._page_timers.remove(timer)
            timer.stop()
            timer.deleteLater()

    def _clear_page_timers(self):
        for timer in self._page_timers[:]:
            self._release_page_timer(timer)

    def _request(self, url, step, done):
        self._clear_page_timers()
        generation = self._generation
        self._request_serial += 1
        serial = self._request_serial
        self._step = step
        self.status.setText(step + '…')
        started = time.monotonic()
        deadline = QTimer(self)
        deadline.setSingleShot(True)
        self._page_timers.append(deadline)
        def valid():
            return not self._closed and generation == self._generation and serial == self._request_serial
        def timeout():
            self._release_page_timer(deadline)
            if valid():
                self._request_serial += 1
                if hasattr(self.fetcher, 'cancel'):
                    self.fetcher.cancel()
                self.status.setText(step + '失败：加载超时（90 秒）')
        deadline.timeout.connect(timeout)
        deadline.start(90000)
        def receive(final_url, html):
            if not valid():
                return
            if twodfan.is_challenge(html):
                # Injected fetchers can surface a challenge directly. The normal
                # WebFetcher polls privately for 20s before revealing its view.
                self.reveal_browser()
                self.status.setText('2DFan 要人机验证，请在下面的网页里点一下')
                if time.monotonic() - started < 90:
                    QTimer.singleShot(1500, self, retry)
                return
            self._release_page_timer(deadline)
            self._request_serial += 1  # ignore duplicate or delayed callbacks
            try:
                done(html)
            except Exception as error:
                self.status.setText(step + '失败：' + str(error))
        def retry():
            if valid():
                try:
                    self.fetcher.load(url, receive)
                except Exception as error:
                    self._fetch_failed(error)
        retry()

    def _search(self, index):
        if index >= len(self.titles):
            self.status.setText('2DFan 上没搜到这部')
            return
        def searched(html):
            subjects = twodfan.parse_search(html)
            if not subjects:
                self._search(index + 1)
                return
            subject = twodfan.pick_subject(subjects, self.titles)
            if subject:
                self._subject(subject)
            else:
                self.status.setText('请选择 2DFan 上的作品（标题 / 品牌 / 发售日）')
                self._choices([(s, f'{s.title} / {s.brand or "未知品牌"} / {s.release or "未知发售日"}') for s in subjects], self._subject, '选择这部作品')
        self._request(twodfan.search_url(self.titles[index]), '在 2DFan 搜索作品', searched)

    def _choices(self, choices, callback, button_text):
        self.choice_list.clear()
        for value, label in choices:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, value)
            self.choice_list.addItem(item)
        self.choice_list.setCurrentRow(0)
        self.choice_list.show()
        self.choice_button.setText(button_text)
        self._choice_callback = callback
        self.choice_button.show()

    def _choose_online(self, *args):
        current = self.choice_list.currentItem()
        if current and self._choice_callback:
            value = current.data(Qt.ItemDataRole.UserRole)
            self.choice_list.hide()
            self.choice_button.hide()
            self._choice_callback(value)

    def _subject(self, subject):
        def listed(html):
            items = twodfan.parse_downloads(html)
            by_id = {i.id: i for i in items}
            # Search-card saves remain candidates even when the downloads page
            # changes structure. Preserve their order after parsed save entries.
            for ident in subject.save_ids:
                by_id.setdefault(ident, twodfan.DownloadItem(ident, f'存档资源 {ident}', '存档', '', ''))
            items = list(by_id.values())
            if not items:
                self.status.setText('2DFan 上没有这部的存档')
            elif len(items) == 1:
                self._download_detail(items[0])
            else:
                self.status.setText('请选择要获取的存档')
                self._choices([(i, f'{i.title} · {i.size} · {i.excerpt}') for i in items], self._download_detail, '获取选中的存档')
        self._request(twodfan.subject_saves_url(subject.id), '读取 2DFan 存档列表', listed)

    def _download_detail(self, item):
        def detail_loaded(html):
            detail = twodfan.parse_download(html)
            if detail.direct_url is None:
                self.status.setText('2DFan 资源页没有直连下载链接，可能需要积分或登录。请在浏览器打开资源页。')
                self.resource_button.show()
                self._resource_url = twodfan.download_url(item.id)
                return
            self.status.setText('正在下载 2DFan 存档…')
            directory = os.path.join(self.backup_root, '_downloads', str(self.work_id), str(item.id))
            downloader = self.downloader
            def downloaded(path):
                pack = saves.SavePack(path, 'archive', '2DFan：' + (detail.title or item.title))
                self.sources.setCurrentItem(self._source_item(pack, f'2dfan:{item.id}', detail.description))
            self._job(lambda: downloader(detail.direct_url, directory), downloaded, '下载 2DFan 存档')
        self._request(twodfan.download_url(item.id), '读取 2DFan 资源页', detail_loaded)

    def done(self, result):
        if self._writing:
            return
        self._closed = True
        self._generation += 1
        self._request_serial += 1
        self._clear_page_timers()
        self._extract_workspace.close()
        if hasattr(self.fetcher, 'cancel'):
            self.fetcher.cancel()
        super().done(result)

    def closeEvent(self, event):
        if self._writing:
            event.ignore()
        else:
            super().closeEvent(event)
