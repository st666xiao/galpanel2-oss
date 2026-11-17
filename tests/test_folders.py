"""Tests for library merging, moving games between folders, and folder UI."""
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from galpanel2.core import merge, repository as repo, shelf, storage, store
from galpanel2.core.domain import Copy, Source, path_key, utc_now
from galpanel2.ui import library as L
from test_ui import app, wait, window  # noqa: F401  (shared fixtures)


def _key(path):
    try:
        return path_key(str(path))
    except ValueError:
        return os.path.normcase(os.path.abspath(str(path)))


def _dump(lib):
    with lib.connection() as c:
        return tuple(c.iterdump())


def _digest(path):
    path = Path(path)
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    out = {}
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        out[relative] = "<dir>" if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest()
    return out


def _add_work(lib, wid, title, *, uuid_value=None, developer="", hidden=0):
    with lib.transaction() as c:
        c.execute("INSERT INTO works(id,uuid,title,developer,notes,hidden,added_at,updated_at,"
                  "revision) VALUES(?,?,?,?,'',?,?,?,1)",
                  (wid, uuid_value or str(uuid.uuid4()), title, developer, hidden,
                   "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"))


def _add_source(lib, sid, path, label=""):
    with lib.transaction() as c:
        c.execute("INSERT INTO sources(id,path,path_key,label,added_at) VALUES(?,?,?,?,?)",
                  (sid, str(path), _key(path), label, "2020-01-01T00:00:00Z"))


def _add_copy(lib, wid, sid, path, *, kind="folder", size=10):
    with lib.transaction() as c:
        c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,"
                  "size_bytes,availability,checked_at,origin,added_at) "
                  "VALUES(?,?,?,?,?,?,?,'ok',?,'scan',?)",
                  (wid, sid, kind, str(path), _key(path),
                   "game.exe" if kind == "folder" else None, size,
                   "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_link(lib, wid, site, site_id):
    with lib.transaction() as c:
        c.execute("INSERT INTO external_links(work_id,site,site_id,bound_at) "
                  "VALUES(?,?,?,?)", (wid, site, site_id, "2020"))


def _add_brand(lib, bid, name, *, aliases=(), links=()):
    from galpanel2.core import meta
    with lib.transaction() as c:
        c.execute("INSERT INTO brands(id,name,name_key,created_at) VALUES(?,?,?,?)",
                  (bid, name, meta.name_key(name), "2020"))
        for alias in aliases:
            c.execute("INSERT INTO brand_aliases(brand_id,alias,alias_key) VALUES(?,?,?)",
                      (bid, alias, meta.name_key(alias)))
        for site, site_id in links:
            c.execute("INSERT INTO brand_links(site,site_id,brand_id) VALUES(?,?,?)",
                      (site, site_id, bid))


def _attach_brand(lib, wid, bid, position=0):
    with lib.transaction() as c:
        c.execute("INSERT INTO work_brands(work_id,brand_id,position) VALUES(?,?,?)",
                  (wid, bid, position))


def _add_tag(lib, tid, name, group=1):
    from galpanel2.core import meta
    with lib.transaction() as c:
        c.execute("INSERT INTO tags(id,group_id,name,name_key,created_at) VALUES(?,?,?,?,?)",
                  (tid, group, name, meta.name_key(name), "2020"))


def _attach_tag(lib, wid, tid):
    with lib.transaction() as c:
        c.execute("INSERT INTO work_tags(work_id,tag_id) VALUES(?,?)", (wid, tid))


def _add_cover(lib, wid, data):
    digest = hashlib.sha256(data).hexdigest()
    name = digest + ".png"
    directory = Path(lib.path + ".covers")
    directory.mkdir(exist_ok=True)
    (directory / name).write_bytes(data)
    with lib.transaction() as c:
        c.execute("INSERT INTO covers(work_id,file_name,sha256,origin) VALUES(?,?,?,'user')",
                  (wid, name, digest))
    return name, digest


@pytest.fixture
def libs(tmp_path):
    dst = store.create_library(str(tmp_path / "dst.galdb"))
    src = store.create_library(str(tmp_path / "src.galdb"))
    try:
        yield dst, src
    finally:
        dst.close()
        src.close()


# ---------------------------------------------------------------- merge
def test_plan_merge_reports_counts_without_writing(libs):
    dst, src = libs
    _add_source(dst, 1, r"D:\Games", "主库")
    _add_work(dst, 1, "已有作品")
    _add_link(dst, 1, "dlsite", "RJ1")
    _add_copy(dst, 1, 1, r"D:\Games\日系\旧")
    _add_source(src, 1, r"G:\Games", "G 盘")
    _add_work(src, 1, "源里的同一部")
    _add_link(src, 1, "dlsite", "RJ1")
    _add_copy(src, 1, 1, r"G:\Games\日系\新")
    _add_work(src, 2, "源里全新的一部")
    _add_copy(src, 2, 1, r"G:\Games\同人\全新")

    before = _dump(dst)
    plan = merge.plan_merge(dst, src.path)
    assert plan.works_created == 1
    assert plan.copies_attached == 1
    assert plan.copies_skipped == 0
    assert plan.sources_added == 1
    assert plan.merged_into == (("源里的同一部", "已有作品"),)
    assert plan.new_sources == ((r"G:\Games", "G 盘"),)
    assert _dump(dst) == before


def test_merge_by_external_link_attaches_copies_and_extra_links(libs):
    dst, src = libs
    _add_source(dst, 1, r"D:\Games", "主库")
    _add_work(dst, 1, "目标标题")
    _add_link(dst, 1, "dlsite", "RJ1")
    _add_source(src, 7, r"G:\Games", "G 盘")
    _add_work(src, 1, "源标题")
    _add_link(src, 1, "dlsite", "RJ1")
    _add_link(src, 1, "steam", "1144400")
    source_path = r"G:\Games\日系\标题"
    _add_copy(src, 1, 7, source_path)

    result = merge.merge_library(dst, src.path)
    assert result.works_created == 0 and result.copies_attached == 1
    assert result.merged_into == (("源标题", "目标标题"),)
    with dst.connection() as c:
        assert c.execute("SELECT count(*) FROM works").fetchone()[0] == 1
        assert dict(c.execute("SELECT site,site_id FROM external_links WHERE work_id=1")) == {
            "dlsite": "RJ1", "steam": "1144400"}
        row = c.execute("SELECT work_id,source_id FROM copies WHERE path_key=?",
                        (_key(source_path),)).fetchone()
        assert row is not None and row[0] == 1
        source_id = c.execute("SELECT id FROM sources WHERE path_key=?", (_key(r"G:\Games"),)
                              ).fetchone()[0]
        assert row[1] == source_id


def test_work_without_links_is_created_even_with_same_title(libs):
    dst, src = libs
    _add_source(dst, 1, r"D:\Games", "")
    _add_work(dst, 1, "同名")
    _add_source(src, 1, r"G:\Games", "")
    _add_work(src, 1, "同名")
    _add_copy(src, 1, 1, r"G:\Games\日系\同名")
    result = merge.merge_library(dst, src.path)
    assert result.works_created == 1
    with dst.connection() as c:
        assert c.execute("SELECT count(*) FROM works").fetchone()[0] == 2


def test_duplicate_copy_path_is_skipped(libs):
    dst, src = libs
    _add_source(dst, 1, r"D:\Games", "")
    _add_work(dst, 1, "作品")
    _add_link(dst, 1, "dlsite", "RJ1")
    _add_copy(dst, 1, 1, r"G:\Games\日系\作品")
    _add_source(src, 1, r"G:\Games", "")
    _add_work(src, 1, "作品")
    _add_link(src, 1, "dlsite", "RJ1")
    _add_copy(src, 1, 1, r"G:\Games\日系\作品")
    plan = merge.plan_merge(dst, src.path)
    assert plan.copies_skipped == 1 and plan.copies_attached == 0
    result = merge.merge_library(dst, src.path)
    assert result.copies_skipped == 1
    with dst.connection() as c:
        assert c.execute("SELECT count(*) FROM copies").fetchone()[0] == 1


def test_cover_file_is_copied_with_hash_name(libs):
    dst, src = libs
    _add_source(dst, 1, r"D:\Games", "")
    _add_source(src, 1, r"G:\Games", "")
    _add_work(src, 1, "有封面")
    _add_copy(src, 1, 1, r"G:\Games\日系\有封面")
    data = b"\x89PNG\r\n\x1a\n" + b"cover-data"
    name, digest = _add_cover(src, 1, data)
    result = merge.merge_library(dst, src.path)
    assert result.covers_copied == 1
    with dst.connection() as c:
        row = c.execute("SELECT file_name,sha256 FROM covers").fetchone()
        assert row is not None and row[0] == name and row[1] == digest
    target_file = Path(dst.path + ".covers") / name
    assert target_file.is_file() and target_file.read_bytes() == data


def test_brand_tag_and_shelf_are_merged(libs):
    dst, src = libs
    _add_source(dst, 1, r"D:\Games", "")
    _add_work(dst, 1, "作品")
    _add_link(dst, 1, "dlsite", "RJ1")
    _add_brand(dst, 1, "Brand", links=(("dlsite", "B1"),))
    _attach_brand(dst, 1, 1)
    _add_tag(dst, 1, "纯爱", group=1)
    _attach_tag(dst, 1, 1)
    shelf.set_category(dst, 1, "欧美")
    _add_source(src, 1, r"G:\Games", "")
    _add_work(src, 1, "作品")
    _add_link(src, 1, "dlsite", "RJ1")
    _add_brand(src, 9, "Brand", aliases=("BrandAlias",), links=(("dlsite", "B1"),))
    _attach_brand(src, 1, 9)
    _add_tag(src, 9, "NTR", group=1)
    _add_tag(src, 10, "纯爱", group=1)
    _attach_tag(src, 1, 9)
    _attach_tag(src, 1, 10)
    shelf.set_category(src, 1, "日系")
    shelf.set_status(src, 1, "玩过")

    result = merge.merge_library(dst, src.path)
    assert result.brands_created == 0 and result.tags_created == 1
    with dst.connection() as c:
        assert c.execute("SELECT count(*) FROM brands").fetchone()[0] == 1
        assert c.execute("SELECT brand_id FROM brand_links WHERE site='dlsite' AND site_id='B1'"
                         ).fetchone()[0] == 1
        assert c.execute("SELECT 1 FROM brand_aliases WHERE brand_id=1 AND alias='BrandAlias'"
                         ).fetchone() is not None
        assert {r[0] for r in c.execute("SELECT name FROM tags")} == {"纯爱", "NTR"}
        assert {r[0] for r in c.execute("SELECT tag_id FROM work_tags WHERE work_id=1")} == {1, 2}
        assert c.execute("SELECT category,status FROM work_shelf WHERE work_id=1").fetchone()[:] == \
            ("欧美", "玩过")


def test_target_source_tag_decision_wins(libs):
    dst, src = libs
    _add_source(dst, 1, r"D:\Games", "")
    _add_work(dst, 1, "作品")
    _add_link(dst, 1, "dlsite", "RJ1")
    with dst.transaction() as c:
        c.execute("INSERT INTO source_tags(site,tag_key,text,suggestion,category,action,tag_id,"
                  "decided_at) VALUES('dlsite','t1','旧','旧','ero','ignore',NULL,'2020')")
    _add_source(src, 1, r"G:\Games", "")
    _add_work(src, 1, "作品")
    _add_link(src, 1, "dlsite", "RJ1")
    _add_tag(src, 5, "新标签", group=1)
    with src.transaction() as c:
        c.execute("INSERT INTO source_tags(site,tag_key,text,suggestion,category,action,tag_id,"
                  "decided_at) VALUES('dlsite','t1','旧','旧','ero','map',5,'2020')")
        c.execute("INSERT INTO work_source_tags(work_id,site,tag_key) VALUES(1,'dlsite','t1')")
        c.execute("INSERT INTO source_tags(site,tag_key,text,suggestion,category,action,tag_id,"
                  "decided_at) VALUES('dlsite','t2','新','新','ero','map',5,'2020')")
        c.execute("INSERT INTO work_source_tags(work_id,site,tag_key) VALUES(1,'dlsite','t2')")

    merge.merge_library(dst, src.path)
    with dst.connection() as c:
        first = c.execute("SELECT action,tag_id FROM source_tags WHERE site='dlsite' AND tag_key='t1'"
                          ).fetchone()
        assert tuple(first) == ("ignore", None)
        second = c.execute("SELECT action,tag_id FROM source_tags WHERE site='dlsite' AND tag_key='t2'"
                           ).fetchone()
        assert second[0] == "map" and second[1] is not None


def test_merge_keeps_source_file_and_writes_backup(libs):
    dst, src = libs
    _add_source(src, 1, r"G:\Games", "")
    _add_work(src, 1, "唯一作品")
    _add_copy(src, 1, 1, r"G:\Games\日系\唯一作品")
    source = src.path
    with open(source, "rb") as stream:
        before = hashlib.sha256(stream.read()).hexdigest()
    result = merge.merge_library(dst, source)
    with open(source, "rb") as stream:
        after = hashlib.sha256(stream.read()).hexdigest()
    assert before == after
    assert result.backup_path and os.path.isfile(result.backup_path)
    assert result.integrity_check == "ok"
    assert os.path.isfile(source)
    with store.open_library(source, readonly=True) as check:
        assert check.schema_version == 6


def test_merge_failure_rolls_back_and_removes_new_covers(libs, monkeypatch):
    dst, src = libs
    _add_source(dst, 1, r"D:\Games", "")
    _add_work(dst, 1, "现有")
    _add_source(src, 1, r"G:\Games", "")
    _add_work(src, 1, "新作品")
    _add_copy(src, 1, 1, r"G:\Games\日系\新作品")
    _add_cover(src, 1, b"\x89PNG\r\n\x1a\n" + b"will-fail")
    before = _dump(dst)

    def boom(*args, **kwargs):
        raise RuntimeError("cover write failed")

    monkeypatch.setattr(merge.repository, "copy_cover", boom)
    with pytest.raises(RuntimeError):
        merge.merge_library(dst, src.path)
    assert _dump(dst) == before
    covers_dir = Path(dst.path + ".covers")
    assert not covers_dir.exists() or not list(covers_dir.glob("*"))


def test_merge_copies_optional_tables_and_sessions(libs):
    dst, src = libs
    _add_source(src, 1, r"G:\Games", "")
    _add_work(src, 1, "冷藏作品")
    _add_copy(src, 1, 1, r"G:\cold\Games\日系\冷藏作品")
    with src.transaction() as c:
        c.execute(storage._CREATE_SQL)
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) "
                  "VALUES(1,'冷藏',?, '2020')", (r"G:\Games\日系\冷藏作品",))
        c.execute("INSERT INTO launch_options(copy_id,exe_relpath,args,updated_at) "
                  "VALUES(1,'game.exe','-x','2020')")
        c.execute("INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,started_at,"
                  "last_beat_at,ended_at,seconds,end_reason,edited) "
                  "VALUES(1,1,'tracked','closed','2020-01-01','2020-01-01','2020-01-01',"
                  "'2020-01-01',120,'exited',0)")
    merge.merge_library(dst, src.path)
    with dst.connection() as c:
        assert c.execute("SELECT state,home_path FROM copy_storage").fetchone()[:] == \
            ("冷藏", r"G:\Games\日系\冷藏作品")
        assert c.execute("SELECT exe_relpath,args FROM launch_options").fetchone()[:] == \
            ("game.exe", "-x")
        assert c.execute("SELECT work_id,copy_id,seconds FROM play_sessions").fetchone()[:] == \
            (1, 1, 120)


def test_merge_upgrades_an_older_source_on_a_temp_copy(libs, tmp_path):
    import sqlite3
    from galpanel2.core import store as store_module
    dst, _src = libs
    old_path = tmp_path / "old-v5.galdb"
    c = sqlite3.connect(str(old_path))
    try:
        for name in ("schema.sql", "schema_v2.sql", "schema_v3.sql", "schema_v4.sql",
                     "schema_v5.sql"):
            c.executescript((Path(store_module.__file__).with_name(name)).read_text(encoding="utf-8"))
        c.execute(f"PRAGMA application_id={store_module.APPLICATION_ID}")
        c.execute("PRAGMA user_version=5")
        c.execute("INSERT INTO library_info VALUES('library_uuid',?)", (str(uuid.uuid4()),))
        c.execute("INSERT INTO library_info VALUES('created_at','2020')")
        c.execute("INSERT INTO library_info VALUES('app_version','5')")
        c.execute("INSERT INTO works(id,uuid,title,developer,notes,hidden,added_at,updated_at) "
                  "VALUES(1,?,'旧库作品','','',0,'2020','2020')", (str(uuid.uuid4()),))
        c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,size_bytes,"
                  "availability,checked_at,origin,added_at) "
                  "VALUES(1,NULL,'folder',?,?, 'game.exe',10,'ok','2020','scan','2020')",
                  (r"G:\Games\旧库作品", _key(r"G:\Games\旧库作品")))
        c.commit()
    finally:
        c.close()
    before = old_path.read_bytes()
    result = merge.merge_library(dst, str(old_path))
    assert result.works_created == 1
    assert old_path.read_bytes() == before
    with dst.connection() as check:
        assert check.execute("SELECT title FROM works").fetchone()[0] == "旧库作品"


def test_merge_rejects_self_and_readonly(libs):
    dst, _src = libs
    with pytest.raises(merge.MergeError):
        merge.plan_merge(dst, dst.path)
    with store.open_library(dst.path, readonly=True) as ro:
        with pytest.raises(merge.MergeError):
            merge.plan_merge(ro, dst.path)
        with pytest.raises(merge.MergeError):
            merge.merge_library(ro, dst.path)


# ---------------------------------------------------------------- move
def _folder(path, files=None):
    path = Path(path)
    path.mkdir(parents=True)
    for name, data in (files or {"game.exe": b"MZ" + b"x" * 100}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return path


def _sources(lib, tmp_path):
    first = tmp_path / "D" / "Games"
    second = tmp_path / "E" / "Games2"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _add_source(lib, 1, first, "D 盘")
    _add_source(lib, 2, second, "E 盘")
    return first, second


def test_move_same_volume_renames_and_updates_path_source(lib, tmp_path, monkeypatch):
    first, second = _sources(lib, tmp_path)
    source = _folder(first / "日系" / "Brand" / "Title", {"game.exe": b"MZ", "a.bin": b"a"})
    _add_work(lib, 1, "Title")
    cid = _add_copy(lib, 1, 1, source)
    log = tmp_path / "storage-log.jsonl"

    def boom(*args, **kwargs):
        raise AssertionError("same-volume move must not copy")

    monkeypatch.setattr(storage, "_copy_to_part", boom)
    before = _digest(source)
    [move] = storage.plan_moves(lib, [cid], "move", target_source_id=2)
    assert move.dst == str(second / "日系" / "Brand" / "Title")
    assert move.action == "move" and move.source_id == 2
    assert storage.run_moves(lib, [move], log_path=str(log)) == [cid]
    assert not os.path.lexists(source)
    assert _digest(move.dst) == before
    with lib.connection() as c:
        path, key, sid = c.execute("SELECT path,path_key,source_id FROM copies WHERE id=?",
                                   (cid,)).fetchone()
    assert path == move.dst and key == _key(move.dst) and sid == 2
    item = storage.list_storage(lib)[cid]
    assert item.location == "常用" and item.path == move.dst
    line = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert line["action"] == "move" and line["result"] == "ok"


def test_move_fallback_uses_category_and_work_title(lib, tmp_path):
    first, second = _sources(lib, tmp_path)
    loose = _folder(tmp_path / "loose" / "Whatever")
    _add_work(lib, 1, "标题")
    shelf.set_category(lib, 1, "日系")
    cid = _add_copy(lib, 1, None, loose)
    [move] = storage.plan_moves(lib, [cid], "move", target_source_id=2)
    assert move.dst == str(second / "Games" / "日系" / "标题")
    assert storage.run_moves(lib, [move]) == [cid]
    assert os.path.isdir(Path(move.dst))


def test_move_rejects_existing_target(lib, tmp_path):
    first, second = _sources(lib, tmp_path)
    source = _folder(first / "日系" / "A")
    _add_work(lib, 1, "A")
    cid = _add_copy(lib, 1, 1, source)
    _folder(second / "日系" / "A")
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [cid], "move", target_source_id=2)
    assert "目标已存在" in str(caught.value)


def test_move_rejects_cold_copy(lib, tmp_path):
    first, second = _sources(lib, tmp_path)
    source = _folder(first / "日系" / "A")
    _add_work(lib, 1, "A")
    cid = _add_copy(lib, 1, 1, source)
    with lib.transaction() as c:
        c.execute(storage._CREATE_SQL)
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) "
                  "VALUES(?, '冷藏', ?, '2020')", (cid, str(source)))
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [cid], "move", target_source_id=2)
    assert "不在常用盘" in str(caught.value)


def test_move_rejects_target_inside_source(lib, tmp_path):
    first, _second = _sources(lib, tmp_path)
    source = _folder(first / "日系" / "A")
    _add_work(lib, 1, "A")
    cid = _add_copy(lib, 1, 1, source)
    with lib.transaction() as c:
        c.execute("INSERT INTO sources(id,path,path_key,label,added_at) VALUES(3,?,?,?,?)",
                  (str(source / "nested"), _key(source / "nested"), "", "2020"))
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [cid], "move", target_source_id=3)
    assert "互相包含" in str(caught.value)


def test_move_checks_free_space_for_cross_volume(lib, tmp_path, monkeypatch):
    first, second = _sources(lib, tmp_path)
    source = _folder(first / "日系" / "Big")
    _add_work(lib, 1, "Big")
    cid = _add_copy(lib, 1, 1, source)
    monkeypatch.setattr(storage, "_same_volume", lambda *args: False)
    Usage = type("Usage", (), {"total": 10 << 30, "used": 9 << 30, "free": 1 << 30})
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: Usage())
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [cid], "move", target_source_id=2)
    assert "空间不足" in str(caught.value)


def test_failed_move_commit_restores_rename(lib, tmp_path, monkeypatch):
    first, second = _sources(lib, tmp_path)
    source = _folder(first / "日系" / "Restore")
    _add_work(lib, 1, "Restore")
    cid = _add_copy(lib, 1, 1, source)
    before = _dump(lib)
    [move] = storage.plan_moves(lib, [cid], "move", target_source_id=2)

    def boom(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(storage, "_commit", boom)
    with pytest.raises(storage.StorageError) as caught:
        storage.run_moves(lib, [move])
    assert "库没有提交" in str(caught.value) and "移回原位置" in str(caught.value)
    assert os.path.isdir(source) and not os.path.lexists(move.dst)
    assert _dump(lib) == before


# ---------------------------------------------------------------- badge / UI
def _copy(cid, path, source_id):
    return Copy(cid, "folder", path, "game.exe", 10, "ok", "2020", source_id, "scan")


def test_folder_badge_text_single_multi_label_and_cold():
    d = Source(1, r"D:\Games", "", None)
    g = Source(2, r"G:\Games", "G 盘", None)
    copies = [_copy(1, r"D:\Games\A", 1)]
    assert L.folder_badge_text(copies, {1: d, 2: g}) == "D"
    copies.append(_copy(2, r"G:\Games\A", 2))
    assert L.folder_badge_text(copies, {1: d, 2: g}) == "D+G"
    cold = storage.CopyStorage(2, 1, "folder", r"G:\cold\A", "冷藏", r"G:\Games\A")
    assert L.folder_badge_text(copies, {1: d, 2: g}, {2: cold}) == "D"
    assert L.folder_badge_text([], {1: d}) is None
    unc = Source(3, r"\\nas\games", "NAS", None)
    assert L.folder_badge_text([_copy(3, r"\\nas\games\A", 3)], {3: unc}) == "NAS"


def _make_folders_lib(tmp_path):
    first = tmp_path / "D" / "Games"
    second = tmp_path / "G" / "Games"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    path = tmp_path / "folders-ui.galdb"
    with store.create_library(str(path)) as lib:
        _add_source(lib, 1, first, "D 盘")
        _add_source(lib, 2, second, "G 盘")
        sid_d, sid_g = 1, 2
        _add_work(lib, 1, "Alpha")
        _add_work(lib, 2, "Bravo")
        _folder(first / "日系" / "Alpha")
        _folder(second / "同人" / "Bravo")
        _add_copy(lib, 1, sid_d, first / "日系" / "Alpha")
        _add_copy(lib, 2, sid_g, second / "同人" / "Bravo")
    return path, sid_d, sid_g


def _open(window, app, path):
    window.open_library_path(str(path))
    wait(app, window)
    window.show_library()
    wait(app, window)
    return window.library_page


def test_sidebar_lists_game_folders_and_filters(window, app, tmp_path):
    path, sid_d, sid_g = _make_folders_lib(tmp_path)
    page = _open(window, app, path)
    assert set(page.folder_buttons) == {sid_d, sid_g}
    assert page.folder_buttons[sid_d].text() == "D 盘 1"
    assert page.folder_buttons[sid_g].text() == "G 盘 1"
    page.set_location_filter(L.folder_key(sid_g))
    assert [w.id for w in page.model.works] == [2]
    page.set_location_filter(L.folder_key(sid_d))
    assert [w.id for w in page.model.works] == [1]
    page.set_location_filter("全部")
    assert sorted(w.id for w in page.model.works) == [1, 2]


def test_detail_shows_each_copy_folder(window, app, tmp_path):
    path, sid_d, _sid_g = _make_folders_lib(tmp_path)
    page = _open(window, app, path)
    page.open_detail(1)
    app.processEvents()
    label = page.detail.findChild(QLabel, "detailCopyFolders")
    assert label is not None and "D 盘" in label.text()
    assert label.isVisible()


def test_move_menu_action_is_present(window, app, tmp_path):
    path, _sid_d, _sid_g = _make_folders_lib(tmp_path)
    page = _open(window, app, path)
    page.open_detail(1)
    app.processEvents()
    page.detail._rebuild_more_menu()
    actions = {a.text(): a.isEnabled() for a in page.detail.more_menu.actions() if a.text()}
    assert actions["移到其他游戏目录…"] is True


def test_source_page_rename_and_merge_button(window, app, tmp_path):
    path, sid_d, _sid_g = _make_folders_lib(tmp_path)
    _open(window, app, path)
    source_page = window.source_page
    assert source_page.table.columnCount() == 4
    assert source_page.table.item(0, 1).text() == "D 盘"
    before = source_page.table.item(1, 1).text()
    source_page.set_label(source_page.sources[1], "第二库")
    wait(app, window)
    assert source_page.table.item(1, 1).text() == "第二库"
    assert before == "G 盘"
    assert any(button.text() == "合并另一个库…"
               for button in source_page.findChildren(QPushButton))


def test_move_works_ui_moves_and_refreshes(window, app, tmp_path):
    path, sid_d, sid_g = _make_folders_lib(tmp_path)
    page = _open(window, app, path)
    source = tmp_path / "D" / "Games" / "日系" / "Alpha"
    assert (source / "game.exe").is_file()
    window.ask_move_target = lambda items: sid_g
    window.confirm_moves = lambda moves: True
    window.move_works([1])
    wait(app, window)
    assert not source.exists()
    target = tmp_path / "G" / "Games" / "日系" / "Alpha"
    assert (target / "game.exe").is_file()
    with window.lib.connection() as c:
        assert c.execute("SELECT source_id FROM copies WHERE work_id=1").fetchone()[0] == sid_g
    assert "已把 1 部移到" in window.status.message.text()


def test_source_page_merge_preview_starts_merge(window, app, tmp_path, monkeypatch):
    path, _sid_d, _sid_g = _make_folders_lib(tmp_path)
    _open(window, app, path)
    source_page = window.source_page
    plan = merge.MergePlan(2, 1, 0, 1, 1, (("A", "B"),), (("G:\\Games", "G"),))
    monkeypatch.setattr(merge, "plan_merge", lambda lib, src: plan)
    seen = []
    monkeypatch.setattr(source_page, "ask_merge_plan", lambda item: True)
    monkeypatch.setattr(window, "merge_library", lambda src: seen.append(src))
    source_page.start_merge("G:\\other.galdb")
    wait(app, window)
    assert seen == ["G:\\other.galdb"]
