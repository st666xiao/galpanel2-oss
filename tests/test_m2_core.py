"""Offline M2 contracts, including failed nested writes and credential isolation."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import threading
import uuid
import pytest

from galpanel2.core import binding as B, records as R, sites as S, store, meta as M, domain as D, repository

FIX = Path(__file__).parent / 'fixtures/m2'

def data(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))

@pytest.fixture
def work(lib):
    with lib.transaction() as c:
        c.execute("INSERT INTO works(id,uuid,title,developer,added_at,updated_at) VALUES(1,?,'我的作品','保持原值',?,?)", (str(uuid.uuid4()), D.utc_now(), D.utc_now()))
        c.execute("INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(2,?,'另一本',?,?)", (str(uuid.uuid4()), D.utc_now(), D.utc_now()))
    return lib

@pytest.fixture
def rec():
    return R.parse_vndb(data('vndb_vn.json'))[0]

class Clock:
    def __init__(self): self.t = 1000.; self.sleeps = []
    def clock(self): return self.t
    def sleep(self, delay): self.t += delay; self.sleeps.append(delay)

class Transport:
    def __init__(self, responses): self.responses = iter(responses); self.calls = []
    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        return next(self.responses)


def client(responses, token=None, cancel=None):
    t, clock = Transport(responses), Clock()
    return S.SiteClient(t, token=token, limiter=S.RateLimiter(clock.clock, clock.sleep), cancel=cancel), t, clock

@pytest.mark.parametrize('value,site,sid', [
    ('rj297120','dlsite','RJ297120'), ('VJ01004386','dlsite','VJ01004386'), ('BJ01234567','dlsite','BJ01234567'),
    ('RE01234567','dlsite','RE01234567'), ('https://www.dlsite.com/maniax/work/=/product_id/RJ297120.html','dlsite','RJ297120'),
    ('V55281','vndb','v55281'), ('https://vndb.org/v42','vndb','v42'), ('bgm:186291','bangumi','186291'),
    ('BGM 12','bangumi','12'), ('https://chii.in/subject/1','bangumi','1'), ('https://bangumi.tv/subject/2','bangumi','2')])
def test_ref(value, site, sid): assert R.parse_ref(value) == R.Ref(site, sid)

@pytest.mark.parametrize('value', ['RJ12345','RJ123456789','xRJ123456','1999','v','bgm','普通书名',''])
def test_non_ref(value): assert R.parse_ref(value) is None

@pytest.mark.parametrize('title,want', [('Summer Days -pc','Summer Days'), ('Moon Trial','Moon'),
    ('乙女の誓い 体験版','乙女の誓い'), ('魔法少女セレスフォニア ver1.02','魔法少女セレスフォニア'), ('[Group]','[Group]')])
def test_search_text(title, want): assert R.search_text(title) == want

@pytest.mark.parametrize('tag', ['url=http://x','b','i','u','s','spoiler','quote','code','raw'])
def test_bbcode(tag): assert R.clean_bbcode(f'[{tag}]保留[/{tag.split("=")[0]}]') == '保留'

@pytest.mark.parametrize('field,value', [('released','2025'), ('image',None), ('description',None), ('titles',None), ('developers',None), ('tags',None)])
def test_nullable_vndb(field, value):
    raw = data('vndb_vn.json')['results'][0]
    raw[field] = value
    r = R.parse_vndb({'results':[raw]})[0]
    assert r.complete and json.loads(r.payload)[field] == value


def test_parser_content(rec):
    assert rec.translated_title == '寄宿少女的誓约与魔法'
    assert rec.skipped_tags == 12 and len(rec.tags) == 32
    assert R.tag_translation('Magic') == '魔法' and R.tag_translation('unknown') == 'unknown'
    assert all(t.category in ('cont','tech','ero') for t in rec.tags)
    assert R.parse_dlsite_suggest(data('dl_suggest.json'))[0].complete is False
    assert R.parse_dlsite_product(data('dl_product.json')).release_date == '2023-01-16'
    assert R.parse_bangumi_subject(data('bgm_subject.json')).tags == ()
    with pytest.raises(D.SiteNotFound): R.parse_dlsite_product([])


def test_rank_and_keys(rec):
    assert R.match_key('Ｓａｍｐｌｅ・ Works! †2') == 'sampleworks2'
    assert R.grade(rec.translated_title, rec) == ('ok',1.0)
    assert R.grade('Yadorishi Otome', rec) == ('near',.9)
    assert R.grade('', rec) == ('no',0.)
    rows = R.rank(rec.title, [replace(rec,site='dlsite'), rec, replace(rec,site='bangumi'),
                              replace(rec,site='steam'), replace(rec,site='f95')])
    assert [c.record.site for c in rows] == list(R.SITE_ORDER)

@pytest.mark.parametrize('version',[1,2,3])
def test_upgrade_and_readonly(tmp_path, version):
    path = tmp_path / 'old.galdb'
    with sqlite3.connect(path) as c:
        for n in range(1,version+1):
            c.executescript(Path(store.__file__).with_name('schema.sql' if n==1 else f'schema_v{n}.sql').read_text())
        c.execute(f'PRAGMA application_id={store.APPLICATION_ID}')
        c.execute(f'PRAGMA user_version={version}')
        c.execute('INSERT INTO library_info VALUES(?,?)',('library_uuid',str(uuid.uuid4())))
        c.execute("INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(1,?,'old','x','x')",(str(uuid.uuid4()),))
    before = path.read_bytes()
    with sqlite3.connect(path) as c: original_dump = list(c.iterdump())
    with pytest.raises(D.NeedsUpgrade) as e: store.open_library(str(path))
    assert e.value.version == version and before == path.read_bytes()
    assert not Path(str(path)+'.lock').exists()
    with store.open_library(str(path), True) as ro:
        assert B.get_info(ro,1).links == {} and B.list_titles(ro) == {}
        assert B.list_source_tags(ro) == []
        with pytest.raises(KeyError): B.get_info(ro,999)
    result = store.upgrade_library(str(path))
    assert result.from_version == version and result.to_version == 6
    with store.open_library(str(path)) as upgraded:
        assert upgraded.schema_version == 6 and repository.get_work(upgraded,1).title == 'old'
    with sqlite3.connect(result.backup_path) as c:
        assert list(c.iterdump()) == original_dump
        assert c.execute('PRAGMA user_version').fetchone()[0] == version
    with pytest.raises(ValueError,match='already version 6'): store.upgrade_library(str(path))

@pytest.mark.parametrize('status,exc', [(404,D.SiteNotFound),(401,D.SiteError),(403,D.SiteError),(500,D.SiteError),(200,D.SiteError)])
def test_status_errors(status, exc):
    c,t,_ = client([S.Response(status,b'bad',{})], token='SECRET')
    with pytest.raises(exc) as e: c.fetch(R.Ref('bangumi','1'))
    assert 'SECRET' not in str(e.value) and e.value.site == 'bangumi'

@pytest.mark.parametrize('site,ref,payload', [('dlsite',R.Ref('dlsite','RJ01014447'), data('dl_product.json')),
    ('vndb',R.Ref('vndb','v55281'),data('vndb_vn.json')), ('bangumi',R.Ref('bangumi','186291'),data('bgm_subject.json'))])
def test_requests_auth(site, ref, payload):
    c,t,_ = client([S.Response(200,json.dumps(payload).encode(),{})], token='SECRET')
    assert c.fetch(ref).site == site
    headers = t.calls[0][2]
    assert ('Authorization' in headers) == (site == 'bangumi')
    assert headers['User-Agent'] == S.USER_AGENT

@pytest.mark.parametrize('url', ['https://lain.bgm.tv/a','https://t.vndb.org/a','https://img.dlsite.jp/a'])
def test_image_no_auth(url):
    c,t,_ = client([S.Response(200,(FIX/'cover.png').read_bytes(),{})], token='SECRET')
    c.image(url)
    assert 'Authorization' not in t.calls[0][2]


def test_rate_and_retry_cancel():
    c,t,clock = client([S.Response(429,b'',{'Retry-After':'2'}),S.Response(200,json.dumps(data('vndb_vn.json')).encode(),{})])
    c.search('vndb','中文')
    assert len(t.calls)==2 and 2 in clock.sleeps and b'\\u' not in t.calls[-1][3]
    c,t,_ = client([],cancel=lambda:True)
    with pytest.raises(D.Cancelled): c.search_all('x')
    assert t.calls == []
    clock = Clock(); lim = S.RateLimiter(clock.clock, clock.sleep)
    lim.wait('api.vndb.org'); lim.wait('www.dlsite.com'); lim.wait('api.vndb.org')
    assert clock.sleeps == [1.5]

@pytest.mark.parametrize('bad',['','   ','a b','x\ty','x\x01','x\x7f','x\x80','x\u202e'])
def test_token_validation(tmp_path, monkeypatch, bad):
    monkeypatch.setenv('GALPANEL2_SETTINGS_DIR',str(tmp_path))
    S.write_token('GOOD')
    with pytest.raises(ValueError): S.write_token(bad)
    assert S.read_token() == 'GOOD'


def test_token_bom_and_file_log(tmp_path, monkeypatch):
    monkeypatch.setenv('GALPANEL2_SETTINGS_DIR',str(tmp_path/'settings'))
    assert S.read_token() is None
    S.write_token(' SECRET ')
    S.token_path().write_bytes(b'\xef\xbb\xbfSECRET\r\n')
    assert S.read_token() == 'SECRET'
    path = tmp_path/'rules.json'; path.write_text(json.dumps({'rules':[{'method':'GET','url_prefix':'https://x/','json':{}}]}))
    monkeypatch.setenv('GALPANEL2_FAKE_SITES',str(path))
    transport = S.default_transport()
    transport.request('GET','https://x/',{'Authorization':'Bearer SECRET'},None,1)
    assert 'SECRET' not in Path(str(path)+'.log').read_text()
    assert json.loads(Path(str(path)+'.log').read_text())['auth'] is True
    S.clear_token(); S.clear_token()
    assert S.read_token() is None


def rev(lib,wid=1): return repository.get_work(lib,wid).revision

def dump(lib):
    with lib.connection() as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t:[tuple(r) for r in c.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}


def test_plan_readonly_and_bind(work, rec):
    before = dump(work); plan = B.plan_bind(work,1,rec)
    assert dump(work) == before and plan.tags_pending == 32 and plan.cover == 'set'
    B.apply_bind(work,1,rev(work),rec,rec.translated_title,(FIX/'cover.png').read_bytes())
    info = B.get_info(work,1)
    assert info.summary_source == 'vndb' and info.cover_source == 'vndb'
    assert info.links == {'vndb':'v55281'} and info.titles[0][1] == 'original'
    assert repository.get_work(work,1).developer == '保持原值'
    assert repository.get_work(work,1).title == rec.translated_title
    assert B.list_links(work)[1] == info.links and rec.title in B.list_titles(work)[1]

@pytest.mark.parametrize('failure',['revision','duplicate','title','image','incomplete'])
def test_bind_atomic_failures(work, rec, failure):
    if failure=='duplicate': B.apply_bind(work,2,rev(work,2),rec)
    before = dump(work)
    args = dict(expected_revision=rev(work),record=rec)
    exc = ValueError
    if failure=='revision': args['expected_revision']-=1; exc=D.WriteConflict
    if failure=='duplicate': exc=D.DuplicateLink
    if failure=='title': args['new_title']='not allowed'
    if failure=='image': args['cover_bytes']=b'broken'
    if failure=='incomplete': args['record']=replace(rec,complete=False)
    with pytest.raises(exc): B.apply_bind(work,1,**args)
    assert dump(work) == before


def test_user_lock_priority_and_unbind(work, rec):
    B.apply_bind(work,1,rev(work),rec)
    B.set_summary(work,1,None)
    bg = R.parse_bangumi_subject(data('bgm_subject.json'))
    assert B.plan_bind(work,1,bg).summary == 'locked'
    B.apply_bind(work,1,rev(work),bg)
    assert B.get_info(work,1).summary is None and B.get_info(work,1).release_source == 'vndb'
    revision=rev(work); B.unbind(work,1,'vndb')
    assert rev(work)==revision and B.get_info(work,1).titles and B.get_info(work,1).links=={'bangumi':'186291'}
    with pytest.raises(KeyError): B.unbind(work,1,'vndb')


def test_mapping_blocks_merge_delete(work, rec):
    B.apply_bind(work,1,rev(work),rec)
    magic=M.create_tag(work,'魔法',1)
    rows=B.list_source_tags(work); row=next(r for r in rows if r.tag_key=='g4')
    assert row.suggested_tag_id == magic and row.suggested_action=='map'
    B.block_tags(work,1,[magic]); B.block_tags(work,1,[magic])
    result=B.apply_tag_decisions(work,[B.TagDecision('vndb','g4','map',magic)])
    assert result.mapped==1 and result.works_tagged==0
    assert B.plan_bind(work,1,rec).tags_blocked==1
    B.unblock_tags(work,1,[magic]); result=B.apply_tag_decisions(work,[B.TagDecision('vndb','g4','map',magic)])
    assert result.works_tagged==1
    other=M.create_tag(work,'目标',1); B.block_tags(work,1,[magic,other]); M.merge_tags(work,magic,other)
    assert next(r for r in B.list_source_tags(work) if r.tag_key=='g4').tag_id==other
    M.delete_tag(work,other)
    assert next(r for r in B.list_source_tags(work) if r.tag_key=='g4').status=='pending'

@pytest.mark.parametrize('bad',['duplicate','unknown','tag','group','existing','action'])
def test_mapping_batch_rollback(work, rec, bad):
    B.apply_bind(work,1,rev(work),rec)
    first=B.TagDecision('vndb','g4','new',name='新建测试',group_id=1)
    second={'duplicate':first,'unknown':B.TagDecision('vndb','none','ignore'),
            'tag':B.TagDecision('vndb','g23','map',999), 'group':B.TagDecision('vndb','g23','new',name='名字',group_id=999),
            'existing':B.TagDecision('vndb','g23','new',name='已有',group_id=1), 'action':B.TagDecision('vndb','g23','bad')}[bad]
    M.create_tag(work,'已有',1); before=dump(work)
    with pytest.raises((ValueError,KeyError,D.DuplicateName)): B.apply_tag_decisions(work,[first,second])
    assert dump(work)==before


def test_new_same_batch_and_nested_rollback(work,rec):
    B.apply_bind(work,1,rev(work),rec)
    result=B.apply_tag_decisions(work,[B.TagDecision('vndb','g4','new',name='同一个',group_id=1), B.TagDecision('vndb','g23','new',name='同一个',group_id=4)])
    assert result.created_tags==1 and result.mapped==2 and result.works_tagged==1
    before=dump(work)
    with work.transaction():
        with pytest.raises(ValueError): B.apply_bind(work,1,rev(work),replace(rec,site_id='v999'),cover_bytes=b'bad')
    assert dump(work)==before


def test_replacement_and_brand_links(work,rec):
    bid=M.create_brand(work,'アンサンブル')
    assert B.plan_bind(work,1,rec).brands==(('アンサンブル',bid,'existing'),)
    B.apply_bind(work,1,rev(work),rec)
    changed=replace(rec,site_id='v999',brands=(replace(rec.brands[0],name='changed'),),tags=())
    assert B.plan_bind(work,1,changed).replaces=='v55281'
    B.apply_bind(work,1,rev(work),changed)
    with work.connection() as c:
        assert c.execute('SELECT count(*) FROM work_source_tags WHERE work_id=1').fetchone()[0]==0
    target=M.create_brand(work,'品牌目标'); M.merge_brands(work,bid,target)
    with work.connection() as c: assert c.execute('SELECT brand_id FROM brand_links').fetchone()[0]==target


@pytest.mark.parametrize('mode',['cover','no-cover','image-error'])
def test_cli_bind_clean_library_and_duplicate(work,tmp_path,monkeypatch,capsys,mode):
    from galpanel2.core.__main__ import main
    rules=[{'method':'GET','url_prefix':S.DLSITE_PRODUCT.split('{section}')[0],'json':data('dl_product.json')},
           {'method':'POST','url_prefix':S.VNDB_API,'json':data('vndb_vn.json')},
           {'method':'GET','url_prefix':'https://','body_file':'cover.png','status':500 if mode=='image-error' else 200}]
    (tmp_path/'cover.png').write_bytes((FIX/'cover.png').read_bytes())
    rp=tmp_path/'rules.json';rp.write_text(json.dumps({'rules':rules},ensure_ascii=False),encoding='utf-8')
    monkeypatch.setenv('GALPANEL2_FAKE_SITES',str(rp))
    monkeypatch.setenv('GALPANEL2_SETTINGS_DIR',str(tmp_path/'settings'))
    clock=Clock();monkeypatch.setattr(S,'DEFAULT_LIMITER',S.RateLimiter(clock.clock,clock.sleep))
    def forbidden(*args,**kwargs):raise AssertionError('Unexpected real transport')
    monkeypatch.setattr(S.UrllibTransport,'request',forbidden)
    path=work.path;work.close()
    def cli(*args):
        code=main([str(a) for a in args]);return code,json.loads(capsys.readouterr().out)
    args=['bind',path,1,'RJ01014447','--title','original']+(['--no-cover'] if mode=='no-cover' else [])
    code,result=cli(*args);assert code==0 and result['site_id']=='RJ01014447'
    assert bool(result['cover_error'])==(mode=='image-error')
    with store.open_library(path,readonly=True) as lib:
        assert repository.get_work(lib,1).title==R.parse_dlsite_product(data('dl_product.json')).title
        assert bool(repository.get_work(lib,1).cover_path)==(mode=='cover')
        before=dump(lib)
    code,error=cli('bind',path,2,'RJ01014447');assert code==2 and error['error']=='DuplicateLink'
    with store.open_library(path,readonly=True) as lib:assert dump(lib)==before
    code,result=cli('bind',path,2,'v55281','--title','translated','--no-cover')
    assert code==0 and result['site']=='vndb'
    before=Path(path).read_bytes();code,result=cli('info',path,2)
    assert code==0 and result['summary_source']=='vndb' and Path(path).read_bytes()==before
    rows=[json.loads(s) for s in Path(str(rp)+'.log').read_text(encoding='utf-8').splitlines()]
    assert rows and not any(r['auth'] for r in rows)
