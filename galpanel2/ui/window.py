import os
import ntpath
import shutil
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QProcess, Slot, QObject, Signal, QTimer, QEvent
from PySide6.QtGui import QPainter, QDesktopServices, QPalette
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QStackedWidget,
    QFileDialog, QMessageBox, QDialog, QCheckBox, QComboBox, QTableWidget, QTableWidgetItem, QTextEdit, QHeaderView)

from galpanel2.core import store, repository as repo, scanning, compare, launchopts, play, batch as BT, records as R
from galpanel2.core import storage, merge
from galpanel2.core.domain import LibraryLocked, Cancelled, WriteConflict, NeedsUpgrade, NotLaunchable, LaunchError
from .theme import DAY, NIGHT, MEDIA, THEMES, stylesheet, font, set_active_theme
from .widgets import Header, Button, Status, Paper, WindowControls
from .settings import Settings, default_library_path
from .frame import FrameHelper
from .workers import Job, NetJob
from .library import LibraryPage, Editor, load_projection
from .pages import TitlePage, SourcePage, ScanPage, TransferPage
from .record import RecordPage, load_record
from .launchdialog import LaunchDialog, choose_executable
from .metadialogs import BrandTidyDialog, TagManagerDialog
from .externaldialogs import FindDialog, TagMappingDialog, SettingsDialog
from .review import BatchRunner, ReviewPage
from .tidy import TidyPage
from galpanel2.launch.tracker import Tracker


class TrackingSignals(QObject):
    tick = Signal(object, int)
    ended = Signal(object, object)


class StorageSignals(QObject):
    progress = Signal(str)


class CompareDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.paths = [None, None]
        self.setWindowTitle("两库只读对比")
        self.resize(820, 630)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("按副本路径配对，只比较保留的旧字段；不合并，不推荐哪边为准。"))
        row = QHBoxLayout()
        self.labels = [QLabel("未选择 A"), QLabel("未选择 B")]
        for i in range(2):
            col = QVBoxLayout()
            col.addWidget(Button(owner, f"选择库 {'AB'[i]}", lambda checked=False, idx=i: self.choose(idx)))
            self.labels[i].setWordWrap(True)
            col.addWidget(self.labels[i])
            row.addLayout(col)
        lay.addLayout(row)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["旧字段", "不同的作品数"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table)
        self.paths_text = QTextEdit()
        self.paths_text.setReadOnly(True)
        lay.addWidget(self.paths_text)
        self.compare_button = Button(owner, "只读对比", self.run, primary=True)
        self.compare_button.setEnabled(False)
        lay.addWidget(self.compare_button)

    def choose(self, index):
        if self.owner.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择 GalPanel 2 库", "", "GalPanel 2 库 (*.galdb)")
        if path:
            self.paths[index] = path
            self.labels[index].setText(path)
        self.compare_button.setEnabled(all(self.paths))

    def run(self):
        if all(self.paths) and not self.owner.busy:
            a, b = self.paths
            self.compare_button.setEnabled(False)
            self.owner.run_job("只读对比两个库", lambda cancel: compare.compare_libraries(a, b), self.done_report, self.failed,
                               cancellable=False)

    def done_report(self, report):
        self.compare_button.setEnabled(True)
        self.table.setRowCount(len(report.field_diffs))
        for row, (field, count) in enumerate(report.field_diffs.items()):
            self.table.setItem(row, 0, QTableWidgetItem(field))
            self.table.setItem(row, 1, QTableWidgetItem(str(count)))
        self.paths_text.setPlainText(f"共同路径：{report.common}\n\n仅 A：\n" + "\n".join(report.only_a) +
                                     "\n\n仅 B：\n" + "\n".join(report.only_b))

    def failed(self, error):
        self.compare_button.setEnabled(True)
        self.paths_text.setPlainText(str(error))


def _queue_ref(text):
    if not isinstance(text, str) or ":" not in text:
        return None
    site, site_id = text.split(":", 1)
    if not site or not site_id:
        return None
    return R.Ref(site, site_id)


def _nearest_existing_dir(path):
    parent = os.path.dirname(path)
    while parent:
        if os.path.isdir(parent):
            return parent
        following = os.path.dirname(parent)
        if following == parent:
            break
        parent = following
    return None


class Window(QMainWindow):
    def __init__(self, theme=None, screenshot_mode=False):
        super().__init__()
        self.settings = Settings()
        name = theme or self.settings.data["theme"]
        self.theme = THEMES.get(name, MEDIA) if isinstance(name, str) else (name or MEDIA)
        set_active_theme(self.theme.name)
        self.show_mascot = self.settings.data["show_mascot"]
        self.screenshot_mode = screenshot_mode
        self.lib = None
        self.tracker = None
        self._stopped_tracker = None
        self._playing_title = ""
        self._early_ends = {}
        self._tracking_results = {}
        self._completed_tracker = None
        self.play_refresh = None
        self.tracking_signals = TrackingSignals(self)
        self.tracking_signals.tick.connect(self._tracker_tick, Qt.ConnectionType.QueuedConnection)
        self.tracking_signals.ended.connect(self._tracker_ended, Qt.ConnectionType.QueuedConnection)
        self.jobs = set()
        self.slot_jobs = set()
        self.net_jobs = set()
        self.file_jobs = set()
        self.find_dialog = None
        self.find_veil = None
        self.tag_mapping = None
        self.batch_runner = None
        self.active_job = None
        self.fatal_error = None
        self.last_message = ""
        self.closing = False
        self.after_load = None
        self.editor_reload = None
        self.screen = "title"
        self.native_frame = self.settings.data["native_frame"] is True
        if not self.native_frame:
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("蔵書帖 · GalPanel 2")
        self.setMinimumSize(1280, 800)
        self.resize(1440, 860)
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = Header(self)
        lay.addWidget(self.header)
        self.pages = QStackedWidget()
        self.title_page = TitlePage(self)
        self.library_page = LibraryPage(self)
        self.header.set_search(self.library_page.search)
        self.source_page = SourcePage(self)
        self.scan_page = ScanPage(self)
        self.transfer_page = TransferPage(self)
        self.record_page = RecordPage(self)
        self.review_page = ReviewPage(self)
        self.tidy_page = TidyPage(self)
        for page in (self.title_page, self.library_page, self.source_page, self.scan_page, self.transfer_page,
                     self.record_page, self.review_page, self.tidy_page):
            self.pages.addWidget(page)
        lay.addWidget(self.pages, 1)
        self.status = Status(self)
        lay.addWidget(self.status)
        self.frame_helper = FrameHelper(self) if not self.native_frame else None
        self.apply_theme()
        self.show_title()
        if not screenshot_mode:
            position = self.settings.data["window_position"]
            if position:
                x, y, w, h = position
                self.resize(max(1280, min(3000, w)), max(800, min(2000, h)))
                self.move(x, y)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            welcome = getattr(self, "title_page", None)
            header = getattr(self, "header", None)
            if welcome is not None:
                welcome.welcome_controls.sync_state()
            if header is not None:
                header.window_controls.sync_state()
            if getattr(self, "frame_helper", None) is not None:
                self.frame_helper.sync_state()
        super().changeEvent(event)

    def resizeEvent(self, event):
        if getattr(self, "frame_helper", None) is not None:
            self.frame_helper.layout()
            self.frame_helper.sync_state()
        if getattr(self, 'find_dialog', None) is not None and self.find_dialog.isVisible():
            self.find_dialog.align_to_owner()
        super().resizeEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, 'find_dialog', None) is not None and self.find_dialog.isVisible():
            self.find_dialog.align_to_owner()

    @property
    def busy(self):
        return self.active_job is not None

    @property
    def pending_jobs(self):
        return len(self.jobs) + len(self.slot_jobs) + len(self.net_jobs) + len(self.file_jobs)

    def run_net(self, label, task, callback, error_callback=None):
        if self.closing:
            raise ValueError("Window is closing")
        job = NetJob(task, self)
        job.label, job.callback, job.error_callback = label, callback, error_callback
        self.net_jobs.add(job)
        job.result.connect(self._net_result)
        job.failed.connect(self._net_error)
        job.finished.connect(self._net_finished)
        try:
            job.start()
        except Exception:
            self.net_jobs.discard(job)
            job.deleteLater()
            raise
        return job

    @Slot(object)
    def _net_result(self, value):
        job = self.sender()
        if not self.closing:
            try:
                job.callback(value)
            except Exception as error:
                if job.error_callback: job.error_callback(error)
                else: self.status.idle(str(error))

    @Slot(object)
    def _net_error(self, error):
        job = self.sender()
        if not self.closing and job.error_callback:
            job.error_callback(error)

    @Slot()
    def _net_finished(self):
        job = self.sender()
        self.net_jobs.discard(job)
        job.deleteLater()
        if self.closing and not self.net_jobs and not self.jobs:
            self.close()

    def _stop_net(self):
        jobs = [job for job in list(self.net_jobs) if job is not self.batch_runner]
        for job in jobs: job.cancel()
        deadline = time.monotonic() + 5
        for job in jobs:
            if job.isRunning(): job.wait(max(0, int((deadline - time.monotonic()) * 1000)))
        return not any(job.isRunning() for job in jobs)

    def _stop_file_checks(self):
        """Wait for quiet stat checks before the library file is released."""
        jobs = list(self.file_jobs)
        deadline = time.monotonic() + 5
        for job in jobs:
            if job.isRunning():
                job.wait(max(0, int((deadline - time.monotonic()) * 1000)))
        return not any(job.isRunning() for job in jobs)

    def _close_metadata_dialogs(self):
        for name in ('find_dialog', 'tag_mapping'):
            dialog = getattr(self, name, None)
            if dialog is not None: dialog.close()
        for job in list(self.net_jobs): job.cancel()

    def open_external(self, url):
        if not self.screenshot_mode:
            QDesktopServices.openUrl(QUrl(url))

    def open_find(self, work_id, preset=None, query=None):
        if not self.lib or self.busy: return
        if self.find_dialog is not None and self.find_dialog.isVisible():
            self.find_dialog.set_work(work_id)
        else:
            self.find_dialog = FindDialog(self, work_id)
        self.find_dialog.present()
        if query:
            self.find_dialog.query.setText(query)
            if not self.screenshot_mode:
                self.find_dialog.search()
        elif preset is None:
            self.find_dialog.search()
        else:
            self.find_dialog.show_preset(preset)

    def ask_continue_batch(self, n) -> bool:
        answer = QMessageBox.question(self, "批量找资料", f"上次的批量还有 {n} 部没搜完，继续吗？")
        return answer == QMessageBox.StandardButton.Yes

    def start_batch(self, ids, refs=None):
        if not self.lib or self.busy:
            return
        if self.lib.readonly:
            self.notify("只读打开的库不能批量绑定")
            return
        if self.batch_runner is not None and self.batch_runner.isRunning():
            self.show_review()
            return
        store = BT.QueueStore(self.lib.path, self.lib.uuid)
        existing = store.load()
        if existing and existing.get("todo"):
            if not self.ask_continue_batch(len(existing["todo"])):
                existing = None
        if not existing or not existing.get("todo"):
            work_ids = self._unbound_ids() if ids is None else list(ids)
            if not work_ids:
                self.notify("没有需要找资料的作品")
                return
            encoded = None
            if refs:
                encoded = {wid: f"{ref.site}:{ref.site_id}" for wid, ref in refs.items()}
            store.start(work_ids, encoded)
        self._launch_runner(store)

    def _unbound_ids(self):
        page = self.library_page
        return [w.id for w in sorted(page.works, key=lambda w: w.id) if not w.hidden and not page.links.get(w.id)]

    def _batch_works(self, ids, queue=None):
        raw = {}
        if queue is not None:
            stored = queue.snapshot().get("refs") or {}
            if isinstance(stored, dict):
                raw = stored
        works = {}
        for wid in ids:
            detail = self.library_page.details.get(wid)
            if detail is None:
                detail = repo.get_work(self.lib, wid)
            path = detail.copies[0].path if detail.copies else ""
            works[wid] = {"title": detail.title, "paths": [c.path for c in detail.copies], "path": path}
            ref = _queue_ref(raw.get(str(wid)))
            if ref is not None:
                works[wid]["ref"] = ref
        return works

    def _missing_todo(self, ids):
        missing = []
        for wid in ids:
            if wid in self.library_page.details:
                continue
            try:
                repo.get_work(self.lib, wid)
            except KeyError:
                missing.append(wid)
        return missing

    def _launch_runner(self, store):
        data = store.snapshot()
        todo = list(data.get("todo") or [])
        missing = self._missing_todo(todo)
        if missing:
            store.drop(missing)
            todo = list(store.snapshot().get("todo") or [])
        if missing and not todo:
            store.set_state("done")
            self.review_page.store = store
            self.review_page.refresh()
            self.status.idle(f"{len(missing)} 部已经不在书架上，从队列里拿掉了")
            return
        if data.get("state") != "running":
            store.set_state("running")
        runner = BatchRunner(store, self._batch_works(todo, store), BT.Reachability(), self)
        self.batch_runner = runner
        self.net_jobs.add(runner)
        self.review_page.store = store
        self.review_page.attach(runner)
        runner.advanced.connect(self.review_page.on_advanced)
        runner.notice.connect(self.review_page.on_notice)
        runner.finished.connect(self._batch_finished)
        runner.start()
        self.show_review()
        if missing:
            self.status.idle(f"{len(missing)} 部已经不在书架上，从队列里拿掉了")

    def _batch_finished(self):
        job = self.sender()
        self.net_jobs.discard(job)
        if self.batch_runner is job:
            self.batch_runner = None
        job.deleteLater()
        if self.closing and not self.net_jobs and not self.jobs:
            self.close()
            return
        if not self.closing:
            self.review_page.refresh()

    def _pause_batch(self):
        runner = self.batch_runner
        if runner is None or not runner.isRunning():
            return True
        runner.pause()
        runner.wait(5000)
        return not runner.isRunning()

    def show_review(self):
        if not self.lib:
            return
        self.review_page.bind_store()
        if self.batch_runner is not None and self.batch_runner.isRunning() and getattr(self.batch_runner, "reach", None) is not None:
            self.review_page.reach = self.batch_runner.reach
        self.review_page.refresh()
        self._set_page("review", self.review_page)

    def open_tag_mapping(self):
        if not self.lib or self.busy: return
        if self.tag_mapping is not None: self.tag_mapping.close()
        self.tag_mapping = TagMappingDialog(self)
        self.tag_mapping.present()
        self.tag_mapping.reload()

    def track_slot_check(self, job):
        """Count a title-slot existence check for screenshots. It must not block close."""
        self.slot_jobs.add(job)
        try:
            job.finished.connect(self._slot_job_finished)
            job.start()
        except Exception:
            self._drop_slot_job(job)
            raise

    def _drop_slot_job(self, job):
        self.slot_jobs.discard(job)
        for signal in (job.result, job.failed, job.finished):
            try:
                signal.disconnect()
            except RuntimeError:
                pass
        if not job.isRunning():
            job.deleteLater()

    def _slot_job_finished(self):
        job = self.sender()
        self.slot_jobs.discard(job)
        job.deleteLater()

    def _stop_slot_checks(self):
        for job in list(self.slot_jobs):
            job.cancel()
            self.slot_jobs.discard(job)
            for signal in (job.result, job.failed):
                try:
                    signal.disconnect()
                except RuntimeError:
                    pass
            if job.isFinished() and not job.isRunning():
                job.deleteLater()

    def notify(self, message):
        self.last_message = message
        self.status.idle(message)
        if self.screenshot_mode:
            self.fatal_error = message
        else:
            QMessageBox.information(self, "蔵書帖", message)

    def run_job(self, label, task, callback, error_callback=None, cancellable=True, *, writes_library=False):
        if self.busy:
            raise ValueError("Another operation is already running")
        job = Job(task, self)
        job.callback = callback
        job.error_callback = error_callback
        job.label = label
        job.cancellable = cancellable
        job.writes_library = writes_library
        self.jobs.add(job)
        self.active_job = job
        try:
            self.pages.setEnabled(False)
            self.header.setEnabled(False)
            self.status.busy(label, cancellable)
            job.result.connect(self._job_result)
            job.failed.connect(self._job_error)
            job.finished.connect(self._job_finished)
            job.start()
        except Exception as error:
            self.jobs.discard(job)
            self._unlock_job(job)
            job.deleteLater()
            self.status.idle(f"无法启动任务：{error}")
            raise
        return job

    def _unlock_job(self, job):
        if self.active_job is job:
            self.active_job = None
        self.pages.setEnabled(True)
        self.header.setEnabled(True)

    @Slot(object)
    def _job_result(self, value):
        job = self.sender()
        self._unlock_job(job)
        self.status.idle(job.label + " · 完成")
        if self.closing:
            if isinstance(value, store.Library):
                value.close()
            # A launch cannot be cancelled. Attach its tracker normally, then
            # let _job_finished re-enter closeEvent after this job leaves jobs.
            if job.label != "启动游戏":
                return
        try:
            job.callback(value)
        except Exception as e:
            if self.closing:
                self.last_message = str(e)
                self.status.idle(str(e))
            else:
                self.notify(str(e))

    @Slot(object)
    def _job_error(self, error):
        job = self.sender()
        self._unlock_job(job)
        message = ("已取消，库没有改变" if job.writes_library else "已取消") if isinstance(error, Cancelled) else str(error)
        self.last_message = message
        self.status.idle(message)
        if self.screenshot_mode:
            self.fatal_error = f"{type(error).__name__}: {error}"
        if job.error_callback and (not self.closing or job.label == "启动游戏"):
            job.error_callback(error)
        elif not isinstance(error, Cancelled) and not self.closing:
            self.notify(str(error))

    @Slot()
    def _job_finished(self):
        job = self.sender()
        self.jobs.discard(job)
        if self.active_job is job:
            self._unlock_job(job)
        job.deleteLater()
        if self.closing and not self.jobs:
            self.close()
        elif not self.closing and not self.busy and self.editor_reload:
            lib, select = self.editor_reload
            self.editor_reload = None
            if self.lib is lib:
                self.reload_library(select)
        elif not self.closing and not self.busy and self.play_refresh:
            lib, select, message = self.play_refresh
            self.play_refresh = None
            if self.lib is lib:
                self.refresh_play_views(select, message=message)

    def cancel_job(self):
        if self.active_job and self.active_job.cancellable:
            self.active_job.cancel()
            self.status.cancel.setEnabled(False)
            self.status.message.setText("正在取消，等待回滚完成" if self.active_job.writes_library else "正在取消")

    def _set_page(self, name, page):
        self.screen = name
        self.pages.setCurrentWidget(page)
        self.header.setVisible(name not in ("title", "transfer"))
        self.status.setVisible(name != "title")
        self.header.set_library_path(self.lib.path if self.lib else "")
        self.header.set_page(name)
        if self.header.search is not None:
            self.header.search.setVisible(name == "library")
        self.title_page.welcome_controls.sync_state()

    def auto_open(self):
        """Startup: reopen the remembered library, or refresh the welcome page."""
        recent = self.settings.data.get("recent") or []
        if recent and os.path.isfile(recent[0]):
            self.open_library_path(recent[0])
        else:
            self.show_title()

    def _create_library_file(self, path):
        """Creation logic shared by 「开始使用」 and the ⚙ 「新建库…」 action."""
        return store.create_library(path)

    def show_title(self):
        if self.busy or not self._stop_tracking("title"):
            return
        if not self._pause_batch():
            self.notify("后台找资料还在停下，请稍后再试")
            return
        self._close_metadata_dialogs()
        if self.lib:
            self.lib.close()
            self.lib = None
            self.review_page.clear_library_queue()
        self.play_refresh = None
        self.scan_page.plan = None
        self.title_page.refresh()
        self._set_page("title", self.title_page)

    def show_library(self):
        if not self.busy and self.lib:
            self.library_page.close_detail()
            self._set_page("library", self.library_page)

    def show_tidy(self):
        if not self.lib or self.busy:
            return
        self._set_page("tidy", self.tidy_page)
        self.tidy_page.reload()

    def show_sources(self):
        if not self.busy and self.lib:
            self._set_page("source", self.source_page)

    def show_scan(self):
        if not self.busy and self.lib:
            self._set_page("scan", self.scan_page)

    def show_transfer(self):
        if not self.busy:
            self._set_page("transfer", self.transfer_page)

    def choose_library(self):
        if self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择库文件", "", "GalPanel 2 库 (*.galdb)")
        if path:
            self.open_library_path(path)

    def new_library(self):
        if self.busy:
            return
        path, _ = QFileDialog.getSaveFileName(self, "选择新库位置", "", "GalPanel 2 库 (*.galdb)")
        if path:
            if not self._stop_tracking("library"):
                return
            if self.lib:
                self.lib.close()
                self.lib = None
            self.run_job("新建库", lambda cancel: self._create_library_file(path), self._opened,
                         lambda error: self._open_failed(error, path), cancellable=False)

    def open_library_path(self, path, select=None, after_load=None):
        if not path or self.busy or not self._stop_tracking("library"):
            return
        if not self._pause_batch():
            self.notify("后台找资料还在停下，请稍后再试")
            return
        self._close_metadata_dialogs()
        if self.lib:
            self.lib.close()
            self.lib = None
            self.review_page.clear_library_queue()
        self.scan_page.plan = None
        self.after_load = after_load
        self.run_job("打开库", lambda cancel: store.open_library(path, readonly=self.screenshot_mode),
                     lambda lib: self._opened(lib, select), lambda error: self._open_failed(error, path, select), cancellable=False)

    def _opened(self, lib, select=None):
        self.lib = lib
        self.status.right.setText(f"库版本 {lib.schema_version} · GalPanel 2")
        if not self.screenshot_mode:
            self.settings.remember(lib.path)
        self._set_page("library", self.library_page)
        self.reload_library(select)

    def _open_failed(self, error, path=None, select=None):
        if isinstance(error, NeedsUpgrade) and path:
            self.upgrade_from_version = error.version
            if self.confirm_upgrade(path):
                def upgraded(result):
                    message = "已升级。备份在：" + result.backup_path
                    after = self.after_load
                    def ready():
                        self.status.idle(message)
                        if after:
                            after()
                    self.open_library_path(path, select, after_load=ready)
                self.run_job("备份并升级库", lambda cancel: store.upgrade_library(path), upgraded,
                             self._open_failed, cancellable=False)
            else:
                self.after_load = None
                self.show_title()
            return
        self.show_title()
        self.notify("这个库已经在另一个窗口里打开了" if isinstance(error, LibraryLocked) else str(error))

    def reload_library(self, select=None, *, message=None):
        if not self.lib or self.busy:
            return
        lib = self.lib
        def loaded(projection):
            if select is not None and select not in projection[1] and self.screenshot_mode:
                raise KeyError(f"没有作品 {select}")
            self.library_page.set_data(projection, select)
            self.source_page.set_data(projection[3], projection[1])
            if message:
                self.status.idle(message)
            callback, self.after_load = self.after_load, None
            if callback:
                callback()
        label = message + " · 正在载入书架与封面" if message else "载入书架与封面"
        def failed(error):
            detail = "已取消" if isinstance(error, Cancelled) else "书架载入失败：" + str(error)
            text = message + " · " + detail if message else detail
            self.status.idle(text)
            if not isinstance(error, Cancelled):
                self.notify(text)
        self.run_job(label, lambda cancel: load_projection(lib, cancel), loaded, failed, cancellable=False)

    def reload_after_editor(self, select):
        if not self.lib or self.closing:
            return
        if self.busy:
            self.editor_reload = (self.lib, select)
        else:
            self.reload_library(select)

    def confirm_upgrade(self, path: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("升级库文件")
        version = getattr(self,'upgrade_from_version',None)
        if version not in (1,2,3,4,5):
            self.notify('无法确认库的版本');return False
        text = {1:'一', 2:'二', 3:'三', 4:'四', 5:'五'}[version]
        extra = '（以及游玩记录）' if version==1 else ''
        box.setText(f"这个库是第{text}版格式。要使用书架整理{extra}，需要先升级到第六版。\n"
                    f"升级前会在同一个目录存一份备份（<文件名>.v{version}-<时间>.bak）。\n"
            "升级后，第五版及更老的蔵書帖就打不开它了。")
        yes = box.addButton("备份并升级", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def confirm_stop_tracking(self, title: str, action: str) -> bool:
        words, button = {"close": ("关闭蔵書帖", "停止计时并关闭"),
                         "title": ("返回标题画面", "停止计时并返回"),
                         "library": ("打开别的库", "停止计时并打开")}[action]
        box = QMessageBox(self)
        box.setWindowTitle("还在游玩")
        box.setText(f"「{title}」还在运行。{words}会停止计时，游戏本身不会被关掉。")
        yes = box.addButton(button, QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def _stop_tracking(self, action):
        tracker = self.tracker
        if tracker is None:
            return True
        if not self.confirm_stop_tracking(self._playing_title, action):
            return False
        tracker.stop()
        tracker.join(3)
        if tracker._thread and tracker._thread.is_alive():
            # The library remains open until the final write has actually returned.
            self.status.idle("正在保存计时，请稍后再试")
            return False
        result = self._tracking_results.get(tracker)
        if result is not None:
            self._tracker_ended(tracker, result, refresh=False)
        else:
            self._stopped_tracker, self.tracker = tracker, None
        self.status.playing.clear()
        self.status.playing.hide()
        self.library_page.detail.update_start()
        return True

    def start_game(self, work_id: int, copy_id: int | None = None) -> None:
        if self.tracker is not None:
            self.notify(f"「{self._playing_title}」还在运行，先关掉它再开始另一部。")
            return
        if not self.lib or self.lib.readonly or self.busy:
            return
        lib = self.lib
        title = self.library_page.details[work_id].title if work_id in self.library_page.details else str(work_id)
        def task(cancel):
            cid = copy_id if copy_id is not None else launchopts.default_copy(lib, work_id)
            spec = launchopts.resolve_launch(lib, cid)
            if spec.work_id != work_id:
                raise ValueError("副本不属于这部作品")
            def ended(result):
                # Pure Python result storage lets confirmed close show a write error after join,
                # even before Qt delivers the queued signal. Widgets are only touched by slots.
                self._tracking_results[tracker] = result
                self.tracking_signals.ended.emit(tracker, result)
            tracker = Tracker(lib, spec,
                on_tick=lambda seconds: self.tracking_signals.tick.emit(tracker, seconds), on_end=ended)
            tracker.start()
            return tracker
        def started(tracker):
            self._playing_title = title
            self.tracker = tracker
            self.library_page.detail.update_start()
            self._tracker_tick(tracker, tracker.seconds)
            if tracker in self._early_ends:
                self._tracker_ended(tracker, self._early_ends.pop(tracker))
        self.run_job("启动游戏", task, started,
                     lambda error: self._launch_failed(error, work_id, copy_id), cancellable=False)

    def _launch_failed(self, error, work_id, copy_id):
        if self.closing:
            self.last_message = str(error)
            self.status.idle(str(error))
            return
        if not isinstance(error, NotLaunchable):
            self.notify(str(error))
            return
        work = self.library_page.details.get(work_id)
        cp = next((c for c in work.copies if c.id == copy_id), None) if work else None
        if cp is None and work and work.copies:
            cp = work.copies[0]
        messages = {"archive": "压缩包不能直接启动", "copy_missing": f"这份副本的目录不在了：{cp.path if cp else ''}",
                    "bad_exe_path": "启动文件不在这份副本的目录里，请在「启动设置」里重新选",
                    "no_copy": "这部作品没有登记副本"}
        if error.reason == "no_exe" and cp:
            relative = choose_executable(self, cp)
            if relative is not None:
                lib = self.lib
                self.run_job("保存启动文件", lambda cancel: launchopts.set_launch_options(lib, cp.id, relative,
                             launchopts.get_launch_options(lib, cp.id).args),
                             lambda _: self.start_game(work_id, cp.id), cancellable=False)
            return
        if error.reason == "exe_missing" and cp:
            lib = self.lib
            def options_read(opts):
                rel = opts.exe_relpath if opts.exe_relpath is not None else opts.scan_exe_relpath
                exe = ntpath.join(cp.path, rel or "")
                self.notify(f"启动文件不见了：{exe}。在「启动设置」里重新选一个吧。")
            self.run_job("读取启动设置", lambda cancel: launchopts.get_launch_options(lib, cp.id), options_read,
                         cancellable=False)
            return
        self.notify(messages.get(error.reason, str(error)))

    @Slot(object, int)
    def _tracker_tick(self, tracker, seconds):
        if tracker is self.tracker:
            h, remainder = divmod(seconds, 3600)
            m, s = divmod(remainder, 60)
            self.status.playing.setText(f"游玩中「{self._playing_title}」 {h}:{m:02d}:{s:02d}")
            self.status.playing.show()

    @Slot(object, object)
    def _tracker_ended(self, tracker, result, *, refresh=True):
        if tracker is self._completed_tracker:
            return
        if tracker is not self.tracker and tracker is not self._stopped_tracker:
            if self.busy and self.active_job.label == "启动游戏":
                self._early_ends[tracker] = result
            return
        self._completed_tracker = tracker
        self._tracking_results.pop(tracker, None)
        if tracker is self.tracker:
            self.tracker = None
        self._stopped_tracker = None
        self.status.playing.clear()
        self.status.playing.hide()
        self.library_page.detail.update_start()
        if isinstance(result, Exception):
            message = f"计时没能写进库：{result}。下次打开这个库时会按最后一次记录恢复。"
            if self.closing:
                self.last_message = message
                self.status.idle(message)
            else:
                self.notify(message)
            return
        message = f"「{self._playing_title}」本次游玩 {play.format_duration(result.seconds)}"
        self.status.idle(message)
        if refresh and self.lib is tracker.lib and not self.closing:
            self.refresh_play_views(message=message)

    def launch_settings(self, work_id, copy_id=None):
        if not self.lib or self.lib.readonly or self.busy:
            return
        work = self.library_page.details.get(work_id)
        if not work or not any(cp.kind == "folder" for cp in work.copies):
            return
        lib = self.lib
        def loaded(options):
            self.launch_dialog = LaunchDialog(self, work, options, copy_id)
            self.launch_dialog.open()
        self.run_job("读取启动设置", lambda cancel: {cp.id: launchopts.get_launch_options(lib,cp.id)
                     for cp in work.copies if cp.kind == "folder"}, loaded, cancellable=False)

    def show_record(self):
        if self.lib and not self.busy:
            self._set_page("record", self.record_page)
            self.reload_record()

    def reload_record(self):
        if not self.lib or self.busy:
            return
        lib = self.lib
        self.run_job("载入游玩记录", lambda cancel: load_record(lib), self.record_page.set_data, cancellable=False)

    def refresh_play_views(self, select=None, *, message=None):
        if not self.lib or self.closing:
            return
        if select is None and self.library_page.detail.work:
            select = self.library_page.detail.work.id
        if self.busy:
            self.play_refresh = (self.lib, select, message)
            return
        lib = self.lib
        def loaded(data):
            projection, record = data
            self.library_page.set_data(projection, select)
            self.source_page.set_data(projection[3], projection[1])
            self.record_page.set_data(record)
            if message:
                self.status.idle(message)
        self.run_job("刷新游玩记录", lambda cancel: (load_projection(lib,cancel), load_record(lib)), loaded,
                     cancellable=False)

    def start_scan(self, source_id):
        if not self.lib or self.busy:
            return
        lib = self.lib
        self._set_page("scan", self.scan_page)
        self.scan_page.notice.setText("正在扫描。计划完成前不会写入库。")
        self.run_job("计划扫描（尚未写入）", lambda cancel: scanning.plan_scan(lib, source_id, cancel=cancel),
                     self.scan_page.set_plan)

    def check_files(self, work_ids=None):
        if not self.lib or self.busy or self.screenshot_mode:
            return
        lib = self.lib
        def completed(result):
            message = f"检查 {result.checked} 份 · 可访问 {result.ok} · 不在 {result.missing} · 错误 {result.error}"
            self.reload_library(message=message)
        self.run_job("检查已登记副本", lambda cancel: scanning.check_availability(lib, work_ids, cancel=cancel), completed,
                     writes_library=True)

    def check_work_files(self, work_id, callback=None):
        """Quiet per-work stat check; never consumes the busy slot or reloads."""
        if not self.lib or self.lib.readonly or self.busy or self.screenshot_mode or self.closing:
            return False
        lib = self.lib
        job = Job(lambda cancel: scanning.check_work_copies(lib, work_id), self)
        job.library = lib
        job.work_id = work_id
        job.file_callback = callback
        self.file_jobs.add(job)
        job.result.connect(self._file_result)
        job.failed.connect(self._file_failed)
        job.finished.connect(self._file_finished)
        try:
            job.start()
        except Exception:
            self.file_jobs.discard(job)
            job.deleteLater()
            return False
        return True

    @Slot(object)
    def _file_result(self, states):
        job = self.sender()
        if self.closing or job.library is not self.lib or job.file_callback is None:
            return
        try:
            job.file_callback(states)
        except Exception as error:
            # The user asked for this to stay silent; keep a trace for tests
            # and for the status line's last message, not a popup.
            self.last_message = f"文件自检失败：{error}"

    @Slot(object)
    def _file_failed(self, error):
        self.last_message = f"文件自检失败：{error}"

    @Slot()
    def _file_finished(self):
        job = self.sender()
        self.file_jobs.discard(job)
        job.deleteLater()
        if self.closing and not self.file_jobs and not self.jobs:
            self.close()

    def edit_work(self, work_id, focus_notes=False):
        if not self.lib or self.busy or self.screenshot_mode:
            return
        from galpanel2.core import meta
        lib=self.lib
        def read(cancel):
            from galpanel2.core import binding
            return (repo.get_work(lib,work_id),meta.list_work_meta(lib),meta.list_brands(lib),meta.list_tags(lib),meta.list_tag_groups(lib),binding.get_info(lib,work_id))
        def loaded(data):
            work,metas,brands,tags,groups,info=data
            self.library_page.infos[work_id]=info
            dialog=Editor(self,work,focus_notes)
            dialog.brand_input.set_items(brands);dialog.tag_input.set_items(tags,groups)
            dialog.brand_input.set_ids(metas[work_id].brand_ids);dialog.tag_input.set_ids(metas[work_id].tag_ids)
            self.editor=dialog
            dialog.show()
        self.run_job('载入编辑作品',read,loaded,lambda error:self.reload_library() if isinstance(error,KeyError) else self.notify(str(error)),cancellable=False)

    def open_brand_tidy(self):
        if not self.lib or self.busy:return
        if getattr(self,'brand_tidy',None) is not None:
            self.brand_tidy.close()
        self.brand_tidy=BrandTidyDialog(self)
        self.brand_tidy.present();self.brand_tidy.reload()

    def open_tag_manager(self):
        if not self.lib or self.busy:return
        if getattr(self,'tag_manager',None) is not None:
            self.tag_manager.close()
        self.tag_manager=TagManagerDialog(self)
        self.tag_manager.present();self.tag_manager.reload()

    def hide_works(self, ids, hidden=True):
        if not ids or not self.lib or self.busy or self.screenshot_mode:
            return
        expected = {wid: self.library_page.details[wid].revision for wid in ids}
        lib = self.lib
        def task(cancel):
            with lib.transaction():
                for wid, revision in expected.items():
                    repo.edit_work(lib, wid, revision, hidden=hidden)
        self.run_job("隐藏作品" if hidden else "取消隐藏", task, lambda _: self.reload_library(),
                     self._mutation_failed, cancellable=False)

    def _mutation_failed(self, error):
        self.notify("这部作品刚被别处改过，已重新载入" if isinstance(error, WriteConflict) else str(error))
        self.reload_library()

    def remove_work(self, work_id):
        if self.tracker and self.tracker.work_id == work_id:
            self.notify("这部作品正在游玩，结束后再移除。")
            return
        if self.busy or self.screenshot_mode:
            return
        expected = self.library_page.details[work_id].revision
        if QMessageBox.question(self, "从书架移除", "只删除库里的记录，不删除磁盘上的文件。确定移除？") == QMessageBox.StandardButton.Yes:
            self.run_job("移除作品记录", lambda cancel: repo.remove_work(self.lib, work_id, expected),
                         lambda _: self.reload_library(), self._mutation_failed, cancellable=False)

    # ---------------------------------------------------------- cold storage
    @staticmethod
    def _gb(size):
        return f"{max(0, int(size or 0)) / storage.GIB:.1f}"

    def _storage_items(self, work_ids, action):
        if not self.lib:
            return []
        state = storage.list_storage(self.lib)
        items, seen = [], set()
        for work_id in work_ids:
            detail = self.library_page.details.get(work_id)
            if detail is None:
                continue
            for copy in detail.copies:
                if copy.id in seen:
                    continue
                item = state.get(copy.id)
                location = item.location if item is not None else "常用"
                if action in ("cold", "move") and location != "常用":
                    continue
                if action == "restore" and location != "冷藏":
                    continue
                if action == "delete" and location == "已删除":
                    continue
                seen.add(copy.id)
                items.append({"copy_id": copy.id, "work_id": work_id, "title": detail.title,
                              "path": copy.path, "kind": copy.kind,
                              "size_bytes": int(copy.size_bytes or 0), "location": location,
                              "source_id": copy.source_id})
        return items

    def cold_works(self, work_ids):
        self._storage_action("cold", work_ids)

    def restore_works(self, work_ids):
        self._storage_action("restore", work_ids)

    def delete_work_files(self, work_ids):
        self._storage_action("delete", work_ids)

    def _storage_action(self, action, work_ids):
        if not self.lib or self.busy or self.screenshot_mode or self.tracker is not None:
            return
        items = self._storage_items(work_ids, action)
        if not items:
            return
        chosen = self.ask_storage(action, items)
        if not chosen:
            return
        item_ids = {item["copy_id"] for item in items}
        chosen = [copy_id for copy_id in dict.fromkeys(chosen) if copy_id in item_ids]
        if not chosen:
            return
        selected = [item for item in items if item["copy_id"] in set(chosen)]
        if not selected:
            return
        lib = self.lib
        page = self.library_page
        select = page.detail.work.id if page.detail_open() and page.detail.work else None
        log_path = os.path.join(os.path.dirname(lib.path), "storage-log.jsonl")
        if action in ("cold", "restore"):
            cold_root = self.settings.data.get("cold_root") or storage.DEFAULT_COLD_ROOT
            try:
                moves = storage.plan_moves(lib, chosen, action, cold_root if action == "cold" else None)
            except storage.StorageError as error:
                QMessageBox.warning(self, "冷藏到机械盘" if action == "cold" else "搬回常用盘", str(error))
                return
            label = "冷藏" if action == "cold" else "搬回常用盘"
            signals = StorageSignals(self)
            titles = {item["path"]: item["title"] for item in selected}
            verb = "冷藏中" if action == "cold" else "搬回中"
            signals.progress.connect(self._storage_progress, Qt.ConnectionType.QueuedConnection)

            def progress(done, total, src):
                signals.progress.emit(f"{verb} {done / storage.GIB:.1f} / {total / storage.GIB:.1f} GB"
                                      f" · {titles.get(src, '')}")

            left = []

            def task(cancel):
                return storage.run_moves(lib, moves, cancel=cancel, progress=progress,
                                         log_path=log_path, left=left)

            def completed(result):
                count = len({item["work_id"] for item in selected if item["copy_id"] in set(result)})
                message = f"已冷藏 {count} 部" if action == "cold" else f"已搬回 {count} 部"
                if left:
                    message += f"；有 {len(left)} 部原文件没删干净，详见 storage-log.jsonl"
                self.reload_library(select, message=message)

            self.run_job(label, task, completed,
                         lambda error: self._storage_failed(action, chosen, select, left, error),
                         cancellable=True, writes_library=True)
            return

        def task(cancel):
            return storage.delete_copies(lib, chosen, log_path=log_path)

        def completed(result):
            count = len({item["work_id"] for item in selected if item["copy_id"] in set(result)})
            self.reload_library(select, message=f"已删除 {count} 部的游戏文件")

        self.run_job("删除游戏文件", task, completed,
                     lambda error: self._storage_failed(action, chosen, select, None, error),
                     cancellable=False, writes_library=True)

    def _storage_done_count(self, action, copy_ids):
        if not self.lib:
            return 0
        try:
            state = storage.list_storage(self.lib)
        except Exception:  # noqa: BLE001 - only used to make a message
            return 0
        wanted = {"cold": "冷藏", "restore": "常用", "delete": "已删除"}.get(action)
        if wanted is None:
            return 0
        return sum(1 for copy_id in copy_ids
                   if copy_id in state and state[copy_id].location == wanted)

    def _storage_failed(self, action, copy_ids, select, left, error):
        done = self._storage_done_count(action, copy_ids)
        self.reload_library(select)
        if isinstance(error, storage.Cancelled):
            text = f"已取消：已完成 {done} 部，其余没动"
        else:
            text = f"{error}（已完成 {done} 部，其余没动）"
        if left:
            text += f"；有 {len(left)} 部原文件没删干净，详见 storage-log.jsonl"
        self.notify(text)

    def _storage_progress(self, text):
        self.status.message.setText(text)

    # ---------------------------------------------------------- move folders
    @staticmethod
    def _source_label(source):
        return (source.label or "").strip() or source.path

    def _free_gb(self, path):
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def move_works(self, work_ids):
        """Move common copies of the selected works to another game folder."""
        if not self.lib or self.busy or self.screenshot_mode or self.tracker is not None:
            return
        items = self._storage_items(work_ids, "move")
        if not items:
            return
        target_id = self.ask_move_target(items)
        if target_id is None:
            return
        chosen = [item["copy_id"] for item in items]
        lib = self.lib
        try:
            moves = storage.plan_moves(lib, chosen, "move", target_source_id=target_id)
        except storage.StorageError as error:
            QMessageBox.warning(self, "移到其他游戏目录", str(error))
            return
        if not moves or not self.confirm_moves(moves):
            return
        target = next((source for source in repo.list_sources(lib) if source.id == target_id),
                      None)
        target_name = self._source_label(target) if target is not None else f"目录 {target_id}"
        page = self.library_page
        select = page.detail.work.id if page.detail_open() and page.detail.work else None
        log_path = os.path.join(os.path.dirname(lib.path), "storage-log.jsonl")
        signals = StorageSignals(self)
        titles = {item["path"]: item["title"] for item in items}
        work_by_copy = {item["copy_id"]: item["work_id"] for item in items}
        signals.progress.connect(self._storage_progress, Qt.ConnectionType.QueuedConnection)

        def progress(done, total, src):
            signals.progress.emit(f"移动中 {done / storage.GIB:.1f} / {total / storage.GIB:.1f} GB"
                                  f" · {titles.get(src, '')}")

        left = []

        def task(cancel):
            return storage.run_moves(lib, moves, cancel=cancel, progress=progress,
                                     log_path=log_path, left=left)

        def completed(result):
            done = set(result)
            count = len({work_by_copy[move.copy_id] for move in moves
                         if move.copy_id in done and move.copy_id in work_by_copy})
            message = f"已把 {count} 部移到 {target_name}"
            if left:
                message += f"；有 {len(left)} 部原文件没删干净，详见 storage-log.jsonl"
            self.reload_library(select, message=message)

        self.run_job("移到其他游戏目录", task, completed,
                     lambda error: self._move_failed(moves, select, left, error),
                     cancellable=True, writes_library=True)

    def ask_move_target(self, items):
        """Pick one of the other game folders; returns its source id or None."""
        if not self.lib:
            return None
        current = {item.get("source_id") for item in items if item.get("source_id") is not None}
        sources = [source for source in repo.list_sources(self.lib) if source.id not in current]
        if not sources:
            QMessageBox.information(self, "移到其他游戏目录",
                                    "库里还没有另一个游戏目录。先在「游戏目录」页添加一个。")
            return None
        dialog = QDialog(self)
        dialog.setWindowTitle("移到其他游戏目录")
        dialog.resize(680, 220)
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel(f"把 {len({item['work_id'] for item in items})} 部作品的 "
                             f"{len(items)} 份副本移到："))
        combo = QComboBox()
        for source in sources:
            free = self._free_gb(source.path)
            room = f"剩余 {self._gb(free)} GB" if free is not None else "剩余空间未知"
            label = (source.label or "").strip()
            text = f"{label + ' · ' if label else ''}{source.path}  ·  {room}"
            combo.addItem(text, source.id)
        box.addWidget(combo)
        note = QLabel("会保留游戏目录里的相对路径；冷藏中或已删除的副本不能移动。")
        note.setWordWrap(True)
        box.addWidget(note)
        box.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = Button(self, "取消", dialog.reject)
        confirm = Button(self, "下一步：确认搬移", primary=True)
        confirm.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        box.addLayout(buttons)
        if not dialog.exec():
            return None
        return combo.currentData()

    def confirm_moves(self, moves) -> bool:
        total = sum(move.size_bytes for move in moves)
        lines = [f"{move.src}\n  →  {move.dst}" for move in moves[:12]]
        if len(moves) > 12:
            lines.append(f"…… 还有 {len(moves) - 12} 份")
        box = QMessageBox(self)
        box.setWindowTitle("移到其他游戏目录")
        box.setText(f"移动 {len(moves)} 份副本，共 {self._gb(total)} GB？")
        box.setInformativeText("\n\n".join(lines))
        move = box.addButton("开始搬移", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(move)
        box.exec()
        return box.clickedButton() is move

    def _moved_count(self, moves):
        if not self.lib:
            return 0
        try:
            state = storage.list_storage(self.lib)
        except Exception:  # noqa: BLE001 - only used to make a message
            return 0
        return sum(1 for move in moves if move.copy_id in state
                   and state[move.copy_id].path == move.dst)

    def _move_failed(self, moves, select, left, error):
        done = self._moved_count(moves)
        self.reload_library(select)
        if isinstance(error, storage.Cancelled):
            text = f"已取消：已移动 {done} 份，其余没动"
        else:
            text = f"{error}（已移动 {done} 份，其余没动）"
        if left:
            text += f"；有 {len(left)} 部原文件没删干净，详见 storage-log.jsonl"
        self.notify(text)

    # ---------------------------------------------------------- merge library
    def merge_library(self, src_path):
        if not self.lib or self.busy or self.screenshot_mode or self.lib.readonly:
            return
        lib = self.lib
        select = self.library_page.detail.work.id \
            if self.library_page.detail_open() and self.library_page.detail.work else None
        signals = StorageSignals(self)
        signals.progress.connect(self._storage_progress, Qt.ConnectionType.QueuedConnection)

        def progress(done, total, title):
            signals.progress.emit(f"合并中 {done}/{total} · {title}")

        def task(cancel):
            return merge.merge_library(lib, src_path, cancel=cancel, progress=progress)

        def completed(result):
            message = (f"已合并：新增 {result.works_created} 部作品，"
                       f"{result.copies_attached} 份副本挂到已有作品，"
                       f"跳过 {result.copies_skipped} 份已存在的副本，"
                       f"新增 {result.sources_added} 个目录、{result.covers_copied} 张封面。"
                       f"备份：{result.backup_path}")
            self.reload_library(select, message=message)

        self.run_job("合并另一个库", task, completed, None,
                     cancellable=True, writes_library=True)

    def ask_storage(self, action, items):
        """Ask the user which copies to move/delete; [] or None means cancel."""
        if action not in ("cold", "restore", "delete"):
            raise ValueError(action)
        if not items:
            return []
        grouped = {}
        for item in items:
            grouped.setdefault(item["work_id"], []).append(item)
        multiple_copies = any(len(group) > 1 for group in grouped.values())
        if action == "delete":
            if len(grouped) == 1 and not multiple_copies:
                return self._ask_delete_single(items[0])
            if not multiple_copies:
                return self._ask_delete_batch(items, grouped)
            return self._ask_storage_choice(action, items, grouped)
        if multiple_copies:
            return self._ask_storage_choice(action, items, grouped)
        return self._ask_move(action, items, grouped)

    def _ask_delete_single(self, item):
        box = QMessageBox(self)
        box.setWindowTitle("删除游戏文件")
        box.setText(f"永久删除《{item['title']}》的游戏文件？")
        box.setInformativeText(f"{item['path']}\n{self._gb(item['size_bytes'])} GB\n"
                               "删了找不回。卡片、封面和游玩记录会保留，标为“已删除”。")
        delete = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        delete.setStyleSheet("color:#e2557b")
        box.exec()
        return [item["copy_id"]] if box.clickedButton() is delete else []

    def _ask_delete_batch(self, items, grouped):
        total = sum(item["size_bytes"] for item in items)
        lines = []
        for group in grouped.values():
            item = group[0]
            lines.append(f"《{item['title']}》\n{item['path']} · {self._gb(item['size_bytes'])} GB")
        box = QMessageBox(self)
        box.setWindowTitle("删除游戏文件")
        box.setText(f"永久删除以下 {len(grouped)} 部作品的游戏文件？\n\n" + "\n\n".join(lines))
        box.setInformativeText(f"合计 {self._gb(total)} GB。删了找不回。卡片、封面和游玩记录会保留，"
                               "标为“已删除”。")
        delete = box.addButton(f"全部删除（{len(grouped)} 部，{self._gb(total)} GB）",
                               QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        delete.setStyleSheet("color:#e2557b")
        box.exec()
        return [item["copy_id"] for item in items] if box.clickedButton() is delete else []

    def _ask_storage_choice(self, action, items, grouped):
        titles = {"cold": "冷藏到机械盘", "restore": "搬回常用盘", "delete": "删除游戏文件"}
        cold_root = self.settings.data.get("cold_root") or storage.DEFAULT_COLD_ROOT
        state = storage.list_storage(self.lib) if self.lib else {}
        first_target = None
        dialog = QDialog(self)
        dialog.setWindowTitle(titles[action])
        dialog.resize(780, 460)
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel("选择要对哪些副本操作："))
        checks = []
        for group in grouped.values():
            title = QLabel(f"《{group[0]['title']}》")
            title.setFont(font("body", 13, True))
            box.addWidget(title)
            for item in group:
                check = QCheckBox(f"{item['path']}  ·  {self._gb(item['size_bytes'])} GB  ·  {item['location']}")
                check.setChecked(action != "delete")
                checks.append((check, item))
                box.addWidget(check)
                if action != "delete":
                    try:
                        if action == "cold":
                            target = storage.cold_target(item["path"], cold_root)
                        else:
                            stored = state.get(item["copy_id"])
                            target = stored.home_path if stored is not None else None
                            if not target:
                                target = "（没有记录原来的位置）"
                    except storage.StorageError:
                        target = "（路径里没有 Games 段）"
                    if first_target is None:
                        first_target = target
                    direction = QLabel(f"    →  {target}")
                    direction.setWordWrap(True)
                    box.addWidget(direction)
        summary = QLabel()
        box.addWidget(summary)
        if action != "delete":
            parent = _nearest_existing_dir(first_target) if first_target else None
            try:
                free = shutil.disk_usage(parent).free if parent else None
            except OSError:
                free = None
            box.addWidget(QLabel(f"目标盘剩余 {self._gb(free)} GB" if free is not None
                                 else "无法读取目标盘剩余空间"))
        box.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        confirm = Button(self, "删除" if action == "delete" else "开始搬", primary=True)
        cancel = Button(self, "取消", dialog.reject)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        box.addLayout(buttons)

        def refresh():
            chosen = [item for check, item in checks if check.isChecked()]
            summary.setText(f"合计 {self._gb(sum(item['size_bytes'] for item in chosen))} GB")
            confirm.setEnabled(bool(chosen))

        for check, _item in checks:
            check.toggled.connect(refresh)
        confirm.clicked.connect(dialog.accept)
        refresh()
        if not dialog.exec():
            return []
        return [item["copy_id"] for check, item in checks if check.isChecked()]

    def _ask_move(self, action, items, grouped):
        cold_root = self.settings.data.get("cold_root") or storage.DEFAULT_COLD_ROOT
        state = storage.list_storage(self.lib) if self.lib else {}
        first_target = None
        dialog = QDialog(self)
        dialog.setWindowTitle("冷藏到机械盘" if action == "cold" else "搬回常用盘")
        dialog.resize(760, 420)
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel("从下面的位置搬到哪里："))
        total = 0
        for group in grouped.values():
            item = group[0]
            try:
                if action == "cold":
                    target = storage.cold_target(item["path"], cold_root)
                else:
                    stored = state.get(item["copy_id"])
                    target = stored.home_path if stored is not None else None
                    if not target:
                        target = "（没有记录原来的位置）"
            except storage.StorageError:
                target = "（路径里没有 Games 段）"
            if first_target is None:
                first_target = target
            total += item["size_bytes"]
            label = QLabel(f"《{item['title']}》\n{item['path']}  →  {target}")
            label.setWordWrap(True)
            box.addWidget(label)
        box.addWidget(QLabel(f"合计 {self._gb(total)} GB"))
        parent = _nearest_existing_dir(first_target) if first_target else None
        try:
            free = shutil.disk_usage(parent).free if parent else None
        except OSError:
            free = None
        box.addWidget(QLabel(f"目标盘剩余 {self._gb(free)} GB" if free is not None
                             else "无法读取目标盘剩余空间"))
        box.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        confirm = Button(self, "开始搬", primary=True)
        cancel = Button(self, "取消", dialog.reject)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        box.addLayout(buttons)
        confirm.clicked.connect(dialog.accept)
        if not dialog.exec():
            return []
        return [item["copy_id"] for item in items]

    def open_copy_folder(self, copy):
        if self.screenshot_mode:
            return
        if os.name == "nt" and copy.kind == "archive":
            ok, _ = QProcess.startDetached("explorer.exe", ["/select," + copy.path])
            if not ok:
                self.notify("资源管理器未能打开目录")
        else:
            folder = copy.path if copy.kind == "folder" else str(Path(copy.path).parent)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(folder)):
                self.notify("未能打开目录")

    def open_compare(self):
        if not self.busy:
            CompareDialog(self).exec()

    def make_settings_dialog(self):
        return SettingsDialog(self)

    def configure(self):
        if not self.busy:
            self.make_settings_dialog().exec()

    def toggle_theme(self):
        if not self.busy:
            order = ("media", "day", "night")
            current = self.theme.name if self.theme.name in order else "media"
            self.theme = THEMES[order[(order.index(current) + 1) % len(order)]]
            self.settings.data["theme"] = self.theme.name
            if not self.screenshot_mode:
                self.settings.save()
            self.apply_theme()

    def apply_theme(self):
        set_active_theme(self.theme.name)
        self.setFont(font())
        palette = self.palette()
        for role, color in [(QPalette.ColorRole.Window, "paper"), (QPalette.ColorRole.Base, "card"),
                            (QPalette.ColorRole.WindowText, "ink"), (QPalette.ColorRole.Text, "ink"),
                            (QPalette.ColorRole.Button, "card"), (QPalette.ColorRole.ButtonText, "ink"),
                            (QPalette.ColorRole.Highlight, "usube"), (QPalette.ColorRole.HighlightedText, "ink")]:
            palette.setColor(role, self.theme.color(color))
        self.setPalette(palette)
        self.setStyleSheet(stylesheet(self.theme))
        self.header.apply_theme()
        self.title_page.apply_theme()
        self.transfer_page.mascot.setVisible(self.show_mascot)
        self.review_page.adv.setVisible(self.show_mascot)
        if self.tidy_page.snap is not None:
            self.tidy_page.rebuild()
        if self.review_page.store is not None:
            self.review_page.refresh()
        self.library_page.detail.apply_theme()
        if self.find_dialog is not None:
            self.find_dialog.apply_theme()
        self.library_page.selection_bar.apply_theme()
        self.library_page.empty_state.apply_theme()
        self.library_page._style_batch_link()
        self.library_page.empty_image.hide()
        for widget in self.findChildren(QWidget):
            widget.update()

    def closeEvent(self, event):
        if not self._pause_batch():
            self.closing = True
            self.status.idle("正在等后台找资料停下，停下后会自动关闭")
            event.ignore()
            return
        if not self._stop_net():
            self.closing = True
            self.status.idle("正在取消联网任务，请稍后再试")
            event.ignore()
            return
        if any(job.label == "启动游戏" for job in self.jobs):
            self.closing = True
            self._stop_slot_checks()
            event.ignore()
            return
        if not self._stop_tracking("close"):
            self.closing = False
            event.ignore()
            return
        # on_end is emitted just before the timer thread exits. Even an early
        # end must finish returning before close releases its lib, including
        # retries after a deferred close's join timed out.
        completed = self._completed_tracker
        if completed is not None and completed.lib is self.lib:
            if completed._thread and completed._thread.is_alive():
                completed.join(3)
                if completed._thread.is_alive():
                    self.closing = False
                    self.status.idle("正在保存计时，请稍后再试")
                    event.ignore()
                    return
        self._stop_slot_checks()
        if not self._stop_file_checks():
            self.closing = False
            self.status.idle("正在等文件自检停下，请稍后再试")
            event.ignore()
            return
        if self.jobs:
            self.closing = True
            self.cancel_job()
            event.ignore()
            return
        if self.frame_helper is not None:
            self.frame_helper.uninstall()
        self.closing = False
        self._close_metadata_dialogs()
        if self.lib:
            self.lib.close()
            self.lib = None
        if not self.screenshot_mode:
            self.settings.data["window_position"] = [self.x(), self.y(), self.width(), self.height()]
            try:
                self.settings.save()
            except OSError:
                pass
        event.accept()
