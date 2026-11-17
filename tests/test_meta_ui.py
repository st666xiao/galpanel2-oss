"""Offscreen metadata flows, temporary libraries and deterministic failed saves."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QMessageBox
from galpanel2.core import store, meta as M, repository as R, domain as D
from galpanel2.ui.library import Editor, matches_filter, filter_counts, load_projection
from galpanel2.ui.metawidgets import MetaChip, TokenInput
from galpanel2.ui import metadialogs
from galpanel2.ui.workers import Job
from test_ui import app, window, wait
from test_meta_core import snapshot
from test_p2_core import v1


@pytest.fixture
def meta_library(tmp_path):
    path=tmp_path/'meta.galdb'
    with store.create_library(str(path)) as lib:
        with lib.transaction() as c:
            for i,dev in enumerate(('Sample','Sample','Other','WINDOWS','[Gearbox]',''),1):
                c.execute('INSERT INTO works(id,uuid,title,developer,notes,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                          (i,str(uuid.uuid4()),f'Story {i}',dev,'memo' if i==3 else '',int(i==6),'2020-01-01','2020-01-01'))
        a=M.create_brand(lib,'Sample',['サンプル']);b=M.create_brand(lib,'Lantern',['ランタン'])
        fan=M.create_tag(lib,'奇幻',1);school=M.create_tag(lib,'校园',1);pix=M.create_tag(lib,'像素',3)
        for wid,bs,ts in ((1,[a],[fan,school]),(2,[a,b],[fan,pix]),(3,[b],[fan,school]),(4,[],[]),(5,[],[pix])):
            M.set_work_meta(lib,wid,1,bs,ts)
    return path,a,b,fan,school,pix


def opened(window,app,fx):
    window.open_library_path(str(fx[0]));wait(app,window)
    assert window.lib is not None
    return window.library_page


def test_click_cycle_conditions_counts_and_clear(window,app,meta_library):
    p=opened(window,app,meta_library);path,a,b,fan,school,pix=meta_library
    p.brand_button.click();app.processEvents()
    chip=next(x for x in p.brand_popover.findChildren(MetaChip) if x.text()=='Sample' and x.isVisible())
    chip.click();app.processEvents()
    assert p.meta_filter.brands_in=={a} and p.cond_text()=='品牌 Sample' and sorted(p.visible_work_ids())==[1,2]
    assert p.brand_popover.counts=={a:2,b:2,0:2}
    p.cycle_meta('brand',a);assert p.meta_filter.brands_out=={a} and p.cond_text()=='品牌 不含 Sample'
    p.cycle_meta('brand',a);assert p.meta_filter.is_empty()
    p.set_meta_state('brand',b,'in');p.set_meta_state('brand',a,'in')
    p.set_meta_state('tag',school,'in');p.set_meta_state('tag',fan,'in');p.set_meta_state('tag',pix,'out')
    assert p.cond_text()=='品牌 Lantern 或 Sample · 标签 校园 且 奇幻 不含 像素'
    assert sorted(p.visible_work_ids())==[1,3] and p.tag_button.text()=='3 个'
    p.search.setText('Story 1');p.clear_meta_filter()
    assert p.search.text()=='Story 1' and sorted(p.visible_work_ids())==[1] and p.cond_bar.isHidden()


@pytest.mark.parametrize('query,want',[('ランタン',[2,3]),('校园',[1,3]),('Sample',[1,2]),('memo',[3]),('WINDOWS',[4]),('missing',[])])
def test_search_alias_tags_notes_and_developer(window,app,meta_library,query,want):
    p=opened(window,app,meta_library);p.search.setText(query)
    assert sorted(p.visible_work_ids())==want


def test_intersection_union_exclusions_and_zero_brand(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    p.set_meta_state('brand',a,'in');p.set_meta_state('brand',b,'in')
    assert sorted(p.visible_work_ids())==[1,2,3]
    p.set_meta_state('tag',fan,'in');p.set_meta_state('tag',school,'in')
    assert sorted(p.visible_work_ids())==[1,3]
    p.set_meta_state('brand',b,'out');assert sorted(p.visible_work_ids())==[1]
    p.clear_meta_filter();p.set_meta_state('brand',0,'in');assert sorted(p.visible_work_ids())==[4,5]
    assert p.cond_text()=='品牌 （无品牌）'
    p.cycle_meta('brand',0);assert sorted(p.visible_work_ids())==[1,2,3]


def test_pending_rule_compatibility_and_chip_independence(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    first=p.works[0];with_cover=replace(first,has_cover=True)
    assert not matches_filter(with_cover,'待整理')
    assert not matches_filter(with_cover,'待整理',p.metas)
    assert matches_filter(with_cover,'待整理',{first.id:M.WorkMeta(first.id,(),())})
    counts=dict(p.counts);p.set_meta_state('tag',fan,'in')
    assert p.counts==counts
    p.set_filter('待整理');assert p.counts['待整理']==5


def test_detail_links_byline_and_old_text(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    p.select_work(2);d=p.detail
    assert d.byline_text()=='Sample、Lantern' and '标签' in d.facts.text()
    d.activate_link(f'brand:{b}');d.activate_link(f'tag:{fan}')
    assert p.meta_filter.brands_in=={b} and p.meta_filter.tags_in=={fan}
    p.clear_meta_filter();p.select_work(5)
    assert d.byline_text()=='[Gearbox]' and '旧社团' in d.facts.text() \
           and '还没整理成品牌' in d.facts.text()
    p.select_work(4);assert '还没有标签' in d.facts.text() and '未绑定' in d.facts.text() and '找资料' in d.facts.text()


def test_reload_prunes_deleted_conditions_preserves_others(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    p.set_meta_state('tag',pix,'out');p.set_meta_state('tag',fan,'in');p.set_meta_state('brand',b,'in')
    M.delete_tag(window.lib,pix);window.reload_library();wait(app,window)
    assert not p.meta_filter.tags_out and p.meta_filter.tags_in=={fan} and p.meta_filter.brands_in=={b}


def test_popover_alias_search_zero_counts_outside_click(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    p.brand_button.click();app.processEvents();pop=p.brand_popover
    assert pop.isVisible() and pop.width()==520
    pop.search.setText('ランタン');app.processEvents()
    visible=[x.text() for x in pop.findChildren(MetaChip) if x.isVisible()]
    assert visible==['Lantern']
    QTest.mouseClick(p.filter_button,Qt.MouseButton.LeftButton);app.processEvents()
    assert not pop.isVisible()


def test_open_popover_reuses_chips_and_updates_counts(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    p.tag_button.click();app.processEvents();pop=p.tag_popover
    chips=dict(pop.chips)
    containers=[pop.content_layout.itemAt(i).widget() for i in range(pop.content_layout.count()) if pop.content_layout.itemAt(i).widget()]
    def assert_reused():
        assert list(pop.chips)==list(chips)
        assert all(pop.chips[i] is chip for i,chip in chips.items())
        current=[pop.content_layout.itemAt(i).widget() for i in range(pop.content_layout.count()) if pop.content_layout.itemAt(i).widget()]
        assert len(current)==len(containers) and all(a is b for a,b in zip(current,containers))
    chip=pop.chips[school]
    p.set_meta_state('tag',fan,'in');app.processEvents()
    assert_reused();assert chip.count == 2
    chip.click();app.processEvents()
    assert_reused();assert chip.state == 'in'
    assert sorted(p.visible_work_ids())==[1,3]
    pop.search.setText('像素');app.processEvents()
    assert list(pop.chips) == [pix]


def test_open_popover_rebuilds_when_tags_change_on_reload(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    p.tag_button.click();app.processEvents();pop=p.tag_popover
    new=M.create_tag(window.lib,'悬疑',1);window.reload_library();wait(app,window)
    assert pop.isVisible() and pop.chips[new].text()=='悬疑'
    M.rename_tag(window.lib,new,'推理');window.reload_library();wait(app,window)
    assert pop.isVisible() and pop.chips[new].text()=='推理'
    M.delete_tag(window.lib,school);window.reload_library();wait(app,window)
    assert pop.isVisible() and school not in pop.chips
    assert {chip.text() for chip in pop.chips.values()}=={'奇幻','像素','推理'}


def test_open_popover_reload_counts_reuses_chips_and_context_uses_current_item(window,app,meta_library,monkeypatch):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    p.brand_button.click();app.processEvents();pop=p.brand_popover;chips=dict(pop.chips)
    containers=[pop.content_layout.itemAt(i).widget() for i in range(pop.content_layout.count()) if pop.content_layout.itemAt(i).widget()]
    w=R.get_work(window.lib,1);M.set_work_meta(window.lib,1,w.revision,[],[fan,school])
    window.reload_library();wait(app,window)
    assert pop.isVisible() and all(pop.chips[i] is chip for i,chip in chips.items())
    current=[pop.content_layout.itemAt(i).widget() for i in range(pop.content_layout.count()) if pop.content_layout.itemAt(i).widget()]
    assert len(current)==len(containers) and all(a is b for a,b in zip(current,containers))
    seen=[];monkeypatch.setattr(p,'meta_context',lambda kind,item,pos:seen.append(item))
    chips[a].customContextMenuRequested.emit(QPoint(0,0))
    assert seen==[p.brands[a]] and seen[0].work_count==1


def test_open_popovers_rebuild_for_alias_and_group_changes(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    p.brand_button.click();app.processEvents();pop=p.brand_popover;chip=pop.chips[a]
    M.set_brand_aliases(window.lib,a,['新别名']);window.reload_library();wait(app,window)
    assert pop.isVisible() and pop.chips[a] is not chip
    pop.search.setText('新别名');app.processEvents()
    assert pop.isVisible() and list(pop.chips)==[a]
    p.tag_button.click();app.processEvents();pop=p.tag_popover;chip=pop.chips[fan]
    M.set_tag_group(window.lib,fan,3);window.reload_library();wait(app,window)
    assert pop.isVisible() and pop.chips[fan] is not chip
    assert pop.chips[fan].parentWidget() is pop.chips[pix].parentWidget()


def test_editor_save_metadata_and_developer_immutable(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    w=R.get_work(window.lib,4);e=Editor(window,w);e.show()
    assert not hasattr(e,'developer') and not hasattr(e,'developer_input')
    e.brand_input.add_existing(a);e.brand_input.add_new('New Brand')
    e.tag_input.add_existing(school);e.tag_input.add_new('悬疑',1)
    assert e.brand_input.ids()==[a,('new','New Brand')]
    e.save();wait(app,window)
    assert not e.isVisible() and R.get_work(window.lib,4).developer=='WINDOWS'
    wm=M.list_work_meta(window.lib)[4]
    assert [M.find_brand(window.lib,n).id for n in ('Sample','New Brand')]==list(wm.brand_ids)
    assert {t.name for t in M.list_tags(window.lib) if t.id in wm.tag_ids}=={'校园','悬疑'}


def test_editor_duplicate_rollback_leaves_new_brand_and_tag_absent(window,app,meta_library):
    p=opened(window,app,meta_library);e=Editor(window,R.get_work(window.lib,4));e.show()
    e.title.setText('Should roll back');e.brand_input.add_new('Fresh');e.tag_input.add_new('新标签',1);e.tag_input.add_new('奇幻',1)
    before=snapshot(window.lib);e.save();wait(app,window)
    assert e.isVisible() and '已经有了' in e.message.text() and snapshot(window.lib)==before
    assert M.find_brand(window.lib,'Fresh') is None
    e.close()


def test_editor_cover_failure_rolls_back_all_database_changes(window,app,meta_library,tmp_path):
    opened(window,app,meta_library);e=Editor(window,R.get_work(window.lib,4));e.show()
    e.title.setText('Rollback');e.brand_input.add_new('Fresh');e.tag_input.add_new('新标签',1)
    e.cover_changed=True;e.cover_path=str(tmp_path/'missing.png')
    before=snapshot(window.lib);e.save();wait(app,window)
    assert snapshot(window.lib)==before and e.isVisible() and e.message.text()
    e.close()


def test_editor_conflict_reloads_metadata(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    w=R.get_work(window.lib,1);e=Editor(window,w);e.show()
    M.set_work_meta(window.lib,1,w.revision,[b],[pix]);e.title.setText('stale');e.save();wait(app,window)
    assert e.message.text()=='这部作品刚被别处改过，已重新载入'
    assert e.brand_input.ids()==[b] and e.tag_input.ids()==[pix]
    e.close()


def test_tidy_apply_and_already_processed_rows(window,app,meta_library):
    opened(window,app,meta_library);window.open_brand_tidy();wait(app,window);t=window.brand_tidy
    assert t.only_pending.isChecked() and t.table.rowCount()==4
    t.set_row('other','new','Other');t.apply();wait(app,window)
    assert len(t.rows())==0 and '挂上了品牌' in t.message.text()
    t.only_pending.setChecked(False);assert len(t.rows())==4
    assert next(d for d in t.rows() if d.text_key=='windows').action=='ignore'
    t.close()


def test_tidy_failure_rollback_keeps_choices(window,app,meta_library):
    opened(window,app,meta_library);window.open_brand_tidy();wait(app,window);t=window.brand_tidy
    t.set_row('sample','new','Fresh');t.set_row('other','new','Sample')
    before=snapshot(window.lib);rows=t.rows();t.apply();wait(app,window)
    assert snapshot(window.lib)==before and t.rows()==rows and t.message.text()
    t.close()


def test_tidy_readonly_and_tag_manager_readonly(window,app,meta_library):
    window.screenshot_mode=True;p=opened(window,app,meta_library);before=Path(meta_library[0]).read_bytes()
    window.open_brand_tidy();wait(app,window);t=window.brand_tidy
    assert not t.apply_button.isEnabled();t.apply();t.close()
    window.open_tag_manager();wait(app,window);g=window.tag_manager
    assert g.table.rowCount()==3 and not g.create_button.isEnabled();g.close()
    e=Editor(window,R.get_work(window.lib,1));assert not e.brand_input.isEnabled() and not e.tag_input.isEnabled()
    e.save();assert Path(meta_library[0]).read_bytes()==before;e.close()


def test_tag_manager_create_and_regroup(window,app,meta_library):
    opened(window,app,meta_library);window.open_tag_manager();wait(app,window);g=window.tag_manager
    g.name_input.setText('悬疑');g.group_input.setCurrentIndex(4);g.create();wait(app,window)
    assert g.table.rowCount()==4 and next(t for t in M.list_tags(window.lib) if t.name=='悬疑').group_id==5
    row=next(i for i in range(g.table.rowCount()) if g.table.item(i,0).text()=='悬疑')
    g.table.cellWidget(row,1).setCurrentIndex(0);wait(app,window)
    assert next(t for t in M.list_tags(window.lib) if t.name=='悬疑').group_id==1
    g.close()


@pytest.mark.parametrize('version',[1,2,5])
def test_upgrade_confirmation_version_text(window,app,tmp_path,monkeypatch,version):
    window.upgrade_from_version=version;seen=[]
    monkeypatch.setattr(QMessageBox,'exec',lambda box:seen.append(box.text()))
    window.confirm_upgrade(str(tmp_path/'test.galdb'))
    assert {1:'第一版',2:'第二版',5:'第五版'}[version] in seen[0] and '第六版' in seen[0] and '第五版及更老的' in seen[0]
    assert ('（以及游玩记录）' in seen[0])==(version==1)
    assert f'.v{version}-' in seen[0]


@pytest.mark.parametrize('spec',['x+1','b+abc','b+1,','b1','t--2'])
def test_bad_screenshot_filter_rejected(tmp_path,spec):
    out=tmp_path/'bad.png'
    r=subprocess.run([sys.executable,'-m','galpanel2','--screen','library','--library',str(tmp_path/'missing.galdb'),
                      '--meta-filter',spec,'--screenshot',str(out)],capture_output=True,text=True,encoding='utf-8')
    assert r.returncode!=0 and not out.exists()


def test_token_alias_candidates_and_pending_duplicates(window,app,meta_library):
    p=opened(window,app,meta_library);_,a,b,fan,school,pix=meta_library
    widget=TokenInput(window,'brand',p.brands.values());widget.edit.setText('ランタン')
    assert widget.suggestions.count()==1 and widget.suggestions.item(0).data(Qt.ItemDataRole.UserRole)==b
    widget.add_existing(b);widget.add_existing(b);widget.add_new('Brand');widget.add_new('ｂｒａｎｄ')
    assert widget.ids()==[b,('new','Brand')]


@pytest.mark.parametrize('kind',['brand','tag'])
def test_manager_job_start_failure_recovers(window,app,meta_library,monkeypatch,kind):
    opened(window,app,meta_library)
    (window.open_brand_tidy if kind=='brand' else window.open_tag_manager)();wait(app,window)
    dialog=window.brand_tidy if kind=='brand' else window.tag_manager
    if kind=='tag':dialog.name_input.setText('Fresh')
    before=snapshot(window.lib)
    def broken_start(job):raise RuntimeError('injected metadata worker start failure')
    monkeypatch.setattr(Job,'start',broken_start)
    (dialog.apply if kind=='brand' else dialog.create)()
    assert not dialog.saving and not window.busy and not window.pending_jobs
    assert 'start failure' in dialog.message.text() and snapshot(window.lib)==before
    dialog.close()


def test_tidy_reads_in_worker_and_keeps_event_loop_responsive(window,app,meta_library,monkeypatch):
    opened(window,app,meta_library);started=threading.Event();release=threading.Event();thread_ids=[]
    original=M.developer_texts
    def blocked(lib):
        thread_ids.append(threading.get_ident());started.set()
        assert release.wait(5)
        return original(lib)
    monkeypatch.setattr(M,'developer_texts',blocked)
    window.open_brand_tidy()
    assert started.wait(2)
    app.processEvents()
    assert window.brand_tidy.isVisible() and not window.brand_tidy.apply_button.isEnabled()
    release.set();wait(app,window)
    assert thread_ids==[thread_ids[0]] and thread_ids[0]!=threading.get_ident()
    window.brand_tidy.close()


def test_popover_filters_exist_only_while_visible(window,app,meta_library):
    p=opened(window,app,meta_library)
    for _ in range(4):
        assert not p.brand_popover._filter_active and not p.tag_popover._filter_active
        p.brand_button.click();app.processEvents()
        assert p.brand_popover._filter_active and not p.tag_popover._filter_active
        p.tag_button.click();app.processEvents()
        assert not p.brand_popover._filter_active and p.tag_popover._filter_active
        p.tag_popover.hide();app.processEvents()
    p.brand_button.click();window.close();app.processEvents()
    assert not p.brand_popover._filter_active and not p.tag_popover._filter_active
