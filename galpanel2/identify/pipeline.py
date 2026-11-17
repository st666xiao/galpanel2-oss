"""把线索、搜索、两段 AI 调用串成一条可断点续跑的流水线。"""
from __future__ import annotations

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .ai import AiConfig, ask
from .prompts import decide_prompt, extract_prompt

CATEGORIES = ("日系", "同人", "欧美", "其他")
DECISIONS = ("match", "no_match", "not_gal")


def _read_existing(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("key") is not None:
                    out[str(row["key"])] = row
    except OSError:
        pass
    return out


def _append_result(path: str, result: dict, lock: threading.Lock) -> None:
    line = json.dumps(result, ensure_ascii=False) + "\n"
    with lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()


def _clamp_confidence(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _empty_result(item: dict) -> dict:
    return {
        "key": str(item.get("key")),
        "path": item.get("path") or "",
        "name": item.get("name") or "",
        "status": "error",
        "ref": None,
        "category": None,
        "brand": "",
        "title": item.get("name") or "",
        "title_original": "",
        "release": None,
        "confidence": 0.0,
        "reason": "",
        "flags": [],
        "candidates": 0,
        "error": None,
    }


def _not_gal_result(item: dict, extract: dict) -> dict:
    category = extract.get("category_guess")
    if category not in CATEGORIES:
        category = "其他"
    title = extract.get("title_guess")
    if not isinstance(title, str) or not title.strip():
        title = item.get("name") or ""
    brand = extract.get("brand_guess")
    reason = "AI 判断不是 galgame"
    result = _empty_result(item)
    result.update({
        "status": "not_gal",
        "ref": None,
        "category": category,
        "brand": brand if isinstance(brand, str) else "",
        "title": title,
        "title_original": title,
        "confidence": 0.0,
        "reason": reason,
        "candidates": 0,
        "error": None,
    })
    return result


def _validated_result(item: dict, extract: dict, candidates: list[dict], decide: dict) -> dict:
    flags: list[str] = []
    decision = decide.get("decision")
    if decision not in DECISIONS:
        decision = "no_match"

    cand_refs = {c.get("ref") for c in candidates if isinstance(c, dict)}
    ref = decide.get("ref")
    if decision == "match":
        if not candidates or not isinstance(ref, str) or ref not in cand_refs:
            decision = "no_match"
            ref = None
            flags.append("ref_not_in_candidates")
    else:
        ref = None

    category = decide.get("category")
    if category not in CATEGORIES:
        category = "其他"
        flags.append("bad_category")

    confidence = _clamp_confidence(decide.get("confidence"))

    title = decide.get("title")
    if not isinstance(title, str) or not title.strip():
        title = extract.get("title_guess")
    if not isinstance(title, str) or not title.strip():
        title = item.get("name") or ""

    title_original = decide.get("title_original")
    if not isinstance(title_original, str) or not title_original.strip():
        title_original = title

    brand = decide.get("brand")
    if not isinstance(brand, str) or not brand:
        brand = extract.get("brand_guess")
    if not isinstance(brand, str):
        brand = ""

    release = decide.get("release")
    if not isinstance(release, str) or not release:
        release = None

    reason = decide.get("reason")
    if not isinstance(reason, str):
        reason = ""

    result = _empty_result(item)
    result.update({
        "status": decision,
        "ref": ref,
        "category": category,
        "brand": brand,
        "title": title,
        "title_original": title_original,
        "release": release,
        "confidence": confidence,
        "reason": reason,
        "flags": flags,
        "candidates": len(candidates),
        "error": None,
    })
    return result


def _process_item(item: dict, work_dir: str, config: AiConfig, searcher) -> dict:
    hints = item
    log_path = os.path.join(work_dir, "ai-log.jsonl")
    extract = ask(config, extract_prompt(hints), log_path=log_path)
    if not isinstance(extract, dict):
        raise ValueError("提取结果不是 JSON 对象")
    if extract.get("is_gal") is False:
        return _not_gal_result(item, extract)

    candidates = searcher.candidates(hints, extract)
    if not isinstance(candidates, list):
        candidates = []
    decide = ask(config, decide_prompt(hints, extract, candidates), log_path=log_path)
    if not isinstance(decide, dict):
        raise ValueError("判断结果不是 JSON 对象")
    return _validated_result(item, extract, candidates, decide)


def run(items: list[dict], work_dir: str, config: AiConfig, *, searcher, jobs: int = 1,
        limit: int | None = None, progress=None) -> list[dict]:
    """处理 items，结果追加写 results.jsonl；已有非 error 结果的 key 跳过。"""
    os.makedirs(work_dir, exist_ok=True)
    results_path = os.path.join(work_dir, "results.jsonl")
    existing = _read_existing(results_path)

    pending = [it for it in items
               if str(it.get("key")) not in existing
               or existing[str(it.get("key"))].get("status") == "error"]
    if limit is not None:
        pending = pending[:max(0, limit)]

    write_lock = threading.Lock()
    done_count = 0
    count_lock = threading.Lock()
    total = len(pending)

    def one(item: dict) -> dict:
        nonlocal done_count
        try:
            result = _process_item(item, work_dir, config, searcher)
        except Exception as exc:  # noqa: BLE001 - 单部失败不能中断整批
            message = str(exc) or type(exc).__name__
            result = _empty_result(item)
            result["error"] = message[:300]
        _append_result(results_path, result, write_lock)
        with count_lock:
            done_count += 1
            current = done_count
        if progress:
            progress(current, total, item.get("name") or str(item.get("key")))
        return result

    fresh: dict[str, dict] = {}
    if jobs > 1 and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(one, it): str(it.get("key")) for it in pending}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    fresh[key] = future.result()
                except Exception as exc:  # noqa: BLE001 - 兜底
                    item = next(it for it in pending if str(it.get("key")) == key)
                    result = _empty_result(item)
                    result["error"] = (str(exc) or type(exc).__name__)[:300]
                    _append_result(results_path, result, write_lock)
                    fresh[key] = result
    else:
        for item in pending:
            fresh[str(item.get("key"))] = one(item)

    all_results = dict(existing)
    all_results.update(fresh)
    return [all_results[str(it.get("key"))] for it in items
            if str(it.get("key")) in all_results]
