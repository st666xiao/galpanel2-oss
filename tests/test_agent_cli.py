"""Agent contracts, using only temporary libraries and fixture transport."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest

from galpanel2.agent import api
from galpanel2.agent.__main__ import main
from galpanel2.core import domain, store, sites


@pytest.fixture
def agent_library(tmp_path, monkeypatch):
    # Never touch default user settings or make real HTTP calls.
    monkeypatch.setenv('GALPANEL2_SETTINGS_DIR', str(tmp_path / 'settings'))
    monkeypatch.setattr(sites.UrllibTransport, 'request', lambda *a, **kw: pytest.fail('real network forbidden'))
    path = tmp_path / 'test.galdb'
    folder = tmp_path / 'Example RJ123456'
    folder.mkdir()
    (folder / 'game.exe').write_bytes(b'example')
    (folder / 'steam_appid.txt').write_text('698780')
    with store.create_library(str(path)) as lib:
        with lib.transaction() as c:
            for wid, title in ((1, 'Example'), (2, 'Another')):
                c.execute('INSERT INTO works(id,uuid,title,developer,added_at,updated_at,revision) VALUES(?,?,?,?,?,?,0)',
                          (wid, str(uuid.uuid4()), title, 'Example Brand', domain.utc_now(), domain.utc_now()))
            c.execute("INSERT INTO copies(work_id,kind,path,path_key,availability,origin,added_at) VALUES(1,'folder',?,?,'ok','scan','2026-01-01')", (str(folder), str(folder).casefold()))
    rule = tmp_path / 'sites.json'
    record = {'id': 'v17', 'title': 'Example', 'released': '2020-01-01', 'description': 'Example summary',
              'titles': [], 'developers': [{'id': 'p1', 'name': 'Example Brand'}], 'image': {'url': 'https://example.test/cover.png'}, 'tags': []}
    rule.write_text(json.dumps({'rules': [
        {'method': 'POST', 'url_prefix': 'https://api.vndb.org/kana/vn', 'json': {'results': [record]}},
        {'method': 'GET', 'url_prefix': 'https://example.test/cover.png', 'status': 404, 'json': {}},
    ]}))
    monkeypatch.setenv('GALPANEL2_FAKE_SITES', str(rule))
    monkeypatch.setattr(sites.DEFAULT_LIMITER, 'wait', lambda *a: None)
    return str(path)


def call(library, command, **args):
    envelope, code = api.execute(command, library=library, **args)
    assert code == 0, envelope
    return envelope['data']


def test_reads_and_filters(agent_library):
    assert call(agent_library, 'status')['works'] == 2
    rows = call(agent_library, 'list', unbound=True, query='brand', limit=1, offset=1)
    assert rows['total'] == 2 and rows['items'][0]['id'] == 2
    row = call(agent_library, 'show', work_id=1)
    assert row['copies'][0]['exists'] and row['revision'] == 0
    clues = call(agent_library, 'hints', work_id=1)['copies'][0]
    assert 'dlsite:RJ123456' in clues['codes'] and 'steam:698780' in clues['codes']
    assert 'game.exe' in clues['exes']


def test_search_fetch_plan_bind_and_conflict(agent_library):
    result = call(agent_library, 'search', site='vndb', query='Example')
    assert result['candidates'][0]['ref'] == 'vndb:v17'
    assert result['errors'] == []
    assert call(agent_library, 'fetch', ref='vndb:v17')['complete']
    assert call(agent_library, 'bind-plan', work_id=1, ref='vndb:v17')['revision'] == 0
    before = Path(agent_library).read_bytes()
    dry = call(agent_library, 'bind', work_id=1, ref='vndb:v17', revision=0)
    assert not dry['applied'] and Path(agent_library).read_bytes() == before
    assert not list(Path(agent_library).parent.glob('*.bak'))
    bound = call(agent_library, 'bind', work_id=1, ref='vndb:v17', revision=0, apply=True)
    assert bound['applied'] and bound['revision'] == 1 and bound['cover_error']
    with sqlite3.connect(bound['backup']) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT revision FROM works WHERE id=1').fetchone()[0] == 0
        assert backup.execute('SELECT count(*) FROM external_links').fetchone()[0] == 0
    assert call(agent_library, 'show', work_id=1)['sites'] == {'vndb': 'v17'}
    envelope, code = api.execute('bind', library=agent_library, work_id=1, ref='vndb:v17', revision=0, apply=True)
    assert code == 1 and envelope['error']['code'] == 'revision_conflict'
    assert len(list(Path(agent_library).parent.glob('*.bak'))) == 1


def test_lock_reads_and_dry_run_allowed(agent_library):
    with store.open_library(agent_library):
        assert call(agent_library, 'list')['total'] == 2
        assert not call(agent_library, 'set', work_id=1, category='日系')['applied']
        result, code = api.execute('set', library=agent_library, work_id=1, category='日系', apply=True)
        assert code == 1 and result['error']['code'] == 'library_locked'


def test_set_summary_unbind_revisions(agent_library):
    call(agent_library, 'bind', work_id=1, ref='vndb:v17', revision=0, apply=True)
    before = call(agent_library, 'show', work_id=1)
    assert not call(agent_library, 'set', work_id=1, category='同人', status='想玩')['applied']
    assert call(agent_library, 'show', work_id=1) == before
    assert call(agent_library, 'set', work_id=1, category='同人', status='想玩', revision=1, apply=True)['revision'] == 2
    assert call(agent_library, 'list', category='同人', status='想玩')['total'] == 1
    assert call(agent_library, 'summary', work_id=1, text=' My summary ', revision=2, apply=True)['revision'] == 3
    assert call(agent_library, 'show', work_id=1)['info']['summary_source'] == 'user'
    assert call(agent_library, 'unbind', work_id=1, site='vndb', revision=3, apply=True)['revision'] == 4
    assert call(agent_library, 'show', work_id=1)['sites'] == {}
    call(agent_library, 'set', work_id=1, category=None, status=None, revision=4, apply=True)
    assert call(agent_library, 'list', category=None)['total'] == 2


def test_batch_validates_all_and_backup_once(agent_library, tmp_path):
    operations = [{'command': 'set', 'work_id': 1, 'revision': 0, 'category': '日系'},
                  {'command': 'summary', 'work_id': 2, 'revision': 99, 'text': 'stale'},
                  {'command': 'set', 'work_id': 2, 'revision': 0, 'status': '玩过'}]
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(operations))
    result, code = api.execute('apply', library=agent_library, file=str(path))
    assert code == 1 and not list(tmp_path.glob('*.bak'))
    result, code = api.execute('apply', library=agent_library, file=str(path), apply=True)
    assert code == 1 and result['error']['code'] == 'partial_failure'
    assert [r['ok'] for r in result['data']['results']] == [True, False, True]
    assert len(list(tmp_path.glob('*.bak'))) == 1
    with sqlite3.connect(result['data']['backup']) as c:
        assert c.execute('SELECT sum(revision) FROM works').fetchone()[0] == 0
    before = Path(agent_library).read_bytes()
    operations[0]['revision'] = 1
    operations[-1]['status'] = 'invalid'
    result, code = api.execute('apply', library=agent_library, operations=operations, apply=True)
    assert code == 2 and Path(agent_library).read_bytes() == before


def test_cli_json_usage_and_environment(agent_library, monkeypatch, capsys):
    monkeypatch.setenv('GALPANEL2_LIBRARY', agent_library)
    for args in (['list'], ['show', '1', '--library', agent_library], ['--library', agent_library, 'status']):
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)['ok']
    for args in ([], ['unknown'], ['bind', '1', 'v17'], ['list', '--limit', '-1'], ['set', '1', '--category', 'wrong']):
        assert main(args) == 2
        assert not json.loads(capsys.readouterr().out)['ok']
    assert main(['set', '1', '--category', 'none']) == 0
    assert json.loads(capsys.readouterr().out)['data']['changes']['category']['after'] is None


def test_malformed_batch_and_backup_failure(agent_library, monkeypatch):
    before = Path(agent_library).read_bytes()
    for operations in ({}, [1], [{'command': 'set', 'work_id': 1, 'category': '日系', 'apply': True}]):
        result, code = api.execute('apply', library=agent_library, operations=operations, apply=True)
        assert code == 2 and not result['ok']
    monkeypatch.setattr(store.Library, 'backup', lambda *a: (_ for _ in ()).throw(OSError('full')))
    result, code = api.execute('set', library=agent_library, work_id=1, category='日系', apply=True)
    assert code == 2 and result['error']['code'] == 'environment_error'
    assert Path(agent_library).read_bytes() == before


def test_cli_subprocess(agent_library):
    proc = subprocess.run([sys.executable, '-m', 'galpanel2.agent', '--library', agent_library, 'show', '1'], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)['data']['id'] == 1
    assert not proc.stderr


def test_search_work_uses_title_and_codes(agent_library):
    result = call(agent_library, 'search', work=1, site='vndb')
    assert 'Example' in result['queries']
    assert any('RJ123456' in q for q in result['queries'])
    assert result['candidates'][0]['ref'] == 'vndb:v17'


def test_cover_success_and_batch_bind_unbind(agent_library, monkeypatch):
    rules_path = Path(os.environ['GALPANEL2_FAKE_SITES'])
    rules = json.loads(rules_path.read_text())
    cover = rules_path.parent / 'cover.png'
    cover.write_bytes(b'\x89PNG\r\n\x1a\nexample')
    rules['rules'][1] = {'method': 'GET', 'url_prefix': 'https://example.test/cover.png', 'body_file': cover.name}
    rules_path.write_text(json.dumps(rules))
    result = call(agent_library, 'apply', operations=[{'command': 'bind', 'work_id': 1, 'ref': 'vndb:v17', 'revision': 0}], apply=True)
    bound = result['results'][0]['data']
    assert bound['cover_error'] is None
    show = call(agent_library, 'show', work_id=1)
    assert Path(show['cover_path']).read_bytes() == cover.read_bytes()
    result = call(agent_library, 'apply', operations=[{'command': 'unbind', 'work_id': 1, 'site': 'vndb', 'revision': 1}], apply=True)
    assert result['results'][0]['data']['revision'] == 2
    assert call(agent_library, 'show', work_id=1)['sites'] == {}


@pytest.mark.parametrize('operation', [
    {'command': 'set', 'category': None, 'status': None},
    {'command': 'summary', 'text': '  '},
    {'command': 'unbind', 'site': 'vndb'},
])
def test_noop_never_writes_or_backs_up(agent_library, monkeypatch, operation):
    before = Path(agent_library).read_bytes()
    def forbidden(*a, **kw):
        pytest.fail('no-op must not write or back up')
    monkeypatch.setattr(store.Library, 'transaction', forbidden)
    monkeypatch.setattr(store.Library, 'backup', forbidden)
    for apply in (False, True):
        result = call(agent_library, operation['command'], work_id=1, revision=0,
                      apply=apply, **{k: v for k, v in operation.items() if k != 'command'})
        assert result['changed'] is False and result['applied'] is False
        assert result['revision'] == 0 and result['backup'] is None
    result = call(agent_library, 'apply', operations=[{**operation, 'work_id': 1, 'revision': 0}], apply=True)
    assert result['backup'] is None
    assert result['results'][0]['data']['changed'] is False
    assert Path(agent_library).read_bytes() == before
    assert not list(Path(agent_library).parent.glob('*.bak'))


def test_repeated_nonempty_values_and_mixed_batch(agent_library):
    call(agent_library, 'set', work_id=1, category='日系', status='想玩', apply=True)
    call(agent_library, 'summary', work_id=1, text='Existing', apply=True)
    before = Path(agent_library).read_bytes()
    backups = set(Path(agent_library).parent.glob('*.bak'))
    for command, args in [('set', {'category': '日系', 'status': '想玩'}),
                          ('summary', {'text': ' Existing '})]:
        result = call(agent_library, command, work_id=1, revision=2, apply=True, **args)
        assert result['changed'] is False and result['backup'] is None
    assert Path(agent_library).read_bytes() == before
    assert set(Path(agent_library).parent.glob('*.bak')) == backups
    result = call(agent_library, 'apply', operations=[
        {'command': 'set', 'work_id': 1, 'category': '日系'},
        {'command': 'set', 'work_id': 1, 'status': '玩过'},
        {'command': 'set', 'work_id': 1, 'status': '玩过'},
    ], apply=True)
    assert [r['data']['changed'] for r in result['results']] == [False, True, False]
    assert call(agent_library, 'show', work_id=1)['revision'] == 3
    assert len(set(Path(agent_library).parent.glob('*.bak')) - backups) == 1


def test_cli_forces_lf_stdout(agent_library, monkeypatch):
    import io
    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding='utf-8', newline='\r\n')
    monkeypatch.setattr(sys, 'stdout', stdout)
    assert main(['--library', agent_library, 'status']) == 0
    stdout.flush()
    assert buffer.getvalue().endswith(b'\n') and b'\r' not in buffer.getvalue()
    assert json.loads(buffer.getvalue())['ok']
