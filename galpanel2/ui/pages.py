from dataclasses import replace
import html
import os
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, Slot
from PySide6.QtGui import QPainter, QLinearGradient, QPen
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QHeaderView, QFileDialog, QMessageBox, QScrollArea, QInputDialog, QDialog,
    QTextEdit)

from galpanel2 import __version__
from galpanel2.core import repository as repo, scanning, legacy, merge
from galpanel2.core.domain import PlanConflict, Cancelled
from .widgets import Paper, Button, Adv, rounded, blossom, vertical_title, WindowControls, HeaderLogo
from .theme import asset, font
from .library import draw_image
from .workers import Job
from .settings import default_library_path
from .frame import interactive_at, start_move, toggle_maximize


class WelcomeLinkButton(Button):
    """Text-link style button used by the first-run page."""

    def __init__(self, owner, text, callback=None, parent=None):
        super().__init__(owner, text, callback, parent=parent)
        self.link = True
        self.setMinimumHeight(30)
        self.setFixedWidth(460)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(1 if self.isEnabled() else .38)
        p.setPen(t.color("beni" if self.underMouse() else "sub"))
        p.setFont(font("body", 12))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())


class TitlePage(QWidget):
    """The first-run / missing-library welcome page (screen name stays "title")."""

    WIDTH = 460

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setObjectName("titlePage")
        self.missing_path = None
        self.state = "empty"
        self.window_bar = QWidget(self)
        self.window_bar.setObjectName("welcomeBar")
        self.window_bar.setFixedHeight(56)
        bar_lay = QHBoxLayout(self.window_bar)
        bar_lay.setContentsMargins(0, 0, 0, 0)
        bar_lay.setSpacing(0)
        bar_lay.addStretch(1)
        self.welcome_controls = WindowControls(owner, self.window_bar, prefix="welcome")
        bar_lay.addWidget(self.welcome_controls)
        column = QWidget(self)
        column.setObjectName("welcomeColumn")
        column.setFixedWidth(self.WIDTH)
        lay = QVBoxLayout(column)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.logo = HeaderLogo(owner, 30, column, centered=True)
        self.logo.setObjectName("welcomeLogo")
        self.logo.setFixedHeight(46)
        self.logo.setFixedWidth(self.WIDTH)
        lay.addWidget(self.logo)
        lay.addSpacing(14)
        self.tagline = QLabel("把散在各个盘里的 galgame 收进一个书架。", column)
        self.tagline.setObjectName("welcomeTagline")
        self.tagline.setFont(font("body", 13))
        self.tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.tagline)
        lay.addSpacing(34)
        self.message = QLabel(column)
        self.message.setObjectName("welcomeMessage")
        self.message.setWordWrap(True)
        self.message.setFont(font("body", 13))
        self.message.hide()
        lay.addWidget(self.message)
        self.primary = Button(owner, "开始使用", self._primary, primary=True, parent=column)
        self.primary.setObjectName("welcomePrimary")
        self.primary.setFixedSize(self.WIDTH, 42)
        lay.addWidget(self.primary)
        lay.addSpacing(10)
        self.hint = QLabel(column)
        self.hint.setObjectName("welcomeHint")
        self.hint.setFont(font("body", 12))
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.hint)
        self.open_button = Button(owner, "打开已有的库", owner.choose_library, parent=column)
        self.open_button.setObjectName("welcomeOpen")
        self.open_button.setFixedSize(self.WIDTH, 42)
        lay.addWidget(self.open_button)
        self.new_button = Button(owner, "在默认位置新建", self._start, parent=column)
        self.new_button.setObjectName("welcomeNew")
        self.new_button.setFixedSize(self.WIDTH, 42)
        lay.addWidget(self.new_button)
        lay.addSpacing(12)
        self.import_button = WelcomeLinkButton(owner, "从旧版 GalPanel 导入 →", owner.show_transfer, column)
        self.import_button.setObjectName("welcomeImport")
        lay.addWidget(self.import_button)
        self._sync_names()

    def _sync_names(self):
        for b in (self.primary, self.open_button, self.new_button, self.import_button):
            b.setAccessibleName(b.text())

    def apply_theme(self):
        t = self.owner.theme
        self.logo.update()
        self.tagline.setStyleSheet(f"color:{t.roles['sub']};")
        self.message.setStyleSheet(f"color:{t.roles['beni']};")
        self.hint.setStyleSheet(f"color:{t.roles['sub']};")

    def refresh(self):
        """Rebuild the welcome page for the current recent-library state."""
        recent = self.owner.settings.data.get("recent") or []
        missing = bool(recent) and not os.path.isfile(recent[0])
        self.missing_path = recent[0] if missing else None
        self.state = "missing" if missing else "empty"
        default = default_library_path()
        parent = str(Path(default).parent)
        self.message.setVisible(missing)
        self.message.setText(f"上次的库找不到了：{self.missing_path}" if missing else "")
        self.primary.setText("重新找到它…" if missing else "开始使用")
        self.primary.setAccessibleName(self.primary.text())
        self.hint.setText(f"库放在 {parent}")
        self.hint.setVisible(not missing)
        self.new_button.setVisible(missing)
        self.welcome_controls.sync_state()
        self.apply_theme()

    # Kept as an alias for the older call site in show_title().
    def refresh_slots(self):
        self.refresh()

    def _primary(self):
        if self.state == "missing":
            self.owner.choose_library()
        else:
            self._start()

    def _start(self):
        owner = self.owner
        path = default_library_path()
        if os.path.isfile(path):
            owner.open_library_path(path)
            return
        try:
            os.makedirs(str(Path(path).parent), exist_ok=True)
        except OSError as error:
            owner.show_title()
            owner.notify(str(error))
            return
        owner.run_job("新建库", lambda cancel: owner._create_library_file(path),
                      owner._opened, lambda error: owner._open_failed(error, path), cancellable=False)

    def layout_children(self):
        self.window_bar.setGeometry(0, 0, self.width(), 56)
        self.welcome_controls.setVisible(not self.owner.native_frame)
        height = self.window_bar.height() + 24 + 40 + 14 + 20 + 34
        hint_height = 20 if self.hint.isVisible() else 0
        primary_height = 42 + 10 + hint_height
        open_height = 42
        new_height = (8 + 42) if self.new_button.isVisible() else 0
        total = height + primary_height + open_height + new_height + 12 + 30
        top = max(56, (self.height() - total) // 2)
        self.window_bar.raise_()
        column = self.logo.parentWidget()
        column.setGeometry((self.width() - self.WIDTH) // 2, top, self.WIDTH, total)

    def resizeEvent(self, event):
        self.layout_children()
        super().resizeEvent(event)

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton and self.owner.native_frame is False
                and not interactive_at(self, event.position().toPoint())):
            start_move(self.owner)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton and self.owner.native_frame is False
                and not interactive_at(self, event.position().toPoint())):
            toggle_maximize(self.owner)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), self.owner.theme.color("paper"))


class SourcePage(Paper):
    def __init__(self, owner):
        super().__init__(owner)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(50, 30, 50, 24)
        title = QLabel("游戏目录  GAME FOLDERS")
        title.setFont(font("title", 28, True))
        lay.addWidget(title)
        lay.addWidget(QLabel("只扫描你明确选择的目录。移除目录会保留作品与副本。"
                             "双击目录名可以给它起一个好认的名字。"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["位置 PATH", "名称 LABEL", "上次扫描 UTC", "副本数"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self._double_clicked)
        lay.addWidget(self.table, 1)
        actions = QHBoxLayout()
        actions.addWidget(Button(owner, "添加目录", self.add, primary=True))
        actions.addWidget(Button(owner, "设置名称…", self.rename))
        actions.addWidget(Button(owner, "移除目录", self.remove))
        actions.addStretch()
        actions.addWidget(Button(owner, "合并另一个库…", self.merge_another))
        actions.addWidget(Button(owner, "扫描这个目录", self.scan, primary=True, subtitle="SCAN"))
        lay.addLayout(actions)
        self.sources = []

    def set_data(self, sources, details):
        self.sources = sources
        counts = {}
        for detail in details.values():
            for cp in detail.copies:
                counts[cp.source_id] = counts.get(cp.source_id, 0) + 1
        self.table.setRowCount(len(sources))
        for row, src in enumerate(sources):
            values = (src.path, src.label or "", src.last_scan_at or "尚未扫描",
                      str(counts.get(src.id, 0)))
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
            self.table.setRowHeight(row, 56)
        if sources:
            self.table.selectRow(0)

    def selected(self):
        row = self.table.currentRow()
        return self.sources[row] if 0 <= row < len(self.sources) else None

    def _double_clicked(self, item):
        if item is not None and item.column() == 1:
            self.rename()

    def add(self):
        if self.owner.busy or self.owner.screenshot_mode:
            return
        path = QFileDialog.getExistingDirectory(self, "选择游戏根目录")
        if path:
            self.owner.run_job("添加目录", lambda cancel: repo.add_source(self.owner.lib, path),
                               lambda _: self.owner.reload_library(), cancellable=False)

    def rename(self):
        src = self.selected()
        if not src or self.owner.busy or self.owner.screenshot_mode:
            return
        label, ok = QInputDialog.getText(self, "设置目录名称", "名称（留空显示盘符或路径）：",
                                         text=src.label or "")
        if not ok:
            return
        self.set_label(src, label)

    def set_label(self, source, label):
        self.owner.run_job("设置目录名称",
                           lambda cancel: repo.set_source_label(self.owner.lib, source.id, label),
                           lambda _: self.owner.reload_library(), cancellable=False)

    def remove(self):
        src = self.selected()
        if not src or self.owner.busy or self.owner.screenshot_mode:
            return
        if QMessageBox.question(self, "移除目录", "移除这个目录？副本保留，作品不会删除。") == QMessageBox.StandardButton.Yes:
            self.owner.run_job("移除目录", lambda cancel: repo.remove_source(self.owner.lib, src.id),
                               lambda _: self.owner.reload_library(), cancellable=False)

    def scan(self):
        src = self.selected()
        if src and not self.owner.busy:
            self.owner.start_scan(src.id)

    def merge_another(self):
        if not self.owner.lib or self.owner.busy or self.owner.screenshot_mode:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择要合并的库", "",
                                               "GalPanel 2 库 (*.galdb)")
        if path:
            self.start_merge(path)

    def start_merge(self, path):
        """Plan in the background, show the preview, then ask the window to merge."""
        if not self.owner.lib or self.owner.busy:
            return
        lib = self.owner.lib

        def planned(plan):
            if self.ask_merge_plan(plan):
                self.owner.merge_library(path)

        def failed(error):
            QMessageBox.warning(self, "合并另一个库", str(error))

        self.owner.run_job("读取要合并的库",
                           lambda cancel: merge.plan_merge(lib, path),
                           planned, failed, cancellable=False)

    def ask_merge_plan(self, plan) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("合并另一个库")
        dialog.resize(760, 460)
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel(f"会新增 {plan.works_created} 部作品、"
                             f"{plan.sources_added} 个游戏目录、{plan.covers_copied} 张封面。"))
        box.addWidget(QLabel(f"挂到已有作品的副本：{plan.copies_attached} 份；"
                             f"已存在而跳过的副本：{plan.copies_skipped} 份。"))
        detail = QTextEdit()
        detail.setReadOnly(True)
        lines = []
        if plan.merged_into:
            lines.append("会合并到已有作品：")
            lines += [f"  {source}  →  {target}" for source, target in plan.merged_into]
        if plan.new_sources:
            lines.append("\n新增游戏目录：")
            lines += [f"  {path}" + (f"（{label}）" if label else "")
                      for path, label in plan.new_sources]
        if plan.covers_missing:
            lines.append(f"\n有 {plan.covers_missing} 张封面文件找不到，会跳过。")
        if not lines:
            lines.append("没有需要合并的内容。")
        detail.setPlainText("\n".join(lines))
        box.addWidget(detail, 1)
        box.addWidget(QLabel("合并前会自动备份目标库；源库文件不会被改动，也不会被删除。"))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = Button(self.owner, "取消", dialog.reject)
        confirm = Button(self.owner, "开始合并", primary=True)
        confirm.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        box.addLayout(buttons)
        return bool(dialog.exec())


SCAN_LABELS = {"new": "新发现", "same_name": "同名待判", "known": "已在书架", "missing": "没找到", "skipped": "跳过"}


class ScanPair(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.setFixedHeight(285)

    def paintEvent(self, event):
        it = self.page.selected_item()
        t = self.page.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.page.owner.show_mascot:
            pix = asset("shiori/s2-loupe.png").scaledToWidth(240, Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(self.width() - 218, 8, pix)
        if not it:
            return
        candidate = it.candidates[0] if it.candidates else None
        boxes = [(QRectF(26, 20, 118, 172), candidate, f"书架上的 No.{candidate:03d}" if candidate else "尚未登记"),
                 (QRectF(163, 20, 118, 172), None, "这次发现的")]
        for rect, wid, label in boxes:
            pix = self.page.owner.library_page.cache.get(wid) if wid else asset("jacket/jacket-04.jpg")
            rounded(p, rect.adjusted(-5, -5, 5, 5), t.color("card"), 12, t.color("line"))
            draw_image(p, rect, pix)
            if not wid:
                blossom(p, rect.center().x(), rect.top() + 29, 26, t.color("white"))
                vertical_title(p, rect.adjusted(0, 56, 0, -10), it.title, t.color("white"), 13)
            p.setFont(font("body", 10, True))
            p.setPen(t.color("beni"))
            p.drawText(QRectF(rect.left(), rect.bottom() + 10, rect.width(), 20), label)
            path = it.path
            if wid and wid in self.page.owner.library_page.details:
                detail = self.page.owner.library_page.details[wid]
                path = detail.copies[0].path if detail.copies else "没有副本"
            p.setFont(font("mono", 9))
            p.setPen(t.color("sub"))
            p.drawText(QRectF(rect.left(), rect.bottom() + 34, rect.width(), 46), Qt.TextFlag.TextWordWrap,
                       p.fontMetrics().elidedText(path, Qt.TextElideMode.ElideMiddle, 210))
        rounded(p, QRectF(138, 100, 32, 32), t.color("ai"), 16, t.color("white"), 2)
        p.setFont(font("mono", 11, True, True))
        p.setPen(t.color("white"))
        p.drawText(QRectF(138, 100, 32, 32), Qt.AlignmentFlag.AlignCenter, "VS")


class ScanPage(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.plan = None
        self.decisions = {}
        self.current_key = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = Paper(owner, wall=True)
        bar.setFixedHeight(52)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(24, 5, 24, 5)
        self.filters = {}
        all_button = Button(owner, "全部", lambda: self.filter_rows(None))
        bl.addWidget(all_button)
        for status, label in SCAN_LABELS.items():
            b = Button(owner, label + " 0", lambda checked=False, st=status: self.filter_rows(st))
            self.filters[status] = b
            bl.addWidget(b)
        bl.addStretch()
        self.root_label = QLabel("从「出所」选择一个来源")
        self.root_label.setMaximumWidth(480)
        bl.addWidget(self.root_label)
        lay.addWidget(bar)
        body = QHBoxLayout()
        body.setSpacing(0)
        ledger = Paper(owner)
        ll = QVBoxLayout(ledger)
        ll.setContentsMargins(28, 18, 28, 12)
        title = QLabel("検収  RESULT")
        title.setFont(font("title", 22, True))
        ll.addWidget(title)
        self.notice = QLabel("这次扫描还没有写入。勾选的条目会一次入库；放弃的话书架保持原样。")
        self.notice.setWordWrap(True)
        ll.addWidget(self.notice)
        totals = QHBoxLayout()
        self.totals = {}
        for st, label in SCAN_LABELS.items():
            label_widget = QLabel("0  " + label)
            label_widget.setFont(font("title", 18, True))
            label_widget.setMargin(10)
            self.totals[st] = label_widget
            totals.addWidget(label_widget)
        ll.addLayout(totals)
        self.table = QTableWidget(0, 7)
        self.table.verticalHeader().hide()
        self.table.setHorizontalHeaderLabels(["入库", "状态", "", "书名（可改）", "形态", "所在", "启动文件"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 42)
        self.table.setColumnWidth(1, 68)
        self.table.setColumnWidth(2, 48)
        self.table.setColumnWidth(4, 64)
        self.table.setColumnWidth(5, 195)
        self.table.setColumnWidth(6, 94)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self.update_choice)
        self.table.itemChanged.connect(self.update_commit_label)
        ll.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        footer.addWidget(self.error_label, 1)
        self.discard_button = Button(owner, "放弃这次扫描", self.discard)
        self.commit_button = Button(owner, "入库 0 部", self.commit, primary=True, subtitle="COMMIT")
        footer.addWidget(self.discard_button)
        footer.addWidget(self.commit_button)
        ll.addLayout(footer)
        aside = Paper(owner, wall=True)
        aside.setFixedWidth(470)
        al = QVBoxLayout(aside)
        al.setContentsMargins(0, 0, 0, 16)
        al.setSpacing(7)
        self.pair = ScanPair(self)
        al.addWidget(self.pair)
        al.addStretch()
        self.adv = Adv(owner, "扫描完成后，我来帮你逐项检収。", pose=None, height=150)
        al.addWidget(self.adv)
        scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(180)
        scroll.setMaximumHeight(220)
        self.choice_widget = QWidget()
        self.choice_layout = QVBoxLayout(self.choice_widget)
        self.choice_layout.setContentsMargins(24, 0, 24, 0)
        self.choice_layout.setSpacing(8)
        scroll.setWidget(self.choice_widget)
        al.addWidget(scroll)
        body.addWidget(ledger, 1)
        body.addWidget(aside)
        lay.addLayout(body, 1)
        self.commit_button.setEnabled(False)

    def set_plan(self, plan):
        self.plan, self.decisions = plan, {}
        self.root_label.setText(plan.root if len(plan.root) <= 72 else plan.root[:20] + "…" + plan.root[-44:])
        self.root_label.setToolTip(plan.root)
        self.table.blockSignals(True)
        self.table.setRowCount(len(plan.items))
        for row, it in enumerate(plan.items):
            eligible = it.status in ("new", "same_name")
            check = QTableWidgetItem()
            check.setFlags((Qt.ItemFlag.ItemIsUserCheckable if eligible else Qt.ItemFlag.NoItemFlags) |
                           (Qt.ItemFlag.ItemIsEnabled if it.status != "same_name" else Qt.ItemFlag.NoItemFlags))
            if it.status == "same_name":
                check.setToolTip("先在右侧选择怎么处理")
            check.setCheckState(Qt.CheckState.Checked if it.status == "new" else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            status = {"new": "NEW", "same_name": "同名?"}.get(it.status, SCAN_LABELS[it.status])
            for col, text in [(1, status), (3, it.title), (4, "文件夹" if it.kind == "folder" else "压缩包"),
                              (5, it.path), (6, it.exe_relpath or "—")]:
                item = QTableWidgetItem(text)
                item.setToolTip(text if col != 1 else it.reason)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                              (Qt.ItemFlag.ItemIsEditable if col == 3 and eligible else Qt.ItemFlag.NoItemFlags))
                self.table.setItem(row, col, item)
            thumbnail = QLabel()
            wid = it.candidates[0] if it.candidates else row + 1
            pix = self.owner.library_page.cache.get(wid) if it.candidates else asset(f"jacket/jacket-{wid % 8 + 1:02d}.jpg")
            thumbnail.setPixmap(pix.scaled(36, 54, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.table.setCellWidget(row, 2, thumbnail)
            self.table.setRowHeight(row, 72)
        self.table.blockSignals(False)
        for status, label in SCAN_LABELS.items():
            count = sum(i.status == status for i in plan.items)
            self.filters[status].setText(f"{label} {count}")
            self.totals[status].setText(f"{count}  {label}")
        self.error_label.setText(f"{len(plan.errors)} 条读取问题（详情悬停）" if plan.errors else "没找到的副本只标记，记录保留。")
        self.error_label.setToolTip("\n".join(plan.errors))
        self.notice.setText("这次扫描还没有写入。勾选的条目会一次入库；放弃的话书架保持原样。")
        index = next((i for i, it in enumerate(plan.items) if it.status == "same_name"), 0)
        if plan.items:
            self.table.selectRow(index)
        self.update_choice()
        self.update_commit_label()

    def selected_item(self):
        row = self.table.currentRow()
        return self.plan.items[row] if self.plan and 0 <= row < len(self.plan.items) else None

    def update_choice(self):
        while self.choice_layout.count():
            item = self.choice_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        it = self.selected_item()
        self.pair.update()
        if not it:
            return
        self.current_key = it.key
        if it.status == "same_name":
            self.adv.text = f"唔……书架上已经有一部「{it.title}」了。光看名字可不能下结论哦，要怎么处理呢？"
            options = [(wid, f"作为另一份文件，挂到 No.{wid:03d} 下") for wid in it.candidates]
            options += [("new", "作为新作品入库"), ("skip", "这次先不处理")]
            for action, text in options:
                b = Button(self.owner, "○  " + text, lambda checked=False, a=action, key=it.key: self.choose(key, a))
                b.setFixedHeight(44)
                b.choice = True
                b.selected = self.decisions.get(it.key) == action
                self.choice_layout.addWidget(b)
        elif it.status == "new":
            self.adv.text = "这是一条新发现。请确认书名；勾选后才会作为新作品入库。"
        else:
            self.adv.text = {"known": "已经在书架里啦。这次只刷新副本检查结果。",
                             "missing": "这份副本没找到。提交后只标记不在，作品和文件都不会删除。",
                             "skipped": "这条已跳过：" + it.reason}.get(it.status, "")
        self.choice_layout.addStretch()
        self.adv.update()

    def choose(self, key, action):
        self.decisions[key] = action
        for row, it in enumerate(self.plan.items):
            if it.key == key:
                check = self.table.item(row, 0)
                check.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                               (Qt.ItemFlag.ItemIsEnabled if action != "skip" else Qt.ItemFlag.NoItemFlags))
                check.setToolTip("" if action != "skip" else "先在右侧选择怎么处理")
                check.setCheckState(Qt.CheckState.Unchecked if action == "skip" else Qt.CheckState.Checked)
        self.update_choice()
        self.update_commit_label()

    def filter_rows(self, status):
        if self.plan:
            for row, it in enumerate(self.plan.items):
                self.table.setRowHidden(row, status is not None and status != it.status)

    def prepare_commit(self):
        items, decisions = [], {}
        for row, it in enumerate(self.plan.items):
            if it.status in ("new", "same_name"):
                title = self.table.item(row, 3).text()
                items.append(replace(it, title=title))
                checked = self.table.item(row, 0).checkState() == Qt.CheckState.Checked
                decisions[it.key] = (self.decisions.get(it.key, "add" if it.status == "new" else "skip") if checked else "skip")
            else:
                items.append(it)
        return replace(self.plan, items=tuple(items)), decisions

    def update_commit_label(self, *args):
        count = 0
        if self.plan:
            _, decisions = self.prepare_commit()
            count = sum(v != "skip" for v in decisions.values())
        self.commit_button.setText(f"入库 {count} 部")
        self.commit_button.setEnabled(bool(self.plan) and not self.owner.screenshot_mode and not self.owner.busy)

    def commit(self):
        if not self.plan or self.owner.busy or self.owner.screenshot_mode:
            return
        plan, decisions = self.prepare_commit()
        self.owner.run_job("正在一次提交检収结果", lambda cancel: scanning.commit_scan(self.owner.lib, plan, decisions),
                           self.committed, self.failed, cancellable=False)
        self.commit_button.setEnabled(False)

    def committed(self, result):
        self.plan = None
        message = (f"已入库：新作品 {result.added_works} 部，挂接 {result.attached_copies} 份，"
                   f"确认 {result.marked_known} 份，标记不在 {result.marked_missing} 份")
        self.notice.setText(message)
        self.owner.status.idle(message)
        self.owner.show_library()
        self.owner.reload_library(message=message)

    def failed(self, error):
        self.notice.setText("书架在审阅期间变了，请重新扫描" if isinstance(error, PlanConflict) else str(error))
        self.commit_button.setEnabled(False if isinstance(error, PlanConflict) else True)

    def discard(self):
        if self.owner.busy:
            self.owner.cancel_job()
            return
        self.plan, self.decisions = None, {}
        self.table.setRowCount(0)
        self.commit_button.setEnabled(False)
        self.owner.show_library()


class DataCard(Paper):
    def __init__(self, owner, heading, primary=False):
        super().__init__(owner)
        self.primary, self.heading = primary, heading
        self.rows = []
        self.setMinimumSize(420, 300)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), t.color("card"), 18, t.color("line"))
        header = QRectF(1, 1, self.width() - 2, 54)
        rounded(p, header, t.color("beni" if self.primary else "ai"), 17)
        p.fillRect(1, 36, self.width() - 2, 19, t.color("beni" if self.primary else "ai"))
        p.setFont(font("body", 12, True))
        p.setPen(t.color("white"))
        p.drawText(header.adjusted(20, 0, -20, 0), Qt.AlignmentFlag.AlignVCenter, self.heading)
        for i in range(4):
            rect = QRectF(18 + i % 2 * 47, 72 + i // 2 * 70, 42, 61)
            draw_image(p, rect, asset(f"jacket/jacket-{i + 1:02d}.jpg"))
            blossom(p, rect.center().x(), rect.center().y(), 20, t.color("white"))
        y = 70
        for label, value in self.rows:
            p.setPen(t.color("sub"))
            p.setFont(font("body", 12))
            p.drawText(QRectF(125, y, 105, 34), Qt.AlignmentFlag.AlignVCenter, label)
            p.setPen(t.color("ink"))
            p.setFont(font("mono" if label in ("所在", "SHA-256") else "body", 10 if label in ("所在", "SHA-256") else 12))
            p.drawText(QRectF(230, y + 5, self.width() - 247, 37), Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignRight, str(value))
            p.setPen(QPen(t.color("line"), 1, Qt.PenStyle.DashLine))
            p.drawLine(125, y + 44, self.width() - 18, y + 44)
            y += 48


class TransferPage(Paper):
    def __init__(self, owner):
        super().__init__(owner)
        self.info = None
        self.report = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(60, 30, 50, 24)
        lay.setSpacing(12)
        title = QLabel("データ引き継ぎ   DATA TRANSFER")
        title.setFont(font("title", 31, True))
        lay.addWidget(title)
        lay.addWidget(QLabel("旧库只读打开；导入前后核对哈希。每套旧库单独生成新文件。"))
        cards = QHBoxLayout()
        self.old_card = DataCard(owner, "OLD DATA    ·    只读旧 galpanel.db")
        self.new_card = DataCard(owner, "NEW DATA    ·    等待生成新库", True)
        cards.addWidget(self.old_card, 1)
        arrow = QLabel("→")
        arrow.setFont(font("title", 32, True))
        arrow.setFixedWidth(65)
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cards.addWidget(arrow)
        cards.addWidget(self.new_card, 1)
        self.mascot = QLabel()
        self.mascot.setFixedWidth(250)
        self.mascot.setMaximumHeight(400)
        self.mascot.setPixmap(asset("shiori/s2-carry.png").scaledToWidth(240, Qt.TransformationMode.SmoothTransformation))
        self.mascot.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.mascot.setVisible(owner.show_mascot)
        cards.addWidget(self.mascot)
        lay.addLayout(cards, 1)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setMaximumHeight(80)
        lay.addWidget(self.details)
        self.reject_button = Button(owner, "展开拒绝行 / 守恒问题", self.show_errors)
        self.reject_button.hide()
        lay.addWidget(self.reject_button)
        self.adv = Adv(owner, "嘿咻……先选一个旧库吧。旧文件只读，新的书架会放在你选择的位置。", pose=None, height=136)
        lay.addWidget(self.adv)
        actions = QHBoxLayout()
        actions.addWidget(Button(owner, "换一个旧库", self.choose_old))
        self.transfer_button = Button(owner, "生成新库", self.transfer, primary=True, subtitle="TRANSFER")
        self.transfer_button.setEnabled(False)
        actions.addWidget(self.transfer_button)
        self.open_button = Button(owner, "打开新库", self.open_new)
        self.open_button.setEnabled(False)
        actions.addWidget(self.open_button)
        actions.addStretch()
        actions.addWidget(Button(owner, "两库对比", owner.open_compare))
        actions.addWidget(Button(owner, "返回", self.go_back))
        lay.addLayout(actions)

    def go_back(self):
        if self.owner.lib:
            self.owner.show_library()
        else:
            self.owner.show_title()

    def choose_old(self):
        if self.owner.busy or self.owner.screenshot_mode:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择只读旧库", "", "旧库 (*.db);;所有文件 (*)")
        if path:
            self.inspect(path)

    def inspect(self, path):
        self.info, self.report = None, None
        self.transfer_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.old_card.rows = []
        self.new_card.rows = []
        self.details.clear()
        self.reject_button.hide()
        self.owner.run_job("只读检查旧库", lambda cancel: legacy.inspect_legacy(path), self.inspected, cancellable=False)

    def inspected(self, info):
        self.info = info
        h = info.sha256
        compact = " ".join(h[i:i + 4] for i in (0, 4, 8, 12)) + " … " + " ".join(h[i:i + 4] for i in (48, 52, 56, 60))
        self.old_card.rows = [("所在", info.path), ("SHA-256", compact), ("作品", info.games),
                              ("非空备注", info.notes_nonempty), ("资料绑定", info.bindings_nonempty), ("游玩记录", info.play_logs)]
        self.old_card.setToolTip(info.path + "\nSHA-256: " + info.sha256)
        self.old_card.update()
        self.transfer_button.setEnabled(not self.owner.screenshot_mode)
        self.adv.text = f"嘿咻……读到了 {info.games} 部作品和 {info.notes_nonempty} 条备注。请选择一个不存在的 .galdb 文件，原来的书架会原封不动。"
        self.adv.update()

    def transfer(self):
        if not self.info or self.owner.busy or self.owner.screenshot_mode:
            return
        target, _ = QFileDialog.getSaveFileName(self, "为新库选择位置", "", "GalPanel 2 库 (*.galdb)")
        if not target:
            return
        source = self.info.path
        self.transfer_button.setEnabled(False)
        self.owner.run_job("正在只读引き継ぎ", lambda cancel: legacy.import_legacy(source, target, cancel=cancel),
                           self.imported, self.failed, writes_library=True)

    def imported(self, report):
        self.report = report
        self.new_card.heading = "NEW DATA    ·    新库已生成"
        self.new_card.rows = [("所在", report.target_path), ("作品 / 文件", f"{report.imported} / {report.imported}"),
            ("守恒检查", "逐字段一致" if not report.conservation_errors else "存在问题"),
            ("拒绝导入", f"{len(report.rejected)} 条"), ("封面复制 / 缺失", f"{report.covers_copied} / {report.covers_missing}"),
            ("旧文件哈希", "前后一致" if report.sha256_before == report.sha256_after else "不一致")]
        self.new_card.update()
        self.adv.text = f"嘿咻……{report.imported} 部作品已经搬好了！逐字段检查通过，旧文件哈希前后一致。"
        if report.rejected:
            self.adv.text += f"另有 {len(report.rejected)} 条未导入，请展开查看原因。"
        self.adv.update()
        self.details.setText(f"目标：{report.target_path}")
        self.reject_button.setVisible(bool(report.rejected or report.conservation_errors))
        self.transfer_button.setEnabled(False)
        self.open_button.setEnabled(True)

    def failed(self, error):
        self.transfer_button.setEnabled(True)
        message = "已取消，库没有改变" if isinstance(error, Cancelled) else str(error)
        self.details.setText(message)
        self.adv.text = "嘿咻……引き継ぎ没有完成：" + message
        self.adv.update()

    def show_errors(self):
        if self.report:
            lines = [f"id={r.old_id}: {r.reason} · {r.path}" for r in self.report.rejected] + list(self.report.conservation_errors)
            QMessageBox.information(self, "拒绝导入 / 守恒问题", "\n".join(lines))

    def open_new(self):
        if self.report:
            self.owner.open_library_path(self.report.target_path)
