import math
from PySide6.QtCore import Qt, QRectF, QRect, QPoint, QPointF, QSize, Signal, QTimer
from PySide6.QtGui import (QPainter, QPen, QLinearGradient, QPainterPath, QBrush, QColor, QFontMetrics,
                           QPixmap, QIcon, QAction)
from PySide6.QtWidgets import (QWidget, QPushButton, QLabel, QHBoxLayout, QProgressBar,
                               QAbstractButton, QMenu, QSizePolicy, QFrame, QLineEdit)
from .theme import asset, font
from .frame import interactive_at, start_move, toggle_maximize, draw_icon, icon_pen


def rounded(p, rect, fill, radius=14, border=None, width=1):
    p.setBrush(fill)
    p.setPen(QPen(border, width) if border else Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(rect), radius, radius)


def blossom(p, x, y, size, color):
    p.save()
    p.translate(x, y)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for _ in range(5):
        path = QPainterPath()
        path.moveTo(0, 0)
        path.cubicTo(-size * .24, -size * .16, -size * .28, -size * .48, -size * .12, -size * .5)
        path.lineTo(0, -size * .39)
        path.lineTo(size * .12, -size * .5)
        path.cubicTo(size * .28, -size * .48, size * .24, -size * .16, 0, 0)
        p.drawPath(path)
        p.rotate(72)
    p.restore()


def vertical_title(p, rect, title, color, size=16):
    p.setFont(font("title", size, True))
    p.setPen(color)
    chars = list(title)
    per_col = max(1, int(rect.height() / (size + 4)))
    columns = min(3, math.ceil(len(chars) / per_col))
    left = rect.center().x() + (columns - 1) * (size + 7) / 2
    for i, char in enumerate(chars[:per_col * 3]):
        col, row = divmod(i, per_col)
        x = left - col * (size + 7)
        p.drawText(QRectF(x - size / 2, rect.top() + row * (size + 4), size + 4, size + 4), Qt.AlignmentFlag.AlignCenter, char)


class Paper(QWidget):
    def __init__(self, owner, wall=False, parent=None, role=None):
        super().__init__(parent)
        self.owner, self.wall = owner, wall
        self.role = role

    def paintEvent(self, event):
        p = QPainter(self)
        if self.role:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            rounded(p, self.rect(), self.owner.theme.color(self.role), 24)
            return
        p.fillRect(self.rect(), self.owner.theme.color("wall" if self.wall else "paper"))
        if self.owner.theme.name == "media":
            return  # no washi paper / seal decorations in the media theme
        p.setOpacity(.13 if self.owner.theme.name == "day" else .22)
        texture = asset(f"texture/washi-{self.owner.theme.name}.jpg").scaledToWidth(420)
        p.fillRect(self.rect(), QBrush(texture))


class Button(QPushButton):
    def __init__(self, owner, text, callback=None, primary=False, subtitle="", parent=None):
        super().__init__(text, parent)
        self.owner, self.primary, self.subtitle = owner, primary, subtitle
        self.selected = False
        self.menu = False
        self.choice = False
        self.setMinimumHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if callback:
            self.clicked.connect(callback)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(1 if self.isEnabled() else .38)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.primary:
            g = QLinearGradient(0, 0, 0, self.height())
            g.setColorAt(0, t.color("primary_top"))
            g.setColorAt(1, t.color("primary_bottom"))
            fill, ink, border = QBrush(g), t.color("on_accent"), None
        elif self.menu:
            if self.selected:
                g = QLinearGradient(0, 0, 0, self.height())
                g.setColorAt(0, t.color("white"))
                g.setColorAt(1, t.color("menu_tint"))
                fill, ink, border = QBrush(g), t.color("ai"), t.color("sakura")
            else:
                fill, ink, border = t.color("white", 0), t.color("white", 200), None
        elif self.selected and self.choice:
            fill, ink, border = t.color("usube"), t.color("beni"), t.color("beni")
        elif self.selected:
            fill, ink, border = t.color("ai"), t.color("white"), None
        else:
            fill, ink, border = t.color("usube" if self.underMouse() else "card"), t.color("ink"), t.color("line")
        radius = self.height() / 2 if t.name == "media" else min(23, self.height() / 2)
        if t.name == "media":
            border = None  # capsule buttons have no outline in the media theme
        rounded(p, rect, fill, radius, border)
        p.setPen(ink)
        p.setFont(font("title" if self.subtitle else "body", 16 if self.subtitle else 13, True))
        textrect = rect.adjusted(8, 0, -8, -12 if self.subtitle else 0)
        if self.text() == "☾":
            center = textrect.center()
            moon, cut = QPainterPath(), QPainterPath()
            moon.addEllipse(QRectF(center.x() - 9, center.y() - 9, 18, 18))
            cut.addEllipse(QRectF(center.x() - 3, center.y() - 12, 18, 18))
            p.fillPath(moon.subtracted(cut), ink)
            p.setPen(ink)
        else:
            p.drawText(textrect, Qt.AlignmentFlag.AlignCenter, self.text())
        if self.subtitle:
            p.setFont(font("body", 9, True))
            p.drawText(rect.adjusted(0, 22, 0, 0), Qt.AlignmentFlag.AlignCenter, self.subtitle)
        if self.menu and self.selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.color("beni"))
            p.drawEllipse(QRectF(11, self.height() / 2 - 3, 6, 6))


class HeaderLogo(QLabel):
    """「● 蔵書帖」: an exact 8px beni dot followed by the title in ai/ink."""

    def __init__(self, owner, size, parent=None, centered=False):
        super().__init__(parent)
        self.owner = owner
        self.size = size
        self.centered = centered
        self.setFont(font("title", size, True))
        self.setFixedHeight(56 if size == 18 else 46)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _group_width(self):
        return QFontMetrics(font("title", self.size, True)).horizontalAdvance("蔵書帖") + 16

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dot = 8
        y = (self.height() - dot) / 2
        x = (self.width() - self._group_width()) / 2 if self.centered else 0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.color("beni"))
        p.drawEllipse(QRectF(x, y, dot, dot))
        f = font("title", self.size, True)
        p.setFont(f)
        p.setPen(t.color("ai" if t.name == "day" else "ink"))
        p.drawText(QRectF(x + dot + 8, 0, max(1, self.width() - x - dot - 8), self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "蔵書帖")

    def sizeHint(self):
        return QSize(self._group_width(), self.height())


class HeaderNavButton(QAbstractButton):
    """A quiet text tab: ink/bold with a 2px beni underline when current."""

    def __init__(self, owner, text, callback=None):
        super().__init__(owner.header if hasattr(owner, "header") else None)
        self.setText(text)
        self.owner = owner
        self.selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if callback:
            self.clicked.connect(callback)
        f = QFontMetrics(font("body", 14))
        self.setFixedSize(f.horizontalAdvance(self.text()) + 20, 56)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(font("body", 14, self.selected))
        p.setPen(t.color("ink" if self.selected else "sub"))
        p.drawText(self.rect().adjusted(0, 0, 0, -2), Qt.AlignmentFlag.AlignCenter, self.text())
        if self.selected:
            if t.name == "media":
                p.fillRect(QRect((self.width() - 18) // 2, self.height() - 10, 18, 3), t.color("beni"))
            else:
                width = QFontMetrics(p.font()).horizontalAdvance(self.text())
                x = (self.width() - width) // 2
                p.fillRect(QRect(x, self.height() - 2, width, 2), t.color("beni"))


class HeaderIconButton(QAbstractButton):
    """34×34 borderless icon button; hover uses wall, beni dot for pending work."""

    def __init__(self, owner, text, accessible, tooltip, callback=None, name=None):
        super().__init__(owner.header if hasattr(owner, "header") else None)
        self.setText(text)
        self.owner = owner
        self.dot = False
        self.setFixedSize(34, 34)
        self.setObjectName(name or "")
        self.setAccessibleName(accessible)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if callback:
            self.clicked.connect(callback)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(1 if self.isEnabled() else .38)
        if self.underMouse():
            rounded(p, self.rect(), t.color("wall"), 8)
        # The icon is drawn as vectors: ☾ / ☀ / ⚙ / ＋ have no glyph in the
        # Windows fonts used by the screenshots. text() is kept for tests/a11y.
        color = t.color("ink" if self.underMouse() else "sub")
        draw_icon(p, self.text(), self.width() / 2, self.height() / 2, 14, color)
        if self.dot:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.color("beni"))
            p.drawEllipse(QRectF(self.width() - 8, 1, 6, 6))

    def setText(self, text):
        super().setText(text)
        self.update()


class HeaderTextButton(QAbstractButton):
    """A capsule-shaped text button for the header (＋ 添加游戏 / 菜单)."""

    def __init__(self, owner, text, accessible, tooltip, callback=None, name=None):
        super().__init__(owner.header if hasattr(owner, "header") else None)
        self.owner = owner
        self.dot = False  # kept for Header.set_page() compatibility
        self.setText(text)
        self.setObjectName(name or "")
        self.setAccessibleName(accessible)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        f = QFontMetrics(font("body", 13))
        self.setFixedSize(max(40, f.horizontalAdvance(self.text()) + 30), 36)
        if callback:
            self.clicked.connect(callback)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = t.color("usube" if self.underMouse() else ("card" if t.name == "media" else "white"))
        rounded(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), bg, self.height() / 2)
        p.setPen(t.color("ink"))
        p.setFont(font("body", 13))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        if self.dot:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.color("beni"))
            p.drawEllipse(QRectF(self.width() - 8, 1, 6, 6))

    def setText(self, text):
        super().setText(text)
        if hasattr(self, "owner"):
            f = QFontMetrics(font("body", 13))
            self.setFixedSize(max(40, f.horizontalAdvance(self.text()) + 30), 36)
        self.update()


class WindowControlButton(QAbstractButton):
    """46×56 frameless-window control; winClose has a red hover."""

    def __init__(self, owner, text, accessible, name, callback=None):
        super().__init__(None)
        self.setText(text)
        self.owner = owner
        self.close_button = name.endswith("Close")
        self.setFixedSize(46, 56)
        self.setObjectName(name)
        self.setAccessibleName(accessible)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if callback:
            self.clicked.connect(callback)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        close_hover = self.close_button and self.underMouse()
        if close_hover:
            p.fillRect(self.rect(), QColor("#C42B1C"))
        elif self.underMouse():
            p.fillRect(self.rect(), t.color("wall"))
        color = QColor("#FFFFFF") if close_hover else t.color("ink" if self.underMouse() else "sub")
        draw_icon(p, self.text(), self.width() / 2, self.height() / 2, 10, color)

    def setText(self, text):
        super().setText(text)
        self.update()


class WindowControls(QWidget):
    """The three buttons at the far right of a frameless header/welcome bar."""

    LABELS = ("winMin", "winMax", "winClose")

    def __init__(self, owner, parent=None, prefix=""):
        super().__init__(parent)
        self.owner = owner
        self.prefix = prefix
        self.setFixedHeight(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        def name(n):
            return {"winMin": "welcomeMin", "winMax": "welcomeMax", "winClose": "welcomeClose"}[n] if prefix else n
        self.winMin = WindowControlButton(owner, "─", "最小化", name("winMin"), owner.showMinimized)
        self.winMax = WindowControlButton(owner, "☐", "最大化", name("winMax"), self._maximize)
        self.winClose = WindowControlButton(owner, "✕", "关闭", name("winClose"), owner.close)
        for b in (self.winMin, self.winMax, self.winClose):
            lay.addWidget(b)
        self.setFixedWidth(46 * 3)
        self.setVisible(not owner.native_frame)

    def _maximize(self):
        toggle_maximize(self.owner)

    def sync_state(self):
        maximized = self.owner.isMaximized()
        self.winMax.setText("❐" if maximized else "☐")
        self.winMax.setAccessibleName("还原" if maximized else "最大化")


class Header(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setFixedHeight(56)
        self.setObjectName("header")
        self.setAutoFillBackground(False)
        self.logo = HeaderLogo(owner, 18, self)
        self.logo.setObjectName("headerLogo")
        self.logo.setToolTip("")
        self.logo.setFixedHeight(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.logo)
        lay.addSpacing(28)
        self.navLibrary = HeaderNavButton(owner, "游戏库", owner.show_library)
        self.navLibrary.setObjectName("navLibrary")
        self.navLibrary.setAccessibleName("游戏库")
        self.navTidy = HeaderNavButton(owner, "整理", owner.show_tidy)
        self.navTidy.setObjectName("navTidy")
        self.navTidy.setAccessibleName("整理")
        self.navLibrary.setParent(self)
        self.navTidy.setParent(self)
        lay.addWidget(self.navLibrary)
        lay.addSpacing(22)
        lay.addWidget(self.navTidy)
        lay.addStretch(1)
        self.search = None
        self._search_action = None
        self.addButton = HeaderTextButton(owner, "＋ 添加游戏", "添加", "添加游戏（来源与扫描）", self._add, "addButton")
        self.themeButton = HeaderIconButton(owner, "☾", "主题", "切换配色", owner.toggle_theme, "themeButton")
        self.menuButton = HeaderTextButton(owner, "菜单", "菜单", "菜单", self._open_menu, "menuButton")
        for b in (self.addButton, self.themeButton, self.menuButton):
            lay.addWidget(b)
            lay.addSpacing(6)
        self.settings_menu = QMenu(self)
        for text, callback in (("设定…", owner.configure), ("游玩记录", owner.show_record),
                               ("换库…", owner.choose_library), ("新建库…", owner.new_library),
                               ("从旧版导入…", owner.show_transfer), ("两库只读对比", owner.open_compare)):
            self.settings_menu.addAction(text, callback)
        self.window_controls = WindowControls(owner, self)
        lay.addWidget(self.window_controls)
        self.buttons = {"library": self.navLibrary, "tidy": self.navTidy,
                        "scan": self.navLibrary, "source": self.navLibrary, "record": self.navLibrary}
        self.apply_theme()

    def _add(self):
        owner = self.owner
        if getattr(owner.scan_page, "plan", None) is not None:
            owner.show_scan()
        else:
            owner.show_sources()

    @staticmethod
    def _search_icon(theme):
        """A 14px vector magnifier; only theme roles are used for its colour."""
        size = 20
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(icon_pen(theme.color("sub")))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(3, 3, 9, 9))
        p.drawLine(QPointF(10.5, 10.5), QPointF(16, 16))
        p.end()
        return QIcon(pix)

    def set_search(self, widget):
        """Put the (single) library search box before ＋ and theme it."""
        assert isinstance(widget, QLineEdit)
        self.search = widget
        widget.setParent(self)
        widget.setObjectName("headerSearch")
        widget.setPlaceholderText("搜索标题、品牌、标签")
        widget.setClearButtonEnabled(True)
        self._search_action = QAction(self._search_icon(self.owner.theme), "", widget)
        widget.addAction(self._search_action, QLineEdit.ActionPosition.LeadingPosition)
        self._apply_search_style()
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self.addButton), widget)
        widget.hide()
        self._resize_search()

    def _apply_search_style(self):
        s = self.search
        if s is None:
            return
        c = self.owner.theme.roles
        s.setStyleSheet(f"QLineEdit {{ background:{c['chip']}; color:{c['ink']}; border:0; border-radius:10px; "
                         f"padding:0 10px 0 6px; selection-background-color:{c['usube']}; }} "
                         f"QLineEdit::placeholder {{ color:{c['faint']}; }}")
        if self._search_action is not None:
            self._search_action.setIcon(self._search_icon(self.owner.theme))

    def _resize_search(self):
        if self.search is not None:
            width = 340 if self.owner.width() >= 1200 else 220
            self.search.setFixedSize(width, 34)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_search()

    def _open_menu(self):
        self.settings_menu.exec(self.menuButton.mapToGlobal(QPoint(0, self.menuButton.height())))

    def apply_theme(self):
        self.logo.update()
        self.themeButton.setText("☀" if self.owner.theme.name == "night" else "☾")
        self._apply_search_style()

    def set_library_path(self, path):
        self.logo.setToolTip(path or "")

    def set_page(self, name):
        self.navLibrary.selected = name == "library"
        self.navTidy.selected = name == "tidy"
        self.navLibrary.update()
        self.navTidy.update()
        self.window_controls.sync_state()
        self.addButton.dot = getattr(self.owner.scan_page, "plan", None) is not None
        self.addButton.update()

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
        t = self.owner.theme
        p.fillRect(self.rect(), t.color("wall" if t.name == "media" else "paper"))
        if t.name != "media":
            p.fillRect(QRect(0, self.height() - 1, self.width(), 1), t.color("line"))


class Adv(QWidget):
    clicked = Signal()

    def __init__(self, owner, text="", pose="stamp", height=245):
        super().__init__()
        self.owner, self.text, self.pose = owner, text, pose
        self.subtitle = ""
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tick = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(120)

    def _animate(self):
        self.tick += 1
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(10, self.height() - 122, self.width() - 20, 112)
        if self.owner.show_mascot and self.pose and self.owner.theme.name != "media":
            pix = asset(f"shiori/s2-{self.pose}.png")
            # Full-height character at 176 px wide, clipped above the message panel.
            scaled = pix.scaledToWidth(176, Qt.TransformationMode.SmoothTransformation)
            p.save()
            p.setClipRect(QRectF(self.width() - 190, 0, 186, box.top() + 60))
            p.drawPixmap(self.width() - 186, max(0, int(box.top()) - 145), scaled)
            p.restore()
        g = QLinearGradient(0, box.top(), 0, box.bottom())
        g.setColorAt(0, t.color("card"))
        g.setColorAt(1, t.color("usube"))
        rounded(p, box, QBrush(g), 14, t.color("white" if t.name == "day" else "line"), 2)
        rounded(p, box.adjusted(5, 5, -5, -5), Qt.BrushStyle.NoBrush, 10, t.color("sakura"))
        if t.name != "media":
            name = QRectF(28, box.top() - 13, 136, 28)
            rounded(p, name, t.color("beni"), 14)
            p.setPen(t.color("white"))
            p.setFont(font("title", 14, True))
            p.drawText(name, Qt.AlignmentFlag.AlignCenter, "栞   SHIORI")
        p.setPen(t.color("ink"))
        p.setFont(font("body", 14))
        p.drawText(box.adjusted(22, 24, -25, -15 if not self.subtitle else -32),
                   Qt.TextFlag.TextWordWrap, self.text)
        if self.subtitle:
            p.setFont(font("body", 11))
            p.setPen(t.color("sub"))
            p.drawText(box.adjusted(22, 75, -22, -10), Qt.TextFlag.TextWordWrap, self.subtitle)
        p.setPen(t.color("beni"))
        p.drawText(int(box.right()) - 23, int(box.bottom()) - 12 + int(math.sin(self.tick * .63)), "▼")


class Status(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setFixedHeight(28)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(26, 1, 26, 1)
        self.message = QLabel("先选择一个库文件")
        self.progress = QProgressBar()
        self.progress.setFixedSize(150, 5)
        self.progress.setTextVisible(False)
        self.cancel = Button(owner, "取消", owner.cancel_job)
        self.cancel.setMinimumHeight(24)
        self.cancel.setFixedSize(55, 24)
        self.right = QLabel("库版本 2 · GalPanel 2")
        self.playing = QLabel()
        self.playing.hide()
        lay.addWidget(self.message)
        lay.addWidget(self.progress)
        lay.addWidget(self.cancel)
        lay.addStretch()
        lay.addWidget(self.playing)
        lay.addWidget(self.right)
        self.idle("先选择一个库文件")

    def idle(self, message):
        self.message.setText(message)
        self.progress.hide()
        self.cancel.hide()

    def busy(self, message, cancellable=True):
        self.message.setText(message)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel.setVisible(cancellable)
        self.cancel.setEnabled(cancellable)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.fillRect(self.rect(), t.color("paper"))
        p.fillRect(QRect(0, 0, self.width(), 1), t.color("line"))
        self.message.setStyleSheet(f"color:{t.roles['sub']}; font-size:11px")
        self.playing.setStyleSheet(f"color:{t.roles['beni']}; font-size:11px")
        self.right.setStyleSheet(f"color:{t.roles['faint']}; font-size:11px")
        self.progress.setStyleSheet(f"QProgressBar {{ background:{t.roles['line']}; border:0; border-radius:2px }} QProgressBar::chunk {{ background:{t.roles['beni']}; border-radius:2px }}")
