"""Offscreen M2 interactions; all requests resolve to temporary fake rules."""
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
import uuid

import pytest
from PySide6.QtWidgets import QLineEdit
from galpanel2.core import store, repository as repo, binding as B, sites as S, records as R, meta as M, domain as D
from galpanel2.ui.library import Editor
from galpanel2.ui.window import Window
from galpanel2.ui.workers import NetJob
from test_ui import app, window, wait
from test_m2_core import dump, Clock, FIX, data

@pytest.fixture(autouse=True)
def fake_sites(tmp_path,monkeypatch):
    # A missed rule fails locally instead of ever falling through to urllib.
    def forbidden(*args,**kwargs):raise AssertionError('Unexpected real transport')
    monkeypatch.setattr(S.UrllibTransport,'request',forbidden)
    clock=Clock(); monkeypatch.setattr(S,'DEFAULT_LIMITER',S.RateLimiter(clock.clock,clock.sleep))
    dl=data('dl_product.json'); dl[0].update(workno='RJ297120',work_name='魔法少女セレスフォニア',maker_id='RG39138',maker_name='しもばしら工房',maker_name_en='Shimobashira Koubou',regist_date='2020-09-12 00:00:00',work_type_string='角色扮演')
    dl[0]['image_main']['url']='//img.dlsite.jp/a.png'
    image=tmp_path/'cover.png';image.write_bytes((FIX/'cover.png').read_bytes())
    rules=[{'method':'GET','url_prefix':'https://www.dlsite.com/suggest/','json':data('dl_suggest.json')},
           {'method':'GET','url_prefix':'https://www.dlsite.com/maniax/api/','json':dl},
           {'method':'POST','url_prefix':S.VNDB_API,'body_contains':'宿りし乙女','json':data('vndb_vn.json')},
           {'method':'POST','url_prefix':S.VNDB_API,'body_contains':'v55281','json':data('vndb_vn.json')},
           {'method':'POST','url_prefix':S.VNDB_API,'json':{'results':[]}},
           {'method':'POST','url_prefix':S.BGM_SEARCH,'body_contains':'サクラノモリ','json':data('bgm_search.json')},
           {'method':'POST','url_prefix':S.BGM_SEARCH,'json':{'data':[]}},
           {'method':'GET','url_prefix':'https://api.bgm.tv/v0/subjects/','json':data('bgm_subject.json')}]
    for host in ('t.vndb.org','lain.bgm.tv','img.dlsite.jp'):
        rules.append({'method':'GET','url_prefix':f'https://{host}/','body_file':'cover.png'})
    path=tmp_path/'rules.json';path.write_text(json.dumps({'rules':rules},ensure_ascii=False),encoding='utf-8')
    monkeypatch.setenv('GALPANEL2_FAKE_SITES',str(path))
    return path

@pytest.fixture
def m2_library(tmp_path):
    path=tmp_path/'m2.galdb'
    with store.create_library(str(path)) as lib:
        with lib.transaction() as c:
            for i,title in enumerate(('宿りし乙女の誓いと魔法','魔法少女セレスフォニア ver1.02','サクラノモリ†ドリーマーズ 2','另一本作品'),1):
                c.execute("INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(?,?,?,'2020-01-01','2020-01-01')",(i,str(uuid.uuid4()),title))
        M.create_brand(lib,'アンサンブル');magic=M.create_tag(lib,'魔法',1)
    return path,magic


def open_window(window,app,fx,select=1):
    window.open_library_path(str(fx[0]),select);wait(app,window)
    return window.library_page


def find(window,app,wid=1):
    window.open_find(wid);wait(app,window)
    return window.find_dialog


def selected(window,app,d,i=0):
    d.select(i);wait(app,window)
    assert d.current_plan is not None
    return d.current_plan

@pytest.mark.parametrize('wid,site,sid',[(1,'vndb','v55281'),(2,'dlsite','RJ297120'),(3,'bangumi','186291')])
def test_search_rank(window,app,m2_library,wid,site,sid):
    open_window(window,app,m2_library,wid);d=find(window,app,wid)
    assert d.sites()==R.SITES and d.candidates()[0].record.site==site
    assert d.candidates()[0].record.site_id==sid and d.candidates()[0].grade=='ok'


def test_dlsite_complete_fetch_is_readonly(window,app,m2_library):
    open_window(window,app,m2_library,2);before=dump(window.lib)
    d=find(window,app,2);assert not d.candidates()[0].record.complete
    selected(window,app,d)
    assert d.candidates()[0].record.complete and '2020-09-12' in d.preview_text()
    assert dump(window.lib)==before

@pytest.mark.parametrize('text,site',[('v55281','vndb'),('bgm:186291','bangumi'),('RJ297120','dlsite')])
def test_ref_bypasses_switches(window,app,m2_library,text,site):
    open_window(window,app,m2_library);d=find(window,app)
    for s in R.SITES:d.set_site(s,False)
    d.query.setText(text);d.search();wait(app,window)
    assert len(d.candidates())==1 and d.candidates()[0].record.site==site

@pytest.mark.parametrize('fragment',['v55281','アンサンブル','并入已有品牌','2025-07-25','作品还没有封面','＋ 32 个没对照过','略过 12 个'])
def test_preview_rows(window,app,m2_library,fragment):
    open_window(window,app,m2_library);d=find(window,app);selected(window,app,d)
    assert fragment in d.preview_text()

@pytest.mark.parametrize('rename',[False,True])
def test_bind_confirm(window,app,m2_library,rename):
    open_window(window,app,m2_library);d=find(window,app);selected(window,app,d)
    if rename:d.title_choice.setCurrentIndex(1)
    d.bind();wait(app,window)
    assert not d.isVisible()
    info=B.get_info(window.lib,1)
    assert info.links=={'vndb':'v55281'} and info.cover_source=='vndb'
    assert repo.get_work(window.lib,1).title==('寄宿少女的誓约与魔法' if rename else '宿りし乙女の誓いと魔法')


def test_bind_failed_rolls_back(window,app,m2_library,monkeypatch):
    open_window(window,app,m2_library);d=find(window,app);selected(window,app,d);before=dump(window.lib)
    original=B.apply_bind
    def fail(lib,*args,**kwargs):
        with lib.transaction():
            original(lib,*args,**kwargs)
            raise OSError('Injected write failure')
    monkeypatch.setattr(B,'apply_bind',fail)
    d.bind();wait(app,window)
    assert d.isVisible() and 'Injected' in d.message.text() and dump(window.lib)==before


def test_conflict_repreviews(window,app,m2_library):
    open_window(window,app,m2_library);d=find(window,app);selected(window,app,d)
    w=repo.get_work(window.lib,1);repo.edit_work(window.lib,1,w.revision,notes='other edit')
    d.bind();wait(app,window)
    assert d.isVisible() and '刚被别处改过' in d.message.text()
    assert d.revision==repo.get_work(window.lib,1).revision and B.get_info(window.lib,1).links=={}


def test_taken_and_unbind(window,app,m2_library):
    with store.open_library(str(m2_library[0])) as lib:B.apply_bind(lib,1,repo.get_work(lib,1).revision,R.parse_vndb(data('vndb_vn.json'))[0])
    open_window(window,app,m2_library,4);d=find(window,app,4)
    d.query.setText('v55281');d.search();wait(app,window);selected(window,app,d)
    assert d.current_plan.taken_by==1 and not d.bind_button.isEnabled()
    before=dump(window.lib);d.bind();assert dump(window.lib)==before
    window.open_find(1);wait(app,window);d.unbind('vndb');wait(app,window)
    assert 'v55281' not in d.bound_text() and B.get_info(window.lib,1).summary


def test_image_failure_still_binds(window,app,m2_library,monkeypatch):
    open_window(window,app,m2_library);d=find(window,app);selected(window,app,d)
    def fail(*args):raise D.SiteError('fake image error')
    monkeypatch.setattr(S.SiteClient,'image',fail);d.bind();wait(app,window)
    assert B.get_info(window.lib,1).links and repo.get_work(window.lib,1).cover_path is None
    assert '封面没下载下来' in window.status.message.text()


def test_no_network_open_and_search_titles(window,app,m2_library,fake_sites):
    with store.open_library(str(m2_library[0])) as lib:B.apply_bind(lib,1,1,R.parse_vndb(data('vndb_vn.json'))[0])
    page=open_window(window,app,m2_library)
    for wid in (1,2,3):page.select_work(wid)
    assert not Path(str(fake_sites)+'.log').exists()
    for query in ('寄宿少女','yadorishi'):
        page.search.setText(query);assert page.visible_work_ids()==[1]


def test_detail_urls_and_summary(window,app,m2_library,monkeypatch):
    with store.open_library(str(m2_library[0])) as lib:B.apply_bind(lib,1,1,R.parse_vndb(data('vndb_vn.json'))[0])
    page=open_window(window,app,m2_library);urls=[];monkeypatch.setattr(window,'open_external',urls.append)
    assert '简介' in page.detail.facts.text() and '发售' in page.detail.facts.text() \
           and '2025-07-25' in page.detail.facts.text()
    page.detail.activate_link('site:vndb');assert urls==['https://vndb.org/v55281']
    page.detail.activate_link('find:');wait(app,window);assert window.find_dialog.work_id==1


def bound_magic(path,magic):
    with store.open_library(str(path)) as lib:
        B.apply_bind(lib,1,1,R.parse_vndb(data('vndb_vn.json'))[0])
        B.apply_tag_decisions(lib,[B.TagDecision('vndb','g4','map',magic)])


def test_editor_summary_and_block(window,app,m2_library):
    path,magic=m2_library;bound_magic(path,magic);open_window(window,app,m2_library)
    e=Editor(window,repo.get_work(window.lib,1));e.show()
    assert e.summary.toPlainText().startswith('After fixing up')
    e.summary.setPlainText('手动简介');e.tag_input.remove_existing(magic);e.save();wait(app,window)
    assert B.get_info(window.lib,1).summary_source=='user'
    with window.lib.connection() as c:assert list(c.execute('SELECT tag_id FROM work_tag_blocks'))[0][0]==magic
    r=R.parse_vndb(data('vndb_vn.json'))[0];B.apply_bind(window.lib,1,repo.get_work(window.lib,1).revision,r)
    assert B.get_info(window.lib,1).summary=='手动简介' and magic not in M.list_work_meta(window.lib)[1].tag_ids


def test_editor_atomic_rollback(window,app,m2_library,monkeypatch):
    path,magic=m2_library;bound_magic(path,magic);open_window(window,app,m2_library)
    e=Editor(window,repo.get_work(window.lib,1));e.show();e.summary.setPlainText('应该回滚');e.tag_input.remove_existing(magic)
    before=dump(window.lib)
    def fail(*args):raise ValueError('block failed')
    monkeypatch.setattr(B,'block_tags',fail);e.save();wait(app,window)
    assert e.isVisible() and dump(window.lib)==before and 'block failed' in e.message.text()
    e.close()


def test_tag_mapping_defaults_and_apply(window,app,m2_library):
    path,magic=m2_library;bound_magic(path,magic);open_window(window,app,m2_library)
    window.open_tag_mapping();wait(app,window);t=window.tag_mapping
    rows=t.rows();assert len(rows)==31 and t.only_pending.isChecked()
    for r in rows:t.set_row(r.site,r.tag_key,'pending')
    t.set_row('vndb','g260','new',name='魔法少女',group_id=1);t.set_row('vndb','g23','ignore');t.apply();wait(app,window)
    assert len(t.rows())==29 and '已对照 2' in t.footer_text() and '忽略 1' in t.footer_text()


def test_tag_mapping_failed_whole_batch(window,app,m2_library):
    path,magic=m2_library;bound_magic(path,magic);open_window(window,app,m2_library)
    window.open_tag_mapping();wait(app,window);t=window.tag_mapping
    for r in t.rows():t.set_row(r.site,r.tag_key,'pending')
    t.set_row('vndb','g260','new',name='新标签',group_id=1);t.set_row('vndb','g23','new',name='魔法',group_id=1)
    before=dump(window.lib);t.apply();wait(app,window)
    assert dump(window.lib)==before and t.message.text()


def test_settings_local_token(window,app):
    d=window.make_settings_dialog();assert d.token_input.text()=='' and d.token_state.text()=='未填'
    assert d.token_input.echoMode()==QLineEdit.EchoMode.Password
    d.token_input.setText('  tok-123  ');d.save();assert S.read_token()=='tok-123'
    d=window.make_settings_dialog();assert d.token_input.text()=='' and d.token_state.text()=='已保存'
    settings_path=S.settings_dir()/'settings.json';before=settings_path.read_bytes()
    d.token_input.setText('a b');d.theme.setCurrentIndex(1);d.save()
    assert d.message.text()=='token 格式不对' and S.read_token()=='tok-123' and settings_path.read_bytes()==before
    assert b'tok-123' not in before
    d.clear_token();assert S.read_token() is None and d.token_state.text()=='未填'


def test_settings_screenshot_no_files(app,tmp_path,monkeypatch):
    sd=tmp_path/'screen-settings';monkeypatch.setenv('GALPANEL2_SETTINGS_DIR',str(sd))
    w=Window(screenshot_mode=True);w.show();wait(app,w)
    d=w.make_settings_dialog();d.token_input.setText('fake-token');d.save();d.clear_token()
    assert not sd.exists();w.close();app.processEvents()


def test_settings_token_value_error_leaves_settings(window,app,monkeypatch):
    d=window.make_settings_dialog();d.show();before=dict(window.settings.data)
    def invalid(token):raise ValueError('invalid')
    monkeypatch.setattr(S,'write_token',invalid)
    d.token_input.setText('valid-looking');d.theme.setCurrentIndex(1);d.save()
    assert d.isVisible() and d.message.text()=='token 格式不对'
    assert window.settings.data==before and S.read_token() is None


def test_detail_long_summary_does_not_overlap_hero(window,app,m2_library):
    with store.open_library(str(m2_library[0])) as lib:
        B.set_summary(lib,1,'A long story with words and line breaks.\n'*30)
    page=open_window(window,app,m2_library);detail=page.detail
    # E3: the detail is a two-column full page; the summary now shows up to 12
    # lines (was 4) and the old "summary sits below the hero / above the
    # bottom button row" geometry no longer applies.
    assert detail.summary.max_lines == 12
    assert detail.back_button.text().startswith("←")
    assert detail.start_button.text() == "▶ 开始游戏"
    assert ('A long story'*1) in detail.facts.toolTip()


def test_net_job_does_not_lock_ui(window,app):
    release=threading.Event()
    job=window.run_net('fake',lambda cancel:release.wait(2),lambda value:None)
    assert isinstance(job,NetJob) and not window.busy and window.pages.isEnabled()
    assert job in window.net_jobs and window.pending_jobs>=1
    release.set();wait(app,window);assert not window.net_jobs


def test_net_cancel_close(window,app):
    started=threading.Event()
    def task(cancel):
        started.set()
        while not cancel():time.sleep(.001)
        raise D.Cancelled('cancelled')
    window.run_net('fake',task,lambda value:None)
    assert started.wait(2)
    window.close();app.processEvents();wait(app,window)
    assert not window.isVisible() and window.lib is None


def test_switch_work_drops_old_result(window,app,m2_library,monkeypatch):
    open_window(window,app,m2_library);release=threading.Event();original=S.SiteClient.search_all
    def delayed(self,query,sites=R.SITES):
        if '宿りし乙女' in query:release.wait(2)
        return original(self,query,sites)
    monkeypatch.setattr(S.SiteClient,'search_all',delayed)
    window.open_find(1);app.processEvents();window.open_find(2);release.set();wait(app,window)
    assert window.find_dialog.work_id==2 and window.find_dialog.candidates()[0].record.site_id=='RJ297120'


def test_readonly_find_and_mapping(app,m2_library,tmp_path):
    w=Window(screenshot_mode=True);w.show();w.open_library_path(str(m2_library[0]),1);wait(app,w)
    d=find(w,app);selected(w,app,d)
    assert not d.bind_button.isEnabled() and '只读' in d.message.text()
    before=dump(w.lib);d.bind();assert dump(w.lib)==before
    d.close();w.open_tag_mapping();wait(app,w);assert not w.tag_mapping.apply_button.isEnabled()
    w.close();app.processEvents()
