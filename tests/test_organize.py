"""Tests for the Games-layout organizer (plan / run / undo)."""
import json
import os
from pathlib import Path
import shutil

import pytest

from galpanel2.core import storage
from galpanel2.core.domain import path_key


def _key(path):
    try:
        return path_key(str(path))
    except ValueError:
        return os.path.normcase(os.path.abspath(str(path)))


def _source(lib, path):
    with lib.transaction() as c:
        c.execute("INSERT INTO sources(path,path_key,label,added_at) VALUES(?,?,?,?)",
                  (str(path), _key(path), "", "2020-01-01"))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_work(lib, work_id, title, copies, *, category=None, brands=()):
    with lib.transaction() as c:
        c.execute("INSERT INTO works(id,uuid,title,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?)",
                  (work_id, f"uuid-{work_id}", title, 0, "2020-01-01", "2020-01-01"))
    if category:
        with lib.transaction() as c:
            c.execute("CREATE TABLE IF NOT EXISTS work_shelf(work_id INTEGER PRIMARY KEY,"
                      "category TEXT,status TEXT,updated_at TEXT NOT NULL)")
            c.execute("INSERT OR IGNORE INTO work_shelf(work_id,category,status,updated_at) "
                      "VALUES(?,?,?,?)", (work_id, category, None, "2020-01-01"))
    for position, name in enumerate(brands):
        with lib.transaction() as c:
            row = c.execute("SELECT id FROM brands WHERE name_key=?", (name.casefold(),)).fetchone()
            if row is None:
                c.execute("INSERT INTO brands(name,name_key,created_at) VALUES(?,?,?)",
                          (name, name.casefold(), "2020-01-01"))
                brand_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                brand_id = row[0]
            c.execute("INSERT OR IGNORE INTO work_brands(work_id,brand_id,position) VALUES(?,?,?)",
                      (work_id, brand_id, position))
    ids = []
    for kind, path, source_id in copies:
        with lib.transaction() as c:
            c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,"
                      "availability,origin,added_at) VALUES(?,?,?,?,?,?,?,'scan','2020-01-01')",
                      (work_id, source_id, kind, str(path), _key(path),
                       "game.exe" if kind == "folder" else None, "ok"))
            ids.append(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    return ids


def _folder(path, files=None):
    path = Path(path)
    path.mkdir(parents=True)
    for name, data in (files or {"game.exe": b"MZ"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return path


def _archive(path, data=b"PK\x03\x04abc"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _state(lib, copy_id):
    with lib.connection() as c:
        return c.execute("SELECT path,path_key FROM copies WHERE id=?", (copy_id,)).fetchone()


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "D"
    path.mkdir()
    return path


def test_plan_single_folder(root, lib):
    source = _source(lib, root)
    copy = _folder(root / "old" / "Some Game")
    [copy_id] = _add_work(lib, 1, "作品甲", [("folder", copy, source)],
                          category="日系", brands=("品牌A", "品牌B"))
    plan = storage.plan_organize(lib)
    assert plan.already_ok == 0 and not plan.skipped
    assert len(plan.moves) == 1
    move = plan.moves[0]
    assert move.action == "organize" and move.source_id == source
    assert move.src == str(copy)
    assert move.dst == str(root / "Games" / "日系" / "品牌A" / "作品甲")
    assert move.size_bytes > 0
    # planning is read-only
    assert copy.is_dir() and not (root / "Games").exists()
    assert _state(lib, copy_id)[0] == str(copy)


def test_plan_single_archive_keeps_filename(root, lib):
    source = _source(lib, root)
    archive = _archive(root / "dl" / "game.zip")
    _add_work(lib, 1, "作品乙", [("archive", archive, source)], category="同人",
              brands=("品牌B",))
    plan = storage.plan_organize(lib)
    assert plan.moves[0].dst == str(root / "Games" / "同人" / "品牌B" / "作品乙" / "game.zip")


def test_plan_multi_copy_keeps_original_names(root, lib):
    source = _source(lib, root)
    first = _folder(root / "v1" / "Game A")
    second = _archive(root / "v2" / "Game A DL.rar")
    _add_work(lib, 1, "同名作品", [("folder", first, source), ("archive", second, source)],
              category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    title = root / "Games" / "日系" / "品牌A" / "同名作品"
    assert {move.dst for move in plan.moves} == {str(title / "Game A"),
                                                 str(title / "Game A DL.rar")}


def test_plan_duplicate_names_are_numbered(root, lib):
    source = _source(lib, root)
    one = _folder(root / "a" / "Game")
    two = _folder(root / "b" / "Game")
    _add_work(lib, 1, "重复名", [("folder", one, source), ("folder", two, source)],
              category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    title = root / "Games" / "日系" / "品牌A" / "重复名"
    assert [move.dst for move in plan.moves] == [str(title / "Game"), str(title / "Game (2)")]


def test_plan_placeholders_for_missing_category_and_brand(root, lib):
    source = _source(lib, root)
    copy = _folder(root / "loose" / "无名")
    _add_work(lib, 1, "无分类作品", [("folder", copy, source)])
    plan = storage.plan_organize(lib)
    assert plan.moves[0].dst == str(root / "Games" / "未分类" / "未知品牌" / "无分类作品")


def test_plan_windows_illegal_and_reserved_names(root, lib):
    source = _source(lib, root)
    copy = _folder(root / "bad" / "x")
    _add_work(lib, 1, 'CON:<>|?* "x". ', [("folder", copy, source)], category="日系",
              brands=("AUX",))
    plan = storage.plan_organize(lib)
    dst = Path(plan.moves[0].dst)
    assert "CON_" in dst.parts[-1]
    assert not any(char in str(dst.name) for char in '<>:"/\\|?*')
    assert dst.parts[-2] == "AUX_"


def test_plan_already_ok_is_not_moved(root, lib):
    source = _source(lib, root)
    copy = _folder(root / "Games" / "日系" / "品牌A" / "就位作品")
    _add_work(lib, 1, "就位作品", [("folder", copy, source)], category="日系",
              brands=("品牌A",))
    plan = storage.plan_organize(lib)
    assert not plan.moves and plan.already_ok == 1


def test_plan_volumes_are_extras(root, lib):
    source = _source(lib, root)
    main = _archive(root / "vol" / "big.part1.rar", b"a")
    _archive(root / "vol" / "big.part2.rar", b"b")
    _archive(root / "vol" / "big.part3.rar", b"c")
    _add_work(lib, 1, "分卷作品", [("archive", main, source)], category="日系",
              brands=("品牌A",))
    plan = storage.plan_organize(lib)
    move = plan.moves[0]
    title = root / "Games" / "日系" / "品牌A" / "分卷作品"
    assert move.dst == str(title / "big.part1.rar")
    assert [Path(dst).name for _src, dst in move.extras] == ["big.part2.rar", "big.part3.rar"]


def test_plan_seven_zip_numbered_volumes(root, lib):
    source = _source(lib, root)
    main = _archive(root / "vol" / "x.7z.001")
    _archive(root / "vol" / "x.7z.002")
    _add_work(lib, 1, "七哥", [("archive", main, source)], category="日系",
              brands=("品牌A",))
    move = storage.plan_organize(lib).moves[0]
    assert [Path(dst).name for _src, dst in move.extras] == ["x.7z.002"]


def test_plan_target_exists_is_skipped(root, lib):
    source = _source(lib, root)
    copy = _folder(root / "old" / "已占")
    (root / "Games" / "日系" / "品牌A" / "目标").mkdir(parents=True)
    _add_work(lib, 1, "目标", [("folder", copy, source)], category="日系",
              brands=("品牌A",))
    plan = storage.plan_organize(lib)
    assert not plan.moves
    assert plan.skipped[0][0] == 1 and "目标已存在" in plan.skipped[0][2]


def test_plan_cross_volume_is_skipped(root, lib, monkeypatch):
    source = _source(lib, root)
    copy = _folder(root / "old" / "跨盘")
    _add_work(lib, 1, "跨盘", [("folder", copy, source)], category="日系",
              brands=("品牌A",))
    monkeypatch.setattr(storage, "_same_volume", lambda src, dst: False)
    plan = storage.plan_organize(lib)
    assert not plan.moves and "跨盘" in plan.skipped[0][2]


def test_plan_cold_and_deleted_are_skipped(root, lib):
    source = _source(lib, root)
    cold = _folder(root / "old" / "冷")
    deleted = _folder(root / "old" / "删")
    ids = _add_work(lib, 1, "冷作品", [("folder", cold, source), ("folder", deleted, source)],
                    category="日系", brands=("品牌A",))
    with lib.transaction() as c:
        c.execute("CREATE TABLE IF NOT EXISTS copy_storage(copy_id INTEGER PRIMARY KEY,"
                  "state TEXT NOT NULL,home_path TEXT,updated_at TEXT NOT NULL)")
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (ids[0], "冷藏", str(cold), "2020-01-01"))
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (ids[1], "已删除", None, "2020-01-01"))
    plan = storage.plan_organize(lib)
    assert not plan.moves
    reasons = {row[0]: row[2] for row in plan.skipped}
    assert reasons[ids[0]] == "冷藏中" and reasons[ids[1]] == "已删除"


def test_plan_outside_root_and_missing_source(root, lib):
    source = _source(lib, root)
    outside = root.parent / "outside"
    outside.mkdir()
    copy = _folder(outside / "Game")
    missing = root / "gone" / "NotThere"
    _add_work(lib, 1, "外面", [("folder", copy, None)], category="日系", brands=("品牌A",))
    _add_work(lib, 2, "没了", [("folder", missing, source)], category="日系",
              brands=("品牌A",))
    plan = storage.plan_organize(lib)
    reasons = {row[0]: row[2] for row in plan.skipped}
    assert reasons[1] == "不在任何游戏目录里"
    assert reasons[2] == "源路径不存在"


def test_plan_source_ids_filter(root, lib):
    one = _source(lib, root)
    other_root = root.parent / "G"
    other_root.mkdir()
    two = _source(lib, other_root)
    first = _folder(root / "a" / "甲")
    second = _folder(other_root / "b" / "乙")
    _add_work(lib, 1, "甲", [("folder", first, one)], category="日系", brands=("品牌A",))
    _add_work(lib, 2, "乙", [("folder", second, two)], category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib, source_ids=[one])
    assert [move.src for move in plan.moves] == [str(first)]


def test_run_moves_files_and_path_key(root, lib, tmp_path):
    source = _source(lib, root)
    copy = _folder(root / "old" / "跑")
    [copy_id] = _add_work(lib, 1, "跑起来", [("folder", copy, source)], category="日系",
                          brands=("品牌A",))
    plan = storage.plan_organize(lib)
    log = tmp_path / "storage-log.jsonl"
    result = storage.run_organize(lib, plan, log_path=str(log))
    assert result.moved == 1 and not result.failed and not result.integrity_check
    target = root / "Games" / "日系" / "品牌A" / "跑起来"
    assert (target / "game.exe").is_file() and not copy.exists()
    path, key = _state(lib, copy_id)
    assert path == str(target) and key == _key(target)
    records = storage.read_organize_log(str(log))
    assert records[0]["action"] == "organize" and records[0]["result"] == "ok"
    assert records[0]["batch"] == result.batch


def test_run_removes_empty_dirs_and_reports_leftovers(root, lib, tmp_path):
    source = _source(lib, root)
    first = _folder(root / "trash" / "a" / "甲")
    second = _folder(root / "trash" / "b" / "乙")
    _add_work(lib, 1, "甲", [("folder", first, source)], category="日系", brands=("品牌A",))
    _add_work(lib, 2, "乙", [("folder", second, source)], category="日系", brands=("品牌A",))
    (root / "trash" / "b" / "readme.txt").write_text("keep", encoding="utf-8")
    plan = storage.plan_organize(lib)
    result = storage.run_organize(lib, plan, log_path=str(tmp_path / "log.jsonl"))
    assert not (root / "trash" / "a").exists()
    assert (root / "trash" / "b" / "readme.txt").is_file()
    assert str(root / "trash") in result.leftover_dirs
    assert result.removed_empty_dirs
    assert all(not os.path.lexists(path) for path in result.removed_empty_dirs)
    assert all(str(path).startswith(str(root)) for path in result.removed_empty_dirs)
    assert str(root / "Games") not in result.removed_empty_dirs


def test_run_failure_rolls_back_item_and_continues(root, lib, tmp_path, monkeypatch):
    source = _source(lib, root)
    good = _folder(root / "old" / "好")
    bad = _folder(root / "old" / "坏")
    _add_work(lib, 1, "好", [("folder", good, source)], category="日系", brands=("品牌A",))
    _add_work(lib, 2, "坏", [("folder", bad, source)], category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    real_rename = os.rename

    def flaky(src, dst):
        if str(src) == str(bad):
            raise PermissionError("locked")
        return real_rename(src, dst)

    monkeypatch.setattr(storage.os, "rename", flaky)
    result = storage.run_organize(lib, plan, log_path=str(tmp_path / "log.jsonl"))
    assert result.moved == 1 and len(result.failed) == 1
    assert result.failed[0][0] == plan.moves[1].copy_id
    assert bad.is_dir() and (bad / "game.exe").is_file()
    assert not (root / "Games" / "日系" / "品牌A" / "坏").exists()
    assert (root / "Games" / "日系" / "品牌A" / "好" / "game.exe").is_file()


def test_run_failure_rolls_back_extras(root, lib, tmp_path, monkeypatch):
    source = _source(lib, root)
    main = _archive(root / "vol" / "x.part1.rar")
    _archive(root / "vol" / "x.part2.rar")
    _add_work(lib, 1, "分卷", [("archive", main, source)], category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    real_rename = os.rename

    def flaky(src, dst):
        if Path(str(src)).name == "x.part2.rar":
            raise PermissionError("volume locked")
        return real_rename(src, dst)

    monkeypatch.setattr(storage.os, "rename", flaky)
    result = storage.run_organize(lib, plan, log_path=str(tmp_path / "log.jsonl"))
    assert result.moved == 0 and len(result.failed) == 1
    assert main.is_file() and (root / "vol" / "x.part2.rar").is_file()
    assert not (root / "Games" / "日系" / "品牌A" / "分卷").exists()


def test_run_creates_backup_before_moving(root, lib, tmp_path):
    source = _source(lib, root)
    copy = _folder(root / "old" / "备份")
    _add_work(lib, 1, "备份", [("folder", copy, source)], category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    result = storage.run_organize(lib, plan, log_path=str(tmp_path / "log.jsonl"))
    assert result.backup_path and os.path.isfile(result.backup_path)
    assert ".pre-organize-" in result.backup_path
    assert Path(result.backup_path).stat().st_size > 0


def test_undo_restores_files_and_database(root, lib, tmp_path):
    source = _source(lib, root)
    copy = _archive(root / "dl" / "undo.zip")
    [copy_id] = _add_work(lib, 1, "撤销", [("archive", copy, source)], category="日系",
                          brands=("品牌A",))
    plan = storage.plan_organize(lib)
    log = tmp_path / "log.jsonl"
    storage.run_organize(lib, plan, log_path=str(log))
    assert not copy.exists()
    result = storage.undo_organize(lib, str(log))
    assert result.moved == 1 and not result.failed and not result.integrity_check
    path, key = _state(lib, copy_id)
    assert copy.is_file() and path == str(copy) and key == _key(copy)


def test_undo_restores_extras(root, lib, tmp_path):
    source = _source(lib, root)
    main = _archive(root / "vol" / "x.part1.rar")
    second = _archive(root / "vol" / "x.part2.rar")
    _add_work(lib, 1, "分卷", [("archive", main, source)], category="日系", brands=("品牌A",))
    log = tmp_path / "log.jsonl"
    storage.run_organize(lib, storage.plan_organize(lib), log_path=str(log))
    assert not main.exists() and not second.exists()
    result = storage.undo_organize(lib, str(log))
    assert result.moved == 1
    assert main.is_file() and second.is_file()


def test_undo_skips_occupied_original(root, lib, tmp_path):
    source = _source(lib, root)
    copy = _folder(root / "old" / "占着")
    [copy_id] = _add_work(lib, 1, "占着", [("folder", copy, source)], category="日系",
                          brands=("品牌A",))
    log = tmp_path / "log.jsonl"
    storage.run_organize(lib, storage.plan_organize(lib), log_path=str(log))
    copy.mkdir(parents=True)
    result = storage.undo_organize(lib, str(log))
    assert result.moved == 0 and result.skipped
    assert "占用" in result.skipped[0][2]
    target = root / "Games" / "日系" / "品牌A" / "占着"
    assert (target / "game.exe").is_file()
    assert _state(lib, copy_id)[0] == str(target)


def test_long_path_shortens_title_only(root, lib):
    deep = root
    # Deep enough on every OS (Windows temp roots are short) to force shortening.
    while len(str(deep)) < 150:
        deep = deep / "nested_directory_level"
    deep.mkdir(parents=True)
    source = _source(lib, deep)
    copy = _folder(deep / "old" / "long")
    title = "很长的标题" * 20
    _add_work(lib, 1, title, [("folder", copy, source)], category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    assert len(plan.moves) == 1
    dst = plan.moves[0].dst
    assert len(dst) <= 240
    assert Path(dst).name != storage._organize_segment(title)


def test_too_long_even_with_short_title_is_skipped(root, lib):
    deep = root
    for index in range(10):
        deep = deep / ("nested%02d" % index)
    deep.mkdir(parents=True)
    source = _source(lib, deep)
    copy = _folder(deep / "x")
    _add_work(lib, 1, "标题", [("folder", copy, source)],
              category="日系", brands=("很长很长的品牌" * 20,))
    plan = storage.plan_organize(lib)
    assert not plan.moves


def test_plan_wrong_copy_id_is_reported(root, lib):
    source = _source(lib, root)
    copy = _folder(root / "old" / "x")
    _add_work(lib, 1, "x", [("folder", copy, source)], category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib, copy_ids=[999])
    assert not plan.moves and "不存在" in plan.skipped[0][2]


def test_plan_multiple_copies_already_numbered_on_disk(root, lib):
    source = _source(lib, root)
    one = _folder(root / "Games" / "日系" / "品牌A" / "多份" / "Game A")
    two = _folder(root / "Games" / "日系" / "品牌A" / "多份" / "Game B")
    _add_work(lib, 1, "多份", [("folder", one, source), ("folder", two, source)],
              category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    assert not plan.moves and plan.already_ok == 2


def test_plan_self_nesting_copy_is_marked_and_ordered(root, lib):
    source = _source(lib, root)
    title = root / "Games" / "日系" / "品牌A" / "嵌套作品"
    inside = _folder(title)
    outside = _folder(root / "old" / "Other")
    _add_work(lib, 1, "嵌套作品", [("folder", outside, source), ("folder", inside, source)],
              category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    assert not plan.skipped
    nested = [move for move in plan.moves if move.nested]
    assert len(nested) == 1
    assert nested[0].src == str(title)
    assert nested[0].dst == str(title / "嵌套作品")
    # The self-nesting copy must vacate the title folder before its sibling.
    assert plan.moves[0].nested
    assert Path(plan.moves[0].dst).parent == title


def test_run_and_undo_self_nesting_copy(root, lib, tmp_path):
    source = _source(lib, root)
    title = root / "Games" / "日系" / "品牌A" / "嵌套作品"
    inside = _folder(title)
    outside = _folder(root / "old" / "Other")
    ids = _add_work(lib, 1, "嵌套作品",
                    [("folder", outside, source), ("folder", inside, source)],
                    category="日系", brands=("品牌A",))
    log = tmp_path / "log.jsonl"
    result = storage.run_organize(lib, storage.plan_organize(lib), log_path=str(log))
    assert result.moved == 2 and not result.failed and not result.integrity_check
    target = title / "嵌套作品"
    assert (target / "game.exe").is_file() and not (title / "game.exe").exists()
    assert (title / "Other" / "game.exe").is_file() and not outside.exists()
    records = storage.read_organize_log(str(log))
    nested_record = next(record for record in records if record.get("nested"))
    assert nested_record["temp"] == str(title) + f".gp2tmp-{nested_record['copy_id']}"

    undo = storage.undo_organize(lib, str(log))
    assert undo.moved == 2 and not undo.failed and not undo.integrity_check
    assert (inside / "game.exe").is_file()
    assert (outside / "game.exe").is_file()
    assert not target.exists()
    assert _state(lib, ids[1])[0] == str(inside)


def test_run_self_nesting_failure_rolls_back(root, lib, tmp_path, monkeypatch):
    source = _source(lib, root)
    title = root / "Games" / "日系" / "品牌A" / "嵌套作品"
    inside = _folder(title)
    outside = _folder(root / "old" / "Other")
    _add_work(lib, 1, "嵌套作品", [("folder", outside, source), ("folder", inside, source)],
              category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    real_rename = os.rename
    state = {"failed": False}

    def flaky(src, dst):
        if ".gp2tmp-" in Path(str(src)).name and not state["failed"]:
            state["failed"] = True
            raise PermissionError("locked")
        return real_rename(src, dst)

    monkeypatch.setattr(storage.os, "rename", flaky)
    result = storage.run_organize(lib, plan, log_path=str(tmp_path / "log.jsonl"))
    assert result.moved == 1 and len(result.failed) == 1
    assert (title / "game.exe").is_file()
    assert not (title / "嵌套作品").exists()
    assert not list(title.parent.glob("*.gp2tmp-*"))
    assert (title / "Other" / "game.exe").is_file()


def test_plan_unreadable_size_is_warning_not_skip(root, lib, tmp_path, monkeypatch):
    source = _source(lib, root)
    copy = _folder(root / "old" / "读不了")
    _add_work(lib, 1, "读不了", [("folder", copy, source)], category="日系",
              brands=("品牌A",))

    def broken(_path):
        raise storage.StorageError("无法读取大小：读不了（WinError 1392）")

    monkeypatch.setattr(storage, "_tree_size", broken)
    plan = storage.plan_organize(lib)
    assert not plan.skipped and len(plan.moves) == 1
    move = plan.moves[0]
    assert move.size_bytes == 0 and move.warnings
    assert "1392" in move.warnings[0]
    result = storage.run_organize(lib, plan, log_path=str(tmp_path / "log.jsonl"))
    assert result.moved == 1 and not result.failed
    assert (root / "Games" / "日系" / "品牌A" / "读不了" / "game.exe").is_file()


@pytest.mark.parametrize("name", ["B014", "1346044", "104", "D755-1", "012708"])
def test_plan_multi_copy_code_like_names_use_title(root, lib, name):
    source = _source(lib, root)
    coded = _folder(root / "a" / name)
    named = _folder(root / "b" / "Game A")
    _add_work(lib, 1, "作品丙", [("folder", coded, source), ("folder", named, source)],
              category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    assert {Path(move.dst).name for move in plan.moves} == {"作品丙", "Game A"}


def test_plan_multi_copy_code_like_archive_names_use_title(root, lib):
    source = _source(lib, root)
    one = _archive(root / "a" / "B014.rar")
    two = _archive(root / "b" / "1346044.rar")
    _add_work(lib, 1, "作品丁", [("archive", one, source), ("archive", two, source)],
              category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    assert {Path(move.dst).name for move in plan.moves} == {"作品丁.rar", "作品丁 (2).rar"}


def test_list_organize_batches_groups_runs(root, lib, tmp_path):
    source = _source(lib, root)
    copy = _folder(root / "old" / "批")
    _add_work(lib, 1, "批", [("folder", copy, source)], category="日系", brands=("品牌A",))
    log = tmp_path / "log.jsonl"
    first = storage.run_organize(lib, storage.plan_organize(lib), log_path=str(log))
    assert len(storage.list_organize_batches(str(log))) == 1
    batch = storage.list_organize_batches(str(log))[0]
    assert batch["moved"] == 1 and batch["batch"] == first.batch


def test_run_cancel_between_items_and_keeps_finished(root, lib, tmp_path):
    source = _source(lib, root)
    one = _folder(root / "old" / "一")
    two = _folder(root / "old" / "二")
    _add_work(lib, 1, "一", [("folder", one, source)], category="日系", brands=("品牌A",))
    _add_work(lib, 2, "二", [("folder", two, source)], category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    state = {"count": 0}

    def cancel():
        state["count"] += 1
        return state["count"] > 1

    with pytest.raises(storage.Cancelled):
        storage.run_organize(lib, plan, cancel=cancel, log_path=str(tmp_path / "log.jsonl"))
    assert (root / "Games" / "日系" / "品牌A" / "一").is_dir()
    assert two.is_dir()


def test_integrity_check_reports_missing_target(root, lib, tmp_path):
    source = _source(lib, root)
    copy = _folder(root / "old" / "缺")
    _add_work(lib, 1, "缺", [("folder", copy, source)], category="日系", brands=("品牌A",))
    plan = storage.plan_organize(lib)
    storage.run_organize(lib, plan, log_path=str(tmp_path / "log.jsonl"))
    shutil.rmtree(root / "Games" / "日系" / "品牌A" / "缺")
    result = storage.undo_organize(lib, str(tmp_path / "log.jsonl"))
    assert result.moved == 0 and result.skipped
    assert "不存在" in result.skipped[0][2]
