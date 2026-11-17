"""Metadata contracts and failure rollback; all data belongs to tmp_path."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest
from galpanel2.core import meta as M, store, repository as R, domain as D, play, launchopts
from test_p2_core import v1


@pytest.fixture
def works(lib):
    with lib.transaction() as c:
        for i, text in enumerate(("Sample", "Sample", "[Gearbox]", "WINDOWS", "(unfinished", "Other", ""), 1):
            c.execute("INSERT INTO works(id,uuid,title,developer,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (i, str(uuid.uuid4()), f"Work {i}", text, int(i == 2), "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"))
    return list(range(1, 8))


def snapshot(lib):
    with lib.connection() as c:
        return {t[0]: [tuple(r) for r in c.execute(f'SELECT * FROM "{t[0]}" ORDER BY rowid')]
                for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


@pytest.mark.parametrize('text,want', [(' ＬＡＮＴＥＲＮ  SOFT ', 'lantern soft'), ('ﾗﾝﾀﾝ', 'ランタン'),
                                    ('Straße', 'strasse'), ('a\n\tb', 'a b'), ('中文', '中文')])
def test_name_key(tmp_path, text, want):
    assert M.name_key(text) == want


@pytest.mark.parametrize('bad', ['', '  ', 'x' * 101, 'a\x00b', 'a\x1fb', 'a\nb'])
@pytest.mark.parametrize('kind', ['brand', 'alias', 'tag'])
def test_invalid_names_roll_back(lib, bad, kind):
    before = snapshot(lib)
    with pytest.raises(ValueError):
        if kind == 'brand': M.create_brand(lib, bad)
        elif kind == 'alias': M.create_brand(lib, 'Good', [bad])
        else: M.create_tag(lib, bad, 1)
    assert snapshot(lib) == before


def test_brand_namespace_and_alias_replacement(lib, works):
    a = M.create_brand(lib, '  Alpha  Co ', ['ＡＬＰＨＡ CO', 'Ａ', 'a'])
    b = M.create_brand(lib, 'Beta')
    assert M.find_brand(lib, 'ａ').id == a
    assert M.list_brands(lib)[0].aliases == ('Ａ',)
    for fn, args in ((M.create_brand, ('a',)), (M.create_brand, ('Gamma', ['Alpha Co'])),
                     (M.rename_brand, (b, 'a')), (M.set_brand_aliases, (b, ['New', 'a']))):
        before = snapshot(lib)
        with pytest.raises(D.DuplicateName) as err: fn(lib, *args)
        assert err.value.existing_id == a and snapshot(lib) == before
    M.rename_brand(lib, a, 'a')
    assert M.find_brand(lib, 'Alpha Co') is None and M.find_brand(lib, 'A').aliases == ()
    M.set_brand_aliases(lib, a, ['Z', 'z', 'a'])
    assert M.find_brand(lib, 'a').aliases == ('Z',)
    M.set_work_meta(lib, 2, R.get_work(lib, 2).revision, [a], [])
    assert M.list_brands(lib)[0].work_count == 1  # includes hidden works
    with pytest.raises(ValueError, match='Brand still has works'): M.delete_brand(lib, a)
    M.delete_brand(lib, b)


def test_merge_brands_order_aliases_decisions(lib, works):
    a, b, c = [M.create_brand(lib, n, [n+' alias']) for n in ('Alpha', 'Beta', 'Gamma')]
    for wid, ids in ((1, [a, c]), (2, [b, a]), (3, [b])):
        M.set_work_meta(lib, wid, R.get_work(lib, wid).revision, ids, [])
    M.apply_developer_decisions(lib, [M.DevDecision('sample', 'merge', brand_id=b)])
    M.merge_brands(lib, b, a)
    wm = M.list_work_meta(lib)
    assert wm[1].brand_ids == (a, c) and wm[2].brand_ids == (a,) and wm[3].brand_ids == (a,)
    assert {'Beta', 'Beta alias'} <= set(M.find_brand(lib, 'Alpha').aliases)
    assert M.developer_texts(lib)[0].decided_brand_id == a
    with pytest.raises(ValueError): M.merge_brands(lib, a, a)


def test_delete_brand_restores_pending(lib, works):
    a = M.create_brand(lib, 'Empty')
    with lib.transaction() as c:
        c.execute("INSERT INTO brand_decisions VALUES('sample','Sample','brand',?,?)", (a, D.utc_now()))
    M.delete_brand(lib, a)
    assert M.developer_texts(lib)[0].decided is None


def test_tags_groups_order_merge_delete(lib, works):
    assert [g.name for g in M.list_tag_groups(lib)] == ['题材', '角色', '系统', 'H 内容', '其他']
    a, b, t = M.create_tag(lib, '奇幻', 1), M.create_tag(lib, '校园', 1), M.create_tag(lib, 'H', 4)
    M.set_work_meta(lib, 1, 1, [], [t, b, a, a])
    M.set_work_meta(lib, 2, 1, [], [b])
    assert M.list_work_meta(lib)[1].tag_ids == (a, b, t)
    assert [x.id for x in M.list_tags(lib)] == [b, a, t]
    with pytest.raises(D.DuplicateName): M.rename_tag(lib, a, '校园')
    M.rename_tag(lib, t, '新名'); M.set_tag_group(lib, t, 3)
    M.merge_tags(lib, a, b)
    assert M.list_work_meta(lib)[1].tag_ids == (b, t)
    M.delete_tag(lib, b)
    assert M.list_work_meta(lib)[1].tag_ids == (t,)


@pytest.mark.parametrize('fn,args', [(M.rename_brand,(999,'X')), (M.set_brand_aliases,(999,['X'])),
    (M.merge_brands,(999,998)), (M.delete_brand,(999,)), (M.create_tag,('X',9)),
    (M.rename_tag,(999,'X')), (M.set_tag_group,(999,1)), (M.merge_tags,(999,998)), (M.delete_tag,(999,))])
def test_missing_ids_atomic(lib, fn, args):
    before = snapshot(lib)
    with pytest.raises(KeyError): fn(lib, *args)
    assert snapshot(lib) == before


def test_work_metadata_cas_replace_and_delete(lib, works):
    a, b = M.create_brand(lib, 'A'), M.create_brand(lib, 'B')
    t = M.create_tag(lib, '校园', 1)
    before = R.get_work(lib, 1)
    assert M.set_work_meta(lib, 1, before.revision, [b, a, b], [t, t]) == before.revision + 1
    assert M.list_work_meta(lib)[1] == M.WorkMeta(1, (b, a), (t,))
    after = R.get_work(lib, 1)
    assert (after.title,after.developer,after.notes,after.hidden,after.added_at) == \
           (before.title,before.developer,before.notes,before.hidden,before.added_at)
    for args, exc in (((1,1,[a],[]), D.WriteConflict), ((1,2,[999],[]), KeyError),
                      ((1,2,[],[999]), KeyError), ((999,1,[],[]), KeyError)):
        snap = snapshot(lib)
        with pytest.raises(exc): M.set_work_meta(lib,*args)
        assert snapshot(lib) == snap
    snap = snapshot(lib)
    with pytest.raises(KeyError):
        with lib.transaction():
            R.edit_work(lib, 1, 2, title='Rollback')
            M.set_work_meta(lib,1,3,[999],[])
    assert snapshot(lib) == snap
    R.remove_work(lib,1,2)
    assert 1 not in M.list_work_meta(lib) and len(M.list_brands(lib)) == 2


def test_nested_caught_failure_retains_outer_transaction(lib):
    with lib.transaction():
        a = M.create_brand(lib,'A')
        try: M.create_brand(lib,'B',['A'])
        except D.DuplicateName: pass
        M.create_tag(lib,'校园',1)
    assert [b.id for b in M.list_brands(lib)] == [a] and len(M.list_tags(lib)) == 1


@pytest.mark.parametrize('f,want', [(M.MetaFilter(), [1,2,3]),
    (M.MetaFilter(brands_in=frozenset({1,2})),[1,2]),
    (M.MetaFilter(brands_in=frozenset({0})),[3]),
    (M.MetaFilter(brands_out=frozenset({2})),[1,3]),
    (M.MetaFilter(tags_in=frozenset({11,12})),[1]),
    (M.MetaFilter(tags_out=frozenset({12})),[2,3]),
    (M.MetaFilter(brands_in=frozenset({1}),brands_out=frozenset({1})),[])])
def test_filter_and_counts(tmp_path, f, want):
    ms = [M.WorkMeta(1,(1,),(11,12)),M.WorkMeta(2,(1,2),(11,)),M.WorkMeta(3,(),())]
    assert [m.work_id for m in ms if M.matches(m,f)] == want
    bc, tc = M.facet_counts(iter(ms), M.MetaFilter(brands_in=frozenset({2}),tags_in=frozenset({11})))
    assert bc == {1:2,2:1} and tc == {11:1}
    assert M.MetaFilter().is_empty()


@pytest.mark.parametrize('text,want', [('[Double Soft Cream]','Double Soft Cream'), ('【Foo】','Foo'),
    ('（Foo）','Foo'), ('(Foo)','Foo'), ('Foo -_・ (unfinished','Foo'), ('Foo 【unfinished','Foo'),
    ('[',''), ('Foo (complete)','Foo (complete)'), (' Foo   Bar ','Foo Bar')])
def test_clean_developer(tmp_path,text,want):
    assert M.clean_developer(text) == want


def test_developer_grouping_batch_and_preservation(lib, works):
    before = snapshot(lib)
    ts = {x.text_key:x for x in M.developer_texts(lib)}
    assert ts['sample'].work_ids == (1,2) and ts[''].suggestion == 'ignore'
    assert ts['gearbox'].text == '[Gearbox]' and ts['windows'].suggestion == 'ignore'
    result = M.apply_developer_decisions(lib,[M.DevDecision('sample','new','Brand'),
        M.DevDecision('other','new','brand'), M.DevDecision('gearbox','new','Gearbox'),
        M.DevDecision('windows','ignore'),M.DevDecision('','ignore')])
    assert result == M.ApplyResult(2,4,2,2)
    after = snapshot(lib)
    for i in range(7):
        assert before['works'][i][3] == after['works'][i][3]  # developer preserved
        assert before['works'][i][-1] == after['works'][i][-1]  # revision preserved
    assert next(t for t in M.developer_texts(lib) if t.text_key=='sample').decided == 'brand'
    M.apply_developer_decisions(lib,[M.DevDecision('sample','merge',brand_id=M.find_brand(lib,'Brand').id)])
    assert M.find_brand(lib,'Brand').work_count == 3


@pytest.mark.parametrize('tail,exc', [(M.DevDecision('nope','ignore'),ValueError),
    (M.DevDecision('sample','ignore'),ValueError), (M.DevDecision('other','merge',brand_id=999),KeyError),
    (M.DevDecision('other','new','Existing'),D.DuplicateName), (M.DevDecision('other','what'),ValueError),
    (M.DevDecision('other','new',''),ValueError)])
def test_tidy_batch_rollback(lib,works,tail,exc):
    M.create_brand(lib,'Existing')
    before = snapshot(lib)
    with pytest.raises(exc):
        M.apply_developer_decisions(lib,[M.DevDecision('sample','new','Fresh'),tail])
    assert snapshot(lib) == before


def test_tidy_alias_collision_rolls_back(lib,works):
    M.create_brand(lib,'Existing',['Gearbox'])
    before = snapshot(lib)
    with pytest.raises(D.DuplicateName):
        M.apply_developer_decisions(lib,[M.DevDecision('sample','new','Fresh'),M.DevDecision('gearbox','new','New')])
    assert snapshot(lib) == before


@pytest.mark.parametrize('version',[1,2])
def test_old_library_reads_upgrade_and_atomic_failure(tmp_path,version,monkeypatch):
    p = v1(tmp_path)
    if version==2:
        with sqlite3.connect(p) as c:
            c.executescript(Path(store.__file__).with_name('schema_v2.sql').read_text(encoding='utf-8'))
            c.execute('PRAGMA user_version=2')
    before=p.read_bytes()
    with pytest.raises(D.NeedsUpgrade) as err: store.open_library(str(p))
    assert err.value.version == version and p.read_bytes()==before and not Path(str(p)+'.lock').exists()
    with store.open_library(str(p),True) as ro:
        assert M.list_brands(ro)==M.list_tags(ro)==M.list_tag_groups(ro)==M.developer_texts(ro)==[]
        assert M.find_brand(ro,'X') is None and M.list_work_meta(ro)=={1:M.WorkMeta(1,(),())}
        with pytest.raises(ValueError,match='read-only'): M.create_brand(ro,'X')
    original=Path.read_text
    def broken(path,*args,**kwargs):
        text=original(path,*args,**kwargs)
        return text+'INVALID;' if path.name=='schema_v3.sql' else text
    with monkeypatch.context() as patch:
        patch.setattr(Path,'read_text',broken)
        with pytest.raises(sqlite3.DatabaseError): store.upgrade_library(str(p))
    assert p.read_bytes()==before
    # UTC backup timestamps are second precision; remove no files, advance the test clock.
    monkeypatch.setattr(store,'utc_now',lambda:'2026-10-13T00:00:00Z')
    result=store.upgrade_library(str(p))
    assert (result.from_version,result.to_version)==(version,6)
    assert f'.v{version}-' in result.backup_path
    with store.open_library(str(p)) as upgraded:
        assert upgraded.schema_version==6 and len(M.list_tag_groups(upgraded))==5
    with pytest.raises(ValueError,match='already version 6'): store.upgrade_library(str(p))


def test_cli_metadata_read_only(lib,works,tmp_path):
    a=M.create_brand(lib,'Brand',['别名']); t=M.create_tag(lib,'校园',1)
    M.set_work_meta(lib,1,1,[a],[t])
    before=Path(lib.path).read_bytes()
    r=subprocess.run([sys.executable,'-m','galpanel2.core','meta',lib.path],capture_output=True,text=True,encoding='utf-8')
    j=json.loads(r.stdout)
    assert r.returncode==0 and j['schema_version']==6 and j['works'][0]['brand_ids']==[a]
    assert Path(lib.path).read_bytes()==before


def test_read_only_all_writes(lib,tmp_path):
    a=M.create_brand(lib,'A'); b=M.create_brand(lib,'B'); t=M.create_tag(lib,'校园',1); u=M.create_tag(lib,'奇幻',1)
    with store.open_library(lib.path,True) as ro:
        for fn,args in [(M.create_brand,('X',)),(M.rename_brand,(a,'X')),(M.set_brand_aliases,(a,[])),
            (M.merge_brands,(a,b)),(M.delete_brand,(a,)),(M.create_tag,('X',1)),(M.rename_tag,(t,'X')),
            (M.set_tag_group,(t,2)),(M.merge_tags,(t,u)),(M.delete_tag,(t,)),
            (M.set_work_meta,(1,1,[],[])),(M.apply_developer_decisions,([],))]:
            with pytest.raises(ValueError,match='Library is read-only'): fn(ro,*args)


@pytest.mark.parametrize('text',['4K','1080p','v1.2','1_02b','x','体验版','DLsite','rpg','【】'])
def test_platform_marker_suggestions(lib,text):
    with lib.transaction() as c:
        c.execute('INSERT INTO works(uuid,title,developer,added_at,updated_at) VALUES(?,?,?,?,?)',
                  (str(uuid.uuid4()),'Fixture',text,D.utc_now(),D.utc_now()))
    assert M.developer_texts(lib)[0].suggestion=='ignore'


def test_developer_representative_frequency_and_id_tie(lib):
    with lib.transaction() as c:
        for i,text in enumerate(('Tie  Co','Tie Co','Major  Co','Major Co','Major Co'),1):
            c.execute('INSERT INTO works(id,uuid,title,developer,added_at,updated_at) VALUES(?,?,?,?,?,?)',
                      (i,str(uuid.uuid4()),'Fixture',text,D.utc_now(),D.utc_now()))
    texts=M.developer_texts(lib)
    assert [(t.text,t.work_ids) for t in texts]==[('Major Co',(3,4,5)),('Tie  Co',(1,2))]


def test_version_gate_all_metadata_writes(lib,works):
    a=M.create_brand(lib,'A');b=M.create_brand(lib,'B');t=M.create_tag(lib,'校园',1);u=M.create_tag(lib,'奇幻',1)
    before=snapshot(lib);lib.schema_version=2
    try:
        for fn,args in [(M.create_brand,('X',)),(M.rename_brand,(a,'X')),(M.set_brand_aliases,(a,[])),
            (M.merge_brands,(a,b)),(M.delete_brand,(a,)),(M.create_tag,('X',1)),(M.rename_tag,(t,'X')),
            (M.set_tag_group,(t,2)),(M.merge_tags,(t,u)),(M.delete_tag,(t,)),
            (M.set_work_meta,(1,1,[],[])),(M.apply_developer_decisions,([],))]:
            with pytest.raises(ValueError,match='Library needs version 3'):fn(lib,*args)
        assert snapshot(lib)==before
    finally:lib.schema_version=3


def test_tag_group_and_self_merge_fail_atomically(lib):
    tag=M.create_tag(lib,'校园',1);before=snapshot(lib)
    with pytest.raises(KeyError):M.set_tag_group(lib,tag,99)
    with pytest.raises(ValueError):M.merge_tags(lib,tag,tag)
    assert snapshot(lib)==before
