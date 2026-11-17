"""Atomic sessions and aggregated, read-only legacy time."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import re
import sqlite3

from .domain import SessionActive, utc_now


@dataclass(frozen=True)
class Session:
    id: int
    work_id: int
    copy_id: int | None
    kind: str
    state: str
    local_day: str
    started_at: str
    ended_at: str | None
    seconds: int
    end_reason: str | None
    edited: bool


@dataclass(frozen=True)
class Playtime:
    work_id: int
    legacy_seconds: int
    session_seconds: int
    total_seconds: int
    session_count: int
    last_played_at: str | None
    last_played_source: str | None


@dataclass(frozen=True)
class DayTotal:
    day: str
    seconds: int
    legacy_seconds: int


def _v2(lib):
    if lib.schema_version < 2:
        raise ValueError("Library needs version 2")


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected YYYY-MM-DD")
    return date.fromisoformat(value)


def _integer(value, low, high=None):
    if type(value) is not int or value < low or (high is not None and value > high):
        raise ValueError("Invalid duration")


def _work(c, work_id):
    if c.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone() is None:
        raise KeyError(work_id)


def _row(c, session_id):
    row = c.execute("SELECT * FROM play_sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(session_id)
    return row


def _session(row):
    return Session(**{k: bool(row[k]) if k == "edited" else row[k] for k in Session.__dataclass_fields__})


def _running(c, session_id):
    try:
        row = _row(c, session_id)
    except KeyError as e:
        raise ValueError("Session is not running") from e
    if row["state"] != "running":
        raise ValueError("Session is not running")
    return row


def start_session(lib, work_id: int, copy_id: int | None, local_day: str | None = None) -> int:
    _v2(lib)
    local_day = datetime.now().astimezone().date().isoformat() if local_day is None else local_day
    _day(local_day)
    with lib.transaction() as c:
        _work(c, work_id)
        if copy_id is not None and c.execute("SELECT 1 FROM copies WHERE id=? AND work_id=?",
                                            (copy_id, work_id)).fetchone() is None:
            raise ValueError("Copy does not belong to this work")
        if c.execute("SELECT 1 FROM play_sessions WHERE state='running'").fetchone():
            raise SessionActive("A game is already being tracked")
        now = utc_now()
        try:
            return c.execute("INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,started_at,last_beat_at) "
                             "VALUES(?,?,'tracked','running',?,?,?)", (work_id, copy_id, local_day, now, now)).lastrowid
        except sqlite3.IntegrityError as e:
            if "play_sessions.state" in str(e):
                raise SessionActive("A game is already being tracked") from e
            raise


def checkpoint_session(lib, session_id: int, seconds: int) -> None:
    _v2(lib)
    _integer(seconds, 0)
    with lib.transaction() as c:
        row = _running(c, session_id)
        if seconds < row["seconds"]:
            raise ValueError("Duration cannot decrease")
        c.execute("UPDATE play_sessions SET seconds=?,last_beat_at=? WHERE id=?", (seconds, utc_now(), session_id))


def end_session(lib, session_id: int, seconds: int, reason: str) -> Session:
    _v2(lib)
    _integer(seconds, 0)
    if reason not in ("exited", "panel_closed"):
        raise ValueError("Invalid end reason")
    with lib.transaction() as c:
        row = _running(c, session_id)
        if seconds < row["seconds"]:
            raise ValueError("Duration cannot decrease")
        now = utc_now()
        c.execute("UPDATE play_sessions SET state='closed',seconds=?,ended_at=?,last_beat_at=?,end_reason=? "
                  "WHERE id=?", (seconds, now, now, reason, session_id))
        return _session(_row(c, session_id))


def discard_session(lib, session_id: int) -> None:
    _v2(lib)
    with lib.transaction() as c:
        row = _running(c, session_id)
        if row["seconds"] != 0:
            raise ValueError("Cannot discard recorded time")
        c.execute("DELETE FROM play_sessions WHERE id=?", (session_id,))


def recover_sessions(lib) -> int:
    _v2(lib)
    with lib.transaction() as c:
        return c.execute("UPDATE play_sessions SET state='recovered',end_reason='recovered',ended_at=last_beat_at "
                         "WHERE state='running'").rowcount


def add_manual_session(lib, work_id: int, day: str, minutes: int) -> int:
    _v2(lib)
    _day(day)
    _integer(minutes, 1, 1440)
    with lib.transaction() as c:
        _work(c, work_id)
        now = utc_now()
        return c.execute("INSERT INTO play_sessions(work_id,kind,state,local_day,started_at,last_beat_at,ended_at,"
                         "seconds,end_reason) VALUES(?,'manual','closed',?,?,?,?,?,'manual')",
                         (work_id, day, now, now, now, minutes * 60)).lastrowid


def edit_session(lib, session_id: int, minutes: int) -> Session:
    _v2(lib)
    _integer(minutes, 0, 1440)
    with lib.transaction() as c:
        row = _row(c, session_id)
        if row["state"] == "running":
            raise ValueError("Cannot edit a running session")
        c.execute("UPDATE play_sessions SET seconds=?,edited=1 WHERE id=?", (minutes * 60, session_id))
        return _session(_row(c, session_id))


def delete_session(lib, session_id: int) -> None:
    _v2(lib)
    with lib.transaction() as c:
        if _row(c, session_id)["state"] == "running":
            raise ValueError("Cannot delete a running session")
        c.execute("DELETE FROM play_sessions WHERE id=?", (session_id,))


def get_session(lib, session_id: int) -> Session:
    if lib.schema_version == 1:
        raise KeyError(session_id)
    with lib.connection() as c:
        return _session(_row(c, session_id))


def list_sessions(lib, work_id: int | None = None, limit: int | None = None) -> list[Session]:
    if limit is not None:
        _integer(limit, 0)
    if lib.schema_version == 1:
        return []
    sql, args = "SELECT * FROM play_sessions", []
    if work_id is not None:
        sql += " WHERE work_id=?"
        args.append(work_id)
    sql += " ORDER BY started_at DESC,id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    with lib.connection() as c:
        return [_session(row) for row in c.execute(sql, args)]


def _legacy_value(text):
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def list_playtimes(lib) -> dict[int, Playtime]:
    with lib.connection() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM works ORDER BY id")]
        legacy = {}
        for row in c.execute("SELECT work_id,column_name,value_json FROM legacy_fields "
                             "WHERE column_name IN ('playtime_minutes','last_played_at')"):
            legacy.setdefault(row[0], {})[row[1]] = _legacy_value(row[2])
        sessions = {r[0]: r for r in c.execute("SELECT work_id,SUM(seconds),COUNT(*),"
                    "MAX(CASE WHEN kind='tracked' THEN ended_at END) FROM play_sessions WHERE state!='running' "
                    "GROUP BY work_id")} if lib.schema_version >= 2 else {}
    result = {}
    for wid in ids:
        old = legacy.get(wid, {})
        pm, lp = old.get("playtime_minutes", {}), old.get("last_played_at", {})
        value = pm.get("v")
        legacy_seconds = value * 60 if pm.get("t") == "integer" and type(value) is int and value >= 0 else 0
        last = lp.get("v") if lp.get("t") == "text" and isinstance(lp.get("v"), str) and lp.get("v") else None
        r = sessions.get(wid)
        seconds, count, tracked = (r[1], r[2], r[3]) if r else (0, 0, None)
        result[wid] = Playtime(wid, legacy_seconds, seconds, legacy_seconds + seconds, count,
                               tracked or last, "session" if tracked else "legacy" if last else None)
    return result


def playtime(lib, work_id: int) -> Playtime:
    all_times = list_playtimes(lib)
    if work_id not in all_times:
        raise KeyError(work_id)
    return all_times[work_id]


def daily_totals(lib, first_day: str, last_day: str) -> list[DayTotal]:
    first, last = _day(first_day), _day(last_day)
    span = (last - first).days
    if not 0 <= span <= 366:
        raise ValueError("Expected an ascending range spanning at most 366 days")
    with lib.connection() as c:
        tracked = dict(c.execute("SELECT local_day,SUM(seconds) FROM play_sessions WHERE state!='running' "
                                "AND local_day BETWEEN ? AND ? GROUP BY local_day", (first_day, last_day))) \
            if lib.schema_version >= 2 else {}
        legacy = dict(c.execute("SELECT day,SUM(minutes)*60 FROM legacy_play_logs WHERE minutes>=0 "
                               "AND day BETWEEN ? AND ? GROUP BY day", (first_day, last_day)))
    days = [(first + timedelta(days=i)).isoformat() for i in range(span + 1)]
    return [DayTotal(d, tracked.get(d, 0), legacy.get(d, 0)) for d in days]


def format_duration(seconds: int) -> str:
    if seconds < 0:
        raise ValueError("Negative duration")
    seconds = int(seconds)
    if seconds == 0:
        return "0 分"
    if seconds < 60:
        return "不到 1 分"
    minutes = seconds // 60
    return f"{minutes} 分" if seconds < 3600 else f"{minutes // 60} 小时 {minutes % 60:02d} 分"
