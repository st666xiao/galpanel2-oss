"""Review page for a batch metadata search. The search thread never touches the library."""
import threading
import time
from math import ceil

from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtGui import QImage, QPalette, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QTableWidget,
    QTableWidgetItem, QHeaderView, QCheckBox)

from galpanel2.core import batch as BT, binding as B, records as R, repository as repo, sites as S
from galpanel2.core.domain import Cancelled, DuplicateLink, SiteError, WriteConflict, check_cancel, utc_now
from .theme import font
from .widgets import Button, Adv

FILTERS = {
    "全部": {"ok", "near", "none", "error"},
    "标题一致": {"ok"},
    "相近": {"near"},
    "没找到": {"none"},
    "出错": {"error"},
    "已跳过": {"skipped"},
}
MATCH = {"ok": "标题一致", "near": "相近", "none": "没找到", "error": "出错", "skipped": "已跳过"}
MATCH_COLOR = {"ok": ("#e3f1e6", "#3c7a50"), "near": ("#fbf0d8", "#a8741a"), "none": (None, None),
               "error": (None, None), "skipped": (None, None)}
SITE_COLOR = {"dlsite": "#1f5fa8", "vndb": "#3a6b45", "bangumi": "#e2557b",
              "steam": "#1b2838", "f95": "#2f6f4e"}
HINT = "「不是游戏」= 从书架隐藏，以后批量不再搜它（在「隐藏」里能找回）。「跳过」= 这次不处理，下次批量还会搜。确认前什么都不写进库。"
ADVISOR = "我把各个站都翻了一遍……标题对得上的放在前面了。乱七八糟的文件夹，点「不是游戏」我就不再管它。"


def _empty_snap():
    return {"version": 1, "state": "done", "todo": [], "items": [], "library_uuid": ""}


class BatchRunner(QThread):
    advanced = Signal(object)
    notice = Signal(object)

    def __init__(self, store, works, reach, parent=None):
        super().__init__(parent)
        self.store = store
        self.works = works
        self.reach = reach
        self._pause = threading.Event()
        self._cancel = threading.Event()

    def pause(self):
        self._pause.set()

    def cancel(self):
        self._cancel.set()

    def run(self):
        client = S.SiteClient(S.default_transport(), token=S.read_token(), cancel=self._cancel.is_set)
        while True:
            if self._cancel.is_set():
                self.store.set_state("cancelled")
                return
            if self._pause.is_set():
                self.store.set_state("paused")
                return
            wid = self.store.next_todo()
            if wid is None:
                self.store.set_state("done")
                return
            info = self.works.get(wid) or {"title": "", "paths": [], "path": ""}
            started = time.monotonic()
            try:
                extra = {}
                if info.get("ref") is not None:
                    extra["ref"] = info["ref"]
                item = BT.search_one(client, info["title"], info.get("paths") or [], sites=self.reach.sites(),
                                     **extra)
            except Cancelled:
                self.store.set_state("cancelled" if self._cancel.is_set() else "paused")
                return
            item.update(work_id=wid, title=info["title"], path=info.get("path") or "",
                        searched_at=utc_now(), note="", match_status=item["status"])
            self.store.put_item(item)
            newly = self.reach.record(item)
            self.advanced.emit((wid, time.monotonic() - started))
            if newly:
                self.notice.emit(list(newly))


def bind_item(lib, work_id: int, item: dict, cancel) -> tuple[str, str]:
    """Bind confirm_set candidates. One candidate failing leaves the others in place."""
    candidates = BT.confirm_set(item)
    if not candidates:
        return item.get("status") or "none", item.get("note") or ""
    client = S.SiteClient(S.default_transport(), token=S.read_token(), cancel=cancel)
    notes, success = [], 0
    revision = repo.get_work(lib, work_id).revision
    cover_done = False
    for cand in candidates:
        check_cancel(cancel)
        label = R.SITE_LABEL.get(cand.get("site"), "")
        try:
            record = BT.record_from_dict(cand["record"])
            if not record.complete:
                record = client.fetch(R.Ref(record.site, record.site_id))
            plan = B.plan_bind(lib, work_id, record)
            if plan.taken_by is not None:
                raise DuplicateLink("这个条目已经绑在别的作品上", work_id=plan.taken_by)
            cover = None
            if plan.cover == "set" and not cover_done:
                cover_done = True
                if record.cover_url:
                    cover, cover_error = B.download_cover(client, record)
                    if cover_error:
                        notes.append(f"{label} 封面：{cover_error}")
            revision = B.apply_bind(lib, work_id, revision, record, None, cover)
            success += 1
        except Cancelled:
            raise
        except (DuplicateLink, SiteError, WriteConflict, ValueError, OSError) as error:
            notes.append(f"{label} {error}".strip())
    note = "；".join(x for x in notes if x)
    if success:
        return "bound", note
    return "error", note or "没有绑上"


class ReviewPage(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.setAutoFillBackground(True)
        self.owner = owner
        self.store = None
        self.reach = BT.Reachability()
        self.snap = _empty_snap()
        self.chip = "全部"
        self.checked = set()
        self.durations = []
        self.images = {}
        self._loading = set()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 14, 22, 8)
        lay.setSpacing(8)
        head = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setFont(font("title", 22, True))
        self.title_label.setTextFormat(Qt.TextFormat.RichText)
        head.addWidget(self.title_label)
        self.progress_label = QLabel()
        self.progress_label.setFont(font("body", 12))
        head.addWidget(self.progress_label)
        head.addStretch()
        self.pause_button = Button(owner, "暂停", self._toggle_run)
        self.cancel_button = Button(owner, "取消", self.cancel)
        self.back_button = Button(owner, "返回书架", owner.show_library)
        for button in (self.pause_button, self.cancel_button, self.back_button):
            button.setMinimumWidth(88)
            head.addWidget(button)
        lay.addLayout(head)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        lay.addWidget(self.bar)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setFont(font("body", 11))
        self.notice.hide()
        lay.addWidget(self.notice)
        chips = QHBoxLayout()
        self.chips = {}
        for name in FILTERS:
            button = Button(owner, name, lambda checked=False, n=name: self.set_chip(n))
            button.choice = True
            button.setMinimumWidth(108)
            self.chips[name] = button
            chips.addWidget(button)
        chips.addStretch()
        self.sure_button = Button(owner, "确认全部「标题一致且只有一个」", self.confirm_sure, primary=True, subtitle="0 部")
        self.sure_button.setMinimumWidth(280)
        chips.addWidget(self.sure_button)
        lay.addLayout(chips)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "你库里的作品", "", "最好的候选", "匹配", "处理"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for index, width in ((0, 36), (2, 58), (4, 110), (5, 230)):
            self.table.setColumnWidth(index, width)
        self.table.verticalHeader().setDefaultSectionSize(72)
        lay.addWidget(self.table, 1)
        self.hint = QLabel(HINT)
        self.hint.setWordWrap(True)
        self.hint.setFont(font("body", 11))
        lay.addWidget(self.hint)
        foot = QHBoxLayout()
        self.footer = QLabel()
        self.footer.setFont(font("body", 12))
        foot.addWidget(self.footer)
        foot.addStretch()
        self.checked_button = Button(owner, "确认选中的 0 部", self.confirm_checked)
        self.checked_button.hide()
        foot.addWidget(self.checked_button)
        lay.addLayout(foot)
        self.adv = Adv(owner, ADVISOR, pose="loupe", height=168)
        self.adv.setVisible(owner.show_mascot)
        lay.addWidget(self.adv)

    def bind_store(self):
        lib = self.owner.lib
        if lib is None:
            return
        runner = self.owner.batch_runner
        store = getattr(runner, "store", None) if runner is not None else None
        if store is not None and store.lib_path == lib.path and store.library_uuid == lib.uuid:
            self.store = runner.store
            return
        if self.store is not None and self.store.lib_path == lib.path and self.store.library_uuid == lib.uuid:
            return
        self.store = BT.QueueStore(lib.path, lib.uuid)

    def clear_library_queue(self):
        self.store = None
        self.snap = _empty_snap()
        self.checked.clear()
        self.refresh()

    def attach(self, runner):
        self.reach = runner.reach
        self.durations = []
        self._show_notice()

    def refresh(self):
        if self.store is not None:
            self.snap = self.store.snapshot()
        self._show_notice()
        self._update_chrome()
        self._fill()

    def items(self) -> list[dict]:
        allowed = FILTERS.get(self.chip, FILTERS["全部"])
        return [item for item in self.snap.get("items") or [] if item.get("status") in allowed]

    def set_chip(self, name):
        if name not in FILTERS:
            return
        self.chip = name
        self._update_chrome()
        self._fill()

    def progress_text(self) -> str:
        done = len(self.snap.get("items") or [])
        total = done + len(self.snap.get("todo") or [])
        remaining = total - done
        if done == 0:
            eta = "正在估算"
        elif remaining == 0:
            eta = "约 0 分钟"
        else:
            avg = self._average()
            eta = "正在估算" if avg is None else f"约 {ceil(remaining * avg / 60)} 分钟"
        return f"后台在搜：{done} / {total} 部　·　{eta}"

    def footer_text(self) -> str:
        items = self.snap.get("items") or []
        bound = sum(item["status"] == "bound" for item in items)
        hidden = sum(item["status"] == "hidden" for item in items)
        pending = sum(item["status"] in FILTERS["全部"] for item in items)
        return f"已绑定 {bound} · 待确认 {pending} · 不是游戏 {hidden}"

    def notice_text(self) -> str:
        return BT.skipped_notice(self.reach.skipped)

    def confirm(self, work_id):
        self._confirm([work_id], "绑定资料", bulk=False)

    def confirm_sure(self):
        ids = [item["work_id"] for item in self.snap.get("items") or [] if BT.is_sure(item)]
        self._confirm(ids, "绑定资料", bulk=True)

    def confirm_checked(self):
        visible = {item["work_id"] for item in self.items()}
        ids = [item["work_id"] for item in self.items() if item["work_id"] in self.checked and item["work_id"] in visible]
        self._confirm(ids, "绑定资料", bulk=True)

    def not_game(self, work_id):
        lib = self.owner.lib
        store = self.store
        def task(cancel):
            try:
                revision = repo.get_work(lib, work_id).revision
            except KeyError:
                if store is not None:
                    store.drop([work_id])
                return "gone"
            repo.edit_work(lib, work_id, revision, hidden=True)
            if store is not None:
                store.set_status(work_id, "hidden", self._note(work_id))
            return "hidden"
        def done(result):
            if result == "gone":
                self.refresh()
                return
            self._reloaded()
        self.owner.run_job("不是游戏", task, done, self._failed, cancellable=False, writes_library=True)

    def skip(self, work_id):
        if self.store is None:
            return
        self.store.set_status(work_id, "skipped", self._note(work_id))
        self.refresh()

    def restore(self, work_id):
        item = self._item(work_id)
        if item is None or self.store is None:
            return
        self.store.set_status(work_id, item.get("match_status") or "none", item.get("note") or "")
        self.refresh()

    def pause(self):
        runner = self.owner.batch_runner
        if runner is not None and runner.isRunning():
            runner.pause()

    def resume(self):
        owner = self.owner
        if not owner.lib or owner.lib.readonly or owner.busy:
            return
        if owner.batch_runner is not None and owner.batch_runner.isRunning():
            return
        self.bind_store()
        data = self.store.load() if self.store is not None else None
        if not data or not data.get("todo"):
            return
        owner._launch_runner(self.store)

    def cancel(self):
        runner = self.owner.batch_runner
        if runner is not None and runner.isRunning():
            runner.cancel()

    def note_bound(self, work_id):
        self.bind_store()
        item = None if self.store is None else next((x for x in (self.store.snapshot().get("items") or [])
                                                      if x.get("work_id") == work_id), None)
        if item is None or item.get("status") in ("bound", "hidden"):
            return
        self.store.set_status(work_id, "bound", item.get("note") or "")
        self.refresh()

    def on_advanced(self, payload):
        _wid, seconds = payload
        if seconds >= 0:
            self.durations.append(seconds)
        self.refresh()

    def on_notice(self, _newly):
        self._show_notice()

    def _toggle_run(self):
        runner = self.owner.batch_runner
        if runner is not None and runner.isRunning():
            self.pause()
        else:
            self.resume()

    def _confirm(self, work_ids, label, bulk):
        if not work_ids or not self.owner.lib or self.owner.busy:
            if bulk:
                self.owner.status.idle("绑定了 0 部，0 部没成功（原因写在各行里）")
            return
        lib, store = self.owner.lib, self.store
        wanted = list(work_ids)
        def task(cancel):
            ok = failed = gone = 0
            snap = store.snapshot() if store is not None else {"items": []}
            by_id = {item["work_id"]: item for item in snap.get("items") or []}
            for wid in wanted:
                check_cancel(cancel)
                item = by_id.get(wid)
                if item is None:
                    failed += 1
                    continue
                try:
                    repo.get_work(lib, wid)
                except KeyError:
                    gone += 1
                    if store is not None:
                        store.drop([wid])
                    continue
                status, note = bind_item(lib, wid, item, cancel)
                if store is not None and status != item.get("status"):
                    store.set_status(wid, status, note)
                elif store is not None and note != (item.get("note") or ""):
                    store.set_status(wid, status, note)
                if status == "bound":
                    ok += 1
                elif status == "error" and item.get("status") != "error":
                    failed += 1
                elif status == "error":
                    failed += 1
            return ok, failed, gone
        def done(result):
            ok, failed, gone = result
            if bulk and gone:
                message = f"绑定了 {ok} 部，{failed} 部没成功（原因写在各行里），{gone} 部已经不在书架上"
            elif bulk:
                message = f"绑定了 {ok} 部，{failed} 部没成功（原因写在各行里）"
            else:
                message = None
            self.checked.clear()
            self.owner.reload_library(message=message)
            self.refresh()
        self.owner.run_job(label, task, done, self._failed, cancellable=False, writes_library=True)

    def _reloaded(self):
        self.refresh()
        self.owner.reload_library()

    def _failed(self, error):
        self.refresh()
        if not isinstance(error, Cancelled):
            self.owner.notify(str(error))

    def _item(self, work_id):
        return next((item for item in self.snap.get("items") or [] if item.get("work_id") == work_id), None)

    def _note(self, work_id):
        item = self._item(work_id)
        return (item or {}).get("note") or ""

    def _average(self):
        if self.durations:
            return sum(self.durations) / len(self.durations)
        return None

    def _show_notice(self):
        text = self.notice_text()
        color = self.owner.theme.roles["sub"]
        paper = self.owner.theme.roles["paper"]
        ink = self.owner.theme.roles["ink"]
        line = self.owner.theme.roles["line"]
        page = self.palette()
        page.setColor(QPalette.ColorRole.Window, self.owner.theme.color("paper"))
        self.setPalette(page)
        self.setAutoFillBackground(True)
        self.title_label.setText(
            f"<span style='color:{ink}'>待确认</span>  "
            "<span style='color:#D4506C;font-size:9px;letter-spacing:3px'>REVIEW MATCHES</span>")
        for label, role in ((self.title_label, ink), (self.progress_label, color),
                            (self.notice, color), (self.hint, color), (self.footer, ink)):
            label.setStyleSheet(f"background:{paper};color:{role}")
        self.table.setStyleSheet(
            f"QTableWidget {{ background:{paper}; color:{ink}; border:0; }}"
            f"QTableWidget::item {{ background:{paper}; color:{ink}; border-bottom:1px solid {line}; }}")
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
        self.adv.setVisible(self.owner.show_mascot)

    def _update_chrome(self):
        for name, button in self.chips.items():
            button.setText(f"{name} {self._count(name)}")
            button.selected = name == self.chip
            button.update()
        sure = sum(1 for item in self.snap.get("items") or [] if BT.is_sure(item))
        self.sure_button.subtitle = f"{sure} 部"
        self.sure_button.setEnabled(sure > 0 and not (self.owner.lib and self.owner.lib.readonly))
        self.sure_button.update()
        self.progress_label.setText(self.progress_text())
        done = len(self.snap.get("items") or [])
        total = done + len(self.snap.get("todo") or [])
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(done)
        accent, track = self.owner.theme.roles["beni"], self.owner.theme.roles["line"]
        self.bar.setStyleSheet(
            f"QProgressBar {{background:{track};border:0;border-radius:4px;}}"
            f"QProgressBar::chunk {{background:{accent};border-radius:4px;}}")
        self.footer.setText(self.footer_text())
        running = self.owner.batch_runner is not None and self.owner.batch_runner.isRunning()
        paused = self.snap.get("state") == "paused" and bool(self.snap.get("todo"))
        self.pause_button.setText("暂停" if running else "继续")
        self.pause_button.setEnabled(running or paused)
        self.cancel_button.setEnabled(running)
        self._update_checked_button()

    def _count(self, name):
        allowed = FILTERS[name]
        return sum(item.get("status") in allowed for item in self.snap.get("items") or [])

    def _update_checked_button(self):
        shown = {item["work_id"] for item in self.items()}
        n = sum(1 for wid in self.checked if wid in shown)
        self.checked_button.setText(f"确认选中的 {n} 部")
        self.checked_button.setVisible(n > 0)

    def _fill(self):
        rows = self.items()
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            self._fill_row(row, item)
        self._update_checked_button()

    def _fill_row(self, row, item):
        wid = item["work_id"]
        ink = self.owner.theme.roles["ink"]
        box = QCheckBox()
        box.setChecked(wid in self.checked)
        box.toggled.connect(lambda on, work_id=wid: self._toggle(work_id, on))
        self._clear(box)
        hold = QWidget()
        self._clear(hold)
        lay = QHBoxLayout(hold)
        lay.setContentsMargins(8, 0, 0, 0)
        lay.addWidget(box)
        self.table.setCellWidget(row, 0, hold)
        work = QLabel(
            f"<span style='color:{ink}'>{item.get('title') or ''}</span><br>"
            f"<span style='color:{self.owner.theme.roles['sub']};font-size:10px'>{item.get('path') or ''}</span>")
        work.setTextFormat(Qt.TextFormat.RichText)
        work.setFont(font("body", 12))
        self._clear(work)
        self.table.setCellWidget(row, 1, work)
        self.table.setCellWidget(row, 2, self._thumb(item))
        self.table.setCellWidget(row, 3, self._candidate(item))
        self.table.setCellWidget(row, 4, self._match(item))
        self.table.setCellWidget(row, 5, self._actions(item))
        self.table.setItem(row, 0, QTableWidgetItem())

    def _toggle(self, work_id, on):
        if on:
            self.checked.add(work_id)
        else:
            self.checked.discard(work_id)
        self._update_checked_button()

    def _thumb(self, item):
        label = QLabel()
        label.setFixedSize(QSize(40, 54))
        self._clear(label)
        url = _cover(item)
        pix = self.images.get(url) if url else None
        if pix is not None:
            label.setPixmap(pix.scaled(40, 54, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation))
        elif url:
            self._fetch_thumb(url)
        return label

    def _fetch_thumb(self, url):
        if url in self.images or url in self._loading or self.owner.closing:
            return
        self._loading.add(url)
        def done(raw):
            self._loading.discard(url)
            image = QImage.fromData(raw)
            if not image.isNull():
                self.images[url] = QPixmap.fromImage(image)
                self._fill()
        def failed(_error):
            self._loading.discard(url)
        try:
            self.owner.run_net("下载缩略图", lambda cancel: S.SiteClient(
                S.default_transport(), token=S.read_token(), cancel=cancel).image(url), done, failed)
        except ValueError:
            self._loading.discard(url)

    def _clear(self, widget):
        color = self.owner.theme.color("paper")
        palette = widget.palette()
        palette.setColor(QPalette.ColorRole.Window, color)
        palette.setColor(QPalette.ColorRole.Base, color)
        widget.setPalette(palette)
        widget.setAutoFillBackground(True)
        widget.setStyleSheet(f"background:{self.owner.theme.roles['paper']};")

    def _candidate(self, item):
        label = QLabel()
        label.setFont(font("body", 12))
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        self._clear(label)
        label.setStyleSheet(
            f"background:{self.owner.theme.roles['paper']};color:{self.owner.theme.roles['ink']}")
        cands = item.get("candidates") or []
        status = item.get("status")
        if status in ("none", "error") or not cands or cands[0].get("grade") == "no":
            if status == "error":
                errs = item.get("errors") or {}
                text = "　".join(f"{R.SITE_LABEL.get(site, site)}：{msg}" for site, msg in errs.items()) or "出错"
            else:
                text = "所有站都没找到"
            label.setText(f"<span style='color:{self.owner.theme.roles['sub']}'>{text}</span>")
            return label
        best = cands[0]
        record = best.get("record") or {}
        brand = ((record.get("brands") or [{}])[0].get("name")) if record.get("brands") else ""
        year = (record.get("release_date") or "")[:4]
        tail = " · ".join(part for part in (brand, year or record.get("extra") or "") if part)
        more = f"　另有 {len(cands) - 1} 个候选" if len(cands) > 1 else ""
        color = SITE_COLOR.get(best.get("site"), "#857A86")
        name = R.SITE_LABEL.get(best.get("site"), "")
        badge = f"<span style='color:#ffffff;background-color:{color}'> {name} </span>"
        label.setText(f"{badge} {record.get('title') or ''}{'　' + tail if tail else ''}{more}")
        return label

    def _match(self, item):
        wrap = QWidget()
        self._clear(wrap)
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(4, 8, 4, 8)
        lay.setSpacing(2)
        status = item.get("status")
        text = MATCH.get(status, "")
        badge = QLabel(text)
        badge.setFont(font("body", 11, True))
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bg, fg = MATCH_COLOR.get(status, (None, None))
        if bg is None:
            bg, fg = self.owner.theme.roles["line"], self.owner.theme.roles["sub"]
        badge.setStyleSheet(f"background:{bg};color:{fg};border-radius:10px;padding:1px 8px")
        lay.addWidget(badge)
        skipped = [R.SITE_LABEL[site] for site in R.SITES if (item.get("errors") or {}).get(site) == BT.SKIPPED_TEXT]
        if skipped:
            small = QLabel(f"{'、'.join(skipped)} {BT.SKIPPED_TEXT}")
            small.setFont(font("body", 10))
            small.setStyleSheet(
                f"background:{self.owner.theme.roles['paper']};color:{self.owner.theme.roles['sub']}")
            small.setWordWrap(True)
            lay.addWidget(small)
        lay.addStretch()
        return wrap

    def _actions(self, item):
        status = item.get("status")
        wid = item["work_id"]
        if status in ("ok", "near"):
            specs = [("确认", lambda work_id=wid: self.confirm(work_id), "ai"),
                     ("换一个", lambda work_id=wid: self._preset(work_id), "ai"),
                     ("跳过", lambda work_id=wid: self.skip(work_id), "sub")]
        elif status == "skipped":
            specs = [("恢复", lambda work_id=wid: self.restore(work_id), "ai")]
        elif status in ("none", "error"):
            specs = [("手动找", lambda work_id=wid: self._preset(work_id), "ai"),
                     ("不是游戏", lambda work_id=wid: self.not_game(work_id), "beni"),
                     ("跳过", lambda work_id=wid: self.skip(work_id), "sub")]
        else:
            specs = []
        wrap = QWidget()
        self._clear(wrap)
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(8)
        for text, callback, role in specs:
            link = QLabel(f"<a href='x' style='color:{self.owner.theme.roles[role]};text-decoration:none'>{text}</a>")
            link.setFont(font("body", 12, True))
            link.setTextFormat(Qt.TextFormat.RichText)
            self._clear(link)
            link.linkActivated.connect(lambda _href, fn=callback: fn())
            lay.addWidget(link)
        lay.addStretch()
        return wrap

    def _preset(self, work_id):
        item = self._item(work_id)
        self.owner.open_find(work_id, [] if item is None else list(item.get("candidates") or []))


def _cover(item):
    cands = item.get("candidates") or []
    if not cands:
        return None
    return (cands[0].get("record") or {}).get("cover_url") or None
