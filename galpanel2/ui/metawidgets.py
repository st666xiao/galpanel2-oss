"""Metadata additions from galpanel3-meta.html; use the existing theme and fonts."""
import html
from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, QSize, QEvent
from PySide6.QtGui import QPainter, QPen, QFontMetrics, QPolygonF
from PySide6.QtWidgets import (QWidget, QFrame, QPushButton, QLayout, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QScrollArea, QListWidget, QListWidgetItem, QComboBox,
                             QApplication, QSizePolicy)
from galpanel2.core import meta as M
from .theme import font
from .widgets import rounded


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item): self.items.append(item)
    def count(self): return len(self.items)
    def itemAt(self, i): return self.items[i] if 0 <= i < len(self.items) else None
    def takeAt(self, i): return self.items.pop(i) if 0 <= i < len(self.items) else None
    def expandingDirections(self): return Qt.Orientation(0)
    def hasHeightForWidth(self): return True
    def heightForWidth(self, width): return self._arrange(QRect(0, 0, width, 0), True)
    def sizeHint(self): return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self.items: size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left+right, top+bottom)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, False)

    def _arrange(self, rect, measure):
        left, top, right, bottom = self.getContentsMargins()
        outer_y = rect.y()
        rect = rect.adjusted(left,top,-right,-bottom)
        x, y, height = rect.x(), rect.y(), 0
        for item in self.items:
            size = item.sizeHint()
            if x > rect.x() and x + size.width() > rect.right() + 1:
                x, y, height = rect.x(), y + height + self.spacing(), 0
            if not measure: item.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + self.spacing()
            height = max(height, size.height())
        return y + height - outer_y + bottom


class RoundedPanel(QFrame):
    def __init__(self, owner, parent=None, condition=False):
        super().__init__(parent)
        self.owner, self.condition = owner, condition

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.owner.theme
        border = QPen(t.color('sakura'), 1.5)
        if self.condition: border.setStyle(Qt.PenStyle.DashLine)
        p.setPen(border)
        p.setBrush(t.color('card', 190) if self.condition else t.color('card'))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 16, 16)


class MetaChip(QPushButton):
    def __init__(self, owner, text, callback, state='off', count=None, brand_token=False):
        super().__init__(text)
        self.owner, self.state, self.count, self.brand_token = owner, state, count, brand_token
        self.setFont(font('body', 12, state == 'in'))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(callback)
        self.setFixedHeight(28)
        self.setToolTip(text)

    def sizeHint(self):
        width = QFontMetrics(font('body',12,self.state=='in')).horizontalAdvance(self.text().replace('✕','×')) + 24
        if self.state in ('in','out') and self.count is not None:width+=16
        if self.count is not None: width += QFontMetrics(font('mono', 10, True)).horizontalAdvance(str(self.count)) + 10
        return QSize(min(460, width), 28)

    def paintEvent(self, event):
        t, state = self.owner.theme, self.state
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = 'usube' if state == 'in' else 'paper'
        ink = 'beni' if state == 'in' else 'sub' if state == 'out' or self.count == 0 else 'ink'
        border = QPen(t.color('beni' if state == 'in' else 'sub' if state == 'out' else 'line'), 1)
        if state == 'out': border.setStyle(Qt.PenStyle.DashLine)
        p.setPen(border)
        p.setBrush(t.color('ai', 25) if self.brand_token else t.color(bg, 0 if state == 'out' else 255))
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.drawRoundedRect(rect, 13, 13)
        f = font('body', 12, state == 'in'); f.setStrikeOut(state == 'out')
        p.setFont(f); p.setPen(t.color('ai' if self.brand_token else ink))
        reserve = QFontMetrics(font('mono',10,True)).horizontalAdvance(str(self.count))+10 if self.count is not None else 0
        prefix = 16 if state in ('in','out') and self.count is not None else 0
        if prefix:
            p.setPen(QPen(t.color('beni'),1.5))
            if state=='in':
                p.drawLine(QPointF(10,14),QPointF(13,17));p.drawLine(QPointF(13,17),QPointF(19,10))
            else:
                p.drawEllipse(QRectF(9,9,11,11));p.drawLine(QPointF(12,14),QPointF(17,14))
            p.setPen(t.color(ink))
        text = p.fontMetrics().elidedText(self.text().replace('✕','×'), Qt.TextElideMode.ElideRight, self.width() - 22 - reserve - prefix)
        p.drawText(rect.adjusted(11+prefix, 0, -reserve-8, 0), Qt.AlignmentFlag.AlignVCenter, text)
        if self.count is not None:
            p.setFont(font('mono', 10, True)); p.setPen(t.color('sub'))
            p.drawText(rect.adjusted(0, 3, -10, -8), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, str(self.count))


class MetaButton(QPushButton):
    def __init__(self, owner, callback):
        super().__init__('全部')
        self.owner, self.active = owner, False
        self.setFixedSize(72, 30)
        self.clicked.connect(callback)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), t.color('card'), 12,
                t.color('sakura' if self.active else 'line'))
        p.setFont(font('body',12,True)); p.setPen(t.color('beni' if self.active else 'ink'))
        p.drawText(self.rect().adjusted(2,0,-13,0), Qt.AlignmentFlag.AlignCenter, self.text())
        p.setPen(Qt.PenStyle.NoPen);p.setBrush(t.color('sub'))
        p.drawPolygon(QPolygonF([QPointF(self.width()-14,13),QPointF(self.width()-8,13),QPointF(self.width()-11,17)]))


class MetaLink(QPushButton):
    def __init__(self, owner, text):
        super().__init__(text)
        self.owner = owner
        self.setFixedSize(QFontMetrics(font('body',11,True)).horizontalAdvance(text)+8,24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        p=QPainter(self)
        p.setFont(font('body',11,True))
        # Match the mockup footer link, including readable night/disabled text.
        p.setPen(self.owner.theme.color('ink' if self.isEnabled() else 'sub'))
        p.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,self.text())


class MetaPopover(RoundedPanel):
    def __init__(self, page, kind):
        super().__init__(page.owner, page)
        self.page, self.kind, self.counts = page, kind, {}
        self.chips = {}
        self.setFixedWidth(520)
        lay = QVBoxLayout(self); lay.setContentsMargins(16,14,16,12); lay.setSpacing(8)
        head = QHBoxLayout()
        label = QLabel('品牌' if kind == 'brand' else '标签'); label.setFont(font('title',15,True))
        head.addWidget(label)
        description = QLabel(('多选＝任一' if kind == 'brand' else '多选＝全部满足') + ' · 点一下包含，再点排除，再点取消')
        description.setFont(font('body',11)); head.addWidget(description); head.addStretch()
        lay.addLayout(head)
        self.search = QLineEdit()
        self.search.setPlaceholderText('找品牌（名字、别名都行）' if kind == 'brand' else '找标签')
        self.search.textChanged.connect(self.rebuild)
        lay.addWidget(self.search)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.content = QWidget(); self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0,0,0,0); self.content_layout.setSpacing(7)
        self.scroll.setWidget(self.content); lay.addWidget(self.scroll)
        foot = QHBoxLayout()
        label = QLabel('数字＝点上之后还剩几部'); label.setFont(font('body',11)); foot.addWidget(label); foot.addStretch()
        self.tidy = MetaLink(self.owner,'整理品牌…' if kind == 'brand' else '整理标签…')
        self.tidy.clicked.connect(self.manage); foot.addWidget(self.tidy)
        if kind == 'tag':
            self.mapping = MetaLink(self.owner,'标签对照…')
            self.mapping.clicked.connect(lambda:(self.hide(),self.owner.open_tag_mapping()))
            foot.addWidget(self.mapping)
        lay.addLayout(foot)
        self._filter_active=False
        self.hide()

    def showEvent(self,event):
        super().showEvent(event)
        if not self._filter_active:
            QApplication.instance().installEventFilter(self)
            self._filter_active=True

    def hideEvent(self,event):
        if self._filter_active:
            QApplication.instance().removeEventFilter(self)
            self._filter_active=False
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        if self.isVisible() and event.type() == QEvent.Type.MouseButtonPress:
            point = event.globalPosition().toPoint()
            button = self.page.brand_button if self.kind=='brand' else self.page.tag_button
            if not self.rect().contains(self.mapFromGlobal(point)) and not button.rect().contains(button.mapFromGlobal(point)):
                self.hide()
        if self.isVisible() and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.hide(); return True
        return False

    def show_for_button(self):
        other = self.page.tag_popover if self.kind=='brand' else self.page.brand_popover
        other.hide()
        if self.isVisible(): self.hide(); return
        self.rebuild()
        button = self.page.brand_button if self.kind=='brand' else self.page.tag_button
        point = button.mapTo(self.page,QPoint(button.width(),button.height()+7))
        self.move(max(6, min(point.x()-520, self.page.width()-520-6)), point.y())
        self.show(); self.raise_(); self.search.setFocus()

    def refresh(self, counts):
        self.counts = counts
        if self.isVisible():
            # Keep the current order while the panel is open: replacing hundreds
            # of widgets on each click makes filtering noticeably sluggish.
            for item_id, chip in self.chips.items():
                chip.state = self.page.meta_state(self.kind, item_id)
                chip.count = counts.get(item_id, 0)
                chip.updateGeometry()
                chip.update()
            self._resize_content()

    def rebuild(self):
        clear_layout(self.content_layout)
        self.chips = {}
        query = M.name_key(self.search.text())
        all_items = self.page.brands.values() if self.kind=='brand' else self.page.tags.values()
        items = [x for x in all_items if not query or any(query in M.name_key(n) for n in
                 ((x.name, *x.aliases) if self.kind=='brand' else (x.name,)))]
        blocks = [(None, items)] if self.kind=='brand' else [(g.name,[x for x in items if x.group_id==g.id]) for g in self.page.groups]
        if self.kind=='brand' and (not query or query in M.name_key('（无品牌）')):
            blocks[0][1].append(M.Brand(M.NO_BRAND,'（无品牌）',(),0))
        for group, xs in blocks:
            if not xs: continue
            if group:
                label=QLabel(group); label.setFont(font('body',10,True)); label.setStyleSheet(f"color:{self.owner.theme.roles['beni']}")
                self.content_layout.addWidget(label)
            holder=QWidget(); flow=FlowLayout(holder)
            xs.sort(key=lambda x:(x.id==M.NO_BRAND, -self.counts.get(x.id,0), M.name_key(x.name)))
            for x in xs:
                state=self.page.meta_state(self.kind,x.id)
                chip=MetaChip(self.owner,x.name,lambda checked=False,i=x.id:self.page.cycle_meta(self.kind,i),state,self.counts.get(x.id,0))
                self.chips[x.id] = chip
                if x.id != M.NO_BRAND or self.kind=='tag':
                    chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                    chip.customContextMenuRequested.connect(lambda pos, i=x.id, widget=chip:self.page.meta_context(self.kind,(self.page.brands if self.kind=='brand' else self.page.tags)[i],widget.mapToGlobal(pos)))
                flow.addWidget(chip)
            self.content_layout.addWidget(holder)
        self.content_layout.addStretch()
        self._resize_content()

    def _resize_content(self):
        for i in range(self.content_layout.count()):
            w = self.content_layout.itemAt(i).widget()
            if w and w.layout():
                w.layout().invalidate()
        self.content_layout.activate()
        # Show wrapped chips, bound the scroll area for large user libraries.
        height = 0
        for i in range(self.content_layout.count()):
            w=self.content_layout.itemAt(i).widget()
            if w:
                height += (w.layout().heightForWidth(480) if isinstance(w.layout(),FlowLayout) else w.sizeHint().height())+7
        self.scroll.setFixedHeight(max(40,min(300,height+6)))
        self.adjustSize()

    def manage(self):
        self.hide()
        (self.owner.open_brand_tidy if self.kind=='brand' else self.owner.open_tag_manager)()


class TokenInput(QWidget):
    def __init__(self, owner, kind, items, groups=()):
        super().__init__()
        self.owner, self.kind = owner, kind
        self.items, self.groups, self.values = {x.id:x for x in items}, list(groups), []
        lay=QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(4)
        self.box=RoundedPanel(owner); self.flow=FlowLayout(self.box)
        self.flow.setContentsMargins(8,5,8,5)
        lay.addWidget(self.box)
        self.edit=QLineEdit(); self.edit.setPlaceholderText('输入品牌名' if kind=='brand' else '输入标签名')
        self.edit.setFont(font('body',13));self.edit.setMinimumWidth(140);self.edit.setFixedHeight(26)
        self.edit.setStyleSheet('QLineEdit {background:transparent; border:0; padding:2px;}')
        self.group=QComboBox(self)
        for g in self.groups:self.group.addItem(g.name,g.id)
        self.group.hide()
        self.suggestions=QListWidget(); self.suggestions.setMaximumHeight(105); self.suggestions.hide()
        self.suggestions.setFont(font('body',12))
        self.suggestions.setStyleSheet('QListWidget {border:1px solid '+owner.theme.roles['line']+'; border-radius:12px;} '
                                      'QListWidget::item:selected {background:'+owner.theme.roles['usube']+';}')
        lay.addWidget(self.suggestions)
        self.edit.textChanged.connect(self.suggest)
        self.edit.returnPressed.connect(self.add_selected)
        self.suggestions.itemClicked.connect(self.choose)
        self.render()

    def ids(self): return list(self.values)

    def set_items(self,items,groups=None):
        self.items={x.id:x for x in items}
        if groups is not None:
            selected=self.group.currentData();self.groups=list(groups);self.group.clear()
            for g in self.groups:self.group.addItem(g.name,g.id)
            if selected:self.group.setCurrentIndex(self.group.findData(selected))
        self.render(); self.suggest()

    def set_ids(self,ids):
        self.values=list(dict.fromkeys(ids)); self.render()

    def add_existing(self,item_id):
        if item_id not in self.items:raise KeyError(item_id)
        if item_id not in self.values:self.values.append(item_id)
        self.render()

    def add_new(self,name,group_id=None):
        name=M._name(name)
        if self.kind=='tag' and group_id not in {g.id for g in self.groups}:raise KeyError(group_id)
        value=('new',name) if self.kind=='brand' else ('new',name,group_id)
        if not any(isinstance(v,tuple) and M.name_key(v[1])==M.name_key(name) for v in self.values):self.values.append(value)
        self.render()

    def remove(self,value):
        self.values.remove(value); self.render()

    def remove_existing(self,tag_id):
        self.remove(tag_id)

    def render(self):
        while self.flow.count():
            widget=self.flow.takeAt(0).widget()
            if widget and widget is not self.edit:widget.hide();widget.deleteLater()
        for value in self.values:
            name=self.items[value].name if isinstance(value,int) and value in self.items else value[1] if isinstance(value,tuple) else str(value)
            chip=MetaChip(self.owner,name+' ✕',lambda checked=False,v=value:self.remove(v),'in',brand_token=self.kind=='brand')
            self.flow.addWidget(chip)
        self.flow.addWidget(self.edit)
        self.box.setMinimumHeight(36)

    def suggest(self,*args):
        self.suggestions.clear(); text=self.edit.text(); query=M.name_key(text)
        if not query:self.suggestions.hide(); return
        for x in self.items.values():
            names=(x.name,*x.aliases) if self.kind=='brand' else (x.name,)
            if any(query in M.name_key(n) for n in names):
                group=next((g.name for g in self.groups if g.id==getattr(x,'group_id',None)), '')
                extra=('别名 '+'、'.join(x.aliases)) if self.kind=='brand' and x.aliases else group
                item=QListWidgetItem(f'{x.name}　{x.work_count} 部 · {extra}'); item.setData(Qt.ItemDataRole.UserRole,x.id)
                self.suggestions.addItem(item)
        occupied=any(query==M.name_key(n) for x in self.items.values() for n in
                     ((x.name,*x.aliases) if self.kind=='brand' else (x.name,)))
        if not occupied:
            item=QListWidgetItem(f'＋ 新建{"品牌" if self.kind=="brand" else "标签"}「{text}」'+(' → 分组' if self.kind=='tag' else ''))
            item.setData(Qt.ItemDataRole.UserRole,('new',text)); self.suggestions.addItem(item)
            if self.kind=='tag':
                holder=QWidget();row=QHBoxLayout(holder);row.setContentsMargins(8,2,8,2)
                row.addWidget(QLabel(item.text()));row.addStretch()
                group=QComboBox()
                for g in self.groups:group.addItem(g.name,g.id)
                group.setCurrentIndex(self.group.currentIndex())
                group.currentIndexChanged.connect(self.group.setCurrentIndex)
                row.addWidget(group);item.setText('');item.setSizeHint(holder.sizeHint());self.suggestions.setItemWidget(item,holder)
        self.suggestions.setVisible(self.suggestions.count()>0)
        if self.suggestions.count():self.suggestions.setCurrentRow(0)

    def add_selected(self):
        item=self.suggestions.currentItem()
        if item:self.choose(item)

    def choose(self,item):
        value=item.data(Qt.ItemDataRole.UserRole)
        try:
            if isinstance(value,int):self.add_existing(value)
            else:self.add_new(value[1],self.group.currentData() if self.kind=='tag' else None)
        except (ValueError,KeyError) as error:
            self.edit.setToolTip(str(error));return
        self.edit.clear();self.edit.setFocus()
