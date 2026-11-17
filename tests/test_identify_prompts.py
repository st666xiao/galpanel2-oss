"""prompts.py 的单元测试：两段提示词的结构、长度与删减规则。"""
from __future__ import annotations

from galpanel2.identify import prompts


def hint():
    return {"key": "1", "path": "/games/[240329][1265724][SUKARADOG] 朔夜様",
            "name": "[240329][1265724][SUKARADOG] 朔夜様", "parents": ["games"],
            "size_bytes": 1, "date": "2024-03-29", "files_top": ["Game.exe"],
            "exes": ["Game.exe"], "readmes": [{"name": "r.txt", "text": "说明"}],
            "potatovn": None, "codes": ["getchu:1265724"]}


def candidate(i):
    return {"ref": f"vndb:v{i}", "site": "vndb", "title": f"T{i}", "alt_titles": [],
            "zh_title": None, "brand": "B", "release": None, "url": "",
            "summary": "s" * 300, "image": ""}


def test_marks_and_first_lines():
    assert prompts.EXTRACT_MARK == "【任务:提取】"
    assert prompts.DECIDE_MARK == "【任务:判断】"
    assert prompts.extract_prompt(hint()).splitlines()[0].startswith(prompts.EXTRACT_MARK)
    assert prompts.decide_prompt(hint(), {}, []).splitlines()[0].startswith(prompts.DECIDE_MARK)


def test_extract_prompt_contains_requirements():
    text = prompts.extract_prompt(hint())
    assert "朔夜様" in text and len(text) <= 6000
    for word in ("Getchu", "getchu", "工具", "JSON", "queries", "category_guess"):
        assert word in text, word


def test_extract_prompt_drops_readmes_first_when_too_long():
    h = hint()
    h["readmes"] = [{"name": "r.txt", "text": "READMETEXT" + "长" * 1500} for _ in range(4)]
    h["files_top"] = ["KEEPFILE" + "f" * 200 for _ in range(15)]
    text = prompts.extract_prompt(h)
    assert len(text) <= 6000
    assert "READMETEXT" not in text
    assert "KEEPFILE" in text


def test_extract_prompt_drops_files_top_when_still_too_long():
    h = hint()
    h["readmes"] = [{"name": "r.txt", "text": "READMETEXT" + "长" * 1500} for _ in range(4)]
    h["files_top"] = ["KEEPFILE" + "f" * 200 for _ in range(15)]
    h["potatovn"] = {f"k{i}": "z" * 300 for i in range(20)}
    text = prompts.extract_prompt(h)
    assert len(text) <= 6000
    assert "READMETEXT" not in text
    assert "KEEPFILE" not in text


def test_decide_prompt_keeps_all_candidates_and_truncates_summary():
    cands = [candidate(i) for i in range(20)]
    text = prompts.decide_prompt(hint(), {"is_gal": True, "queries": []}, cands)
    assert "vndb:v19" in text and len(text) > 6000
    assert "s" * 151 not in text
    assert "s" * 150 in text
    for word in ("日系", "同人", "欧美", "其他", "no_match", "错绑比不绑糟糕"):
        assert word in text, word


def test_decide_prompt_contains_extract_json():
    text = prompts.decide_prompt(hint(), {"is_gal": True, "queries": ["唯一关键词"]},
                                 [candidate(1)])
    assert "唯一关键词" in text
    assert "title_original" in text and "confidence" in text
