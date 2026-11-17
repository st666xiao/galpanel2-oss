"""把流水线结果整理成 identify.json 和 review.md。"""
from __future__ import annotations

import json
import os

_CONCLUSION = {
    "match": "匹配",
    "no_match": "待查",
    "not_gal": "非 gal",
    "error": "出错",
}


def _confidence(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _link(ref) -> str:
    if not isinstance(ref, str) or ":" not in ref:
        return ""
    site, _, site_id = ref.partition(":")
    if site == "vndb":
        return f"https://vndb.org/{site_id}"
    if site == "dlsite":
        return f"https://www.dlsite.com/maniax/work/=/product_id/{site_id}.html"
    if site == "steam":
        return f"https://store.steampowered.com/app/{site_id}/"
    if site == "f95":
        return f"https://f95zone.to/threads/{site_id}/"
    return ""


def _cell(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "/")
    return text.strip()


def _identified(result: dict) -> dict | None:
    status = result.get("status")
    title = result.get("title")
    if status == "match":
        return {
            "category": result.get("category") or "其他",
            "brand": result.get("brand") or "",
            "title": title or "",
            "ref": result.get("ref") or "",
        }
    if status == "no_match" and _confidence(result.get("confidence")) >= 0.5 and title:
        return {
            "category": result.get("category") or "其他",
            "brand": result.get("brand") or "",
            "title": title,
            "ref": "",
        }
    return None


def _decision_text(result: dict, identified: bool) -> str:
    status = result.get("status")
    if status == "match":
        return "匹配"
    if status == "no_match":
        return "只认出名字" if identified else "待查"
    return _CONCLUSION.get(status, "待查")


def _sort_key(result: dict):
    status = result.get("status")
    if status == "error":
        group = 0
        subgroup = 0
    elif status == "not_gal":
        group = 0
        subgroup = 1
    else:
        group = 1
        subgroup = 0
    return (group, subgroup, _confidence(result.get("confidence")), str(result.get("key")))


def write_report(results: list[dict], work_dir: str) -> dict:
    os.makedirs(work_dir, exist_ok=True)

    identified: dict[str, dict] = {}
    not_gal: list[str] = []
    todo: list[str] = []
    for result in results:
        key = str(result.get("key"))
        status = result.get("status")
        entry = _identified(result)
        if entry is not None:
            identified[key] = entry
        elif status == "not_gal":
            not_gal.append(key)
        else:
            todo.append(key)

    data = {
        "identified": identified,
        "not_gal": not_gal,
        "todo": todo,
        "merges": [],
    }
    with open(os.path.join(work_dir, "identify.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    match_n = sum(1 for r in results if r.get("status") == "match")
    name_n = sum(1 for r in results
                 if r.get("status") == "no_match" and _identified(r) is not None)
    nongal_n = sum(1 for r in results if r.get("status") == "not_gal")
    error_n = sum(1 for r in results if r.get("status") == "error")
    todo_n = len(results) - match_n - name_n - nongal_n - error_n

    lines = [
        "# AI 识别复核",
        "",
        (f"共 {len(results)} 部：匹配 {match_n}、只认出名字 {name_n}、"
         f"非 gal {nongal_n}、待查 {todo_n}、出错 {error_n}"),
        "",
        "| key | 文件夹名 | 结论 | 标题 | 品牌 | 分类 | 资料页链接 | 把握 | 理由 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for result in sorted(results, key=_sort_key):
        key = str(result.get("key"))
        entry = _identified(result)
        status = result.get("status")
        confidence = _confidence(result.get("confidence"))
        conclusion = _decision_text(result, entry is not None)
        link = _link(result.get("ref"))
        reason = result.get("reason") or result.get("error") or ""
        warn = confidence < 0.7 or bool(result.get("flags"))
        key_cell = f"⚠ {key}" if warn else key
        lines.append("| " + " | ".join([
            _cell(key_cell),
            _cell(result.get("name")),
            _cell(conclusion),
            _cell(result.get("title")),
            _cell(result.get("brand")),
            _cell(result.get("category")),
            _cell(link),
            _cell(f"{confidence:.2f}"),
            _cell(reason),
        ]) + " |")

    with open(os.path.join(work_dir, "review.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return data
