"""从游戏目录 / 现有库里只读收集识别线索。"""
from __future__ import annotations

import codecs
import html
import json
import os
import re
import sqlite3
import urllib.parse
from datetime import date

# 先匹配位置，再按出现顺序输出；前缀统一转大写。
_DLSITE_RE = re.compile(r"(?<![A-Za-z0-9])(RJ|VJ|BJ|RE)(\d{6}|\d{8})(?!\d)", re.IGNORECASE)
_VNDB_RE = re.compile(r"vndb\.org/(v\d+)", re.IGNORECASE)
_GETCHU_RE = re.compile(r"\[(\d{7})\]")
_DATE_RE = re.compile(r"^\[(\d{2})(\d{2})(\d{2})\]")

_README_RE = re.compile(
    r"readme|read me|説明|说明|使用须知|はじめ|最初|manual|steam_appid", re.IGNORECASE)
_README_EXTS = {".txt", ".md", ".htm", ".html", ".json"}
_MAX_README_BYTES = 8000
_MAX_README_CHARS = 1500


def find_codes(text: str) -> list[str]:
    """从普通文本里找 DLsite / VNDB / Getchu 编号，去重并保持出现顺序。"""
    if not isinstance(text, str) or not text:
        return []
    hits: list[tuple[int, str]] = []
    for m in _DLSITE_RE.finditer(text):
        hits.append((m.start(), f"dlsite:{m.group(1).upper()}{m.group(2)}"))
    for m in _VNDB_RE.finditer(text):
        hits.append((m.start(), f"vndb:{m.group(1).lower()}"))
    for m in _GETCHU_RE.finditer(text):
        hits.append((m.start(), f"getchu:{m.group(1)}"))
    hits.sort(key=lambda pair: pair[0])

    out: list[str] = []
    seen: set[str] = set()
    for _, code in hits:
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _parse_date(name: str) -> str | None:
    m = _DATE_RE.match(name or "")
    if not m:
        return None
    yy, mm, dd = (int(x) for x in m.groups())
    try:
        return date(2000 + yy, mm, dd).isoformat()
    except ValueError:
        return None


def _parents(path: str) -> list[str]:
    out: list[str] = []
    current = os.path.dirname(os.path.normpath(path))
    while current and len(out) < 3:
        name = os.path.basename(current)
        if not name:
            break
        out.append(name)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return out


def _walk_limited(root: str, max_dir_depth: int):
    """遍历 root；只产出所在目录深度 <= max_dir_depth 的文件。"""
    for base, dirs, files in os.walk(root):
        rel = os.path.relpath(base, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_dir_depth:
            dirs[:] = []
        dirs.sort()
        yield base, depth, sorted(files)


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    for encoding in ("utf-8", "gbk", "cp932"):
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            # final=False 会忽略末尾被截断的半个字符
            return decoder.decode(data, final=False)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _html_text(text: str) -> str:
    parts: list[str] = []
    for m in re.finditer(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL):
        parts.append(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))))
    parts.extend(re.findall(r"https?://[^\s\"'<>]+", text))
    return " ".join(parts)


def _read_text_file(path: str, ext: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read(_MAX_README_BYTES)
    except OSError:
        return ""
    text = _decode(data)
    if ext in (".htm", ".html"):
        text = _html_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_README_CHARS]


def _trim_potatovn(value):
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, list):
        if all(isinstance(x, str) for x in value):
            return [x[:300] for x in value]
        return _DROP
    return _DROP


class _Drop:
    pass


_DROP = _Drop()


def _load_potatovn(root: str) -> dict | None:
    if not os.path.isdir(root):
        return None
    for base, depth, files in _walk_limited(root, 3):
        if os.path.basename(base) != ".PotatoVN":
            continue
        if "meta.json" not in files:
            continue
        try:
            with open(os.path.join(base, "meta.json"), "rb") as fh:
                raw = json.loads(_decode(fh.read()))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        clean: dict = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                continue
            trimmed = _trim_potatovn(value)
            if trimmed is not _DROP:
                clean[key] = trimmed
        return clean
    return None


def _steam_appids(root: str) -> list[str]:
    out: list[str] = []
    if not os.path.isdir(root):
        return out
    for base, _depth, files in _walk_limited(root, 2):
        for name in files:
            if name.lower() != "steam_appid.txt":
                continue
            try:
                with open(os.path.join(base, name), "rb") as fh:
                    text = fh.read().decode("ascii", "ignore").strip()
            except OSError:
                continue
            if re.fullmatch(r"\d+", text):
                out.append(f"steam:{text}")
    return out


def _add_codes(out: list[str], seen: set[str], codes) -> None:
    for code in codes:
        if code not in seen:
            seen.add(code)
            out.append(code)


def collect_folder(key: str, path: str) -> dict:
    """只读收集一个文件夹 / 单个文件的线索。路径不存在也不抛异常。"""
    path = os.fspath(path)
    norm = os.path.normpath(path)
    name = os.path.basename(norm) or norm
    is_dir = os.path.isdir(path)
    exists = os.path.exists(path)

    size = 0
    files_top: list[str] = []
    exes: list[str] = []
    readmes: list[dict] = []

    if is_dir:
        try:
            with os.scandir(path) as it:
                entries = sorted((e for e in it if e.name not in (".", "..")), key=lambda e: e.name)
            files_top = [e.name for e in entries][:15]
        except OSError:
            entries = []
        for base, _depth, files in _walk_limited(path, 2):
            for filename in files:
                full = os.path.join(base, filename)
                try:
                    size += os.path.getsize(full)
                except OSError:
                    pass
                lower = filename.lower()
                if lower.endswith(".exe") and len(exes) < 10:
                    exes.append(filename)
                ext = os.path.splitext(filename)[1].lower()
                if (len(readmes) < 4 and ext in _README_EXTS and _README_RE.search(filename)):
                    text = _read_text_file(full, ext)
                    readmes.append({"name": filename, "text": text})
        # 顶层文件也算进 size（_walk_limited 已经包含 depth=0 的文件）
    elif os.path.isfile(path):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0

    potatovn = _load_potatovn(path) if is_dir else None

    codes: list[str] = []
    seen: set[str] = set()
    parents = _parents(path)
    _add_codes(codes, seen, find_codes(name))
    _add_codes(codes, seen, find_codes("\n".join(parents)))
    for readme in readmes:
        _add_codes(codes, seen, find_codes(readme["text"]))
    if potatovn is not None:
        _add_codes(codes, seen, find_codes(json.dumps(potatovn, ensure_ascii=False)))
    if is_dir:
        for appid in _steam_appids(path):
            _add_codes(codes, seen, [appid])

    return {
        "key": key,
        "path": path,
        "name": name,
        "parents": parents,
        "size_bytes": size,
        "date": _parse_date(name),
        "files_top": files_top,
        "exes": exes,
        "readmes": readmes,
        "potatovn": potatovn,
        "codes": codes,
    }


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    absolute = os.path.abspath(db_path).replace(os.sep, "/")
    uri = "file:" + urllib.parse.quote(absolute, safe="/:") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def collect_library(lib_path: str, only_unbound: bool = True) -> list[dict]:
    """从现有 .galdb 只读收集每部作品的线索。"""
    con = _connect_readonly(lib_path)
    try:
        bound: set[int] = set()
        if only_unbound:
            for (work_id,) in con.execute("SELECT work_id FROM external_links"):
                bound.add(work_id)

        works = list(con.execute("SELECT id, title FROM works ORDER BY id"))
        out: list[dict] = []
        for work_id, title in works:
            if only_unbound and work_id in bound:
                continue
            copies = [row[0] for row in con.execute(
                "SELECT path FROM copies WHERE work_id = ?", (work_id,))]
            if not copies:
                continue
            chosen = next((p for p in copies if os.path.exists(p)), copies[0])
            hint = collect_folder(str(work_id), chosen)
            hint["work_title"] = title
            hint["copies"] = copies
            out.append(hint)
        return out
    finally:
        con.close()
