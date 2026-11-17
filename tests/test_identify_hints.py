"""hints.py 的单元测试：编号、文件夹线索、只读库收集。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from galpanel2.identify import hints


def snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.rglob("*"))


def make_game(root):
    g = root / "Games" / "paradox" / "[240329][1265724][だーくニャー！]ボクが当主"
    inner = g / "Boku_chs"
    (inner / ".PotatoVN").mkdir(parents=True)
    (inner / "data").mkdir()
    (inner / "Game.exe").write_bytes(b"MZ")
    (inner / "data" / "tool.exe").write_bytes(b"MZ")
    (inner / "Readme_zh-CN.txt").write_bytes(("这是说明 RJ01112222\n" + "说明" * 3000).encode("utf-8"))
    (g / "使用须知.txt").write_text("请支持正版", encoding="utf-8")
    (inner / "steam_appid.txt").write_text("1234560\n", encoding="utf-8")
    (inner / ".PotatoVN" / "meta.json").write_text(json.dumps({
        "Name": "ボクが当主", "Id": 5, "Tags": ["a", "b"], "Deep": {"x": 1}, "Long": "z" * 1000,
    }, ensure_ascii=False), encoding="utf-8")
    return g


def test_find_codes_order_and_case():
    got = hints.find_codes(
        "[240329][1265724][SUKARADOG] rj01265724 RJ123456 see https://vndb.org/v26054 and RJ01265724 again VJ01002777")
    assert got == ["getchu:1265724", "dlsite:RJ01265724", "dlsite:RJ123456",
                   "vndb:v26054", "dlsite:VJ01002777"]


def test_find_codes_getchu_needs_exactly_seven_digits():
    assert hints.find_codes("[12345678] 价格 1980") == []
    assert hints.find_codes("[1234567]") == ["getchu:1234567"]


def test_find_codes_empty_and_unknown_prefixes():
    assert hints.find_codes("") == []
    assert hints.find_codes("AB123456 XY12345678") == []


def test_collect_folder_reads_everything_without_writing(tmp_path):
    g = make_game(tmp_path)
    before = snapshot(tmp_path)
    h = hints.collect_folder("7", str(g))
    assert snapshot(tmp_path) == before

    assert h["key"] == "7" and h["path"] == str(g) and h["name"] == g.name
    assert h["parents"] == ["paradox", "Games", tmp_path.name]
    assert h["date"] == "2024-03-29"
    assert set(h["exes"]) == {"Game.exe", "tool.exe"}
    assert "Boku_chs" in h["files_top"] and len(h["files_top"]) <= 15
    names = [r["name"] for r in h["readmes"]]
    assert "Readme_zh-CN.txt" in names and "使用须知.txt" in names
    readme = next(r for r in h["readmes"] if r["name"] == "Readme_zh-CN.txt")
    assert readme["text"].startswith("这是说明") and len(readme["text"]) <= 1500
    assert "\ufffd" not in readme["text"]
    assert h["potatovn"]["Name"] == "ボクが当主"
    assert h["potatovn"]["Tags"] == ["a", "b"]
    assert "Deep" not in h["potatovn"] and len(h["potatovn"]["Long"]) == 300
    for code in ("getchu:1265724", "dlsite:RJ01112222", "steam:1234560"):
        assert code in h["codes"], (code, h["codes"])
    assert h["size_bytes"] > 3000


def test_collect_folder_single_file(tmp_path):
    path = tmp_path / "[221223] [みるく] pack RJ00999999.zip"
    path.write_bytes(b"PK" + b"0" * 100)
    h = hints.collect_folder("8", str(path))
    assert h["date"] == "2022-12-23"
    assert h["size_bytes"] == 102
    assert "dlsite:RJ00999999" in h["codes"]
    assert h["readmes"] == [] and h["files_top"] == []


def test_collect_folder_missing_path_is_safe(tmp_path):
    h = hints.collect_folder("9", str(tmp_path / "missing" / "[991340] bad date"))
    assert h["size_bytes"] == 0
    assert h["files_top"] == [] and h["exes"] == [] and h["readmes"] == []
    assert h["potatovn"] is None
    assert h["date"] is None


def test_collect_folder_decodes_gbk_readme(tmp_path):
    folder = tmp_path / "gbk game"
    folder.mkdir()
    (folder / "说明.txt").write_bytes("中文说明".encode("gbk"))
    h = hints.collect_folder("g", str(folder))
    assert h["readmes"][0]["text"] == "中文说明"


def test_collect_folder_html_keeps_title_and_links(tmp_path):
    folder = tmp_path / "html game"
    folder.mkdir()
    (folder / "readme.html").write_text(
        "<html><head><title>作品标题</title></head><body>"
        "<a href='https://example.com/x'>link</a></body></html>", encoding="utf-8")
    h = hints.collect_folder("h", str(folder))
    text = h["readmes"][0]["text"]
    assert "作品标题" in text and "https://example.com/x" in text
    assert "<html>" not in text


def test_collect_folder_limits_readmes_to_four(tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    for i in range(6):
        (folder / f"readme{i}.txt").write_text(f"text {i}", encoding="utf-8")
    h = hints.collect_folder("m", str(folder))
    assert len(h["readmes"]) == 4


def test_collect_library_is_readonly_and_skips_bound(tmp_path):
    g = make_game(tmp_path)
    other = tmp_path / "Other"
    other.mkdir()
    db = tmp_path / "t.galdb"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE works(id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE copies(id INTEGER PRIMARY KEY, work_id INTEGER, path TEXT, kind TEXT);
        CREATE TABLE external_links(work_id INTEGER, site TEXT, site_id TEXT);""")
    con.executemany("INSERT INTO works VALUES(?,?)", [(1, "甲"), (2, "乙"), (3, "丙")])
    con.executemany("INSERT INTO copies(work_id,path,kind) VALUES(?,?,?)", [
        (1, str(tmp_path / "gone"), "folder"), (1, str(g), "folder"),
        (2, str(other), "folder"), (3, str(other), "folder")])
    con.execute("INSERT INTO external_links VALUES(2,'vndb','v1')")
    con.commit()
    con.close()

    sha = hashlib.sha256(db.read_bytes()).hexdigest()
    files = sorted(p.name for p in tmp_path.iterdir())
    items = hints.collect_library(str(db))
    assert hashlib.sha256(db.read_bytes()).hexdigest() == sha
    assert sorted(p.name for p in tmp_path.iterdir()) == files
    assert [i["key"] for i in items] == ["1", "3"]
    first = items[0]
    assert first["path"] == str(g) and first["work_title"] == "甲"
    assert len(first["copies"]) == 2 and first["date"] == "2024-03-29"
    assert [i["key"] for i in hints.collect_library(str(db), only_unbound=False)] == ["1", "2", "3"]
