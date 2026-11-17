from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import html as html_mod
import re
import time
from PySide6.QtCore import Qt, QSize, QRectF, QRect, QPointF, QAbstractListModel, QModelIndex, QTimer, QEvent
from PySide6.QtGui import (QPainter, QPen, QLinearGradient, QBrush, QPixmap, QImage, QPainterPath, QShortcut, QKeySequence,
                           QFontMetrics, QColor, QIcon)
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QListView,
    QStyledItemDelegate, QAbstractItemView, QMenu, QMessageBox, QDialog, QFormLayout, QTextEdit, QCheckBox,
    QFileDialog, QPlainTextEdit, QScrollArea, QFrame, QSizePolicy, QPushButton, QAbstractButton, QStackedWidget)

from galpanel2.core import repository as repo, play, meta, binding, records, shelf, storage
from galpanel2.core.domain import WriteConflict, DuplicateName
from .theme import asset, font
from .widgets import Paper, Button, rounded
from .metawidgets import MetaButton, MetaPopover, RoundedPanel, MetaChip, FlowLayout, TokenInput, clear_layout


def local_played_date(pt):
    if pt.last_played_source == "session" and pt.last_played_at:
        return datetime.fromisoformat(pt.last_played_at.replace("Z", "+00:00")).astimezone().date().isoformat()
    return pt.last_played_at or "—"


def _last_played_instant(pt):
    try:
        played = datetime.fromisoformat(pt.last_played_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    # Legacy dates have no time or offset and represent local midnight.
    return played.astimezone(timezone.utc)


def recent(added_at):
    try:
        age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(added_at.replace("Z", "+00:00")).date()).days
        return 0 <= age <= 14
    except (ValueError, TypeError):
        return False


FILTERS = ("全部", "最近", "待整理", "压缩包", "不可访问", "隐藏")
SEGMENTS = ("全部", "未绑定", "最近玩")
STATE_CHIPS = (("压缩包", "压缩包"), ("不可访问", "不可访问"), ("待整理", "待整理"),
               ("最近加入", "最近"), ("隐藏", "隐藏"))


def matches_filter(w, name, metas=None, links=None):
    """Legacy filters plus the two new segments (old call signatures are intact)."""
    if name == "隐藏":
        return w.hidden
    if w.hidden:
        return False
    if name == "未绑定":
        return not (links.get(w.id) if links is not None else {})
    if name == "最近玩":
        return False
    pending = not w.has_cover or (not metas.get(w.id, meta.WorkMeta(w.id, (), ())).brand_ids if metas is not None else not w.developer)
    return {"全部": True, "最近": recent(w.added_at), "待整理": pending,
            "压缩包": is_packed_only(w), "不可访问": w.availability == "missing"}[name]


def filter_counts(works, metas=None):
    return {name: sum(matches_filter(w, name, metas) for w in works) for name in FILTERS}


def _played_recently(pt, today=None):
    """The last session/legacy date converted to a local date, within 30 days."""
    raw = local_played_date(pt) if pt else "—"
    if not raw or raw == "—":
        return False
    try:
        played = date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        return False
    today = today or date.today()
    return 0 <= (today - played).days <= 30


def segment_counts(works, links=None, playtimes=None):
    links = links or {}
    playtimes = playtimes or {}
    visible = [w for w in works if not w.hidden]
    return {"全部": len(visible),
            "未绑定": sum(not links.get(w.id) for w in visible),
            "最近玩": sum(_played_recently(playtimes.get(w.id)) for w in visible)}


def load_projection(lib, cancel=None):
    from galpanel2.core.domain import check_cancel
    works = repo.list_works(lib, include_hidden=True)
    details, images = {}, {}
    for w in works:
        check_cancel(cancel)
        detail = repo.get_work(lib, w.id)
        details[w.id] = detail
        if detail.cover_path:
            image = QImage(detail.cover_path)
            if not image.isNull():
                images[w.id] = image.scaled(460, 670, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return (works, details, images, repo.list_sources(lib), play.list_playtimes(lib),
            meta.list_work_meta(lib), meta.list_brands(lib), meta.list_tags(lib), meta.list_tag_groups(lib),
            meta.developer_texts(lib), binding.list_titles(lib), binding.list_links(lib),
            {w.id:binding.get_info(lib,w.id) for w in works}, shelf.list_shelf(lib))


def draw_image(p, rect, pix, radius=8):
    if pix.isNull():
        return
    p.save()
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    p.setClipPath(clip)
    ratio = max(rect.width() / pix.width(), rect.height() / pix.height())
    w, h = pix.width() * ratio, pix.height() * ratio
    p.drawPixmap(QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h), pix, QRectF(pix.rect()))
    p.restore()


def poster_mode(width: int, height: int) -> str:
    """Vertical-ish art fills the poster; everything else is letterboxed."""
    if width <= 0 or height <= 0:
        return "contain"
    ratio = width / height
    return "cover" if 0.55 <= ratio <= 0.85 else "contain"


def location_tag(location: str) -> str | None:
    """The cold/deleted chip drawn before the ordinary file tag."""
    if location in ("冷藏", "已删除"):
        return location
    return None


def _drive_letter(path: str) -> str | None:
    match = re.match(r"^([A-Za-z]):", str(path or ""))
    return match.group(1).upper() if match else None


def source_short_name(source) -> str:
    """A label, or the drive letter / last path segment for an unlabelled source."""
    label = (getattr(source, "label", "") or "").strip()
    if label:
        return label
    path = getattr(source, "path", "") or ""
    letter = _drive_letter(path)
    if letter:
        return letter
    parts = [part for part in re.split(r"[\\/]", path) if part]
    return parts[-1] if parts else path


def source_path_tail(path: str, levels: int = 2) -> str:
    parts = [part for part in re.split(r"[\\/]", str(path or "")) if part]
    if not parts:
        return str(path or "")
    if re.match(r"^[A-Za-z]:$", parts[0]):
        parts = parts[1:]
    return "\\".join(parts[-levels:]) if parts else str(path)


def source_display_name(source) -> str:
    """Label when set, otherwise the full folder path (details page)."""
    label = (getattr(source, "label", "") or "").strip()
    return label or getattr(source, "path", "") or ""


def folder_key(source_id: int) -> str:
    return f"folder:{int(source_id)}"


def folder_badge_text(copies, sources, storage_map=None) -> str | None:
    """The top-right folder chip: D, G, or D+G for a work spread over drives."""
    storage_map = storage_map or {}
    names = []
    for copy in copies:
        stored = storage_map.get(getattr(copy, "id", None))
        if stored is not None and stored.location != "常用":
            continue
        # Drive letter first (the user wants to see which disk); a label only
        # stands in for paths without one (UNC shares etc.).
        name = _drive_letter(getattr(copy, "path", ""))
        source = sources.get(getattr(copy, "source_id", None))
        if not name and source is not None:
            name = source_short_name(source)
        if not name:
            parts = [part for part in re.split(r"[\\/]", str(getattr(copy, "path", ""))) if part]
            name = parts[-1] if parts else "?"
        if name and name not in names:
            names.append(name)
    return "+".join(names) if names else None


WORK_CHECK_INTERVAL = 60


def poster_is_gray(work) -> bool:
    """A card is grey only when every registered copy is gone."""
    return work.availability == "missing"


def is_packed_only(work) -> bool:
    """Only archives, no unpacked folder: a zip kept beside its extracted
    folder does not make the work "未解压"."""
    return "archive" in work.kinds and "folder" not in work.kinds


def file_tag(work) -> str:
    """The small dark chip at a card's bottom-left.

    A deleted work must say so before the archive hint: if a zip itself was
    removed it is still "gone", not merely "not unpacked".
    """
    if work.availability == "missing":
        return "已失效"
    if work.availability == "partial":
        return "部分失效"
    if is_packed_only(work):
        return "未解压"
    if work.hidden:
        return "已隐藏"
    return ""


def ambient_color(image: QImage):
    """Average colour of an image (shrink to 1x1); None for an empty image."""
    if image is None or image.isNull():
        return None
    tiny = image.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    if tiny.isNull():
        return None
    return QColor(tiny.pixelColor(0, 0))


def draw_poster(p, rect, pix, radius=10):
    """Draw a cover using the cover/contain rule shared by cards and the hero."""
    if pix.isNull():
        return
    p.save()
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    p.setClipPath(clip)
    mode = poster_mode(pix.width(), pix.height())
    if mode == "cover":
        ratio = max(rect.width() / pix.width(), rect.height() / pix.height())
        w, h = pix.width() * ratio, pix.height() * ratio
        p.drawPixmap(QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h), pix, QRectF(pix.rect()))
    else:
        # A dimmed, blurred-looking copy fills the frame behind the full image.
        ratio = max(rect.width() / pix.width(), rect.height() / pix.height())
        w, h = pix.width() * ratio, pix.height() * ratio
        p.drawPixmap(QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h), pix, QRectF(pix.rect()))
        p.fillRect(QRectF(rect), QColor(0, 0, 0, 89))  # ~0.35 opacity over black
        scaled = pix.scaled(int(rect.width()), int(rect.height()), Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap(QRectF(rect.center().x() - scaled.width() / 2, rect.center().y() - scaled.height() / 2,
                            scaled.width(), scaled.height()), scaled, QRectF(scaled.rect()))
    p.restore()


class WorkModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.works = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.works)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        w = self.works[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return w.title
        if role == Qt.ItemDataRole.UserRole:
            return w
        if role == Qt.ItemDataRole.ToolTipRole:
            return w.title + "\n" + w.developer

    def reset(self, works):
        self.beginResetModel()
        self.works = works
        self.endResetModel()


class CoverCache:
    def __init__(self):
        self.images = {}
        self.gray = {}

    def load(self, works, images):
        # One entry per work (older performance tests count this dict). A null
        # pixmap means "no cover"; cards draw a typographic placeholder instead.
        self.images = {w.id: (QPixmap.fromImage(images[w.id]) if w.id in images and not images[w.id].isNull()
                             else QPixmap()) for w in works}
        self.gray = {}

    def has_cover(self, wid):
        pix = self.images.get(wid)
        return pix is not None and not pix.isNull()

    def get(self, wid, gray=False):
        """Always a usable QPixmap: other pages (scan/compare) still show jackets.

        Card/detail widgets ask `has_cover()` first and draw the placeholder
        themselves when it is False.
        """
        pix = self.images.get(wid)
        if pix is None or pix.isNull():
            pix = asset(f"jacket/jacket-{wid % 8 + 1:02d}.jpg")
        if gray:
            if wid not in self.gray:
                self.gray[wid] = QPixmap.fromImage(pix.toImage().convertToFormat(QImage.Format.Format_Grayscale8))
            return self.gray[wid]
        return pix


class GhostButton(QAbstractButton):
    """Transparent, quiet text button used in the filter line and detail."""
    def __init__(self, owner, text, callback=None, parent=None, name=None, arrow=False):
        super().__init__(parent)
        self.owner = owner
        self.arrow = arrow
        self.highlight = False
        self.setText(text)
        self.setObjectName(name or "")
        self.highlight = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if callback:
            self.clicked.connect(callback)
        self._resize()

    def setText(self, text):
        super().setText(text)
        if hasattr(self, "owner"):
            self._resize()

    def _resize(self):
        if hasattr(self, "arrow"):
            w = QFontMetrics(font("body", 12.5)).horizontalAdvance(self.text()) + 26 + (12 if self.arrow else 0)
            self.setFixedSize(w, 30)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(1 if self.isEnabled() else .4)
        if self.highlight or self.underMouse():
            bg = t.color("usube" if self.highlight else "wall")
            rounded(p, QRectF(self.rect()), bg, 8)
        color = t.color("beni" if self.highlight else "sub")
        p.setPen(color)
        p.setFont(font("body", 12.5, self.highlight))
        text_rect = QRectF(self.rect()).adjusted(10, 0, -(20 if self.arrow else 10), 0)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        if self.arrow:
            p.setPen(QPen(t.color("faint"), 1.5))
            x, y = self.width() - 15, self.height() / 2 - 2
            p.drawLine(QPointF(x - 3, y), QPointF(x, y + 3))
            p.drawLine(QPointF(x, y + 3), QPointF(x + 3, y))


class DetailMoreButton(Button):
    """Button whose dropdown chevron is painted (the ▾ glyph shows as a box on Windows)."""

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(self.palette().buttonText().color(), 1.5))
        x, y = self.width() - 22, self.height() / 2 - 2
        p.drawLine(QPointF(x - 4, y), QPointF(x, y + 4))
        p.drawLine(QPointF(x, y + 4), QPointF(x + 4, y))
        p.end()


class SegmentButton(QAbstractButton):
    def __init__(self, owner, label, accessible, callback, name):
        super().__init__()
        self.owner = owner
        self.setObjectName(name)
        self.setAccessibleName(accessible)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(callback)
        self.label = label
        self.count = 0
        self._resize()

    def set_label(self, text):
        self.label = text
        self.setText(text)
        self._resize()

    def _resize(self):
        self.setFixedHeight(28)
        self.setMinimumWidth(QFontMetrics(font("body", 12.5)).horizontalAdvance(self.text()) + 28)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isChecked():
            rounded(p, QRectF(self.rect()), t.color("card"), 7, t.color("line"))
        p.setFont(font("body", 12.5, self.isChecked()))
        label = self.text() or ""
        digits = len(label) - len(label.rstrip("0123456789"))
        name, number = (label[:-digits].rstrip(), label[-digits:]) if digits else (label, "")
        p.setPen(t.color("ink" if self.isChecked() else "sub"))
        nw = QFontMetrics(p.font()).horizontalAdvance(number) if number else 0
        left = QRectF(14, 0, max(1, self.width() - 28 - nw - 4), self.height())
        p.drawText(left, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        if number:
            p.setPen(t.color("faint"))
            p.drawText(QRectF(left.right(), 0, nw + 4, self.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, number)


class SegmentBar(QFrame):
    def __init__(self, owner, page):
        super().__init__()
        self.owner = owner
        self.setObjectName("segmentBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.buttons = {}
        for name, obj, accessible in (("全部", "segAll", "全部"), ("未绑定", "segUnbound", "未绑定"),
                                     ("最近玩", "segRecent", "最近玩")):
            b = SegmentButton(owner, name, accessible, lambda checked=False, n=name: page.set_filter(n), obj)
            self.buttons[name] = b
            lay.addWidget(b)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()), self.owner.theme.color("chip"), 9)


class CoverDelegate(QStyledItemDelegate):
    PAD = 8
    GAP_X, GAP_Y = 18, 16
    MIN_W, MAX_W = 176, 240

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.card_width = 180
        self.hover_row = -1

    def card_height(self):
        # poster (2:3 of the padded width) + two text lines (45) + top/bottom padding
        return int((self.card_width - 2 * self.PAD) * 1.5) + 2 * self.PAD + 45

    def sizeHint(self, option, index):
        return QSize(self.card_width, self.card_height())

    def card_rect(self, item_rect):
        return QRectF(item_rect).adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)

    def _art_rect(self, item_rect):
        r = self.card_rect(item_rect)
        return QRectF(r.left(), r.top(), r.width(), r.width() * 1.5)

    def _work_at(self, item_rect):
        """Resolve the work for an item rectangle passed in from outside paint()."""
        view = self.page.view
        index = view.indexAt(QRectF(item_rect).center().toPoint())
        return index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None

    def find_rect(self, item_rect: QRect, work=None) -> QRectF | None:
        """The '找资料' tag in viewport coordinates, or None.

        `work` can be supplied by paint() so that no viewport layout query is
        made while a QPainter is active (that recurses into Qt and crashes).
        """
        rect = QRectF(item_rect)
        work = work if work is not None else self._work_at(rect)
        if work is None or self.page.links.get(work.id) or not self.page.writable():
            return None
        art = self._art_rect(rect)
        return self._tag_rect(art, "找资料", right=True)

    def _tag_rect(self, art, text, right=False):
        fm = QFontMetrics(font("body", 11))
        width = fm.horizontalAdvance(text) + 16
        height = fm.height() + 2
        x = art.right() - 8 - width if right else art.left() + 8
        return QRectF(x, art.bottom() - 8 - height, width, height)

    def paint(self, p, option, index):
        w = index.data(Qt.ItemDataRole.UserRole)
        if w is None:
            return
        t = self.page.owner.theme
        selected = index in self.page.view.selectionModel().selectedIndexes()
        multi = len(self.page.view.selectionModel().selectedIndexes()) > 1
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = self.card_rect(option.rect)
        art = self._art_rect(option.rect)
        if selected:
            p.setPen(QPen(t.color("beni"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(art.adjusted(-3, -3, 3, 3), 10, 10)
            p.setPen(QPen(t.color("paper"), 2))
            p.drawRoundedRect(art.adjusted(-1, -1, 1, 1), 9, 9)
        has_cover = self.page.cache.has_cover(w.id)
        pix = self.page.cache.get(w.id, poster_is_gray(w)) if has_cover else None
        hidden = w.hidden
        if pix is not None:
            p.save()
            if hidden:
                p.setOpacity(.45)
            draw_poster(p, art, pix, 10)
            p.restore()
            if t.name != "media":
                p.setPen(QPen(t.color("line"), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(art, 8, 8)
        else:
            p.save()
            if hidden:
                p.setOpacity(.45)
            rounded(p, art, t.color("placeholder"), 10, t.color("line"))
            p.setPen(t.color("sub"))
            p.setFont(font("body", 15, True))
            fm = p.fontMetrics()
            words, lines, current = str(w.title), [], ""
            for char in words:
                if char == "\n" or fm.horizontalAdvance(current + char) > art.width() - 32:
                    lines.append(current)
                    current = ""
                    if len(lines) == 4:
                        break
                    if char == "\n":
                        continue
                current += char
            if len(lines) < 4 and current:
                lines.append(current)
            if len(lines) == 4 and "".join(lines) != words:
                lines[-1] = fm.elidedText(lines[-1] + "…", Qt.TextElideMode.ElideRight, int(art.width() - 32))
            box = QRectF(art.left() + 16, art.top(), art.width() - 32, art.height())
            p.drawText(box, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, "\n".join(lines))
            p.restore()
        # Status badge: a rounded chip in the poster's top-left corner.
        st = self.page.status_of(w.id)
        STATUS_ROLES = {"想玩": "status_want", "在玩": "status_playing", "玩过": "status_done", "不玩了": "status_dropped"}
        if st in STATUS_ROLES:
            badge = QRectF(art.left() + 8, art.top() + 8, QFontMetrics(font("body", 11, True)).horizontalAdvance(st) + 16, 20)
            rounded(p, badge, t.color(STATUS_ROLES[st]), 10)
            p.setPen(t.color("on_accent"))
            p.setFont(font("body", 11, True))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, st)
        # Folder badge: top-right so it never covers the status chip on the left.
        folder = self.page.folder_badge(w.id)
        if folder:
            p.setFont(font("body", 10.5, True))
            fm = p.fontMetrics()
            label = fm.elidedText(folder, Qt.TextElideMode.ElideRight, 74)
            badge = QRectF(art.right() - 8 - max(26, fm.horizontalAdvance(label) + 14),
                           art.top() + 8, max(26, fm.horizontalAdvance(label) + 14), 18)
            rounded(p, badge, t.color("white" if t.name == "day" else "card", 238), 9,
                    t.color("line"))
            p.setPen(t.color("sub"))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)
        # Small flat file/status tags at the bottom-left; a location tag wins
        # over the normal file tag (a deleted work must not say "不在").
        tag_text = location_tag(self.page.location_of(w.id)) or file_tag(w)
        if tag_text:
            p.setFont(font("body", 11))
            tag = self._tag_rect(art, tag_text)
            rounded(p, tag, t.color("white" if t.name == "day" else "card", 242), 6)
            p.setPen(t.color("sub"))
            p.drawText(tag, Qt.AlignmentFlag.AlignCenter, tag_text)
        find = self.find_rect(option.rect, w)
        if find is not None:
            hover = self.hover_row == index.row()
            rounded(p, find, t.color("beni") if hover else t.color("white" if t.name == "day" else "card", 242), 6)
            p.setPen(t.color("on_accent") if hover else t.color("beni"))
            p.setFont(font("body", 11))
            p.drawText(find, Qt.AlignmentFlag.AlignCenter, "找资料")
        if selected and multi:
            b = QRectF(card.right() - 18, card.top() + 4, 22, 22)
            rounded(p, b, t.color("beni"), 11, t.color("on_accent"), 1)
            p.setPen(QPen(t.color("on_accent"), 1.8))
            c = b.center()
            p.drawLine(QPointF(c.x() - 5, c.y()), QPointF(c.x() - 1, c.y() + 4))
            p.drawLine(QPointF(c.x() - 1, c.y() + 4), QPointF(c.x() + 5, c.y() - 4))
        p.setPen(t.color("ink"))
        p.setFont(font("body", 13))
        title = p.fontMetrics().elidedText(w.title, Qt.TextElideMode.ElideRight, int(card.width()))
        p.drawText(QRectF(card.left(), art.bottom() + 7, card.width(), 19), title)
        p.setPen(t.color("faint"))
        p.setFont(font("body", 11.5))
        byline = self.page.byline(w)
        if byline == "社团未整理":
            byline = "—"
        p.drawText(QRectF(card.left(), art.bottom() + 27, card.width(), 18),
                   p.fontMetrics().elidedText(byline, Qt.TextElideMode.ElideRight, int(card.width())))
        p.restore()


class ShelfView(QListView):
    """Mouse tracking + find-tag clicks; the filter only lives on the viewport."""
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_F:
            idx = self.currentIndex()
            if idx.isValid():
                w = idx.data(Qt.ItemDataRole.UserRole)
                if w is not None and self.page.writable():
                    self.page.owner.open_find(w.id)
                    return
        elif key == Qt.Key.Key_Slash:
            self.page.search.setFocus()
            self.page.search.selectAll()
            return
        elif key == Qt.Key.Key_Escape:
            if self.page.detail_open():
                self.page.close_detail()
                return
            if self.page.search.text():
                self.page.search.clear()
                return
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not self.page.detail_open():
                self.page.open_detail()
                return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is not self.viewport():
            return False
        delegate = self.itemDelegate()
        if event.type() == QEvent.Type.MouseMove:
            pos = event.position().toPoint()
            row = self.indexAt(pos).row() if self.indexAt(pos).isValid() else -1
            if row != delegate.hover_row:
                delegate.hover_row = row
                self.viewport().update()
        elif event.type() == QEvent.Type.Leave:
            if delegate.hover_row != -1:
                delegate.hover_row = -1
                self.viewport().update()
        elif event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            index = self.indexAt(pos)
            if index.isValid():
                tag = delegate.find_rect(self.visualRect(index))
                if tag is not None and tag.contains(QPointF(pos)):
                    w = index.data(Qt.ItemDataRole.UserRole)
                    if w is not None:
                        self.page.owner.open_find(w.id)
                        return True
        elif event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            index = self.indexAt(pos)
            if index.isValid():
                rect = self.visualRect(index)
                tag = delegate.find_rect(rect)
                if tag is not None and tag.contains(QPointF(pos)):
                    return False  # the "找资料" chip was handled on press
                mods = event.modifiers() | QApplication.keyboardModifiers()
                if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
                    return False  # Ctrl / Shift click is multi-select
                w = index.data(Qt.ItemDataRole.UserRole)
                if w is not None:
                    self.setCurrentIndex(index)
                    self.page.open_detail()
        return False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.page.update_grid()


STATUS_TEXT = {"ok": "可访问", "missing": "不在", "partial": "部分不在", "unknown": "未检查", "error": "检查错误"}


class Hero(QWidget):
    """A quiet, full-width cover plate; no blurred backdrop or overprinted text."""
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail
        self.setObjectName("detailHero")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(100)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return min(380, int(width * 1.5))

    def sizeHint(self):
        return QSize(312, self.heightForWidth(312))

    def paintEvent(self, event):
        t = self.detail.owner.theme
        work = self.detail.work
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        cache = self.detail.library_page.cache
        pix = cache.get(work.id) if work and cache.has_cover(work.id) else None
        if pix is not None:
            draw_poster(p, rect, pix, 14)
            if t.name != "media":
                p.setPen(QPen(t.color("line"), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect, 10, 10)
        else:
            rounded(p, rect, t.color("placeholder"), 14, t.color("line"))
            if work:
                p.setPen(t.color("sub"))
                p.setFont(font("body", 15, True))
                p.drawText(rect.adjusted(16, 0, -16, 0),
                           Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, work.title)


class WrapLabel(QLabel):
    """Plain text, wrapped to at most `max_lines` lines, with an ellipsis."""
    def __init__(self, size, color, max_lines=2, bold=False, family="body", parent=None):
        super().__init__(parent)
        self.max_lines = max_lines
        self._full = ""
        self.setFont(font(family, size, bold))
        self.setStyleSheet(f"color:{color}; background:transparent")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)

    def set_full_text(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        text, width = self._full, max(1, self.width())
        fm = QFontMetrics(self.font())
        lines, current = [], ""
        for char in text:
            if char == "\n" or fm.horizontalAdvance(current + char) > width:
                lines.append(current)
                current = ""
                if len(lines) == self.max_lines:
                    break
                if char == "\n":
                    continue
            current += char
        if len(lines) < self.max_lines and current:
            lines.append(current)
        if len(lines) == self.max_lines and "".join(lines) != text:
            lines[-1] = fm.elidedText(lines[-1] + "…", Qt.TextElideMode.ElideRight, width)
        super().setText("\n".join(lines))


class TagChip(QPushButton):
    def __init__(self, owner, text, callback=None, parent=None):
        super().__init__(text, parent)
        self.owner = owner
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(font("body", 11.5))
        self.setFixedHeight(24)
        self.setMinimumWidth(QFontMetrics(self.font()).horizontalAdvance(text) + 18)
        if callback:
            self.clicked.connect(callback)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()), t.color("chip"), 6)
        p.setFont(font("body", 11.5))
        p.setPen(t.color("sub"))
        p.drawText(QRectF(self.rect()).adjusted(9, 0, -9, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


class DetailFacts(QWidget):
    """The flat tag row + a plain-text, accessible description of the panel."""
    def __init__(self, detail):
        super().__init__()
        self.detail = detail
        self._text = ""
        self.ids = []
        self.expanded = False
        self.flow = FlowLayout(self, spacing=6)
        self.flow.setContentsMargins(0, 0, 0, 0)

    def sizeHint(self):
        return QSize(300, self.flow.heightForWidth(300))

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text or ""
        self.setAccessibleDescription(self._text)
        self.setToolTip(self._text)

    def set_tags(self, ids):
        """Preserve the legacy 'replace tag chips' semantics."""
        self.ids = list(ids)
        self.expanded = False
        self._render()
        self.setVisible(bool(self.ids))
        self.updateGeometry()

    def _expand(self):
        self.expanded = True
        self._render()

    def _render(self):
        clear_layout(self.flow)
        page = self.detail.library_page
        if not self.ids:
            return
        shown = self.ids if self.expanded else self.ids[:10]
        for i in shown:
            if i not in page.tags:
                continue
            chip = TagChip(self.detail.owner, page.tags[i].name,
                           lambda checked=False, tag_id=i: page.set_meta_state("tag", tag_id, "in"))
            self.flow.addWidget(chip)
            chip.show()
        rest = len(self.ids) - 10
        if rest > 0 and not self.expanded:
            more = TagChip(self.detail.owner, f"+{rest}", self._expand)
            self.flow.addWidget(more)
            more.show()
        self.adjustSize()


class ConditionBar(QWidget):
    """One or more beni pills on a usube background; ✕ removes a condition."""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), self.owner.theme.color("usube"), 13)


class Pill(QAbstractButton):
    def __init__(self, owner, text, callback=None):
        super().__init__()
        self.owner = owner
        self.setText(text)
        self.setFixedHeight(26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(QFontMetrics(font("body", 12)).horizontalAdvance(text) + 20)
        if callback:
            self.clicked.connect(callback)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()), self.owner.theme.color("usube"), 13)
        p.setPen(self.owner.theme.color("beni"))
        p.setFont(font("body", 12))
        p.drawText(QRectF(self.rect()).adjusted(10, 0, -10, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, self.text())


class StatusChoiceButton(QPushButton):
    """A checkable status button inside the detail page's capsule slot."""

    STATUS_ROLES = {"想玩": "status_want", "在玩": "status_playing",
                    "玩过": "status_done", "不玩了": "status_dropped"}

    def __init__(self, owner, name, parent=None):
        super().__init__(name, parent)
        self.owner = owner
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(28)
        self.setMinimumWidth(QFontMetrics(font("body", 12)).horizontalAdvance(name) + 30)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isChecked():
            rounded(p, QRectF(self.rect()), t.color(self.STATUS_ROLES[self.text()]), 14)
            p.setPen(t.color("on_accent"))
        else:
            p.setPen(t.color("sub"))
        p.setFont(font("body", 13, self.isChecked()))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())


class CapsuleSlot(QWidget):
    """The card-coloured rounded槽 that holds the four status choices."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()), self.owner.theme.color("card"), self.height() / 2)


class SidebarWidget(QWidget):
    """The 200px wall-coloured filter rail; no right-hand separator line."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), self.owner.theme.color("wall"))


class SideButton(QAbstractButton):
    """A selectable filter row in the left sidebar: name, count, optional dot."""

    def __init__(self, owner, name, dot_role=None, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.name = name
        self.dot_role = dot_role
        self._count = 0
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setText(f"{name} 0")

    def set_count(self, n):
        self._count = n
        self.setText(f"{self.name} {n}")
        self.update()

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        if on:
            rounded(p, QRectF(self.rect()), t.color("usube"), 10)
        elif self.underMouse():
            rounded(p, QRectF(self.rect()), t.color("card"), 10)
        x = 12
        if self.dot_role is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.color(self.dot_role))
            p.drawEllipse(QRectF(x, self.height() / 2 - 4, 8, 8))
            x += 18
        name = self.name
        count = str(self._count)
        p.setFont(font("body", 13, on))
        fm = p.fontMetrics()
        p.setPen(t.color("ink" if on else "sub"))
        p.drawText(QRectF(x, 0, max(1, fm.horizontalAdvance(name) + 6), self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        p.setFont(font("body", 12, on))
        p.setPen(t.color("beni" if on else "faint"))
        p.drawText(QRectF(self.rect()).adjusted(0, 0, -12, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, count)


class FindRow(QWidget):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail
        self.setObjectName("detailFindRow")
        self.setFixedHeight(54)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 10, 8)
        lay.setSpacing(8)
        self.label = QLabel("还没有封面和资料")
        self.label.setFont(font("body", 12.5))
        lay.addWidget(self.label)
        lay.addStretch(1)
        self.button = Button(detail.owner, "找资料", self._find, primary=True)
        self.button.setObjectName("detailFindButton")
        self.button.setFixedHeight(30)
        self.button.setFixedWidth(76)
        lay.addWidget(self.button)

    def _find(self):
        if self.detail.work:
            self.detail.owner.open_find(self.detail.work.id)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()), self.detail.owner.theme.color("usube"), 10)

    def apply_theme(self):
        t = self.detail.owner.theme
        self.label.setStyleSheet(f"color:{t.roles['beni']}; background:transparent")


class MoreButton(QAbstractButton):
    """42×42 button whose three dots are drawn, not rendered from a font."""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setText("⋯")
        self.setAccessibleName("更多")
        self.setFixedSize(42, 42)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), t.color("chip"), 9, t.color("line"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.color("sub"))
        for x in (self.width() / 2 - 7, self.width() / 2, self.width() / 2 + 7):
            p.drawEllipse(QRectF(x - 2, self.height() / 2 - 2, 4, 4))


class SelectionBar(QFrame):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setObjectName("selectionBar")
        self.setFixedHeight(44)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 4, 12, 4)
        lay.setSpacing(8)
        self.label = QLabel()
        self.label.setFont(font("body", 12.5))
        lay.addWidget(self.label)
        lay.addStretch(1)
        self.buttons = {}
        for text in ("找资料", "隐藏", "检查文件", "取消"):
            b = GhostButton(owner, text, None, name="selection" + text)
            self.buttons[text] = b
            lay.addWidget(b)
        self.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), self.owner.theme.color("card"), 22,
                self.owner.theme.color("line"), 1)

    def apply_theme(self):
        self.label.setStyleSheet(f"color:{self.owner.theme.roles['sub']}; background:transparent")


class EmptyState(QWidget):
    def __init__(self, owner, page):
        super().__init__(page.shelf)
        self.owner, self.page = owner, page
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setFont(font("body", 14))
        lay.addWidget(self.label, alignment=Qt.AlignmentFlag.AlignCenter)
        self.add_button = Button(owner, "添加游戏目录", owner.show_sources, primary=True)
        self.add_button.setObjectName("emptyAdd")
        self.add_button.setFixedWidth(180)
        self.add_button.setFixedHeight(38)
        lay.addWidget(self.add_button, alignment=Qt.AlignmentFlag.AlignCenter)
        self.clear_button = GhostButton(owner, "清除筛选", self._clear)
        self.clear_button.setObjectName("emptyClear")
        lay.addWidget(self.clear_button, alignment=Qt.AlignmentFlag.AlignCenter)
        self.hide()

    def _clear(self):
        self.page.search.clear()
        self.page.clear_meta_filter()
        self.page.set_filter("全部")

    def set_state(self, empty_library, filtered):
        self.label.setText("书架还是空的" if empty_library else "没有符合条件的作品")
        self.add_button.setVisible(empty_library)
        self.clear_button.setVisible(filtered)
        self.setVisible(empty_library or filtered)

    def apply_theme(self):
        self.label.setStyleSheet(f"color:{self.owner.theme.roles['sub']}; background:transparent")


class Detail(Paper):
    def __init__(self, owner, page):
        super().__init__(owner, wall=False)
        self.library_page = page
        self.work = None
        self.copy_index = 0
        self.aggregate = "unknown"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 48, 48, 32)
        outer.setSpacing(0)
        self.back_button = Button(owner, "← 返回", page.close_detail)
        self.back_button.setObjectName("detailBack")
        self.back_button.setFixedHeight(36)
        self.back_button.setMinimumWidth(90)
        outer.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignLeft)
        outer.addSpacing(20)
        self.empty_label = QLabel("选一部作品")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setFont(font("body", 14))
        outer.addWidget(self.empty_label, 1, Qt.AlignmentFlag.AlignCenter)
        self.content = QWidget()
        self.content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        outer.addWidget(self.content, 1)
        outer.addStretch(1)
        grid = QHBoxLayout(self.content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(44)
        # --- left: fixed 300x450 poster ---
        self.hero = Hero(self)
        self.hero.setFixedSize(300, 450)
        grid.addWidget(self.hero, 0, Qt.AlignmentFlag.AlignTop)
        # --- right: stacked text / controls ---
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        grid.addWidget(right, 1)
        self.title = WrapLabel(32, owner.theme.roles["ink"], 2, True, "body")
        self.subtitle = WrapLabel(14, owner.theme.roles["sub"], 1)
        rl.addWidget(self.title)
        rl.addSpacing(4)
        rl.addWidget(self.subtitle)
        rl.addSpacing(24)
        # button row
        self.start_button = Button(owner, "▶ 开始游戏", self.start_game, primary=True)
        self.start_button.setObjectName("detailStart")
        self.start_button.setFixedHeight(46)
        self.start_button.setMinimumWidth(190)
        self.open_button = Button(owner, "打开文件夹", self.open_folder)
        self.open_button.setObjectName("detailOpen")
        self.open_button.setFixedHeight(46)
        self.open_button.setMinimumWidth(130)
        self.saves_button = Button(owner, "存档", self.open_saves)
        self.saves_button.setObjectName("detailSaves")
        self.saves_button.setFixedHeight(46)
        self.find_button = Button(owner, "找资料", self._find)
        self.find_button.setObjectName("detailFind")
        self.find_button.setFixedHeight(46)
        self.find_button.setMinimumWidth(100)
        self.edit_button = Button(owner, "修改资料", self.edit)
        self.edit_button.setObjectName("detailEdit")
        self.edit_button.setFixedHeight(46)
        self.edit_button.setMinimumWidth(120)
        self.more_button = DetailMoreButton(owner, "更多    ", self._show_more)
        self.more_button.setObjectName("detailMore")
        self.more_button.setFixedHeight(46)
        self.more_button.setMinimumWidth(110)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(10)
        for b in (self.start_button, self.open_button, self.saves_button, self.find_button, self.edit_button, self.more_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        rl.addLayout(buttons)
        rl.addSpacing(22)
        # status + category row
        shelf_row = QHBoxLayout()
        shelf_row.setContentsMargins(0, 0, 0, 0)
        shelf_row.setSpacing(12)
        status_label = QLabel("状态")
        status_label.setFont(font("body", 12))
        shelf_row.addWidget(status_label)
        slot = CapsuleSlot(owner)
        slot_lay = QHBoxLayout(slot)
        slot_lay.setContentsMargins(3, 3, 3, 3)
        slot_lay.setSpacing(2)
        self.status_buttons = {}
        for name in shelf.STATUSES:
            b = StatusChoiceButton(owner, name)
            b.clicked.connect(lambda checked=False, n=name: self._status_choice_clicked(n))
            self.status_buttons[name] = b
            slot_lay.addWidget(b)
        slot.setFixedHeight(34)
        shelf_row.addWidget(slot)
        shelf_row.addSpacing(20)
        category_label = QLabel("分类")
        category_label.setFont(font("body", 12))
        shelf_row.addWidget(category_label)
        self.category_combo = QComboBox()
        self.category_combo.setObjectName("detailCategory")
        self.category_combo.addItems(["未分类", *shelf.CATEGORIES])
        self.category_combo.setFixedHeight(34)
        self.category_combo.activated.connect(self._category_activated)
        shelf_row.addWidget(self.category_combo)
        shelf_row.addStretch(1)
        rl.addLayout(shelf_row)
        rl.addSpacing(22)
        # three numbers
        self.stats = QWidget()
        sl = QHBoxLayout(self.stats)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.stat_values = {}
        for i, (key, label) in enumerate((("played", "玩过"), ("recent", "最近"), ("added", "加入"))):
            if i:
                line = QFrame()
                line.setFrameShape(QFrame.Shape.VLine)
                line.setStyleSheet(f"color:{owner.theme.roles['line']}")
                sl.addWidget(line)
            block = QWidget()
            bl = QVBoxLayout(block)
            bl.setContentsMargins(28 if i else 0, 0, 28, 0)
            bl.setSpacing(2)
            value = QLabel("—")
            value.setFont(font("body", 20, True))
            text = QLabel(label)
            text.setFont(font("body", 12))
            bl.addWidget(value)
            bl.addWidget(text)
            sl.addWidget(block)
            self.stat_values[key] = (value, text)
        sl.addStretch(1)
        rl.addWidget(self.stats)
        rl.addSpacing(24)
        self.summary = WrapLabel(14, "#CFCAC3", 12)
        rl.addWidget(self.summary)
        rl.addSpacing(14)
        self.facts = DetailFacts(self)
        self.facts.hide()
        rl.addWidget(self.facts)
        self.links_label = QLabel()
        self.links_label.setFont(font("body", 12.5))
        self.links_label.setTextFormat(Qt.TextFormat.RichText)
        self.links_label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.links_label.linkActivated.connect(self.activate_link)
        rl.addWidget(self.links_label)
        self.notes = WrapLabel(12.5, owner.theme.roles["faint"], 1)
        rl.addWidget(self.notes)
        self.path = QLabel()
        self.path.setFont(font("mono", 12))
        self.path.setTextFormat(Qt.TextFormat.RichText)
        rl.addWidget(self.path)
        self.copy_folders = QLabel()
        self.copy_folders.setObjectName("detailCopyFolders")
        self.copy_folders.setFont(font("body", 11.5))
        self.copy_folders.setTextFormat(Qt.TextFormat.RichText)
        self.copy_folders.setWordWrap(True)
        rl.addWidget(self.copy_folders)
        self.location = QLabel()
        self.location.setObjectName("detailLocation")
        self.location.setFont(font("body", 12))
        rl.addWidget(self.location)
        rl.addStretch(1)
        self.find_row = FindRow(self)
        self.find_row.hide()
        self.more_menu = QMenu(self)
        self.adv = None
        self.apply_theme()
        self.set_work(None)

    def paintEvent(self, event):
        t = self.owner.theme
        p = QPainter(self)
        p.fillRect(self.rect(), t.color("paper"))
        work = self.work
        if work is not None:
            cache = self.library_page.cache
            if cache.has_cover(work.id):
                c = ambient_color(cache.get(work.id).toImage())
                if c is not None:
                    g = QLinearGradient(0, 0, 0, 420)
                    top = QColor(c.red(), c.green(), c.blue(), 66)
                    bottom = QColor(c.red(), c.green(), c.blue(), 0)
                    g.setColorAt(0, top)
                    g.setColorAt(1, bottom)
                    p.fillRect(QRect(0, 0, self.width(), 420), QBrush(g))

    def _find(self):
        if self.work:
            self.owner.open_find(self.work.id)

    def _status_choice_clicked(self, name):
        current = self.library_page.status_of(self.work.id) if self.work else None
        self.set_status(None if current == name else name)

    def _category_activated(self, index):
        text = self.category_combo.itemText(index)
        self.set_category(None if text == "未分类" else text)

    def set_status(self, status):
        if not self.work or not self.library_page.writable():
            return
        lib = self.library_page.owner.lib
        shelf.set_status(lib, self.work.id, status)
        self._after_shelf_write(self.work.id)

    def set_category(self, category):
        if not self.work or not self.library_page.writable():
            return
        lib = self.library_page.owner.lib
        shelf.set_category(lib, self.work.id, category)
        self._after_shelf_write(self.work.id)

    def _after_shelf_write(self, wid):
        page = self.library_page
        page.shelves = shelf.list_shelf(page.owner.lib)
        page._refresh_sidebar()
        page.view.viewport().update()
        self._sync_shelf_controls()

    def _sync_shelf_controls(self):
        work = self.work
        if work is None:
            return
        st = self.library_page.status_of(work.id)
        for name, b in self.status_buttons.items():
            b.setChecked(name == st)
        cat = self.library_page.category_of(work.id)
        idx = 0 if cat is None else max(0, self.category_combo.findText(cat))
        blocked = self.category_combo.blockSignals(True)
        self.category_combo.setCurrentIndex(idx)
        self.category_combo.blockSignals(blocked)

    def apply_theme(self):
        t = self.owner.theme
        self.empty_label.setStyleSheet(f"color:{t.roles['sub']}; background:transparent")
        self.title.setStyleSheet(f"color:{t.roles['ink']}; background:transparent")
        self.subtitle.setStyleSheet(f"color:{t.roles['sub']}; background:transparent")
        self.summary.setStyleSheet(f"color:{t.roles['sub']}; background:transparent")
        self.notes.setStyleSheet(f"color:{t.roles['faint']}; background:transparent")
        self.path.setStyleSheet(f"color:{t.roles['faint']}; background:transparent")
        self.copy_folders.setStyleSheet(f"color:{t.roles['sub']}; background:transparent")
        self.location.setStyleSheet(f"color:{t.roles['sub']}; background:transparent")
        for value, label in self.stat_values.values():
            value.setStyleSheet(f"color:{t.roles['ink']}; background:transparent")
            label.setStyleSheet(f"color:{t.roles['sub']}; background:transparent")
        self.find_row.apply_theme()
        self.links_label.setStyleSheet(f"color:{t.roles['sub']}; background:transparent")
        self.update_facts()

    def current_copy(self):
        return self.work.copies[self.copy_index] if self.work and self.work.copies else None

    def set_work(self, work, aggregate="unknown"):
        same = self.work and work and self.work.id == work.id
        self.work, self.aggregate = work, aggregate
        if not same or not work or self.copy_index >= len(work.copies):
            self.copy_index = 0
        self.open_button.setEnabled(bool(work and work.copies))
        self.content.setVisible(bool(work))
        self.empty_label.setVisible(not work)
        writable = self.library_page.writable()
        self.find_button.setEnabled(writable)
        self.edit_button.setEnabled(writable)
        self.category_combo.setEnabled(writable)
        for b in self.status_buttons.values():
            b.setEnabled(writable)
        self._rebuild_more_menu()
        self._sync_shelf_controls()
        self.hero.update()
        self.update_facts()
        if work is not None:
            # Reached both from a card click and from keyboard selection while
            # the detail page is current.  The page throttles per work and
            # ignores startup/list refreshes where the detail page is hidden.
            self.library_page.request_work_check(work.id)

    def _rebuild_more_menu(self):
        self.more_menu.clear()
        work = self.work
        if not work:
            return
        cp = self.current_copy()
        self.more_menu.addAction("编辑…", self.edit)
        self.more_menu.addAction("启动设置…", lambda: self.owner.launch_settings(work.id, cp.id if cp else None))
        self.more_menu.addAction("找资料…", lambda: self.owner.open_find(work.id))
        if len(work.copies) > 1:
            self.more_menu.addAction(f"切换副本（{self.copy_index + 1}/{len(work.copies)}）", self.next_copy)
        self.more_menu.addAction("取消隐藏" if work.hidden else "隐藏",
                                 lambda: self.owner.hide_works([work.id], not work.hidden))
        self.more_menu.addAction("检查文件", lambda: self.owner.check_files([work.id]))
        allowed = bool(self.owner.lib) and not self.owner.lib.readonly \
            and not self.owner.screenshot_mode and self.owner.tracker is None
        locations = [self.library_page.copy_storage.get(cp.id) for cp in work.copies]
        locations = [item.location for item in locations if item is not None]
        cold_action = self.more_menu.addAction("冷藏到机械盘…", lambda: self.owner.cold_works([work.id]))
        cold_action.setEnabled(allowed and "常用" in locations)
        restore_action = self.more_menu.addAction("搬回常用盘…", lambda: self.owner.restore_works([work.id]))
        restore_action.setEnabled(allowed and "冷藏" in locations)
        move_action = self.more_menu.addAction("移到其他游戏目录…",
                                               lambda: self.owner.move_works([work.id]))
        move_action.setEnabled(allowed and "常用" in locations)
        delete_action = self.more_menu.addAction("删除游戏文件…", lambda: self.owner.delete_work_files([work.id]))
        delete_action.setEnabled(allowed and any(location != "已删除" for location in locations))
        self.more_menu.addSeparator()
        self.more_menu.addAction("从书架移除…", lambda: self.owner.remove_work(work.id))

    def _show_more(self):
        self._rebuild_more_menu()
        self.more_menu.exec(self.more_button.mapToGlobal(QPointF(0, self.more_button.height()).toPoint()))

    def start_game(self):
        if self.work:
            cp = self.current_copy()
            self.owner.start_game(self.work.id, cp.id if cp else None)

    def open_saves(self):
        if self.work and self.saves_button.isEnabled():
            from .savedialog import SaveDialog
            dialog = SaveDialog(self.owner, self.work.id, self.current_copy())
            dialog.exec()

    def update_start(self):
        cp = self.current_copy()
        reason = ("没有可用的游戏文件夹" if cp is None else
                  "库是只读的，不能应用存档" if not self.owner.lib or self.owner.lib.readonly else
                  "压缩包要先解压" if cp.kind != "folder" else
                  "游戏文件夹已失效" if cp.availability == "missing" else "")
        self.saves_button.setEnabled(not reason)
        self.saves_button.setToolTip(reason)
        playing = self.owner.tracker is not None
        # Re-assert the 46px height: showing the stacked page re-polishes the
        # QPushButton stylesheet, which can otherwise restore Button's 36px min.
        for b in (self.start_button, self.open_button, self.saves_button, self.find_button, self.edit_button, self.more_button):
            b.setMinimumHeight(46)
            b.setFixedHeight(46)
        self.start_button.setText("游戏中" if playing else "▶ 开始游戏")
        archive = bool(cp and cp.kind == "archive")
        deleted = bool(self.work and self.library_page.location_of(self.work.id) == "已删除")
        self.start_button.setEnabled(bool(self.work and self.owner.lib and not self.owner.lib.readonly
                                          and not archive and not playing and not deleted))
        self.start_button.setToolTip("压缩包不能直接启动" if archive else "")
        self.start_button.update()

    def availability_text(self):
        if self.work and self.work.copies and all(c.availability == "error" for c in self.work.copies):
            return "检查出错"
        return STATUS_TEXT[self.aggregate]

    def byline_text(self):
        return self.library_page.byline(self.work) if self.work else ''

    @staticmethod
    def short_date(raw):
        if not raw or raw == "—":
            return "—"
        try:
            d = date.fromisoformat(raw[:10])
        except (TypeError, ValueError):
            return raw[:10] or "—"
        return d.strftime("%m-%d") if d.year == date.today().year else d.isoformat()

    def _meta_line(self):
        info = self.library_page.infos.get(self.work.id)
        links = info.links if info else {}
        c = self.owner.theme.roles
        color = c["ai"] if self.owner.theme.name == "day" else c["beni"]
        parts = [f'<a style="color:{color};text-decoration:none" href="site:{site}">'
                 f'{html_mod.escape(records.SITE_LABEL[site])} {html_mod.escape(links[site])}</a>'
                 for site in records.SITES if site in links]
        return " · ".join(parts) + (f' · <a style="color:{color}" href="find:">换一个</a>' if parts else "")

    def update_facts(self):
        self.update_start()
        work = self.work
        if not work:
            self.facts.setText("选一部作品")
            self.facts.set_tags(())
            self.find_row.hide()
            self.links_label.hide()
            for value, _ in self.stat_values.values():
                value.setText("—")
            for widget in (self.summary, self.notes, self.path, self.copy_folders):
                widget.hide()
            self.location.hide()
            self.title.set_full_text("")
            self.subtitle.set_full_text("")
            return
        page = self.library_page
        t = self.owner.theme
        info = page.infos.get(work.id)
        links = info.links if info else {}
        unbound = not links and not page.links.get(work.id)
        self.find_row.hide()  # kept for compatibility; find_button replaces it
        self.links_label.show()
        self.links_label.setText(self._meta_line())
        self.links_label.setStyleSheet(f"color:{t.roles['sub']}; background:transparent")
        lines = []
        brand = self.byline_text()
        release = info.release_date if info else None
        if brand == "社团未整理" and not release:
            subtitle = "—"
        elif release:
            subtitle = f"{brand} · {release}"
        else:
            subtitle = brand
        self.title.set_full_text(work.title)
        self.subtitle.set_full_text(subtitle)
        lines.append("副标题：" + subtitle)
        if release:
            lines.append("发售：" + release)
        key = meta.name_key(meta.clean_developer(work.developer))
        if work.developer and not page.metas.get(work.id, meta.WorkMeta(work.id, (), ())).brand_ids \
                and key not in page.decided_keys:
            lines.append(f"旧社团：{work.developer}（还没整理成品牌）")
        if unbound:
            lines.append("未绑定 · 还没有封面和资料 · 找资料")
        else:
            bound = " · ".join(f"{records.SITE_LABEL[site]} {links[site]}" for site in records.SITES if site in links)
            lines.append((bound + " · " if bound else "已绑定 · ") + "找资料")
        pt = page.playtimes.get(work.id)
        played = play.format_duration(pt.total_seconds) if pt and pt.total_seconds else "—"
        recent = self.short_date(local_played_date(pt) if pt else "—")
        added = self.short_date(work.added_at)
        for key_name, text in (("played", played), ("recent", recent), ("added", added)):
            self.stat_values[key_name][0].setText(text)
        lines += [f"玩过：{played}", f"最近：{recent}", f"加入：{added}"]
        wm = page.metas.get(work.id, meta.WorkMeta(work.id, (), ()))
        tag_names = [page.tags[i].name for i in wm.tag_ids if i in page.tags]
        self.facts.set_tags(wm.tag_ids)
        lines.append("标签：" + ("、".join(tag_names) if tag_names else "还没有标签"))
        summary = info.summary if info else ""
        self.summary.setVisible(bool(summary))
        self.summary.set_full_text(summary or "")
        if summary:
            lines.append("简介：" + summary)
        note = work.notes or ""
        self.notes.setVisible(bool(note))
        self.notes.set_full_text("备注：" + note if note else "")
        if note:
            lines.append("备注：" + note)
        cp = self.current_copy()
        path = cp.path if cp else "没有登记副本"
        fm = QFontMetrics(font("mono", 11))
        missing = cp is not None and cp.availability == "missing"
        prefix = "文件夹找不到了 · " if missing else ""
        available = self.width() - 48 - fm.horizontalAdvance(prefix)
        shown_path = fm.elidedText(path, Qt.TextElideMode.ElideMiddle, max(0, available))
        self.path.show()
        self.path.setText(f'<span style="color:{t.roles["beni"]}">{html_mod.escape(prefix)}</span>'
                          f'{html_mod.escape(shown_path)}')
        checked = f"\n检查：{cp.checked_at or '未检查'} · {STATUS_TEXT[cp.availability]}" if cp else ""
        self.path.setToolTip(path + checked)
        folder_lines = []
        for i, copy in enumerate(work.copies, 1):
            name = self.library_page.copy_folder_name(copy)
            stored = self.library_page.copy_storage.get(copy.id)
            state = stored.location if stored is not None else "常用"
            suffix = "" if state == "常用" else f"（{state}）"
            line = html_mod.escape(f"副本 {i}：{name}{suffix}")
            if copy.availability == "missing":
                line = (f'<span style="color:{t.roles["faint"]}">{line}'
                        f' · 文件已不在（程序外删除或移走）</span>')
            folder_lines.append(line)
        self.copy_folders.setVisible(bool(folder_lines))
        self.copy_folders.setText("<br>".join(folder_lines))
        location = self.library_page.location_of(work.id)
        if location == "冷藏":
            self.location.setText(f"冷藏在 {path}")
            self.location.show()
        elif location == "已删除":
            self.location.setText("游戏文件已删除")
            self.location.show()
        else:
            self.location.hide()
        lines.append(prefix + path)
        self.facts.setText("\n".join(lines))
        self.hero.update()

    def activate_link(self, href):
        try:
            kind, item_id = href.split(':', 1)
            if kind == 'find' and self.work:
                self.owner.open_find(self.work.id)
            elif kind == 'site' and self.work:
                sid = self.library_page.links.get(self.work.id, {}).get(item_id)
                if sid:
                    self.owner.open_external(records.site_url(item_id, sid))
            if kind in ('brand', 'tag'):
                self.library_page.set_meta_state(kind, int(item_id), 'in')
        except (ValueError, TypeError):
            return

    def next_copy(self):
        if self.work and self.work.copies:
            self.copy_index = (self.copy_index + 1) % len(self.work.copies)
            self._rebuild_more_menu()
            self.update_facts()

    def open_folder(self):
        if self.current_copy():
            self.owner.open_copy_folder(self.current_copy())

    def edit(self, focus_notes=False):
        if self.work and not self.owner.busy and not self.owner.screenshot_mode:
            self.owner.edit_work(self.work.id, focus_notes)


class LibraryPage(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.works, self.details = [], {}
        self.playtimes = {}
        self.titles, self.links, self.infos = {}, {}, {}
        self.metas, self.brands, self.tags = {}, {}, {}
        self.groups, self.decided_keys, self.search_texts = [], set(), {}
        self.meta_filter = meta.MetaFilter()
        self._meta_order = {k: [] for k in ('brands_in', 'brands_out', 'tags_in', 'tags_out')}
        self.cache = CoverCache()
        self.filter_name = "全部"
        self.segment_counts = {name: 0 for name in SEGMENTS}
        self.shelves = {}
        self.category_name = "全部"
        self.status_name = "全部"
        self.location_name = "全部"
        self.copy_storage = {}
        self.locations = {}
        self.sources = {}
        self.folder_buttons = {}
        self._file_check_at = {}
        self.model = WorkModel(self)
        self.counts = {n: 0 for n in FILTERS}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # Page 0 holds the wall (sidebar + toolbar + shelf); page 1 is the
        # full-page detail.  Only one of them is visible at a time.
        self.pages = QStackedWidget()
        self.pages.setObjectName("libraryPages")
        wall_page = QWidget()
        wall_lay = QHBoxLayout(wall_page)
        wall_lay.setContentsMargins(0, 0, 0, 0)
        wall_lay.setSpacing(0)
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        # 52px filter line: segment switch, ghost filter, batch link, sort.
        filter_bar = Paper(owner)
        filter_bar.setObjectName("filterBar")
        filter_bar.setFixedHeight(52)
        bar = QHBoxLayout(filter_bar)
        bar.setContentsMargins(24, 8, 24, 8)
        bar.setSpacing(6)
        self.segment_bar = SegmentBar(owner, self)
        self.heading = QLabel("")
        self.heading.hide()  # legacy probe target: the COLLECTION title is gone
        self.tidy_link = GhostButton(owner, "整理", owner.show_tidy)
        self.tidy_link.setAccessibleName("整理")
        self.tidy_link.setObjectName("tidyLink")
        bar.addWidget(self.segment_bar)
        self.filter_button = GhostButton(owner, "筛选", self._toggle_filter_row, name="filterButton", arrow=True)
        bar.addWidget(self.filter_button)
        self.batch_link = GhostButton(owner, "全部找资料", self._start_batch, name="batchFindLink")
        self.batch_link.hide()
        bar.addWidget(self.batch_link)
        bar.addStretch(1)
        self.order = QComboBox()
        self.order.setObjectName("orderCombo")
        self.order.addItems(["加入顺序", "标题", "最近游玩"])
        self.order.setFixedHeight(30)
        self.order.currentIndexChanged.connect(self._order_changed)
        self.order.setStyleSheet(f"QComboBox {{ background:transparent; color:{owner.theme.roles['sub']}; "
                                 f"border:0; padding:0 22px 0 8px; font-size:12.5px; }} "
                                 f"QComboBox:disabled {{ color:{owner.theme.roles['faint']}; }}")
        bar.addWidget(self.order)
        # Sort direction: newest first for 加入顺序/最近游玩, A→Z for 标题; click to flip.
        self.order_desc = True
        self.order_dir = QPushButton()
        self.order_dir.setObjectName("orderDir")
        self.order_dir.setFixedSize(30, 30)
        self.order_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.order_dir.clicked.connect(self._flip_order)
        self.order_dir.setStyleSheet(f"QPushButton {{ background:transparent; color:{owner.theme.roles['sub']}; "
                                     f"border:0; font-size:15px; }} "
                                     f"QPushButton:disabled {{ color:{owner.theme.roles['faint']}; }}")
        self._sync_order_dir()
        bar.addWidget(self.order_dir)
        ml.addWidget(filter_bar)
        # Extra filter row, hidden by default; it stays a descendant of the page.
        self.filter_row = Paper(owner)
        self.filter_row.setObjectName("filterRow")
        self.filter_row.setFixedHeight(46)
        row = QHBoxLayout(self.filter_row)
        row.setContentsMargins(24, 4, 24, 4)
        row.setSpacing(8)
        self.brand_button = MetaButton(owner, lambda: self.brand_popover.show_for_button())
        self.tag_button = MetaButton(owner, lambda: self.tag_popover.show_for_button())
        for label, button in (('品牌', self.brand_button), ('标签', self.tag_button)):
            word = QLabel(label)
            word.setFont(font('body', 11))
            word.setStyleSheet(f"color:{owner.theme.roles['sub']}; background:transparent")
            row.addWidget(word)
            row.addWidget(button)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color:{owner.theme.roles['line']}")
        row.addWidget(sep)
        self.state_buttons = {}
        for label, name in STATE_CHIPS:
            b = GhostButton(owner, label, lambda checked=False, n=name: self._toggle_state(n))
            self.state_buttons[name] = b
            row.addWidget(b)
        row.addStretch(1)
        self.filter_row.hide()
        ml.addWidget(self.filter_row)
        self.cond_bar = ConditionBar(owner)
        self.cond_bar.setFixedHeight(34)
        self.cond_layout = FlowLayout(self.cond_bar)
        self.cond_layout.setContentsMargins(12, 5, 8, 5)
        ml.addWidget(self.cond_bar)
        shelf = Paper(owner)
        self.shelf = shelf
        sl = QVBoxLayout(shelf)
        sl.setContentsMargins(24, 6, 24, 24)
        sl.setSpacing(0)
        self.view = ShelfView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(CoverDelegate(self))
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setGridSize(QSize(172, 292))
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.context_menu)
        self.view.selectionModel().selectionChanged.connect(self.selection_changed)
        sl.addWidget(self.view, 1)
        self.empty_state = EmptyState(owner, self)
        self.empty = self.empty_state.label  # legacy text probe; the new label is a QLabel too
        self.empty_image = QLabel()
        self.empty_image.hide()
        sl.addWidget(self.empty_image)
        sl.addWidget(self.empty_state, 1, Qt.AlignmentFlag.AlignCenter)
        self.selection_bar = SelectionBar(owner)
        self.selection_bar.buttons["找资料"].clicked.connect(self._selection_find)
        self.selection_bar.buttons["隐藏"].clicked.connect(self.hide_selected)
        self.selection_bar.buttons["检查文件"].clicked.connect(self.check_selected)
        self.selection_bar.buttons["取消"].clicked.connect(self.view.clearSelection)
        self.stack = QHBoxLayout()
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.addStretch(1)
        self.stack.addWidget(self.selection_bar)
        self.stack.addStretch(1)
        sl.addLayout(self.stack)
        ml.addWidget(shelf, 1)
        self.detail = Detail(owner, self)
        self._build_sidebar()
        wall_lay.addWidget(self.sidebar)
        wall_lay.addWidget(main, 1)
        self.pages.addWidget(wall_page)
        self.pages.addWidget(self.detail)
        lay.addWidget(self.pages, 1)
        self.search = QLineEdit()
        self.search.textChanged.connect(self._search_changed)
        self.search.textChanged.connect(self.apply_filter)
        self.search.returnPressed.connect(self._focus_view)
        self.shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self.shortcut.activated.connect(self.search.setFocus)
        self.search_esc_shortcut = QShortcut(QKeySequence("Escape"), self.search)
        self.search_esc_shortcut.activated.connect(self._clear_search)
        self.brand_popover = MetaPopover(self, 'brand')
        self.tag_popover = MetaPopover(self, 'tag')
        self._update_filter_controls()
        QTimer.singleShot(0, self.update_grid)

    # -------------------------------------------------------------- pages
    def detail_open(self):
        return self.pages.currentIndex() == 1

    def open_detail(self, wid=None):
        if wid is None:
            current = self.view.currentIndex()
            w = current.data(Qt.ItemDataRole.UserRole) if current.isValid() else None
            if w is None:
                return
            wid = w.id
        if wid in self.details:
            summary = next((w for w in self.works if w.id == wid), None)
            aggregate = summary.availability if summary is not None else "unknown"
            # Make the detail page current first: set_work uses detail_open() to
            # decide whether this is a real detail visit (not a startup refresh).
            self.pages.setCurrentIndex(1)
            self.detail.set_work(self.details[wid], aggregate)
            self.detail.update_start()  # re-assert 46px height after the page re-polish

    def close_detail(self):
        self.pages.setCurrentIndex(0)

    def request_work_check(self, work_id):
        """Queue a quiet availability check for the work now shown in detail.

        Returns True when a background check was actually started.  Busy and
        read-only libraries are skipped, and the same work is not checked more
        than once per WORK_CHECK_INTERVAL seconds.
        """
        if not self.detail_open() or not self.writable():
            return False
        owner = self.owner
        if owner.busy or owner.screenshot_mode or getattr(owner, "closing", False):
            return False
        now = time.monotonic()
        if now - self._file_check_at.get(work_id, -float("inf")) < WORK_CHECK_INTERVAL:
            return False
        started = owner.check_work_files(
            work_id, lambda states, wid=work_id: self.apply_work_check(wid, states))
        if started:
            self._file_check_at[work_id] = now
        return bool(started)

    def apply_work_check(self, work_id, states):
        """Fold a background result into one card and the open detail page."""
        detail = self.details.get(work_id)
        if detail is None:
            return
        copies = tuple(replace(copy, availability=states[copy.id]) if copy.id in states else copy
                       for copy in detail.copies)
        detail = replace(detail, copies=copies)
        self.details[work_id] = detail
        aggregate = repo.availability_of(copy.availability for copy in copies)
        summary = next((w for w in self.works if w.id == work_id), None)
        if summary is not None:
            summary = replace(summary, availability=aggregate)
            self.works = [summary if w.id == work_id else w for w in self.works]
            for row, w in enumerate(self.model.works):
                if w.id == work_id:
                    self.model.works[row] = summary
                    index = self.model.index(row, 0)
                    self.model.dataChanged.emit(index, index, [])
                    break
            self.counts = filter_counts(self.works, self.metas)
            self.segment_counts = segment_counts(self.works, self.links, self.playtimes)
            self._refresh_sidebar()
        if self.detail.work is not None and self.detail.work.id == work_id:
            self.detail.set_work(detail, aggregate)
        self.view.viewport().update()

    def keyPressEvent(self, event):
        if self.detail_open() and event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self.close_detail()
            event.accept()
            return
        super().keyPressEvent(event)

    # -------------------------------------------------------------- sidebar
    def _build_sidebar(self):
        self.sidebar = SidebarWidget(self.owner)
        self.sidebar.setObjectName("wallSidebar")
        self.sidebar.setFixedWidth(200)
        lay = QVBoxLayout(self.sidebar)
        lay.setContentsMargins(12, 14, 12, 14)
        lay.setSpacing(2)

        def section(text):
            label = QLabel(text)
            label.setFont(font("body", 11))
            label.setStyleSheet(f"color:{self.owner.theme.roles['faint']}; background:transparent")
            lay.addWidget(label)
            lay.addSpacing(4)

        section("分类")
        self.category_buttons = {}
        for key in ("全部", *shelf.CATEGORIES, "未分类"):
            b = SideButton(self.owner, key)
            b.clicked.connect(lambda checked=False, n=key: self.set_category_filter(n))
            self.category_buttons[key] = b
            lay.addWidget(b)
        lay.addSpacing(14)
        section("状态")
        self.status_buttons = {}
        dots = {"想玩": "status_want", "在玩": "status_playing", "玩过": "status_done",
                "不玩了": "status_dropped", "未标记": "line"}
        for key in (*shelf.STATUSES, "未标记"):
            b = SideButton(self.owner, key, dot_role=dots[key])
            b.clicked.connect(lambda checked=False, n=key: self._status_button_clicked(n))
            self.status_buttons[key] = b
            lay.addWidget(b)
        lay.addSpacing(14)
        section("位置")
        self.location_buttons = {}
        for key in ("全部", *storage.LOCATIONS):
            b = SideButton(self.owner, key)
            b.clicked.connect(lambda checked=False, n=key: self.set_location_filter(n))
            self.location_buttons[key] = b
            lay.addWidget(b)
        self.folder_box = QWidget()
        self.folder_layout = QVBoxLayout(self.folder_box)
        self.folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_layout.setSpacing(2)
        lay.addWidget(self.folder_box)
        lay.addStretch(1)

    def _status_button_clicked(self, name):
        self.set_status_filter("全部" if self.status_name == name else name)

    def category_of(self, work_id):
        row = self.shelves.get(work_id)
        if row is not None and row.category:
            return row.category
        detail = self.details.get(work_id)
        if detail is not None:
            for cp in detail.copies:
                guess = shelf.guess_category(cp.path)
                if guess:
                    return guess
        return None

    def status_of(self, work_id):
        row = self.shelves.get(work_id)
        return row.status if row is not None else None

    def _refresh_sidebar(self):
        visible = [w for w in self.works if not w.hidden]
        cats = [self.category_of(w.id) for w in visible]
        stats = [self.status_of(w.id) for w in visible]
        locs = [self.location_of(w.id) for w in visible]
        self._sync_folder_buttons()
        folder_counts = {sid: 0 for sid in self.sources}
        for work in visible:
            for sid in self.work_folders(work.id):
                if sid in folder_counts:
                    folder_counts[sid] += 1
        self.category_buttons["全部"].set_count(len(visible))
        for key in shelf.CATEGORIES:
            self.category_buttons[key].set_count(cats.count(key))
        uncategorized = sum(c is None for c in cats)
        self.category_buttons["未分类"].set_count(uncategorized)
        self.category_buttons["未分类"].setVisible(uncategorized > 0)
        for key in shelf.STATUSES:
            self.status_buttons[key].set_count(stats.count(key))
        self.status_buttons["未标记"].set_count(sum(s is None for s in stats))
        self.location_buttons["全部"].set_count(sum(location != "已删除" for location in locs))
        for key in storage.LOCATIONS:
            self.location_buttons[key].set_count(locs.count(key))
        for sid, b in self.folder_buttons.items():
            b.set_count(folder_counts.get(sid, 0))
        for key, b in self.category_buttons.items():
            b.setChecked(key == self.category_name)
        for key, b in self.status_buttons.items():
            b.setChecked(key == self.status_name)
        for key, b in self.location_buttons.items():
            b.setChecked(key == self.location_name)

    def set_category_filter(self, name):
        self.category_name = name
        self._refresh_sidebar()
        self.apply_filter()

    def set_status_filter(self, name):
        self.status_name = name
        self._refresh_sidebar()
        self.apply_filter()

    def _sync_folder_buttons(self):
        """Rebuild the per-game-folder entries under 位置."""
        if not hasattr(self, "folder_layout"):
            return
        clear_layout(self.folder_layout)
        self.folder_buttons = {}
        if self.location_name.startswith("folder:"):
            try:
                selected = int(self.location_name.split(":", 1)[1])
            except ValueError:
                selected = None
            if selected not in self.sources:
                self.location_name = "全部"
        for source in sorted(self.sources.values(), key=lambda item: item.id):
            label = (source.label or "").strip() or source_path_tail(source.path, 2)
            if not label:
                label = source_short_name(source)
            button = SideButton(self.owner, label)
            key = folder_key(source.id)
            button.clicked.connect(lambda checked=False, n=key: self.set_location_filter(n))
            self.folder_layout.addWidget(button)
            self.folder_buttons[source.id] = button
        self.folder_box.setVisible(bool(self.folder_buttons))

    def _copy_folder_ids(self, copy) -> set[int]:
        stored = self.copy_storage.get(copy.id)
        if stored is not None and stored.location != "常用":
            return set()
        ids = set()
        if copy.source_id in self.sources:
            ids.add(copy.source_id)
        try:
            key = storage._key(copy.path)
        except Exception:  # noqa: BLE001 - a malformed legacy path just has no folder
            return ids
        for source in self.sources.values():
            try:
                root = storage._key(source.path)
            except Exception:  # noqa: BLE001
                continue
            if key == root or storage._is_parent(root, key):
                ids.add(source.id)
        return ids

    def work_folders(self, work_id) -> set[int]:
        detail = self.details.get(work_id)
        if detail is None:
            return set()
        out = set()
        for copy in detail.copies:
            out.update(self._copy_folder_ids(copy))
        return out

    def folder_badge(self, work_id) -> str | None:
        detail = self.details.get(work_id)
        if detail is None:
            return None
        return folder_badge_text(detail.copies, self.sources, self.copy_storage)

    def copy_folder_name(self, copy) -> str:
        source = self.sources.get(copy.source_id)
        if source is not None:
            return source_display_name(source)
        letter = _drive_letter(copy.path)
        return letter or str(copy.path)

    def set_location_filter(self, name):
        if name not in ("全部", *storage.LOCATIONS):
            if not name.startswith("folder:"):
                raise ValueError(name)
            try:
                source_id = int(name.split(":", 1)[1])
            except ValueError as error:
                raise ValueError(name) from error
            if source_id not in self.sources:
                raise ValueError(name)
        self.location_name = name
        self._refresh_sidebar()
        self.apply_filter()

    def location_of(self, work_id):
        return self.locations.get(work_id, "常用")

    # -------------------------------------------------------------- helpers
    def _focus_view(self):
        if self.model.works:
            self.view.setFocus()
            if not self.view.currentIndex().isValid():
                self.view.setCurrentIndex(self.model.index(0, 0))
                self.detail.set_work(self.details[self.model.works[0].id], self.model.works[0].availability)

    def _clear_search(self):
        if self.detail_open():
            self.close_detail()
            return
        if self.search.text():
            self.search.clear()
        self.view.setFocus()

    def _search_changed(self, _text):
        if self.detail_open():
            self.close_detail()

    def writable(self):
        return bool(self.owner.lib and not self.owner.lib.readonly)

    def _toggle_filter_row(self):
        self.filter_row.setVisible(not self.filter_row.isVisible())
        self._update_filter_controls()

    def _toggle_state(self, name):
        self.set_filter("全部" if self.filter_name == name else name)

    def _selection_find(self):
        self.owner.start_batch(self.selected_ids())

    def _start_batch(self):
        if self.owner.lib and self.owner.lib.readonly:
            self.owner.notify('只读打开的库不能批量绑定')
            return
        self.owner.start_batch(None)

    def update_grid(self):
        delegate = self.view.itemDelegate()
        # IconMode can reserve scrollbar width even before the scrollbar appears.
        scrollbar = self.view.verticalScrollBar().sizeHint().width()
        available = min(self.view.viewport().width(), self.view.maximumViewportSize().width() - scrollbar) - 4
        if available < delegate.MIN_W:
            return
        columns = max(2, available // (delegate.MIN_W + delegate.GAP_X))
        card_w = available // columns - delegate.GAP_X
        while card_w > delegate.MAX_W:
            columns += 1
            card_w = available // columns - delegate.GAP_X
        delegate.card_width = max(delegate.MIN_W, card_w)
        height = delegate.card_height()
        self.view.setGridSize(QSize(delegate.card_width + delegate.GAP_X,
                                    height + delegate.GAP_Y))

    def condition_count(self):
        n = sum(len(v) for v in self._meta_order.values())
        if self.filter_name in FILTERS and self.filter_name != "全部":
            n += 1
        return n

    def _update_filter_controls(self):
        for name, b in self.segment_bar.buttons.items():
            display = "缺资料" if name == "未绑定" else name
            b.set_label(f"{display} {self.segment_counts.get(name, 0)}")
            b.setChecked(name == self.filter_name)
            b.update()
        n = self.condition_count()
        self.filter_button.setText("筛选" if n == 0 else f"筛选 · {n}")
        self.batch_link.setVisible(self.filter_name == "未绑定" and self.writable())
        self.batch_link.setEnabled(self.writable())
        self.order.setEnabled(self.filter_name != "最近玩")
        self.order_dir.setEnabled(self.filter_name != "最近玩")
        for name, b in self.state_buttons.items():
            b.highlight = self.filter_name == name
            b.update()
        self.order.setStyleSheet(f"QComboBox {{ background:transparent; color:{self.owner.theme.roles['sub']}; "
                                 f"border:0; padding:0 22px 0 8px; font-size:12.5px; }} "
                                 f"QComboBox:disabled {{ color:{self.owner.theme.roles['faint']}; }}")

    def _sync_meta_filter(self):
        self.meta_filter = meta.MetaFilter(**{k: frozenset(v) for k, v in self._meta_order.items()})

    def set_data(self, projection, select=None):
        old_brands = {i: (b.name, b.aliases) for i, b in self.brands.items()}
        old_tags = {i: (t.name, t.group_id) for i, t in self.tags.items()}
        old_groups = self.groups
        self.works, self.details, images, source_list = projection[:4]
        self.sources = {source.id: source for source in source_list}
        self.playtimes = projection[4] if len(projection) > 4 else {}
        self.metas = projection[5] if len(projection) > 5 else {}
        self.brands = {b.id: b for b in projection[6]} if len(projection) > 6 else {}
        self.tags = {t.id: t for t in projection[7]} if len(projection) > 7 else {}
        self.groups = projection[8] if len(projection) > 8 else []
        self.titles = projection[10] if len(projection) > 10 else {}
        self.links = projection[11] if len(projection) > 11 else {}
        self.infos = projection[12] if len(projection) > 12 else {}
        self.shelves = projection[13] if len(projection) > 13 else {}
        if self.owner.lib is not None:
            self.copy_storage = storage.list_storage(self.owner.lib)
            self.locations = storage.work_locations(self.owner.lib)
        else:
            self.copy_storage, self.locations = {}, {}
        self.decided_keys = {t.text_key for t in projection[9] if t.decided is not None} if len(projection) > 9 else set()
        for key in self._meta_order:
            valid = (set(self.brands) | {meta.NO_BRAND}) if key.startswith('brands') else set(self.tags)
            self._meta_order[key] = [i for i in self._meta_order[key] if i in valid]
        self._sync_meta_filter()
        self.search_texts = {}
        for w in self.works:
            wm = self.metas.get(w.id, meta.WorkMeta(w.id, (), ()))
            names = [n for i in wm.brand_ids if i in self.brands for n in (self.brands[i].name, *self.brands[i].aliases)]
            names += [self.tags[i].name for i in wm.tag_ids if i in self.tags]
            self.search_texts[w.id] = '\n'.join((w.title, w.developer, self.details[w.id].notes,
                                                 *names, *self.titles.get(w.id, ()))).casefold()
        self.cache.load(self.works, images)
        self.counts = filter_counts(self.works, self.metas)
        self.segment_counts = segment_counts(self.works, self.links, self.playtimes)
        self._refresh_sidebar()
        if select in self.details and self.details[select].hidden:
            self.filter_name = "隐藏"
        self.apply_filter()
        if self.brand_popover.isVisible() and old_brands != {i: (b.name, b.aliases) for i, b in self.brands.items()}:
            self.brand_popover.rebuild()
        if self.tag_popover.isVisible() and (old_tags != {i: (t.name, t.group_id) for i, t in self.tags.items()} or old_groups != self.groups):
            self.tag_popover.rebuild()
        self.select_work(select)

    def select_work(self, wid=None):
        for i, w in enumerate(self.model.works):
            if wid is None or w.id == wid:
                self.view.setCurrentIndex(self.model.index(i, 0))
                self.view.scrollTo(self.model.index(i, 0))
                self.detail.set_work(self.details[w.id], w.availability)
                return
        if wid is not None and wid in self.details:
            # A deleted work is no longer on the wall, but an open detail page
            # must stay where it is and refresh to the "已删除" state.
            summary = next((w for w in self.works if w.id == wid), None)
            self.detail.set_work(self.details[wid], summary.availability if summary is not None else "unknown")
            return
        self.detail.set_work(None)

    def set_filter(self, name):
        if name not in FILTERS and name not in SEGMENTS:
            raise ValueError(name)
        self.filter_name = name
        self.apply_filter()
        self.select_work()

    def apply_filter(self, *args):
        query = self.search.text().casefold()
        links = self.links
        playtimes = self.playtimes
        if self.filter_name == "未绑定":
            base_filter = lambda w: not w.hidden and not links.get(w.id)
        elif self.filter_name == "最近玩":
            base_filter = lambda w: not w.hidden and _played_recently(playtimes.get(w.id))
        else:
            base_filter = lambda w: matches_filter(w, self.filter_name, self.metas, self.links)
        def shelf_ok(w):
            cat = self.category_of(w.id)
            st = self.status_of(w.id)
            if self.category_name == "未分类":
                if cat is not None:
                    return False
            elif self.category_name != "全部" and cat != self.category_name:
                return False
            if self.status_name == "未标记":
                if st is not None:
                    return False
            elif self.status_name != "全部" and st != self.status_name:
                return False
            return True
        def location_ok(w):
            location = self.location_of(w.id)
            if self.location_name == "全部":
                return location != "已删除"
            if self.location_name.startswith("folder:"):
                return int(self.location_name.split(":", 1)[1]) in self.work_folders(w.id)
            return location == self.location_name

        base = [w for w in self.works if base_filter(w) and shelf_ok(w) and location_ok(w) and
                (not query or query in self.search_texts.get(w.id, ''))]
        bc, tc = meta.facet_counts((self.metas.get(w.id, meta.WorkMeta(w.id, (), ())) for w in base), self.meta_filter)
        self.brand_popover.refresh(bc)
        self.tag_popover.refresh(tc)
        self.update_conditions()
        works = [w for w in base if meta.matches(self.metas.get(w.id, meta.WorkMeta(w.id, (), ())), self.meta_filter)]
        if self.filter_name == "最近玩":
            works.sort(key=lambda w: (_last_played_instant(playtimes[w.id]), w.id), reverse=True)
        elif self.order.currentIndex() == 2 and self.filter_name != "隐藏":
            works.sort(key=lambda w: w.id, reverse=True)
            works.sort(key=lambda w: (local_played_date(playtimes[w.id]) if w.id in playtimes else "") or "",
                       reverse=True)
        else:
            works.sort(key=(lambda w: w.title.casefold()) if self.order.currentIndex() else (lambda w: (w.added_at, w.id)))
        if self.filter_name != "最近玩":
            if self.order.currentIndex() == 2 and self.filter_name != "隐藏":
                if not self.order_desc:
                    works.reverse()
            elif self.order_desc:
                works.reverse()
        self.model.reset(works)
        empty_library = not self.works
        self.empty_state.set_state(empty_library, not works and not empty_library)
        self.view.setVisible(bool(works))
        self._update_filter_controls()
        self.selection_bar.hide()
        self.detail.set_work(None)
        self.select_work()
        self.update_grid()

    def byline(self, work):
        if not work:
            return ''
        names = [self.brands[i].name for i in self.metas.get(work.id, meta.WorkMeta(work.id, (), ())).brand_ids
                 if i in self.brands]
        return '、'.join(names) or work.developer or '社团未整理'

    def meta_state(self, kind, item_id):
        prefix = 'brands' if kind == 'brand' else 'tags'
        return 'in' if item_id in self._meta_order[prefix + '_in'] else 'out' if item_id in self._meta_order[prefix + '_out'] else 'off'

    def set_meta_state(self, kind, item_id, state):
        if kind not in ('brand', 'tag') or state not in ('in', 'out', 'off'):
            raise ValueError('Unknown metadata state')
        valid = (set(self.brands) | {meta.NO_BRAND}) if kind == 'brand' else set(self.tags)
        if item_id not in valid:
            return
        if self.meta_state(kind, item_id) == state:
            return
        prefix = 'brands' if kind == 'brand' else 'tags'
        for suffix in ('in', 'out'):
            values = self._meta_order[prefix + '_' + suffix]
            if item_id in values:
                values.remove(item_id)
        if state != 'off':
            self._meta_order[prefix + '_' + state].append(item_id)
        self._sync_meta_filter()
        self.apply_filter()

    def cycle_meta(self, kind, item_id):
        self.set_meta_state(kind, item_id, {'off': 'in', 'in': 'out', 'out': 'off'}[self.meta_state(kind, item_id)])

    def clear_meta_filter(self):
        for key in self._meta_order:
            self._meta_order[key] = []
        self._sync_meta_filter()
        self.apply_filter()

    def _order_changed(self, index):
        self.order_desc = index != 1
        self._sync_order_dir()
        self.apply_filter()

    def _flip_order(self):
        self.order_desc = not self.order_desc
        self._sync_order_dir()
        self.apply_filter()

    def _sync_order_dir(self):
        title = self.order.currentIndex() == 1
        if title:
            text, tip = ("↑", "Z→A，点一下改成 A→Z") if self.order_desc else ("↓", "A→Z，点一下改成 Z→A")
        else:
            text, tip = ("↓", "新→旧，点一下改成旧→新") if self.order_desc else ("↑", "旧→新，点一下改成新→旧")
        self.order_dir.setText(text)
        self.order_dir.setToolTip(tip)
        self.order_dir.setAccessibleName("排序方向：" + tip.split("，")[0])

    def visible_work_ids(self):
        return [w.id for w in self.model.works]

    def meta_name(self, kind, item_id):
        if kind == 'brand' and item_id == meta.NO_BRAND:
            return '（无品牌）'
        return (self.brands if kind == 'brand' else self.tags)[item_id].name

    def cond_text(self):
        parts = []
        for kind, label, prefix, joiner in (('brand', '品牌', 'brands', ' 或 '), ('tag', '标签', 'tags', ' 且 ')):
            ins, outs = self._meta_order[prefix + '_in'], self._meta_order[prefix + '_out']
            if not (ins or outs):
                continue
            part = label + ' ' + joiner.join(self.meta_name(kind, i) for i in ins)
            if outs:
                part += (' 不含 ' if ins else '不含 ') + '、'.join(self.meta_name(kind, i) for i in outs)
            parts.append(part)
        return ' · '.join(parts)

    def update_conditions(self):
        clear_layout(self.cond_layout)
        for kind, label, prefix, button in (('brand', '品牌', 'brands', self.brand_button),
                                            ('tag', '标签', 'tags', self.tag_button)):
            ins, outs = self._meta_order[prefix + '_in'], self._meta_order[prefix + '_out']
            button.active = bool(ins or outs)
            button.setText(f'{len(ins) + len(outs)} 个' if ins or outs else '全部')
            button.update()
            for state, ids in (('in', ins), ('out', outs)):
                for item_id in ids:
                    name = ('不含 ' if state == 'out' else '') + self.meta_name(kind, item_id) + ' ✕'
                    self.cond_layout.addWidget(Pill(self.owner, name,
                        lambda checked=False, k=kind, i=item_id: self.set_meta_state(k, i, 'off')))
        if self.filter_name in FILTERS and self.filter_name != "全部":
            self.cond_layout.addWidget(Pill(self.owner, self.filter_name + " ✕",
                                           lambda checked=False: self.set_filter("全部")))
        if not self.meta_filter.is_empty():
            self.cond_layout.addWidget(Pill(self.owner, "清除 ✕", self.clear_meta_filter))
        self.cond_bar.setVisible(bool(self.cond_layout.count() or
                                      (self.filter_name in FILTERS and self.filter_name != "全部")))
        self.cond_bar.setToolTip(self.cond_text())
        self.cond_bar.adjustSize()

    def meta_context(self, kind, item, pos):
        from .metadialogs import metadata_menu
        metadata_menu(self.owner, kind, item, pos)

    def selected_ids(self):
        return [i.data(Qt.ItemDataRole.UserRole).id for i in self.view.selectionModel().selectedIndexes()]

    def selection_changed(self, *args):
        ids = self.selected_ids()
        self.selection_bar.setVisible(len(ids) > 1)
        self.selection_bar.label.setText(f"已选 {len(ids)} 部")
        self.selection_bar.buttons["找资料"].setEnabled(self.writable())
        self.selection_bar.apply_theme()
        if ids:
            current = self.view.currentIndex()
            w = current.data(Qt.ItemDataRole.UserRole) if current.isValid() else None
            if not w or w.id not in ids:
                w = self.view.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
            self.detail.set_work(self.details[w.id], w.availability)
        self.view.viewport().update()

    def _style_batch_link(self):
        """Kept for Window.apply_theme(); the link is now a ghost button."""
        self._update_filter_controls()

    def hide_selected(self):
        self.owner.hide_works(self.selected_ids())

    def check_selected(self):
        self.owner.check_files(self.selected_ids())

    def context_menu(self, pos):
        if self.owner.busy or self.owner.screenshot_mode:
            return
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        w = idx.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("编辑", lambda: self.owner.edit_work(w.id))
        detail = self.details[w.id]
        cp = self.detail.current_copy() if self.detail.work and self.detail.work.id == w.id else next(iter(detail.copies), None)
        opened = menu.addAction("打开目录", lambda: self.owner.open_copy_folder(cp))
        opened.setEnabled(cp is not None)
        launch = menu.addAction("开始游戏", lambda: self.owner.start_game(w.id, cp.id if cp else None))
        settings = menu.addAction("启动设置…", lambda: self.owner.launch_settings(w.id, cp.id if cp else None))
        launch.setEnabled(bool(cp and cp.kind == "folder") and self.owner.tracker is None)
        settings.setEnabled(bool(cp and cp.kind == "folder"))
        menu.addAction("取消隐藏" if w.hidden else "隐藏", lambda: self.owner.hide_works([w.id], not w.hidden))
        menu.addAction("检查文件", lambda: self.owner.check_files([w.id]))
        allowed = self.writable() and not self.owner.screenshot_mode and self.owner.tracker is None
        locations = [self.copy_storage.get(cp.id) for cp in detail.copies]
        locations = [item.location for item in locations if item is not None]
        cold_action = menu.addAction("冷藏到机械盘…", lambda: self.owner.cold_works([w.id]))
        cold_action.setEnabled(allowed and "常用" in locations)
        restore_action = menu.addAction("搬回常用盘…", lambda: self.owner.restore_works([w.id]))
        restore_action.setEnabled(allowed and "冷藏" in locations)
        move_action = menu.addAction("移到其他游戏目录…", lambda: self.owner.move_works([w.id]))
        move_action.setEnabled(allowed and "常用" in locations)
        delete_action = menu.addAction("删除游戏文件…", lambda: self.owner.delete_work_files([w.id]))
        delete_action.setEnabled(allowed and any(location != "已删除" for location in locations))
        ids = self.selected_ids()
        if len(ids) > 1 and w.id in ids:
            action = menu.addAction(f"批量找资料（{len(ids)} 部）", lambda chosen=list(ids): self.owner.start_batch(chosen))
            action.setEnabled(not (self.owner.lib and self.owner.lib.readonly))
            batch_cold = menu.addAction(f"冷藏所选（{len(ids)} 部）…",
                                        lambda chosen=list(ids): self.owner.cold_works(chosen))
            batch_cold.setEnabled(allowed and any(self.location_of(wid) == "常用" for wid in ids))
            batch_move = menu.addAction(f"移到其他游戏目录（{len(ids)} 部）…",
                                        lambda chosen=list(ids): self.owner.move_works(chosen))
            batch_move.setEnabled(allowed and any(self.location_of(wid) == "常用" for wid in ids))
            batch_delete = menu.addAction(f"删除所选的游戏文件（{len(ids)} 部）…",
                                          lambda chosen=list(ids): self.owner.delete_work_files(chosen))
            batch_delete.setEnabled(allowed and any(self.location_of(wid) != "已删除" for wid in ids))
        menu.addSeparator()
        menu.addAction("从书架移除…", lambda: self.owner.remove_work(w.id))
        menu.exec(self.view.viewport().mapToGlobal(pos))


class Editor(QDialog):
    def __init__(self, owner, work, focus_notes=False):
        super().__init__(owner)
        self.owner, self.work = owner, work
        self.lib=owner.lib
        self.cover_changed = False
        self.cover_path = work.cover_path
        self.saving = False
        self.refresh_needed = False
        self.finished.connect(self.closed)
        self.setWindowTitle("编辑作品")
        self.resize(600, 730)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24,20,24,20);layout.setSpacing(9)
        title=QLabel('编辑作品  <small>EDIT</small>'); title.setFont(font('title',18,True)); layout.addWidget(title)
        form = QFormLayout()
        self.title = QLineEdit(work.title)
        page=owner.library_page
        self.brand_input=TokenInput(owner,'brand',page.brands.values())
        self.tag_input=TokenInput(owner,'tag',page.tags.values(),page.groups)
        wm=page.metas.get(work.id,meta.WorkMeta(work.id,(),()))
        self.brand_input.set_ids(wm.brand_ids);self.tag_input.set_ids(wm.tag_ids)
        self.old_developer=QLabel('旧社团文字：'+(work.developer or '无')+'　·　可以填多个')
        self.old_developer.setFont(font('body',11));self.old_developer.setWordWrap(True)
        self.old_developer.setStyleSheet(f"color:{owner.theme.roles['sub']}")
        self._original_tag_ids = set(wm.tag_ids)
        info = page.infos.get(work.id)
        self.summary = QPlainTextEdit(info.summary or '' if info else '')
        self.summary.setFont(font('body',13))
        self.summary.setFixedHeight(88)
        self.summary.setStyleSheet(f"QPlainTextEdit {{background:{owner.theme.roles['card']};color:{owner.theme.roles['ink']};border:1px solid {owner.theme.roles['sakura']};border-radius:12px;padding:7px;}}")
        self._original_summary = self.summary.toPlainText()
        source = info.summary_source if info else None
        clue = '你改过' if source=='user' else f'来自 {records.SITE_LABEL[source]}；改了以后再绑定也不会覆盖' if source else ''
        self.summary_hint = QLabel(clue)
        self.summary_hint.setFont(font('body',11))
        self.summary_hint.setStyleSheet(f"color:{owner.theme.roles['sub']}")
        self.summary_hint.setVisible(bool(clue))
        if owner.lib.schema_version < 4:
            self.summary.clear(); self.summary.setEnabled(False)
        self.notes = QTextEdit(work.notes)
        self.hidden = QCheckBox("在普通书架中隐藏")
        self.hidden.setChecked(work.hidden)
        self.cover_label = QLabel("已有封面" if work.cover_path else "没有封面")
        for key, field in [("书名", self.title), ("品牌", self.brand_input), ("",self.old_developer),
                           ("标签",self.tag_input), ("简介",self.summary), ("",self.summary_hint), ("备注", self.notes), ("隐藏", self.hidden), ("封面", self.cover_label)]:
            form.addRow(key, field)
            label=form.labelForField(field)
            if label:
                label.setFont(font('body',10,True));label.setStyleSheet(f"color:{owner.theme.roles['beni']}")
        layout.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(Button(owner, "选择图片", self.choose_cover))
        row.addWidget(Button(owner, "清除封面", self.clear_cover))
        layout.addLayout(row)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        row = QHBoxLayout()
        self.cancel_button = Button(owner, "取消", self.reject)
        self.save_button = Button(owner, "保存", self.save, primary=True)
        row.addWidget(self.cancel_button)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        if not owner.lib or owner.lib.readonly:
            self.brand_input.setEnabled(False);self.tag_input.setEnabled(False);self.save_button.setEnabled(False)
        if focus_notes:
            self.notes.setFocus()

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t=self.owner.theme
        rounded(p,QRectF(self.rect()).adjusted(1,1,-1,-1),t.color('card'),18,t.color('white' if t.name=='day' else 'line'),2)
        rounded(p,QRectF(self.rect()).adjusted(6,6,-6,-6),t.color('card',0),14,t.color('sakura'))

    def choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择封面", "", "图片 (*.jpg *.jpeg *.png *.webp *.gif)")
        if path:
            self.cover_path, self.cover_changed = path, True
            self.cover_label.setText(path)

    def clear_cover(self):
        self.cover_path, self.cover_changed = None, True
        self.cover_label.setText("保存时清除封面记录（保留文件）")

    def save(self):
        if self.saving:
            return
        if not self.owner.lib or self.owner.lib.readonly:
            self.message.setText('Library is read-only');return
        if self.owner.lib is not self.lib:
            self.message.setText('库已切换，请重新打开编辑作品');return
        if self.owner.busy:
            self.message.setText("后台任务还在进行，请稍后再保存")
            return
        changes = dict(title=self.title.text(), notes=self.notes.toPlainText(), hidden=self.hidden.isChecked())
        if not changes["title"].strip():
            self.message.setText("书名不能为空")
            return
        self.saving = True
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        revision, cover_changed, image = self.work.revision, self.cover_changed, self.cover_path
        brands,tags=self.brand_input.ids(),self.tag_input.ids()
        lib=self.owner.lib
        work_id=self.work.id
        summary=self.summary.toPlainText()
        original_tags=set(self._original_tag_ids)
        self._pending_brands=[v[1] for v in brands if isinstance(v,tuple)]
        self._pending_tags=[v[1] for v in tags if isinstance(v,tuple)]
        def task(cancel):
            with lib.transaction():
                w = repo.edit_work(lib, work_id, revision, **changes)
                bs=[v if isinstance(v,int) else meta.create_brand(lib,v[1]) for v in brands]
                ts=[v if isinstance(v,int) else meta.create_tag(lib,v[1],v[2]) for v in tags]
                current=meta.set_work_meta(lib,w.id,w.revision,bs,ts)
                if lib.schema_version >= 4:
                    if summary != self._original_summary: binding.set_summary(lib,w.id,summary)
                    binding.block_tags(lib,w.id,original_tags-set(ts))
                    binding.unblock_tags(lib,w.id,set(ts)-original_tags)
                if cover_changed:
                    return repo.set_cover(lib,w.id,current,image)
                return repo.get_work(lib,w.id)
        try:
            self.owner.run_job("保存作品", task, self.saved, self.failed, cancellable=False, writes_library=True)
        except Exception as error:
            self.failed(error)

    def saved(self, work):
        self.saving = False
        self.work = work
        self.refresh_needed = True
        self.accept()

    def closed(self, result):
        if self.refresh_needed:
            self.refresh_needed = False
            self.owner.reload_after_editor(self.work.id)

    def failed(self, error):
        self.saving = False
        self.save_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        if isinstance(error, WriteConflict):
            self.refresh_needed = True
            self.message.setText("这部作品刚被别处改过，已重新载入")
            lib=self.owner.lib
            def read(cancel):
                return repo.get_work(lib,self.work.id),meta.list_work_meta(lib),meta.list_brands(lib),meta.list_tags(lib),binding.get_info(lib,self.work.id)
            def missing(err):
                self.message.setText("这部作品已从书架移除，无法继续编辑")
                self.save_button.setEnabled(False)
            def loaded(data):
                self.work,wm,brands,tags,info=data
                self.summary.setPlainText(info.summary or "")
                self._original_summary=self.summary.toPlainText()
                self._original_tag_ids=set(wm[self.work.id].tag_ids)
                self.title.setText(self.work.title)
                self.notes.setPlainText(self.work.notes)
                self.hidden.setChecked(self.work.hidden)
                self.brand_input.set_items(brands);self.tag_input.set_items(tags)
                self.brand_input.set_ids(wm[self.work.id].brand_ids);self.tag_input.set_ids(wm[self.work.id].tag_ids)
                self.old_developer.setText('旧社团文字：'+(self.work.developer or '无')+'　·　可以填多个')
                self.cover_path, self.cover_changed = self.work.cover_path, False
                self.cover_label.setText(self.cover_path or "没有封面")
            self.owner.run_job('重新载入编辑作品',read,loaded,missing,cancellable=False)
        elif isinstance(error,DuplicateName):
            lib=self.owner.lib
            def loaded(data):
                brands,tags=data
                self.brand_input.set_items(brands);self.tag_input.set_items(tags)
                brand_keys={meta.name_key(n) for b in brands for n in (b.name,*b.aliases)}
                tag_keys={meta.name_key(t.name) for t in tags}
                name=(next((n for n in self._pending_brands if meta.name_key(n) in brand_keys),None)
                      or next((n for n in self._pending_tags if meta.name_key(n) in tag_keys),None) or '这个名字')
                self.message.setText(f'「{name}」已经有了，请从候选里选')
            self.message.setText('这个名字已经有了，请从候选里选')
            self.owner.run_job('重新载入品牌和标签',lambda cancel:(meta.list_brands(lib),meta.list_tags(lib)),loaded,
                               lambda err:self.message.setText(str(err)),cancellable=False)
        else:
            self.message.setText(str(error))

    def reject(self):
        if not self.saving:
            super().reject()

    def closeEvent(self, event):
        if self.saving:
            event.ignore()
        else:
            super().closeEvent(event)
