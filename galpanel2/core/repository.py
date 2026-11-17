"""Library projections and CAS writes; never delete user files."""
import hashlib
import os
from pathlib import Path
import tempfile

from .domain import Copy, Source, WorkDetail, WorkSummary, WriteConflict, path_key, utc_now


def add_source(lib, path: str) -> int:
    key = path_key(path)
    with lib.connection() as c:
        old = c.execute("SELECT id FROM sources WHERE path_key=?", (key,)).fetchone()
        if old:
            return old[0]
    if not os.path.isdir(path):
        raise ValueError("Source must be an existing directory")
    with lib.transaction() as c:
        c.execute("INSERT OR IGNORE INTO sources(path,path_key,added_at) VALUES(?,?,?)", (path, key, utc_now()))
        return c.execute("SELECT id FROM sources WHERE path_key=?", (key,)).fetchone()[0]


def list_sources(lib) -> list[Source]:
    with lib.connection() as c:
        return [Source(*r) for r in c.execute("SELECT id,path,label,last_scan_at FROM sources ORDER BY id")]


def remove_source(lib, source_id: int) -> None:
    with lib.transaction() as c:
        c.execute("DELETE FROM sources WHERE id=?", (source_id,))


def set_source_label(lib, source_id: int, label: str) -> None:
    """Set a source's display label (empty clears it)."""
    text = (label or "").strip()
    if len(text) > 40:
        raise ValueError("目录名称最多 40 个字符")
    with lib.transaction() as c:
        if c.execute("UPDATE sources SET label=? WHERE id=?", (text, source_id)).rowcount != 1:
            raise KeyError(source_id)


def availability_of(states):
    s = set(states)
    if s == {"ok"}:
        return "ok"
    if s == {"missing"}:
        return "missing"
    if "ok" in s and s & {"missing", "error"}:
        return "partial"
    return "unknown"


def list_works(lib, include_hidden: bool = False) -> list[WorkSummary]:
    with lib.connection() as c:
        copies = {}
        for r in c.execute("SELECT work_id,kind,availability FROM copies ORDER BY id"):
            copies.setdefault(r[0], []).append((r[1], r[2]))
        covers = {r[0] for r in c.execute("SELECT work_id FROM covers")}
        rows = c.execute("SELECT id,title,developer,hidden,added_at FROM works " +
                         ("" if include_hidden else "WHERE hidden=0 ") + "ORDER BY id")
        out = []
        for r in rows:
            cp = copies.get(r[0], [])
            out.append(WorkSummary(r[0], r[1], r[2], bool(r[3]), r[4], r[0] in covers, len(cp),
                                   tuple(sorted({x[0] for x in cp})), availability_of(x[1] for x in cp)))
        return out


def get_work(lib, work_id: int) -> WorkDetail:
    with lib.connection() as c:
        r = c.execute("SELECT id,uuid,title,developer,notes,hidden,added_at,revision FROM works WHERE id=?",
                      (work_id,)).fetchone()
        if r is None:
            raise KeyError(work_id)
        cr = c.execute("SELECT file_name FROM covers WHERE work_id=?", (work_id,)).fetchone()
        copies = tuple(Copy(*x) for x in c.execute(
            "SELECT id,kind,path,exe_relpath,size_bytes,availability,checked_at,source_id,origin "
            "FROM copies WHERE work_id=? ORDER BY id", (work_id,)))
        return WorkDetail(*r[:5], bool(r[5]), *r[6:],
                          str(Path(lib.path + ".covers") / cr[0]) if cr else None, copies)


def edit_work(lib, work_id: int, expected_revision: int, **changes) -> WorkDetail:
    if set(changes) - {"title", "developer", "notes", "hidden"}:
        raise ValueError("Only title, developer, notes and hidden may be edited")
    for k, v in changes.items():
        if k == "hidden":
            if v not in (True, False, 0, 1):
                raise ValueError("hidden must be a boolean")
            changes[k] = int(v)
        elif not isinstance(v, str):
            raise ValueError(f"{k} must be text")
    if "title" in changes:
        changes["title"] = changes["title"].strip()
        if not changes["title"]:
            raise ValueError("Title must not be blank")
    with lib.transaction() as c:
        assignments = [f"{k}=?" for k in changes] + ["revision=revision+1", "updated_at=?"]
        r = c.execute("UPDATE works SET " + ",".join(assignments) + " WHERE id=? AND revision=?",
                      [*changes.values(), utc_now(), work_id, expected_revision])
        if r.rowcount != 1:
            raise WriteConflict("Work changed or was removed")
        return get_work(lib, work_id)


def image_extension(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise ValueError("Cover must be JPEG, PNG, WebP or GIF")


def _cover_target(image_path):
    data = Path(image_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return data, digest, digest + "." + image_extension(data)


def copy_cover(image_path, directory):
    data, digest, name = _cover_target(image_path)
    directory = Path(directory)
    directory.mkdir(exist_ok=True)
    dest = directory / name
    if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
        fd, tmp = tempfile.mkstemp(prefix=".cover-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return name, digest


def set_cover(lib, work_id: int, expected_revision: int, image_path: str | None) -> WorkDetail:
    with lib.transaction() as c:
        r = c.execute("UPDATE works SET revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                      (utc_now(), work_id, expected_revision))
        if r.rowcount != 1:
            raise WriteConflict("Work changed or was removed")
        if image_path is None:
            c.execute("DELETE FROM covers WHERE work_id=?", (work_id,))
        else:
            directory = Path(lib.path + ".covers")
            existed = (directory / _cover_target(image_path)[2]).exists()
            name, digest = copy_cover(image_path, directory)
            try:
                c.execute("INSERT INTO covers VALUES(?,?,?,'user') ON CONFLICT(work_id) DO UPDATE SET "
                          "file_name=excluded.file_name,sha256=excluded.sha256,origin='user'", (work_id, name, digest))
                return get_work(lib, work_id)
            except BaseException:
                if not existed:
                    try:
                        (directory / name).unlink()
                    except OSError:
                        pass
                raise
        return get_work(lib, work_id)


def remove_work(lib, work_id: int, expected_revision: int) -> None:
    with lib.transaction() as c:
        if c.execute("DELETE FROM works WHERE id=? AND revision=?", (work_id, expected_revision)).rowcount != 1:
            raise WriteConflict("Work changed or was removed")
