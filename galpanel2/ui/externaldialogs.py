"""Explicit metadata confirmation; network jobs never own a database connection."""
from dataclasses import replace
import html

from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QSize, QRectF, QTimer
from PySide6.QtGui import QPainter, QPen, QPixmap, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QListView, QListWidget, QListWidgetItem, QPushButton, QMenu, QStyledItemDelegate, QStyle, QCheckBox, QFormLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea, QMessageBox, QDialog)

from galpanel2.core import batch as BT, binding as B, meta as M, records as R, sites as S, repository as repo
from galpanel2.core import storage as ST
from galpanel2.core.domain import WriteConflict, DuplicateLink, Cancelled
from .metadialogs import MetaDialog
from .widgets import Button, Header, HeaderIconButton, rounded
from .frame import icon_pen
from .theme import font, DAY, NIGHT, THEMES
from .library import WrapLabel, draw_image

SITE_COLORS = {'dlsite': '#1f5fa8', 'vndb': '#3a6b45', 'bangumi': '#e2557b',
               'steam': '#1b2838', 'f95': '#2f6f4e'}


def badge(site):
    return f'<span style="color:#ffffff;background-color:{SITE_COLORS[site]};font-size:10px"> {R.SITE_LABEL[site]} </span>'


def hint(owner, text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setFont(font('body', 11))
    label.setStyleSheet(f"color:{owner.theme.roles['sub']}")
    return label


def heading(text, subtitle):
    label = QLabel(text + f' <small>{subtitle}</small>')
    label.setFont(font('title', 19, True))
    return label


class FindVeil(QWidget):
    def paintEvent(self, event):
        QPainter(self).fillRect(self.rect(), self.parentWidget().theme.color('find_veil', 89))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.parentWidget().find_dialog.reject()


class RefBadge(QLabel):
    def __init__(self, theme):
        super().__init__()
        self.theme = theme

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(icon_pen(self.theme.color('wakaba')))
        y = self.height() / 2
        p.drawLine(QPointF(3, y), QPointF(6, y + 3))
        p.drawLine(QPointF(6, y + 3), QPointF(12, y - 4))


class PosterWall(QListWidget):
    GAP_X, GAP_Y = 16, 20
    MIN_W, MAX_W = 140, 200

    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(PosterDelegate(dialog))

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Resize:
            # IconMode keeps room for a vertical scrollbar while arranging cells.
            scrollbar = self.verticalScrollBar().sizeHint().width()
            available = min(event.size().width(), self.maximumViewportSize().width() - scrollbar) - 4
            cols = max(2, available // (self.MIN_W + self.GAP_X))
            width = available // cols - self.GAP_X
            if width > self.MAX_W:
                width = available // (cols + 1) - self.GAP_X
            width = max(1, width)
            self.setGridSize(QSize(width + self.GAP_X, int(width * 4.2 / 3) + 39 + self.GAP_Y))
        return super().viewportEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.dialog.bind_button.isEnabled():
                self.dialog.bind_button.click()
            return
        super().keyPressEvent(event)


class PosterDelegate(QStyledItemDelegate):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog

    @staticmethod
    def opacity(candidate):
        return .45 if candidate.grade == 'no' else 1.0

    def sizeHint(self, option, index):
        grid = self.dialog.list.gridSize()
        return QSize(grid.width() - PosterWall.GAP_X, grid.height() - PosterWall.GAP_Y)

    def paint(self, p, option, index):
        i = index.data(Qt.ItemDataRole.UserRole)
        if i is None or i >= len(self.dialog._candidates):
            return
        candidate = self.dialog._candidates[i]
        record, theme = candidate.record, self.dialog.owner.theme
        rect = QRectF(option.rect)
        width = option.rect.width()
        art = QRectF(rect.x(), rect.y() + 6, width, width * 4.2 / 3)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self.opacity(candidate))
        if option.state & QStyle.StateFlag.State_Selected:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(theme.color('beni'), 2))
            p.drawRoundedRect(art.adjusted(-3, -3, 3, 3), 10, 10)
            p.setPen(QPen(theme.color('paper'), 2))
            p.drawRoundedRect(art.adjusted(-1, -1, 1, 1), 9, 9)
        pix = self.dialog.images.get(record.cover_url)
        if pix:
            draw_image(p, art, pix)
        else:
            rounded(p, art, theme.color('placeholder'), 8)
            p.setPen(theme.color('sub'))
            p.setFont(font('title', 14, True))
            p.drawText(art.adjusted(10, 8, -10, -8),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, record.title)

        site = R.SITE_LABEL[record.site]
        p.setFont(font('body', 10))
        tag = QRectF(art.x() + 8, art.bottom() - 25, p.fontMetrics().horizontalAdvance(site) + 12, 17)
        rounded(p, tag, theme.color('poster_badge', 140), 5)
        p.setPen(theme.color('white'))
        p.drawText(tag, Qt.AlignmentFlag.AlignCenter, site)

        brand = record.brands[0].name if record.brands else ''
        byline = ' · '.join(part for part in (brand, (record.release_date or '')[:4]) if part)
        for y, text, size, role in ((5, record.title, 12.5, 'ink'),
                                    (22, byline or record.site_id, 11, 'faint')):
            p.setFont(font('body', size))
            p.setPen(theme.color(role))
            elided = p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, width)
            p.drawText(QRectF(art.x(), art.bottom() + y, width, 17), elided)
        p.restore()


class FindSideCover(QWidget):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.setObjectName('findSideCover')

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        record = self.dialog.current_record
        pix = self.dialog.images.get(record.cover_url) if record else None
        if pix:
            draw_image(p, rect, pix, radius=10)
        else:
            rounded(p, rect, self.dialog.owner.theme.color('placeholder'), 10)


class FindDialog(MetaDialog):
    def __init__(self, owner, work_id):
        super().__init__(owner)
        self.setModal(False)
        self.setWindowTitle('找资料')
        self.setObjectName('findDialog')
        self.setStyleSheet('#findDialog {background:transparent;}')
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(900, 560)
        self.work_id = None
        self._generation = 0
        self._selection = 0
        self._net = set()
        self._candidates = []
        self.images, self._image_loading = {}, set()
        self.current_plan = None
        self.current_record = None
        self.info = None
        self.revision = None
        self.links = {}
        self._preview_lines = []
        self._taken_title = None
        self._ref_timer = QTimer(self)
        self._ref_timer.setSingleShot(True)
        self._ref_timer.setInterval(350)
        self._ref_timer.timeout.connect(self.search)
        self._build_ui()
        self.set_work(work_id)
        self.apply_theme()

    def _build_ui(self):
        t = self.owner.theme
        self.layout_box.setContentsMargins(8, 18, 8, 16)
        self.layout_box.setSpacing(10)
        top = QHBoxLayout()
        top.setContentsMargins(8, 0, 8, 0)
        self.query = QLineEdit()
        self.query.setAccessibleName('搜索资料')
        self.query.setFont(font('body', 15))
        self.query.setFixedHeight(46)
        self.query_icon = self.query.addAction(Header._search_icon(t), QLineEdit.ActionPosition.LeadingPosition)
        self.query.returnPressed.connect(self.search)
        self.query.textChanged.connect(self._query_changed)
        trailing = QHBoxLayout(self.query)
        trailing.setContentsMargins(0, 0, 14, 0)
        trailing.addStretch()
        self.query_hint = QLabel('也可以贴网址或编号')
        self.query_hint.setObjectName('findHint')
        self.query_hint.setFont(font('body', 12.5))
        self.ref_badge = RefBadge(t)
        self.ref_badge.setObjectName('findRefBadge')
        self.ref_badge.setFont(font('body', 12.5, True))
        self.ref_badge.setContentsMargins(18, 0, 0, 0)
        for label in (self.query_hint, self.ref_badge):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            trailing.addWidget(label)
        self.close_button = HeaderIconButton(self.owner, '✕', '关闭', '关闭找资料', self.reject, 'findClose')
        self.close_button.setFixedSize(36, 36)
        top.addWidget(self.query, 1)
        top.addWidget(self.close_button)
        self.layout_box.addLayout(top)
        self.search_button = Button(self.owner, '搜索', self.search, parent=self)
        self.search_button.hide()

        context = QHBoxLayout()
        context.setContentsMargins(8, 0, 8, 0)
        self.work_label = hint(self.owner, '')
        self.work_label.setObjectName('findContext')
        self.work_label.setTextFormat(Qt.TextFormat.PlainText)
        self.work_label.setWordWrap(False)
        self.work_label.setMaximumWidth(420)
        self.work_label.setFont(font('body', 12))
        self.back_link = self._link_label('findBackLink', 12)
        self.back_link.linkActivated.connect(self._back_to_search)
        self.back_link.hide()
        context.addWidget(self.work_label)
        context.addWidget(self.back_link)

        self.prev_link = self._link_label('findPrevLink', 11.5)
        self.prev_link.setText('<a href="prev" style="text-decoration:none;">◀ 上一部</a>')
        self.prev_link.linkActivated.connect(lambda: self.nav_prev())
        context.addWidget(self.prev_link)

        self.next_link = self._link_label('findNextLink', 11.5)
        self.next_link.setText('<a href="next" style="text-decoration:none;">下一部 ▶</a>')
        self.next_link.linkActivated.connect(lambda: self.nav_next())
        context.addWidget(self.next_link)

        self.next_unbound_link = self._link_label('findNextUnboundLink', 11.5)
        self.next_unbound_link.setText('<a href="next_unbound" style="text-decoration:none;">⏭ 下一个未绑定</a>')
        self.next_unbound_link.linkActivated.connect(lambda: self.nav_next_unbound())
        context.addWidget(self.next_unbound_link)

        context.addStretch()
        self.site_button = QPushButton()
        self.site_button.setObjectName('findSites')
        self.site_button.setAccessibleName('站点')
        self.site_button.setStyleSheet('QPushButton {background:transparent;border:0;padding:4px 8px;}')
        menu = QMenu(self.site_button)
        self.switches = {}
        for site in R.SITES:
            action = menu.addAction(R.SITE_LABEL[site])
            action.setCheckable(True)
            action.setChecked(True)
            self.switches[site] = action
            action.toggled.connect(self._update_sites)
        self.site_button.setMenu(menu)
        self._update_sites()
        context.addWidget(self.site_button)
        self.layout_box.addLayout(context)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.list = PosterWall(self)
        self.list.setStyleSheet('QListWidget {background:transparent;border:0;}')
        self.list.currentRowChanged.connect(self.select)
        self.empty = hint(self.owner, '所有站都没找到。换个词，或者直接贴网址、编号。')
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        wall = QVBoxLayout()
        wall.addWidget(self.list, 1)
        wall.addWidget(self.empty, 1)
        body.addLayout(wall, 1)

        side = QWidget()
        side.setFixedWidth(320)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(24, 10, 24, 24)
        side_layout.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea {background:transparent;}')
        content = QWidget()
        details = QVBoxLayout(content)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(12)
        self.side_cover = FindSideCover(self)
        self.side_cover.setFixedHeight(360)
        details.addWidget(self.side_cover)
        self.side_title = WrapLabel(17, t.roles['ink'], bold=True, family='title')
        self.side_title.setObjectName('findSideTitle')
        details.addWidget(self.side_title)
        self.side_by = self._link_label('findSideBy', 12.5)
        self.side_by.linkActivated.connect(self._preview_link)
        details.addWidget(self.side_by)
        self.will = self._link_label('findWill', 12)
        self.will.linkActivated.connect(self._preview_link)
        details.addWidget(self.will)
        title_row = QHBoxLayout()
        self.title_caption = hint(self.owner, '书名')
        title_row.addWidget(self.title_caption)
        self.title_choice = QComboBox()
        self.title_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.title_choice.setStyleSheet('QComboBox {background:transparent;border:0;padding:2px 8px;}')
        self.title_choice.currentIndexChanged.connect(self._update_will)
        title_row.addWidget(self.title_choice, 1)
        details.addLayout(title_row)
        details.addStretch()
        scroll.setWidget(content)
        side_layout.addWidget(scroll, 1)
        self.bound_label = self._link_label('findBound', 11)
        self.bound_label.linkActivated.connect(self._confirm_unbind)
        side_layout.addWidget(self.bound_label)
        btn_box = QHBoxLayout()
        btn_box.setSpacing(8)
        self.bind_button = Button(self.owner, '绑定', lambda: self.bind(continue_next=False), primary=False)
        self.bind_button.setAutoDefault(False)
        self.bind_button.setFixedHeight(42)
        self.bind_next_button = Button(self.owner, '绑定并下一部 ⏭', lambda: self.bind(continue_next=True), primary=True)
        self.bind_next_button.setAutoDefault(False)
        self.bind_next_button.setFixedHeight(42)
        self.bind_next_button.setToolTip('绑定当前资料，并自动检索下一个未绑定的游戏 (Ctrl+Enter)')
        btn_box.addWidget(self.bind_button, 1)
        btn_box.addWidget(self.bind_next_button, 2)
        side_layout.addLayout(btn_box)
        body.addWidget(side)
        self.layout_box.addLayout(body, 1)
        self.message.setFont(font('body', 11))
        self.layout_box.addWidget(self.message)

        QShortcut(QKeySequence("PageDown"), self, self.nav_next_unbound)
        QShortcut(QKeySequence("PageUp"), self, self.nav_prev)
        QShortcut(QKeySequence("Alt+Right"), self, self.nav_next)
        QShortcut(QKeySequence("Alt+Left"), self, self.nav_prev)
        QShortcut(QKeySequence("Ctrl+Return"), self, lambda: self.bind(continue_next=True))
        QShortcut(QKeySequence("Ctrl+Enter"), self, lambda: self.bind(continue_next=True))

        # Keep the legacy preview builder and preview_text() output intact.
        self.fields = {key: QLabel(self) for key in ('站点', '品牌', '发售日', '简介', '封面', '标签')}
        for field in self.fields.values():
            field.hide()

    def _link_label(self, name, size):
        label = QLabel()
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        label.setWordWrap(True)
        label.setFont(font('body', size))
        return label

    def apply_theme(self):
        theme = self.owner.theme
        colors = theme.roles
        self.query_icon.setIcon(Header._search_icon(theme))
        self.ref_badge.theme = theme
        for widget, role in (
            (self.query_hint, 'faint'), (self.ref_badge, 'wakaba'), (self.work_label, 'faint'),
            (self.empty, 'sub'), (self.side_title, 'ink'), (self.side_by, 'sub'),
            (self.will, 'sub'), (self.title_caption, 'sub'), (self.bound_label, 'faint'),
            (self.message, 'sub'),
        ):
            widget.setStyleSheet(f"color:{colors[role]};background:transparent")
        self.back_link.setText(f'<a href="back" style="color:{colors["beni"]}">回到搜索结果</a>')
        self._style_query(R.parse_ref(self.query.text()))
        self._update_side(self._taken_title)
        self._update_bound()

    def _query_changed(self, text):
        ref = R.parse_ref(text)
        self._ref_timer.stop()
        self.query_hint.setVisible(ref is None)
        self.ref_badge.setVisible(ref is not None)
        if ref:
            self.ref_badge.setText(f'{R.SITE_LABEL[ref.site]} {ref.site_id}')
            if self.isVisible() and not self.owner.screenshot_mode:
                self._ref_timer.start()
        self._style_query(ref)

    def _style_query(self, ref):
        t = self.owner.theme.roles
        label = self.ref_badge if ref else self.query_hint
        self.query.setStyleSheet(
            f"QLineEdit {{background:{t['card']};border:1.5px solid {t['wakaba' if ref else 'line']};"
            f"border-radius:12px;padding-right:{label.sizeHint().width() + 24}px;}}")

    def _update_sites(self):
        sites = self.sites()
        text = '全部站点' if sites == R.SITES else ' · '.join(R.SITE_LABEL[s] for s in sites)
        self.site_button.setText(text or '站点')

    def _context(self, status, back=False):
        self.work_label.setText(status)
        self.back_link.setVisible(back)

    def _result_context(self, ref=None, errors=None):
        back = bool(ref and self._candidates)
        result = '按链接取到 1 个 ·' if back else f'找到 {len(self._candidates)} 个'
        suffix = ''.join(f' · {R.SITE_LABEL[s]} 连不上' for s in errors or ())
        self._context(f'为「{self._work_title}」找资料 · {result}{suffix}', back)
        self.list.setVisible(bool(self._candidates))
        self.empty.setVisible(not self._candidates)

    def _back_to_search(self):
        self.query.setText(R.search_text(self._work_title))
        self.search()

    def align_to_owner(self):
        owner = self.owner
        size = owner.frameGeometry().size()
        dx = min(150, max(0, (size.width() - 900) // 2))
        dy = min(56, max(0, (size.height() - 560) * 56 // 96))
        bottom = min(40, max(0, (size.height() - 560) * 40 // 96))
        self.resize(max(900, size.width() - 2 * dx), max(560, size.height() - dy - bottom))
        self.move(QPoint(dx, dy) if owner.screenshot_mode else owner.frameGeometry().topLeft() + QPoint(dx, dy))
        veil = owner.find_veil
        top = owner.header.mapTo(owner, QPoint(0, owner.header.height())).y()
        veil.setGeometry(0, top, owner.width(), max(0, owner.height() - top))

    def present(self):
        owner = self.owner
        if owner.find_veil is None:
            owner.find_veil = FindVeil(owner)
            owner.find_veil.setObjectName('findVeil')
        self.align_to_owner()
        owner.find_veil.show()
        owner.find_veil.raise_()
        self.show()
        self.raise_()
        self.query.setFocus()

    def hideEvent(self, event):
        self._ref_timer.stop()
        if self.owner.find_dialog is self and self.owner.find_veil is not None:
            self.owner.find_veil.hide()
        super().hideEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), self.owner.theme.color('paper'), 14)

    def set_work(self, work_id):
        self._generation += 1; self._selection += 1
        for job in list(self._net): job.cancel()
        self.lib = self.owner.lib
        self.work_id = work_id
        work = self.owner.library_page.details[work_id]
        self._work_title = work.title
        self.query.setText(R.search_text(work.title))
        self._query_changed(self.query.text())
        self._ref_timer.stop()
        self._context(f'为「{work.title}」找资料')
        self.links = self.owner.library_page.links.get(work_id,{})
        self._update_bound()
        self._candidates = []; self.list.clear(); self._clear_preview()
        self.search_button.setEnabled(True)

    def set_site(self, site, on): self.switches[site].setChecked(on)
    def sites(self): return tuple(s for s in R.SITES if self.switches[s].isChecked())
    def candidates(self): return list(self._candidates)
    def preview_text(self): return '\n'.join(self._preview_lines)
    def bound_text(self):
        return '同一部作品可以在每个站各绑一个；已绑：' + '　'.join(f'{R.SITE_LABEL[s]} {self.links.get(s,"无")}' for s in R.SITES)

    def _alive(self, generation):
        return generation == self._generation and self.owner.lib is self.lib and self.isVisible() and not self.owner.closing

    def _run_net(self, label, task, done, failed):
        job = self.owner.run_net(label, task, done, failed)
        self._net.add(job)
        job.finished.connect(lambda j=job: self._net.discard(j))
        return job

    def _clear_preview(self):
        self.current_plan = self.current_record = None
        self.bind_button.setEnabled(False); self.title_choice.clear()
        if hasattr(self, 'bind_next_button'):
            self.bind_next_button.setEnabled(False)
        self.title_choice.addItem('保持现在的书名',None)
        for field in self.fields.values(): field.clear()
        self._preview_lines = []
        self.bind_button.setText('绑定')
        if hasattr(self, 'bind_next_button'):
            self.bind_next_button.setText('绑定并下一部 ⏭')
        self._update_side()

    def search(self):
        self._ref_timer.stop()
        if not self.isVisible() or self.saving or self.owner.lib is not self.lib: return
        self._generation += 1; self._selection += 1
        generation = self._generation
        for job in list(self._net): job.cancel()
        self._candidates = []; self.list.clear(); self._clear_preview()
        self.message.setText('正在搜索…'); self.search_button.setEnabled(False)
        query, selected = self.query.text(), self.sites()
        self.empty.hide()
        self.list.show()
        self._context('正在搜索…')
        def done(result):
            if not self._alive(generation): return
            self._candidates, errors = result
            self.search_button.setEnabled(True)
            self.message.setText('　'.join(f'{R.SITE_LABEL[s]}：{e}' for s,e in errors.items()))
            self._readonly_message()
            self.list.blockSignals(True); self.list.clear()
            for i,c in enumerate(self._candidates):
                item = QListWidgetItem(); item.setData(Qt.ItemDataRole.UserRole,i); self.list.addItem(item)
                self._thumbnail(c.record.cover_url,generation)
            self.list.blockSignals(False)
            self._result_context(R.parse_ref(query), errors)
        def failed(error):
            if not self._alive(generation): return
            self.search_button.setEnabled(True); self.message.setText(str(error)); self._readonly_message()
            ref = R.parse_ref(query)
            self._result_context(errors=(ref.site,) if ref and isinstance(error, S.SiteUnreachable) else ())
        self._run_net('搜索资料',lambda cancel:S.SiteClient(S.default_transport(),token=S.read_token(),cancel=cancel).search_all(query,selected),done,failed)

    def show_preset(self, candidates):
        """Show candidates already stored for this work. Does not search."""
        self._generation += 1
        generation = self._generation
        self._candidates = []
        for raw in candidates or []:
            try:
                record = BT.record_from_dict(raw['record'])
                self._candidates.append(R.Candidate(record, raw.get('grade') or 'no', float(raw.get('score') or 0)))
            except (KeyError, TypeError, ValueError):
                continue
        self.search_button.setEnabled(True)
        self.message.clear()
        self._readonly_message()
        self.list.blockSignals(True)
        self.list.clear()
        for i, candidate in enumerate(self._candidates):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.list.addItem(item)
            self._thumbnail(candidate.record.cover_url, generation)
        self.list.blockSignals(False)
        self._result_context()

    def _readonly_message(self):
        if self.lib.readonly:
            self.message.setText(self.message.text() + ('　' if self.message.text() else '') + '只读打开的库不能绑定')

    def _thumbnail(self, url, generation):
        if not url or url in self.images or url in self._image_loading: return
        self._image_loading.add(url)
        def done(raw):
            self._image_loading.discard(url)
            image = QImage.fromData(raw)
            if not image.isNull(): self.images[url] = QPixmap.fromImage(image)
            if self._alive(generation):
                self.list.viewport().update(); self._show_cover()
        def failed(error): self._image_loading.discard(url)
        self._run_net('下载缩略图',lambda cancel:S.SiteClient(S.default_transport(),token=S.read_token(),cancel=cancel).image(url),done,failed)

    def select(self, i):
        if i < 0 or i >= len(self._candidates) or self.saving: return
        self.list.blockSignals(True); self.list.setCurrentRow(i); self.list.blockSignals(False)
        self._selection += 1; selected = self._selection; generation = self._generation
        self._clear_preview(); self.message.setText('正在取资料…')
        record = self._candidates[i].record
        def ready(record):
            if not self._alive(generation) or selected != self._selection: return
            candidate = self._candidates[i]
            self._candidates[i] = replace(candidate,record=record)
            self.list.viewport().update(); self._thumbnail(record.cover_url,generation)
            self._plan(record,generation,selected)
        if record.complete: ready(record)
        else:
            self._run_net('取完整资料',lambda cancel:S.SiteClient(S.default_transport(),token=S.read_token(),cancel=cancel).fetch(R.Ref(record.site,record.site_id)),ready,
                          lambda error:self.message.setText(str(error)) if self._alive(generation) and selected==self._selection else None)

    def _plan(self, record, generation, selected):
        if not self._alive(generation) or selected != self._selection: return
        if self.owner.busy:
            QTimer.singleShot(20,lambda:self._plan(record,generation,selected)); return
        lib, wid = self.lib, self.work_id
        def read(cancel):
            plan=B.plan_bind(lib,wid,record); info=B.get_info(lib,wid)
            work=repo.get_work(lib,wid)
            taken=repo.get_work(lib,plan.taken_by).title if plan.taken_by is not None else None
            return plan,info,work.revision,taken
        def loaded(result):
            if not self._alive(generation) or selected != self._selection: return
            self.current_plan,self.info,self.revision,taken=result
            self.current_record=record; self.links=self.info.links; self._update_bound(); self._preview()
            self._update_side(taken)
            self.message.setText(f'这个条目已经绑在「{taken}」上' if taken else getattr(self,'_plan_notice',''))
            self._plan_notice=''
            self._readonly_message()
            self.bind_button.setEnabled(not self.lib.readonly and taken is None)
            if hasattr(self, 'bind_next_button'):
                self.bind_next_button.setEnabled(not self.lib.readonly and taken is None)
        self.owner.run_job('预览绑定资料',read,loaded,lambda error:self.message.setText(str(error)),cancellable=False)

    def _preview(self):
        p,r,t = self.current_plan,self.current_record,self.owner.theme
        if p is None: return
        site = f'{R.SITE_LABEL[p.site]} {p.site_id}'
        accent = t.roles['ai'] if t.name == 'day' else t.roles['beni']
        self.fields['站点'].setText(badge(p.site)+' '+html.escape(p.site_id)+f'　<a style="color:{accent}" href="external:">在浏览器打开</a>' +
                                    (f'<br><small>会替换已绑的 {html.escape(p.replaces)}</small>' if p.replaces else ''))
        self.title_choice.clear(); self.title_choice.addItem('保持现在的书名',None)
        for name in p.title_options: self.title_choice.addItem(f'改成「{name}」',name)
        brands=[]
        for name,bid,status in p.brands:
            state={'linked':'并入已有品牌','existing':'并入已有品牌','new':'新建品牌','attached':'已挂上'}[status]
            brands.append(f'{name}　{state}')
        brand_text=' · '.join(brands) or '站点没写'
        self.fields['品牌'].setText('<br>'.join(f'<span style="color:{accent};background-color:{t.roles["wall"]}">{html.escape(name)}</span>' for name in brands) or '站点没写')
        release = r.release_date if p.release=='set' else f'已有 {self.info.release_date}，不改' if p.release=='keep' else '站点没写'
        summary = ((r.summary[:120]+('…' if len(r.summary)>120 else '')) if p.summary=='set' else
                   '已有简介，不改' if p.summary=='keep' else '你改过简介，不覆盖' if p.summary=='locked' else '站点没写')
        self.fields['发售日'].setText(release)
        self.fields['简介'].setText(html.escape(summary)+ ('<br><small>你手动改过的简介不会被覆盖</small>' if p.summary=='set' else ''))
        cover={'set':'作品还没有封面 → 用这张','keep':'已经有封面时不动','none':'站点没有封面'}[p.cover]
        self.fields['封面'].setText(cover)
        tags=[self.owner.library_page.tags[i].name for i in p.tags_mapped if i in self.owner.library_page.tags]
        tag_html=' '.join(f'<span style="color:{t.roles["beni"]};background-color:{t.roles["usube"]}"> {html.escape(n)} </span>' for n in tags)
        if p.tags_pending:
            tags.append(f'＋ {p.tags_pending} 个没对照过'); tag_html+=f' <a style="color:{t.roles["beni"]}" href="tagmap:">＋ {p.tags_pending} 个没对照过</a>'
        skipped=p.skipped+p.tags_ignored+p.tags_blocked
        if skipped:
            tags.append(f'略过 {skipped} 个'); tag_html+=f' <span style="color:{t.roles["sub"]}"><s>略过 {skipped} 个</s></span>'
        self.fields['标签'].setText(tag_html or '站点没写')
        self.fields['标签'].setToolTip(f'剧透或低分 {p.skipped} · 忽略 {p.tags_ignored} · 你删过 {p.tags_blocked}')
        self._preview_lines=[f'站点 {site}',*( [f'会替换已绑的 {p.replaces}'] if p.replaces else []),
                             '书名 保持现在的书名；别名会存下来，搜索能用','品牌 '+brand_text,
                             '发售日 '+release,'简介 '+summary,'封面 '+cover,'标签 '+' · '.join(tags)]
        self._show_cover()

    def _show_cover(self):
        self.side_cover.update()

    def _update_side(self, taken=None):
        self._taken_title = taken
        record = self.current_record
        self.side_title.set_full_text(record.title if record else '点一张海报看看')
        self.side_by.setVisible(record is not None)
        self.title_caption.setVisible(record is not None)
        self.title_choice.setVisible(record is not None)
        if record:
            brand = record.brands[0].name if record.brands else ''
            t = self.owner.theme.roles
            link = f'<a href="external:" style="color:{t["beni"]}">{R.SITE_LABEL[record.site]} 页面</a>'
            self.side_by.setText(' · '.join(html.escape(part) for part in (brand, record.release_date) if part)
                                 + (' · ' if brand or record.release_date else '') + link)
        self._update_will()
        self._show_cover()

    def _update_will(self):
        plan = self.current_plan
        self.will.setVisible(plan is not None)
        if plan is None:
            return
        self.bind_button.setText('替换绑定' if plan.replaces else '绑定')
        if hasattr(self, 'bind_next_button'):
            self.bind_next_button.setText('替换并下一部 ⏭' if plan.replaces else '绑定并下一部 ⏭')
        t = self.owner.theme.roles
        if self._taken_title:
            title = html.escape(self._taken_title)
            self.will.setText(f'<span style="color:{t["beni"]}">这个条目已经绑在「{title}」上</span>')
            return
        additions = [text for condition, text in (
            (plan.cover == 'set', '封面'), (plan.brands, '品牌'),
            (plan.release == 'set', '发售日'), (plan.summary == 'set', '简介'),
            (plan.tags_mapped, f'{len(plan.tags_mapped)} 个标签')) if condition]
        lines = ([f'绑定后会加上 <b style="color:{t["ink"]}">{"、".join(additions)}</b>。']
                 if additions else ['绑定后不会新增资料，只记下链接。'])
        if plan.summary == 'locked' or self.title_choice.currentIndex() == 0:
            lines.append('你改过的书名和简介不动。')
        if plan.tags_pending:
            lines.append(f'<a href="tagmap:" style="color:{t["beni"]}">还有 {plan.tags_pending} 个标签没对照过</a>')
        if plan.replaces:
            lines.append(f'会替换已绑的 {html.escape(plan.replaces)}。')
        self.will.setText('<br>'.join(lines))

    def _preview_link(self, href):
        if href=='tagmap:': self.owner.open_tag_mapping()
        elif href=='external:' and self.current_record: self.owner.open_external(self.current_record.url)

    def _update_bound(self):
        color = self.owner.theme.roles['beni']
        parts = [f'{R.SITE_LABEL[site]} {html.escape(self.links[site])}'
                 f'（<a href="{site}" style="color:{color}">解除</a>）'
                 for site in R.SITES if site in self.links]
        self.bound_label.setText('已绑：' + ' · '.join(parts) if parts else '')
        self.bound_label.setVisible(bool(parts))

    def _confirm_unbind(self, site):
        if site not in self.links or self.lib.readonly: return
        text=f'解除和 {R.SITE_LABEL[site]} {self.links[site]} 的绑定？已经写进来的品牌、标签、简介不会删。'
        if QMessageBox.question(self,'解除绑定',text)==QMessageBox.StandardButton.Yes: self.unbind(site)

    def unbind(self, site):
        if self.lib.readonly or self.owner.busy or self.owner.lib is not self.lib: return
        lib,wid=self.lib,self.work_id
        def task(cancel):
            B.unbind(lib,wid,site); return B.get_info(lib,wid)
        def done(info):
            if self.owner.lib is not lib: return
            self.links=info.links; self._update_bound(); self.owner.reload_library(wid)
        self.owner.run_job('解除绑定',task,done,lambda error:self.message.setText(str(error)),cancellable=False,writes_library=True)

    def _find_next_unbound_id(self):
        page = getattr(self.owner, 'library_page', None)
        if not page or not page.model.works:
            return None
        works = page.model.works
        cur_id = self.work_id
        idx = next((i for i, w in enumerate(works) if w.id == cur_id), -1)
        for i in range(idx + 1, len(works)):
            wid = works[i].id
            if not page.links.get(wid):
                return wid
        for i in range(0, idx):
            wid = works[i].id
            if not page.links.get(wid):
                return wid
        return None

    def _find_adjacent_id(self, step=1):
        page = getattr(self.owner, 'library_page', None)
        if not page or not page.model.works:
            return None
        works = page.model.works
        cur_id = self.work_id
        idx = next((i for i, w in enumerate(works) if w.id == cur_id), -1)
        if idx == -1:
            return works[0].id if works else None
        next_idx = (idx + step) % len(works)
        return works[next_idx].id

    def nav_next(self):
        nid = self._find_adjacent_id(1)
        if nid is not None and nid != self.work_id:
            self.set_work(nid)
            self.search()
            if hasattr(self.owner, 'library_page'):
                self.owner.library_page.select_work(nid)

    def nav_prev(self):
        pid = self._find_adjacent_id(-1)
        if pid is not None and pid != self.work_id:
            self.set_work(pid)
            self.search()
            if hasattr(self.owner, 'library_page'):
                self.owner.library_page.select_work(pid)

    def nav_next_unbound(self):
        nid = self._find_next_unbound_id()
        if nid is not None and nid != self.work_id:
            self.set_work(nid)
            self.search()
            if hasattr(self.owner, 'library_page'):
                self.owner.library_page.select_work(nid)
        elif nid == self.work_id:
            self.message.setText('已经是当前唯一未绑定的作品了')
        else:
            self.message.setText('所有作品均已绑定资料')

    def bind(self, continue_next=False):
        if self.saving or self.lib.readonly or self.owner.busy or self.owner.lib is not self.lib or not self.current_plan or self.current_plan.taken_by is not None: return
        lib,wid,revision,record,plan=self.lib,self.work_id,self.revision,self.current_record,self.current_plan
        title=self.title_choice.currentData()
        self.saving=True; self.bind_button.setEnabled(False)
        if hasattr(self, 'bind_next_button'):
            self.bind_next_button.setEnabled(False)
        def task(cancel):
            cover,error=None,None
            if plan.cover=='set':
                client=S.SiteClient(S.default_transport(),token=S.read_token(),cancel=cancel)
                cover,error=B.download_cover(client,record)
            B.apply_bind(lib,wid,revision,record,title,cover)
            return error
        def done(error):
            self.saving=False
            review = getattr(self.owner, 'review_page', None)
            if review is not None:
                review.note_bound(wid)
            message=f'已绑定 {R.SITE_LABEL[record.site]} {record.site_id}'
            if error: message+='　封面没下载下来：'+error
            self.owner.reload_library(wid,message=message)
            if continue_next:
                next_id = self._find_next_unbound_id()
                if next_id is not None and next_id != wid:
                    self.set_work(next_id)
                    self.search()
                    if hasattr(self.owner, 'library_page'):
                        self.owner.library_page.select_work(next_id)
                    return
            self.close()
        def failed(error):
            self.saving=False; self.bind_button.setEnabled(not self.lib.readonly)
            if hasattr(self, 'bind_next_button'):
                self.bind_next_button.setEnabled(not self.lib.readonly)
            if isinstance(error,WriteConflict):
                self._plan_notice='这部作品刚被别处改过，已重新载入'
                self.message.setText(self._plan_notice)
                self._plan(record,self._generation,self._selection)
            elif isinstance(error,DuplicateLink):
                self._plan(record,self._generation,self._selection)
            else: self.message.setText(str(error))
        self.owner.run_job('绑定资料',task,done,failed,cancellable=False,writes_library=True)

    def closeEvent(self,event):
        if self.saving: event.ignore(); return
        self._generation+=1
        for job in list(self._net): job.cancel()
        super().closeEvent(event)

    def reject(self):
        if self.saving:return
        self._generation+=1
        for job in list(self._net):job.cancel()
        super().reject()


class TagMappingDialog(MetaDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle('标签对照 · TAG MAPPING'); self.resize(1110,690)
        self.sources=[]; self.tags=[]; self.groups=[]; self.choices={}; self.visible=[]
        self.layout_box.addWidget(heading('标签对照','TAG MAPPING'))
        self.layout_box.addWidget(hint(owner,'站点上的标签第一次出现时，在这里决定一次，以后自动生效。'))
        self.only_pending=QCheckBox('只看待定的'); self.only_pending.setChecked(True)
        self.only_pending.toggled.connect(self.rebuild); self.layout_box.addWidget(self.only_pending)
        self.table=QTableWidget(0,5); self.table.setHorizontalHeaderLabels(['站点','站点原文','出现','怎么处理','你的标签'])
        self.table.verticalHeader().hide(); self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for col,width in ((0,82),(1,270),(2,58),(3,116)): self.table.setColumnWidth(col,width)
        self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeMode.Stretch)
        self.layout_box.addWidget(self.table,1)
        self.counts=hint(owner,''); self.layout_box.addWidget(self.counts)
        self.layout_box.addWidget(self.message)
        row=QHBoxLayout(); row.addStretch(); row.addWidget(Button(owner,'以后再说',self.reject))
        self.apply_button=Button(owner,'应用 0 条',self.apply,primary=True,subtitle='APPLY')
        self.apply_button.setEnabled(False); row.addWidget(self.apply_button); self.layout_box.addLayout(row)
        self.footer=hint(owner,''); self.layout_box.addWidget(self.footer)

    def reload(self):
        if self.owner.lib is not self.lib or not self.isVisible(): return
        if self.owner.busy:
            QTimer.singleShot(20,self.reload); return
        lib=self.lib; self.apply_button.setEnabled(False)
        self.owner.run_job('读取标签对照',lambda cancel:(B.list_source_tags(lib),M.list_tags(lib),M.list_tag_groups(lib)),self.loaded,
                           lambda error:self.message.setText(str(error)),cancellable=False)

    def loaded(self, data):
        if self.owner.lib is not self.lib:return
        self.sources,self.tags,self.groups=data
        self.choices={}
        for s in self.sources:
            action=s.status if s.status!='pending' else s.suggested_action
            tid=s.tag_id if s.status=='map' else s.suggested_tag_id if action=='map' else None
            self.choices[(s.site,s.tag_key)]=B.TagDecision(s.site,s.tag_key,action,tid,
                        s.suggestion if action=='new' else None,s.suggested_group if action=='new' else None)
        self.rebuild()

    def rows(self): return [self.choices[(s.site,s.tag_key)] for s in self.visible]
    def footer_text(self): return self.footer.text()

    def set_row(self, site, tag_key, action, tag_id=None, name=None, group_id=None):
        key=(site,tag_key)
        if key not in self.choices:raise KeyError(key)
        self.choices[key]=B.TagDecision(site,tag_key,action,tag_id,name,group_id)
        self.rebuild()

    def _change(self, key, **changes):
        self.choices[key]=replace(self.choices[key],**changes); self._counts()

    def rebuild(self,*args):
        self.table.setRowCount(0)
        self.visible=[s for s in self.sources if not self.only_pending.isChecked() or s.status=='pending']
        for i,s in enumerate(self.visible):
            key=(s.site,s.tag_key); d=self.choices[key]
            self.table.insertRow(i); self.table.setRowHeight(i,60)
            site=QLabel(badge(s.site)); site.setAlignment(Qt.AlignmentFlag.AlignCenter); self.table.setCellWidget(i,0,site)
            source=QLabel(html.escape(s.text)+(f'<br><small>{html.escape(s.suggestion)}</small>' if s.site=='vndb' else ''))
            source.setFont(font('body',12)); source.setWordWrap(True); source.setContentsMargins(7,0,7,0)
            self.table.setCellWidget(i,1,source); self.table.setItem(i,2,QTableWidgetItem(str(s.work_count)))
            action=QComboBox()
            for label,a in (('对到','map'),('新建','new'),('忽略','ignore'),('待定','pending')):action.addItem(label,a)
            action.setCurrentIndex(action.findData(d.action)); self.table.setCellWidget(i,3,action)
            def changed(index,k=key,source=s,combo=action):
                value=combo.itemData(index)
                self.choices[k]=B.TagDecision(*k,value,source.suggested_tag_id if value=='map' else None,
                             source.suggestion if value=='new' else None,source.suggested_group if value=='new' else None)
                self.rebuild()
            action.currentIndexChanged.connect(changed)
            holder=QWidget(); row=QHBoxLayout(holder); row.setContentsMargins(6,3,6,3)
            if d.action=='map':
                combo=QComboBox(); combo.setEditable(True); combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
                for group in self.groups:
                    for tag in self.tags:
                        if tag.group_id==group.id:combo.addItem(group.name+' · '+tag.name,tag.id)
                combo.setCurrentIndex(combo.findData(d.tag_id))
                if combo.completer():combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
                combo.currentIndexChanged.connect(lambda i,k=key,c=combo:self._change(k,tag_id=c.itemData(i)))
                row.addWidget(combo)
            elif d.action=='new':
                edit=QLineEdit(d.name or ''); edit.textChanged.connect(lambda text,k=key:self._change(k,name=text)); row.addWidget(edit,1)
                group=QComboBox()
                for g in self.groups:group.addItem(g.name,g.id)
                group.setCurrentIndex(group.findData(d.group_id))
                group.currentIndexChanged.connect(lambda i,k=key,c=group:self._change(k,group_id=c.itemData(i)))
                row.addWidget(group)
            elif d.action=='ignore':row.addWidget(hint(self.owner,'以后这个站点标签都不加'))
            self.table.setCellWidget(i,4,holder)
        count={a:sum(s.status==a for s in self.sources) for a in ('map','ignore','pending')}
        self.footer.setText(f'站点标签 {len(self.sources)} · 已对照 {count["map"]} · 忽略 {count["ignore"]} · 待定 {count["pending"]}')
        self._counts()

    def _counts(self):
        count={a:sum(d.action==a for d in self.rows()) for a in ('map','new','ignore')}
        n=sum(count.values()); self.counts.setText(f'对到 {count["map"]} · 新建 {count["new"]} · 忽略 {count["ignore"]}')
        self.apply_button.setText(f'应用 {n} 条')
        self.apply_button.setEnabled(n>0 and not self.lib.readonly and not self.saving)

    def apply(self):
        if self.lib.readonly or self.saving or self.owner.busy or self.owner.lib is not self.lib:return
        rows=[d for d in self.rows() if d.action!='pending']
        if not rows:return
        self.saving=True;self.apply_button.setEnabled(False);lib=self.lib
        def done(result):
            self.saving=False
            message=f'对到 {result.mapped} 个，新建 {result.created_tags} 个，{result.works_tagged} 处作品加上了标签'
            self.message.setText(message)
            self.owner.after_load=self.reload
            self.owner.reload_library(message=message)
        def failed(error):
            self.saving=False; self.message.setText(str(error)); self._counts()
        self.owner.run_job('标签对照',lambda cancel:B.apply_tag_decisions(lib,rows),done,failed,cancellable=False,writes_library=True)


class SettingsDialog(MetaDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle('設定 CONFIG'); self.resize(530,520)
        self.layout_box.addWidget(heading('设置','CONFIG'))
        self.theme=QComboBox()
        for label,name in (('余晖 / MEDIA','media'),('日 / DAY','day'),('夜 / NIGHT','night')):self.theme.addItem(label,name)
        self.theme.setCurrentIndex(max(0,self.theme.findData(owner.theme.name)))
        self.mascot=QCheckBox('显示栞');self.mascot.setChecked(owner.show_mascot)
        self.native_frame=QCheckBox('使用系统标题栏（重启后生效）')
        self.native_frame.setChecked(owner.settings.data['native_frame'] is True)
        self.cold_root=QLineEdit(owner.settings.data.get('cold_root') or ST.DEFAULT_COLD_ROOT)
        self.cold_root.setPlaceholderText(ST.DEFAULT_COLD_ROOT)
        self.layout_box.addWidget(self.theme); self.layout_box.addWidget(self.mascot)
        self.layout_box.addWidget(self.native_frame)
        cold_label=QLabel('冷藏位置')
        cold_label.setFont(font('body',11))
        self.layout_box.addWidget(cold_label)
        self.layout_box.addWidget(self.cold_root)
        self.layout_box.addWidget(Button(owner,'两库只读对比',owner.open_compare))
        self.layout_box.addWidget(heading('联网资料','METADATA'))
        form=QFormLayout(); form.setHorizontalSpacing(12); form.setVerticalSpacing(12)
        holder=QWidget(); box=QVBoxLayout(holder); box.setContentsMargins(0,0,0,0)
        row=QHBoxLayout(); self.token_input=QLineEdit(); self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_state=QLabel('已保存' if S.read_token() else '未填')
        row.addWidget(self.token_input,1);row.addWidget(self.token_state);box.addLayout(row)
        box.addWidget(hint(owner,'成人条目要 token 才搜得到。只存在这台电脑的程序设置里，不进库文件。'))
        clear=QLabel('<a href="clear:">清除</a>');clear.linkActivated.connect(lambda _:self.clear_token());box.addWidget(clear)
        form.addRow('Bangumi',holder)
        form.addRow('VNDB',hint(owner,'标签只取评分 ≥ 1.5、不剧透的'))
        form.addRow('DLsite',hint(owner,'每秒最多 1 次请求'))
        self.layout_box.addLayout(form);self.layout_box.addWidget(self.message);self.layout_box.addStretch()
        row=QHBoxLayout();row.addStretch();row.addWidget(Button(owner,'取消',self.reject));row.addWidget(Button(owner,'保存',self.save,primary=True))
        self.layout_box.addLayout(row)

    def clear_token(self):
        if not self.owner.screenshot_mode:S.clear_token()
        self.token_state.setText('未填');self.token_input.clear()

    def save(self):
        token=self.token_input.text()
        clean=token.strip()
        if token and not S.valid_token(clean):
            self.message.setText('token 格式不对');return
        try:
            if clean and not self.owner.screenshot_mode:S.write_token(clean)
            theme=THEMES[self.theme.currentData()]
            cold_root=self.cold_root.text().strip()
            if not cold_root:
                self.message.setText('冷藏位置不能为空');return
            if not self.owner.screenshot_mode:
                old=dict(self.owner.settings.data)
                self.owner.settings.data.update(theme=theme.name,show_mascot=self.mascot.isChecked(),
                                                native_frame=self.native_frame.isChecked(),
                                                cold_root=cold_root)
                try:self.owner.settings.save()
                except OSError:
                    self.owner.settings.data=old;raise
            self.owner.theme=theme;self.owner.show_mascot=self.mascot.isChecked();self.owner.apply_theme()
            self.token_input.clear();self.accept()
        except OSError:
            self.message.setText('设置保存失败，请重试')
        except ValueError:
            self.message.setText('token 格式不对')
