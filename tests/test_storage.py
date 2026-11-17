"""Tests for galpanel2.core.storage (cold archive and permanent delete)."""
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from galpanel2.core import storage, store
from galpanel2.core.domain import path_key


def _key(path):
    try:
        return path_key(path)
    except ValueError:
        return os.path.normcase(os.path.abspath(path))


def _insert_work(lib, work_id, title, copies, hidden=0):
    """Insert one work and its copies; return the new copy ids in order."""
    with lib.transaction() as c:
        c.execute("INSERT INTO works(id,uuid,title,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?)",
                  (work_id, f"uuid-{work_id}", title, hidden, "2020-01-01", "2020-01-01"))
    ids = []
    for kind, path in copies:
        with lib.transaction() as c:
            c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,"
                      "availability,origin,added_at) VALUES(?,NULL,?,?,?,?,'ok','scan','2020-01-01')",
                      (work_id, kind, str(path), _key(str(path)), "game.exe" if kind == "folder" else None))
            ids.append(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    return ids


def _folder(path, files=None, empty=()):
    path = Path(path)
    path.mkdir(parents=True)
    for name, data in (files or {"game.exe": b"MZ" + b"x" * 100}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for name in empty:
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def _archive(path, size=1024):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x03\x04" + b"a" * max(1, size - 4))
    return path


def _digest(path):
    path = Path(path)
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    out = {}
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        out[relative] = "<dir>" if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest()
    return out


def _dump(lib):
    with lib.connection() as c:
        return tuple(c.iterdump())


def _revisions(lib):
    with lib.connection() as c:
        return dict(c.execute("SELECT id,revision FROM works"))


def _state(lib, copy_id):
    with lib.connection() as c:
        return c.execute("SELECT path,path_key,availability FROM copies WHERE id=?", (copy_id,)).fetchone()


@pytest.fixture
def game_root(tmp_path):
    root = tmp_path / "D"
    (root / "Games").mkdir(parents=True)
    return root


@pytest.fixture
def cold_root(tmp_path):
    root = tmp_path / "G" / "冷藏" / "Games"
    root.mkdir(parents=True)
    return root


def test_constants_and_exception_hierarchy():
    assert storage.LOCATIONS == ("常用", "冷藏", "已删除")
    assert storage.DEFAULT_COLD_ROOT == "D:\\ColdStorage\\Games"
    assert issubclass(storage.Cancelled, storage.StorageError)
    for name in ("list_storage", "work_locations", "cold_target", "plan_moves",
                 "run_moves", "delete_copies", "CopyStorage", "Move"):
        assert hasattr(storage, name)
    move = storage.Move(1, r"D:\x", r"G:\y", 3)
    assert move.size_bytes == 3
    item = storage.CopyStorage(1, 2, "folder", r"D:\x", "常用", None)
    assert item.location == "常用" and item.home_path is None


def test_cold_target_translates_and_rejects_bad_paths():
    assert storage.cold_target(r"D:\Library\Games\日系\POISON\导师X催眠", r"G:\冷藏\Games") == \
        r"G:\冷藏\Games\日系\POISON\导师X催眠"
    assert storage.cold_target("D:/Library/games/欧美/A/B", r"G:\冷藏\Games") == r"G:\冷藏\Games\欧美\A\B"
    assert storage.cold_target(r"D:\Library\Games\其他\Z\x.zip", r"E:\c") == r"E:\c\其他\Z\x.zip"
    for bad in (r"D:\Library\Foo\Bar", r"D:\Library\Games", "D:\\Library\\Games\\", ""):
        with pytest.raises(storage.StorageError):
            storage.cold_target(bad, r"G:\冷藏\Games")


def test_list_storage_returns_all_and_creates_nothing(lib, tmp_path):
    _insert_work(lib, 1, "A", [("folder", _folder(tmp_path / "D" / "Games" / "A"))])
    _insert_work(lib, 2, "B", [("archive", _archive(tmp_path / "D" / "Games" / "B.zip"))])
    before = _dump(lib)
    got = storage.list_storage(lib)
    assert len(got) == 2
    assert all(item.location == "常用" and item.home_path is None for item in got.values())
    assert {item.work_id for item in got.values()} == {1, 2}
    with lib.connection() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='copy_storage'"
                         ).fetchone() is None
    assert _dump(lib) == before
    with store.open_library(lib.path, readonly=True) as ro:
        assert len(storage.list_storage(ro)) == 2
        assert storage.work_locations(ro) == {1: "常用", 2: "常用"}


def test_work_locations_aggregation(lib, tmp_path, game_root):
    a = _insert_work(lib, 1, "普通", [("folder", _folder(game_root / "Games" / "日系" / "A"))])[0]
    b = _insert_work(lib, 2, "冷藏", [("folder", _folder(game_root / "Games" / "日系" / "B"))])[0]
    deleted_copy = _insert_work(lib, 3, "删除", [("folder", _folder(game_root / "Games" / "日系" / "C"))])[0]
    d1 = _insert_work(lib, 4, "混合", [("folder", _folder(game_root / "Games" / "日系" / "D1")),
                                       ("folder", _folder(game_root / "Games" / "日系" / "D2"))])
    e1, e2 = _insert_work(lib, 5, "全删", [("folder", _folder(game_root / "Games" / "同人" / "E1")),
                                           ("folder", _folder(game_root / "Games" / "同人" / "E2"))])
    with lib.transaction() as c:
        c.execute(storage._CREATE_SQL)
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (b, "冷藏", str(game_root / "Games" / "日系" / "B"), "2020"))
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (deleted_copy, "已删除", None, "2020"))
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (d1[0], "冷藏", str(game_root / "Games" / "日系" / "D1"), "2020"))
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (d1[1], "已删除", None, "2020"))
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (e1, "已删除", None, "2020"))
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (e2, "已删除", None, "2020"))
    assert storage.work_locations(lib) == {1: "常用", 2: "冷藏", 3: "已删除", 4: "冷藏", 5: "已删除"}
    assert storage.list_storage(lib)[b].home_path == str(game_root / "Games" / "日系" / "B")
    assert storage.list_storage(lib)[deleted_copy].home_path is None


def test_plan_moves_rejects_nested_src_and_dst(lib, game_root):
    # cold_root inside the source would copy into itself and then delete both.
    source = _folder(game_root / "Games" / "日系" / "Nested")
    cid = _insert_work(lib, 1, "Nested", [("folder", source)])[0]
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [cid], "cold", str(source))
    assert "互相包含" in str(caught.value)

    # restore into an ancestor of src is refused for the same reason.
    child = _folder(game_root / "Games" / "同人" / "Parent" / "Child")
    rid = _insert_work(lib, 2, "Restore", [("folder", child)])[0]
    with lib.transaction() as c:
        c.execute(storage._CREATE_SQL)
        c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?)",
                  (rid, "冷藏", str(child.parent), "2020"))
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [rid], "restore")
    assert "互相包含" in str(caught.value)


def test_plan_moves_builds_dst_and_actual_size(lib, game_root, cold_root):
    source = _folder(game_root / "Games" / "日系" / "Lumen" / "标题",
                     files={"game.exe": b"MZabc", "data/a.bin": b"x" * 1234}, empty=("data/empty",))
    cid = _insert_work(lib, 1, "标题", [("folder", source)])[0]
    before = _dump(lib)
    [move] = storage.plan_moves(lib, [cid], "cold", str(cold_root))
    assert move.copy_id == cid and move.src == str(source)
    assert Path(move.dst) == cold_root / "日系" / "Lumen" / "标题"
    real = sum(item.stat().st_size for item in Path(source).rglob("*") if item.is_file())
    assert move.size_bytes == real
    assert _dump(lib) == before


def test_plan_moves_input_checks(lib, game_root, cold_root):
    good = _insert_work(lib, 1, "Good", [("folder", _folder(game_root / "Games" / "日系" / "G"))])[0]
    elsewhere = _insert_work(lib, 2, "Bad", [("folder", _folder(game_root / "elsewhere" / "B"))])[0]
    with pytest.raises(ValueError):
        storage.plan_moves(lib, [good], "move", str(cold_root))
    for ids, action in (((99999,), "cold"), ((good,), "restore"), ((elsewhere,), "cold")):
        with pytest.raises(storage.StorageError):
            storage.plan_moves(lib, ids, action, str(cold_root))


def test_plan_moves_rejects_bad_locations_and_existing_targets(lib, game_root, cold_root):
    one = _insert_work(lib, 1, "One", [("folder", _folder(game_root / "Games" / "日系" / "A"))])[0]
    two = _insert_work(lib, 2, "Two", [("folder", _folder(game_root / "Games" / "日系" / "B"))])[0]
    (cold_root / "日系" / "B").mkdir(parents=True)
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [one, two], "cold", str(cold_root))
    assert "B" in str(caught.value)
    # The whole batch is refused; the good move cannot quietly run.
    assert os.path.isdir(game_root / "Games" / "日系" / "A")


def test_plan_moves_rejects_duplicate_dst_and_occupied_target(lib, tmp_path, game_root, cold_root):
    first = _folder(tmp_path / "E" / "Games" / "同人" / "V" / "x")
    second = _folder(tmp_path / "F" / "Games" / "同人" / "V" / "x")
    a = _insert_work(lib, 1, "A", [("folder", first)])[0]
    b = _insert_work(lib, 2, "B", [("folder", second)])[0]
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [a, b], "cold", str(cold_root))
    assert "同一个目标" in str(caught.value)

    occupied = _folder(cold_root / "同人" / "V" / "x")
    _insert_work(lib, 3, "Occupied", [("folder", occupied)])
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [a], "cold", str(cold_root))
    assert "目标已存在" in str(caught.value)


def test_plan_moves_checks_free_space(lib, game_root, cold_root, monkeypatch):
    cid = _insert_work(lib, 1, "Zip", [("archive", _archive(game_root / "Games" / "其他" / "x.zip",
                                                             20 << 20))])[0]
    Usage = type("Usage", (), {"total": 10 << 30, "used": 9 << 30, "free": 1 << 30})
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: Usage())
    with pytest.raises(storage.StorageError) as caught:
        storage.plan_moves(lib, [cid], "cold", str(cold_root))
    assert "GB" in str(caught.value)
    # Build a usage with enough free space; the exact object type is irrelevant.
    enough = type("Usage", (), {"total": 10 << 30, "used": 0, "free": 2 << 30})
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: enough())
    assert len(storage.plan_moves(lib, [cid], "cold", str(cold_root))) == 1


def test_run_moves_roundtrip_and_log(lib, game_root, cold_root, tmp_path):
    folder = _folder(game_root / "Games" / "日系" / "Lumen" / "标题",
                     files={"game.exe": b"MZ" + b"x" * 3000, "data/a.bin": b"a" * 5000},
                     empty=("data/empty",))
    readonly = folder / "data" / "readonly.dat"
    readonly.write_bytes(b"r" * 100)
    os.chmod(readonly, 0o444)
    archive = _archive(game_root / "Games" / "其他" / "Zip" / "x.zip", 9 << 20)
    a = _insert_work(lib, 1, "标题", [("folder", folder)])[0]
    b = _insert_work(lib, 2, "压缩包", [("archive", archive)])[0]
    sources = [str(folder), str(archive)]
    digests = [_digest(folder), _digest(archive)]
    revisions = _revisions(lib)
    log = tmp_path / "storage-log.jsonl"
    seen = []
    moves = storage.plan_moves(lib, [a, b], "cold", str(cold_root))
    done = storage.run_moves(lib, moves, progress=lambda d, t, s: seen.append((d, t)),
                             log_path=str(log))
    assert done == [a, b]
    for copy_id, source, move, expected in zip((a, b), sources, moves, digests):
        assert not os.path.lexists(source)
        assert _digest(move.dst) == expected
        assert (Path(move.dst) / "data" / "empty").is_dir() if copy_id == a else True
        path, key, availability = _state(lib, copy_id)
        assert path == move.dst and key == _key(move.dst) and availability == "ok"
        item = storage.list_storage(lib)[copy_id]
        assert item.location == "冷藏" and item.home_path == source
    assert seen and seen[-1][0] == seen[-1][1] == sum(move.size_bytes for move in moves)
    assert _revisions(lib) == revisions
    assert not list(tmp_path.rglob("*.gp2part"))
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(x["action"], x["copy_id"], x["result"]) for x in lines] == [
        ("cold", a, "ok"), ("cold", b, "ok")]
    assert lines[0]["src"] == sources[0] and lines[0]["dst"] == moves[0].dst

    back = storage.plan_moves(lib, [a, b], "restore")
    assert [move.dst for move in back] == sources
    assert storage.run_moves(lib, back, log_path=str(log)) == [a, b]
    assert _digest(folder) == digests[0] and _digest(archive) == digests[1]
    for copy_id in (a, b):
        assert storage.list_storage(lib)[copy_id].location == "常用"
        with lib.connection() as c:
            assert c.execute("SELECT count(*) FROM copy_storage WHERE copy_id=?",
                             (copy_id,)).fetchone()[0] == 0
    assert _revisions(lib) == revisions


def test_run_moves_cancel_folder_and_archive(lib, game_root, cold_root, tmp_path):
    folder = _folder(game_root / "Games" / "日系" / "F",
                     files={"a.bin": b"a", "b.bin": b"b", "c.bin": b"c"})
    archive = _archive(game_root / "Games" / "其他" / "z.zip", 9 << 20)
    a = _insert_work(lib, 1, "Folder", [("folder", folder)])[0]
    b = _insert_work(lib, 2, "Archive", [("archive", archive)])[0]
    before = _dump(lib)
    folder_digest = _digest(folder)
    archive_digest = _digest(archive)
    log = tmp_path / "cancel.jsonl"

    calls = [0]

    def cancel_folder():
        calls[0] += 1
        return calls[0] >= 2

    moves = storage.plan_moves(lib, [a], "cold", str(cold_root))
    with pytest.raises(storage.Cancelled):
        storage.run_moves(lib, moves, cancel=cancel_folder, log_path=str(log))
    assert calls[0] >= 2
    assert _digest(folder) == folder_digest  # source still readable
    assert not os.path.lexists(moves[0].dst)
    assert not list(tmp_path.rglob("*.gp2part"))
    assert _dump(lib) == before
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["result"] == "cancelled"

    moves = storage.plan_moves(lib, [b], "cold", str(cold_root))
    with pytest.raises(storage.Cancelled):
        storage.run_moves(lib, moves, cancel=lambda: True)
    assert _digest(archive) == archive_digest
    assert not os.path.lexists(moves[0].dst)
    assert not list(tmp_path.rglob("*.gp2part"))
    assert _dump(lib) == before


def test_run_moves_progress_reaches_total(lib, game_root, cold_root):
    archive = _archive(game_root / "Games" / "其他" / "p.zip", 9 << 20)
    cid = _insert_work(lib, 1, "P", [("archive", archive)])[0]
    seen = []
    moves = storage.plan_moves(lib, [cid], "cold", str(cold_root))
    storage.run_moves(lib, moves, progress=lambda done, total, src: seen.append((done, total, src)))
    total = sum(move.size_bytes for move in moves)
    assert len(seen) >= 2 and seen[-1][0] == seen[-1][1] == total
    assert all(src == str(archive) for _, _, src in seen)


def test_run_moves_stops_batch_on_failure(lib, game_root, cold_root):
    sources = [_folder(game_root / "Games" / "日系" / f"W{i}") for i in range(3)]
    ids = [_insert_work(lib, i + 1, f"W{i}", [("folder", source)])[0]
           for i, source in enumerate(sources)]
    moves = storage.plan_moves(lib, ids, "cold", str(cold_root))
    Path(moves[1].dst).mkdir(parents=True)
    (Path(moves[1].dst) / "someone.txt").write_text("x", encoding="utf-8")
    with pytest.raises(storage.StorageError):
        storage.run_moves(lib, moves)
    state = storage.list_storage(lib)
    assert state[ids[0]].location == "冷藏" and not os.path.lexists(sources[0])
    assert state[ids[1]].location == "常用" and os.path.isdir(sources[1])
    assert state[ids[2]].location == "常用" and os.path.isdir(sources[2])
    assert not os.path.lexists(moves[2].dst)
    assert (Path(moves[1].dst) / "someone.txt").read_text(encoding="utf-8") == "x"


def test_run_moves_failure_cleans_part_and_keeps_library(lib, game_root, cold_root,
                                                         monkeypatch, tmp_path):
    source = _folder(game_root / "Games" / "日系" / "Broken")
    cid = _insert_work(lib, 1, "Broken", [("folder", source)])[0]
    moves = storage.plan_moves(lib, [cid], "cold", str(cold_root))
    before = _dump(lib)

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(storage.shutil, "copy2", boom)
    with pytest.raises(storage.StorageError) as caught:
        storage.run_moves(lib, moves)
    assert "disk on fire" in str(caught.value)
    assert not os.path.lexists(moves[0].dst)
    assert not list(tmp_path.rglob("*.gp2part"))
    assert os.path.isdir(source)
    assert _dump(lib) == before


def test_run_moves_removes_dst_when_commit_fails(lib, game_root, cold_root,
                                                monkeypatch, tmp_path):
    source = _folder(game_root / "Games" / "日系" / "CommitFail")
    cid = _insert_work(lib, 1, "CommitFail", [("folder", source)])[0]
    moves = storage.plan_moves(lib, [cid], "cold", str(cold_root))
    log = tmp_path / "commit-fail.jsonl"

    def boom(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(storage, "_commit", boom)
    with pytest.raises(storage.StorageError) as caught:
        storage.run_moves(lib, moves, log_path=str(log))
    assert "db gone" in str(caught.value)
    assert "库没有提交" in str(caught.value) and "已删除刚复制好的目标" in str(caught.value)
    assert os.path.isdir(source)
    assert not os.path.lexists(moves[0].dst)
    assert not list(tmp_path.rglob("*.gp2part"))
    assert storage.list_storage(lib)[cid].location == "常用"
    line = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert line["result"] == "failed" and "库没有提交" in line["error"]


def test_run_moves_reports_sources_left_behind(lib, game_root, cold_root, monkeypatch):
    source = _folder(game_root / "Games" / "日系" / "Locked")
    cid = _insert_work(lib, 1, "Locked", [("folder", source)])[0]
    moves = storage.plan_moves(lib, [cid], "cold", str(cold_root))
    real_remove = storage._remove_path

    def locked(path):
        if os.path.abspath(path) == os.path.abspath(source):
            raise OSError("still in use")
        return real_remove(path)

    monkeypatch.setattr(storage, "_remove_path", locked)
    left = []
    assert storage.run_moves(lib, moves, left=left) == [cid]
    assert left == [str(source)]
    assert os.path.isdir(source) and os.path.lexists(moves[0].dst)
    assert storage.list_storage(lib)[cid].location == "冷藏"


def test_run_moves_and_delete_refuse_readonly_before_touching_files(lib, game_root, cold_root):
    source = _folder(game_root / "Games" / "日系" / "Readonly")
    cid = _insert_work(lib, 1, "Readonly", [("folder", source)])[0]
    moves = storage.plan_moves(lib, [cid], "cold", str(cold_root))
    with store.open_library(lib.path, readonly=True) as ro:
        with pytest.raises(storage.StorageError):
            storage.run_moves(ro, moves)
        with pytest.raises(storage.StorageError):
            storage.delete_copies(ro, [cid])
        assert os.path.isdir(source) and not os.path.lexists(moves[0].dst)


def test_delete_copies_removes_files_and_marks(lib, game_root, tmp_path):
    folder = _folder(game_root / "Games" / "日系" / "Del",
                     files={"game.exe": b"MZ", "ro.dat": b"x"})
    os.chmod(folder / "ro.dat", 0o444)
    archive = _archive(game_root / "Games" / "其他" / "del.zip")
    a = _insert_work(lib, 1, "Del", [("folder", folder)])[0]
    b = _insert_work(lib, 2, "Zip", [("archive", archive)])[0]
    revisions = _revisions(lib)
    log = tmp_path / "delete.jsonl"
    assert storage.delete_copies(lib, [a, b], log_path=str(log)) == [a, b]
    assert not os.path.lexists(folder) and not os.path.lexists(archive)
    assert os.path.isdir(folder.parent)
    state = storage.list_storage(lib)
    assert state[a].location == state[b].location == "已删除"
    assert state[a].home_path is None and state[b].home_path is None
    assert _state(lib, a)[2] == "missing"
    with lib.connection() as c:
        assert c.execute("SELECT count(*) FROM works WHERE id IN (1,2)").fetchone()[0] == 2
    assert storage.work_locations(lib) == {1: "已删除", 2: "已删除"}
    assert _revisions(lib) == revisions
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(x["action"], x["result"], x["dst"]) for x in lines] == [("delete", "ok", None)] * 2


def test_delete_copies_marks_missing_path(lib, game_root):
    folder = _folder(game_root / "Games" / "日系" / "Gone")
    cid = _insert_work(lib, 1, "Gone", [("folder", folder)])[0]
    shutil.rmtree(folder)
    assert storage.delete_copies(lib, [cid]) == [cid]
    assert storage.list_storage(lib)[cid].location == "已删除"


def test_delete_copies_rejects_unsafe_paths_before_deleting(lib, game_root):
    good = _folder(game_root / "Games" / "日系" / "Good")
    good_id = _insert_work(lib, 1, "Good", [("folder", good)])[0]
    no_games = _folder(game_root / "elsewhere" / "NoGames")
    no_games_id = _insert_work(lib, 2, "NoGames", [("folder", no_games)])[0]
    shallow = game_root / "Games" / "欧美"
    shallow.mkdir(parents=True)
    shallow_id = _insert_work(lib, 3, "Shallow", [("folder", shallow)])[0]
    parent = _folder(game_root / "Games" / "同人" / "Parent")
    child = _folder(parent / "Child")
    parent_id = _insert_work(lib, 4, "Parent", [("folder", parent)])[0]
    _insert_work(lib, 5, "Child", [("folder", child)])

    for ids in ((good_id, no_games_id), (shallow_id,), (parent_id,)):
        with pytest.raises(storage.StorageError):
            storage.delete_copies(lib, ids)
    assert all(os.path.isdir(path) for path in (good, no_games, shallow, parent, child))

    with lib.transaction() as c:
        c.execute("INSERT INTO sources(path,path_key,added_at) VALUES(?,?,?)",
                  (str(good), _key(str(good)), "2020"))
    with pytest.raises(storage.StorageError):
        storage.delete_copies(lib, [good_id])
    assert os.path.isdir(good)


def test_delete_copies_rejects_dot_segments(lib, game_root):
    base = game_root / "Games" / "日系" / "Dot"
    first = _folder(base / "B")
    second = _folder(base / "C")
    dotdot = f"{first}{os.sep}..{os.sep}B"
    dot = f"{second}{os.sep}."
    a = _insert_work(lib, 1, "DotDot", [("folder", dotdot)])[0]
    b = _insert_work(lib, 2, "Dot", [("folder", dot)])[0]
    for copy_id in (a, b):
        with pytest.raises(storage.StorageError) as caught:
            storage.delete_copies(lib, [copy_id])
        assert ". 或 .." in str(caught.value)
    assert os.path.isdir(first) and os.path.isdir(second)


def test_delete_copies_rejects_already_deleted_and_missing(lib, game_root):
    folder = _folder(game_root / "Games" / "日系" / "Once")
    cid = _insert_work(lib, 1, "Once", [("folder", folder)])[0]
    storage.delete_copies(lib, [cid])
    for ids in ([cid], [99999]):
        with pytest.raises(storage.StorageError):
            storage.delete_copies(lib, ids)
