"""pipeline.py 的单元测试：校验、断点续跑、并发与错误隔离。"""
from __future__ import annotations

import json
import sys

from galpanel2.identify import ai, pipeline

FAKE_AI = r'''
import json, os, sys, tempfile
args = sys.argv[1:]
counter = os.environ["FAKE_COUNTER"]
# 每次调用建一个独立文件计数：Windows 上多进程追加同一文件会丢行
os.close(tempfile.mkstemp(dir=counter, suffix=".call")[0])
prompt = open(args[args.index("--task") + 1], encoding="utf-8").read()
out = args[args.index("--out") + 1]
behaviour = os.environ.get("FAKE_BEHAVIOUR", "")
n = len(os.listdir(counter))
if behaviour == "fail" or (behaviour == "fail_first" and n == 1):
    sys.exit(3)
if behaviour == "garbage":
    reply = "不知道"
elif "【任务:提取】" in prompt:
    if "NOTGAL" in prompt:
        reply = json.dumps({"is_gal": False, "title_guess": "", "brand_guess": "",
                            "category_guess": "其他", "codes": [], "queries": []})
    else:
        reply = "好的：\n```json\n" + json.dumps({
            "is_gal": True, "title_guess": "猜的标题", "brand_guess": "B",
            "category_guess": "日系", "codes": [], "queries": ["q1"]},
            ensure_ascii=False) + "\n```"
elif "【任务:判断】" in prompt:
    if "WANT_BADREF" in prompt:
        d = {"decision": "match", "ref": "vndb:v999999", "category": "日系",
             "brand": "B", "title": "T", "title_original": "T", "release": None,
             "confidence": 0.9, "reason": "r"}
    elif "WANT_BADCAT" in prompt:
        d = {"decision": "match", "ref": "vndb:v1", "category": "火星", "brand": "B",
             "title": "", "title_original": "", "release": None,
             "confidence": "7", "reason": "r"}
    elif "WANT_NOMATCH" in prompt:
        d = {"decision": "no_match", "ref": None, "category": "同人", "brand": "社团",
             "title": "只认出名字", "title_original": "x", "release": None,
             "confidence": 0.6, "reason": "r"}
    elif "WANT_RAISE" in prompt:
        sys.exit(5)
    else:
        d = {"decision": "match", "ref": "vndb:v1", "category": "日系", "brand": "POISON",
             "title": "导师X催眠", "title_original": "x", "release": "2020-01-01",
             "confidence": 0.95, "reason": "编号一致"}
    reply = "结论 " + json.dumps(d, ensure_ascii=False) + " 完"
else:
    reply = "?"
open(out, "w", encoding="utf-8").write(reply)
'''


def fake_ai(tmp_path, monkeypatch, behaviour=""):
    d = tmp_path / "ai"
    d.mkdir()
    script = d / "fake_ai.py"
    script.write_text(FAKE_AI, encoding="utf-8")
    counter = d / "calls"
    counter.mkdir()
    monkeypatch.setenv("FAKE_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_BEHAVIOUR", behaviour)
    argv = [sys.executable, str(script), "--task", "{prompt}", "--out", "{out}",
            "--dir", "{dir}", "--timeout", "{timeout}"]
    cfg = ai.AiConfig(argv, "file", 20)
    calls = lambda: len(list(counter.iterdir()))  # noqa: E731
    return cfg, calls


class FakeSearcher:
    def __init__(self, cands=None):
        self.cands = cands if cands is not None else [{
            "ref": "vndb:v1", "site": "vndb", "title": "Saimin", "alt_titles": [],
            "zh_title": "导师X催眠", "brand": "POISON", "release": "2020-01-01",
            "url": "https://vndb.org/v1", "summary": "", "image": ""}]
        self.calls = 0

    def candidates(self, hints, extract):
        self.calls += 1
        return list(self.cands)


class BrokenSearcher:
    def __init__(self):
        self.calls = 0

    def candidates(self, hints, extract):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("search down")
        return FakeSearcher().cands


def item(key, name, extra=""):
    return {"key": key, "path": f"/nowhere/{name}", "name": name, "parents": [],
            "size_bytes": 1, "date": None, "files_top": [extra] if extra else [],
            "exes": [], "readmes": [], "potatovn": None, "codes": []}


def run(tmp_path, monkeypatch, items, searcher=None, behaviour="", jobs=1, limit=None, progress=None):
    cfg, calls = fake_ai(tmp_path, monkeypatch, behaviour)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    s = searcher or FakeSearcher()
    results = pipeline.run(items, str(work), cfg, searcher=s, jobs=jobs,
                           limit=limit, progress=progress)
    return results, calls, work, s, cfg


def test_pipeline_basic_statuses_and_flags(tmp_path, monkeypatch):
    items = [item("1", "good"), item("2", "NOTGAL thing"), item("3", "x", "WANT_BADREF"),
             item("4", "x", "WANT_BADCAT"), item("5", "x", "WANT_NOMATCH")]
    results, calls, work, searcher, _ = run(tmp_path, monkeypatch, items)
    by = {r["key"]: r for r in results}
    assert [r["key"] for r in results] == ["1", "2", "3", "4", "5"]
    assert by["1"]["status"] == "match" and by["1"]["ref"] == "vndb:v1"
    assert by["1"]["title"] == "导师X催眠" and by["1"]["flags"] == []
    assert by["1"]["confidence"] == 0.95 and by["1"]["candidates"] == 1
    assert by["2"]["status"] == "not_gal"
    assert by["3"]["status"] == "no_match" and by["3"]["ref"] is None
    assert "ref_not_in_candidates" in by["3"]["flags"]
    assert by["4"]["category"] == "其他" and "bad_category" in by["4"]["flags"]
    assert by["4"]["confidence"] == 1.0 and by["4"]["title"] == "猜的标题"
    assert by["5"]["status"] == "no_match" and by["5"]["title"] == "只认出名字"
    assert calls() == 9
    assert searcher.calls == 4
    lines = (work / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and all(json.loads(line)["key"] for line in lines)
    assert (work / "ai-log.jsonl").is_file()


def test_not_gal_skips_search(tmp_path, monkeypatch):
    cfg, calls = fake_ai(tmp_path, monkeypatch)
    searcher = FakeSearcher()
    work = tmp_path / "w"
    results = pipeline.run([item("1", "NOTGAL")], str(work), cfg, searcher=searcher)
    assert results[0]["status"] == "not_gal"
    assert searcher.calls == 0 and calls() == 1


def test_empty_candidates_forces_no_match(tmp_path, monkeypatch):
    results, _, _, _, _ = run(tmp_path, monkeypatch, [item("1", "good")],
                              searcher=FakeSearcher([]))
    assert results[0]["status"] == "no_match" and results[0]["ref"] is None
    assert "ref_not_in_candidates" in results[0]["flags"]


def test_resume_skips_done_and_retries_error(tmp_path, monkeypatch):
    items = [item("1", "good"), item("2", "x", "WANT_RAISE"), item("3", "good2")]
    first, calls, work, searcher, cfg = run(tmp_path, monkeypatch, items, limit=2)
    assert [r["key"] for r in first] == ["1", "2"]
    assert first[1]["status"] == "error" and first[1]["error"]
    n = calls()
    second = pipeline.run(items, str(work), cfg, searcher=searcher)
    by = {r["key"]: r for r in second}
    assert [r["key"] for r in second] == ["1", "2", "3"]
    assert by["1"]["status"] == "match" and by["3"]["status"] == "match"
    assert by["2"]["status"] == "error"
    assert calls() - n == 5
    final = {}
    for line in (work / "results.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        final[row["key"]] = row
    assert set(final) == {"1", "2", "3"}


def test_limit_zero_processes_nothing(tmp_path, monkeypatch):
    results, calls, work, _, _ = run(tmp_path, monkeypatch, [item("1", "good")], limit=0)
    assert results == [] and calls() == 0
    assert not (work / "results.jsonl").exists()


def test_parallel_results_are_complete_json_lines(tmp_path, monkeypatch):
    items = [item(str(i), f"good{i}") for i in range(1, 9)]
    results, calls, work, _, _ = run(tmp_path, monkeypatch, items, jobs=4)
    assert [r["key"] for r in results] == [str(i) for i in range(1, 9)]
    assert all(r["status"] == "match" for r in results)
    assert calls() == 16
    lines = (work / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8
    assert all(json.loads(line) for line in lines)


def test_progress_callback_reports_done_total_name(tmp_path, monkeypatch):
    seen = []
    run(tmp_path, monkeypatch, [item("1", "a"), item("2", "b")],
        progress=lambda done, total, name: seen.append((done, total, name)))
    assert seen == [(1, 2, "a"), (2, 2, "b")]


def test_search_exception_isolated_per_item(tmp_path, monkeypatch):
    results, calls, _, _, _ = run(tmp_path, monkeypatch,
                                  [item("1", "bad"), item("2", "good")],
                                  searcher=BrokenSearcher())
    by = {r["key"]: r for r in results}
    assert by["1"]["status"] == "error" and "search down" in by["1"]["error"]
    assert by["2"]["status"] == "match"
    assert calls() == 3


def test_ai_failure_becomes_error_result(tmp_path, monkeypatch):
    results, calls, _, searcher, _ = run(tmp_path, monkeypatch, [item("1", "good")],
                                         behaviour="fail")
    assert results[0]["status"] == "error" and results[0]["error"]
    assert calls() == 2 and searcher.calls == 0


def test_title_original_falls_back_to_title(tmp_path, monkeypatch):
    results, _, _, _, _ = run(tmp_path, monkeypatch, [item("1", "x", "WANT_BADCAT")])
    assert results[0]["title"] == "猜的标题"
    assert results[0]["title_original"] == "猜的标题"
