"""p2-1 rules, all libraries and process fixtures live in tmp_path."""
from dataclasses import asdict
from datetime import datetime
import json
import ntpath
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest
from galpanel2.core import store, play, launchopts as lo, domain as D, repository as repo, legacy
from galpanel2.launch import process, tracker as T


@pytest.fixture
def work(lib, tmp_path):
    folder = tmp_path / "Game"
    folder.mkdir()
    (folder / "Game.exe").write_bytes(b"fixture")
    with lib.transaction() as c:
        c.execute("INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(1,?,'Story',?,?)",
                  (str(uuid.uuid4()), D.utc_now(), D.utc_now()))
        c.execute("INSERT INTO copies(id,work_id,kind,path,path_key,exe_relpath,origin,added_at) "
                  "VALUES(1,1,'folder',?,?,'Game.exe','scan',?)", (str(folder), D.path_key(str(folder)), D.utc_now()))
    return 1, 1, folder


def v1(tmp_path):
    path = tmp_path / "v1.galdb"
    c = sqlite3.connect(path)
    c.executescript(Path(store.__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    c.execute(f"PRAGMA application_id={store.APPLICATION_ID}")
    c.execute("PRAGMA user_version=1")
    c.executemany("INSERT INTO library_info VALUES(?,?)", [("library_uuid", str(uuid.uuid4())),
                  ("created_at", "2026-01-01T00:00:00Z"), ("app_version", "2.0.0a1")])
    c.execute("INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(1,?,'Old',?,?)",
              (str(uuid.uuid4()), D.utc_now(), D.utc_now()))
    c.commit()
    c.close()
    return path


def test_v2_create_and_atomic_upgrade(lib, tmp_path):
    assert lib.schema_version == 6
    with lib.connection() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    path = v1(tmp_path)
    before = path.read_bytes()
    with pytest.raises(D.NeedsUpgrade):
        store.open_library(str(path))
    assert not Path(str(path) + ".lock").exists() and path.read_bytes() == before
    with store.open_library(str(path), True) as ro:
        assert ro.schema_version == 1 and play.playtime(ro, 1).total_seconds == 0
        assert play.list_sessions(ro) == [] and len(play.list_playtimes(ro)) == 1
        assert len(play.daily_totals(ro, "2026-01-01", "2026-01-03")) == 3
        for fn, args in [(play.start_session, (1, None)), (play.checkpoint_session, (1, 0)),
                         (play.end_session, (1, 0, 'exited')), (play.discard_session, (1,)),
                         (play.recover_sessions, ()), (play.add_manual_session, (1,'2026-01-01',1)),
                         (play.edit_session, (1,1)), (play.delete_session, (1,))]:
            with pytest.raises(ValueError):
                fn(ro, *args)
    result = store.upgrade_library(str(path))
    assert (result.from_version, result.to_version) == (1, 6)
    with sqlite3.connect(result.backup_path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == 'ok'
        assert c.execute("SELECT title FROM works").fetchone()[0] == 'Old'
    with store.open_library(str(path)) as upgraded:
        assert upgraded.schema_version == 6
    with pytest.raises(ValueError, match="already version 6"):
        store.upgrade_library(str(path))


@pytest.mark.parametrize("version", [0, 7, 10])
def test_unknown_version(tmp_path, version):
    path = v1(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute(f"PRAGMA user_version={version}")
    for fn in (store.open_library, store.upgrade_library):
        with pytest.raises(D.NotALibrary):
            fn(str(path))


def test_upgrade_backup_collision_and_writer(tmp_path, monkeypatch):
    path = v1(tmp_path)
    monkeypatch.setattr(store, 'utc_now', lambda: '2026-10-11T09:30:00Z')
    dest = Path(str(path) + '.v1-20261011T093000Z.bak')
    dest.write_bytes(b'occupied')
    before = path.read_bytes()
    with pytest.raises(D.TargetExists):
        store.upgrade_library(str(path))
    assert path.read_bytes() == before and dest.read_bytes() == b'occupied'
    lock = store._WriterLock(str(path))
    try:
        with pytest.raises(D.LibraryLocked):
            store.upgrade_library(str(path))
    finally:
        lock.release()


def test_upgrade_failure_rolls_back(tmp_path, monkeypatch):
    path = v1(tmp_path)
    before = path.read_bytes()
    original = Path.read_text
    def ddl(p, *a, **k):
        if p.name == 'schema_v2.sql':
            return original(p, *a, **k) + 'INVALID SQL;'
        return original(p, *a, **k)
    monkeypatch.setattr(Path, 'read_text', ddl)
    with pytest.raises(sqlite3.DatabaseError):
        store.upgrade_library(str(path))
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.partial'))


def test_upgrade_killed_transaction(tmp_path):
    path = v1(tmp_path)
    before = path.read_bytes()
    pause = tmp_path / 'paused'
    child = subprocess.Popen([sys.executable, '-c', f'from galpanel2.core.store import upgrade_library; upgrade_library({str(path)!r})'],
                             env=dict(os.environ, GALPANEL2_TEST_PAUSE_FILE=str(pause)))
    try:
        deadline = time.monotonic() + 15
        while not pause.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert pause.exists()
    finally:
        child.kill()
        child.wait(10)
    with sqlite3.connect(path) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 1
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='play_sessions'").fetchone()
    assert path.read_bytes() == before


@pytest.mark.parametrize('bad', ['..\\x.exe','C:\\x.exe','C:x.exe','\\x.exe','/x.exe','', 'a.txt','sub/../../x.exe','x\0.exe'])
def test_options_invalid_paths(lib, work, bad):
    with pytest.raises(ValueError):
        lo.set_launch_options(lib, 1, bad, '')
    assert lo.get_launch_options(lib, 1).exe_relpath is None


@pytest.mark.parametrize('args', ['x\0y','x'*2001, None, 2])
def test_options_invalid_args(lib, work, args):
    with pytest.raises(ValueError):
        lo.set_launch_options(lib, 1, None, args)


def test_options_and_resolution_order(lib, work):
    assert lo.get_launch_options(lib, 1) == lo.LaunchOptions(1, None, '', 'Game.exe')
    assert lo.resolve_launch(lib, 1).cwd == str(work[2])
    opts = lo.set_launch_options(lib, 1, 'sub/../Game.exe', '  -x ')
    assert opts.exe_relpath == 'Game.exe' and lo.resolve_launch(lib, 1).args == '  -x '
    lo.set_launch_options(lib, 1, None, 'x'*2000)
    for fn in [lo.get_launch_options, lo.resolve_launch, lo.default_copy]:
        with pytest.raises(KeyError):
            fn(lib, 999)
    with pytest.raises(KeyError):
        lo.set_launch_options(lib, 999, None, '')
    with lib.transaction() as c:
        c.execute("UPDATE copies SET kind='archive' WHERE id=1")
    with pytest.raises(ValueError):
        lo.set_launch_options(lib, 1, None, '')
    with pytest.raises(D.NotLaunchable) as e:
        lo.resolve_launch(lib, 1)
    assert e.value.reason == 'archive'
    with lib.transaction() as c:
        c.execute("UPDATE copies SET kind='folder' WHERE id=1")
    (work[2] / 'Game.exe').unlink()
    with pytest.raises(D.NotLaunchable) as e:
        lo.resolve_launch(lib, 1)
    assert e.value.reason == 'exe_missing'
    with lib.transaction() as c:
        c.execute("UPDATE copies SET exe_relpath=NULL WHERE id=1")
    with pytest.raises(D.NotLaunchable) as e:
        lo.resolve_launch(lib, 1)
    assert e.value.reason == 'no_exe'
    work[2].rmdir()
    with pytest.raises(D.NotLaunchable) as e:
        lo.resolve_launch(lib, 1)
    assert e.value.reason == 'copy_missing'


def test_resolution_containment_and_default(lib, work):
    with lib.transaction() as c:
        c.execute("INSERT INTO launch_options VALUES(1,'..\\outside.exe','',?)", (D.utc_now(),))
    with pytest.raises(D.NotLaunchable) as e:
        lo.default_copy(lib, 1)
    assert e.value.reason == 'bad_exe_path'
    with lib.transaction() as c:
        c.execute('DELETE FROM copies')
    with pytest.raises(D.NotLaunchable) as e:
        lo.default_copy(lib, 1)
    assert e.value.reason == 'no_copy'


def test_session_lifecycle_rules(lib, work):
    sid = play.start_session(lib, 1, 1)
    assert play.get_session(lib, sid).local_day == datetime.now().astimezone().date().isoformat()
    with pytest.raises(D.SessionActive):
        play.start_session(lib, 1, None)
    with pytest.raises(KeyError):
        play.start_session(lib, 999, None)
    with pytest.raises(ValueError):
        play.start_session(lib, 1, 999)
    with pytest.raises(ValueError):
        play.start_session(lib, 1, None, '2026/10/11')
    play.checkpoint_session(lib, sid, 8)
    for fn, args in [(play.checkpoint_session,(sid,7)), (play.end_session,(sid,7,'exited')),
                     (play.end_session,(sid,10,'invalid')), (play.discard_session,(sid,)),
                     (play.edit_session,(sid,1)), (play.delete_session,(sid,))]:
        with pytest.raises(ValueError):
            fn(lib, *args)
    assert play.playtime(lib, 1).total_seconds == 0
    closed = play.end_session(lib, sid, 10, 'panel_closed')
    assert closed.state == 'closed' and closed.end_reason == 'panel_closed'
    for fn, args in [(play.checkpoint_session,(sid,11)),(play.end_session,(sid,11,'exited')),
                     (play.discard_session,(sid,)), (play.checkpoint_session,(999,1))]:
        with pytest.raises(ValueError):
            fn(lib, *args)
    empty = play.start_session(lib, 1, None, '2026-10-01')
    play.discard_session(lib, empty)
    with pytest.raises(KeyError):
        play.get_session(lib, empty)


@pytest.mark.parametrize('day,minutes', [('2026-02-30',1),('2026-10-2',1),('2026-10-01',0),
                                        ('2026-10-01',1441),('2026-10-01',1.5),('2026-10-01',True)])
def test_bad_manual_values(lib, work, day, minutes):
    with pytest.raises(ValueError):
        play.add_manual_session(lib, 1, day, minutes)


def test_manual_edit_delete_sorting_and_cascade(lib, work):
    a = play.add_manual_session(lib, 1, '2026-10-01', 1440)
    b = play.add_manual_session(lib, 1, '2026-10-02', 1)
    assert play.list_sessions(lib, 1, 1)[0].id == b
    assert play.playtime(lib,1).last_played_at is None
    edited = play.edit_session(lib,a,0)
    assert edited.edited and edited.seconds == 0
    for m in [-1,1441,1.5]:
        with pytest.raises(ValueError):
            play.edit_session(lib,a,m)
    play.delete_session(lib,b)
    for fn, args in [(play.get_session,(b,)), (play.edit_session,(b,1)), (play.delete_session,(b,)),
                     (play.playtime,(999,)), (play.add_manual_session,(999,'2026-10-01',1))]:
        with pytest.raises(KeyError):
            fn(lib,*args)
    repo.remove_work(lib,1,repo.get_work(lib,1).revision)
    assert play.list_sessions(lib) == []


def test_recover_preserves_checkpoint(lib, work):
    sid = play.start_session(lib,1,1)
    play.checkpoint_session(lib,sid,7)
    with lib.connection() as c:
        beat = c.execute('SELECT last_beat_at FROM play_sessions WHERE id=?',(sid,)).fetchone()[0]
    path=lib.path
    lib.close()
    with store.open_library(path,True) as ro:
        assert play.get_session(ro,sid).state == 'running'
    with store.open_library(path) as reopened:
        s=play.get_session(reopened,sid)
        assert (s.state,s.seconds,s.ended_at,s.end_reason)==('recovered',7,beat,'recovered')
        assert play.recover_sessions(reopened)==0
        assert play.playtime(reopened,1).last_played_source=='session'


@pytest.mark.parametrize('first,last', [('2026-10-02','2026-10-01'),('2026-1-01','2026-10-01'),
                                      ('2026-01-01','2027-01-03')])
def test_bad_day_range(lib, first, last):
    with pytest.raises(ValueError):
        play.daily_totals(lib,first,last)


@pytest.mark.parametrize('seconds,expected', [(0,'0 分'),(1,'不到 1 分'),(59,'不到 1 分'),(60,'1 分'),
                                            (3599,'59 分'),(3600,'1 小时 00 分'),(5400,'1 小时 30 分'),
                                            (36000,'10 小时 00 分')])
def test_duration(seconds, expected, tmp_path):
    assert play.format_duration(seconds)==expected
    with pytest.raises(ValueError):
        play.format_duration(-1)


def test_legacy_time_daily_and_aggregate(lib, work):
    with lib.transaction() as c:
        c.execute("INSERT INTO imports VALUES(1,'fixture','x','x','[]',1,'[]',?)",(D.utc_now(),))
        c.executemany('INSERT INTO legacy_fields VALUES(1,1,?,?)',[
            ('playtime_minutes',json.dumps({'t':'integer','v':90})),
            ('last_played_at',json.dumps({'t':'text','v':'old text'}))])
        c.executemany('INSERT INTO legacy_play_logs VALUES(1,?,?)',[('2026-10-01',5),('2026-10-02',-4)])
    with lib.connection() as c:
        before=list(c.execute('SELECT * FROM legacy_fields'))
    manual=play.add_manual_session(lib,1,'2026-10-01',1)
    pt=play.playtime(lib,1)
    assert (pt.legacy_seconds,pt.session_seconds,pt.last_played_at,pt.last_played_source)==(5400,60,'old text','legacy')
    days=play.daily_totals(lib,'2026-10-01','2026-10-03')
    assert [(d.seconds,d.legacy_seconds) for d in days]==[(60,300),(0,0),(0,0)]
    assert len(play.daily_totals(lib,'2026-01-01','2027-01-01'))==366
    assert len(play.daily_totals(lib,'2026-01-01','2027-01-02'))==367
    sid=play.start_session(lib,1,None)
    ended=play.end_session(lib,sid,100,'exited')
    assert play.playtime(lib,1).last_played_at==ended.ended_at
    assert play.list_playtimes(lib)[1]==play.playtime(lib,1)
    play.edit_session(lib,manual,2)
    play.delete_session(lib,manual)
    with lib.connection() as c:
        assert list(c.execute('SELECT * FROM legacy_fields'))==before


def test_legacy_import_creates_v2(tmp_path):
    old=tmp_path/'legacy.db'
    with sqlite3.connect(old) as c:
        c.execute('CREATE TABLE games(id INTEGER PRIMARY KEY,title TEXT,kind TEXT,path TEXT,playtime_minutes,last_played_at)')
        for i,val in enumerate([12,'12',-2,None],1):
            c.execute('INSERT INTO games VALUES(?,?,?,?,?,?)',(i,f'Old{i}','folder',f'D:\\fixture\\G{i}',val,''))
    new=tmp_path/'import.galdb'
    report=legacy.import_legacy(str(old),str(new))
    assert report.imported==4
    with store.open_library(str(new),True) as lib:
        assert lib.schema_version==6
        assert sorted(p.total_seconds for p in play.list_playtimes(lib).values())==[0,0,0,720]


@pytest.mark.parametrize('value,want', [('',30),('0',30),('61',30),('x',30),('1',1),('60',60)])
def test_checkpoint_env(tmp_path, monkeypatch, value, want):
    monkeypatch.setenv('GALPANEL2_TEST_CHECKPOINT_SECONDS',value)
    assert T._checkpoint_interval()==want
    assert T.accumulate(10,3600)==15 and T.accumulate(10,-1)==10 and T.accumulate(10,.5)==10.5


def test_tracker_failures_and_retry(lib, work, monkeypatch):
    spec=lo.resolve_launch(lib,1)
    def fail(spec):
        raise D.LaunchError('fixture failure')
    monkeypatch.setattr(process,'launch',fail)
    with pytest.raises(D.LaunchError):
        T.Tracker(lib,spec).start()
    assert play.list_sessions(lib)==[]
    class Fake:
        pid=1
        released=False
        def alive(self): return True
        def release(self): self.released=True
    fake=Fake()
    monkeypatch.setattr(process,'launch',lambda _: fake)
    monkeypatch.setattr(T,'TICK_SECONDS',.02)
    monkeypatch.setattr(T,'_checkpoint_interval',lambda: .02)
    checkpoint=play.checkpoint_session
    attempts=[]
    def flaky(*args):
        attempts.append(1)
        if len(attempts)==1: raise sqlite3.OperationalError('busy')
        checkpoint(*args)
    monkeypatch.setattr(play,'checkpoint_session',flaky)
    ended=[]
    tr=T.Tracker(lib,spec,on_end=ended.append)
    tr.start()
    time.sleep(.13)
    tr.stop();tr.join(3)
    assert len(attempts)>1 and ended[0].end_reason=='panel_closed' and fake.released
    monkeypatch.setattr(play,'end_session',lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('busy')))
    ended=[]
    tr=T.Tracker(lib,spec,on_end=ended.append)
    tr.start();tr.stop();tr.join(3)
    assert isinstance(ended[0],sqlite3.OperationalError)
    assert play.get_session(lib,tr.session_id).state=='running'
    assert play.recover_sessions(lib)==1


def test_cli_upgrade_playtime(tmp_path):
    path=v1(tmp_path)
    for args in [('upgrade',str(path)),('playtime',str(path),'--work','1')]:
        r=subprocess.run([sys.executable,'-m','galpanel2.core',*args],capture_output=True,text=True,encoding='utf-8')
        assert r.returncode==0
        data=json.loads(r.stdout)
        assert data
    r=subprocess.run([sys.executable,'-m','galpanel2.core','upgrade',str(path)],capture_output=True,text=True,encoding='utf-8')
    assert r.returncode==2 and json.loads(r.stdout)['error']=='ValueError'


@pytest.mark.parametrize('wrapper', [False,True])
def test_real_job_child_tracking(lib,work,wrapper):
    folder=work[2]
    shutil.copy2(Path(os.environ['SystemRoot'])/'System32/PING.EXE',folder/'Game.exe')
    if wrapper:
        shutil.copy2(Path(os.environ['SystemRoot'])/'System32/cmd.exe',folder/'Launcher.exe')
        lo.set_launch_options(lib,1,'Launcher.exe',f'/c start "" /b "{folder}\\Game.exe" -n 6 127.0.0.1')
    else:
        lo.set_launch_options(lib,1,None,'-n 4 127.0.0.1')
    ended=[]
    tr=T.Tracker(lib,lo.resolve_launch(lib,1),on_end=ended.append)
    tr.start()
    try:
        tr.join(15)
        assert ended and ended[0].end_reason=='exited'
        assert (4 if wrapper else 2)<=ended[0].seconds<=10
    finally:
        tr.stop();tr.join(3)
        subprocess.run(['taskkill','/F','/T','/PID',str(tr.process.pid)],capture_output=True)


def test_real_stop_leaves_game_running(lib,work):
    shutil.copy2(Path(os.environ['SystemRoot'])/'System32/PING.EXE',work[2]/'Game.exe')
    lo.set_launch_options(lib,1,None,'-n 30 127.0.0.1')
    ended=[]
    tr=T.Tracker(lib,lo.resolve_launch(lib,1),on_end=ended.append)
    tr.start()
    pid=tr.process.pid
    try:
        time.sleep(1.1)
        tr.stop();tr.join(3)
        assert ended[0].end_reason=='panel_closed'
        r=subprocess.run(['tasklist','/FI',f'PID eq {pid}','/NH','/FO','CSV'],capture_output=True,text=True)
        assert f'"{pid}"' in r.stdout
    finally:
        subprocess.run(['taskkill','/F','/T','/PID',str(pid)],capture_output=True)


def test_cli_stop_does_not_inherit_captured_output(lib,work,tmp_path):
    shutil.copy2(Path(os.environ['SystemRoot'])/'System32/PING.EXE',work[2]/'Game.exe')
    lo.set_launch_options(lib,1,None,'-n 30 127.0.0.1')
    path=lib.path
    lib.close()
    stop=tmp_path/'stop'
    stop.touch()
    child=subprocess.Popen([sys.executable,'-m','galpanel2.launch','play',path,'1','--stop-file',str(stop)],
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    pid=None
    try:
        started=json.loads(child.stdout.readline())
        pid=started['pid']
        out,err=child.communicate(timeout=6)
        assert child.returncode==0,err
        assert json.loads(out)['end_reason']=='panel_closed'
        r=subprocess.run(['tasklist','/FI',f'PID eq {pid}','/NH','/FO','CSV'],capture_output=True,text=True)
        assert f'"{pid}"' in r.stdout
    finally:
        if child.poll() is None:
            child.kill();child.communicate(timeout=6)
        if pid is not None:
            subprocess.run(['taskkill','/F','/T','/PID',str(pid)],capture_output=True)
