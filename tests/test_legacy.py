from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
from galpanel2.core import legacy as l, domain as d, compare, store, repository as r


@pytest.fixture
def old(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE games(id INTEGER PRIMARY KEY,title TEXT,original_title TEXT,developer TEXT,"
        "notes TEXT,hidden INTEGER,added_at TEXT,kind TEXT,path TEXT,exe_path TEXT,poster_path TEXT,"
        "extra,bindings TEXT); CREATE TABLE sources(id INTEGER PRIMARY KEY,name TEXT,path TEXT);"
        "CREATE TABLE play_logs(game_id INTEGER,date TEXT,minutes INTEGER);")
    rows = [
        (1, " First ", "", "Developer", "notes", 0, "2026-01-01", "folder", "D:\\fixture\\first", "D:\\fixture\\first\\game.exe", "", b"\x00\xff", "{}"),
        (2, "", "Original", "", "", 1, "", "archive", "D:\\fixture\\second.7z", "E:\\other.exe", "", None, "{\"x\":1}"),
        (3, "Duplicate", "", "", "", 0, "", "folder", "D:\\fixture\\duplicate", "", "", 2.5, ""),
        (4, "Duplicate", "", "", "", 0, "", "folder", "d:/fixture/duplicate/", "", "", 7, ""),
        (5, "Empty", "", "", "", 0, "", "folder", "", "", "", "", ""),
        (6, "Relative", "", "", "", 0, "", "folder", "relative", "", "", "", ""),
        (7, "Kind", "", "", "", 0, "", "iso", "D:\\fixture\\other", "", "", "", ""),
        (8, "", "", "", "", 0, "", "folder", "D:\\fixture\\Fallback", "", "", 1, ""),
    ]
    c.executemany("INSERT INTO games VALUES(" + ",".join(["?"] * 13) + ")", rows)
    c.execute("INSERT INTO sources VALUES(8,'Old source','D:\\fixture')")
    c.executemany("INSERT INTO play_logs VALUES(?,?,?)", [(1, "2026-01-01", 10), (3, "2026-01-01", 99)])
    c.commit()
    c.close()
    return p


def test_dynamic_columns_types_mapping_conservation(old, tmp_path):
    before = old.read_bytes()
    info = l.inspect_legacy(str(old))
    assert info.games == 8 and info.notes_nonempty == 1 and info.bindings_nonempty == 1
    target = tmp_path / "new.galdb"
    rep = l.import_legacy(str(old), str(target))
    assert rep.imported == 3 and len(rep.rejected) == 5
    assert {x.reason for x in rep.rejected} == {"duplicate_path", "relative_path", "empty_path", "bad_kind"}
    assert l.verify_import(str(old), str(target)) == []
    assert rep.sha256_before == rep.sha256_after == hashlib.sha256(before).hexdigest()
    assert old.read_bytes() == before
    with store.open_library(str(target), True) as lib:
        works = {w.title: r.get_work(lib, w.id) for w in r.list_works(lib, True)}
        assert set(works) == {"First", "Original", "Fallback"}
        assert works["First"].copies[0].exe_relpath == "game.exe"
        assert works["Original"].copies[0].exe_relpath is None
        assert works["Original"].hidden
        with lib.connection() as c:
            vals = [json.loads(x[0]) for x in c.execute("SELECT value_json FROM legacy_fields WHERE column_name='extra'")]
            assert {v["t"] for v in vals} == {"blob", "null", "integer"}
            assert c.execute("SELECT minutes FROM legacy_play_logs").fetchall()[0][0] == 10
            assert c.execute("SELECT count(*) FROM legacy_fields").fetchone()[0] == 3 * 13
    with pytest.raises(d.TargetExists):
        l.import_legacy(str(old), str(target))


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_legacy_sidecars_refused(old, tmp_path, suffix):
    Path(str(old) + suffix).write_bytes(b"")
    with pytest.raises(d.LegacyError):
        l.inspect_legacy(str(old))
    with pytest.raises(d.LegacyError):
        l.import_legacy(str(old), str(tmp_path / "new.galdb"))
    assert not (tmp_path / "new.galdb").exists()


def test_cancel_exception_and_stale_partial(old, tmp_path, monkeypatch):
    target = tmp_path / "new.galdb"
    partial = Path(str(target) + ".partial")
    pc = Path(str(target) + ".covers.partial")
    partial.write_bytes(b"stale")
    pc.mkdir()
    (pc / "stale").write_bytes(b"stale")
    with pytest.raises(d.Cancelled):
        l.import_legacy(str(old), str(target), cancel=lambda: True)
    assert not partial.exists() and not pc.exists() and not target.exists()
    monkeypatch.setattr(l, "_verify", lambda *a: ["injected error"])
    with pytest.raises(d.LegacyError):
        l.import_legacy(str(old), str(target))
    assert not target.exists() and not partial.exists() and not pc.exists()


def test_legacy_cover_magic(old, tmp_path):
    poster = tmp_path / "arbitrary.data"
    poster.write_bytes(b"GIF89a contents")
    c = sqlite3.connect(old)
    c.execute("UPDATE games SET poster_path=? WHERE id=1", (str(poster),))
    c.execute("UPDATE games SET poster_path='missing.png' WHERE id=2")
    c.commit()
    c.close()
    target = tmp_path / "new.galdb"
    rep = l.import_legacy(str(old), str(target))
    assert (rep.covers_copied, rep.covers_missing) == (1, 1)
    assert list(Path(str(target) + ".covers").iterdir())[0].suffix == ".gif"


@pytest.mark.parametrize("tamper", ["UPDATE works SET notes='changed'", "UPDATE copies SET path='D:\\wrong'",
    "DELETE FROM legacy_play_logs", "DELETE FROM legacy_fields WHERE column_name='extra'",
    "UPDATE legacy_fields SET value_json='{\"t\":\"text\",\"v\":1}' WHERE column_name='extra'"])
def test_verify_detects_tampering(old, tmp_path, tamper):
    target = tmp_path / "new.galdb"
    l.import_legacy(str(old), str(target))
    with sqlite3.connect(target) as c:
        c.execute(tamper)
    assert l.verify_import(str(old), str(target))


def test_compare_readonly_and_skip_ids(old, tmp_path):
    a, b = tmp_path / "a.galdb", tmp_path / "b.galdb"
    l.import_legacy(str(old), str(a))
    l.import_legacy(str(old), str(b))
    with sqlite3.connect(b) as c:
        c.execute("UPDATE legacy_fields SET value_json='{\"t\":\"integer\",\"v\":999}' WHERE column_name='id'")
        c.execute("UPDATE legacy_fields SET value_json='{\"t\":\"text\",\"v\":\"different\"}' WHERE column_name='notes'")
    before = a.read_bytes(), b.read_bytes()
    rep = compare.compare_libraries(str(a), str(b))
    assert rep.common == 3 and not rep.only_a and not rep.only_b
    assert rep.field_diffs == {"notes": 3}
    assert before == (a.read_bytes(), b.read_bytes())


def test_cli_exit_codes(old, tmp_path):
    target = tmp_path / "new.galdb"
    for args, code in [(["inspect", str(old)], 0), (["import", str(old), str(target)], 1),
                       (["verify", str(old), str(target)], 0), (["import", str(old), str(target)], 2)]:
        result = subprocess.run([sys.executable, "-m", "galpanel2.core", *args], capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == code, result.stdout + result.stderr
        json.loads(result.stdout)


def test_import_kill_then_retry(old, tmp_path):
    target = tmp_path / "new.galdb"
    pause = tmp_path / "pause"
    import os
    env = dict(os.environ, GALPANEL2_TEST_PAUSE_FILE=str(pause))
    code = f"from galpanel2.core.legacy import import_legacy; import_legacy({str(old)!r},{str(target)!r})"
    child = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not pause.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert pause.exists()
    finally:
        child.kill()
        child.communicate(timeout=10)
    assert not target.exists() and not Path(str(target) + ".covers").exists()
    assert l.import_legacy(str(old), str(target)).imported == 3


def test_no_games_table_and_existing_cover_dir(tmp_path, old):
    bad = tmp_path / "empty.db"
    sqlite3.connect(bad).close()
    with pytest.raises(d.LegacyError):
        l.inspect_legacy(str(bad))
    target = tmp_path / "new.galdb"
    Path(str(target) + ".covers").mkdir()
    with pytest.raises(d.TargetExists):
        l.import_legacy(str(old), str(target))
