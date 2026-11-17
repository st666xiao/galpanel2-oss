from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
from galpanel2.core import domain as d, names, repository as r, scanning as s, store


def tree(tmp_path, name="[Circle] Long Story", size=1024):
    root = tmp_path / "root"
    p = root / name
    p.mkdir(parents=True, exist_ok=True)
    with (p / "game.exe").open("wb") as f:
        f.truncate(size)
    return root, p


def populate(lib, tmp_path):
    root, p = tree(tmp_path)
    sid = r.add_source(lib, str(root))
    plan = s.plan_scan(lib, sid, min_size_bytes=1)
    s.commit_scan(lib, plan)
    return sid, r.get_work(lib, r.list_works(lib)[0].id), p


def snapshot(lib):
    with lib.connection() as c:
        return tuple(c.iterdump())


@pytest.mark.parametrize("value", ["C:\\Games\\A\\", "c:/games/a", "\\\\?\\C:\\Games\\A", "C:\\Games\\.\\A\\..\\A"])
def test_path_variants(value, monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda _: (_ for _ in ()).throw(AssertionError("disk accessed")))
    assert d.path_key(value) == "c:\\games\\a"


@pytest.mark.parametrize("value", ["", "games\\a", "C:relative", "\\root", "\\\\server", "\\\\.\\device", "C:\\x\0y"])
def test_bad_paths(value):
    with pytest.raises(ValueError):
        d.path_key(value)


def test_roots_and_unc():
    assert d.path_key("C:\\") == "c:\\"
    assert d.path_key("\\\\Server\\Share\\Dir\\") == "\\\\server\\share\\dir"
    assert d.path_key("\\\\?\\UNC\\Server\\Share\\Dir") == "\\\\server\\share\\dir"


def test_store_identity_pragmas_readonly(lib, tmp_path):
    with lib.connection() as c:
        for pragma, expected in [("foreign_keys", 1), ("journal_mode", "delete"), ("synchronous", 2), ("busy_timeout", 5000)]:
            assert c.execute("PRAGMA " + pragma).fetchone()[0] == expected
        assert c.execute("SELECT value FROM library_info WHERE key='created_at'").fetchone()[0].endswith("Z")
    before = Path(lib.path).read_bytes()
    with store.open_library(lib.path, readonly=True) as ro:
        assert ro.uuid == lib.uuid
        with pytest.raises(ValueError):
            r.remove_source(ro, 1)
    assert before == Path(lib.path).read_bytes()
    with pytest.raises(d.LibraryLocked):
        store.open_library(lib.path)
    with pytest.raises(d.TargetExists):
        store.create_library(lib.path)
    with pytest.raises(ValueError):
        store.create_library(str(tmp_path / "x.db"))
    with pytest.raises(ValueError):
        store.create_library(str(tmp_path / "absent" / "x.galdb"))
    with pytest.raises(d.NotALibrary):
        store.open_library(str(tmp_path / "missing.galdb"))
    assert not (tmp_path / "missing.galdb").exists()


@pytest.mark.parametrize("contents", [b"bad sqlite", b""])
def test_not_library(tmp_path, contents):
    p = tmp_path / "bad.galdb"
    p.write_bytes(contents)
    with pytest.raises(d.NotALibrary):
        store.open_library(str(p))
    assert p.read_bytes() == contents


def test_lock_process_death(lib):
    path = lib.path
    lib.close()
    code = f"from galpanel2.core.store import open_library; import time; l=open_library({path!r}); print('ready',flush=True); time.sleep(30)"
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(d.LibraryLocked):
            store.open_library(path)
    finally:
        child.kill()
        child.communicate(timeout=10)
    store.open_library(path).close()


@pytest.mark.parametrize("stem,want", [("[CircleA] 星降る夜のアトリエ", ("星降る夜のアトリエ", "CircleA")),
    ("MoonlitCafe_v1.02", ("MoonlitCafe", "")), ("【Dev】Long_Title（extra） ver.1.2a", ("Long Title", "Dev")),
    ("inner_game", ("inner game", "")), ("(Dev) Title [demo]", ("Title", "Dev"))])
def test_name_parse(stem, want):
    assert names.parse(stem) == want


@pytest.mark.parametrize("excluded", sorted(names.EXCLUDED))
def test_all_excluded_dirs(lib, tmp_path, excluded):
    root, _ = tree(tmp_path, excluded)
    sid = r.add_source(lib, str(root))
    assert not s.plan_scan(lib, sid, min_size_bytes=1).items


@pytest.mark.parametrize("mark", s.SKIP_MARK)
def test_archive_skip_marks(lib, tmp_path, mark):
    root = tmp_path / "root"
    root.mkdir()
    (root / ("Long " + mark + ".tar.gz")).write_bytes(b"x")
    sid = r.add_source(lib, str(root))
    item = s.plan_scan(lib, sid).items[0]
    assert (item.status, item.reason) == ("skipped", "skip_mark")


def test_plan_detection_depth_size_and_readonly(lib, tmp_path):
    root, p = tree(tmp_path, "1")
    (p / "game.exe").rename(p / "RealName.exe")
    (p / "patch.zip").write_bytes(b"x")
    (root / "Long archive.7z").write_bytes(b"x")
    (root / "installer").mkdir()
    (root / "installer/setup.exe").write_bytes(b"x")
    sid = r.add_source(lib, str(root))
    before = snapshot(lib)
    plan = s.plan_scan(lib, sid, min_size_bytes=1)
    assert [(i.title, i.kind) for i in plan.items] == [("RealName", "folder"), ("Long archive", "archive")]
    assert snapshot(lib) == before
    assert [i.reason for i in s.plan_scan(lib, sid).items].count("too_small") == 1
    assert not s.plan_scan(lib, sid, max_depth=0).items[0].kind == "folder"
    with pytest.raises(d.Cancelled):
        s.plan_scan(lib, sid, cancel=lambda: True)
    assert snapshot(lib) == before


def test_generic_subdir_exe_selection(lib, tmp_path):
    root = tmp_path / "root"
    game = root / "Long game"
    (game / "bin").mkdir(parents=True)
    (game / "bin/Game.exe").write_bytes(b"x")
    (game / "bin/setup.exe").write_bytes(b"x" * 1000)
    sid = r.add_source(lib, str(root))
    plan = s.plan_scan(lib, sid, min_size_bytes=1)
    assert plan.items[0].exe_relpath == "bin\\Game.exe"
    assert len(plan.items) == 1


def test_commit_decisions_conflicts_and_missing(lib, tmp_path):
    sid, work, path = populate(lib, tmp_path)
    root2, _ = tree(tmp_path / "other")
    sid2 = r.add_source(lib, str(root2))
    plan = s.plan_scan(lib, sid2, min_size_bytes=1)
    assert plan.items[0].status == "same_name" and plan.items[0].candidates == (work.id,)
    before = snapshot(lib)
    for decision in ("add", "bad", True):
        with pytest.raises(ValueError):
            s.commit_scan(lib, plan, {plan.items[0].key: decision})
    assert snapshot(lib) == before
    result = s.commit_scan(lib, plan)
    assert result.added_works == result.attached_copies == 0
    s.commit_scan(lib, plan, {plan.items[0].key: work.id})
    assert len(r.get_work(lib, work.id).copies) == 2
    before = snapshot(lib)
    with pytest.raises(d.PlanConflict):
        s.commit_scan(lib, plan)
    assert snapshot(lib) == before
    (path / "game.exe").unlink()
    path.rmdir()
    missing = s.plan_scan(lib, sid, min_size_bytes=1)
    assert missing.items[0].status == "missing"
    s.commit_scan(lib, missing)
    assert r.list_works(lib)[0].availability == "partial"


def test_new_same_name_and_attach_new(lib, tmp_path):
    sid, work, _ = populate(lib, tmp_path)
    root2, _ = tree(tmp_path / "other")
    sid2 = r.add_source(lib, str(root2))
    plan = s.plan_scan(lib, sid2, min_size_bytes=1)
    s.commit_scan(lib, plan, {plan.items[0].key: "new"})
    assert len(r.list_works(lib)) == 2
    root3, _ = tree(tmp_path / "third", "Different title")
    sid3 = r.add_source(lib, str(root3))
    plan = s.plan_scan(lib, sid3, min_size_bytes=1)
    s.commit_scan(lib, plan, {plan.items[0].key: work.id})
    assert len(r.get_work(lib, work.id).copies) == 2


def test_commit_exception_rolls_back(lib, tmp_path, monkeypatch):
    root, _ = tree(tmp_path)
    sid = r.add_source(lib, str(root))
    plan = s.plan_scan(lib, sid, min_size_bytes=1)
    before = snapshot(lib)
    monkeypatch.setattr(s, "test_pause", lambda: (_ for _ in ()).throw(RuntimeError("injected failure")))
    with pytest.raises(RuntimeError):
        s.commit_scan(lib, plan)
    assert before == snapshot(lib)


def test_availability_cancel_error_selection(lib, tmp_path, monkeypatch):
    sid, work, p = populate(lib, tmp_path)
    before = snapshot(lib)
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls == 4
    with pytest.raises(d.Cancelled):
        s.check_availability(lib, cancel=cancel)
    assert snapshot(lib) == before
    assert s.check_availability(lib, []).checked == 0
    monkeypatch.setattr(os.path, "isdir", lambda _: (_ for _ in ()).throw(PermissionError("denied")))
    assert s.check_availability(lib, [work.id]).error == 1
    assert r.get_work(lib, work.id).copies[0].availability == "error"


@pytest.mark.parametrize("states,want", [([], "unknown"), (["ok"], "ok"), (["missing"], "missing"),
    (["ok", "missing"], "partial"), (["ok", "error"], "partial"), (["error"], "unknown"),
    (["ok", "unknown"], "unknown"), (["missing", "unknown"], "unknown")])
def test_availability_projection(states, want):
    assert r.availability_of(states) == want


def test_repo_cas_remove_sources_and_files(lib, tmp_path):
    sid, work, path = populate(lib, tmp_path)
    assert r.add_source(lib, str(path.parent) + "\\") == sid
    with pytest.raises(ValueError):
        r.add_source(lib, str(tmp_path / "absent"))
    updated = r.edit_work(lib, work.id, work.revision, title="  Renamed  ", developer="D", notes="N", hidden=True)
    assert updated.title == "Renamed" and updated.revision == work.revision + 1
    assert not r.list_works(lib) and r.list_works(lib, True)[0].hidden
    for op in (r.edit_work, r.remove_work):
        with pytest.raises(d.WriteConflict):
            op(lib, work.id, work.revision)
    with pytest.raises(ValueError):
        r.edit_work(lib, work.id, updated.revision, title=" ")
    with pytest.raises(ValueError):
        r.edit_work(lib, work.id, updated.revision, rating=1)
    r.remove_source(lib, sid)
    assert r.get_work(lib, work.id).copies[0].source_id is None
    r.remove_work(lib, work.id, updated.revision)
    assert path.is_dir()
    with pytest.raises(KeyError):
        r.get_work(lib, work.id)


@pytest.mark.parametrize("magic,ext", [(b"\xff\xd8\xffx", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF89a", "gif"), (b"RIFFxxxxWEBP", "webp")])
def test_cover_magic_cas_and_keep_file(lib, tmp_path, magic, ext):
    _, work, _ = populate(lib, tmp_path)
    p = tmp_path / "wrong.extension"
    p.write_bytes(magic)
    updated = r.set_cover(lib, work.id, work.revision, str(p))
    cover = Path(updated.cover_path)
    assert cover.name == hashlib.sha256(magic).hexdigest() + "." + ext
    with pytest.raises(d.WriteConflict):
        r.set_cover(lib, work.id, work.revision, None)
    cleared = r.set_cover(lib, work.id, updated.revision, None)
    assert cleared.cover_path is None and cover.exists()
    p.write_bytes(b"invalid image")
    before = snapshot(lib)
    with pytest.raises(ValueError):
        r.set_cover(lib, work.id, cleared.revision, str(p))
    assert snapshot(lib) == before


def test_nested_edit_cover_atomic(lib, tmp_path):
    _, work, _ = populate(lib, tmp_path)
    before = snapshot(lib)
    p = tmp_path / "bad.png"
    p.write_bytes(b"bad")
    with pytest.raises(ValueError), lib.transaction():
        w = r.edit_work(lib, work.id, work.revision, title="Changed")
        r.set_cover(lib, work.id, w.revision, str(p))
    assert snapshot(lib) == before


def test_plan_errors_and_junction_symlink_exclusion(lib, tmp_path, monkeypatch):
    class Entry:
        def is_symlink(self):
            return True
    assert not s._safe_entry(Entry())
    class Junction:
        def is_symlink(self):
            return False
        def is_junction(self):
            return True
    assert not s._safe_entry(Junction())
    root, _ = tree(tmp_path)
    sid = r.add_source(lib, str(root))
    def denied(_):
        raise PermissionError("injected denied scan")
    monkeypatch.setattr(os, "scandir", denied)
    before = snapshot(lib)
    assert s.plan_scan(lib, sid).errors
    assert snapshot(lib) == before


def test_commit_missing_source_and_work_conflicts(lib, tmp_path):
    sid, work, _ = populate(lib, tmp_path)
    root2, _ = tree(tmp_path / "other", "Different story")
    sid2 = r.add_source(lib, str(root2))
    plan = s.plan_scan(lib, sid2, min_size_bytes=1)
    r.remove_work(lib, work.id, work.revision)
    before = snapshot(lib)
    with pytest.raises(d.PlanConflict):
        s.commit_scan(lib, plan, {plan.items[0].key: work.id})
    assert snapshot(lib) == before
    r.remove_source(lib, sid2)
    before = snapshot(lib)
    with pytest.raises(d.PlanConflict):
        s.commit_scan(lib, plan)
    assert snapshot(lib) == before


def test_depth_boundary_and_exe_order(lib, tmp_path):
    root = tmp_path / "root"
    game = root.joinpath(*[f"level{i}" for i in range(1, 7)])
    game.mkdir(parents=True)
    (game / "Ordinary.exe").write_bytes(b"x" * 100)
    (game / "Start.exe").write_bytes(b"x")
    too_deep = root.joinpath(*[f"other{i}" for i in range(1, 8)])
    too_deep.mkdir(parents=True)
    (too_deep / "Deep.exe").write_bytes(b"x")
    sid = r.add_source(lib, str(root))
    plan = s.plan_scan(lib, sid, min_size_bytes=1)
    assert len(plan.items) == 1 and plan.items[0].exe_relpath == "Start.exe"


def test_scan_kill_rolls_back(lib, tmp_path):
    root, _ = tree(tmp_path)
    sid = r.add_source(lib, str(root))
    path = lib.path
    before = snapshot(lib)
    lib.close()
    pause = tmp_path / "pause"
    code = (f"from galpanel2.core import store,scanning; l=store.open_library({path!r}); "
            f"p=scanning.plan_scan(l,{sid},min_size_bytes=1); scanning.commit_scan(l,p)")
    child = subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ, GALPANEL2_TEST_PAUSE_FILE=str(pause)),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not pause.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert pause.exists()
    finally:
        child.kill()
        child.communicate(timeout=10)
    with store.open_library(path) as reopened:
        assert snapshot(reopened) == before
        with reopened.connection() as c:
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
