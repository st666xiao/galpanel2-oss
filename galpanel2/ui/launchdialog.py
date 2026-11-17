"""Launch settings use lexical containment; writes run off the UI thread."""
import ntpath
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLabel,
                               QLineEdit, QFileDialog)

from galpanel2.core import launchopts
from galpanel2.core.domain import path_key
from .widgets import Button


def choose_executable(owner, copy, parent=None):
    path, _ = QFileDialog.getOpenFileName(parent or owner, "选择启动文件", copy.path, "启动文件 (*.exe)")
    if not path:
        return None
    try:
        if not path_key(path).startswith(path_key(copy.path).rstrip("\\") + "\\"):
            raise ValueError("outside copy")
        return ntpath.relpath(path, copy.path)
    except ValueError:
        owner.notify("启动文件必须在这份副本的目录里")
        return None


class LaunchDialog(QDialog):
    def __init__(self, owner, work, options, copy_id=None):
        super().__init__(owner)
        self.owner, self.lib, self.work = owner, owner.lib, work
        self.copies = [cp for cp in work.copies if cp.kind == "folder"]
        self.options = options
        self.drafts = {cid: [o.exe_relpath, o.args] for cid, o in options.items()}
        self.current_id = None
        self.saving = False
        self.setWindowTitle("启动设置")
        self.resize(640, 340)
        lay = QVBoxLayout(self)
        self.copy_box = QComboBox()
        for cp in self.copies:
            self.copy_box.addItem(cp.path, cp.id)
        self.copy_box.setVisible(len(self.copies) > 1)
        lay.addWidget(self.copy_box)
        self.exe = QLineEdit()
        self.exe.setReadOnly(True)
        self.source = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.exe, 1)
        row.addWidget(Button(owner, "选择…", self.choose))
        form = QFormLayout()
        form.addRow("启动文件", row)
        form.addRow("", self.source)
        self.args = QLineEdit()
        self.args.setMaxLength(2000)
        form.addRow("启动参数", self.args)
        lay.addLayout(form)
        lay.addWidget(Button(owner, "用扫描结果", self.use_scan))
        self.message = QLabel()
        self.message.setWordWrap(True)
        lay.addWidget(self.message)
        row = QHBoxLayout()
        row.addStretch()
        self.save_button = Button(owner, "保存", self.save, primary=True)
        self.cancel_button = Button(owner, "取消", self.reject)
        row.addWidget(self.save_button)
        row.addWidget(self.cancel_button)
        lay.addLayout(row)
        self.copy_box.currentIndexChanged.connect(self.switch)
        idx = self.copy_box.findData(copy_id)
        if idx >= 0:
            self.copy_box.setCurrentIndex(idx)
        self.switch()

    def current_copy(self):
        return self.copies[self.copy_box.currentIndex()]

    def switch(self):
        if self.current_id is not None:
            self.drafts[self.current_id][1] = self.args.text()
        self.current_id = self.current_copy().id
        self.args.setText(self.drafts[self.current_id][1])
        self.update_exe()

    def update_exe(self):
        selected = self.drafts[self.current_id][0]
        self.exe.setText((selected if selected is not None else self.options[self.current_id].scan_exe_relpath) or "")
        self.source.setText("来自扫描" if selected is None else "手动指定")

    def choose(self):
        relative = choose_executable(self.owner, self.current_copy(), self)
        if relative is not None:
            self.drafts[self.current_id][0] = relative
            self.update_exe()

    def use_scan(self):
        self.drafts[self.current_id][0] = None
        self.update_exe()

    def save(self):
        if self.saving:
            return
        if self.owner.busy:
            self.message.setText("后台任务还在进行，请稍后再保存")
            return
        cid, exe, args = self.current_id, self.drafts[self.current_id][0], self.args.text()
        self.saving = True
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.copy_box.setEnabled(False)
        try:
            self.owner.run_job("保存启动设置", lambda cancel: launchopts.set_launch_options(self.lib, cid, exe, args),
                               self.saved, self.failed, cancellable=False)
        except Exception as e:
            self.failed(e)

    def saved(self, value):
        self.saving = False
        self.accept()

    def failed(self, error):
        self.saving = False
        self.save_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.copy_box.setEnabled(True)
        self.message.setText(str(error))

    def reject(self):
        if not self.saving:
            super().reject()

    def closeEvent(self, event):
        if self.saving:
            event.ignore()
        else:
            super().closeEvent(event)
