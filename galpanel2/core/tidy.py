"""Explicit shelf cleanup: backup, ignore, remove, merge, and read-only reports."""
from datetime import date
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import ntpath
import tempfile
import threading
import time

from . import binding, names, repository
from .domain import NotALibrary, SessionActive, TargetExists, WriteConflict, path_key, utc_now


def _copy_database(src_conn, dest_conn):
    src_conn.backup(dest_conn)


_LEAD = re.compile(r"^(?:\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）)")
_TRAIL = re.compile(r"(?:\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）)$")
_TAILS = (
    re.compile(r"[\s_\-]*v(?:er)?\.?\s*\d+(?:[._]\d+)*[a-z]?$", re.I),
    re.compile(r"[\s_\-]+\d+(?:\.\d+){1,3}[a-z]?$", re.I),
    re.compile(r"[\s_\-]+(?:windows|win64|win32|win|pc|mac|linux|android)$", re.I),
    re.compile(r"[\s_\-]+(?:r-18|r18)$", re.I),
    re.compile(r"[\s_\-]*\(?(?:mdf\+mds|iso)\)?$", re.I),
    re.compile(r"\s+パッケージ版$"),
    re.compile(r"\s+DL版$"),
)
_WEAK_WORDS = {"data", "pc", "game", "bin", "windows", "win", "release", "dist", "build", "app", "files", "main"}
_VERSION_NAME = re.compile(r"v?\.?\d+(?:[._]\d+)*[a-z]?$", re.I)
_FIX_NAME = re.compile(r"fix\s*\d+$", re.I)
_VERSION_WORD = re.compile(r"version\s.*$", re.I)
_DATE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
TOOL_WORDS = (
    "补丁", "patch", "翻译", "translation", "汉化补丁", "dlc", "unlock", "crack", "破解", "存档",
    "セーブ", "savedata", "backup", "字体", "font", "dictionar", "工具", "tool", "去广告", "绿色版",
    "sync", "migration", "mtool", "installer", "redist", "_auto",
)
_MIB10 = 10 * 1024 * 1024


def _v5(lib):
    if lib.schema_version < 5:
        raise ValueError("Library needs version 5")
    if lib.readonly:
        raise ValueError("Library is read-only")


def _v4(lib):
    if lib.schema_version < 4:
        raise ValueError("Library needs version 4")


def _has_table(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _log(c, action, backup, detail):
    c.execute("INSERT INTO tidy_log(action,at,backup_path,detail) VALUES(?,?,?,?)",
              (action, utc_now(), backup, _dump(detail)))


def _running(lib, work_ids):
    if not work_ids:
        return False
    marks = ",".join("?" * len(work_ids))
    with lib.connection() as c:
        return c.execute(
            f"SELECT 1 FROM play_sessions WHERE state='running' AND work_id IN ({marks})",
            list(work_ids)).fetchone() is not None


def backup_library(lib, tag: str) -> str:
    path = lib.path
    stamp = utc_now().replace("-", "").replace(":", "")
    n = 1
    while True:
        suffix = "" if n == 1 else f"-{n}"
        backup = f"{path}.{tag}-{stamp}{suffix}.bak"
        if not os.path.lexists(backup):
            break
        n += 1
    partial = None
    try:
        candidate = backup + ".partial"
        try:
            with open(candidate, "xb"):
                pass
        except FileExistsError as e:
            raise TargetExists(candidate) from e
        partial = candidate
        dest = sqlite3.connect(partial)
        try:
            with lib.connection() as c:
                _copy_database(c, dest)
                version = c.execute("PRAGMA user_version").fetchone()[0]
        finally:
            dest.close()
        with open(partial, "r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        check = sqlite3.connect(Path(partial).as_uri() + "?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or \
                    check.execute("PRAGMA user_version").fetchone()[0] != version:
                raise NotALibrary("Invalid tidy backup")
        finally:
            check.close()
        if os.path.lexists(backup):
            raise TargetExists(backup)
        os.replace(partial, backup)
        partial = None
        return os.path.abspath(backup)
    finally:
        if partial and os.path.exists(partial):
            os.unlink(partial)


def list_log(lib) -> list[dict]:
    with lib.connection() as c:
        if not _has_table(c, "tidy_log"):
            return []
        rows = []
        for r in c.execute("SELECT id,action,at,backup_path,detail FROM tidy_log ORDER BY id"):
            rows.append({"id": r[0], "action": r[1], "at": r[2], "backup_path": r[3],
                         "detail": json.loads(r[4])})
        return rows


def add_ignored(lib, path: str, scope: str = "exact", reason: str = "user") -> None:
    _v5(lib)
    key = path_key(path)
    if scope not in ("exact", "tree"):
        raise ValueError("scope must be exact or tree")
    if reason not in ("removed", "user"):
        raise ValueError("reason must be removed or user")
    with lib.transaction() as c:
        row = c.execute("SELECT scope FROM ignored_paths WHERE path_key=?", (key,)).fetchone()
        if row is None:
            kept = scope
            c.execute("INSERT INTO ignored_paths(path_key,path,scope,reason,added_at) VALUES(?,?,?,?,?)",
                      (key, path, scope, reason, utc_now()))
        else:
            kept = "tree" if row[0] == "tree" else scope
            c.execute("UPDATE ignored_paths SET path=?,scope=? WHERE path_key=?", (path, kept, key))
        _log(c, "ignore", None, {"path": path, "scope": kept, "op": "add"})


def remove_ignored(lib, path: str) -> bool:
    _v5(lib)
    key = path_key(path)
    with lib.transaction() as c:
        row = c.execute("SELECT path,scope FROM ignored_paths WHERE path_key=?", (key,)).fetchone()
        if row is None:
            return False
        c.execute("DELETE FROM ignored_paths WHERE path_key=?", (key,))
        _log(c, "ignore", None, {"path": row[0], "scope": row[1], "op": "remove"})
        return True


def list_ignored(lib) -> list[dict]:
    with lib.connection() as c:
        if not _has_table(c, "ignored_paths"):
            return []
        return [{"path": r[0], "path_key": r[1], "scope": r[2], "reason": r[3], "added_at": r[4]}
                for r in c.execute(
                    "SELECT path,path_key,scope,reason,added_at FROM ignored_paths ORDER BY path_key")]


def is_ignored(key: str, ignored: list[dict]) -> bool:
    for row in ignored:
        pk = row["path_key"]
        if row.get("scope") == "exact":
            if pk == key:
                return True
            continue
        if key == pk:
            return True
        prefix = pk if pk.endswith("\\") else pk + "\\"
        if key.startswith(prefix):
            return True
    return False


def remove_works(lib, revisions: dict[int, int], *, ignore: bool = True) -> dict:
    _v5(lib)
    if not revisions:
        raise ValueError("nothing to remove")
    if _running(lib, revisions):
        raise SessionActive("A game is already being tracked")
    backup = backup_library(lib, "tidy")
    with lib.transaction() as c:
        pending = []
        fresh = 0
        for wid in sorted(revisions):
            row = c.execute("SELECT uuid,title,revision FROM works WHERE id=?", (wid,)).fetchone()
            if row is None or row[2] != revisions[wid]:
                raise WriteConflict("Work changed or was removed")
            paths = [r[0] for r in c.execute("SELECT path FROM copies WHERE work_id=? ORDER BY id", (wid,))]
            pending.append({"id": wid, "uuid": row[0], "title": row[1], "paths": paths})
            if ignore:
                for copy_path in paths:
                    key = path_key(copy_path)
                    cur = c.execute(
                        "INSERT OR IGNORE INTO ignored_paths(path_key,path,scope,reason,added_at) VALUES(?,?,?,?,?)",
                        (key, copy_path, "exact", "removed", utc_now()))
                    fresh += cur.rowcount
        for item in pending:
            if c.execute("DELETE FROM works WHERE id=? AND revision=?",
                         (item["id"], revisions[item["id"]])).rowcount != 1:
                raise WriteConflict("Work changed or was removed")
        detail = {"works": pending, "ignored": fresh}
        _log(c, "remove", backup, detail)
    return {"removed": len(pending), "ignored": fresh, "backup": backup}


def _drive_root(directory: str) -> str:
    return ntpath.splitdrive(directory)[0] + "\\"


def remove_missing_group(lib, directory: str) -> dict:
    _v5(lib)
    group = next((row for row in missing_groups(lib) if row["group_key"] == path_key(directory)), None)
    if group is None:
        raise KeyError(directory)
    root = _drive_root(directory)
    if not os.path.exists(root):
        raise ValueError(f"drive offline: {root}")
    still, gone = [], []
    with lib.connection() as c:
        for wid in group["work_ids"]:
            paths = [row[0] for row in c.execute("SELECT path FROM copies WHERE work_id=?", (wid,))]
            if any(os.path.exists(copy_path) for copy_path in paths):
                still.append(wid)
            else:
                gone.append(wid)
    still.sort()
    if not gone:
        return {"removed": 0, "still_present": still, "tree_ignored": False, "backup": None}
    if _running(lib, gone):
        raise SessionActive("A game is already being tracked")
    backup = backup_library(lib, "tidy")
    tree = not still
    with lib.transaction() as c:
        pending = []
        revisions = {}
        for wid in sorted(gone):
            row = c.execute("SELECT uuid,title,revision FROM works WHERE id=?", (wid,)).fetchone()
            if row is None:
                raise WriteConflict("Work changed or was removed")
            paths = [item[0] for item in c.execute(
                "SELECT path FROM copies WHERE work_id=? ORDER BY id", (wid,))]
            revisions[wid] = row[2]
            pending.append({"id": wid, "uuid": row[0], "title": row[1], "paths": paths})
        fresh = 0
        for item in pending:
            if c.execute("DELETE FROM works WHERE id=? AND revision=?",
                         (item["id"], revisions[item["id"]])).rowcount != 1:
                raise WriteConflict("Work changed or was removed")
            for copy_path in item["paths"]:
                existed = c.execute("SELECT 1 FROM ignored_paths WHERE path_key=?",
                                    (path_key(copy_path),)).fetchone()
                add_ignored(lib, copy_path, "exact", "removed")
                if existed is None:
                    fresh += 1
        if tree:
            add_ignored(lib, directory, "tree", "user")
        _log(c, "remove", backup, {"works": pending, "ignored": fresh})
    return {"removed": len(pending), "still_present": still, "tree_ignored": tree, "backup": backup}


def _merge_logs(c, keep, other):
    for day, minutes in c.execute("SELECT day,minutes FROM legacy_play_logs WHERE work_id=?", (other,)):
        have = c.execute("SELECT minutes FROM legacy_play_logs WHERE work_id=? AND day=?", (keep, day)).fetchone()
        if have is None:
            c.execute("UPDATE legacy_play_logs SET work_id=? WHERE work_id=? AND day=?", (keep, other, day))
        else:
            c.execute("UPDATE legacy_play_logs SET minutes=? WHERE work_id=? AND day=?",
                      (have[0] + minutes, keep, day))


def _merge_fields(c, keep, other):
    have = {r[0]: r[1] for r in c.execute(
        "SELECT column_name,value_json FROM legacy_fields WHERE work_id=?", (keep,))}
    for name, raw in c.execute("SELECT column_name,value_json FROM legacy_fields WHERE work_id=?", (other,)):
        if name not in have:
            c.execute("UPDATE legacy_fields SET work_id=? WHERE work_id=? AND column_name=?", (keep, other, name))
            continue
        kept = json.loads(have[name])
        incoming = json.loads(raw)
        if name == "playtime_minutes" and kept.get("t") == "integer" and incoming.get("t") == "integer" \
                and type(kept.get("v")) is int and type(incoming.get("v")) is int:
            merged = {"t": "integer", "v": kept["v"] + incoming["v"]}
            have[name] = _dump(merged)
            c.execute("UPDATE legacy_fields SET value_json=? WHERE work_id=? AND column_name=?",
                      (have[name], keep, name))
        elif name == "last_played_at" and kept.get("t") == "text" and incoming.get("t") == "text" \
                and isinstance(kept.get("v"), str) and isinstance(incoming.get("v"), str) \
                and incoming["v"] > kept["v"]:
            have[name] = raw
            c.execute("UPDATE legacy_fields SET value_json=? WHERE work_id=? AND column_name=?",
                      (raw, keep, name))


def _merge_info(c, keep, other, now):
    keep_cover = c.execute("SELECT 1 FROM covers WHERE work_id=?", (keep,)).fetchone()
    other_cover = c.execute("SELECT 1 FROM covers WHERE work_id=?", (other,)).fetchone()
    brought = not keep_cover and bool(other_cover)
    if brought:
        c.execute("UPDATE covers SET work_id=? WHERE work_id=?", (keep, other))
    k = c.execute("SELECT summary,summary_source,release_date,release_source,cover_source "
                  "FROM work_info WHERE work_id=?", (keep,)).fetchone()
    o = c.execute("SELECT summary,summary_source,release_date,release_source,cover_source "
                  "FROM work_info WHERE work_id=?", (other,)).fetchone()
    if o is None:
        return
    if k is None:
        c.execute("UPDATE work_info SET work_id=?,updated_at=? WHERE work_id=?", (keep, now, other))
        return
    summary, ssrc, release, rsrc, cover_source = k
    if summary is None and ssrc is None:
        summary, ssrc = o[0], o[1]
    if release is None:
        release, rsrc = o[2], o[3]
    if brought and cover_source is None:
        cover_source = o[4]
    c.execute("UPDATE work_info SET summary=?,summary_source=?,release_date=?,release_source=?,"
              "cover_source=?,updated_at=? WHERE work_id=?",
              (summary, ssrc, release, rsrc, cover_source, now, keep))


def _merge_one(c, keep, other, dropped, now):
    c.execute("UPDATE copies SET work_id=? WHERE work_id=?", (keep, other))
    c.execute("UPDATE play_sessions SET work_id=? WHERE work_id=?", (keep, other))
    _merge_logs(c, keep, other)
    _merge_fields(c, keep, other)
    _merge_info(c, keep, other, now)
    c.execute("INSERT OR IGNORE INTO work_titles(work_id,title,title_key,kind,site) "
              "SELECT ?,title,title_key,kind,site FROM work_titles WHERE work_id=?", (keep, other))
    for site, site_id in c.execute("SELECT site,site_id FROM external_links WHERE work_id=? ORDER BY site", (other,)):
        if c.execute("SELECT 1 FROM external_links WHERE work_id=? AND site=?", (keep, site)).fetchone():
            dropped.append({"site": site, "site_id": site_id, "from": other})
        else:
            c.execute("UPDATE external_links SET work_id=? WHERE work_id=? AND site=?", (keep, other, site))
    pos = c.execute("SELECT COALESCE(MAX(position),-1) FROM work_brands WHERE work_id=?", (keep,)).fetchone()[0]
    owned = {r[0] for r in c.execute("SELECT brand_id FROM work_brands WHERE work_id=?", (keep,))}
    for (bid,) in c.execute("SELECT brand_id FROM work_brands WHERE work_id=? ORDER BY position,rowid", (other,)):
        if bid in owned:
            continue
        pos += 1
        c.execute("INSERT INTO work_brands(work_id,brand_id,position) VALUES(?,?,?)", (keep, bid, pos))
        owned.add(bid)
    c.execute("INSERT OR IGNORE INTO work_tags(work_id,tag_id) SELECT ?,tag_id FROM work_tags WHERE work_id=?",
              (keep, other))
    c.execute("INSERT OR IGNORE INTO work_source_tags(work_id,site,tag_key) "
              "SELECT ?,site,tag_key FROM work_source_tags WHERE work_id=?", (keep, other))
    for (tid,) in c.execute("SELECT tag_id FROM work_tag_blocks WHERE work_id=?", (other,)):
        if c.execute("SELECT 1 FROM work_tags WHERE work_id=? AND tag_id=?", (keep, tid)).fetchone():
            continue
        c.execute("INSERT OR IGNORE INTO work_tag_blocks(work_id,tag_id) VALUES(?,?)", (keep, tid))
    notes = c.execute("SELECT notes FROM works WHERE id=?", (keep,)).fetchone()[0]
    extra = c.execute("SELECT notes FROM works WHERE id=?", (other,)).fetchone()[0]
    if extra and extra != notes:
        c.execute("UPDATE works SET notes=? WHERE id=?", (extra if not notes else notes + "\n\n" + extra, keep))
    c.execute("DELETE FROM works WHERE id=?", (other,))


def merge_works(lib, keep_id: int, keep_revision: int, others: dict[int, int]) -> dict:
    _v5(lib)
    if not others or keep_id in others:
        raise ValueError("nothing to merge" if not others else "cannot merge a work into itself")
    if _running(lib, [keep_id, *others]):
        raise SessionActive("A game is already being tracked")
    backup = backup_library(lib, "tidy")
    with lib.transaction() as c:
        wanted = {keep_id: keep_revision, **others}
        for wid, rev in wanted.items():
            row = c.execute("SELECT revision FROM works WHERE id=?", (wid,)).fetchone()
            if row is None or row[0] != rev:
                raise WriteConflict("Work changed or was removed")
        keep_dev = c.execute("SELECT developer FROM works WHERE id=?", (keep_id,)).fetchone()[0]
        if not (keep_dev or "").strip():
            for oid in others:
                incoming = c.execute("SELECT developer FROM works WHERE id=?", (oid,)).fetchone()[0]
                if (incoming or "").strip():
                    c.execute("UPDATE works SET developer=? WHERE id=?", (incoming, keep_id))
                    break
        merged = []
        dropped = []
        now = utc_now()
        for oid in sorted(others):
            row = c.execute("SELECT uuid,title,developer FROM works WHERE id=?", (oid,)).fetchone()
            paths = [r[0] for r in c.execute("SELECT path FROM copies WHERE work_id=? ORDER BY id", (oid,))]
            merged.append({"id": oid, "uuid": row[0], "title": row[1], "developer": row[2], "copies": paths})
            _merge_one(c, keep_id, oid, dropped, now)
        c.execute("UPDATE works SET revision=revision+1,updated_at=? WHERE id=?", (now, keep_id))
        dropped.sort(key=lambda item: (item["from"], item["site"]))
        _log(c, "merge", backup, {"keep": keep_id, "merged": merged, "dropped_links": dropped})
        revision = c.execute("SELECT revision FROM works WHERE id=?", (keep_id,)).fetchone()[0]
    return {"keep": keep_id, "merged": [item["id"] for item in merged], "revision": revision,
            "dropped_links": dropped, "backup": backup}


def clean_title(name: str) -> str:
    return _clean_title(name, True)


def _clean_title(name: str, allow_recurse: bool) -> str:
    original = name
    text = name.strip()
    last_inner = None
    while True:
        match = _LEAD.match(text)
        if not match:
            break
        last_inner = match.group(0)[1:-1]
        text = text[match.end():].strip()
    while True:
        match = _TRAIL.search(text)
        if not match:
            break
        text = text[:match.start()].strip()
    changed = True
    while changed:
        changed = False
        for pattern in _TAILS:
            newer = pattern.sub("", text)
            if newer != text:
                text = newer
                changed = True
                break
    text = re.sub(r"\s+", " ", text.replace("_", " ")).strip().rstrip("-_.").strip()
    if text == "":
        if allow_recurse and last_inner is not None:
            inner = _clean_title(last_inner, False)
            if inner != "":
                return inner
        return original.strip()
    return text


def weak_name(name: str) -> bool:
    if not names.meaningful(name):
        return True
    if name.casefold() in _WEAK_WORDS:
        return True
    if re.fullmatch(r"\d+", name) or _VERSION_NAME.fullmatch(name) or _FIX_NAME.fullmatch(name) \
            or _VERSION_WORD.fullmatch(name):
        return True
    return len(name) <= 2


def container_name(name: str) -> bool:
    if re.fullmatch(r"\d{1,2}-\d{1,2}", name) or re.fullmatch(r"\d{1,2}\.\d{1,2}", name) \
            or re.fullmatch(r"\d{8}_\d{6}", name) or name.startswith("新建文件夹") \
            or re.match(r"^TRD\d", name) or re.fullmatch(r"[A-Z]\d{3,4}", name) \
            or re.fullmatch(r"\d{2,3}", name):
        return True
    return weak_name(name)


def _strip_archive(name: str) -> str:
    from .scanning import ARCHIVES
    low = name.lower()
    for ext in ARCHIVES:
        if low.endswith(ext):
            return name[:-len(ext)]
    return name


def _is_root(path: str) -> bool:
    drive, tail = ntpath.splitdrive(path.replace("/", "\\"))
    if re.fullmatch(r"[A-Za-z]:", drive or ""):
        return tail.strip("\\") == ""
    if drive.startswith("\\\\"):
        return tail.strip("\\") == ""
    return False


def _windows(path: str) -> str:
    return path.replace("/", "\\")


def suggest_title(current: str, copy_path: str, source_root: str | None,
                  original_title: str | None = None) -> tuple[str, str] | None:
    if original_title is not None:
        original = original_title.strip()
        if original and not weak_name(original):
            cleaned = clean_title(original)
            if names.normalized_title(cleaned) != names.normalized_title(current):
                return cleaned, "legacy_original"
    path = _windows(copy_path).rstrip("\\")
    root_key = path_key(source_root) if source_root else None
    chosen = _strip_archive(ntpath.basename(path))
    reason = "cleaned"
    if weak_name(chosen):
        cursor = path
        for _ in range(3):
            parent = ntpath.dirname(cursor)
            if not parent or parent == cursor or _is_root(parent):
                break
            try:
                parent_key = path_key(parent)
            except ValueError:
                break
            if root_key is not None and parent_key == root_key:
                break
            folder = ntpath.basename(parent.rstrip("\\"))
            cursor = parent
            if container_name(folder):
                continue
            chosen = folder
            reason = "parent_folder"
            break
    candidate = clean_title(chosen)
    if names.normalized_title(candidate) == names.normalized_title(current):
        return None
    if weak_name(candidate):
        return None
    return candidate, reason


def _legacy_text(rows, column):
    raw = rows.get(column)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("t") != "text" or not isinstance(obj.get("v"), str):
        return None
    return obj["v"]


def title_report(lib) -> list[dict]:
    _v4(lib)
    with lib.connection() as c:
        sources = dict(c.execute("SELECT id,path FROM sources"))
        copies = {}
        for r in c.execute("SELECT id,work_id,path,availability,source_id FROM copies ORDER BY id"):
            copies.setdefault(r[1], []).append(r)
        legacy = {}
        for r in c.execute("SELECT work_id,column_name,value_json FROM legacy_fields WHERE column_name='original_title'"):
            legacy.setdefault(r[0], {})[r[1]] = r[2]
        out = []
        for work in c.execute("SELECT id,revision,title FROM works ORDER BY id"):
            group = copies.get(work[0])
            if not group:
                continue
            chosen = next((row for row in group if row[3] == "ok"), group[0])
            original = _legacy_text(legacy.get(work[0], {}), "original_title")
            suggested = suggest_title(work[2], chosen[2], sources.get(chosen[4]), original)
            if suggested is None:
                continue
            out.append({"work_id": work[0], "revision": work[1], "current": work[2],
                        "suggested": suggested[0], "reason": suggested[1], "path": chosen[2]})
        return out


def retitle(lib, titles: dict[int, tuple[int, str]]) -> int:
    _v5(lib)
    cleaned = {}
    for wid, (rev, title) in titles.items():
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Title must not be blank")
        cleaned[wid] = (rev, title.strip())
    if not cleaned:
        return 0
    with lib.transaction() as c:
        old = {}
        for wid in cleaned:
            row = c.execute("SELECT title FROM works WHERE id=?", (wid,)).fetchone()
            if row is None:
                raise WriteConflict("Work changed or was removed")
            old[wid] = row[0]
        for wid, (rev, title) in cleaned.items():
            repository.edit_work(lib, wid, rev, title=title)
        changes = [{"id": wid, "old": old[wid], "new": cleaned[wid][1]} for wid in sorted(cleaned)]
        _log(c, "retitle", None, {"changes": changes})
    return len(cleaned)


def duplicate_report(lib) -> list[dict]:
    _v4(lib)
    with lib.connection() as c:
        works = [{"id": r[0], "title": r[1]} for r in c.execute("SELECT id,title FROM works ORDER BY id")]
        copies = {}
        for r in c.execute("SELECT work_id,kind,path FROM copies ORDER BY id"):
            copies.setdefault(r[0], []).append((r[1], r[2]))
    buckets = {}
    for work in works:
        buckets.setdefault(names.normalized_title(clean_title(work["title"])), []).append(work)
    groups = []
    together = set()
    for key, bucket in buckets.items():
        if len(bucket) < 2:
            continue
        bucket.sort(key=lambda item: item["id"])
        ids = [item["id"] for item in bucket]
        groups.append({"key": key, "work_ids": ids, "titles": [item["title"] for item in bucket],
                       "reason": "same_title"})
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                together.add((left, right))

    def norm_of(kind, path):
        path = _windows(path)
        base = ntpath.basename(path.rstrip("\\"))
        if kind == "archive":
            base = _strip_archive(base)
        parent = ntpath.dirname(path.rstrip("\\"))
        try:
            parent_key = path_key(parent)
        except ValueError:
            return None
        return names.normalized_title(clean_title(base)), parent_key

    seen = set()
    for left in works:
        for right in works:
            if left["id"] >= right["id"]:
                continue
            pair = (left["id"], right["id"])
            if pair in together or pair in seen:
                continue
            left_arch = [norm_of("archive", path) for kind, path in copies.get(left["id"], []) if kind == "archive"]
            right_arch = [norm_of("archive", path) for kind, path in copies.get(right["id"], []) if kind == "archive"]
            left_dir = [norm_of("folder", path) for kind, path in copies.get(left["id"], []) if kind == "folder"]
            right_dir = [norm_of("folder", path) for kind, path in copies.get(right["id"], []) if kind == "folder"]
            matched = None
            for arch in left_arch:
                for folder in right_dir:
                    if arch and folder and arch == folder:
                        matched = arch[0]
            for arch in right_arch:
                for folder in left_dir:
                    if arch and folder and arch == folder:
                        matched = arch[0]
            if matched is None:
                continue
            seen.add(pair)
            groups.append({"key": matched, "work_ids": [left["id"], right["id"]],
                           "titles": [left["title"], right["title"]], "reason": "archive_and_folder"})
    groups.sort(key=lambda item: item["work_ids"][0])
    return groups


def _group_of(copy_path, source_path):
    norm = ntpath.normpath(_windows(copy_path))
    if not source_path:
        parent = ntpath.dirname(norm)
        return parent, path_key(parent)
    source = ntpath.normpath(_windows(source_path))
    sk = path_key(source)
    ck = path_key(norm)
    prefix = sk if sk.endswith("\\") else sk + "\\"
    if ck == sk or not ck.startswith(prefix):
        parent = ntpath.dirname(norm)
        return parent, path_key(parent)
    rest = ck[len(prefix):]
    if "\\" not in rest:
        return norm[:len(sk)], sk
    group_key = prefix + rest.split("\\", 1)[0]
    return norm[:len(group_key)], path_key(norm[:len(group_key)])


def missing_groups(lib) -> list[dict]:
    _v4(lib)
    with lib.connection() as c:
        sources = dict(c.execute("SELECT id,path FROM sources"))
        copies = {}
        for r in c.execute("SELECT id,work_id,path,availability,source_id FROM copies ORDER BY id"):
            copies.setdefault(r[1], []).append(r)
        works = [r[0] for r in c.execute("SELECT id FROM works ORDER BY id")]
    grouped = {}
    for wid in works:
        rows = copies.get(wid, [])
        if not rows or any(row[3] != "missing" for row in rows):
            continue
        chosen = rows[0]
        display, gkey = _group_of(chosen[2], sources.get(chosen[4]))
        slot = grouped.get(gkey)
        if slot is None:
            grouped[gkey] = {"group": display, "group_key": gkey, "work_ids": [wid], "count": 1}
        else:
            slot["work_ids"].append(wid)
            slot["count"] += 1
    return sorted(grouped.values(), key=lambda item: (-item["count"], item["group_key"]))


def suspect_report(lib) -> list[dict]:
    _v4(lib)
    with lib.connection() as c:
        copies = {}
        for r in c.execute("SELECT id,work_id,kind,path,exe_relpath,size_bytes FROM copies ORDER BY id"):
            copies.setdefault(r[1], []).append(r)
        out = []
        for work in c.execute("SELECT id,title FROM works ORDER BY id"):
            rows = copies.get(work[0], [])
            reasons = []
            haystacks = [work[1].casefold()]
            haystacks.extend(ntpath.basename(_windows(row[3])).casefold() for row in rows)
            if any(word.casefold() in text for text in haystacks for word in TOOL_WORDS):
                reasons.append("tool_word")
            sizes = [row[5] for row in rows]
            if rows and all(size is not None for size in sizes) and max(sizes) < _MIB10:
                reasons.append("tiny")
            if rows and all(row[2] == "folder" and not (row[4] or "") for row in rows):
                reasons.append("no_exe")
            if reasons:
                out.append({"work_id": work[0], "title": work[1], "reasons": reasons,
                            "path": rows[0][3]})
        return out


def _release_date(text):
    match = _DATE.fullmatch(text.strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _vndb_token(value):
    token = str(value).strip().casefold()
    if token.isdigit():
        token = "v" + token
    if re.fullmatch(r"v\d+", token):
        return token, None
    return None, str(value)


def _bindings(text):
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None, {}, [text]
    if not isinstance(obj, dict):
        return None, {}, [text]
    folded = {}
    for key, value in obj.items():
        folded[str(key).casefold()] = value
    bad = []
    vndb = None
    if "vndb" in folded:
        vndb, rejected = _vndb_token(folded.pop("vndb"))
        if rejected is not None:
            bad.append(rejected)
    others = {key: str(value) for key, value in folded.items()}
    return vndb, others, bad


def legacy_report(lib) -> list[dict]:
    _v4(lib)
    with lib.connection() as c:
        fields = {}
        for r in c.execute("SELECT work_id,column_name,value_json FROM legacy_fields"):
            fields.setdefault(r[0], {})[r[1]] = r[2]
        links = {}
        owners = {}
        for wid, site_id in c.execute("SELECT work_id,site_id FROM external_links WHERE site='vndb'"):
            links.setdefault(wid, set()).add(site_id)
            owners.setdefault(site_id, []).append(wid)
        titles = dict(c.execute("SELECT id,title FROM works"))
    out = []
    for wid in sorted(fields):
        raw = {}
        for column, payload in fields[wid].items():
            try:
                obj = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(obj, dict):
                raw[column] = obj
        vndb = None
        others = {}
        bad = []
        bindings = raw.get("bindings")
        if bindings and bindings.get("t") == "text" and isinstance(bindings.get("v"), str):
            vndb, others, bad = _bindings(bindings["v"])
        summary = None
        desc = raw.get("description")
        if desc and desc.get("t") == "text" and isinstance(desc.get("v"), str) and desc["v"].strip():
            summary = desc["v"].strip()
        release = None
        rel = raw.get("release_date")
        if rel and rel.get("t") == "text" and isinstance(rel.get("v"), str):
            release = _release_date(rel["v"])
        original = None
        orig = raw.get("original_title")
        if orig and orig.get("t") == "text" and isinstance(orig.get("v"), str) and orig["v"].strip():
            original = orig["v"].strip()
        poster = None
        poster_raw = raw.get("poster_url")
        if poster_raw and poster_raw.get("t") == "text" and isinstance(poster_raw.get("v"), str) \
                and (poster_raw["v"].startswith("http://") or poster_raw["v"].startswith("https://")):
            poster = poster_raw["v"]
        useful = any((vndb, others, summary, release, original, poster))
        if not useful:
            continue
        holders = [owner for owner in owners.get(vndb, []) if owner != wid] if vndb else []
        out.append({"work_id": wid, "title": titles.get(wid, ""), "vndb": vndb,
                    "vndb_taken_by": min(holders) if holders else None,
                    "has_vndb_link": wid in links, "others": others, "summary": summary,
                    "release_date": release, "original_title": original, "poster_url": poster, "bad": bad})
    return out


def legacy_summary_text(text: str | None) -> str:
    """Old descriptions were stored with HTML entities. Decode for reading; the field itself stays as stored."""
    return html.unescape(text or "")


def use_legacy_summary(lib, work_id: int) -> bool:
    _v5(lib)
    row = next((item for item in legacy_report(lib) if item["work_id"] == work_id), None)
    if row is None or not row["summary"]:
        return False
    with lib.connection() as c:
        info = c.execute("SELECT summary_source FROM work_info WHERE work_id=?", (work_id,)).fetchone()
    if info and info[0] == "user":
        return False
    # set_summary stores the string it is given (after strip). It does not decode entities.
    text = legacy_summary_text(row["summary"]).strip()
    binding.set_summary(lib, work_id, text)
    with lib.transaction() as c:
        _log(c, "legacy_summary", None, {"id": work_id, "chars": len(text)})
    return True


def dismiss_path(lib_path: str) -> str:
    return lib_path + ".tidy-dismissed.json"


def missing_work_ids(lib) -> set[int]:
    found = set()
    for group in missing_groups(lib):
        found.update(group["work_ids"])
    return found


def _int_list(value):
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        raise ValueError("expected int ids")
    return sorted(set(value))


def _dup_list(value):
    if not isinstance(value, list):
        raise ValueError("expected duplicate groups")
    groups = []
    for item in value:
        if not isinstance(item, list) or any(type(n) is not int for n in item):
            raise ValueError("expected duplicate groups")
        groups.append(tuple(sorted(set(item))))
    return sorted(set(groups))


def _dup_key(key):
    if isinstance(key, (str, bytes)) or not isinstance(key, (list, tuple)):
        raise ValueError("duplicates key must be ids")
    if any(type(item) is not int for item in key):
        raise ValueError("duplicates key must be ids")
    return tuple(sorted(set(key)))


class DismissStore:
    """Sidecar of works the tidy page should stop offering. A bad file reads as empty."""

    def __init__(self, lib_path: str, library_uuid: str):
        self.lib_path = lib_path
        self.library_uuid = library_uuid
        self.path = dismiss_path(lib_path)
        self._lock = threading.Lock()

    def load(self) -> dict:
        with self._lock:
            data = self._read()
        if data is None:
            return {"junk": set(), "titles": set(), "duplicates": set()}
        return {"junk": set(data["junk"]), "titles": set(data["titles"]),
                "duplicates": {tuple(item) for item in data["duplicates"]}}

    def add(self, kind, key) -> None:
        if kind not in ("junk", "titles", "duplicates"):
            raise ValueError("kind must be junk, titles or duplicates")
        if kind == "duplicates":
            stored = _dup_key(key)
        elif type(key) is not int:
            raise ValueError("id must be int")
        else:
            stored = key
        with self._lock:
            data = self._read() or {"junk": [], "titles": [], "duplicates": []}
            if kind == "duplicates":
                data["duplicates"] = sorted(set(data["duplicates"]) | {stored})
            else:
                data[kind] = sorted(set(data[kind]) | {stored})
            self._write(data)

    def drop_works(self, work_ids) -> None:
        gone = {item for item in work_ids if type(item) is int}
        if not gone:
            return
        with self._lock:
            data = self._read()
            if data is None:
                return
            data["junk"] = [item for item in data["junk"] if item not in gone]
            data["titles"] = [item for item in data["titles"] if item not in gone]
            data["duplicates"] = [item for item in data["duplicates"] if not any(n in gone for n in item)]
            self._write(data)

    def _read(self) -> dict | None:
        if not os.path.exists(self.path):
            return None
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            self._sideline()
            return None
        if not isinstance(data, dict) or data.get("version") != 1:
            self._sideline()
            return None
        if data.get("library_uuid") != self.library_uuid:
            return None
        try:
            return {"junk": _int_list(data.get("junk")), "titles": _int_list(data.get("titles")),
                    "duplicates": _dup_list(data.get("duplicates"))}
        except ValueError:
            self._sideline()
            return None

    def _sideline(self) -> None:
        if not os.path.exists(self.path):
            return
        os.replace(self.path, f"{self.path}.bad-{time.time_ns()}")

    def _write(self, data: dict) -> None:
        payload = {"version": 1, "library_uuid": self.library_uuid,
                   "junk": list(data["junk"]), "titles": list(data["titles"]),
                   "duplicates": [list(item) for item in data["duplicates"]]}
        directory = str(Path(self.path).parent)
        fd, temp = tempfile.mkstemp(prefix=".tidy-dismiss-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            delay = 0.02
            for attempt in range(6):
                try:
                    os.replace(temp, self.path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(delay)
                    delay *= 2
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
