"""Windows lexical launch options; only resolution visits the filesystem."""
from dataclasses import dataclass
import ntpath
import os

from .domain import NotLaunchable, path_key, utc_now


@dataclass(frozen=True)
class LaunchOptions:
    copy_id: int
    exe_relpath: str | None
    args: str
    scan_exe_relpath: str | None


@dataclass(frozen=True)
class LaunchSpec:
    work_id: int
    copy_id: int
    exe_path: str
    args: str
    cwd: str


def _copy(c, copy_id):
    row = c.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
    if row is None:
        raise KeyError(copy_id)
    return row


def get_launch_options(lib, copy_id: int) -> LaunchOptions:
    with lib.connection() as c:
        cp = _copy(c, copy_id)
        row = c.execute("SELECT exe_relpath,args FROM launch_options WHERE copy_id=?", (copy_id,)).fetchone() \
            if lib.schema_version >= 2 else None
        return LaunchOptions(copy_id, row[0] if row else None, row[1] if row else "", cp["exe_relpath"])


def set_launch_options(lib, copy_id: int, exe_relpath: str | None, args: str) -> LaunchOptions:
    with lib.transaction() as c:
        if lib.schema_version < 2:
            raise ValueError("Library needs version 2")
        cp = _copy(c, copy_id)
        if cp["kind"] == "archive":
            raise ValueError("Archive cannot be launched")
        if exe_relpath is not None:
            if not isinstance(exe_relpath, str) or "\0" in exe_relpath or \
                    ntpath.splitdrive(exe_relpath)[0] or exe_relpath.startswith(("\\", "/")):
                raise ValueError("Expected a relative .exe path")
            exe_relpath = ntpath.normpath(exe_relpath)
            if exe_relpath.startswith("..") or not exe_relpath.lower().endswith(".exe"):
                raise ValueError("Expected a relative .exe path inside the copy")
        if not isinstance(args, str) or "\0" in args or len(args) > 2000:
            raise ValueError("Invalid launch arguments")
        c.execute("INSERT INTO launch_options VALUES(?,?,?,?) ON CONFLICT(copy_id) DO UPDATE SET "
                  "exe_relpath=excluded.exe_relpath,args=excluded.args,updated_at=excluded.updated_at",
                  (copy_id, exe_relpath, args, utc_now()))
        return LaunchOptions(copy_id, exe_relpath, args, cp["exe_relpath"])


def resolve_launch(lib, copy_id: int) -> LaunchSpec:
    with lib.connection() as c:
        cp = dict(_copy(c, copy_id))
        opts = get_launch_options(lib, copy_id)
    if cp["kind"] == "archive":
        raise NotLaunchable("archive")
    if not os.path.isdir(cp["path"]):
        raise NotLaunchable("copy_missing")
    relative = opts.exe_relpath if opts.exe_relpath is not None else opts.scan_exe_relpath
    if not relative:
        raise NotLaunchable("no_exe")
    exe = ntpath.join(cp["path"], relative)
    try:
        contained = path_key(exe).startswith(path_key(cp["path"]).rstrip("\\") + "\\")
    except ValueError:
        contained = False
    if not contained:
        raise NotLaunchable("bad_exe_path")
    if not os.path.isfile(exe):
        raise NotLaunchable("exe_missing")
    return LaunchSpec(cp["work_id"], copy_id, exe, opts.args, ntpath.dirname(exe))


def default_copy(lib, work_id: int) -> int:
    with lib.connection() as c:
        if c.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone() is None:
            raise KeyError(work_id)
        ids = [r[0] for r in c.execute("SELECT id FROM copies WHERE work_id=? ORDER BY id", (work_id,))]
    first = None
    for copy_id in ids:
        try:
            resolve_launch(lib, copy_id)
            return copy_id
        except NotLaunchable as e:
            if first is None:
                first = e
    raise first if first is not None else NotLaunchable("no_copy")
