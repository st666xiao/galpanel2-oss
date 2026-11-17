"""Optional shelf metadata (category / status) for works.

The data lives in an extra table that is created on demand.  The library
schema version (PRAGMA user_version = 6) is intentionally left untouched.
Only the Python standard library is used here.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3

from .domain import utc_now

CATEGORIES = ("日系", "同人", "欧美", "其他")
STATUSES = ("想玩", "在玩", "玩过", "不玩了")

_CREATE_SQL = """CREATE TABLE IF NOT EXISTS work_shelf(
  work_id INTEGER PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
  category TEXT CHECK(category IS NULL OR category IN ('日系','同人','欧美','其他')),
  status TEXT CHECK(status IS NULL OR status IN ('想玩','在玩','玩过','不玩了')),
  updated_at TEXT NOT NULL
)"""


@dataclass(frozen=True)
class Shelf:
    category: str | None
    status: str | None


def _table_exists(c) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_shelf'"
    ).fetchone() is not None


def list_shelf(lib) -> dict[int, Shelf]:
    """Read shelf rows for works that still exist.

    Never creates the table nor writes anything: a read-only library can be
    used.  Works whose category and status are both NULL may be omitted.
    """
    with lib.connection() as c:
        if not _table_exists(c):
            return {}
        rows = c.execute(
            "SELECT s.work_id, s.category, s.status FROM work_shelf s "
            "JOIN works w ON w.id = s.work_id"
        ).fetchall()
    out: dict[int, Shelf] = {}
    for r in rows:
        cat, st = r[1], r[2]
        if cat is None and st is None:
            continue
        out[r[0]] = Shelf(cat, st)
    return out


def _validate(value, allowed, label: str) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"invalid {label}: {value!r}")


def _write(lib, work_id: int, column: str, value) -> None:
    allowed = CATEGORIES if column == "category" else STATUSES
    _validate(value, allowed, column)
    with lib.transaction() as c:
        c.execute(_CREATE_SQL)
        exists = c.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone()
        if not exists:
            raise KeyError(work_id)
        c.execute(
            f"INSERT INTO work_shelf(work_id,{column},updated_at) VALUES(?,?,?) "
            f"ON CONFLICT(work_id) DO UPDATE SET {column}=excluded.{column}, "
            "updated_at=excluded.updated_at",
            (work_id, value, utc_now()),
        )


def set_category(lib, work_id: int, category: str | None) -> None:
    _write(lib, work_id, "category", category)


def set_status(lib, work_id: int, status: str | None) -> None:
    _write(lib, work_id, "status", status)


def set_categories(lib, mapping: dict[int, str], overwrite: bool = False) -> int:
    """Bulk-set categories in one transaction.

    Work ids missing from ``works`` are skipped.  An invalid value raises
    ValueError and nothing is written.  With ``overwrite=False`` works that
    already have a category are left alone.  Returns the number of rows that
    were actually inserted or changed.
    """
    # Validate the whole batch before starting the transaction so that a bad
    # value never leaves a partial write.
    for value in mapping.values():
        _validate(value, CATEGORIES, "category")
    if not mapping:
        return 0
    written = 0
    stamp = utc_now()
    with lib.transaction() as c:
        c.execute(_CREATE_SQL)
        for work_id, value in mapping.items():
            row = c.execute(
                "SELECT s.category, w.id FROM works w LEFT JOIN work_shelf s ON s.work_id=w.id "
                "WHERE w.id=?", (work_id,)
            ).fetchone()
            if row is None:
                continue  # work does not exist -> skip
            current = row[0]
            if not overwrite and current is not None:
                continue
            if current == value:
                continue  # no change
            c.execute(
                "INSERT INTO work_shelf(work_id,category,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(work_id) DO UPDATE SET category=excluded.category, "
                "updated_at=excluded.updated_at",
                (work_id, value, stamp),
            )
            written += 1
    return written


def guess_category(path: str) -> str | None:
    """Guess a category from a path segment right after a ``Games`` segment."""
    if not path:
        return None
    parts = re.split(r"[\\/]", path)
    for i, part in enumerate(parts):
        if part.lower() == "games":
            if i + 1 < len(parts) and parts[i + 1] in CATEGORIES:
                return parts[i + 1]
            return None
    return None
