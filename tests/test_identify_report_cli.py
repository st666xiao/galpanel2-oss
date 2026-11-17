"""report.py 与 __main__.py 的单元测试。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from galpanel2.identify import report

ROOT = Path(__file__).resolve().parents[1]


def base(**extra):
    row = {"path": "/p", "brand": "B", "title_original": "o", "release": None,
           "reason": "因为", "flags": [], "candidates": 1, "error": None}
    row.update(extra)
    return row


def sample():
    return [
        base(key="1", name="a", status="match", ref="vndb:v1", category="日系",
             title="甲", confidence=0.95),
        base(key="2", name="b", status="no_match", ref=None, category="同人",
             title="乙", confidence=0.6),
        base(key="3", name="c", status="no_match", ref=None, category="其他",
             title="丙", confidence=0.3),
        base(key="4", name="d", status="not_gal", ref=None, category="其他",
             title="", confidence=0.0),
        base(key="5", name="e", status="error", ref=None, category=None,
             title=None, confidence=0.0, error="boom"),
        base(key="6", name="f", status="match", ref="dlsite:RJ01", category="同人",
             title="己", confidence=0.65, flags=["x"]),
    ]


def test_write_report_identify_json(tmp_path):
    data = report.write_report(sample(), str(tmp_path))
    on_disk = json.loads((tmp_path / "identify.json").read_text(encoding="utf-8"))
    assert on_disk == data
    assert data["identified"]["1"] == {"category": "日系", "brand": "B", "title": "甲",
                                       "ref": "vndb:v1"}
    assert data["identified"]["2"]["ref"] == ""
    assert "3" not in data["identified"]
    assert data["not_gal"] == ["4"]
    assert sorted(data["todo"]) == ["3", "5"]
    assert data["merges"] == []


def test_write_report_review_summary_and_order(tmp_path):
    report.write_report(sample(), str(tmp_path))
    md = (tmp_path / "review.md").read_text(encoding="utf-8")
    assert "共 6 部" in md and "匹配 2" in md and "只认出名字 1" in md
    assert "非 gal 1" in md and "待查 1" in md and "出错 1" in md
    rows = [line for line in md.splitlines()
            if line.startswith("|") and not set(line) <= set("|-: ")]
    body = rows[1:]
    order = [next(k for k in ("a", "b", "c", "d", "e", "f")
                  if f"| {k} |" in line or f"|{k}|" in line) for line in body]
    assert set(order[:2]) == {"d", "e"}
    assert order[2:] == ["c", "b", "f", "a"]
    warned = {name for name, line in zip(order, body) if "⚠" in line}
    assert warned == {"b", "c", "d", "e", "f"}


def test_write_report_links(tmp_path):
    report.write_report(sample(), str(tmp_path))
    md = (tmp_path / "review.md").read_text(encoding="utf-8")
    assert "https://vndb.org/v1" in md
    assert "https://www.dlsite.com/maniax/work/=/product_id/RJ01.html" in md


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "galpanel2.identify", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)


def test_cli_help_lists_run():
    result = run_cli("--help")
    assert result.returncode == 0 and "run" in result.stdout
    assert "hints" in result.stdout and "report" in result.stdout


def make_game(root):
    g = root / "Games" / "[240329][1265724][社团] 作品"
    g.mkdir(parents=True)
    (g / "Game.exe").write_bytes(b"MZ")
    (g / "steam_appid.txt").write_text("1234560", encoding="utf-8")
    return g


def test_cli_hints_from_folders(tmp_path):
    g = make_game(tmp_path / "games")
    listing = tmp_path / "list.txt"
    listing.write_text(str(g) + "\n", encoding="utf-8")
    out = tmp_path / "hints.jsonl"
    result = run_cli("hints", "--folders", str(listing), "--out", str(out))
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["key"] == "1"
    assert rows[0]["date"] == "2024-03-29"
    assert "steam:1234560" in rows[0]["codes"]


def test_cli_hints_from_library(tmp_path):
    game = make_game(tmp_path / "games")
    db = tmp_path / "lib.galdb"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE works(id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE copies(work_id INTEGER, path TEXT);
        CREATE TABLE external_links(work_id INTEGER, site TEXT, site_id TEXT);""")
    con.execute("INSERT INTO works VALUES(1,'作品')")
    con.execute("INSERT INTO copies VALUES(1,?)", (str(game),))
    con.commit()
    con.close()
    out = tmp_path / "hints.jsonl"
    result = run_cli("hints", "--library", str(db), "--all", "--out", str(out))
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["key"] == "1" and rows[0]["work_title"] == "作品"


def test_cli_report_reads_results_jsonl(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rows = [
        {"key": "1", "path": "/p", "name": "a", "status": "error", "ref": None,
         "category": None, "brand": "", "title": "", "title_original": "",
         "release": None, "confidence": 0.0, "reason": "", "flags": [],
         "candidates": 0, "error": "old"},
        {"key": "1", "path": "/p", "name": "a", "status": "match", "ref": "vndb:v1",
         "category": "日系", "brand": "B", "title": "T", "title_original": "T",
         "release": None, "confidence": 0.9, "reason": "r", "flags": [],
         "candidates": 1, "error": None},
    ]
    (work / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    result = run_cli("report", "--work", str(work))
    assert result.returncode == 0, result.stderr
    data = json.loads((work / "identify.json").read_text(encoding="utf-8"))
    assert data["identified"]["1"]["ref"] == "vndb:v1"
    assert "old" not in (work / "review.md").read_text(encoding="utf-8")


def test_cli_hints_requires_source(tmp_path):
    result = run_cli("hints", "--out", str(tmp_path / "x.jsonl"))
    assert result.returncode == 2


def test_cli_missing_ai_config_has_friendly_error(tmp_path):
    hints = tmp_path / 'hints.jsonl'
    hints.write_text('', encoding='utf-8')
    missing = tmp_path / 'missing.json'
    result = run_cli('run', '--hints', str(hints), '--work', str(tmp_path / 'work'), '--ai-config', str(missing))
    assert result.returncode == 2
    assert result.stderr.strip() == f'配置文件不存在: {missing}'
    assert 'Traceback' not in result.stderr and not result.stdout
