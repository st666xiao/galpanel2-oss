"""Shelf cleanup: schema v5, backup, ignore, remove, merge, reports, and the tidy CLI."""
import json
import os
from pathlib import Path
import sqlite3
import uuid

import pytest

from galpanel2.core import binding, domain as D, legacy, names, play, repository, scanning, store, tidy
from galpanel2.core.__main__ import main

NOW = "2026-01-01T00:00:00Z"
CLEAN = [
    ("Eternum-0.9.5-pc", "Eternum"),
    ("Goodbye_Eternity-v.0.13.6-windows", "Goodbye Eternity"),
    ("DeviantAnomalies-0.9.5-pc", "DeviantAnomalies"),
    ("TheSevenRealms-R3-v1.05-pc", "TheSevenRealms-R3"),
    ("Another Way Of Getting Paid_R18", "Another Way Of Getting Paid"),
    ("[Double Soft Cream]優艶の花 続 淫縛の館-PC", "優艶の花 続 淫縛の館"),
    ("[260130][1349398][ensemble SWEET] ラブラブお痴験バイト性活 パッケージ版 (mdf+mds)", "ラブラブお痴験バイト性活"),
    ("【ロレッタメイデン】v1.04", "ロレッタメイデン"),
    ("【】v1.0", "【】v1.0"),
    ("MaaNTE-win-x86_64-v1.2.0-MXU", "MaaNTE-win-x86 64-v1.2.0-MXU"),
    ("[汐乃コウ] 母子情事2 〜大好きな息子に今夜も抱かれる〜 [中国翻訳]", "母子情事2 〜大好きな息子に今夜も抱かれる〜"),
    ("邻居的人妻2", "邻居的人妻2"),
    ("Onceinalifetime-1.0.0-pc", "Onceinalifetime"),
    ("PhotoHunt-0.21.1-pc", "PhotoHunt"),
    ("StuckInMG-0.11", "StuckInMG"),
    ("MythofSlayer-Ch.11-pc", "MythofSlayer-Ch.11"),
    ("異世界来たのでスケベスキルで全力謳歌しようと思う 22射目", "異世界来たのでスケベスキルで全力謳歌しようと思う 22射目"),
]
SUGGEST = [
    ("Peace Data", r"D:\Library\3-9\エンジョイサマー マキシマム\data", ("エンジョイサマー マキシマム", "parent_folder")),
    ("fix 5", r"D:\Library\20250806_000105\Version 0.2.7.5 (WINDOWS) fix 5 (2)", None),
    ("Eternum-0.9.5-pc", r"D:\Library\3-9\Eternum-0.9.5-pc", ("Eternum", "cleaned")),
    ("OCHIKEN", r"D:\Library\3-9\[260130][1349398][ensemble SWEET] ラブラブお痴験バイト性活 パッケージ版 (mdf+mds)\OCHIKEN", None),
    ("StarTRain", r"D:\Library\3-9\111", None),
    ("Kotoneiro", r"D:\Library\3-9\Kotoneiro\Kotoneiro", None),
    ("v2.05", r"D:\Library\4-20\5-6\v2.05", None),
    ("MXU", r"D:\Library\4-20\5-6\MaaNTE-win-x86_64-v1.2.0-MXU.zip", ("MaaNTE-win-x86 64-v1.2.0-MXU", "cleaned")),
    ("ロレッタメイデン", r"D:\Library\4-20\5-6\8-2\【ロレッタメイデン】v1.04", None),
]


def _old_library(path, version):
    path = Path(path)
    with sqlite3.connect(path) as c:
        for n in range(1, version + 1):
            name = "schema.sql" if n == 1 else f"schema_v{n}.sql"
            c.executescript(Path(store.__file__).with_name(name).read_text(encoding="utf-8"))
        c.execute(f"PRAGMA application_id={store.APPLICATION_ID}")
        c.execute(f"PRAGMA user_version={version}")
        c.execute("INSERT INTO library_info VALUES(?,?)", ("library_uuid", str(uuid.uuid4())))
        c.execute("INSERT INTO works(uuid,title,added_at,updated_at) VALUES(?,'旧作','x','x')", (str(uuid.uuid4()),))
    return path


def _work(lib, title, copies, **kw):
    with lib.transaction() as c:
        wid = c.execute(
            "INSERT INTO works(uuid,title,developer,notes,hidden,added_at,updated_at,revision) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), title, kw.get("developer", ""), kw.get("notes", ""), int(kw.get("hidden", 0)),
             NOW, NOW, kw.get("revision", 1))).lastrowid
        ids = []
        for cp in copies:
            ids.append(c.execute(
                "INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,size_bytes,availability,"
                "origin,added_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (wid, cp.get("source_id"), cp.get("kind", "archive"), cp["path"], D.path_key(cp["path"]),
                 cp.get("exe"), cp.get("size"), cp.get("availability", "ok"), "scan", NOW)).lastrowid)
    return wid, ids


def _source(lib, path):
    with lib.transaction() as c:
        c.execute("INSERT INTO sources(path,path_key,added_at) VALUES(?,?,?)", (path, D.path_key(path), NOW))
        return c.execute("SELECT id FROM sources WHERE path_key=?", (D.path_key(path),)).fetchone()[0]


def _leg(lib, wid, column, typ, value):
    with lib.transaction() as c:
        imp = c.execute("SELECT id FROM imports LIMIT 1").fetchone()
        if imp is None:
            imp_id = c.execute(
                "INSERT INTO imports(source_db_path,sha256_before,sha256_after,columns_json,imported,"
                "rejected_json,imported_at) VALUES('t','aa','aa','[]',0,'[]',?)", (NOW,)).lastrowid
        else:
            imp_id = imp[0]
        c.execute("INSERT INTO legacy_fields(work_id,import_id,column_name,value_json) VALUES(?,?,?,?)",
                  (wid, imp_id, column, json.dumps({"t": typ, "v": value}, ensure_ascii=False)))


def _cli(capsys, *args):
    code = main([str(a) for a in args])
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    return code, json.loads(lines[-1]) if lines else None, out


def test_create_import_and_upgrade_are_version_6(lib, tmp_path):
    assert lib.schema_version == 6
    with lib.connection() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 6
        assert c.execute("SELECT name FROM sqlite_master WHERE name='ignored_paths'").fetchone()
        assert c.execute("SELECT name FROM sqlite_master WHERE name='tidy_log'").fetchone()
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.executescript("CREATE TABLE games(id INTEGER PRIMARY KEY,title TEXT,developer TEXT,path TEXT,kind TEXT,"
                        "hidden INTEGER,added_at TEXT); CREATE TABLE sources(id INTEGER PRIMARY KEY,name TEXT,path TEXT);")
        c.execute("INSERT INTO sources VALUES(1,'s','D:\\\\leg')")
        c.execute("INSERT INTO games VALUES(1,'导入作品','Dev','D:\\\\leg\\\\Game','folder',0,'2026-08-01')")
    target = tmp_path / "imp.galdb"
    report = legacy.import_legacy(str(old), str(target))
    assert report.imported == 1 and not report.rejected
    with store.open_library(str(target)) as imported:
        assert imported.schema_version == 6
    for version in (1, 2, 3, 4, 5):
        path = _old_library(tmp_path / f"v{version}.galdb", version)
        with pytest.raises(D.NeedsUpgrade) as err:
            store.open_library(str(path))
        assert str(err.value) == "Library needs version 6 upgrade" and err.value.version == version
        with store.open_library(str(path), True) as ro:
            assert ro.schema_version == version
            if version == 4:
                assert tidy.title_report(ro) == []
        result = store.upgrade_library(str(path))
        assert (result.from_version, result.to_version) == (version, 6)
        with store.open_library(str(path)) as upgraded:
            assert upgraded.schema_version == 6
        with pytest.raises(ValueError, match="already version 6"):
            store.upgrade_library(str(path))
    with pytest.raises(ValueError, match="already version 6"):
        store.upgrade_library(lib.path)


def test_v4_readonly_report_does_not_write(tmp_path, capsys):
    path = _old_library(tmp_path / "v4.galdb", 4)
    before = path.read_bytes()
    with store.open_library(str(path), True) as ro:
        assert tidy.list_ignored(ro) == []
        for name in ("title_report", "duplicate_report", "missing_groups", "suspect_report", "legacy_report"):
            assert getattr(tidy, name)(ro) == []
    assert path.read_bytes() == before
    assert not Path(str(path) + ".lock").exists()
    code, payload, _ = _cli(capsys, "tidy", str(path), "report")
    assert code == 0 and payload["schema_version"] == 4 and payload["works"] == 1
    assert path.read_bytes() == before
    assert not Path(str(path) + ".lock").exists()


def test_backup_matches_collides_and_drops_partial(lib, tmp_path, monkeypatch):
    wid, _ = _work(lib, "备份作品", [{"path": r"D:\bak\Game"}])
    monkeypatch.setattr(tidy, "utc_now", lambda: "2026-10-20T03:04:05Z")
    bak = tidy.backup_library(lib, "tidy")
    assert os.path.isabs(bak)
    assert os.path.basename(bak) == Path(lib.path).name + ".tidy-20261020T030405Z.bak"
    with sqlite3.connect(bak) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT title FROM works").fetchone()[0] == "备份作品"
    second = tidy.backup_library(lib, "tidy")
    assert os.path.basename(second).endswith(".tidy-20261020T030405Z-2.bak")
    partial = Path(lib.path + ".tidy-20261020T030405Z-3.bak.partial")
    partial.write_bytes(b"KEEP")
    with pytest.raises(D.TargetExists):
        tidy.backup_library(lib, "tidy")
    assert partial.read_bytes() == b"KEEP"
    partial.unlink()
    source = Path(tidy.__file__).read_text(encoding="utf-8")
    assert "ctypes" not in source and callable(tidy._copy_database)

    def boom(src, dest):
        raise sqlite3.DatabaseError("forced")

    monkeypatch.setattr(tidy, "_copy_database", boom)
    with pytest.raises(sqlite3.DatabaseError):
        tidy.backup_library(lib, "tidy")
    assert not list(Path(lib.path).parent.glob("*.partial"))
    assert wid


def test_ignore_exact_tree_root_and_scan(lib, tmp_path):
    root_key = D.path_key("D:\\")
    assert tidy.is_ignored(root_key, [{"path_key": root_key, "scope": "tree"}])
    assert tidy.is_ignored(D.path_key(r"D:\x"), [{"path_key": root_key, "scope": "tree"}])
    assert not tidy.is_ignored(D.path_key(r"D:\x"), [{"path_key": root_key, "scope": "exact"}])
    tree = D.path_key(r"D:\Library\tree")
    assert tidy.is_ignored(D.path_key(r"D:\Library\tree\x"), [{"path_key": tree, "scope": "tree"}])
    assert not tidy.is_ignored(D.path_key(r"D:\Library\tree2"), [{"path_key": tree, "scope": "tree"}])
    tidy.add_ignored(lib, r"D:\Library\Game", "exact", "user")
    tidy.add_ignored(lib, r"D:\LIBRARY\game", "tree", "removed")
    row = tidy.list_ignored(lib)[0]
    assert row["scope"] == "tree" and row["reason"] == "user" and row["path"] == r"D:\LIBRARY\game"
    logs = tidy.list_log(lib)
    assert logs[0]["detail"]["op"] == "add" and logs[0]["backup_path"] is None
    assert logs[0]["detail"]["scope"] == "exact" and logs[1]["detail"]["scope"] == "tree"
    tidy.add_ignored(lib, r"D:\Library\Game", "exact", "user")
    assert tidy.list_ignored(lib)[0]["scope"] == "tree"
    assert tidy.list_log(lib)[-1]["detail"]["scope"] == "tree"

    root = tmp_path / "scan"
    root.mkdir()
    (root / "visible.zip").write_bytes(b"PK")
    (root / "hidden.zip").write_bytes(b"PK")
    branch = root / "branch"
    branch.mkdir()
    (branch / "nested.zip").write_bytes(b"PK")
    game = root / "Sakura"
    game.mkdir()
    (game / "play.exe").write_bytes(b"MZ")
    old = root / "oldgame"
    old.mkdir()
    (old / "play.exe").write_bytes(b"MZ")
    sid = repository.add_source(lib, str(root))
    kept, cids = _work(lib, "旧副本", [{
        "path": str(old), "kind": "folder", "source_id": sid, "exe": "play.exe", "size": 10,
    }])
    tidy.add_ignored(lib, str(root / "hidden.zip"), "exact")
    tidy.add_ignored(lib, str(root / "branch"), "tree")
    tidy.add_ignored(lib, str(old), "exact")
    plan = scanning.plan_scan(lib, sid)
    keys = {item.key: item for item in plan.items}
    assert D.path_key(str(root / "visible.zip")) in keys
    assert D.path_key(str(root / "Sakura")) in keys
    assert D.path_key(str(root / "hidden.zip")) not in keys
    assert D.path_key(str(branch / "nested.zip")) not in keys
    assert keys[D.path_key(str(old))].status == "known" and keys[D.path_key(str(old))].copy_id == cids[0]
    assert plan.ignored == 2
    tidy.add_ignored(lib, str(root), "tree")
    rooted = scanning.plan_scan(lib, sid)
    assert rooted.ignored == 1 and not any(item.key == D.path_key(str(root / "visible.zip")) for item in rooted.items)
    assert kept


def test_remove_batch_conflict_running_and_rescan(lib, tmp_path):
    root = tmp_path / "games"
    root.mkdir()
    come = root / "come.zip"
    back = root / "back.zip"
    come.write_bytes(b"PK")
    back.write_bytes(b"PK")
    sid = repository.add_source(lib, str(root))
    a, _ = _work(lib, "甲", [{"path": str(come), "source_id": sid}, {"path": r"D:\remove\a.zip"}], revision=2)
    b, _ = _work(lib, "乙", [{"path": str(back), "source_id": sid}], revision=4)
    with pytest.raises(ValueError, match="nothing to remove"):
        tidy.remove_works(lib, {})
    with lib.transaction() as c:
        cid = c.execute("SELECT id FROM copies WHERE work_id=?", (b,)).fetchone()[0]
        c.execute("INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,started_at,last_beat_at,seconds) "
                  "VALUES(?,?,'tracked','running','2026-10-20','2026-10-20T00:00:00Z','2026-10-20T00:00:00Z',0)", (b, cid))
    with pytest.raises(D.SessionActive):
        tidy.remove_works(lib, {b: 4})
    with lib.transaction() as c:
        c.execute("DELETE FROM play_sessions")
    with pytest.raises(D.WriteConflict):
        tidy.remove_works(lib, {a: 2, b: 9})
    assert len(list(Path(lib.path).parent.glob("*.bak"))) == 1
    with lib.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 2
    result = tidy.remove_works(lib, {b: 4, a: 2})
    assert result["removed"] == 2 and result["ignored"] == 3
    log = [row for row in tidy.list_log(lib) if row["action"] == "remove"][-1]
    assert [item["id"] for item in log["detail"]["works"]] == [a, b]
    assert log["detail"]["ignored"] == 3 and log["backup_path"] == result["backup"]
    plan = scanning.plan_scan(lib, sid)
    assert D.path_key(str(come)) not in {item.key for item in plan.items}
    fresh = root / "fresh.zip"
    fresh.write_bytes(b"PK")
    c, _ = _work(lib, "会回来", [{"path": str(fresh), "source_id": sid}])
    tidy.remove_works(lib, {c: 1}, ignore=False)
    again = {item.key: item.status for item in scanning.plan_scan(lib, sid).items}
    assert again[D.path_key(str(fresh))] == "new"


def test_merge_table_playtime_and_rollback(lib):
    with lib.transaction() as c:
        brands = [c.execute("INSERT INTO brands(name,name_key,created_at) VALUES(?,?,?)",
                            (name, key, NOW)).lastrowid for name, key in
                  (("B1", "b1"), ("B2", "b2"), ("B3", "b3"))]
        tags = [c.execute("INSERT INTO tags(group_id,name,name_key,created_at) VALUES(1,?,?,?)",
                          (name, key, NOW)).lastrowid for name, key in (("T1", "t1"), ("T2", "t2"), ("T9", "t9"))]
        c.execute("INSERT INTO source_tags(site,tag_key,text,suggestion) VALUES('vndb','tk1','tk1','map')")
    keep, kc = _work(lib, "Keep Title", [{"path": r"D:\merge\keep", "kind": "folder", "exe": "g.exe"}],
                     developer="DevK", notes="主备注")
    other, oc = _work(lib, "Other Title", [
        {"path": r"D:\merge\other\z", "kind": "folder", "exe": "a.exe"},
        {"path": r"D:\merge\other\a.zip"},
    ], developer="DevO", notes="次备注", hidden=1, revision=3)
    with lib.transaction() as c:
        c.execute("INSERT INTO launch_options VALUES(?,?,?,?)", (kc[0], "g.exe", "-k", NOW))
        c.execute("INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,started_at,last_beat_at,ended_at,"
                  "seconds,end_reason) VALUES(?,?,'tracked','closed','2026-01-01','2026-01-01T00:00:00Z',"
                  "'2026-01-01T00:10:00Z','2026-01-01T00:10:00Z',100,'exited')", (keep, kc[0]))
        c.execute("INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,started_at,last_beat_at,ended_at,"
                  "seconds,end_reason) VALUES(?,?,'tracked','closed','2026-01-01','2026-01-01T00:00:00Z',"
                  "'2026-01-01T00:10:00Z','2026-01-01T00:10:00Z',40,'exited')", (other, oc[0]))
        c.execute("INSERT INTO legacy_play_logs VALUES(?,?,?)", (keep, "2026-01-01", 10))
        c.execute("INSERT INTO legacy_play_logs VALUES(?,?,?)", (other, "2026-01-01", 3))
        c.execute("INSERT INTO legacy_play_logs VALUES(?,?,?)", (other, "2026-01-02", 7))
        c.execute("INSERT INTO covers VALUES(?,?,?,'user')", (keep, "k.png", "kk"))
        c.execute("INSERT INTO covers VALUES(?,?,?,'user')", (other, "o.png", "oo"))
        c.execute("INSERT INTO work_info VALUES(?,?,?,?,?,?,?)",
                  (keep, "主简介", "vndb", "2020-01-01", "vndb", None, NOW))
        c.execute("INSERT INTO work_info VALUES(?,?,?,?,?,?,?)",
                  (other, "次简介", "dlsite", "2019-05-05", "dlsite", "bangumi", NOW))
        c.execute("INSERT INTO work_titles VALUES(?,?,?,'original','vndb')", (keep, "主名", "shared"))
        c.execute("INSERT INTO work_titles VALUES(?,?,?,'translated','bangumi')", (other, "只在次", "only-other"))
        c.execute("INSERT INTO external_links VALUES(?,?,?,?)", (keep, "vndb", "v100", NOW))
        c.execute("INSERT INTO external_links VALUES(?,?,?,?)", (other, "vndb", "v999", NOW))
        c.execute("INSERT INTO external_links VALUES(?,?,?,?)", (other, "dlsite", "RJ1", NOW))
        c.execute("INSERT INTO work_brands VALUES(?,?,?)", (keep, brands[0], 0))
        c.execute("INSERT INTO work_brands VALUES(?,?,?)", (other, brands[1], 0))
        c.execute("INSERT INTO work_brands VALUES(?,?,?)", (other, brands[2], 1))
        c.execute("INSERT INTO work_tags VALUES(?,?)", (keep, tags[0]))
        c.execute("INSERT INTO work_tags VALUES(?,?)", (other, tags[1]))
        c.execute("INSERT INTO work_source_tags VALUES(?,?,?)", (other, "vndb", "tk1"))
        c.execute("INSERT INTO work_tag_blocks VALUES(?,?)", (other, tags[0]))
        c.execute("INSERT INTO work_tag_blocks VALUES(?,?)", (other, tags[2]))
    _leg(lib, keep, "playtime_minutes", "integer", 10)
    _leg(lib, other, "playtime_minutes", "integer", 5)
    _leg(lib, keep, "last_played_at", "text", "2020-01-01")
    _leg(lib, other, "last_played_at", "text", "2020-06-01")
    _leg(lib, keep, "description", "text", "KEEP-DESC")
    _leg(lib, other, "description", "text", "OTHER-DESC")
    _leg(lib, other, "rating", "text", "moved")
    before = play.list_playtimes(lib)
    want = before[keep].total_seconds + before[other].total_seconds
    bad, _ = _work(lib, "冲突", [{"path": r"D:\merge\bad"}])
    with pytest.raises(D.WriteConflict):
        tidy.merge_works(lib, keep, 1, {bad: 9})
    with lib.connection() as c:
        assert c.execute("SELECT title FROM works WHERE id=?", (keep,)).fetchone()[0] == "Keep Title"
    result = tidy.merge_works(lib, keep, 1, {other: 3})
    assert result["merged"] == [other] and result["revision"] == 2
    assert result["dropped_links"] == [{"site": "vndb", "site_id": "v999", "from": other}]
    work = repository.get_work(lib, keep)
    assert work.title == "Keep Title" and work.developer == "DevK" and work.hidden is False
    assert work.notes == "主备注\n\n次备注" and len(work.copies) == 3
    with lib.connection() as c:
        assert c.execute("SELECT args FROM launch_options WHERE copy_id=?", (kc[0],)).fetchone()[0] == "-k"
        assert sorted(tuple(row) for row in c.execute(
            "SELECT day, minutes FROM legacy_play_logs WHERE work_id=?", (keep,))) == [
            ("2026-01-01", 13), ("2026-01-02", 7)]
        fields = {row[0]: json.loads(row[1]) for row in c.execute(
            "SELECT column_name, value_json FROM legacy_fields WHERE work_id=?", (keep,))}
        assert fields["playtime_minutes"]["v"] == 15
        assert fields["last_played_at"]["v"] == "2020-06-01"
        assert fields["description"]["v"] == "KEEP-DESC" and fields["rating"]["v"] == "moved"
        assert c.execute("SELECT file_name FROM covers WHERE work_id=?", (keep,)).fetchone()[0] == "k.png"
        info = tuple(c.execute(
            "SELECT summary, release_date, cover_source FROM work_info WHERE work_id=?", (keep,)).fetchone())
        assert info == ("主简介", "2020-01-01", None)
        assert c.execute("SELECT title FROM work_titles WHERE work_id=? AND title_key='only-other'", (keep,)).fetchone()
        links = dict(c.execute("SELECT site, site_id FROM external_links WHERE work_id=?", (keep,)))
        assert links["dlsite"] == "RJ1" and links["vndb"] == "v100"
        assert [tuple(row) for row in c.execute(
            "SELECT brand_id FROM work_brands WHERE work_id=? ORDER BY position", (keep,))] == [
            (brands[0],), (brands[1],), (brands[2],)]
        assert c.execute("SELECT tag_id FROM work_tag_blocks WHERE work_id=?", (keep,)).fetchone()[0] == tags[2]
    assert play.list_playtimes(lib)[keep].total_seconds == want
    empty, _ = _work(lib, "空主", [{"path": r"D:\merge\empty"}], notes="")
    donor, _ = _work(lib, "有次", [{"path": r"D:\merge\donor"}], notes="只有次")
    with lib.transaction() as c:
        c.execute("INSERT INTO covers VALUES(?,?,?,'legacy')", (donor, "d.png", "dd"))
        c.execute("INSERT INTO work_info(work_id,updated_at) VALUES(?,?)", (empty, NOW))
        c.execute("INSERT INTO work_info VALUES(?,?,?,?,?,?,?)",
                  (donor, "次简介", "dlsite", "2019-05-05", "dlsite", "bangumi", NOW))
    tidy.merge_works(lib, empty, 1, {donor: 1})
    with lib.connection() as c:
        assert c.execute("SELECT notes FROM works WHERE id=?", (empty,)).fetchone()[0] == "只有次"
        assert c.execute("SELECT cover_source FROM work_info WHERE work_id=?", (empty,)).fetchone()[0] == "bangumi"
        assert c.execute("SELECT file_name FROM covers WHERE work_id=?", (empty,)).fetchone()[0] == "d.png"


def test_titles_retitle_and_reports(lib):
    for src, want in CLEAN:
        assert tidy.clean_title(src) == want
    assert tidy.clean_title("  Eternum-0.9.5-pc  ") == "Eternum"
    assert tidy.clean_title("Ｅｔｅｒｎｕｍ-0.9.5-pc") == "Ｅｔｅｒｎｕｍ"
    assert tidy.clean_title("Hello (world) End") == "Hello (world) End"
    assert names.parse("Eternum-0.9.5-pc") == ("Eternum-0.9.5-pc", "")
    assert tidy.weak_name("data") and tidy.weak_name("A356") is False
    assert tidy.container_name("A356") and tidy.container_name("A10") is False
    assert tidy.container_name("TRD9") and tidy.container_name("TRD") is False
    root = r"D:\Library"
    for current, path, want in SUGGEST:
        assert tidy.suggest_title(current, path, root) == want
    assert tidy.suggest_title("x", r"D:\data", None) is None
    assert tidy.suggest_title("A356", r"D:\Library\A356", root, "ネトラレ温泉") == ("ネトラレ温泉", "legacy_original")
    assert tidy.suggest_title("ZZ", r"D:\Library\GoodName\a\b\c\v1", root) is None
    assert tidy.suggest_title("Game.tar.gz", r"D:\Library\Game.tar.gz", root) is None
    assert tidy.suggest_title("Sakura.tar.gz", r"D:\Library\Sakura.tar.gz", root) == ("Sakura", "cleaned")
    sid = _source(lib, r"D:\Library")
    peace, _ = _work(lib, "Peace Data", [
        {"path": r"D:\Library\wrong\data", "source_id": sid, "availability": "missing"},
        {"path": r"D:\Library\3-9\エンジョイサマー マキシマム\data", "source_id": sid},
    ])
    one, _ = _work(lib, "Eternum-0.9.5-pc", [{"path": r"D:\Library\3-9\Eternum-0.9.5-pc", "source_id": sid}])
    two, _ = _work(lib, "Eternum", [{"path": r"D:\dup\e2", "source_id": sid}], hidden=1)
    alpha, _ = _work(lib, "Alpha", [{"path": r"D:\Library\dir\PhotoHunt-0.21.1-pc.zip", "source_id": sid}])
    beta, _ = _work(lib, "Beta", [{"path": r"D:\Library\dir\PhotoHunt", "source_id": sid, "kind": "folder", "exe": "g.exe"}])
    gamma, _ = _work(lib, "Gamma", [{"path": r"D:\Library\other\PhotoHunt.zip", "source_id": sid}])
    rows = {row["work_id"]: row for row in tidy.title_report(lib)}
    assert rows[peace]["suggested"] == "エンジョイサマー マキシマム" and rows[peace]["reason"] == "parent_folder"
    assert rows[one]["suggested"] == "Eternum"
    groups = tidy.duplicate_report(lib)
    assert any(g["reason"] == "same_title" and g["work_ids"] == [one, two] for g in groups)
    assert any(g["reason"] == "archive_and_folder" and g["work_ids"] == [alpha, beta] for g in groups)
    assert all(gamma not in g["work_ids"] or g["reason"] != "archive_and_folder" for g in groups)
    with pytest.raises(D.WriteConflict):
        tidy.retitle(lib, {one: (1, "甲二"), two: (99, "乙二")})
    assert repository.get_work(lib, one).title == "Eternum-0.9.5-pc"
    assert tidy.retitle(lib, {one: (1, "甲二"), two: (1, "乙二")}) == 2
    assert repository.get_work(lib, one).revision == 2


def test_missing_suspect_and_legacy(lib):
    sid = _source(lib, r"D:\Library")
    b1, _ = _work(lib, "漫画1", [
        {"path": r"D:\Library\bOOK\7.19\x.zip", "source_id": sid, "availability": "missing"},
        {"path": r"D:\Library\zzz\nope.zip", "source_id": sid, "availability": "missing"},
    ])
    b2, _ = _work(lib, "漫画2", [{"path": r"D:\Library\book\a.zip", "source_id": sid, "availability": "missing"}])
    _work(lib, "不全缺", [
        {"path": r"D:\Library\zzz\miss.zip", "source_id": sid, "availability": "missing"},
        {"path": r"D:\Library\zzz\ok.zip", "source_id": sid, "availability": "ok"},
    ])
    groups = tidy.missing_groups(lib)
    book = next(g for g in groups if g["group_key"] == D.path_key(r"D:\Library\bOOK"))
    assert book["group"] == r"D:\Library\bOOK" and book["work_ids"] == [b1, b2] and book["count"] == 2
    patch, _ = _work(lib, "ZZ patch ZZ", [{"path": r"D:\sus\patch\game.zip", "kind": "folder", "exe": "g.exe",
                                           "size": 11 * 1024 * 1024}])
    tiny, _ = _work(lib, "SmallGame", [{"path": r"D:\sus\tiny\a", "kind": "folder", "exe": "g.exe", "size": 100}])
    bare, _ = _work(lib, "NoProgram", [{"path": r"D:\sus\noexe\a", "kind": "folder", "exe": None, "size": 11 * 1024 * 1024}])
    _work(lib, "ExactTen", [{"path": r"D:\sus\big\a", "kind": "folder", "exe": "g.exe", "size": 10 * 1024 * 1024}])
    suspects = {row["work_id"]: row["reasons"] for row in tidy.suspect_report(lib)}
    assert suspects[patch] == ["tool_word"] and suspects[tiny] == ["tiny"] and suspects[bare] == ["no_exe"]
    rich, _ = _work(lib, "丰富", [{"path": r"D:\leg\rich"}])
    _leg(lib, rich, "bindings", "text", json.dumps({"Vndb": 23, "steam": "1209140"}))
    _leg(lib, rich, "description", "text", "  有简介  ")
    _leg(lib, rich, "release_date", "text", "2020/9/3")
    bad, _ = _work(lib, "坏号", [{"path": r"D:\leg\bad"}])
    _leg(lib, bad, "bindings", "text", "{")
    _leg(lib, bad, "description", "text", "仍然有简介")
    only, _ = _work(lib, "只有坏号", [{"path": r"D:\leg\only"}])
    _leg(lib, only, "bindings", "text", json.dumps({"vndb": "nope"}))
    upper, _ = _work(lib, "大写", [{"path": r"D:\leg\V"}])
    _leg(lib, upper, "bindings", "text", json.dumps({"vndb": "V23"}))
    gap, _ = _work(lib, "中间空格", [{"path": r"D:\leg\gap"}])
    _leg(lib, gap, "bindings", "text", json.dumps({"vndb": "v 1"}))
    owner, _ = _work(lib, "占用者", [{"path": r"D:\leg\owner"}])
    with lib.transaction() as c:
        c.execute("INSERT INTO external_links VALUES(?,?,?,?)", (owner, "vndb", "v23", NOW))
    taken, _ = _work(lib, "被占用", [{"path": r"D:\leg\taken"}])
    _leg(lib, taken, "bindings", "text", json.dumps({"vndb": "v23"}))
    illegal, _ = _work(lib, "非法日期", [{"path": r"D:\leg\date"}])
    _leg(lib, illegal, "release_date", "text", "20200201")
    rows = {row["work_id"]: row for row in tidy.legacy_report(lib)}
    assert rows[rich]["vndb"] == "v23" and rows[rich]["summary"] == "有简介"
    assert rows[rich]["release_date"] == "2020-09-03" and rows[rich]["vndb_taken_by"] == owner
    assert rows[bad]["bad"] == ["{"] and rows[bad]["summary"] == "仍然有简介"
    assert only not in rows and illegal not in rows and gap not in rows
    assert rows[upper]["vndb"] == "v23" and rows[upper]["bad"] == []
    assert rows[taken]["vndb_taken_by"] == owner
    assert tidy.use_legacy_summary(lib, rich) is True
    assert tidy.use_legacy_summary(lib, only) is False
    with lib.connection() as c:
        assert c.execute("SELECT summary_source FROM work_info WHERE work_id=?", (rich,)).fetchone()[0] == "user"
    binding.set_summary(lib, rich, "用户写的")
    assert tidy.use_legacy_summary(lib, rich) is False


def test_cli_subcommands(lib, capsys, tmp_path):
    sid = _source(lib, r"D:\Library")
    wid, _ = _work(lib, "Eternum-0.9.5-pc", [{"path": r"D:\Library\3-9\Eternum-0.9.5-pc", "source_id": sid}])
    other, _ = _work(lib, "次", [{"path": r"D:\cli\merge\o"}])
    missing, _ = _work(lib, "缺", [{"path": r"D:\Library\bOOK\1.zip", "source_id": sid, "availability": "missing"}])
    path = lib.path
    lib.close()
    code, payload, out = _cli(capsys, "tidy", path, "report")
    assert code == 0 and payload["schema_version"] == 6 and payload["works"] == 3
    assert "\\u" not in out
    code, payload, _ = _cli(capsys, "tidy", path, "ignore", "add", r"D:\cli\exact")
    assert code == 0 and payload is None
    code, payload, _ = _cli(capsys, "tidy", path, "ignore", "add", r"D:\cli\tree", "--tree")
    assert code == 0 and payload is None
    code, listed, _ = _cli(capsys, "tidy", path, "ignore", "list")
    assert {row["path"]: row["scope"] for row in listed}[r"D:\cli\tree"] == "tree"
    code, removed, _ = _cli(capsys, "tidy", path, "ignore", "remove", r"D:\cli\exact")
    assert code == 0 and removed is True
    code, payload, _ = _cli(capsys, "tidy", path, "log")
    assert code == 0 and payload and payload[0]["action"] == "ignore"
    code, payload, _ = _cli(capsys, "tidy", path, "retitle", f"{wid}=手动标题")
    assert code == 0 and payload == {"changed": 1}
    code, payload, _ = _cli(capsys, "tidy", path, "retitle", "--suggested", "all")
    assert payload["changed"] >= 1
    code, payload, _ = _cli(capsys, "tidy", path, "merge", str(wid), str(other))
    assert code == 0 and payload["keep"] == wid and payload["merged"] == [other]
    code, payload, _ = _cli(capsys, "tidy", path, "remove-missing", r"D:\no\such")
    assert code == 2 and payload == {"error": "no such missing group"}
    code, payload, _ = _cli(capsys, "tidy", path, "remove-missing", r"D:\Library\bOOK")
    assert code == 0 and payload["removed"] == 1
    with store.open_library(path) as live:
        left, _ = _work(live, "留下", [{"path": r"D:\cli\stay"}])
    code, payload, _ = _cli(capsys, "tidy", path, "remove", str(left), "--no-ignore")
    assert code == 0 and payload["removed"] == 1 and payload["ignored"] == 0
    with store.open_library(path) as live:
        summary, _ = _work(live, "有简介", [{"path": r"D:\cli\sum"}])
        _leg(live, summary, "description", "text", "写进去")
    code, payload, _ = _cli(capsys, "tidy", path, "legacy-summary", str(summary))
    assert payload == {"applied": [summary], "skipped": []}
    assert missing
    old = _old_library(tmp_path / "old.galdb", 4)
    code, payload, _ = _cli(capsys, "tidy", str(old), "remove", "1")
    assert code == 2 and payload["error"] == "NeedsUpgrade"
    assert payload["message"] == "Library needs version 6 upgrade"


def _baks(lib):
    return list(Path(lib.path).parent.glob("*.bak"))


def _free_drive():
    return next((letter for letter in "QXYZJK" if not os.path.exists(letter + ":\\")), None)


def test_remove_missing_group_skips_present_and_rolls_back(lib, tmp_path, capsys, monkeypatch):
    root = tmp_path / "shelf"
    group = root / "grp"
    group.mkdir(parents=True)
    back = group / "back.zip"
    back.write_bytes(b"PK")
    sid = _source(lib, str(root))
    gone, _ = _work(lib, "真没了", [{
        "path": str(group / "gone.zip"), "source_id": sid, "availability": "missing",
    }])
    came, _ = _work(lib, "又回来了", [{
        "path": str(back), "source_id": sid, "availability": "missing",
    }])
    outside = root / "else" / "keep.zip"
    outside.parent.mkdir()
    outside.write_bytes(b"PK")
    half, _ = _work(lib, "有一份还在", [
        {"path": str(group / "half-gone.zip"), "source_id": sid, "availability": "missing"},
        {"path": str(outside), "source_id": sid, "availability": "missing"},
    ])

    real_add = tidy.add_ignored

    def boom(*args, **kwargs):
        raise sqlite3.DatabaseError("forced-ignore")

    monkeypatch.setattr(tidy, "add_ignored", boom)
    with pytest.raises(sqlite3.DatabaseError):
        tidy.remove_missing_group(lib, str(group))
    monkeypatch.setattr(tidy, "add_ignored", real_add)
    with lib.connection() as c:
        assert [row[0] for row in c.execute("SELECT id FROM works ORDER BY id")] == [gone, came, half]
        assert c.execute("SELECT COUNT(*) FROM ignored_paths").fetchone()[0] == 0
    assert _baks(lib), "a failed remove-missing leaves the backup"
    for bak in _baks(lib):
        bak.unlink()

    result = tidy.remove_missing_group(lib, str(group))
    assert result["removed"] == 1 and result["still_present"] == [came, half]
    assert result["tree_ignored"] is False and os.path.isfile(result["backup"])
    assert len(_baks(lib)) == 1
    with lib.connection() as c:
        assert [row[0] for row in c.execute("SELECT id FROM works ORDER BY id")] == [came, half]
        assert c.execute("SELECT scope FROM ignored_paths WHERE path_key=?",
                         (D.path_key(str(group)),)).fetchone() is None
        assert tuple(c.execute("SELECT scope, reason FROM ignored_paths WHERE path_key=?",
                               (D.path_key(str(group / "gone.zip")),)).fetchone()) == ("exact", "removed")
        assert c.execute("SELECT 1 FROM ignored_paths WHERE path_key=?",
                         (D.path_key(str(group / "half-gone.zip")),)).fetchone() is None
    with pytest.raises(KeyError):
        tidy.remove_missing_group(lib, str(root / "nope"))

    roll = root / "roll"
    rolled, _ = _work(lib, "要回滚", [{
        "path": str(roll / "z.zip"), "source_id": sid, "availability": "missing",
    }])

    def tree_boom(target, path, scope="exact", reason="user"):
        if scope == "tree":
            raise sqlite3.DatabaseError("tree-failed")
        return real_add(target, path, scope, reason)

    monkeypatch.setattr(tidy, "add_ignored", tree_boom)
    with pytest.raises(sqlite3.DatabaseError):
        tidy.remove_missing_group(lib, str(roll))
    monkeypatch.setattr(tidy, "add_ignored", real_add)
    with lib.connection() as c:
        assert c.execute("SELECT id FROM works WHERE id=?", (rolled,)).fetchone()[0] == rolled
        assert c.execute("SELECT COUNT(*) FROM ignored_paths WHERE path_key=?",
                         (D.path_key(str(roll / "z.zip")),)).fetchone()[0] == 0

    whole = root / "all"
    only, _ = _work(lib, "甲", [{
        "path": str(whole / "a.zip"), "source_id": sid, "availability": "missing",
    }])
    before = len(_baks(lib))
    result = tidy.remove_missing_group(lib, str(whole))
    assert result == {
        "removed": 1, "still_present": [], "tree_ignored": True, "backup": result["backup"],
    }
    assert len(_baks(lib)) == before + 1 and os.path.isfile(result["backup"])
    with lib.connection() as c:
        assert c.execute("SELECT id FROM works WHERE id=?", (only,)).fetchone() is None
        assert tuple(c.execute("SELECT scope, reason FROM ignored_paths WHERE path_key=?",
                               (D.path_key(str(whole)),)).fetchone()) == ("tree", "user")

    here = root / "here"
    here.mkdir()
    stay_file = here / "stay.zip"
    stay_file.write_bytes(b"PK")
    stay, _ = _work(lib, "都在", [{
        "path": str(stay_file), "source_id": sid, "availability": "missing",
    }])
    before = len(_baks(lib))
    assert tidy.remove_missing_group(lib, str(here)) == {
        "removed": 0, "still_present": [stay], "tree_ignored": False, "backup": None,
    }
    assert len(_baks(lib)) == before

    running_dir = root / "run"
    running, copies = _work(lib, "正在玩", [{
        "path": str(running_dir / "x.zip"), "source_id": sid, "availability": "missing",
    }])
    with lib.transaction() as c:
        c.execute("INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,started_at,last_beat_at,seconds) "
                  "VALUES(?,?,'tracked','running','2026-10-20','2026-10-20T00:00:00Z','2026-10-20T00:00:00Z',0)",
                  (running, copies[0]))
    before = len(_baks(lib))
    with pytest.raises(D.SessionActive):
        tidy.remove_missing_group(lib, str(running_dir))
    assert len(_baks(lib)) == before

    letter = _free_drive()
    assert letter, "need a drive letter that is not mounted"
    offline = letter + r":\Library\grp"
    off_sid = _source(lib, letter + r":\Library")
    _work(lib, "离线", [{
        "path": letter + r":\Library\grp\x.zip", "source_id": off_sid, "availability": "missing",
    }])
    before = len(_baks(lib))
    with lib.connection() as c:
        works_before = c.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    with pytest.raises(ValueError, match="drive offline"):
        tidy.remove_missing_group(lib, offline)
    assert len(_baks(lib)) == before
    with lib.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM works").fetchone()[0] == works_before

    real_exists = os.path.exists

    def offline_unc(path):
        if str(path).startswith("\\\\server\\share"):
            return False
        return real_exists(path)

    unc_sid = _source(lib, r"\\server\share")
    _work(lib, "网络盘", [{
        "path": r"\\server\share\grp\x.zip", "source_id": unc_sid, "availability": "missing",
    }])
    monkeypatch.setattr(os.path, "exists", offline_unc)
    with pytest.raises(ValueError, match="drive offline") as caught:
        tidy.remove_missing_group(lib, r"\\server\share\grp")
    assert str(caught.value) == "drive offline: \\\\server\\share\\"
    monkeypatch.setattr(os.path, "exists", real_exists)

    path = lib.path
    lib.close()
    code, payload, _ = _cli(capsys, "tidy", path, "remove-missing", offline)
    assert code == 2 and payload == {"error": "drive offline"}


def test_merge_developer_and_user_cleared_summary(lib):
    keep, _ = _work(lib, "主", [{"path": r"D:\dev\k"}], developer="   ")
    blank, _ = _work(lib, "空白", [{"path": r"D:\dev\blank"}], developer="  ")
    named, _ = _work(lib, "有名", [{"path": r"D:\dev\named"}], developer="Named")
    later, _ = _work(lib, "更后", [{"path": r"D:\dev\later"}], developer="Later")
    assert blank < named < later
    with lib.transaction() as c:
        c.execute("INSERT INTO work_info(work_id,summary,summary_source,release_date,release_source,updated_at) "
                  "VALUES(?,NULL,'user',NULL,NULL,?)", (keep, NOW))
        c.execute("INSERT INTO work_info VALUES(?,?,?,?,?,?,?)",
                  (later, "旧简介", "vndb", "2019-05-05", "dlsite", None, NOW))
    tidy.merge_works(lib, keep, 1, {later: 1, blank: 1, named: 1})
    assert repository.get_work(lib, keep).developer == "Later"
    with lib.connection() as c:
        assert tuple(c.execute(
            "SELECT summary, summary_source, release_date, release_source FROM work_info WHERE work_id=?",
            (keep,)).fetchone()) == (None, "user", "2019-05-05", "dlsite")
    merged = [row for row in tidy.list_log(lib) if row["action"] == "merge"][-1]["detail"]["merged"]
    assert [item["developer"] for item in merged] == ["  ", "Named", "Later"]

    kept, _ = _work(lib, "主2", [{"path": r"D:\dev\k2"}], developer="DevK")
    other, _ = _work(lib, "次2", [{"path": r"D:\dev\o2"}], developer="DevX")
    empty, _ = _work(lib, "空简介", [{"path": r"D:\dev\empty"}], developer="")
    donor, _ = _work(lib, "有简介", [{"path": r"D:\dev\donor"}], developer="Circle")
    with lib.transaction() as c:
        c.execute("INSERT INTO work_info(work_id,updated_at) VALUES(?,?)", (empty, NOW))
        c.execute("INSERT INTO work_info(work_id,summary,summary_source,updated_at) VALUES(?,?,?,?)",
                  (donor, "补上的简介", "dlsite", NOW))
    tidy.merge_works(lib, kept, 1, {other: 1})
    tidy.merge_works(lib, empty, 1, {donor: 1})
    assert repository.get_work(lib, kept).developer == "DevK"
    assert repository.get_work(lib, empty).developer == "Circle"
    with lib.connection() as c:
        assert tuple(c.execute("SELECT summary, summary_source FROM work_info WHERE work_id=?",
                               (empty,)).fetchone()) == ("补上的简介", "dlsite")
    logs = [row for row in tidy.list_log(lib) if row["action"] == "merge"]
    assert logs[-2]["detail"]["merged"][0]["developer"] == "DevX"
    assert logs[-1]["detail"]["merged"][0]["developer"] == "Circle"
