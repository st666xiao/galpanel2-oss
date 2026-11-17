from dataclasses import dataclass
from pathlib import Path
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPixmap

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets"
ICONS = ROOT / "galpanel2" / "resources" / "icons"


def icon(name):
    """Absolute, forward-slash path usable inside a Qt stylesheet url()."""
    return (ICONS / name).as_posix()


@dataclass(frozen=True)
class Theme:
    name: str
    roles: dict[str, str]

    def color(self, role, alpha=None):
        c = QColor(self.roles[role])
        if alpha is not None:
            c.setAlpha(alpha)
        return c


BASE = dict(find_veil="#14121C", poster_badge="#000000", ai="#2C3E66", wakaba="#6AA37A", yamabuki="#E6A93A", white="#FFFFFF",
             primary_top="#E2667F", primary_bottom="#C9405E", gold_light="#F1D9A0", chrome="#15141C", menu_tint="#FBE9EE",
             status_want="#7EB6FF", status_playing="#F2B35E", status_done="#7FCFA4", status_dropped="#9C95A6",
             on_accent="#FFFFFF")
DAY = Theme("day", dict(BASE, paper="#FBF7F2", card="#FFFFFF", wall="#F4ECE6", ink="#2A2430",
             sub="#857A86", line="#EADFE0", sakura="#F3B6C3", usube="#FBE3E8", beni="#D4506C", kin="#C9A45C",
             chip="#F0ECEA", faint="#B9B2BD", placeholder="#EFEBE8"))
NIGHT = Theme("night", dict(BASE, paper="#1B1D29", card="#262838", wall="#161823", ink="#EFE8EE",
             sub="#A39AAE", line="#33354A", sakura="#C98A9C", usube="#3A2A3A", beni="#EF7590", kin="#D8B878",
             chip="#2E3042", faint="#6E6878", placeholder="#2B2D3D"))
MEDIA = Theme("media", dict(BASE, paper="#131214", wall="#0E0D0F", card="#1D1C20", chrome="#0E0D0F",
             chip="#29272D", placeholder="#232227", line="#302E35", ink="#F2EFEA", sub="#A8A39D", faint="#7A7570",
             beni="#F2B35E", sakura="#B98440", usube="#33291A", menu_tint="#33291A",
             primary_top="#F7C77C", primary_bottom="#EFAA4E", kin="#C9B27A", on_accent="#14110B"))
THEMES = {"day": DAY, "night": NIGHT, "media": MEDIA}

FAMILIES = {"title": "Yu Mincho", "body": "Microsoft YaHei UI",
             "body_bold": "Microsoft YaHei UI", "mono": "Consolas"}
_active_theme = "day"
_pixmaps = {}


def set_active_theme(name):
    """Remember which theme is active so font() can pick the right family."""
    global _active_theme
    _active_theme = name


def load_fonts():
    loaded = {}
    for p in sorted((ROOT / "galpanel2/resources/fonts").glob("*")):
        if p.suffix.lower() in (".otf", ".ttf"):
            i = QFontDatabase.addApplicationFont(str(p))
            if i >= 0:
                loaded[p.name] = QFontDatabase.applicationFontFamilies(i)[0]
    available = set(QFontDatabase.families())
    for role, candidates in {
        "title": [loaded.get("SourceHanSerifCN-SemiBold.otf"), "Yu Mincho", "SimSun", "Songti SC"],
        "body": [loaded.get("ResourceHanRoundedCN-Regular.ttf"), "Microsoft YaHei UI", "PingFang SC"],
        "body_bold": [loaded.get("ResourceHanRoundedCN-Bold.ttf"), loaded.get("ResourceHanRoundedCN-Regular.ttf"),
                      "Microsoft YaHei UI", "PingFang SC"],
        "mono": [loaded.get("JetBrainsMono-Regular.ttf"), "Consolas", "Menlo"],
    }.items():
        FAMILIES[role] = next((x for x in candidates if x and x in available), "sans-serif")
    return loaded


def font(role="body", size=13, bold=False, italic=False):
    key = role
    bold_key = role == "body" and bold
    # The media theme uses the sans-serif body family for titles too; the old
    # day / night themes keep their serif title family.
    if _active_theme == "media" and role == "title":
        key, bold_key = "body", False
    f = QFont(FAMILIES["body_bold" if bold_key else key])
    f.setPixelSize(size)
    f.setBold(bold)
    f.setItalic(italic)
    return f


def asset(relative):
    if relative not in _pixmaps:
        _pixmaps[relative] = QPixmap(str(ASSETS / relative))
    return _pixmaps[relative]


def stylesheet(t):
    c = t.roles
    # No media-specific icon asset exists; reuse the night chevron for media.
    icon_theme = t.name if t.name in ("day", "night") else "night"
    chevron = icon(f"chevron-down-{icon_theme}.svg")
    check = icon("check-white.svg")
    return f"""QWidget {{ color: {c['ink']}; }}
QLineEdit,QTextEdit,QComboBox,QSpinBox {{ background:{c['card']}; color:{c['ink']}; border:1px solid {c['line']}; border-radius:10px; padding:6px 10px; }}
QLineEdit:focus,QTextEdit:focus,QComboBox:focus,QSpinBox:focus {{ border:1px solid {c['beni']}; }}
QComboBox::drop-down {{ border:0; width:24px; }}
QComboBox::down-arrow {{ image:url({chevron}); width:10px; height:10px; }}
QComboBox QAbstractItemView {{ background:{c['card']}; border:1px solid {c['line']}; color:{c['ink']}; selection-background-color:{c['usube']}; selection-color:{c['ink']}; padding:6px 10px; }}
QComboBox QAbstractItemView::item {{ padding:6px 10px; }}
QDialog,QMenu {{ background:{c['paper']}; }}
QScrollArea,QScrollArea > QWidget,QScrollArea > QWidget > QWidget {{ background:transparent; border:0; }}
QTableWidget,QListView {{ background:transparent; color:{c['ink']}; border:0; outline:0; }}
QTableWidget::item {{ padding:6px; border-bottom:1px solid {c['line']}; }}
QTableWidget::item:selected {{ background:{c['usube']}; color:{c['ink']}; }}
QTableWidget::indicator:disabled {{ width:12px; height:12px; background:{c['line']}; border:1px solid {c['sub']}; border-radius:2px; }}
QHeaderView::section {{ background:{c['wall']}; color:{c['sub']}; border:0; padding:8px; }}
QTableCornerButton::section {{ background:{c['wall']}; border:0; }}
QScrollBar:vertical {{ background:{c['wall']}; width:9px; border-radius:4px; }}
QScrollBar::handle:vertical {{ background:{c['sakura']}; min-height:24px; border-radius:4px; }}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0px; }}
QToolTip {{ background:{c['card']}; color:{c['ink']}; border:1px solid {c['sakura']}; }}
QCheckBox {{ spacing:8px; }}
QCheckBox::indicator {{ width:16px; height:16px; border-radius:4px; border:1px solid {c['sub']}; background:{c['card']}; }}
QCheckBox::indicator:checked {{ background:{c['beni']}; border:1px solid {c['beni']}; image:url({check}); }}
QMenu {{ background:{c['card']}; border:1px solid {c['line']}; padding:6px; }}
QMenu::item {{ padding:7px 18px; border-radius:6px; color:{c['ink']}; }}
QMenu::item:selected {{ background:{c['usube']}; color:{c['beni']}; }}
QPushButton {{ background:{c['wall']}; color:{c['ink']}; border:0; border-radius:8px; padding:6px 16px; min-height:24px; }}
QPushButton:default {{ background:{c['beni']}; color:{c['white']}; }}
QMessageBox {{ background:{c['paper']}; }}
"""
