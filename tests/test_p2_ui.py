"""p2 interface behavior under offscreen Qt, with temporary fixtures only."""
from datetime import datetime, timedelta
import json
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from galpanel2.core import store, play, repository as repo
from galpanel2.ui.launchdialog import LaunchDialog
from galpanel2.ui.record import ManualDialog
from test_ui import app, window, library, wait
from test_p2_core import v1


def record_fixture(path):
    day = datetime.now().astimezone().date()
    with store.open_library(str(path)) as lib:
        works = repo.list_works(lib, True)
        a, b, c = [w.id for w in works[:3]]
        with lib.transaction() as db:
            db.execute("INSERT INTO imports VALUES(1,'fixture','x','x','[]',1,'[]',?)", (works[0].added_at,))
            db.execute("INSERT INTO legacy_fields VALUES(?,1,'playtime_minutes',?)",(a,json.dumps({'t':'integer','v':90})))
            db.execute('INSERT INTO legacy_play_logs VALUES(?,?,5)',(a,day.isoformat()))
        play.add_manual_session(lib,a,day.isoformat(),30)
        play.add_manual_session(lib,b,day.isoformat(),20)
        sid=play.start_session(lib,c,None)
        play.end_session(lib,sid,120,'exited')
    return a,b,c,day


def test_record_cards_ranking_days_and_readonly(window, app, library):
    a,b,c,day=record_fixture(library[0])
    window.open_library_path(str(library[0]))
    wait(app,window)
    window.show_record()
    wait(app,window)
    rp=window.record_page
    assert window.screen=='record'
    assert rp.cards['total'].text()==play.format_duration(90*60+30*60+20*60+120)
    assert rp.cards['month'].text()==play.format_duration(30*60+20*60+120+5*60)
    assert rp.cards['titles'].text()=='3'
    assert day.isoformat() in rp.cards['last'].text()
    assert rp.rank_ids==[a,b,c] and rp.sessions.rowCount()==3
    assert len(rp.chart.days)==30 and rp.chart.days[-1].day==day.isoformat()
    assert rp.chart.days[-1].legacy_seconds==300 and rp.add_button.isEnabled()
    assert '只读' in rp.legacy_label.text()
    rp.open_rank(1,0)
    assert window.screen=='library' and window.library_page.detail.work.id==b
    before=library[0].read_bytes()
    window.show_title()
    wait(app,window)
    window.screenshot_mode=True
    window.open_library_path(str(library[0]))
    wait(app,window)
    window.show_record()
    wait(app,window)
    assert not rp.add_button.isEnabled()
    assert not window.library_page.detail.start_button.isEnabled()
    assert library[0].read_bytes()==before


def test_stop_rejected_keeps_window_library_and_title(window, app, library, monkeypatch):
    window.open_library_path(str(library[0]))
    wait(app,window)
    cp=window.library_page.detail.current_copy()
    class Fake:
        work_id=window.library_page.detail.work.id
    window.tracker=Fake()
    window._playing_title='Story'
    calls=[]
    monkeypatch.setattr(window,'confirm_stop_tracking',lambda title,action:calls.append((title,action)) or False)
    window.show_title()
    assert window.screen=='library' and window.lib is not None
    window.open_library_path(str(library[0]))
    assert window.lib is not None
    window.close()
    app.processEvents()
    assert window.isVisible() and window.tracker is not None
    assert calls==[('Story','title'),('Story','library'),('Story','close')]
    window.remove_work(window.tracker.work_id)
    assert window.last_message=='这部作品正在游玩，结束后再移除。'
    window.tracker=None


def test_upgrade_declined_no_file_or_lock_changes(window,app,tmp_path,monkeypatch):
    path=v1(tmp_path)
    before=path.read_bytes()
    monkeypatch.setattr(window,'confirm_upgrade',lambda path:False)
    window.open_library_path(str(path))
    wait(app,window)
    assert window.lib is None and window.screen=='title'
    assert path.read_bytes()==before
    assert not list(tmp_path.glob('*.bak')) and not Path(str(path)+'.lock').exists()


def test_upgrade_accepted_backups_and_status(window,app,tmp_path,monkeypatch):
    path=v1(tmp_path)
    monkeypatch.setattr(window,'confirm_upgrade',lambda path:True)
    window.open_library_path(str(path))
    wait(app,window)
    assert window.lib.schema_version==6
    assert '已升级。备份在：' in window.status.message.text()
    assert len(list(tmp_path.glob('*.bak')))==1


def test_start_button_and_recent_sort(window,app,library):
    a,b,c,day=record_fixture(library[0])
    window.open_library_path(str(library[0]),b)
    wait(app,window)
    detail=window.library_page.detail
    assert detail.start_button.isEnabled() and detail.start_button.text()=='▶ 开始游戏'
    assert '玩过' in detail.facts.text() and '最近' in detail.facts.text() \
           and '加入' in detail.facts.text()
    window.library_page.order.setCurrentIndex(2)
    visible=window.library_page.model.works
    session_visible=[w for w in visible if w.id==c]
    if session_visible:
        assert visible[0].id==c
    archive=next(w.id for w in window.library_page.works if 'archive' in w.kinds)
    window.library_page.select_work(archive)
    assert not detail.start_button.isEnabled() and detail.start_button.toolTip()=='压缩包不能直接启动'


def test_launch_dialog_validation_and_scan_reset(window,app,library,monkeypatch,tmp_path):
    window.open_library_path(str(library[0]))
    wait(app,window)
    wid=next(w.id for w in window.library_page.works if 'folder' in w.kinds and not w.hidden)
    work=window.library_page.details[wid]
    cp=work.copies[0]
    window.launch_settings(wid,cp.id)
    wait(app,window)
    dlg=window.launch_dialog
    assert dlg.source.text()=='来自扫描'
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *a,**k:(str(tmp_path/'outside.exe'),''))
    dlg.choose()
    assert window.last_message=='启动文件必须在这份副本的目录里'
    assert dlg.source.text()=='来自扫描'
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *a,**k:(str(Path(cp.path)/'game.exe'),''))
    dlg.choose()
    assert dlg.source.text()=='手动指定'
    dlg.args.setText('a\0b')
    dlg.save()
    wait(app,window)
    assert 'Invalid launch arguments' in dlg.message.text() and dlg.isVisible()
    dlg.args.setText('  -x ')
    dlg.use_scan()
    dlg.save()
    wait(app,window)
    assert not dlg.isVisible()


def test_record_manual_edit_delete_operations(window,app,library,monkeypatch):
    window.open_library_path(str(library[0]))
    wait(app,window)
    window.show_record()
    wait(app,window)
    rp=window.record_page
    rp.add_manual()
    dlg=rp.manual_dialog
    dlg.minutes.setValue(10)
    dlg.save()
    wait(app,window)
    assert rp.sessions.rowCount()==1
    row=rp.session_rows[0]
    monkeypatch.setattr(QInputDialog,'getInt',lambda *a,**k:(0,True))
    rp.edit(row)
    wait(app,window)
    assert rp.session_rows[0].seconds==0 and rp.session_rows[0].edited
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Yes)
    rp.delete(rp.session_rows[0])
    wait(app,window)
    assert rp.sessions.rowCount()==0


def test_confirmed_close_reports_timer_write_failure_before_library_close(window,app,library,monkeypatch):
    from galpanel2.launch import process
    window.open_library_path(str(library[0]))
    wait(app,window)
    wid=next(w.id for w in window.library_page.works if 'folder' in w.kinds and not w.hidden)
    cp=window.library_page.details[wid].copies[0]
    class FakeProcess:
        pid=1
        def alive(self):return True
        def release(self):pass
    monkeypatch.setattr(process,'launch',lambda spec:FakeProcess())
    monkeypatch.setattr(play,'end_session',lambda *a,**k:(_ for _ in ()).throw(ValueError('fixture write failure')))
    window.start_game(wid,cp.id)
    wait(app,window)
    tracker=window.tracker
    monkeypatch.setattr(window,'confirm_stop_tracking',lambda *a:True)
    window.close()
    assert '计时没能写进库：fixture write failure' in window.last_message
    app.processEvents()
    assert window.tracker is None and window.lib is None and not window.isVisible()
    with store.open_library(str(library[0]),True) as ro:
        assert play.get_session(ro,tracker.session_id).state=='running'
    with store.open_library(str(library[0])) as reopened:
        assert play.get_session(reopened,tracker.session_id).state=='recovered'
