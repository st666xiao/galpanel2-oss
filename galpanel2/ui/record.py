"""Record projection is read in one worker, then painted and edited via jobs."""
from datetime import datetime, timedelta
from PySide6.QtCore import Qt, QRectF, QDate
from PySide6.QtGui import QPainter, QPen, QLinearGradient, QBrush, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QMenu, QInputDialog, QMessageBox, QDialog, QFormLayout, QComboBox,
    QDateEdit, QSpinBox)

from galpanel2.core import play, repository as repo
from .theme import font, asset
from .widgets import Paper, Button, Adv, rounded
from .library import local_played_date


def load_record(lib):
    today = datetime.now().astimezone().date()
    first = today.replace(day=1)
    chart_first = today - timedelta(days=29)
    # Library's mutex gives the projection one consistent view against its timer.
    with lib.connection() as c:
        times = play.list_playtimes(lib)
        days = play.daily_totals(lib, min(first, chart_first).isoformat(), today.isoformat())
        sessions = play.list_sessions(lib, limit=50)
        works = repo.list_works(lib, include_hidden=True)
        month_by_work = dict(c.execute("SELECT work_id,SUM(minutes)*60 FROM legacy_play_logs WHERE minutes>=0 "
                            "AND day BETWEEN ? AND ? GROUP BY work_id", (first.isoformat(), today.isoformat())))
        if lib.schema_version >= 2:
            for wid, seconds in c.execute("SELECT work_id,SUM(seconds) FROM play_sessions WHERE state!='running' "
                         "AND local_day BETWEEN ? AND ? GROUP BY work_id", (first.isoformat(), today.isoformat())):
                month_by_work[wid] = month_by_work.get(wid, 0) + seconds
    return dict(times=times, days=days[-30:], month=sum(d.seconds + d.legacy_seconds for d in days if d.day >= first.isoformat()),
                sessions=sessions, works=works, month_by_work=month_by_work)


class StatCard(QWidget):
    def __init__(self, owner, title, value_role="ink"):
        super().__init__()
        self.owner = owner
        self.value_role = value_role
        self.setMinimumHeight(88)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        label = QLabel(title)
        label.setFont(font("body", 11, True))
        self.value = QLabel("—")
        self.value.setFont(font("title", 20, True))
        self.value.setWordWrap(True)
        lay.addWidget(label)
        lay.addWidget(self.value, 1)

    def paintEvent(self, event):
        self.value.setStyleSheet(f"color:{self.owner.theme.roles[self.value_role]}")
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded(p, self.rect().adjusted(1,1,-1,-1), self.owner.theme.color("card"), 12, self.owner.theme.color("line"))


class DayChart(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner, self.days = owner, []
        self.empty = True
        self.setMinimumHeight(180)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.owner.theme
        # A sakura wash keeps the sparse plot part of the paper/ADV visual language.
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, t.color("card"))
        base, tint = t.color("card"), t.color("sakura")
        strength = 1.0 if t.name == "day" else .35
        end = QColor(*(round(a + (b-a)*strength) for a,b in zip((base.red(),base.green(),base.blue()),
                                                             (tint.red(),tint.green(),tint.blue()))))
        gradient.setColorAt(1, end)
        rounded(p, self.rect().adjusted(1,1,-1,-1), QBrush(gradient), 12, t.color("line"))
        if not self.days:
            return
        maximum = max(60, max(d.seconds + d.legacy_seconds for d in self.days))
        width = (self.width() - 32) / len(self.days)
        base, height = self.height() - 32, self.height() - 60
        p.setPen(QPen(t.color("line"), 1))
        p.drawLine(16, base, self.width() - 16, base)
        for i, d in enumerate(self.days):
            x = 16 + i * width + width * .17
            old, new = height * d.legacy_seconds / maximum, height * d.seconds / maximum
            p.fillRect(QRectF(x, base-old, width*.66, old), t.color("kin"))
            p.fillRect(QRectF(x, base-old-new, width*.66, new), t.color("beni"))
            if i % 7 == 0:
                p.setFont(font("mono", 10))
                p.setPen(t.color("sub"))
                p.drawText(QRectF(16+i*width-10, base+6, 60, 22), Qt.AlignmentFlag.AlignLeft, d.day[5:])
        if self.empty and self.owner.show_mascot:
            pix = asset("shiori/s2-chibi.png").scaledToHeight(130, Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((self.width()-pix.width())//2, 20, pix)

    def mouseMoveEvent(self, event):
        if self.days:
            i = int((event.position().x()-16) / ((self.width()-32) / len(self.days)))
            if 0 <= i < len(self.days):
                d = self.days[i]
                self.setToolTip(d.day + " · " + play.format_duration(d.seconds+d.legacy_seconds))
            else:
                self.setToolTip("")


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(31)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    widget.horizontalHeader().setMinimumSectionSize(45)
    return widget


class ManualDialog(QDialog):
    def __init__(self, page):
        super().__init__(page.owner)
        self.page, self.owner = page, page.owner
        self.saving = False
        self.setWindowTitle("补记一条")
        self.resize(460, 260)
        lay, form = QVBoxLayout(self), QFormLayout()
        self.work = QComboBox()
        self.work.setEditable(True)
        self.work.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for work in page.works:
            self.work.addItem(work.title, work.id)
        self.work.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.work.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.day = QDateEdit(QDate.currentDate())
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.day.setCalendarPopup(True)
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 1440)
        self.minutes.setValue(30)
        form.addRow("作品", self.work)
        form.addRow("日期", self.day)
        form.addRow("分钟", self.minutes)
        lay.addLayout(form)
        self.message = QLabel()
        self.message.setWordWrap(True)
        lay.addWidget(self.message)
        row = QHBoxLayout()
        self.save_button = Button(self.owner, "保存", self.save, primary=True)
        self.cancel_button = Button(self.owner, "取消", self.reject)
        row.addWidget(self.cancel_button)
        row.addWidget(self.save_button)
        lay.addLayout(row)
        self.save_button.setEnabled(bool(page.works))

    def save(self):
        if self.saving or self.owner.busy:
            return
        i = self.work.currentIndex()
        if i < 0 or self.work.currentText() != self.work.itemText(i):
            self.message.setText("请选择已登记的作品")
            return
        lib, wid = self.owner.lib, self.work.currentData()
        day, minutes = self.day.date().toString("yyyy-MM-dd"), self.minutes.value()
        self.saving = True
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        try:
            self.owner.run_job("补记游玩", lambda cancel: play.add_manual_session(lib,wid,day,minutes),
                               self.saved, self.failed, cancellable=False)
        except Exception as error:
            self.failed(error)

    def saved(self, value):
        self.saving = False
        self.accept()
        self.owner.refresh_play_views()

    def failed(self, error):
        self.saving = False
        self.save_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.message.setText(str(error))

    def reject(self):
        if not self.saving:
            super().reject()

    def closeEvent(self, event):
        if self.saving:
            event.ignore()
        else:
            super().closeEvent(event)


class RecordPage(Paper):
    def __init__(self, owner):
        super().__init__(owner)
        self.works, self.session_rows, self.rank_ids = [], [], []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24,16,24,12)
        lay.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("記録")
        title.setFont(font("title",23,True))
        heading.addWidget(title)
        en = QLabel("RECORD")
        en.setFont(font("mono",11))
        heading.addWidget(en)
        heading.addStretch()
        heading.addWidget(QLabel("遊んだ日々を、書き留める。"))
        lay.addLayout(heading)
        self.cards = {}
        cards = QHBoxLayout()
        for key, title in [("total","総時間 TOTAL"),("month","今月 MONTH"),("titles","作品 TITLES"),("last","最近 LAST")]:
            card = StatCard(owner,title,{"total":"beni","month":"kin","titles":"wakaba","last":"ink"}[key])
            if key == "last":
                card.value.setFont(font("body",14,True))
            cards.addWidget(card,1)
            self.cards[key] = card.value
        lay.addLayout(cards)
        middle = QHBoxLayout()
        left, right = QVBoxLayout(), QVBoxLayout()
        chart_heading = QHBoxLayout()
        chart_heading.addWidget(QLabel("最近 30 天"))
        chart_heading.addStretch()
        legend = QLabel("旧记录  ·  新记录")
        legend.setText(f'<span style="color:{owner.theme.roles["kin"]}">● 旧记录</span>　<span style="color:{owner.theme.roles["beni"]}">● 新记录</span>')
        chart_heading.addWidget(legend)
        left.addLayout(chart_heading)
        self.chart = DayChart(owner)
        left.addWidget(self.chart)
        right.addWidget(QLabel("作品排行"))
        self.ranking = table(["No.","书名","总时长","最近"])
        self.ranking.cellDoubleClicked.connect(self.open_rank)
        right.addWidget(self.ranking)
        middle.addLayout(left, 1)
        middle.addLayout(right, 1)
        lay.addLayout(middle, 1)
        bottom = QHBoxLayout()
        history = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel("游玩记录"))
        row.addStretch()
        self.add_button = Button(owner,"补记一条",self.add_manual,primary=True)
        row.addWidget(self.add_button)
        history.addLayout(row)
        self.sessions = table(["日期","书名","时长","类型","备注"])
        self.sessions.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sessions.customContextMenuRequested.connect(self.context_menu)
        history.addWidget(self.sessions,1)
        bottom.addLayout(history,1)
        self.adv = Adv(owner, pose="base", height=300)
        self.adv.setFixedWidth(405)
        bottom.addWidget(self.adv,alignment=Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(bottom,1)
        self.legacy_label = QLabel()
        self.legacy_label.setFont(font("body",11))
        lay.addWidget(self.legacy_label)
        self.update_readonly()

    def update_readonly(self):
        self.add_button.setEnabled(bool(self.owner.lib and not self.owner.lib.readonly))

    def set_data(self, data):
        self.works = data["works"]
        names = {w.id:w.title for w in self.works}
        times = data["times"]
        total = sum(p.total_seconds for p in times.values())
        legacy = sum(p.legacy_seconds for p in times.values())
        tracked = sorted((p for p in times.values() if p.last_played_source == "session"),
                         key=lambda p:(p.last_played_at,-p.work_id),reverse=True)
        last = tracked[0] if tracked else None
        historical = sorted((p for p in times.values() if p.last_played_at),
                            key=lambda p:(p.last_played_at,-p.work_id),reverse=True)
        adv_last = last or (historical[0] if historical else None)
        self.cards["total"].setText(play.format_duration(total))
        self.cards["month"].setText(play.format_duration(data["month"]))
        self.cards["titles"].setText(str(sum(p.total_seconds>0 for p in times.values())))
        self.cards["last"].setText(names[last.work_id]+"\n"+local_played_date(last) if last else "—")
        self.chart.days = data["days"]
        self.chart.empty = not data["sessions"] and not total and not last
        self.chart.update()
        rank = sorted((p for p in times.values() if p.total_seconds>0),key=lambda p:(-p.total_seconds,p.work_id))[:20]
        self.rank_ids = [p.work_id for p in rank]
        self.ranking.setRowCount(len(rank))
        for row,p in enumerate(rank):
            for col,text in enumerate([f"{p.work_id:03d}",names[p.work_id],play.format_duration(p.total_seconds),local_played_date(p)]):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setForeground(self.owner.theme.color("kin"))
                    item.setFont(font("mono",12))
                self.ranking.setItem(row,col,item)
        self.session_rows = data["sessions"]
        self.sessions.setRowCount(len(self.session_rows))
        for row,s in enumerate(self.session_rows):
            kind = "恢复" if s.state == "recovered" else "计时" if s.kind == "tracked" else "补记"
            for col,text in enumerate([s.local_day,names[s.work_id],play.format_duration(s.seconds),kind,"已修改" if s.edited else ""]):
                item = QTableWidgetItem(text)
                if col == 3:
                    item.setForeground(self.owner.theme.color("yamabuki" if s.state=="recovered" else "beni" if s.kind=="tracked" else "kin"))
                self.sessions.setItem(row,col,item)
        self.legacy_label.setText("含旧 GalPanel 带来的时长 "+play.format_duration(legacy)+"（只读，不能修改）" if legacy else "")
        month = data["month"]
        if month:
            wid = min(data["month_by_work"],key=lambda w:(-data["month_by_work"][w],w))
            self.adv.text = f"这个月一共玩了 {play.format_duration(month)}。最常玩的是「{names[wid]}」——要记得休息哦。"
        elif adv_last:
            self.adv.text = f"这个月还没有玩过。上次玩的是「{names[adv_last.work_id]}」。"
        else:
            self.adv.text = "还没有游玩记录。在书架上选一部作品，按「はじめる」开始吧。"
        self.adv.update()
        self.update_readonly()

    def open_rank(self, row, col):
        if 0<=row<len(self.rank_ids):
            wid=self.rank_ids[row]
            page=self.owner.library_page
            page.search.clear()
            page.set_filter("隐藏" if next(w for w in self.works if w.id==wid).hidden else "全部")
            self.owner.show_library()
            page.select_work(wid)

    def add_manual(self):
        if self.owner.lib and not self.owner.lib.readonly and not self.owner.busy:
            self.manual_dialog=ManualDialog(self)
            self.manual_dialog.open()

    def context_menu(self,pos):
        item=self.sessions.itemAt(pos)
        if not item or self.owner.busy:
            return
        s=self.session_rows[item.row()]
        menu=QMenu(self)
        edit=menu.addAction("修改分钟数…",lambda:self.edit(s))
        delete=menu.addAction("删除这条记录…",lambda:self.delete(s))
        editable=bool(self.owner.lib and not self.owner.lib.readonly and s.state!='running')
        edit.setEnabled(editable)
        delete.setEnabled(editable)
        menu.exec(self.sessions.viewport().mapToGlobal(pos))

    def edit(self,s):
        if not self.owner.lib or self.owner.lib.readonly or s.state=='running' or self.owner.busy:
            return
        minutes,ok=QInputDialog.getInt(self,"修改分钟数","分钟",min(1440,s.seconds//60),0,1440)
        if ok:
            lib=self.owner.lib
            self.owner.run_job("修改游玩记录",lambda cancel:play.edit_session(lib,s.id,minutes),
                               lambda _:self.owner.refresh_play_views(),cancellable=False)

    def delete(self,s):
        if not self.owner.lib or self.owner.lib.readonly or s.state=='running' or self.owner.busy:
            return
        if QMessageBox.question(self,"删除这条记录","只删除这条游玩记录，不影响游戏和存档。") == QMessageBox.StandardButton.Yes:
            lib=self.owner.lib
            self.owner.run_job("删除游玩记录",lambda cancel:play.delete_session(lib,s.id),
                               lambda _:self.owner.refresh_play_views(),cancellable=False)
