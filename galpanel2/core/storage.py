"""Move game files to a cold archive, restore them, or delete them permanently.

Only the Python standard library is used here.  The optional
``copy_storage`` table is created on demand (exactly like ``shelf.py``), so
``PRAGMA user_version`` stays at 6 and a read-only library can still be read.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import ntpath
import os
import re
import shutil
import stat
import sys
import uuid

from .domain import path_key, utc_now

LOCATIONS = ("常用", "冷藏", "已删除")
DEFAULT_COLD_ROOT = "D:\\ColdStorage\\Games"

GIB = 1 << 30
_CHUNK = 8 << 20

_CREATE_SQL = """CREATE TABLE IF NOT EXISTS copy_storage(
  copy_id INTEGER PRIMARY KEY REFERENCES copies(id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK(state IN ('冷藏','已删除')),
  home_path TEXT,
  updated_at TEXT NOT NULL
)"""


class StorageError(Exception):
    """A plan or an operation was refused; the message is shown to the user."""


class Cancelled(StorageError):
    """The user cancelled the operation."""


@dataclass(frozen=True)
class CopyStorage:
    copy_id: int
    work_id: int
    kind: str
    path: str
    location: str
    home_path: str | None
    source_id: int | None = None


@dataclass(frozen=True)
class Move:
    copy_id: int
    src: str
    dst: str
    size_bytes: int
    action: str = "cold"
    source_id: int | None = None
    # Extra same-volume files belonging to the same multi-volume archive
    # (``extra.src`` → ``extra.dst``).  The library only records ``dst``.
    extras: tuple[tuple[str, str], ...] = ()
    # True when ``dst`` is inside ``src``: the copy already sits at its title
    # folder and has to be renamed to a sibling temp name first.
    nested: bool = False
    # Non-fatal problems found while planning (for example an unreadable size).
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrganizePlan:
    """Read-only result of :func:`plan_organize`."""

    moves: tuple[Move, ...]
    skipped: tuple[tuple[int, str, str], ...]
    already_ok: int


@dataclass(frozen=True)
class OrganizeResult:
    """What actually happened during organize/undo."""

    moved: int
    failed: tuple[tuple[int, str, str], ...]
    skipped: tuple[tuple[int, str, str], ...]
    removed_empty_dirs: tuple[str, ...]
    leftover_dirs: tuple[str, ...]
    log_path: str | None
    integrity_check: tuple[str, ...]
    backup_path: str | None = None
    batch: str | None = None


# ------------------------------------------------------------------ helpers
def _table_exists(c) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='copy_storage'"
    ).fetchone() is not None


def _segments(path: str) -> list[str]:
    if not isinstance(path, str):
        return []
    return [part for part in re.split(r"[\\/]", path) if part]


def _key(path: str) -> str:
    """A comparable path key.

    Production paths are Windows paths, so ``domain.path_key`` is the rule.
    On a non-Windows development machine the unit tests use real POSIX temp
    directories; those cannot be expressed as a drive path, so fall back to a
    local absolute key there.  Windows behaviour is unchanged.
    """
    try:
        return path_key(path)
    except (TypeError, ValueError):
        if os.name == "nt":
            raise
        return os.path.normcase(os.path.abspath(str(path)))


def _safe_key(path: str) -> str:
    try:
        return _key(path)
    except (TypeError, ValueError) as error:
        raise StorageError(f"路径不合法：{path}") from error


def _is_parent(parent: str, child: str) -> bool:
    """True when ``parent`` is a strict ancestor of ``child`` (both are keys)."""
    if parent == child:
        return False
    sep = "\\" if os.name == "nt" else "/"
    return child.startswith(parent + sep)


def _is_drive_root(path: str) -> bool:
    drive, tail = ntpath.splitdrive(path)
    if drive and not tail.strip("\\/"):
        return True
    try:
        absolute = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    return absolute == os.path.dirname(absolute)


def _existing_parent(path: str) -> str | None:
    parent = os.path.dirname(path)
    while parent:
        if os.path.isdir(parent):
            return parent
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            break
        parent = next_parent
    return None


def _drive(path: str) -> str:
    drive = ntpath.splitdrive(path)[0]
    if drive:
        return drive.lower()
    split = os.path.splitdrive(os.path.abspath(path))[0]
    return split.lower() if split else "\\"


def _relative_parts(root: str, path: str) -> list[str] | None:
    """Path segments of ``path`` relative to ``root`` (lexical, case-aware)."""
    root_parts = _segments(root)
    path_parts = _segments(path)
    if len(path_parts) <= len(root_parts):
        return None
    for left, right in zip(root_parts, path_parts):
        if os.path.normcase(left) != os.path.normcase(right):
            return None
    return path_parts[len(root_parts):]


def _join_segments(root: str, parts: list[str]) -> str:
    if os.name != "nt" and not re.match(r"^[A-Za-z]:", root) \
            and not root.startswith("\\\\"):
        result = root
        for part in parts:
            result = os.path.join(result, part)
        return result
    result = root
    for part in parts:
        result = ntpath.join(result, part)
    if re.match(r"^[A-Za-z]:", result) or result.startswith("\\\\"):
        result = result.replace("/", "\\")
    return result


def _same_volume(src: str, dst: str) -> bool:
    """True when a move between the two paths can be a plain rename."""
    src_drive = ntpath.splitdrive(src)[0]
    dst_drive = ntpath.splitdrive(dst)[0]
    if src_drive or dst_drive:
        return _drive(src) == _drive(dst)
    try:
        parent = _existing_parent(dst) or os.path.dirname(os.path.abspath(dst))
        return os.stat(src).st_dev == os.stat(parent).st_dev
    except OSError:
        return _drive(src) == _drive(dst)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().rstrip(". ")
    return cleaned or "未命名"


def _tree_size(path: str) -> int:
    try:
        if os.path.isdir(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                for name in files:
                    total += os.path.getsize(os.path.join(root, name))
            return total
        return os.path.getsize(path)
    except OSError as error:
        raise StorageError(f"无法读取大小：{path}（{error}）") from error


def _rmtree(path: str) -> None:
    def retry(function, target, error):
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda function, target, error: retry(function, target, error))
    else:  # pragma: no cover - Python 3.11 compatibility
        shutil.rmtree(path, onerror=lambda function, target, info: retry(function, target, info[1]))


def _remove_path(path: str) -> None:
    """Delete a file/archive or a whole folder, including read-only files."""
    if os.path.isdir(path) and not os.path.islink(path):
        _rmtree(path)
        return
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def _remove_part(part: str) -> None:
    if not os.path.lexists(part):
        return
    try:
        _remove_path(part)
    except OSError:
        # A half-written part file must not stop the original operation from
        # reporting its real error; the next attempt removes it anyway.
        pass


def _log(log_path, action, copy_id, work_id, src, dst, size_bytes, result, error=None,
         *, extras=None, extra=None) -> None:
    if log_path is None:
        return
    record = {"time": utc_now(), "action": action, "copy_id": copy_id, "work_id": work_id,
              "src": src, "dst": dst, "bytes": int(size_bytes), "result": result, "error": error}
    if extras:
        record["extras"] = [[src_path, dst_path] for src_path, dst_path in extras]
    if extra:
        record.update(extra)
    try:
        parent = os.path.dirname(os.path.abspath(log_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ reads
def list_storage(lib) -> dict[int, CopyStorage]:
    """Every copy in the library, with its cold/deleted state.

    Never creates the optional table and never writes: a read-only library is
    fine.  Copies without a row are located on the common drive.
    """
    with lib.connection() as c:
        rows = c.execute("SELECT id,work_id,kind,path,source_id FROM copies ORDER BY id").fetchall()
        extra = {}
        if _table_exists(c):
            extra = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT copy_id,state,home_path FROM copy_storage")}
    out: dict[int, CopyStorage] = {}
    for row in rows:
        state, home = extra.get(row[0], ("常用", None))
        if state not in ("冷藏", "已删除"):
            state, home = "常用", None
        out[row[0]] = CopyStorage(int(row[0]), int(row[1]), str(row[2]), str(row[3]),
                                  state, home if state == "冷藏" else None,
                                  int(row[4]) if row[4] is not None else None)
    return out


def work_locations(lib) -> dict[int, str]:
    """Location of every work that has at least one copy."""
    grouped: dict[int, list[str]] = {}
    for item in list_storage(lib).values():
        grouped.setdefault(item.work_id, []).append(item.location)
    out: dict[int, str] = {}
    for work_id, locations in grouped.items():
        if all(location == "已删除" for location in locations):
            out[work_id] = "已删除"
        elif "冷藏" in locations:
            out[work_id] = "冷藏"
        else:
            out[work_id] = "常用"
    return out


def cold_target(path: str, cold_root: str) -> str:
    """Map ``...\\Games\\分类\\品牌\\标题`` below ``cold_root``."""
    if not isinstance(path, str) or not isinstance(cold_root, str) or not cold_root:
        raise StorageError("冷藏位置或游戏路径不合法")
    parts = _segments(path)
    index = next((i for i, part in enumerate(parts) if part.lower() == "games"), None)
    if index is None:
        raise StorageError(f"路径里没有 Games 段：{path}")
    tail = parts[index + 1:]
    if not tail:
        raise StorageError(f"Games 后面没有分类和标题：{path}")
    # Production is Windows and always uses ntpath.  On a non-Windows machine
    # the unit tests use real POSIX temp directories, where ntpath.join would
    # create backslashes that the OS treats as ordinary filename characters.
    if os.name != "nt" and not re.match(r"^[A-Za-z]:", cold_root) \
            and not cold_root.startswith("\\\\"):
        result = cold_root
        for part in tail:
            result = os.path.join(result, part)
        return result
    result = cold_root
    for part in tail:
        result = ntpath.join(result, part)
    if re.match(r"^[A-Za-z]:", result) or result.startswith("\\\\"):
        result = result.replace("/", "\\")
    return result


# ------------------------------------------------------------------ plan
def _move_target(item, target_root: str, *, work_title: str, category: str | None,
                 sources: dict[int, str], all_source_paths: list[str]) -> str:
    """Translate a copy below the target root, keeping its relative layout."""
    root = sources.get(item.source_id) if item.source_id is not None else None
    if root is None:
        key = _key(item.path)
        candidates = []
        for source_path in all_source_paths:
            try:
                source_key = _key(source_path)
            except (TypeError, ValueError):
                continue
            if _is_parent(source_key, key):
                candidates.append(source_path)
        root = max(candidates, key=len) if candidates else None
    if root is not None:
        parts = _relative_parts(root, item.path)
        if parts:
            return _join_segments(target_root, parts)
    if item.kind == "archive":
        suffix = os.path.splitext(item.path)[1]
        name = _safe_name(work_title) + suffix
    else:
        name = _safe_name(work_title)
    parts = ["Games"]
    if category:
        parts.append(_safe_name(category))
    parts.append(name)
    return _join_segments(target_root, parts)


def plan_moves(lib, copy_ids, action: str, cold_root: str | None = None, *,
               target_source_id: int | None = None) -> list[Move]:
    """Validate a batch of cold/restore/move operations without changing anything."""
    if action not in ("cold", "restore", "move"):
        raise ValueError(f"unknown action: {action!r}")
    if action == "cold" and cold_root is None:
        cold_root = DEFAULT_COLD_ROOT
    if action == "move" and target_source_id is None and isinstance(cold_root, int):
        target_source_id, cold_root = cold_root, None
    if action == "move" and target_source_id is None:
        raise ValueError("move needs target_source_id")

    copies = list_storage(lib)
    with lib.connection() as c:
        source_rows = {int(r[0]): str(r[1]) for r in c.execute("SELECT id,path FROM sources")}
        work_titles = {int(r[0]): str(r[1]) for r in c.execute("SELECT id,title FROM works")}
        work_of_copy = {int(r[0]): int(r[1]) for r in c.execute("SELECT id,work_id FROM copies")}
        shelves = {}
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_shelf'"
                     ).fetchone() is not None:
            shelves = {int(r[0]): r[1] for r in c.execute(
                "SELECT work_id,category FROM work_shelf")}
    if action == "move":
        if target_source_id not in source_rows:
            raise StorageError(f"目标游戏目录不存在：{target_source_id}")
        target_root = source_rows[target_source_id]
    else:
        target_root = cold_root

    occupied: dict[str, int] = {}
    for item in copies.values():
        try:
            occupied[_key(item.path)] = item.copy_id
        except (TypeError, ValueError):
            continue

    problems: list[str] = []
    moves: list[Move] = []
    seen_dst: dict[str, int] = {}
    for copy_id in copy_ids:
        item = copies.get(copy_id)
        if item is None:
            problems.append(f"副本 {copy_id} 不存在")
            continue
        if action == "cold":
            if item.location != "常用":
                problems.append(f"{item.path} 不在常用盘，无法冷藏")
                continue
            try:
                dst = cold_target(item.path, cold_root)
            except StorageError as error:
                problems.append(str(error))
                continue
        elif action == "restore":
            if item.location != "冷藏":
                problems.append(f"{item.path} 不在冷藏盘，无法搬回")
                continue
            if not item.home_path:
                problems.append(f"{item.path} 没有记录原来的位置，无法搬回")
                continue
            dst = item.home_path
        else:
            if item.location != "常用":
                problems.append(f"{item.path} 不在常用盘，无法移到其他游戏目录")
                continue
            work_id = work_of_copy.get(copy_id)
            dst = _move_target(item, target_root,
                               work_title=work_titles.get(work_id, os.path.basename(item.path)),
                               category=shelves.get(work_id), sources=source_rows,
                               all_source_paths=list(source_rows.values()))
        src = item.path
        if not os.path.lexists(src):
            problems.append(f"源不存在：{src}")
            continue
        try:
            src_key = _key(src)
        except (TypeError, ValueError):
            problems.append(f"源路径不合法：{src}")
            continue
        try:
            dst_key = _key(dst)
        except (TypeError, ValueError):
            problems.append(f"目标路径不合法：{dst}")
            continue
        if src_key == dst_key or _is_parent(src_key, dst_key) or _is_parent(dst_key, src_key):
            problems.append(f"目标和源不能互相包含：{dst}")
            continue
        if os.path.lexists(dst):
            problems.append(f"目标已存在：{dst}")
            continue
        if dst_key in seen_dst:
            problems.append(f"多个副本指向同一个目标：{dst}")
            continue
        seen_dst[dst_key] = copy_id
        owner = occupied.get(dst_key)
        if owner is not None and owner != copy_id:
            problems.append(f"目标已被另一个副本占用：{dst}")
            continue
        try:
            size = _tree_size(src)
        except StorageError as error:
            problems.append(str(error))
            continue
        source_id = target_source_id if action == "move" else None
        moves.append(Move(int(copy_id), src, dst, int(size), action, source_id))

    # Free space is checked per target drive, after all structural problems.
    # A same-volume move is a rename and needs no room for a second copy.
    drives: dict[str, list[Move]] = {}
    for move in moves:
        if move.action == "move" and _same_volume(move.src, move.dst):
            continue
        drives.setdefault(_drive(move.dst), []).append(move)
    for drive, batch in drives.items():
        needed = sum(move.size_bytes for move in batch)
        parent = _existing_parent(batch[0].dst)
        if parent is None:
            problems.append(f"目标盘 {drive} 没有可用的上级目录：{batch[0].dst}")
            continue
        try:
            free = shutil.disk_usage(parent).free
        except OSError as error:
            problems.append(f"无法检查目标盘 {drive} 的剩余空间：{error}")
            continue
        if free < needed + GIB:
            problems.append(f"目标盘 {drive} 空间不足：需要 {(needed + GIB) / GIB:.1f} GB"
                            f"（含 1 GB 余量），剩 {free / GIB:.1f} GB")

    if problems:
        raise StorageError("\n".join(problems))
    return moves

# ------------------------------------------------------------------ execute
def _copy_to_part(move: Move, part: str, kind: str, done_bytes: int, total: int,
                  cancel, progress) -> int:
    src = move.src
    if kind == "folder" or os.path.isdir(src):
        os.makedirs(part, exist_ok=True)
        for root, dirs, files in os.walk(src):
            relative = os.path.relpath(root, src)
            target_root = part if relative == "." else os.path.join(part, relative)
            for name in dirs:
                os.makedirs(os.path.join(target_root, name), exist_ok=True)
            for name in files:
                source_file = os.path.join(root, name)
                target_file = os.path.join(target_root, name)
                shutil.copy2(source_file, target_file)
                try:
                    size = os.path.getsize(source_file)
                except OSError:
                    size = 0
                done_bytes += size
                if progress is not None:
                    progress(done_bytes, total, src)
                if cancel is not None and cancel():
                    raise Cancelled("已取消搬移")
        return done_bytes

    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(src, "rb") as source, open(part, "wb") as target:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            target.write(chunk)
            done_bytes += len(chunk)
            if progress is not None:
                progress(done_bytes, total, src)
            if cancel is not None and cancel():
                raise Cancelled("已取消搬移")
    shutil.copystat(src, part)
    return done_bytes


def _verify_part(move: Move, part: str, kind: str) -> None:
    src = move.src
    if kind == "folder" or os.path.isdir(src):
        expected_files: dict[str, int] = {}
        expected_dirs: set[str] = set()
        for root, dirs, files in os.walk(src):
            relative = os.path.relpath(root, src)
            if relative != ".":
                expected_dirs.add(relative)
            for name in dirs:
                expected_dirs.add(os.path.join(relative, name) if relative != "." else name)
            for name in files:
                full = os.path.join(root, name)
                key = os.path.join(relative, name) if relative != "." else name
                expected_files[key] = os.path.getsize(full)
        got_files: dict[str, int] = {}
        got_dirs: set[str] = set()
        for root, dirs, files in os.walk(part):
            relative = os.path.relpath(root, part)
            if relative != ".":
                got_dirs.add(relative)
            for name in dirs:
                got_dirs.add(os.path.join(relative, name) if relative != "." else name)
            for name in files:
                full = os.path.join(root, name)
                key = os.path.join(relative, name) if relative != "." else name
                got_files[key] = os.path.getsize(full)
        if expected_files != got_files or not expected_dirs <= got_dirs:
            raise StorageError(f"复制后的文件与源不一致：{src}")
        return
    if os.path.getsize(src) != os.path.getsize(part):
        raise StorageError(f"复制后的压缩包大小与源不一致：{src}")


def _commit(lib, move: Move, action: str) -> None:
    stamp = utc_now()
    with lib.transaction() as c:
        if action == "move" and move.source_id is not None:
            c.execute("UPDATE copies SET path=?,path_key=?,source_id=?,availability='ok',"
                      "checked_at=? WHERE id=?",
                      (move.dst, _key(move.dst), move.source_id, stamp, move.copy_id))
        else:
            c.execute("UPDATE copies SET path=?,path_key=?,availability='ok',checked_at=? "
                      "WHERE id=?", (move.dst, _key(move.dst), stamp, move.copy_id))
        if action == "cold":
            c.execute(_CREATE_SQL)
            c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(copy_id) DO UPDATE SET state=excluded.state, "
                      "home_path=excluded.home_path, updated_at=excluded.updated_at",
                      (move.copy_id, "冷藏", move.src, stamp))
        elif _table_exists(c):
            c.execute("DELETE FROM copy_storage WHERE copy_id=?", (move.copy_id,))


def run_moves(lib, moves, *, cancel=None, progress=None, log_path=None,
              left: list | None = None) -> list[int]:
    """Execute planned moves in order; completed copy ids are returned.

    When ``left`` is a list, every source path that could not be deleted after
    a successful commit is appended to it (the log still says ``src_left``).
    A same-volume ``move`` is a direct rename; everything else uses copy,
    verify, commit, then delete.
    """
    if lib.readonly:
        raise StorageError("这个库是只读打开的，不能搬移游戏文件")
    total = sum(int(move.size_bytes) for move in moves)
    done_bytes = 0
    completed: list[int] = []
    for move in moves:
        if cancel is not None and cancel():
            raise Cancelled("已取消搬移")
        part = move.dst + ".gp2part"
        item = list_storage(lib).get(move.copy_id)
        action = move.action if move.action in ("cold", "restore", "move") else "cold"
        if action == "cold" and item is not None and item.location == "冷藏":
            action = "restore"  # compatibility with manually built Move objects
        work_id = item.work_id if item is not None else 0
        replaced = False
        renamed = False
        committed = False

        def undo_install():
            """Restore/remove dst when the database was not committed."""
            if committed:
                return None
            if renamed:
                if not os.path.lexists(move.dst) or os.path.lexists(move.src):
                    return None
                try:
                    os.makedirs(os.path.dirname(move.src), exist_ok=True)
                    os.replace(move.dst, move.src)
                    return None
                except OSError as cleanup_error:
                    return cleanup_error
            if not replaced:
                return None
            if not os.path.lexists(move.src) or not os.path.lexists(move.dst):
                return None
            try:
                _remove_path(move.dst)
                return None
            except OSError as cleanup_error:
                return cleanup_error

        try:
            if item is None:
                raise StorageError(f"副本 {move.copy_id} 不存在")
            if action in ("cold", "move") and item.location != "常用":
                raise StorageError(f"{move.src} 不在常用盘，不能搬移")
            if action == "restore" and item.location != "冷藏":
                raise StorageError(f"{move.src} 不在冷藏盘，不能搬回")
            if action == "move" and _same_volume(move.src, move.dst):
                # Same volume: a rename is atomic and does not need space for a
                # second copy.  The DB commit still follows before the source is
                # considered gone; undo_install reverses the rename on failure.
                if os.path.lexists(move.dst):
                    raise StorageError(f"目标已存在：{move.dst}")
                os.makedirs(os.path.dirname(os.path.abspath(move.dst)), exist_ok=True)
                os.replace(move.src, move.dst)
                renamed = True
                done_bytes += int(move.size_bytes)
                if progress is not None:
                    progress(done_bytes, total, move.src)
            else:
                _remove_part(part)
                done_bytes = _copy_to_part(move, part, item.kind, done_bytes, total, cancel, progress)
                _verify_part(move, part, item.kind)
                if os.path.lexists(move.dst):
                    raise StorageError(f"目标已存在：{move.dst}")
                os.replace(part, move.dst)
                replaced = True
            _commit(lib, move, action)
            committed = True
            result, error_text = "ok", None
            if not renamed:
                try:
                    _remove_path(move.src)
                except OSError as error:
                    result, error_text = "src_left", str(error)
                    if left is not None:
                        left.append(move.src)
            _log(log_path, action, move.copy_id, work_id, move.src, move.dst,
                 move.size_bytes, result, error_text)
            completed.append(move.copy_id)
        except Cancelled as error:
            cleanup_error = undo_install()
            _remove_part(part)
            message = str(error)
            if (renamed or replaced) and not committed and cleanup_error is not None:
                message += f"；库没有提交，目标未能清理：{cleanup_error}"
            _log(log_path, action, move.copy_id, work_id, move.src, move.dst,
                 move.size_bytes, "cancelled", message)
            raise
        except StorageError as error:
            cleanup_error = undo_install()
            _remove_part(part)
            message = str(error)
            if (renamed or replaced) and not committed:
                if cleanup_error is None:
                    message += ("；库没有提交，已把文件移回原位置" if renamed
                                else "；库没有提交，已删除刚复制好的目标")
                else:
                    message += f"；库没有提交，目标未能清理：{cleanup_error}"
            _log(log_path, action, move.copy_id, work_id, move.src, move.dst,
                 move.size_bytes, "failed", message)
            if message != str(error):
                raise StorageError(message) from error
            raise
        except Exception as error:  # noqa: BLE001 - reported to the UI as StorageError
            cleanup_error = undo_install()
            _remove_part(part)
            message = f"搬移失败：{move.src} → {move.dst}：{error}"
            if (renamed or replaced) and not committed:
                if cleanup_error is None:
                    message += ("；库没有提交，已把文件移回原位置" if renamed
                                else "；库没有提交，已删除刚复制好的目标")
                else:
                    message += f"；库没有提交，目标未能清理：{cleanup_error}"
            _log(log_path, action, move.copy_id, work_id, move.src, move.dst,
                 move.size_bytes, "failed", message)
            raise StorageError(message) from error
    return completed

# ------------------------------------------------------------------ organize
_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?=\.|$)", re.I)
# A barely meaningful name such as ``B014``, ``1346044`` or ``D755-1``.
_CODE_NAME = re.compile(r"^[A-Za-z]{0,3}[-_]?\d+([-_.]\d+)*$")


def _windows_safe_name(name: str) -> str:
    """``_safe_name`` plus the Windows device-name rule (CON → CON_)."""
    text = _safe_name(name)
    match = _WINDOWS_RESERVED.match(text)
    if match:
        text = text[:match.end()] + "_" + text[match.end():]
    return text


def _organize_segment(text: str, limit: int = 80) -> str:
    """One path component: illegal characters, reserved names, 80 chars."""
    text = _windows_safe_name(text)[:limit].rstrip(" .")
    return text or "未命名"[:limit]


def _path_basename(path: str) -> str:
    parts = _segments(path)
    return parts[-1] if parts else "未命名"


def _source_roots(lib) -> dict[int, str]:
    with lib.connection() as c:
        return {int(row[0]): str(row[1]) for row in c.execute("SELECT id,path FROM sources")}


def _resolve_source_root(item, sources: dict[int, str]) -> tuple[str | None, int | None]:
    """The configured source that owns a copy (explicit id, else longest parent)."""
    if item.source_id is not None and item.source_id in sources:
        return sources[item.source_id], item.source_id
    try:
        key = _key(item.path)
    except (TypeError, ValueError):
        return None, None
    best: tuple[int, str, int] | None = None
    for source_id, root in sources.items():
        try:
            root_key = _key(root)
        except (TypeError, ValueError):
            continue
        if _is_parent(root_key, key):
            candidate = (len(root_key), root, source_id)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None:
        return None, None
    return best[1], best[2]


def _volume_patterns(name: str) -> list[re.Pattern] | None:
    """Sibling-name patterns for the multi-volume archive family of ``name``."""
    part = re.fullmatch(r"(.+)\.part\d+\.rar", name, re.I)
    if part:
        base = part.group(1)
        return [re.compile(r"^" + re.escape(base) + r"\.part\d+\.rar$", re.I)]
    numeric = re.fullmatch(r"(.+)\.(7z|zip)\.\d{3}", name, re.I)
    if numeric:
        base, suffix = numeric.group(1), numeric.group(2)
        return [re.compile(r"^" + re.escape(base) + r"\." + suffix + r"\.\d{3}$", re.I)]
    zed = re.fullmatch(r"(.+)\.z\d{2,}", name, re.I)
    if zed:
        base = zed.group(1)
        return [re.compile(r"^" + re.escape(base) + r"\.zip$", re.I),
                re.compile(r"^" + re.escape(base) + r"\.z\d{2,}$", re.I)]
    zipped = re.fullmatch(r"(.+)\.zip", name, re.I)
    if zipped:
        base = zipped.group(1)
        return [re.compile(r"^" + re.escape(base) + r"\.z\d{2,}$", re.I)]
    return None


def _volume_siblings(item) -> tuple[str, ...]:
    """Existing sibling files that belong to the same multi-volume archive."""
    if item.kind != "archive" or not os.path.isfile(item.path):
        return ()
    parent = os.path.dirname(item.path)
    name = os.path.basename(item.path)
    patterns = _volume_patterns(name)
    if not patterns:
        return ()
    try:
        names = os.listdir(parent)
    except OSError:
        return ()
    found = []
    for other in names:
        if other == name or not any(pattern.fullmatch(other) for pattern in patterns):
            continue
        path = os.path.join(parent, other)
        if os.path.isfile(path):
            found.append(path)
    return tuple(sorted(found))


def _numbered_name(name: str, number: int, kind: str) -> str:
    if number == 1:
        return name
    if kind == "archive":
        stem, suffix = os.path.splitext(name)
        return f"{stem} ({number}){suffix}"
    return f"{name} ({number})"


def _code_like_name(name: str, kind: str) -> bool:
    """True for a name that is only a number (archives ignore the suffix)."""
    stem = os.path.splitext(name)[0] if kind == "archive" else name
    return _CODE_NAME.fullmatch(stem) is not None


def _organize_order(moves: list[Move]) -> tuple[Move, ...]:
    """Keep plan order, but put self-nesting copies before their siblings.

    A copy that currently sits at ``<title>`` must vacate that path before
    another copy is renamed into ``<title>/<name>``.  Moves are grouped by the
    destination folder so unrelated works keep their original order.
    """
    groups: dict[str, list[Move]] = {}
    order: list[str] = []
    for move in moves:
        try:
            key = _key(os.path.dirname(os.path.abspath(move.dst)))
        except (TypeError, ValueError):
            key = move.dst
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(move)
    ordered: list[Move] = []
    for key in order:
        group = groups[key]
        ordered.extend(move for move in group if move.nested)
        ordered.extend(move for move in group if not move.nested)
    return tuple(ordered)


def _organize_target(root: str, category: str, brand: str, title: str, *,
                     name: str | None, title_limit: int = 80) -> str:
    parts = ["Games", _organize_segment(category), _organize_segment(brand),
             _organize_segment(title, title_limit)]
    if name is not None:
        parts.append(_windows_safe_name(name))
    return _join_segments(root, parts)


def _fits(path: str) -> bool:
    return len(path) <= 240


def plan_organize(lib, copy_ids=None, *, source_ids=None) -> OrganizePlan:
    """Plan moving copies into ``<game root>\\Games\\分类\\品牌\\标题``.

    Read-only: no table is created, no file is touched and no directory is
    inspected beyond existence/top-level naming.
    """
    copies = list_storage(lib)
    sources = _source_roots(lib)
    with lib.connection() as c:
        titles = {int(r[0]): str(r[1]) for r in c.execute("SELECT id,title FROM works")}
        shelves = {}
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_shelf'"
                     ).fetchone() is not None:
            shelves = {int(r[0]): r[1] for r in c.execute(
                "SELECT work_id,category FROM work_shelf")}
        brands: dict[int, str] = {}
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_brands'"
                     ).fetchone() is not None:
            for work_id, name in c.execute(
                    "SELECT wb.work_id,b.name FROM work_brands wb "
                    "JOIN brands b ON b.id=wb.brand_id "
                    "ORDER BY wb.work_id,wb.position,wb.rowid"):
                brands.setdefault(int(work_id), str(name))
        work_of_copy = {int(r[0]): int(r[1]) for r in c.execute(
            "SELECT id,work_id FROM copies")}

    # A work is "multi copy" based on the whole library (cold copies count;
    # permanently deleted copies do not).  This keeps the layout stable when a
    # cold copy is restored later.
    per_work_count: dict[int, int] = {}
    for item in copies.values():
        if item.location != "已删除":
            per_work_count[item.work_id] = per_work_count.get(item.work_id, 0) + 1

    ordered = sorted(copies.values(), key=lambda item: item.copy_id)
    wanted_ids = None
    if copy_ids is not None:
        wanted_ids = set()
        for value in copy_ids:
            try:
                wanted_ids.add(int(value))
            except (TypeError, ValueError):
                continue
    allowed_sources = None
    if source_ids is not None:
        allowed_sources = set()
        for value in source_ids:
            try:
                allowed_sources.add(int(value))
            except (TypeError, ValueError):
                continue

    occupied: dict[str, int] = {}
    for item in copies.values():
        try:
            occupied[_key(item.path)] = item.copy_id
        except (TypeError, ValueError):
            continue

    skipped: list[tuple[int, str, str]] = []
    moves: list[Move] = []
    already_ok = 0
    seen_dst: dict[str, int] = {}
    used_names: dict[int, set[str]] = {}

    if wanted_ids is not None:
        for missing in sorted(wanted_ids - set(copies)):
            skipped.append((missing, "", f"副本 {missing} 不存在"))
        ordered = [item for item in ordered if item.copy_id in wanted_ids]

    for item in ordered:
        work_id = work_of_copy.get(item.copy_id, item.work_id)
        root, root_id = _resolve_source_root(item, sources)
        if allowed_sources is not None and root_id not in allowed_sources:
            continue
        src = item.path
        if item.location == "冷藏":
            skipped.append((item.copy_id, src, "冷藏中"))
            continue
        if item.location == "已删除":
            skipped.append((item.copy_id, src, "已删除"))
            continue
        if root is None:
            skipped.append((item.copy_id, src, "不在任何游戏目录里"))
            continue
        if not os.path.lexists(src):
            skipped.append((item.copy_id, src, "源路径不存在"))
            continue
        try:
            src_key = _key(src)
        except (TypeError, ValueError):
            skipped.append((item.copy_id, src, "源路径不合法"))
            continue

        category = shelves.get(work_id) or "未分类"
        brand = brands.get(work_id) or "未知品牌"
        title = titles.get(work_id) or _path_basename(src)
        multi = per_work_count.get(work_id, 1) > 1
        original = _windows_safe_name(_path_basename(src))

        name = None
        if multi or item.kind == "archive":
            base_name = original
            if multi and _code_like_name(original, item.kind):
                # A multi-copy work keeps its original folder/file names, but a
                # bare number says nothing: fall back to the work title.  For
                # an archive the extension of this copy is kept.
                base_name = _windows_safe_name(title)
                if item.kind == "archive":
                    base_name += os.path.splitext(original)[1]
            names = used_names.setdefault(work_id, set())
            number = 1
            candidate = _numbered_name(base_name, number, item.kind)
            while candidate.casefold() in names:
                number += 1
                candidate = _numbered_name(base_name, number, item.kind)
            names.add(candidate.casefold())
            name = candidate
            if not multi and item.kind == "archive":
                names.clear()  # only the archive's own name is needed for a single copy

        target = None
        limit = 80
        while limit >= 20:
            candidate_target = _organize_target(root, category, brand, title, name=name,
                                                title_limit=limit)
            if _fits(candidate_target):
                target = candidate_target
                break
            limit -= 10
        if target is None:
            skipped.append((item.copy_id, src, "路径太长（超过 240 字符）"))
            continue

        try:
            dst_key = _key(target)
        except (TypeError, ValueError):
            skipped.append((item.copy_id, src, "目标路径不合法"))
            continue
        if src_key == dst_key:
            already_ok += 1
            continue
        nested = _is_parent(src_key, dst_key)
        if nested:
            # Only a copy that already sits at <title> and moves into
            # <title>/<name> has the safe two-step form; any other ancestor
            # relation is still refused.
            try:
                parent_key = _key(os.path.dirname(os.path.abspath(target)))
            except (TypeError, ValueError):
                parent_key = None
            if parent_key != src_key or not os.path.isdir(src):
                skipped.append((item.copy_id, src, f"目标在源里面：{target}"))
                continue
        if _is_parent(dst_key, src_key):
            skipped.append((item.copy_id, src, f"源在目标里面：{target}"))
            continue
        if not _same_volume(src, target):
            skipped.append((item.copy_id, src, f"跨盘，不能直接搬：{target}"))
            continue
        if os.path.lexists(target):
            skipped.append((item.copy_id, src, f"目标已存在：{target}"))
            continue
        owner = occupied.get(dst_key)
        if owner is not None and owner != item.copy_id:
            skipped.append((item.copy_id, src, f"目标已被另一个副本占用：{target}"))
            continue
        if dst_key in seen_dst:
            skipped.append((item.copy_id, src, f"多个副本指向同一个目标：{target}"))
            continue
        try:
            size = _tree_size(src)
            warnings: tuple[str, ...] = ()
        except StorageError as error:
            # A same-volume organize is a rename and needs no size at all; an
            # unreadable file (WinError 1392, ...) must not lose the copy.
            size, warnings = 0, (f"{error}；按 0 计",)

        extras = []
        extra_conflict = False
        for volume in _volume_siblings(item):
            volume_dst = _join_segments(os.path.dirname(target), [os.path.basename(volume)])
            if os.path.lexists(volume_dst) and _key(volume_dst) != _key(volume):
                extra_conflict = True
                break
            extras.append((volume, volume_dst))
        if extra_conflict:
            skipped.append((item.copy_id, src, f"分卷目标已存在：{target}"))
            continue

        seen_dst[dst_key] = item.copy_id
        moves.append(Move(int(item.copy_id), src, target, int(size), "organize",
                          root_id, tuple(extras), nested=nested, warnings=warnings))

    return OrganizePlan(_organize_order(moves), tuple(skipped), already_ok)


def _rollback_renames(renamed: list[tuple[str, str]], *,
                      remove_empty: tuple[str, ...] = ()) -> list[str]:
    """Undo renames in reverse order.

    ``remove_empty`` lists paths that the operation created as empty parent
    folders (a self-nesting copy recreates its own source path as the title
    folder); they are removed before the temporary name is renamed back.
    """
    removable: set[str] = set()
    for path in remove_empty:
        try:
            removable.add(_key(path))
        except (TypeError, ValueError):
            continue
    problems = []
    for src, dst in reversed(renamed):
        try:
            if not os.path.lexists(dst):
                continue
            if os.path.lexists(src):
                try:
                    key = _key(src)
                except (TypeError, ValueError):
                    key = None
                if key is None or key not in removable:
                    continue
                if not os.path.isdir(src) or os.path.islink(src):
                    continue
                os.rmdir(src)  # only succeeds when the folder is empty
            parent = os.path.dirname(os.path.abspath(src))
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.rename(dst, src)
        except OSError as error:
            problems.append(f"{dst} → {src}：{error}")
    return problems


def _rollback_unnest(dst: str, src: str, temp: str) -> list[str]:
    """Put a self-nesting item back inside its title folder after a failed undo."""
    problems = []
    try:
        if os.path.lexists(temp) and not os.path.lexists(dst):
            # undo stopped after the item was moved to the temp name
            parent = os.path.dirname(os.path.abspath(dst))
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.rename(temp, dst)
        elif os.path.lexists(src) and not os.path.lexists(dst) \
                and not os.path.lexists(temp):
            # undo restored the original folder; nest the item back into it
            os.rename(src, temp)
            parent = os.path.dirname(os.path.abspath(dst))
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.rename(temp, dst)
    except OSError as error:
        problems.append(f"{dst} → 临时名：{error}")
    return problems


def _prune_empty_dirs(start: str, root: str) -> list[str]:
    """Remove empty directories from ``start`` up to (but not including) ``root``."""
    removed = []
    try:
        root_key = _key(root)
    except (TypeError, ValueError):
        return removed
    games = os.path.join(root, "Games")
    try:
        games_key = _key(games)
    except (TypeError, ValueError):
        games_key = None
    current = os.path.abspath(start)
    while True:
        try:
            current_key = _key(current)
        except (TypeError, ValueError):
            break
        if current_key == root_key or (games_key is not None and current_key == games_key):
            break
        if not os.path.isdir(current) or os.path.islink(current):
            break
        try:
            os.rmdir(current)  # only succeeds when the directory is empty
        except OSError:
            break
        removed.append(current)
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
    return removed


def _leftovers(roots: set[str]) -> tuple[str, ...]:
    out = []
    for root in sorted(roots):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if name.casefold() == "games":
                continue
            path = os.path.join(root, name)
            if os.path.isdir(path) and not os.path.islink(path):
                try:
                    if not os.listdir(path):
                        continue
                except OSError:
                    pass
            out.append(path)
    return tuple(out)


def _organize_integrity(lib, expected: list[tuple[int, str]]) -> tuple[str, ...]:
    state = list_storage(lib)
    problems = []
    for copy_id, path in expected:
        item = state.get(copy_id)
        if item is None:
            problems.append(f"副本 {copy_id} 不在库里")
            continue
        try:
            if _key(item.path) != _key(path):
                problems.append(f"副本 {copy_id} 的库路径不一致：{item.path} ≠ {path}")
        except (TypeError, ValueError):
            problems.append(f"副本 {copy_id} 的路径不合法：{item.path}")
        if not os.path.lexists(path):
            problems.append(f"文件不在目标位置：{path}")
    return tuple(problems)


def run_organize(lib, plan, *, cancel=None, progress=None, log_path=None) -> OrganizeResult:
    """Move every item in an :class:`OrganizePlan` with same-volume renames."""
    if lib.readonly:
        raise StorageError("这个库是只读打开的，不能整理游戏文件")
    moves = tuple(plan.moves)
    batch = f"{utc_now().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    backup_path = None
    if moves:
        from . import tidy
        backup_path = tidy.backup_library(lib, "pre-organize")

    sources = _source_roots(lib)
    skipped = list(plan.skipped)
    failed: list[tuple[int, str, str]] = []
    moved = 0
    removed: list[str] = []
    removed_seen: set[str] = set()
    roots: set[str] = set()
    expected: list[tuple[int, str]] = []
    total = len(moves)

    for index, move in enumerate(moves, 1):
        if cancel is not None and cancel():
            raise Cancelled("已取消整理；已经搬好的文件保持新位置")
        item = list_storage(lib).get(move.copy_id)
        work_id = item.work_id if item is not None else 0
        renamed: list[tuple[str, str]] = []
        temp: str | None = None
        root, _root_id = _resolve_source_root(item, sources) if item is not None else (None, None)
        if root is not None:
            roots.add(root)
        extra_info = {"batch": batch, "backup": backup_path}
        if move.nested:
            extra_info["nested"] = True
        if move.warnings:
            extra_info["warnings"] = list(move.warnings)
        try:
            if item is None:
                raise StorageError(f"副本 {move.copy_id} 不存在")
            if item.location != "常用":
                raise StorageError(f"{move.src} 不在常用盘，不能整理")
            if not os.path.lexists(move.src):
                raise StorageError(f"源不存在：{move.src}")
            if move.nested:
                # The copy already sits at <title>, but its destination is
                # <title>/<name>.  Vacate the path first, then create the
                # title folder and move the copy into it.
                temp = move.src + f".gp2tmp-{move.copy_id}"
                extra_info["temp"] = temp
                if os.path.lexists(temp):
                    raise StorageError(f"临时目标已存在：{temp}")
                if os.path.lexists(move.dst):
                    raise StorageError(f"目标已存在：{move.dst}")
                os.rename(move.src, temp)
                renamed.append((move.src, temp))
                parent = os.path.dirname(os.path.abspath(move.dst))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if os.path.lexists(move.dst):
                    raise StorageError(f"目标已存在：{move.dst}")
                os.rename(temp, move.dst)
                renamed.append((temp, move.dst))
            else:
                if os.path.lexists(move.dst):
                    raise StorageError(f"目标已存在：{move.dst}")
                parent = os.path.dirname(os.path.abspath(move.dst))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                os.rename(move.src, move.dst)
                renamed.append((move.src, move.dst))
            for extra_src, extra_dst in move.extras:
                if not os.path.lexists(extra_src):
                    continue  # a missing part must not stop the found ones
                if os.path.lexists(extra_dst):
                    raise StorageError(f"分卷目标已存在：{extra_dst}")
                extra_parent = os.path.dirname(os.path.abspath(extra_dst))
                if extra_parent:
                    os.makedirs(extra_parent, exist_ok=True)
                os.rename(extra_src, extra_dst)
                renamed.append((extra_src, extra_dst))
            stamp = utc_now()
            with lib.transaction() as c:
                cursor = c.execute(
                    "UPDATE copies SET path=?,path_key=?,availability='ok',checked_at=? "
                    "WHERE id=?", (move.dst, _key(move.dst), stamp, move.copy_id))
                if cursor.rowcount != 1:
                    raise StorageError(f"副本 {move.copy_id} 不存在")
        except Exception as error:  # noqa: BLE001 - every item is reported, never aborts the batch
            cleanup = _rollback_renames(
                renamed, remove_empty=(move.src,) if move.nested else ())
            message = str(error)
            if renamed and not cleanup:
                message += "；已把这一项搬回原位置"
            elif cleanup:
                message += "；回滚没有完成：" + "；".join(cleanup)
            if renamed and root is not None:
                _prune_empty_dirs(os.path.dirname(os.path.abspath(move.dst)), root)
            failed.append((move.copy_id, move.src, message))
            _log(log_path, "organize", move.copy_id, work_id, move.src, move.dst,
                 move.size_bytes, "failed", message, extras=move.extras,
                 extra=extra_info)
        else:
            moved += 1
            expected.append((move.copy_id, move.dst))
            _log(log_path, "organize", move.copy_id, work_id, move.src, move.dst,
                 move.size_bytes, "ok", None, extras=move.extras,
                 extra=extra_info)
            if root is not None:
                for path in _prune_empty_dirs(os.path.dirname(os.path.abspath(move.src)), root):
                    if path not in removed_seen:
                        removed_seen.add(path)
                        removed.append(path)
                for extra_src, _extra_dst in move.extras:
                    for path in _prune_empty_dirs(os.path.dirname(os.path.abspath(extra_src)), root):
                        if path not in removed_seen:
                            removed_seen.add(path)
                            removed.append(path)
        if progress is not None:
            progress(index, total, move.src)

    return OrganizeResult(moved=moved, failed=tuple(failed), skipped=tuple(skipped),
                          removed_empty_dirs=tuple(removed),
                          leftover_dirs=_leftovers(roots), log_path=log_path,
                          integrity_check=_organize_integrity(lib, expected),
                          backup_path=backup_path, batch=batch)


def read_organize_log(log_path) -> list[dict]:
    """All well-formed organize records in a storage JSONL log, oldest first."""
    if not log_path:
        return []
    records = []
    try:
        with open(log_path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(record, dict) and record.get("action") == "organize":
                    records.append(record)
    except OSError:
        return []
    return records


def list_organize_batches(log_path) -> list[dict]:
    """One summary row per organize run, for the tidy log / undo dialog."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for record in read_organize_log(log_path):
        batch = record.get("batch") or "legacy"
        if batch not in groups:
            groups[batch] = {"batch": batch if batch != "legacy" else None,
                             "time": record.get("time"),
                             "backup_path": record.get("backup"),
                             "log_path": log_path, "moved": 0, "failed": 0}
            order.append(batch)
        group = groups[batch]
        if record.get("result") == "ok":
            group["moved"] += 1
        elif record.get("result") == "failed":
            group["failed"] += 1
        group["backup_path"] = group["backup_path"] or record.get("backup")
        group["time"] = group["time"] or record.get("time")
    return [groups[key] for key in order]


def undo_organize(lib, log_path, *, batch=None) -> OrganizeResult:
    """Move a logged organize run (or every run) back, newest record first."""
    if lib.readonly:
        raise StorageError("这个库是只读打开的，不能撤销整理")
    records = [record for record in read_organize_log(log_path)
               if record.get("result") == "ok"
               and (batch is None or record.get("batch") == batch)]
    skipped: list[tuple[int, str, str]] = []
    failed: list[tuple[int, str, str]] = []
    moved = 0
    expected: list[tuple[int, str]] = []

    for record in reversed(records):
        try:
            copy_id = int(record.get("copy_id"))
        except (TypeError, ValueError):
            continue
        src, dst = record.get("src"), record.get("dst")
        if not isinstance(src, str) or not isinstance(dst, str):
            skipped.append((copy_id, str(dst or ""), "日志里的路径不完整"))
            continue
        nested = bool(record.get("nested"))
        temp = record.get("temp")
        if not isinstance(temp, str) or not temp:
            temp = src + f".gp2tmp-{copy_id}"
        if not os.path.lexists(dst):
            skipped.append((copy_id, dst, "现在的位置不存在，无法撤销"))
            continue
        if nested:
            # The original path is now the title folder that contains dst; it
            # may only be reused when dst is its only remaining entry.
            if os.path.lexists(src):
                if not os.path.isdir(src) or os.path.islink(src):
                    skipped.append((copy_id, src, "原位置已被占用"))
                    continue
                entries = os.listdir(src)
                if len(entries) != 1 or \
                        entries[0].casefold() != os.path.basename(dst).casefold():
                    skipped.append((copy_id, src, "原位置已被占用"))
                    continue
            if os.path.lexists(temp):
                skipped.append((copy_id, temp, "临时位置已被占用"))
                continue
        elif os.path.lexists(src):
            skipped.append((copy_id, src, "原位置已被占用"))
            continue
        extras = []
        if not nested:
            try:
                for extra_src, extra_dst in record.get("extras") or []:
                    if isinstance(extra_src, str) and isinstance(extra_dst, str):
                        extras.append((extra_src, extra_dst))
            except (TypeError, ValueError):
                extras = []
            conflict = next((extra_src for extra_src, _ in extras if os.path.lexists(extra_src)),
                            None)
            if conflict is not None:
                skipped.append((copy_id, conflict, "原位置已被占用"))
                continue
        renames = [(extra_dst, extra_src) for extra_src, extra_dst in reversed(extras)
                   if os.path.lexists(extra_dst)]
        renames.append((dst, src))
        renamed: list[tuple[str, str]] = []
        try:
            if nested:
                # Undo the two-step nest: dst -> sibling temp, drop the now
                # empty title folder, then temp -> the original path.
                parent = os.path.dirname(os.path.abspath(temp))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                os.rename(dst, temp)
                renamed.append((dst, temp))
                if os.path.lexists(src):
                    os.rmdir(src)
                os.rename(temp, src)
                renamed.append((temp, src))
            else:
                for from_path, to_path in renames:
                    parent = os.path.dirname(os.path.abspath(to_path))
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    os.rename(from_path, to_path)
                    renamed.append((from_path, to_path))
            stamp = utc_now()
            with lib.transaction() as c:
                cursor = c.execute(
                    "UPDATE copies SET path=?,path_key=?,availability='ok',checked_at=? "
                    "WHERE id=?", (src, _key(src), stamp, copy_id))
                if cursor.rowcount != 1:
                    raise StorageError(f"副本 {copy_id} 不存在")
        except Exception as error:  # noqa: BLE001 - reported per record
            cleanup = (_rollback_unnest(dst, src, temp) if nested
                       else _rollback_renames(renamed))
            message = str(error)
            if renamed and not cleanup:
                message += "；已把这一项恢复原状"
            elif cleanup:
                message += "；回滚没有完成：" + "；".join(cleanup)
            failed.append((copy_id, src, message))
        else:
            moved += 1
            expected.append((copy_id, src))
    return OrganizeResult(moved=moved, failed=tuple(failed), skipped=tuple(skipped),
                          removed_empty_dirs=(), leftover_dirs=(), log_path=log_path,
                          integrity_check=_organize_integrity(lib, expected))


# ------------------------------------------------------------------ delete
def delete_copies(lib, copy_ids, *, log_path=None) -> list[int]:
    """Permanently delete the game files of the given copies."""
    if lib.readonly:
        raise StorageError("这个库是只读打开的，不能删除游戏文件")
    copies = list_storage(lib)
    with lib.connection() as c:
        source_paths = [row[0] for row in c.execute("SELECT path FROM sources")]
    keys: dict[int, str | None] = {}
    for item in copies.values():
        try:
            keys[item.copy_id] = _key(item.path)
        except (TypeError, ValueError):
            keys[item.copy_id] = None

    ordered = list(dict.fromkeys(copy_ids))
    problems: list[str] = []
    for copy_id in ordered:
        item = copies.get(copy_id)
        if item is None:
            problems.append(f"副本 {copy_id} 不存在")
            continue
        if item.location == "已删除":
            problems.append(f"{item.path} 已经标记为已删除")
            continue
        parts = _segments(item.path)
        if any(part in (".", "..") for part in parts):
            problems.append(f"路径里不能有 . 或 .. 段：{item.path}")
            continue
        index = next((i for i, part in enumerate(parts) if part.lower() == "games"), None)
        if index is None:
            problems.append(f"路径里没有 Games 段：{item.path}")
            continue
        if len(parts) - index - 1 < 2:
            problems.append(f"Games 后面至少要有分类和标题：{item.path}")
            continue
        if _is_drive_root(item.path):
            problems.append(f"不能删除盘符根目录：{item.path}")
            continue
        try:
            key = _safe_key(item.path)
        except StorageError as error:
            problems.append(str(error))
            continue
        for source_path in source_paths:
            try:
                source_key = _key(source_path)
            except (TypeError, ValueError):
                continue
            if key == source_key or _is_parent(key, source_key):
                problems.append(f"这个路径是扫描来源，不能删除：{item.path}")
                break
        for other_id, other_key in keys.items():
            if other_id == copy_id or other_key is None:
                continue
            if _is_parent(key, other_key):
                problems.append(f"这个路径包含另一个副本，不能删除：{item.path}")
                break

    if problems:
        raise StorageError("\n".join(problems))

    completed: list[int] = []
    runtime: list[str] = []
    for copy_id in ordered:
        item = copies[copy_id]
        try:
            if os.path.lexists(item.path):
                _remove_path(item.path)
        except OSError as error:
            runtime.append(f"{item.path}：{error}")
            _log(log_path, "delete", item.copy_id, item.work_id, item.path, None,
                 0, "failed", str(error))
            continue
        try:
            stamp = utc_now()
            with lib.transaction() as c:
                c.execute(_CREATE_SQL)
                c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) VALUES(?,?,?,?) "
                          "ON CONFLICT(copy_id) DO UPDATE SET state=excluded.state, "
                          "home_path=NULL, updated_at=excluded.updated_at",
                          (copy_id, "已删除", None, stamp))
                c.execute("UPDATE copies SET availability='missing',checked_at=? WHERE id=?",
                          (stamp, copy_id))
        except Exception as error:  # noqa: BLE001 - reported to the UI as StorageError
            _log(log_path, "delete", item.copy_id, item.work_id, item.path, None,
                 0, "failed", str(error))
            raise StorageError(f"删除记录写入失败：{item.path}：{error}") from error
        _log(log_path, "delete", item.copy_id, item.work_id, item.path, None, 0, "ok", None)
        completed.append(copy_id)
    if runtime:
        raise StorageError("\n".join(runtime))
    return completed
