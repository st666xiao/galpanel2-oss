"""Frameless-window helpers: edge grips and blank-area dragging (T3 / fix-1).

Resizing uses eight small transparent child widgets on the window border
instead of an event filter on the application: every Qt event would otherwise
have to pass through Python, which slowed bulk widget rebuilds noticeably.
"""
import math
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QCursor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QCheckBox,
                               QComboBox, QLineEdit, QSpinBox, QTextEdit, QWidget)

EDGE = 5
CORNER = 10
INTERACTIVE = (QAbstractButton, QLineEdit, QTextEdit, QComboBox, QSpinBox, QCheckBox,
               QAbstractItemView)


def interactive_at(widget, pos):
    """True when pos (in widget coordinates) lands on a button / input / list."""
    child = widget.childAt(pos)
    seen = set()
    while child is not None and id(child) not in seen:
        seen.add(id(child))
        if isinstance(child, INTERACTIVE):
            return True
        pos = child.mapFromParent(pos)
        parent = child
        child = parent.childAt(pos)
    return False


def start_move(window):
    handle = window.windowHandle()
    if handle is not None and not window.isMaximized():
        handle.startSystemMove()


def toggle_maximize(window):
    if window.isMaximized():
        window.showNormal()
    else:
        window.showMaximized()


def icon_pen(color):
    """The 1.5px round-cap pen shared by all frameless-shell vector icons."""
    return QPen(color, 1.5, Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def draw_icon(p, text, cx, cy, size, color):
    """Paint a header / window-control icon as vectors; never a font glyph."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(icon_pen(color))
    p.setBrush(Qt.BrushStyle.NoBrush)
    if text == "＋":
        k = size / 14
        a = 5 * k
        p.drawLine(QPointF(cx - a, cy), QPointF(cx + a, cy))
        p.drawLine(QPointF(cx, cy - a), QPointF(cx, cy + a))
    elif text == "☾":
        k = size / 14
        outer = QPainterPath()
        outer.addEllipse(QRectF(cx - 7 * k, cy - 7 * k, 14 * k, 14 * k))
        cut = QPainterPath()
        cut.addEllipse(QRectF(cx - 1 * k, cy - 10 * k, 14 * k, 14 * k))
        p.fillPath(outer.subtracted(cut), color)
    elif text == "☀":
        k = size / 14
        p.drawEllipse(QRectF(cx - 3.5 * k, cy - 3.5 * k, 7 * k, 7 * k))
        for i in range(8):
            a = math.radians(i * 45)
            p.drawLine(QPointF(cx + 5.5 * k * math.cos(a), cy + 5.5 * k * math.sin(a)),
                       QPointF(cx + 8 * k * math.cos(a), cy + 8 * k * math.sin(a)))
    elif text == "⚙":
        k = size / 14
        p.drawEllipse(QRectF(cx - 5 * k, cy - 5 * k, 10 * k, 10 * k))
        p.drawEllipse(QRectF(cx - 2 * k, cy - 2 * k, 4 * k, 4 * k))
        for i in range(6):
            a = math.radians(i * 60)
            p.drawLine(QPointF(cx + 5 * k * math.cos(a), cy + 5 * k * math.sin(a)),
                       QPointF(cx + 7 * k * math.cos(a), cy + 7 * k * math.sin(a)))
    elif text == "─":
        k = size / 10
        p.drawLine(QPointF(cx - 4 * k, cy), QPointF(cx + 4 * k, cy))
    elif text == "☐":
        k = size / 10
        p.drawRect(QRectF(cx - 4 * k, cy - 4 * k, 8 * k, 8 * k))
    elif text == "❐":
        k = size / 10
        back = QRectF(cx - 3 * k, cy - 5 * k, 8 * k, 8 * k)
        p.drawLine(QPointF(back.left(), back.top()), QPointF(back.right(), back.top()))
        p.drawLine(QPointF(back.right(), back.top()), QPointF(back.right(), back.bottom()))
        p.drawRect(QRectF(cx - 5 * k, cy - 3 * k, 8 * k, 8 * k))
    elif text == "✕":
        k = size / 10
        p.drawLine(QPointF(cx - 4 * k, cy - 4 * k), QPointF(cx + 4 * k, cy + 4 * k))
        p.drawLine(QPointF(cx + 4 * k, cy - 4 * k), QPointF(cx - 4 * k, cy + 4 * k))
    p.restore()


# (name, Qt.Edge combination, cursor shape, geometry kind)
GRIPS = (
    ("top", Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor, "top"),
    ("bottom", Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor, "bottom"),
    ("left", Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor, "left"),
    ("right", Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor, "right"),
    ("topLeft", Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.CursorShape.SizeFDiagCursor, "topLeft"),
    ("bottomRight", Qt.Edge.BottomEdge | Qt.Edge.RightEdge, Qt.CursorShape.SizeFDiagCursor, "bottomRight"),
    ("topRight", Qt.Edge.TopEdge | Qt.Edge.RightEdge, Qt.CursorShape.SizeBDiagCursor, "topRight"),
    ("bottomLeft", Qt.Edge.BottomEdge | Qt.Edge.LeftEdge, Qt.CursorShape.SizeBDiagCursor, "bottomLeft"),
)


class EdgeGrip(QWidget):
    """A 5px edge or 10×10 corner handle that starts a system resize."""

    def __init__(self, window, name, edges, shape):
        super().__init__(window)
        self.setObjectName("edgeGrip-" + name)
        self.edges = edges
        self.grip_kind = name
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setCursor(QCursor(shape))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            top = self.window()
            handle = top.windowHandle() if top is not None else None
            if handle is not None:
                handle.startSystemResize(self.edges)
        event.accept()


class FrameHelper:
    """Owns the eight EdgeGrip children of one frameless Window."""

    def __init__(self, window):
        self.window = window
        self.installed = True
        self.grips = []
        for name, edges, shape, _ in GRIPS:
            grip = EdgeGrip(window, name, edges, shape)
            self.grips.append(grip)
        self.layout()
        self.sync_state()

    def layout(self):
        w, h = self.window.width(), self.window.height()
        e, c = EDGE, CORNER
        boxes = {
            "top": (c, 0, w - 2 * c, e),
            "bottom": (c, h - e, w - 2 * c, e),
            "left": (0, c, e, h - 2 * c),
            "right": (w - e, c, e, h - 2 * c),
            "topLeft": (0, 0, c, c),
            "topRight": (w - c, 0, c, c),
            "bottomLeft": (0, h - c, c, c),
            "bottomRight": (w - c, h - c, c, c),
        }
        for grip in self.grips:
            x, y, gw, gh = boxes[grip.grip_kind]
            grip.setGeometry(x, y, max(1, gw), max(1, gh))
            grip.raise_()

    def sync_state(self):
        visible = not (self.window.isMaximized() or self.window.isMinimized())
        for grip in self.grips:
            grip.setVisible(visible)

    def uninstall(self):
        """Called from Window.closeEvent once the close is finally accepted."""
        self.installed = False
        for grip in self.grips:
            grip.hide()
