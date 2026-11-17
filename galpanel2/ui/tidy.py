"""整理书架。查库都在 run_job 里，主线程只拿结果画界面。"""
import html
import ntpath
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFontMetrics, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QCheckBox,
    QLineEdit, QPushButton, QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QGridLayout,
    QSizePolicy)

from galpanel2.core import batch as BT, play, records as R, repository as repo, tidy
from galpanel2.core import storage as cold_storage
from galpanel2.core.domain import SessionActive, WriteConflict
from .theme import asset, font
from .widgets import Button

AI2 = "#3f5689"
SECTIONS = (
    ("missing", "找不到", "MISSING"),
    ("junk", "疑似不是游戏", "NOT A GAME"),
    ("dup", "疑似重复", "DUPLICATES"),
    ("title", "标题", "TITLES"),
    ("legacy", "旧资料", "LEGACY"),
    ("ignored", "扫描跳过的路径", "IGNORED"),
)
TITLE_REASON = {"cleaned": "去版本号", "legacy_original": "旧库原名", "parent_folder": "上层文件夹"}
TITLE_FILTERS = ("全部", "去版本号", "旧库原名", "上层文件夹")
LEGACY_FILTERS = ("全部", "有 VNDB 号", "有简介")
JUNK_REASON = {"tool_word": "像工具/补丁", "tiny": "文件很小", "no_exe": "没有可执行文件"}
DUP_REASON = {"same_title": "书名相同", "archive_and_folder": "压缩包和文件夹"}
SCOPE_TEXT = {"exact": "只这一个", "tree": "整个文件夹"}
IGNORE_REASON = {"removed": "移除时自动", "user": "你加的"}
LOG_ACTION = {"remove": "移除", "merge": "合并", "retitle": "改书名", "ignore": "跳过路径",
              "legacy_summary": "用旧简介"}
OTHER_SITE = {"steam": "Steam", "f95": "F95zone", "ymgal": "月幕"}
PAGE = 50
PLAYING = "正在游玩，先关掉游戏再整理"


def _self_style(widget, css):
    """样式只画在这个控件上。不带选择器的 border 会漏到里面的 QLabel。"""
    name = widget.objectName() or ""
    if not name.startswith("tidy"):
        name = f"tidy{id(widget):x}"
        widget.setObjectName(name)
    widget.setStyleSheet(f"#{name} {{ {css} }}")


def _plain(widget):
    _self_style(widget, "background:transparent; border:none;")


def _stop_text(exc):
    if isinstance(exc, SessionActive):
        return PLAYING
    if isinstance(exc, WriteConflict):
        return "有作品刚被改过，已重新载入，请再确认一次"
    return str(exc)


def _someone_playing(lib):
    with lib.connection() as c:
        return c.execute("SELECT 1 FROM play_sessions WHERE state='running' LIMIT 1").fetchone() is not None


def _copies_present(lib, work_ids):
    found = []
    with lib.connection() as c:
        for wid in work_ids:
            paths = [row[0] for row in c.execute("SELECT path FROM copies WHERE work_id=?", (wid,))]
            if any(os.path.exists(path) for path in paths):
                found.append(wid)
    return found
MISSING_HINT = ("「移除」只删书架上的记录，不删磁盘上的文件；以后扫描遇到这个路径会跳过"
                "（在「扫描跳过的路径」里能撤销）。移除前会再看一眼磁盘，文件又出现了的不会移除。")
MISSING_HINT2 = "文件只是挪了位置的：先在「出所」里加上新位置并扫描，再到「疑似重复」把新旧两条合并，旧资料和游玩记录就保住了。"
TITLE_HINT = "「上层文件夹」的建议默认不勾。改书名不删任何东西，旧书名记在整理记录里。"
LEGACY_HINT = ("「按编号找资料」打开找资料窗口并填好编号，绑定前照常预览。"
               "「用旧简介」把旧简介写成你自己的简介，以后绑定不会覆盖。")


def _local_minute(text):
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return str(text)
    return moment.strftime("%Y-%m-%d %H:%M")


def _elide(text, width, role="body", size=12):
    return QFontMetrics(font(role, size)).elidedText(text or "", Qt.TextElideMode.ElideMiddle, width)


class _Ellipsis(QLabel):
    """Shrinks below the full string and draws an ellipsis, so a long title or path cannot widen the page."""

    def __init__(self, text, role="body", size=12, bold=False, mode=Qt.TextElideMode.ElideRight, tip=False):
        super().__init__()
        self._full = "" if text is None else str(text)
        self._mode = mode
        self._always_tip = tip
        self._fitting = False
        self.setFont(font(role, size, bold))
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(24)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setText(self._full)
        if tip and self._full:
            self.setToolTip(self._full)

    def _line(self):
        metrics = self.fontMetrics()
        return max(metrics.height(), metrics.lineSpacing()) + 2

    def minimumSizeHint(self):
        return QSize(24, self._line())

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, self._line())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fitting:
            return
        width = max(0, self.contentsRect().width())
        if width <= 0:
            return
        shown = self.fontMetrics().elidedText(self._full, self._mode, width)
        self._fitting = True
        try:
            if shown != self.text():
                self.setText(shown)
            if not self._always_tip:
                tip = self._full if shown != self._full else ""
                if tip != self.toolTip():
                    self.setToolTip(tip)
        finally:
            self._fitting = False


def _parent(path):
    return ntpath.dirname(ntpath.normpath(str(path or "").replace("/", "\\")))


def _offline_text(exc):
    rest = str(exc).split("drive offline:", 1)[-1].strip()
    drive = rest[:2] if len(rest) >= 2 and rest[1] == ":" else (rest[:1] or "?")
    return f"{drive} 现在不在，没有移除这一组"


def _log_summary(row):
    action, detail = row.get("action"), row.get("detail") or {}
    if action in ("remove", "merge"):
        works = detail.get("works") if action == "remove" else detail.get("merged") or []
        titles = "、".join(item.get("title") or "" for item in works)
        return f"{len(works)} 部 {titles}".strip()
    if action == "retitle":
        return f"{len(detail.get('changes') or [])} 条"
    if action == "ignore":
        op = "撤销" if detail.get("op") == "remove" else "加上"
        return f"{detail.get('path') or ''} {op}".strip()
    if action == "legacy_summary":
        return f"{detail.get('chars') or 0} 字"
    return ""


def load_snapshot(lib):
    groups = tidy.missing_groups(lib)
    suspect = tidy.suspect_report(lib)
    duplicates = tidy.duplicate_report(lib)
    titles = tidy.title_report(lib)
    legacy = tidy.legacy_report(lib)
    ignored = tidy.list_ignored(lib)
    log = tidy.list_log(lib)
    times = play.list_playtimes(lib)
    dismissed = tidy.DismissStore(lib.path, lib.uuid).load()
    legacy_ids = {row["work_id"] for row in legacy}
    with lib.connection() as c:
        works = {r[0]: {"title": r[1], "developer": r[2], "revision": r[3], "hidden": bool(r[4])}
                 for r in c.execute("SELECT id,title,developer,revision,hidden FROM works")}
        copies = {}
        for cid, wid, kind, path, availability in c.execute(
                "SELECT id,work_id,kind,path,availability FROM copies ORDER BY id"):
            copies.setdefault(wid, []).append({"id": cid, "kind": kind, "path": path,
                                               "availability": availability})
        links = {}
        for wid, site, site_id in c.execute("SELECT work_id,site,site_id FROM external_links ORDER BY site"):
            links.setdefault(wid, {})[site] = site_id
        info = {r[0]: (r[1], r[2]) for r in c.execute("SELECT work_id,summary,summary_source FROM work_info")}
        tags = dict(c.execute("SELECT work_id,COUNT(*) FROM work_tags GROUP BY work_id"))
    deleted = {cid for cid, item in cold_storage.list_storage(lib).items()
               if item.location == "已删除"}
    for wid in list(copies):
        copies[wid] = [copy for copy in copies[wid] if copy["id"] not in deleted]
        if not copies[wid]:
            del copies[wid]
    missing_ids = {wid for group in groups for wid in group["work_ids"]}
    missing = []
    for group in groups:
        rows = []
        for wid in group["work_ids"]:
            cps = copies.get(wid) or []
            if not cps:
                continue  # every copy was permanently deleted
            leg = next((row for row in legacy if row["work_id"] == wid), None)
            rows.append({"id": wid, "revision": works[wid]["revision"], "title": works[wid]["title"],
                         "path": cps[0]["path"] if cps else "", "folder": group["group"],
                         "archive": bool(cps) and all(cp["kind"] == "archive" for cp in cps),
                         "vndb": leg.get("vndb") if leg else None, "in_legacy": leg is not None})
        if rows:
            missing.append({"group": group["group"], "group_key": group["group_key"], "works": rows})
    junk = []
    for item in suspect:
        wid = item["work_id"]
        if wid in missing_ids or wid in dismissed["junk"] or wid not in works:
            continue
        junk.append({"id": wid, "revision": works[wid]["revision"], "title": item["title"],
                     "reasons": list(item["reasons"]), "path": item["path"], "folder": _parent(item["path"])})
    dups = []
    for group in duplicates:
        key = tuple(sorted(group["work_ids"]))
        if key in dismissed["duplicates"]:
            continue
        rows = []
        for wid in group["work_ids"]:
            detail = repo.get_work(lib, wid)
            summary, source = info.get(wid, (None, None))
            rows.append({"id": wid, "revision": detail.revision, "title": detail.title,
                         "developer": detail.developer or "", "hidden": detail.hidden,
                         "copies": [{"path": cp.path, "availability": cp.availability, "kind": cp.kind}
                                    for cp in detail.copies],
                         "cover": detail.cover_path, "seconds": times[wid].total_seconds if wid in times else 0,
                         "links": dict(links.get(wid) or {}), "summary": summary, "summary_source": source,
                         "tags": int(tags.get(wid) or 0), "legacy": wid in legacy_ids})
        dups.append({"work_ids": list(group["work_ids"]), "titles": list(group["titles"]),
                     "reason": group["reason"], "works": rows})
    kept_titles = [row for row in titles if row["work_id"] not in dismissed["titles"]]
    legacy_rows = []
    for row in legacy:
        summary, source = info.get(row["work_id"], (None, None))
        legacy_rows.append(dict(row, summary_source=source, own_summary=summary))
    return {"missing": missing, "junk": junk, "dups": dups, "titles": kept_titles, "legacy": legacy_rows,
            "ignored": ignored, "log": log, "dismiss": dismissed}


def _default_keep(works):
    def rank(work):
        return (work["seconds"], 1 if work["links"] else 0, -work["id"])
    return max(works, key=rank)["id"]


def _merge_note(works, keep_id):
    keep = next(work for work in works if work["id"] == keep_id)
    others = sorted((work for work in works if work["id"] != keep_id), key=lambda work: work["id"])
    keep_club = (keep["developer"] or "").strip()
    donor = next(((work["developer"] or "").strip() for work in others if (work["developer"] or "").strip()), "")
    if keep_club:
        club = "社团用主作品的"
    elif donor:
        club = f"社团主作品没有 → 用 {donor}"
    else:
        club = "社团：都没有"
    held = set(keep["links"])
    dropped = []
    for work in others:
        for site in R.SITES:
            if site not in work["links"]:
                continue
            if site in held:
                if site not in dropped:
                    dropped.append(site)
            else:
                held.add(site)
    binding = "站点绑定主作品已有的站不动。"
    if dropped:
        names = "、".join(R.SITE_LABEL[site] for site in dropped)
        binding += f"次作品的 {names} 会丢弃，写进整理记录。"
    summary = "用次作品的" if keep.get("summary") is None and keep.get("summary_source") is None else "不换"
    copies = sum(len(work["copies"]) for work in works)
    return (f"书名、隐藏用主作品的。{club}。副本 {copies} 个都挂到主作品（启动时可选）。"
            f"游玩记录相加，标签合在一起。{binding}简介{summary}。合并前自动备份。")


class DupCard(QWidget):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._callback()
        else:
            super().mousePressEvent(event)


class RemoveDialog(QDialog):
    def __init__(self, owner, count, folder, scan, backup):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("确认移除")
        self.resize(680, 400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        title = QLabel("确认移除")
        _plain(title)
        title.setFont(font("title", 20, True))
        lay.addWidget(title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        rows = (("移除", f"{count} 部" + (f"（全在 {folder}）" if folder else "")),
                ("磁盘文件", "不动。只删书架记录、游玩记录和封面引用"),
                ("以后扫描", scan), ("先备份到", backup))
        for index, (name, value) in enumerate(rows):
            label = QLabel(name)
            label.setFont(font("body", 12))
            _self_style(label, f"color:{owner.theme.roles['sub']}; background:transparent; border:none;")
            text = QLabel(value)
            _plain(text)
            text.setWordWrap(True)
            text.setMinimumWidth(460)
            text.setFont(font("mono" if name == "先备份到" else "body", 12))
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(label, index, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(text, index, 1)
        lay.addLayout(grid)
        note = QLabel("后悔了可以用备份文件恢复整个库（「整理记录」里有每次的备份位置）。")
        note.setWordWrap(True)
        note.setFont(font("body", 11))
        _self_style(note, f"color:{owner.theme.roles['sub']}; background:transparent; border:none;")
        lay.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(Button(owner, "取消", lambda checked=False: self.reject()))
        ok = Button(owner, "备份并移除", lambda checked=False: self.accept(), primary=True, subtitle="REMOVE")
        ok.setMinimumWidth(150)
        buttons.addWidget(ok)
        lay.addLayout(buttons)


class LogDialog(QDialog):
    def __init__(self, owner, rows):
        super().__init__(owner)
        self.setWindowTitle("整理记录")
        self.resize(860, 480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        title = QLabel("整理记录")
        title.setFont(font("title", 18, True))
        lay.addWidget(title)
        table = QTableWidget(len(rows), 4)
        table.setHorizontalHeaderLabels(["时间", "动作", "概要", "备份"])
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 150), (1, 90), (3, 220)):
            table.setColumnWidth(col, width)
        for index, row in enumerate(rows):
            backup = Path(row["backup_path"]).name if row.get("backup_path") else ""
            values = (_local_minute(row.get("at")), LOG_ACTION.get(row.get("action"), row.get("action") or ""),
                      _log_summary(row), backup)
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                table.setItem(index, col, item)
        lay.addWidget(table, 1)
        lay.addWidget(Button(owner, "关闭", lambda checked=False: self.reject()))


class TidyPage(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.section = "missing"
        self.snap = None
        self.checked_missing = set()
        self.checked_junk = set()
        self.checked_titles = set()
        self.title_edits = {}
        self._title_seeded = set()
        self.expanded = set()
        self.expanded_all = set()
        self.dup_key = None
        self.keeps = {}
        self.title_filter = "全部"
        self.legacy_filter = "全部"
        self._shot = None
        self._pending_expand = False
        self._pending_confirm = False
        self._retitle = False
        self._filling = False
        self._preview = []
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.side = QWidget()
        self.side.setFixedWidth(210)
        side = QVBoxLayout(self.side)
        side.setContentsMargins(14, 14, 8, 12)
        side.setSpacing(6)
        self.nav_host = QVBoxLayout()
        self.nav_host.setSpacing(4)
        side.addLayout(self.nav_host)
        side.addStretch()
        self.backup_label = QLabel("每次移除、合并前自动备份整个库。\n上次备份：还没有备份")
        self.backup_label.setWordWrap(True)
        self.backup_label.setFont(font("body", 11))
        side.addWidget(self.backup_label)
        links = QHBoxLayout()
        links.setSpacing(8)
        links.addWidget(self._link("整理记录", self._show_log))
        links.addWidget(self._link("打开备份所在文件夹", self._open_backup_dir))
        side.addLayout(links)
        root.addWidget(self.side)
        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        self.banner = QLabel("只读打开的库只能看，不能整理")
        self.banner.setFont(font("body", 12, True))
        self.banner.setContentsMargins(18, 8, 18, 0)
        self.banner.hide()
        main.addWidget(self.banner)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        main.addLayout(self.body, 1)
        root.addLayout(main, 1)
        self._paint()

    def prepare(self, section, expand, confirm):
        self._shot = (section or "missing", bool(expand), bool(confirm))

    def reload(self, note=None):
        lib = self.owner.lib
        if lib is None or self.owner.busy:
            return
        def done(snap):
            self._apply(snap)
            if note:
                self._note(note)
        self.owner.run_job("载入整理", lambda cancel: load_snapshot(lib), done, self._fail, cancellable=False)

    def rebuild(self):
        self._paint()
        self._fill_side()
        self._update_backup()
        self.banner.setVisible(bool(self.owner.lib and self.owner.lib.readonly))
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        if not self.snap:
            return
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(16, 10, 20, 10)
        lay.setSpacing(8)
        if self.section in ("title", "legacy"):
            self._build_split(lay)
        elif self.section == "junk":
            self._build_junk(lay)
        elif self.section == "dup":
            self._build_dup(lay)
        elif self.section == "ignored":
            self._build_ignored(lay)
        else:
            self._build_missing(lay)
        self.body.addWidget(host)

    def _apply(self, snap):
        self.snap = snap
        self._seed_titles(snap["titles"])
        live_missing = {work["id"] for group in snap["missing"] for work in group["works"]}
        self.checked_missing &= live_missing
        self.checked_junk &= {row["id"] for row in snap["junk"]}
        if self._shot:
            section, expand, confirm = self._shot
            self._shot = None
            self.section = section
            self._pending_expand = bool(expand and section == "missing")
            self._pending_confirm = bool(confirm and self._pending_expand)
        self.rebuild()

    def _paint(self):
        page = self.palette()
        page.setColor(page.ColorRole.Window, self.owner.theme.color("paper"))
        self.setPalette(page)
        self.setAutoFillBackground(True)
        sub = self.owner.theme.roles["sub"]
        _self_style(self.backup_label, f"color:{sub}; background:transparent; border:none;")
        ink = self.owner.theme.roles["ink"]
        _self_style(self.banner, f"color:{self.owner.theme.roles['beni']}; background:transparent; border:none;")
        _self_style(self.side, f"color:{ink}; background:transparent; border:none;")

    def _note(self, message):
        self.owner.last_message = message
        self.owner.status.idle(message)

    def _can_write(self):
        lib = self.owner.lib
        return bool(lib and not lib.readonly and not self.owner.screenshot_mode)

    def _begin_library_write(self):
        if not self._can_write():
            return False
        runner = self.owner.batch_runner
        if runner is not None and runner.isRunning():
            self._note("后台找资料还在跑，先在待确认页暂停")
            return False
        return True

    def _fail(self, error):
        retitle = self._retitle
        self._retitle = False
        if isinstance(error, SessionActive):
            self._reload_after_error(PLAYING)
            return
        if isinstance(error, WriteConflict) and retitle:
            self._reload_after_error("有作品刚被改过，已重新载入，请再确认一次")
            return
        self.owner.notify(str(error))
        self._reload_after_error(str(error))

    def _reload_after_error(self, note):
        lib = self.owner.lib
        if lib is None or self.owner.busy:
            if note:
                self._note(note)
            return
        def done(snap):
            self._apply(snap)
            if note:
                self._note(note)
            if self.owner.lib and not self.owner.busy:
                self.owner.reload_library(message=note)
        self.owner.run_job("载入整理", lambda cancel: load_snapshot(lib), done,
                           lambda error: self.owner.notify(str(error)), cancellable=False)

    def _link(self, text, callback, enabled=True, color=None):
        color = color or self.owner.theme.roles["beni"]
        if not enabled:
            label = QLabel(text)
            label.setFont(font("body", 12))
            _self_style(label, f"color:{self.owner.theme.roles['sub']}; background:transparent; border:none;")
            return label
        label = QLabel(f"<a href='go' style='color:{color};text-decoration:none'>{html.escape(text)}</a>")
        _plain(label)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setFont(font("body", 12))
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        label.linkActivated.connect(lambda _href, fn=callback: fn())
        return label

    def _pill(self, text, kind):
        palette = {"gray": (self.owner.theme.roles["line"], self.owner.theme.roles["sub"]),
                   "yellow": ("#fbf0d8", "#a8741a"), "green": ("#e3f1e6", "#3c7a50"),
                   "red": (self.owner.theme.roles["usube"], self.owner.theme.roles["beni"])}
        bg, fg = palette[kind]
        label = QLabel(text)
        label.setFont(font("body", 11, True))
        _self_style(label, f"color:{fg}; background:{bg}; border:none; border-radius:8px; padding:1px 8px;")
        return label

    def _hint(self, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setFont(font("body", 11))
        _self_style(label, f"color:{self.owner.theme.roles['sub']}; background:transparent; border:none;")
        return label

    def _scroll(self, inner, sideways=True):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        if not sideways:
            inner.setMinimumWidth(0)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        return scroll

    def _heading(self, title, en, meta):
        row = QHBoxLayout()
        label = QLabel(f"{html.escape(title)}  <span style='color:{self.owner.theme.roles['beni']};"
                       f"font-size:11px'>{html.escape(en)}</span>")
        _plain(label)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setFont(font("title", 22, True))
        row.addWidget(label)
        if meta:
            note = QLabel(meta)
            note.setWordWrap(True)
            note.setFont(font("body", 11))
            _self_style(note, f"color:{self.owner.theme.roles['sub']}; background:transparent; border:none;")
            row.addWidget(note, 1)
        return row

    def _bar(self, color=None):
        bar = QWidget()
        bar.setFixedHeight(52)
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _self_style(bar, f"background:{color or self.owner.theme.roles['ai']}; border:none; border-radius:18px;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 6, 8, 6)
        lay.setSpacing(8)
        return bar, lay

    def _bar_text(self, text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setFont(font("body", 13))
        _self_style(label, "color:#ffffff; background:transparent; border:none;")
        return label

    def _counts(self):
        if not self.snap:
            return {key: 0 for key, _name, _en in SECTIONS}
        snap = self.snap
        return {"missing": sum(len(group["works"]) for group in snap["missing"]), "junk": len(snap["junk"]),
                "dup": len(snap["dups"]), "title": len(snap["titles"]), "legacy": len(snap["legacy"]),
                "ignored": len(snap["ignored"])}

    def _fill_side(self):
        while self.nav_host.count():
            item = self.nav_host.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        counts = self._counts()
        ink, sub, beni = (self.owner.theme.roles[name] for name in ("ink", "sub", "beni"))
        card = self.owner.theme.roles["card"]
        for key, name, en in SECTIONS:
            row = QWidget()
            row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            on = key == self.section
            _self_style(row, f"background:{card if on else 'transparent'}; border:none; border-radius:12px;")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(8, 4, 8, 4)
            label = QLabel(f"<a href='{key}' style='color:{ink};text-decoration:none'>{html.escape(name)}"
                           f"<br><span style='color:{sub};font-size:9px'>{html.escape(en)}</span></a>")
            _plain(label)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setFont(font("body", 13, True))
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            label.linkActivated.connect(lambda href, section=key: self._set_section(section))
            count = QLabel(str(counts[key]))
            count.setFont(font("title", 15, True))
            _self_style(count, f"color:{sub if counts[key] == 0 else beni}; background:transparent; border:none;")
            lay.addWidget(label, 1)
            lay.addWidget(count)
            self.nav_host.addWidget(row)

    def _set_section(self, key):
        if key == self.section and self.body.count():
            return
        self.section = key
        self.rebuild()

    def _update_backup(self):
        last = "还没有备份"
        for row in reversed((self.snap or {}).get("log") or []):
            if row.get("backup_path"):
                last = Path(row["backup_path"]).name
                break
        self.backup_label.setText(f"每次移除、合并前自动备份整个库。\n上次备份：{last}")

    def _seed_titles(self, rows):
        ids = {row["work_id"] for row in rows}
        self.checked_titles &= ids
        self._title_seeded &= ids
        for wid in list(self.title_edits):
            if wid not in ids:
                self.title_edits.pop(wid, None)
        for row in rows:
            wid = row["work_id"]
            self.title_edits.setdefault(wid, row["suggested"])
            if wid not in self._title_seeded:
                self._title_seeded.add(wid)
                if row["reason"] != "parent_folder":
                    self.checked_titles.add(wid)

    def _show_log(self):
        rows = list(reversed((self.snap or {}).get("log") or []))
        LogDialog(self.owner, rows).exec()

    def _open_backup_dir(self):
        lib = self.owner.lib
        if lib is None or self.owner.screenshot_mode:
            return
        self.owner.open_external(Path(lib.path).parent.as_uri())

    def _finish_write(self, payload):
        message, snap = payload
        self._apply(snap)
        if self.owner.lib and not self.owner.busy:
            self.owner.reload_library(message=message)
        else:
            self._note(message)

    def _run(self, label, task):
        lib = self.owner.lib
        def done(payload):
            self._retitle = False
            self._finish_write(payload)
        self.owner.run_job(label, task, done, self._fail, cancellable=False, writes_library=True)

    def _build_missing(self, lay):
        lay.addLayout(self._heading("整理书架", "TIDY", "库里有记录、磁盘上找不到文件的作品，按所在文件夹分组。"))
        if self._pending_expand and self.snap["missing"]:
            group = self.snap["missing"][0]
            self._pending_expand = False
            self.expanded.add(group["group_key"])
            self.checked_missing.update(work["id"] for work in group["works"])
        confirm = self._pending_confirm
        self._pending_confirm = False
        inner = QWidget()
        rows = QVBoxLayout(inner)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(8)
        self._group_ids = {}
        self._group_boxes = {}
        self._work_boxes = {}
        writable = self._can_write()
        for group in self.snap["missing"]:
            rows.addWidget(self._missing_group(group, writable))
        rows.addStretch()
        lay.addWidget(self._scroll(inner, sideways=False), 1)
        lay.addWidget(self._hint(MISSING_HINT))
        lay.addWidget(self._hint(MISSING_HINT2))
        bar, bar_lay = self._bar()
        self.missing_label = self._bar_text("")
        bar_lay.addWidget(self.missing_label, 1)
        clear = Button(self.owner, "全不选", self._clear_missing)
        clear.setMinimumWidth(88)
        self.remove_button = Button(self.owner, "移除勾选的 0 部", self._remove_checked, primary=True, subtitle="REMOVE")
        self.remove_button.setMinimumWidth(200)
        bar_lay.addWidget(clear)
        bar_lay.addWidget(self.remove_button)
        lay.addWidget(bar)
        self._sync_missing()
        if confirm:
            self._open_remove(self._plan_missing(), preview=True)

    def _missing_group(self, group, writable):
        card = QWidget()
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _self_style(card, f"background:{self.owner.theme.roles['card']}; border:1px solid "
                    f"{self.owner.theme.roles['line']}; border-radius:14px;")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        head = QHBoxLayout()
        open_now = group["group_key"] in self.expanded
        tri = QPushButton("▾" if open_now else "▸")
        tri.setFixedSize(22, 22)
        tri.setFlat(True)
        tri.setCursor(Qt.CursorShape.PointingHandCursor)
        tri.setStyleSheet(f"QPushButton {{ border:0; background:transparent; color:{self.owner.theme.roles['sub']}; }}")
        box = QCheckBox()
        ids = [work["id"] for work in group["works"]]
        self._group_ids[group["group_key"]] = ids
        self._group_boxes[group["group_key"]] = box
        box.toggled.connect(lambda on, key=group["group_key"]: self._toggle_group(key, on))
        path = _Ellipsis(group["group"], "mono", 12, mode=Qt.TextElideMode.ElideMiddle, tip=True)
        _plain(path)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        count = QLabel(str(len(group["works"])))
        count.setFont(font("title", 16, True))
        _self_style(count, f"color:{self.owner.theme.roles['beni']}; background:transparent; border:none;")
        head.addWidget(tri)
        head.addWidget(box)
        head.addWidget(path)
        head.addWidget(count)
        unit = QLabel("部")
        _plain(unit)
        head.addWidget(unit)
        if group["works"] and all(work["archive"] for work in group["works"]):
            head.addWidget(self._pill("全是压缩包", "gray"))
        legacy = sum(work["in_legacy"] for work in group["works"])
        if legacy:
            head.addWidget(self._pill(f"{legacy} 部有旧资料", "yellow"))
        head.addStretch()
        head.addWidget(self._link("整组移除，以后扫描跳过这个文件夹",
                                  lambda g=group: self._remove_whole(g), writable))
        lay.addLayout(head)
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(2)
        self._fill_group_rows(group, body_lay, all_rows=group["group_key"] in self.expanded_all)
        body.setVisible(open_now)
        lay.addWidget(body)
        def toggle(*_args):
            opened = not body.isVisible()
            body.setVisible(opened)
            tri.setText("▾" if opened else "▸")
            if opened:
                self.expanded.add(group["group_key"])
            else:
                self.expanded.discard(group["group_key"])
        tri.clicked.connect(toggle)
        return card

    def _fill_group_rows(self, group, lay, all_rows):
        works = group["works"]
        shown = works if all_rows or len(works) <= PAGE else works[:PAGE]
        writable = self._can_write()
        for work in shown:
            lay.addLayout(self._missing_row(work, writable))
        if len(works) > len(shown):
            more = QHBoxLayout()
            more.addSpacing(28)
            extra = QLabel(f"…… 另外 {len(works) - len(shown)} 部")
            _plain(extra)
            more.addWidget(extra)
            more.addStretch()
            def expand_all(g=group, host=lay):
                self.expanded_all.add(g["group_key"])
                while host.count():
                    item = host.takeAt(0)
                    if item.widget():
                        item.widget().hide()
                        item.widget().deleteLater()
                    elif item.layout():
                        self._drop_layout(item.layout())
                self._fill_group_rows(g, host, True)
                self._sync_missing()
            more.addWidget(self._link("展开全部", expand_all))
            lay.addLayout(more)

    def _drop_layout(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout():
                self._drop_layout(item.layout())

    def _missing_row(self, work, writable):
        row = QHBoxLayout()
        box = QCheckBox()
        self._work_boxes[work["id"]] = box
        box.toggled.connect(lambda on, wid=work["id"]: self._toggle_work(wid, on))
        title = _Ellipsis(work["title"], "body", 13)
        _plain(title)
        path = _Ellipsis(work["path"], "mono", 11, mode=Qt.TextElideMode.ElideMiddle, tip=True)
        _self_style(path, f"color:{self.owner.theme.roles['sub']}; background:transparent; border:none;")
        row.addSpacing(24)
        row.addWidget(box)
        row.addWidget(title)
        if work["in_legacy"]:
            text = "有旧资料" + (f" · {work['vndb']}" if work.get("vndb") else "")
            row.addWidget(self._pill(text, "green"))
        row.addWidget(path, 1)
        row.addWidget(self._link("移除", lambda item=work: self._remove_one(item), writable))
        return row

    def _toggle_group(self, key, on):
        if self._filling:
            return
        ids = self._group_ids.get(key) or []
        if on:
            self.checked_missing.update(ids)
        else:
            self.checked_missing.difference_update(ids)
        self._sync_missing()

    def _toggle_work(self, wid, on):
        if self._filling:
            return
        if on:
            self.checked_missing.add(wid)
        else:
            self.checked_missing.discard(wid)
        self._sync_missing()

    def _sync_missing(self):
        self._filling = True
        try:
            for wid, box in self._work_boxes.items():
                box.setChecked(wid in self.checked_missing)
            for key, box in self._group_boxes.items():
                ids = self._group_ids.get(key) or []
                box.setChecked(bool(ids) and all(wid in self.checked_missing for wid in ids))
        finally:
            self._filling = False
        self._update_missing_bar()

    def _update_missing_bar(self):
        n = len(self.checked_missing)
        folders = sum(1 for ids in self._group_ids.values() if ids and all(wid in self.checked_missing for wid in ids))
        self.missing_label.setText(f"已勾选 <b style='color:#F3B6C3'>{n}</b> 部 · {folders} 个文件夹以后扫描跳过")
        self.remove_button.setText(f"移除勾选的 {n} 部")
        self.remove_button.setEnabled(n > 0 and self._can_write())

    def _clear_missing(self, *_args):
        self.checked_missing.clear()
        self._sync_missing()

    def _plan_missing(self):
        full, partial = [], []
        chosen = self.checked_missing
        for group in self.snap["missing"]:
            ids = [work["id"] for work in group["works"]]
            if ids and all(wid in chosen for wid in ids):
                full.append(group)
            else:
                partial.extend(work for work in group["works"] if work["id"] in chosen)
        return full, partial

    def _remove_checked(self, *_args):
        if not self._begin_library_write():
            return
        full, partial = self._plan_missing()
        if not full and not partial:
            return
        self._open_remove((full, partial), recheck=True)

    def _remove_whole(self, group):
        if not self._begin_library_write():
            return
        self.checked_missing.update(work["id"] for work in group["works"])
        self._sync_missing()
        self._open_remove(self._plan_missing(), recheck=True)

    def _remove_one(self, work):
        if not self._begin_library_write():
            return
        self._open_remove(([], [work]), recheck=True)

    def _open_remove(self, plan, preview=False, recheck=False):
        full, partial = plan
        folders = []
        for group in full:
            if group["group"] not in folders:
                folders.append(group["group"])
        for work in partial:
            if work["folder"] not in folders:
                folders.append(work["folder"])
        count = sum(len(group["works"]) for group in full) + len(partial)
        scan = "\n".join([f"跳过 {group['group']} 整个文件夹" for group in full] + ["其余移除的路径各自跳过"])
        lib = self.owner.lib
        backup = f"{lib.path}.tidy-<时间>.bak" if lib else ""
        dialog = RemoveDialog(self.owner, count, folders[0] if len(folders) == 1 else "", scan, backup)
        if preview or self.owner.screenshot_mode:
            self._show_preview(dialog)
            return
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._execute_remove(full, partial, recheck=recheck)

    def _show_preview(self, dialog):
        veil = QWidget(self.owner)
        _self_style(veil, "background:rgba(20,16,30,110); border:none;")
        veil.setGeometry(self.owner.rect())
        veil.show()
        dialog.setWindowFlags(Qt.WindowType.Widget | Qt.WindowType.FramelessWindowHint)
        dialog.setParent(self.owner)
        dialog.resize(680, 400)
        dialog.move(max(0, (self.owner.width() - dialog.width()) // 2),
                    max(0, (self.owner.height() - dialog.height()) // 2))
        dialog.show()
        dialog.raise_()
        self._preview = [veil, dialog]

    def _execute_remove(self, full, partial, *, recheck=False):
        lib = self.owner.lib
        groups = [(group["group"], [work["id"] for work in group["works"]]) for group in full]
        leftovers = {work["id"]: work["revision"] for work in partial}
        def task(cancel):
            removed = 0
            still = []
            removed_ids = []
            notes = []
            try:
                for directory, ids in groups:
                    try:
                        result = tidy.remove_missing_group(lib, directory)
                    except ValueError as exc:
                        if str(exc).startswith("drive offline"):
                            notes.append(_offline_text(exc))
                            continue
                        if removed_ids:
                            notes.append(_stop_text(exc))
                            break
                        raise
                    except Exception as exc:
                        if removed_ids:
                            notes.append(_stop_text(exc))
                            break
                        raise
                    removed += result["removed"]
                    still.extend(result["still_present"])
                    if result["removed"]:
                        gone = [wid for wid in ids if wid not in set(result["still_present"])]
                        removed_ids.extend(gone)
                else:
                    pending = dict(leftovers)
                    if recheck and pending:
                        for wid in _copies_present(lib, list(pending)):
                            pending.pop(wid, None)
                            still.append(wid)
                    if pending:
                        try:
                            result = tidy.remove_works(lib, pending)
                        except Exception as exc:
                            if removed_ids:
                                notes.append(_stop_text(exc))
                            else:
                                raise
                        else:
                            removed += result["removed"]
                            removed_ids.extend(pending)
            finally:
                if removed_ids:
                    tidy.DismissStore(lib.path, lib.uuid).drop_works(removed_ids)
                    BT.QueueStore(lib.path, lib.uuid).drop(removed_ids)
            summary = f"移除了 {removed} 部"
            if still:
                summary += f"；{len(still)} 部文件又出现了，没有移除"
            message = " ".join(notes + [summary]) if notes else summary
            return message, load_snapshot(lib)
        self._run("移除", task)

    def _build_junk(self, lay):
        lay.addLayout(self._heading("疑似不是游戏", "NOT A GAME", "文件名像工具、体积很小，或文件夹里没有可执行文件。"))
        inner = QWidget()
        rows = QVBoxLayout(inner)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(4)
        self._junk_boxes = {}
        writable = self._can_write()
        for row in self.snap["junk"]:
            line = QHBoxLayout()
            box = QCheckBox()
            self._junk_boxes[row["id"]] = box
            box.setChecked(row["id"] in self.checked_junk)
            box.toggled.connect(lambda on, wid=row["id"]: self._toggle_junk(wid, on))
            title = _Ellipsis(row["title"], "body", 13)
            _plain(title)
            line.addWidget(box)
            line.addWidget(title)
            for reason in row["reasons"]:
                line.addWidget(self._pill(JUNK_REASON.get(reason, reason), "gray"))
            path = _Ellipsis(row["path"], "mono", 11, mode=Qt.TextElideMode.ElideMiddle, tip=True)
            _self_style(path, f"color:{self.owner.theme.roles['sub']}; background:transparent; border:none;")
            line.addWidget(path, 1)
            line.addWidget(self._link("移除", lambda item=row: self._remove_junk_one(item), writable))
            line.addWidget(self._link("隐藏", lambda item=row: self._hide_one(item), writable))
            line.addWidget(self._link("是游戏", lambda item=row: self._dismiss("junk", item["id"]), writable,
                                      self.owner.theme.roles["wakaba"]))
            rows.addLayout(line)
        if not self.snap["junk"]:
            rows.addWidget(self._hint("没有要看的条目。"))
        rows.addStretch()
        lay.addWidget(self._scroll(inner, sideways=False), 1)
        bar, bar_lay = self._bar()
        self.junk_label = self._bar_text("")
        bar_lay.addWidget(self.junk_label, 1)
        self.hide_button = Button(self.owner, "隐藏勾选的", self._hide_checked)
        self.hide_button.setMinimumWidth(110)
        self.junk_remove = Button(self.owner, "移除勾选的 0 部", self._remove_junk, primary=True, subtitle="REMOVE")
        self.junk_remove.setMinimumWidth(180)
        bar_lay.addWidget(self.hide_button)
        bar_lay.addWidget(self.junk_remove)
        lay.addWidget(bar)
        self._update_junk_bar()

    def _toggle_junk(self, wid, on):
        if on:
            self.checked_junk.add(wid)
        else:
            self.checked_junk.discard(wid)
        self._update_junk_bar()

    def _update_junk_bar(self):
        n = len(self.checked_junk)
        self.junk_label.setText(f"已勾选 <b style='color:#F3B6C3'>{n}</b> 部")
        writable = self._can_write()
        self.hide_button.setEnabled(n > 0 and writable)
        self.junk_remove.setText(f"移除勾选的 {n} 部")
        self.junk_remove.setEnabled(n > 0 and writable)

    def _junk_selected(self):
        return [row for row in self.snap["junk"] if row["id"] in self.checked_junk]

    def _remove_junk(self, *_args):
        if not self._begin_library_write():
            return
        rows = self._junk_selected()
        if rows:
            self._open_remove(([], rows))

    def _remove_junk_one(self, row):
        if not self._begin_library_write():
            return
        self._open_remove(([], [row]))

    def _hide_one(self, row):
        self._hide_ids([row])

    def _hide_checked(self, *_args):
        self._hide_ids(self._junk_selected())

    def _hide_ids(self, rows):
        if not rows or not self._begin_library_write():
            return
        lib = self.owner.lib
        wanted = [(row["id"], row["revision"]) for row in rows]
        def task(cancel):
            if _someone_playing(lib):
                raise SessionActive(PLAYING)
            hidden = 0
            note = None
            try:
                for wid, revision in wanted:
                    repo.edit_work(lib, wid, revision, hidden=True)
                    hidden += 1
            except Exception as exc:
                if not hidden:
                    raise
                note = _stop_text(exc)
            message = "已隐藏" if note is None else f"{note} 已隐藏 {hidden} 部"
            return message, load_snapshot(lib)
        self._run("隐藏", task)

    def _dismiss(self, kind, key):
        if not self._can_write() or self.owner.lib is None:
            return
        store = tidy.DismissStore(self.owner.lib.path, self.owner.lib.uuid)
        store.add(kind, key)
        self.snap["dismiss"] = store.load()
        if kind == "junk":
            self.snap["junk"] = [row for row in self.snap["junk"] if row["id"] != key]
            self.checked_junk.discard(key)
        elif kind == "titles":
            self.snap["titles"] = [row for row in self.snap["titles"] if row["work_id"] != key]
            self.checked_titles.discard(key)
        elif kind == "duplicates":
            gone = tuple(sorted(key))
            self.snap["dups"] = [group for group in self.snap["dups"]
                                 if tuple(sorted(group["work_ids"])) != gone]
        self.rebuild()

    def _build_dup(self, lay):
        lay.addLayout(self._heading("疑似重复", "DUPLICATES", "书名规范化后相同，或压缩包和文件夹成对的作品。选一部当主作品，其余并进来。"))
        groups = self.snap["dups"]
        keys = [tuple(sorted(group["work_ids"])) for group in groups]
        if self.dup_key not in keys:
            self.dup_key = keys[0] if keys else None
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        for group in groups:
            key = tuple(sorted(group["work_ids"]))
            row = QWidget()
            row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            on = key == self.dup_key
            _self_style(row, f"background:{self.owner.theme.roles['card'] if on else 'transparent'};"
                        f"border-radius:10px; border:1px solid "
                        f"{self.owner.theme.roles['sakura'] if on else 'transparent'};")
            line = QHBoxLayout(row)
            line.setContentsMargins(10, 6, 10, 6)
            title = " / ".join(group["titles"])
            label = self._link(_elide(title, 520, "body", 13), lambda k=key: self._select_dup(k),
                               color=self.owner.theme.roles["ink"])
            label.setFont(font("body", 13))
            line.addWidget(label, 1)
            if len(group["works"]) > 2:
                line.addWidget(self._pill(f"{len(group['works'])} 部", "gray"))
            line.addWidget(self._pill(DUP_REASON.get(group["reason"], group["reason"]),
                                      "yellow" if group["reason"] == "archive_and_folder" else "gray"))
            line.addWidget(self._link("比较", lambda k=key: self._select_dup(k)))
            line.addWidget(self._link("不是重复", lambda ids=key: self._dismiss("duplicates", ids),
                                      self._can_write(), self.owner.theme.roles["wakaba"]))
            col.addWidget(row)
        group = next((item for item in groups if tuple(sorted(item["work_ids"])) == self.dup_key), None)
        if group:
            keep = self._keep_of(group)
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setSpacing(12)
            for index, work in enumerate(sorted(group["works"], key=lambda item: item["id"])):
                grid.addWidget(self._dup_card(group, work, work["id"] == keep), index // 2, index % 2)
            col.addWidget(grid_host)
            note = QLabel(_merge_note(group["works"], keep))
            note.setWordWrap(True)
            note.setFont(font("body", 12))
            _self_style(note, f"color:{self.owner.theme.roles['sub']}; background:transparent; "
                        f"border:1px dashed {self.owner.theme.roles['sakura']}; border-radius:12px; padding:8px;")
            col.addWidget(note)
        elif not groups:
            col.addWidget(self._hint("没有疑似重复。"))
        col.addStretch()
        lay.addWidget(self._scroll(inner), 1)
        bar, bar_lay = self._bar()
        self.dup_label = self._bar_text(self._dup_bar_text(group) if group else "")
        bar_lay.addWidget(self.dup_label, 1)
        rotate = Button(self.owner, "换主作品", self._rotate_keep)
        rotate.setMinimumWidth(110)
        rotate.setEnabled(bool(group))
        self.merge_button = Button(self.owner, "备份并合并", self._merge, primary=True, subtitle="MERGE")
        self.merge_button.setMinimumWidth(150)
        self.merge_button.setEnabled(bool(group) and self._can_write())
        bar_lay.addWidget(rotate)
        bar_lay.addWidget(self.merge_button)
        lay.addWidget(bar)

    def _select_dup(self, key):
        self.dup_key = key
        self.rebuild()

    def _keep_of(self, group):
        key = tuple(sorted(group["work_ids"]))
        ids = {work["id"] for work in group["works"]}
        if self.keeps.get(key) not in ids:
            self.keeps[key] = _default_keep(group["works"])
        return self.keeps[key]

    def _dup_bar_text(self, group):
        if group is None:
            return ""
        keep = self._keep_of(group)
        others = sorted(work["id"] for work in group["works"] if work["id"] != keep)
        left = "、".join(f"No.{wid:03d}" for wid in others)
        return f"把 <b style='color:#F3B6C3'>{left}</b> 并进 <b style='color:#F3B6C3'>No.{keep:03d}</b>"

    def _rotate_keep(self, *_args):
        group = self._current_dup()
        if group is None:
            return
        ids = sorted(work["id"] for work in group["works"])
        keep = self._keep_of(group)
        self.keeps[tuple(ids)] = ids[(ids.index(keep) + 1) % len(ids)]
        self.rebuild()

    def _current_dup(self):
        if not self.snap:
            return None
        return next((group for group in self.snap["dups"]
                     if tuple(sorted(group["work_ids"])) == self.dup_key), None)

    def _dup_card(self, group, work, keep):
        card = DupCard(lambda g=group, wid=work["id"]: self._choose_keep(g, wid))
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        border = self.owner.theme.roles["beni"] if keep else self.owner.theme.roles["line"]
        _self_style(card, f"background:{self.owner.theme.roles['card']}; border:1.5px solid {border}; border-radius:14px;")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        cover = QLabel()
        _plain(cover)
        cover.setFixedSize(78, 112)
        pix = QPixmap(work["cover"]) if work.get("cover") else asset(f"jacket/jacket-{work['id'] % 8 + 1:02d}.jpg")
        if not pix.isNull():
            cover.setPixmap(pix.scaled(78, 112, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation))
        cover.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(cover)
        text = QVBoxLayout()
        title = QLabel(html.escape(work["title"]))
        _plain(title)
        title.setFont(font("title", 15, True))
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text.addWidget(title)
        facts = QLabel(self._dup_facts(work))
        _plain(facts)
        facts.setTextFormat(Qt.TextFormat.RichText)
        facts.setFont(font("body", 12))
        facts.setWordWrap(True)
        facts.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text.addWidget(facts)
        lay.addLayout(text, 1)
        role = QLabel("主作品" if keep else "并进主作品")
        role.setFont(font("body", 11, True))
        bg = self.owner.theme.roles["beni"] if keep else self.owner.theme.roles["line"]
        fg = "#ffffff" if keep else self.owner.theme.roles["sub"]
        _self_style(role, f"background:{bg}; color:{fg}; border:none; border-radius:8px; padding:2px 8px;")
        role.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(role, 0, Qt.AlignmentFlag.AlignTop)
        return card

    def _dup_facts(self, work):
        sub = self.owner.theme.roles["sub"]
        copies = "<br>".join(html.escape(cp["path"]) + ("（找不到）" if cp["availability"] == "missing" else "")
                             for cp in work["copies"]) or "—"
        bits = []
        for site in R.SITES:
            if site in work["links"]:
                bits.append(f"{R.SITE_LABEL[site]} 已绑")
        if (work["summary"] or "").strip():
            bits.append("有简介")
        if work["tags"]:
            bits.append(f"标签 {work['tags']} 个")
        if work["legacy"]:
            bits.append("有旧资料")
        meta = " · ".join(bits) if bits else "—"
        developer = html.escape((work["developer"] or "").strip() or "—")
        rows = (("编号", f"No.{work['id']:03d}"), ("社团", developer), ("副本", copies),
                ("游玩", play.format_duration(work["seconds"])), ("资料", html.escape(meta)))
        return "".join(f"<span style='color:{sub}'>{name}</span>　{value}<br>" for name, value in rows)

    def _choose_keep(self, group, wid):
        self.keeps[tuple(sorted(group["work_ids"]))] = wid
        self.dup_key = tuple(sorted(group["work_ids"]))
        self.rebuild()

    def _merge(self, *_args):
        if not self._begin_library_write():
            return
        group = self._current_dup()
        if group is None:
            return
        keep = self._keep_of(group)
        keep_row = next(work for work in group["works"] if work["id"] == keep)
        others = {work["id"]: work["revision"] for work in group["works"] if work["id"] != keep}
        others = {wid: others[wid] for wid in sorted(others)}
        lib = self.owner.lib
        title = keep_row["title"]
        def task(cancel):
            tidy.merge_works(lib, keep, keep_row["revision"], others)
            tidy.DismissStore(lib.path, lib.uuid).drop_works(list(others))
            BT.QueueStore(lib.path, lib.uuid).drop(list(others))
            return f"合并好了：{title}", load_snapshot(lib)
        self._run("合并", task)

    def _build_split(self, lay):
        row = QHBoxLayout()
        row.setSpacing(16)
        left, right = QWidget(), QWidget()
        left_lay, right_lay = QVBoxLayout(left), QVBoxLayout(right)
        for host in (left_lay, right_lay):
            host.setContentsMargins(0, 0, 0, 0)
            host.setSpacing(6)
        self._build_titles(left_lay)
        self._build_legacy(right_lay)
        row.addWidget(left, 5)
        row.addWidget(right, 4)
        lay.addLayout(row, 1)

    def _chips(self, names, current, choose, counts):
        row = QHBoxLayout()
        row.setSpacing(6)
        for name in names:
            button = Button(self.owner, f"{name} {counts[name]}", lambda checked=False, n=name: choose(n))
            button.choice = True
            button.selected = name == current
            button.setMinimumWidth(108)
            row.addWidget(button)
        row.addStretch()
        return row

    def _title_edit(self, text):
        # 全局样式 padding:7px、圆角 12px，12px 的字在框里只剩上半截。按当前字体把行和框一起撑开。
        edit = QLineEdit(text)
        edit.setFont(font("body", 12))
        metrics = QFontMetrics(edit.font())
        sample = metrics.boundingRect("VIRTUESaegis国")
        glyph = max(metrics.height(), metrics.lineSpacing(), sample.height())
        edit.setMinimumWidth(72)
        edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        edit.setFixedHeight(glyph + 20)
        roles = self.owner.theme.roles
        edit.setStyleSheet(
            "QLineEdit {"
            f"background:{roles['card']}; color:{roles['ink']};"
            f"border:1px solid {roles['sakura']}; border-radius:6px; padding:4px 8px;"
            "}")
        return edit

    def _build_titles(self, lay):
        lay.addLayout(self._heading("标题", "TITLES", "按文件夹名、旧库原名给的建议。可以直接改右边的字再应用。"))
        rows = self.snap["titles"]
        counts = {"全部": len(rows)}
        for name, reason in (("去版本号", "cleaned"), ("旧库原名", "legacy_original"), ("上层文件夹", "parent_folder")):
            counts[name] = sum(row["reason"] == reason for row in rows)
        lay.addLayout(self._chips(TITLE_FILTERS, self.title_filter, self._set_title_filter, counts))
        reason = {"去版本号": "cleaned", "旧库原名": "legacy_original", "上层文件夹": "parent_folder"}.get(self.title_filter)
        shown = [row for row in rows if reason is None or row["reason"] == reason]
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        writable = self._can_write()
        for row in shown:
            line = QHBoxLayout()
            box = QCheckBox()
            box.setChecked(row["work_id"] in self.checked_titles)
            box.toggled.connect(lambda on, wid=row["work_id"]: self._toggle_title(wid, on))
            current = QLabel(_elide(row["current"], 180))
            current.setFont(font("body", 12))
            _self_style(current, f"color:{self.owner.theme.roles['sub']}; background:transparent; border:none;")
            arrow = QLabel("→")
            _self_style(arrow, f"color:{self.owner.theme.roles['beni']}; background:transparent; border:none;")
            edit = self._title_edit(self.title_edits.get(row["work_id"], row["suggested"]))
            edit.textChanged.connect(lambda text, wid=row["work_id"]: self.title_edits.__setitem__(wid, text))
            kind = TITLE_REASON.get(row["reason"], row["reason"])
            pill = "green" if row["reason"] == "legacy_original" else "yellow" if row["reason"] == "parent_folder" else "gray"
            line.addWidget(box)
            line.addWidget(current, 2)
            line.addWidget(arrow)
            line.addWidget(edit, 3)
            line.addWidget(self._pill(kind, pill))
            line.addWidget(self._link("不改", lambda wid=row["work_id"]: self._dismiss("titles", wid), writable))
            col.addLayout(line)
        if not shown:
            col.addWidget(self._hint("这一档没有建议。"))
        col.addStretch()
        lay.addWidget(self._scroll(inner, sideways=False), 1)
        lay.addWidget(self._hint(TITLE_HINT))
        bar, bar_lay = self._bar()
        self.title_label = self._bar_text("")
        bar_lay.addWidget(self.title_label, 1)
        self.title_button = Button(self.owner, "改 0 部的书名", self._retitle_checked, primary=True, subtitle="RETITLE")
        self.title_button.setMinimumWidth(170)
        bar_lay.addWidget(self.title_button)
        lay.addWidget(bar)
        self._update_title_bar()

    def _set_title_filter(self, name):
        if name not in TITLE_FILTERS:
            return
        self.title_filter = name
        self.rebuild()

    def _toggle_title(self, wid, on):
        if on:
            self.checked_titles.add(wid)
        else:
            self.checked_titles.discard(wid)
        self._update_title_bar()

    def _update_title_bar(self):
        n = len(self.checked_titles)
        if not hasattr(self, "title_label"):
            return
        self.title_label.setText(f"已勾选 <b style='color:#F3B6C3'>{n}</b> 条")
        self.title_button.setText(f"改 {n} 部的书名")
        self.title_button.setEnabled(n > 0 and self._can_write())

    def _retitle_checked(self, *_args):
        if not self._begin_library_write():
            return
        reason = {"去版本号": "cleaned", "旧库原名": "legacy_original", "上层文件夹": "parent_folder"}.get(self.title_filter)
        shown = [row["work_id"] for row in self.snap["titles"] if reason is None or row["reason"] == reason]
        for row in self.snap["titles"]:
            if row["work_id"] not in self.checked_titles:
                continue
            if not (self.title_edits.get(row["work_id"]) or "").strip():
                text = f"「{row['current']}」的新书名是空的"
                if row["work_id"] in shown:
                    text += f"（第 {shown.index(row['work_id']) + 1} 行书名是空的）"
                self._note(text)
                return
        payload = {row["work_id"]: (row["revision"], self.title_edits.get(row["work_id"], row["suggested"]).strip())
                   for row in self.snap["titles"] if row["work_id"] in self.checked_titles}
        if not payload:
            return
        lib = self.owner.lib
        self._retitle = True
        def task(cancel):
            if _someone_playing(lib):
                raise SessionActive(PLAYING)
            count = tidy.retitle(lib, payload)
            return f"改好了 {count} 部", load_snapshot(lib)
        self._run("改书名", task)

    def _legacy_rows(self):
        rows = self.snap["legacy"]
        if self.legacy_filter == "有 VNDB 号":
            return [row for row in rows if row.get("vndb")]
        if self.legacy_filter == "有简介":
            return [row for row in rows if row.get("summary")]
        return list(rows)

    def _queueable(self):
        return [row for row in self.snap["legacy"]
                if row.get("vndb") and not row.get("vndb_taken_by") and not row.get("has_vndb_link")]

    def _build_legacy(self, lay):
        lay.addLayout(self._heading("旧资料", "LEGACY", "旧版蔵書帖里存过、新库还没用上的资料"))
        rows = self.snap["legacy"]
        counts = {"全部": len(rows), "有 VNDB 号": sum(bool(row.get("vndb")) for row in rows),
                  "有简介": sum(bool(row.get("summary")) for row in rows)}
        lay.addLayout(self._chips(LEGACY_FILTERS, self.legacy_filter, self._set_legacy_filter, counts))
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        writable = self._can_write()
        for row in self._legacy_rows():
            col.addWidget(self._legacy_card(row, writable))
        if not self._legacy_rows():
            col.addWidget(self._hint("没有旧资料。"))
        col.addStretch()
        lay.addWidget(self._scroll(inner, sideways=False), 1)
        lay.addWidget(self._hint(LEGACY_HINT))
        bar, bar_lay = self._bar(AI2)
        n = len(self._queueable())
        bar_lay.addWidget(self._bar_text(f"有 VNDB 号的 <b style='color:#F3B6C3'>{n}</b> 部"), 1)
        button = Button(self.owner, "全部放进「待确认」", self._queue_legacy, primary=True, subtitle="QUEUE")
        button.setMinimumWidth(190)
        button.setEnabled(n > 0 and self._can_write())
        bar_lay.addWidget(button)
        lay.addWidget(bar)

    def _set_legacy_filter(self, name):
        if name not in LEGACY_FILTERS:
            return
        self.legacy_filter = name
        self.rebuild()

    def _legacy_card(self, row, writable):
        card = QWidget()
        card.setMinimumWidth(0)
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _self_style(card, f"background:{self.owner.theme.roles['card']}; border:1px solid "
                    f"{self.owner.theme.roles['line']}; border-radius:14px;")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        head = QHBoxLayout()
        title = _Ellipsis(row.get("title") or "", "body", 13, bold=True)
        _plain(title)
        head.addWidget(title)
        if row.get("vndb"):
            badge = QLabel("VNDB")
            badge.setFont(font("body", 10, True))
            _self_style(badge, "color:#ffffff; background:#3a6b45; border:none; border-radius:6px; padding:1px 6px;")
            head.addWidget(badge)
            number = QLabel(row["vndb"])
            _plain(number)
            number.setFont(font("mono", 11))
            head.addWidget(number)
        for key, value in (row.get("others") or {}).items():
            name = OTHER_SITE.get(str(key).casefold(), key)
            head.addWidget(self._pill(f"{name} {value}", "gray"))
        head.addStretch()
        if row.get("vndb") and not row.get("vndb_taken_by") and not row.get("has_vndb_link"):
            head.addWidget(self._link("按编号找资料", lambda item=row: self.owner.open_find(item["work_id"], query=item["vndb"])))
        elif row.get("vndb_taken_by"):
            head.addWidget(self._pill(f"{row['vndb']} 已绑在 No.{row['vndb_taken_by']:03d}", "yellow"))
        elif row.get("has_vndb_link"):
            head.addWidget(self._pill("已绑 VNDB", "green"))
        if row.get("summary") and row.get("summary_source") != "user":
            head.addWidget(self._link("用旧简介", lambda item=row: self._use_summary(item), writable,
                                      self.owner.theme.roles["wakaba"]))
        lay.addLayout(head)
        bits = []
        summary = tidy.legacy_summary_text(row["summary"]) if row.get("summary") else ""
        if summary:
            text = summary[:40]
            bits.append("简介「" + text + ("…" if len(summary) > 40 else "") + "」")
        if row.get("release_date"):
            bits.append(row["release_date"])
        if row.get("original_title"):
            bits.append(row["original_title"])
        if bits:
            meta = _Ellipsis(" · ".join(bits), "body", 11)
            _self_style(meta, f"color:{self.owner.theme.roles['sub']}; background:transparent; border:none;")
            lay.addWidget(meta)
        return card

    def _use_summary(self, row):
        if not self._begin_library_write():
            return
        lib = self.owner.lib
        wid = row["work_id"]
        def task(cancel):
            if _someone_playing(lib):
                raise SessionActive(PLAYING)
            ok = tidy.use_legacy_summary(lib, wid)
            return ok, load_snapshot(lib)
        def done(payload):
            ok, snap = payload
            self._apply(snap)
            if ok and self.owner.lib and not self.owner.busy:
                self.owner.reload_library(message="已用旧简介")
            elif not ok:
                self._note("这部已经有你自己写的简介，没有覆盖")
        self.owner.run_job("用旧简介", task, done, self._fail, cancellable=False, writes_library=True)

    def _queue_legacy(self, *_args):
        lib = self.owner.lib
        if lib is None or lib.readonly:
            return
        rows = self._queueable()
        if not rows:
            return
        ids = [row["work_id"] for row in rows]
        refs = {row["work_id"]: R.Ref("vndb", row["vndb"]) for row in rows}
        self.owner.start_batch(ids, refs)

    def _build_ignored(self, lay):
        lay.addLayout(self._heading("扫描跳过的路径", "IGNORED", "移除时记下的路径，和你自己加的。撤销之后，下次扫描会再看见。"))
        rows = self.snap["ignored"]
        if not rows:
            lay.addWidget(self._hint("还没有跳过的路径"), 1)
            return
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        head = QHBoxLayout()
        for name, stretch in (("路径", 4), ("范围", 1), ("来由", 1), ("加入时间", 2)):
            label = QLabel(name)
            label.setFont(font("body", 11))
            _self_style(label, f"color:{self.owner.theme.roles['sub']}; background:transparent; border:none;")
            head.addWidget(label, stretch)
        head.addSpacing(48)
        col.addLayout(head)
        writable = self._can_write()
        for row in rows:
            line = QHBoxLayout()
            path = QLabel(row["path"])
            _plain(path)
            path.setFont(font("mono", 11))
            path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            scope = QLabel(SCOPE_TEXT.get(row["scope"], row["scope"]))
            _plain(scope)
            reason = QLabel(IGNORE_REASON.get(row["reason"], row["reason"]))
            _plain(reason)
            when = QLabel(_local_minute(row.get("added_at")))
            _plain(when)
            when.setFont(font("body", 11))
            line.addWidget(path, 4)
            line.addWidget(scope, 1)
            line.addWidget(reason, 1)
            line.addWidget(when, 2)
            line.addWidget(self._link("撤销", lambda item=row: self._undo(item["path"]), writable))
            col.addLayout(line)
        col.addStretch()
        lay.addWidget(self._scroll(inner), 1)

    def _undo(self, path):
        if not self._begin_library_write():
            return
        lib = self.owner.lib
        def task(cancel):
            if _someone_playing(lib):
                raise SessionActive(PLAYING)
            tidy.remove_ignored(lib, path)
            return "已撤销", load_snapshot(lib)
        self._run("撤销跳过", task)
