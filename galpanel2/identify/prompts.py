"""两段式提示词：先提取线索，再在候选里判断。"""
from __future__ import annotations

import json

EXTRACT_MARK = "【任务:提取】"
DECIDE_MARK = "【任务:判断】"

_EXTRACT_LIMIT = 6000
NO_TOOLS = "不要调用任何工具，不要联网，只输出一个 JSON 对象（不要 Markdown 代码块）。"


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _copy_hints(hints: dict) -> dict:
    try:
        copied = json.loads(json.dumps(hints, ensure_ascii=False))
        if isinstance(copied, dict):
            return copied
    except (TypeError, ValueError):
        pass
    return dict(hints or {})


def _shrink(cur: dict) -> None:
    """按任务书顺序删减：先 readmes，再 files_top；之后才做兜底压缩。"""
    if cur.get("readmes"):
        cur["readmes"] = []
        return
    if cur.get("files_top"):
        cur["files_top"] = []
        return
    if cur.get("potatovn"):
        cur["potatovn"] = None
        return
    # 兜底：截短长字符串字段，避免异常大的线索撑爆提示词
    for key in ("name", "path", "work_title"):
        value = cur.get(key)
        if isinstance(value, str) and len(value) > 300:
            cur[key] = value[:300]
            return
    if isinstance(cur.get("parents"), list) and len(cur["parents"]) > 3:
        cur["parents"] = cur["parents"][:3]
        return
    if isinstance(cur.get("codes"), list) and len(cur["codes"]) > 40:
        cur["codes"] = cur["codes"][:40]
        return
    # 最后只剩核心字段，正常线索不会走到这里
    for key in list(cur):
        if key not in ("key", "name", "path", "parents", "date", "codes"):
            cur.pop(key, None)


def _fit_hints(hints: dict, build, limit: int = _EXTRACT_LIMIT) -> tuple[str, dict]:
    """按提示词正文长度删减 hints：先 readmes，再 files_top。"""
    cur = _copy_hints(hints)
    text = _dumps(cur)
    while len(build(text)) > limit:
        before = _dumps(cur)
        _shrink(cur)
        after = _dumps(cur)
        if after == before:
            break
        text = after
    return text, cur


_EXTRACT_BODY = """你是 galgame 资料识别助手。下面是从游戏文件夹里收集到的线索（JSON）。
{no_tools}
请根据线索判断这是哪部作品，并输出字段：
{{"is_gal": true,
 "title_guess": "标题猜测",
 "brand_guess": "品牌或社团猜测",
 "category_guess": "日系" | "同人" | "欧美" | "其他",
 "codes": ["dlsite:RJ01234567"],
 "queries": ["1-3 个适合在 VNDB/DLsite 搜索的关键词"]}}

说明：
- is_gal：这是不是 galgame / 视觉小说；不是就填 false。
- codes 只填线索里能确认的编号，格式同 dlsite:RJ01234567、vndb:v123、getchu:1265724。
- queries 去掉日期、版本号、汉化组、"DL版"等噪声，保留作品名 / 品牌名。
- 方括号里的 7 位数字通常是 Getchu 编号，不是 DLsite 编号。
- 线索 JSON：
{hints}
"""


def _extract_text(hints_text: str) -> str:
    return EXTRACT_MARK + "\n" + _EXTRACT_BODY.format(no_tools=NO_TOOLS, hints=hints_text)


def extract_prompt(hints: dict) -> str:
    text, _ = _fit_hints(hints, _extract_text, _EXTRACT_LIMIT)
    prompt = _extract_text(text)
    # 极端情况下上面已删无可删；再强行截断，保证不超过 6000 字
    if len(prompt) > _EXTRACT_LIMIT:
        prompt = prompt[:_EXTRACT_LIMIT]
    return prompt


_DECIDE_BODY = """你是 galgame 资料核对助手。下面有作品线索、提取结果和全部候选资料（JSON）。
{no_tools}
请在这些候选里判断哪一个是同一部作品，并输出字段：
{{"decision": "match" | "no_match" | "not_gal",
 "ref": "候选里的 ref，或 null",
 "category": "日系" | "同人" | "欧美" | "其他",
 "brand": "开发商或社团名",
 "title": "有官方中文名用中文名，否则用原名",
 "title_original": "原名",
 "release": "YYYY-MM-DD" 或 null,
 "confidence": 0.0 到 1.0,
 "reason": "一句话理由"}}

规则：
- 分类：日系（日本商业作品）、同人（同人社团 / DLsite 同人）、欧美、其他。
- 品牌：用候选资料里的开发商或社团名。
- 拿不准就选 no_match：错绑比不绑糟糕。
- 只能从下面的候选里选 ref；不要编造资料页链接或编号。

线索 JSON：
{hints}

提取结果 JSON：
{extract}

候选 JSON（全部保留）：
{candidates}
"""


def _decide_text(hints_text: str, extract: dict, candidates: list[dict]) -> str:
    slim: list[dict] = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        item = dict(cand)
        summary = item.get("summary")
        item["summary"] = summary[:150] if isinstance(summary, str) else ""
        slim.append(item)
    return DECIDE_MARK + "\n" + _DECIDE_BODY.format(
        no_tools=NO_TOOLS,
        hints=hints_text,
        extract=_dumps(extract or {}),
        candidates=_dumps(slim),
    )


def decide_prompt(hints: dict, extract: dict, candidates: list[dict]) -> str:
    # 用"不带候选"的正文衡量 hints 部分；候选之后全部保留，总长可以超过 6000
    text, _ = _fit_hints(hints, lambda h: _decide_text(h, extract, []), _EXTRACT_LIMIT)
    return _decide_text(text, extract, candidates)
