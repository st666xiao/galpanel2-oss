"""Read-only legacy snapshots; typed conservation before publishing a new DB."""
import base64
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import ntpath
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .domain import (LegacyInfo, RejectedRow, ImportReport, LegacyError, TargetExists,
                     check_cancel, path_key, utc_now)
from .repository import copy_cover
from .store import Library, _WriterLock, _initialize, open_library, validate_target
from .scanning import test_pause


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def typed(value, type_name):
    return {"t": type_name, "v": base64.b64encode(value).decode("ascii") if type_name == "blob" else value}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@contextmanager
def _legacy_connection(path):
    p = Path(path).absolute()
    for suffix in ("-wal", "-journal", "-shm"):
        if os.path.lexists(str(p) + suffix):
            raise LegacyError(f"Legacy sidecar exists: {p}{suffix}")
    try:
        c = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='games'").fetchone():
            c.close()
            raise LegacyError("Legacy database has no games table")
    except sqlite3.DatabaseError as e:
        raise LegacyError(f"Cannot read legacy database: {e}") from e
    try:
        yield c
    except sqlite3.DatabaseError as e:
        raise LegacyError(f"Invalid legacy schema/data: {e}") from e
    finally:
        c.close()


def _snapshot(path, cancel=None):
    check_cancel(cancel)
    before = sha256(path)
    with _legacy_connection(path) as c:
        c.execute("BEGIN")
        columns = tuple(r[1] for r in c.execute("PRAGMA table_info(games)"))
        if not {"id", "kind", "path"} <= set(columns):
            raise LegacyError("Legacy games table must contain id, kind and path")
        sql = "SELECT " + ",".join(quote(n) for n in columns) + "," + ",".join(
            f"typeof({quote(n)})" for n in columns) + " FROM games ORDER BY id"
        games = []
        for r in c.execute(sql):
            check_cancel(cancel)
            values = dict(zip(columns, r[:len(columns)]))
            fields = {n: typed(r[i], r[i + len(columns)]) for i, n in enumerate(columns)}
            games.append((values, fields))
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id")] if "sources" in tables else []
        logs = [dict(r) for r in c.execute("SELECT * FROM play_logs ORDER BY game_id,date")] if "play_logs" in tables else []
    after = sha256(path)
    if after != before:
        raise LegacyError("Legacy file changed while reading")
    check_cancel(cancel)
    return before, columns, games, sources, logs


def inspect_legacy(db_path: str) -> LegacyInfo:
    digest, columns, games, sources, logs = _snapshot(db_path)
    return LegacyInfo(db_path, digest, columns, len(games), len(sources), len(logs),
                      sum(bool(r.get("notes")) for r, _ in games),
                      sum(r.get("bindings") not in (None, "", "{}") for r, _ in games))


def _rejections(games):
    keys = {}
    for r, _ in games:
        try:
            keys[r["id"]] = path_key(r["path"])
        except ValueError:
            pass
    counts = Counter(keys.values())
    rejected = []
    for r, _ in games:
        reason = ("empty_path" if not r["path"] else "relative_path" if r["id"] not in keys else
                  "duplicate_path" if counts[keys[r["id"]]] > 1 else
                  "bad_kind" if r["kind"] not in ("folder", "archive") else None)
        if reason:
            rejected.append(RejectedRow(r["id"], r["path"], reason))
    return keys, tuple(rejected)


def _mapping(r, imported_at):
    title = (r.get("title") or "").strip() or (r.get("original_title") or "").strip() or ntpath.basename(r["path"])
    if not title.strip():
        raise LegacyError(f"No representable title for legacy id {r['id']}")
    # Missing columns have documented schema defaults; an existing NULL is not silently rewritten.
    developer = r.get("developer", "")
    notes = r.get("notes", "")
    if not isinstance(developer, str) or not isinstance(notes, str):
        raise LegacyError(f"Non-text developer/notes for legacy id {r['id']}")
    added_at = r.get("added_at") or imported_at
    exe = None
    if r.get("exe_path"):
        try:
            root_key = path_key(r["path"])
            exe_key = path_key(r["exe_path"])
            if exe_key.startswith(root_key.rstrip("\\") + "\\"):
                exe = ntpath.relpath(r["exe_path"], r["path"])
        except ValueError:
            pass
    return title, developer, notes, int(bool(r.get("hidden"))), added_at, exe


def _source_json(row):
    return dumps({k: (typed(v, "blob") if isinstance(v, bytes) else v) for k, v in row.items()})


def _remove_partial(path):
    p = Path(path)
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.exists():
        shutil.rmtree(p)


def _verify(snapshot, lib, cancel=None, cover_directory=None):
    digest, columns, games, sources, logs = snapshot
    keys, rejected = _rejections(games)
    rejected_ids = {x.old_id for x in rejected}
    errors = []
    with lib.connection() as c:
        imports = c.execute("SELECT * FROM imports ORDER BY id").fetchall()
        if len(imports) != 1:
            return ["Expected exactly one legacy import"]
        imp = imports[0]
        if imp["sha256_before"] != digest or imp["sha256_after"] != digest:
            errors.append("Legacy SHA-256 differs from import snapshot")
        if json.loads(imp["columns_json"]) != list(columns):
            errors.append("Legacy column manifest differs")
        if json.loads(imp["rejected_json"]) != [asdict(r) for r in rejected]:
            errors.append("Rejected row manifest differs")
        if imp["imported"] + len(rejected) != len(games):
            errors.append("Imported + rejected count differs from games")
        identities = {}
        for r in c.execute("SELECT work_id,value_json FROM legacy_fields WHERE column_name='id'"):
            old_id = json.loads(r[1])["v"]
            identities.setdefault(old_id, []).append(r[0])
        expected_ids = {r["id"] for r, _ in games} - rejected_ids
        if set(identities) != expected_ids:
            errors.append("Imported work identities differ from accepted rows")
        if c.execute("SELECT count(*) FROM works").fetchone()[0] != len(expected_ids) or \
                c.execute("SELECT count(*) FROM copies").fetchone()[0] != len(expected_ids):
            errors.append("Work/copy count differs from accepted rows")
        expected_logs = []
        for r, fields in games:
            check_cancel(cancel)
            if r["id"] in rejected_ids:
                continue
            ids = identities.get(r["id"], [])
            if len(ids) != 1:
                errors.append(f"id={r['id']}: missing/ambiguous imported work")
                continue
            wid = ids[0]
            saved = {x[0]: json.loads(x[1]) for x in c.execute(
                "SELECT column_name,value_json FROM legacy_fields WHERE work_id=? AND import_id=?", (wid, imp["id"]))}
            if saved != fields:
                for col in sorted(set(saved) | set(fields)):
                    if saved.get(col) != fields.get(col):
                        errors.append(f"id={r['id']}: field {col} type/value differs")
            w = c.execute("SELECT title,developer,notes,hidden,added_at FROM works WHERE id=?", (wid,)).fetchone()
            title, dev, notes, hidden, added, exe = _mapping(r, imp["imported_at"])
            if not w or tuple(w) != (title, dev, notes, hidden, added):
                errors.append(f"id={r['id']}: work mapping differs")
            cp = c.execute("SELECT kind,path,path_key,exe_relpath,origin,availability FROM copies WHERE work_id=?", (wid,)).fetchall()
            if len(cp) != 1 or tuple(cp[0]) != (r["kind"], r["path"], keys[r["id"]], exe, "legacy", "unknown"):
                errors.append(f"id={r['id']}: copy mapping differs")
            for log in logs:
                if log["game_id"] == r["id"]:
                    expected_logs.append((wid, log["date"], log["minutes"]))
        got_logs = [tuple(x) for x in c.execute("SELECT work_id,day,minutes FROM legacy_play_logs")]
        if Counter(got_logs) != Counter(expected_logs):
            errors.append("Legacy play logs differ")
        got_sources = [(x[0], x[1]) for x in c.execute("SELECT old_id,row_json FROM legacy_sources WHERE import_id=?", (imp["id"],))]
        if {i: json.loads(row) for i, row in got_sources} != {r["id"]: json.loads(_source_json(r)) for r in sources}:
            errors.append("Legacy source rows differ")
        source_keys = {path_key(r["path"]) for r in sources}
        got_source_keys = {x[0] for x in c.execute("SELECT path_key FROM sources")}
        if got_source_keys != source_keys:
            errors.append("Source mapping differs")
        for cp in c.execute("SELECT id,path_key,source_id FROM copies"):
            candidates = [k for k in source_keys if cp["path_key"].startswith(k.rstrip("\\") + "\\")]
            source = c.execute("SELECT path_key FROM sources WHERE id=?", (cp["source_id"],)).fetchone()
            if (candidates and (not source or source[0] not in candidates)) or (not candidates and source):
                errors.append(f"copy={cp['id']}: source mapping differs")
        for cr in c.execute("SELECT work_id,file_name,sha256 FROM covers"):
            f = Path(cover_directory or lib.path + ".covers") / cr[1]
            if not f.is_file() or sha256(f) != cr[2]:
                errors.append(f"work={cr[0]}: cover file/hash differs")
    return errors


def import_legacy(db_path: str, target_path: str, *, cancel=None) -> ImportReport:
    target = validate_target(target_path)
    covers = target + ".covers"
    def ensure_target_free():
        if os.path.lexists(target):
            raise TargetExists("目标已存在，请换一个保存位置")
        if os.path.lexists(covers):
            raise TargetExists(f"{covers} 已存在，但库文件不在；这可能是上次没完成的导入留下的。"
                               "请换一个保存位置，或自己确认后移走这个目录。")
    ensure_target_free()
    lock = _WriterLock(target)
    partial = target + ".partial"
    partial_covers = target + ".covers.partial"
    renamed_covers = False
    lib = None
    try:
        ensure_target_free()
        _remove_partial(partial)
        _remove_partial(partial_covers)
        snapshot = _snapshot(db_path, cancel)
        digest, columns, games, sources, logs = snapshot
        keys, rejected = _rejections(games)
        rejected_ids = {r.old_id for r in rejected}
        u = _initialize(partial)
        lib = Library(partial, u, False)
        Path(partial_covers).mkdir()
        copied = missing = 0
        now = utc_now()
        identities = {}
        with lib.transaction() as c:
            iid = c.execute("INSERT INTO imports(source_db_path,sha256_before,sha256_after,columns_json,imported,"
                            "rejected_json,imported_at) VALUES(?,?,?,?,?,?,?)",
                            (db_path, digest, digest, dumps(columns), len(games) - len(rejected),
                             dumps([asdict(r) for r in rejected]), now)).lastrowid
            source_ids = {}
            for src in sources:
                check_cancel(cancel)
                c.execute("INSERT INTO legacy_sources VALUES(?,?,?)", (iid, src["id"], _source_json(src)))
                try:
                    key = path_key(src["path"])
                except ValueError as e:
                    raise LegacyError("Legacy source path is not absolute; no mapping rule provided") from e
                c.execute("INSERT OR IGNORE INTO sources(path,path_key,label,added_at) VALUES(?,?,?,?)",
                          (src["path"], key, src.get("name") or "", now))
                source_ids[key] = c.execute("SELECT id FROM sources WHERE path_key=?", (key,)).fetchone()[0]
            for r, fields in games:
                check_cancel(cancel)
                if r["id"] in rejected_ids:
                    continue
                title, dev, notes, hidden, added, exe = _mapping(r, now)
                wid = c.execute("INSERT INTO works(uuid,title,developer,notes,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                (str(uuid.uuid4()), title, dev, notes, hidden, added, now)).lastrowid
                identities[r["id"]] = wid
                matches = [sid for key, sid in source_ids.items() if keys[r["id"]].startswith(key.rstrip("\\") + "\\")]
                if len(matches) > 1:
                    raise LegacyError("Overlapping legacy sources: no unambiguous assignment rule provided")
                c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,origin,added_at) "
                          "VALUES(?,?,?,?,?,?,'legacy',?)",
                          (wid, matches[0] if matches else None, r["kind"], r["path"], keys[r["id"]], exe, added))
                c.executemany("INSERT INTO legacy_fields VALUES(?,?,?,?)",
                              [(wid, iid, col, dumps(value)) for col, value in fields.items()])
                poster = r.get("poster_path")
                if poster:
                    poster_path = poster if ntpath.isabs(poster) or os.path.isabs(poster) else \
                        str(Path(db_path).absolute().parent / poster.replace("\\", os.sep))
                    try:
                        name, h = copy_cover(poster_path, partial_covers)
                    except (OSError, ValueError):
                        missing += 1
                    else:
                        c.execute("INSERT INTO covers VALUES(?,?,?,'legacy')", (wid, name, h))
                        copied += 1
            for log in logs:
                check_cancel(cancel)
                if log["game_id"] in identities:
                    c.execute("INSERT INTO legacy_play_logs VALUES(?,?,?)",
                              (identities[log["game_id"]], log["date"], log["minutes"]))
            check_cancel(cancel)
        errs = _verify(snapshot, lib, cancel, partial_covers)
        lib.close()
        lib = None
        if errs:
            raise LegacyError("Conservation failed: " + "; ".join(errs))
        after = sha256(db_path)
        if after != digest:
            raise LegacyError("Legacy file changed before publication")
        check_cancel(cancel)
        test_pause()
        check_cancel(cancel)
        # The DB is published last. A visible DB always contains a complete import.
        os.rename(partial_covers, covers)
        renamed_covers = True
        os.replace(partial, target)
        return ImportReport(target_path, len(identities), rejected, copied, missing, digest, after, ())
    except BaseException:
        if lib:
            lib.close()
        _remove_partial(partial)
        _remove_partial(partial_covers)
        if renamed_covers and not os.path.exists(target):
            # Only the directory just created by this operation can be cleaned up.
            _remove_partial(covers)
        raise
    finally:
        lock.release()


def verify_import(db_path: str, target_path: str) -> list[str]:
    snapshot = _snapshot(db_path)
    with open_library(target_path, readonly=True) as lib:
        try:
            errors = _verify(snapshot, lib)
        except (ValueError, KeyError, TypeError) as e:
            errors = [f"Malformed import data: {e}"]
    if sha256(db_path) != snapshot[0]:
        errors.append("Legacy file changed during verification")
    return errors
