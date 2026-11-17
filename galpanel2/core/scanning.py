"""Read-only discovery plans, explicit decisions and whole-batch transactions."""
import math
import os
import time
import uuid
from pathlib import Path

from . import names
from .domain import (PlanItem, ScanPlan, CommitResult, AvailabilityResult, PlanConflict,
                     check_cancel, path_key, utc_now)

INSTALLER = ("setup", "install", "unins", "uninst", "uninstall", "crash", "dxsetup", "vcredist", "redist")
ARCHIVES = (".tar.gz", ".7z", ".zip", ".rar", ".tar")
SKIP_MARK = ("patch", "update", "特典", "bonus", "ost", "サウンドトラック", "攻略", "save", "存档", "drama", "ドラマcd", "append", "fd", "fanbox", "ci-en", "体验版", "体験版")


def test_pause():
    pause = os.environ.get("GALPANEL2_TEST_PAUSE_FILE")
    if pause:
        Path(pause).write_text("before commit/publication", encoding="utf-8")
        time.sleep(120)


def _safe_entry(e):
    return not e.is_symlink() and not getattr(e, "is_junction", lambda: False)()


def plan_scan(lib, source_id: int, *, cancel=None, max_depth: int = 6,
              min_size_bytes: int = 20 * 1024 * 1024) -> ScanPlan:
    if type(max_depth) is not int or max_depth < 0 or type(min_size_bytes) is not int or min_size_bytes < 0:
        raise ValueError("Depth and size must be non-negative integers")
    check_cancel(cancel)
    with lib.connection() as c:
        src = c.execute("SELECT path FROM sources WHERE id=?", (source_id,)).fetchone()
        if src is None:
            raise KeyError(source_id)
        existing = {r["path_key"]: dict(r) for r in c.execute("SELECT * FROM copies")}
        ignored_rows = []
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ignored_paths'").fetchone():
            ignored_rows = [{"path_key": r[0], "scope": r[1]}
                            for r in c.execute("SELECT path_key, scope FROM ignored_paths")]
        titles = {}
        for r in c.execute("SELECT id,title FROM works ORDER BY id"):
            titles.setdefault(names.normalized_title(r[1]), []).append(r[0])
    from .tidy import is_ignored
    root = src[0]
    errors = []
    items = []
    ignored_count = 0

    def blocked(path):
        nonlocal ignored_count
        try:
            key = path_key(path)
        except ValueError:
            return False
        if key in existing:
            return False
        if is_ignored(key, ignored_rows):
            ignored_count += 1
            return True
        return False

    def entries(directory):
        check_cancel(cancel)
        try:
            with os.scandir(directory) as it:
                return sorted([e for e in it if _safe_entry(e)], key=lambda e: e.name.casefold())
        except OSError as e:
            errors.append(f"{directory}: {type(e).__name__}: {e}")
            return []

    def noninstaller(e):
        return e.is_file(follow_symlinks=False) and e.name.lower().endswith(".exe") and \
            not any(x in e.name.lower() for x in INSTALLER)

    def size_of(directory):
        total = 0
        pending = [directory]
        while pending:
            check_cancel(cancel)
            d = pending.pop()
            for e in entries(d):
                check_cancel(cancel)
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() not in names.EXCLUDED:
                        pending.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
                    if total > min_size_bytes:
                        return total
        return total

    def make_item(path, kind, stem, exe, size, status=None, reason="", subdirs=()):
        title, dev = names.parse(stem)
        if not names.meaningful(title):
            dev = ""
            fallbacks = ([Path(exe.replace("\\", os.sep)).stem] if exe else []) + list(subdirs)
            title = next((s for s in fallbacks if names.meaningful(s)), stem)
        key = path_key(path)
        old = existing.get(key)
        candidates = tuple(titles.get(names.normalized_title(title), ()))
        actual = "known" if old else status or ("same_name" if candidates else "new")
        items.append(PlanItem(key, path, kind, title, dev, exe, size, actual,
                              candidates if actual == "same_name" else (), old["id"] if old else None,
                              "" if old else reason))

    def walk(directory, depth):
        check_cancel(cancel)
        if blocked(directory):
            return
        es = entries(directory)
        try:
            dirs = [e for e in es if e.is_dir(follow_symlinks=False) and e.name.lower() not in names.EXCLUDED]
            exes = [e for e in es if noninstaller(e)]
            if not exes:
                for d in dirs:
                    if d.name.lower() in names.GENERIC:
                        exes.extend(e for e in entries(d.path) if noninstaller(e))
            if exes:
                def rank(e):
                    rel = os.path.relpath(e.path, directory)
                    return (len(Path(rel).parts), not any(s in e.name.lower() for s in ("game", "start", "launcher", "启动")),
                            -e.stat(follow_symlinks=False).st_size, e.name.casefold())
                exe = os.path.relpath(min(exes, key=rank).path, directory).replace(os.sep, "\\")
                size = size_of(directory)
                subdirs = [d.name for d in dirs if d.name.lower() not in names.GENERIC]
                make_item(directory, "folder", Path(directory).name, exe, size,
                          "skipped" if size < min_size_bytes else None,
                          "too_small" if size < min_size_bytes else "", subdirs)
                return
            for e in es:
                check_cancel(cancel)
                if e.is_file(follow_symlinks=False):
                    ext = next((ext for ext in ARCHIVES if e.name.lower().endswith(ext)), None)
                    if ext:
                        if blocked(e.path):
                            continue
                        stem = e.name[:-len(ext)]
                        skip = any(s in stem.lower() for s in SKIP_MARK)
                        make_item(e.path, "archive", stem, None, e.stat(follow_symlinks=False).st_size,
                                  "skipped" if skip else None, "skip_mark" if skip else "")
            if depth < max_depth:
                for d in dirs:
                    walk(d.path, depth + 1)
        except OSError as e:
            errors.append(f"{directory}: {type(e).__name__}: {e}")

    walk(root, 0)
    found = {i.key for i in items}
    for key, r in existing.items():
        check_cancel(cancel)
        if r["source_id"] == source_id and key not in found:
            try:
                absent = not os.path.exists(r["path"])
            except OSError as e:
                errors.append(f"{r['path']}: {e}")
                absent = False
            if absent:
                items.append(PlanItem(key, r["path"], r["kind"], "", "", r["exe_relpath"],
                                      r["size_bytes"], "missing", (), r["id"], "not_found"))
            elif is_ignored(key, ignored_rows):
                items.append(PlanItem(key, r["path"], r["kind"], "", "", r["exe_relpath"],
                                      r["size_bytes"], "known", (), r["id"], ""))
    check_cancel(cancel)
    return ScanPlan(source_id, root, utc_now(), tuple(sorted(items, key=lambda i: i.key)),
                    tuple(errors), ignored_count)


def commit_scan(lib, plan: ScanPlan, decisions: dict[str, str | int] | None = None) -> CommitResult:
    decisions = decisions or {}
    keys = {i.key for i in plan.items}
    if len(keys) != len(plan.items) or set(decisions) - keys:
        raise ValueError("Duplicate plan keys or decision absent from plan")
    actions = {}
    for it in plan.items:
        if it.key != path_key(it.path) or it.kind not in ("folder", "archive"):
            raise ValueError("Invalid plan path or kind")
        if it.status not in ("new", "same_name", "known", "missing", "skipped"):
            raise ValueError("Invalid plan status")
        action = decisions.get(it.key, "add" if it.status == "new" else "skip")
        if it.key in decisions:
            valid = it.status in ("new", "same_name") and (type(action) is int or action == "skip" or
                    (action == "add" and it.status == "new") or (action == "new" and it.status == "same_name"))
            if not valid:
                raise ValueError("Decision does not match plan status")
        if it.status in ("new", "same_name") and action != "skip" and not it.title.strip():
            raise ValueError("Title must not be blank")
        actions[it.key] = action
    added = attached = known = missing = 0
    now = utc_now()
    with lib.transaction() as c:
        src = c.execute("SELECT path_key FROM sources WHERE id=?", (plan.source_id,)).fetchone()
        if not src or src[0] != path_key(plan.root):
            raise PlanConflict("Source removed or changed")
        # Validation is inside the same transaction as all writes.
        for it in plan.items:
            old = c.execute("SELECT id,path_key,source_id FROM copies WHERE path_key=?", (it.key,)).fetchone()
            action = actions[it.key]
            if it.status in ("new", "same_name"):
                if old:
                    raise PlanConflict("Path was added after planning")
                if type(action) is int and not c.execute("SELECT 1 FROM works WHERE id=?", (action,)).fetchone():
                    raise PlanConflict("Target work no longer exists")
            elif it.status in ("known", "missing"):
                if not old or old[0] != it.copy_id or (it.status == "missing" and old[2] != plan.source_id):
                    raise PlanConflict("Registered copy changed during review")
        new_count = sum(i.status in ("new", "same_name") and actions[i.key] != "skip" for i in plan.items)
        pause_at = max(1, math.ceil(new_count / 2))
        for it in plan.items:
            action = actions[it.key]
            if it.status in ("new", "same_name") and action != "skip":
                if type(action) is int:
                    wid = action
                    attached += 1
                else:
                    wid = c.execute("INSERT INTO works(uuid,title,developer,added_at,updated_at) VALUES(?,?,?,?,?)",
                                    (str(uuid.uuid4()), it.title.strip(), it.developer, now, now)).lastrowid
                    added += 1
                c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,size_bytes,"
                          "availability,checked_at,origin,added_at) VALUES(?,?,?,?,?,?,?,'ok',?,'scan',?)",
                          (wid, plan.source_id, it.kind, it.path, it.key, it.exe_relpath, it.size_bytes, now, now))
                if added + attached == pause_at:
                    test_pause()
            elif it.status == "known":
                c.execute("UPDATE copies SET availability='ok',checked_at=?,size_bytes=?,exe_relpath=? WHERE id=?",
                          (now, it.size_bytes, it.exe_relpath, it.copy_id))
                known += 1
            elif it.status == "missing":
                c.execute("UPDATE copies SET availability='missing',checked_at=? WHERE id=?", (now, it.copy_id))
                missing += 1
        run_id = c.execute("INSERT INTO scan_runs(source_id,planned_at,committed_at,added_works,attached_copies,"
                           "marked_known,marked_missing) VALUES(?,?,?,?,?,?,?)",
                           (plan.source_id, plan.planned_at, now, added, attached, known, missing)).lastrowid
        c.execute("UPDATE sources SET last_scan_at=? WHERE id=?", (now, plan.source_id))
    return CommitResult(run_id, added, attached, known, missing)


def check_work_copies(lib, work_id: int) -> dict[int, str]:
    """Quietly stat one work's copies and persist only states that changed.

    Unlike :func:`check_availability`, this never compares the whole copies
    table, so a concurrent change to another work cannot turn this into a
    revision/PlanConflict failure.  The caller owns scheduling; read-only
    libraries are only inspected, never written.
    """
    with lib.connection() as c:
        rows = [tuple(r) for r in c.execute(
            "SELECT id,kind,path,availability FROM copies WHERE work_id=? ORDER BY id", (work_id,))]
    states = {}
    for cid, kind, path, _old in rows:
        try:
            exists = os.path.isdir(path) if kind == "folder" else os.path.isfile(path)
            state = "ok" if exists else "missing"
        except OSError:
            state = "error"
        states[cid] = state
    changed = {cid: state for cid, kind, path, old in rows if states[cid] != old}
    if changed and not lib.readonly:
        now = utc_now()
        # One small transaction, only this work's rows.  The path guard keeps a
        # copy moved by another task from being marked against its old path.
        with lib.transaction() as c:
            for cid, kind, path, _old in rows:
                state = changed.get(cid)
                if state is not None:
                    c.execute("UPDATE copies SET availability=?,checked_at=? "
                              "WHERE id=? AND work_id=? AND path=?", (state, now, cid, work_id, path))
    return states


def check_availability(lib, work_ids: list[int] | None = None, *, cancel=None) -> AvailabilityResult:
    check_cancel(cancel)
    with lib.connection() as c:
        rows = [tuple(r) for r in c.execute("SELECT id,work_id,kind,path,path_key FROM copies ORDER BY id")
                if work_ids is None or r[1] in work_ids]
    results = []
    counts = {"ok": 0, "missing": 0, "error": 0}
    for r in rows:
        check_cancel(cancel)
        try:
            exists = os.path.isdir(r[3]) if r[2] == "folder" else os.path.isfile(r[3])
            state = "ok" if exists else "missing"
        except OSError:
            state = "error"
        results.append((state, r[0]))
        counts[state] += 1
    check_cancel(cancel)
    with lib.transaction() as c:
        current = [tuple(r) for r in c.execute("SELECT id,work_id,kind,path,path_key FROM copies ORDER BY id")
                   if work_ids is None or r[1] in work_ids]
        if current != rows:
            raise PlanConflict("Copies changed while checking files")
        now = utc_now()
        for state, cid in results:
            check_cancel(cancel)
            c.execute("UPDATE copies SET availability=?,checked_at=? WHERE id=?", (state, now, cid))
        check_cancel(cancel)
    return AvailabilityResult(len(rows), **counts)
