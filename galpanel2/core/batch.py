"""Batch metadata queue. Search results stay in a sidecar file until the user confirms."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict
from pathlib import Path
import tempfile
import threading
import time

from . import records as R
from .domain import Cancelled, SiteError, SiteUnreachable, utc_now

UNREACHABLE_LIMIT = 3
SKIPPED_TEXT = "连不上，已跳过"


def queue_path(lib_path: str) -> str:
    return lib_path + ".m2queue.json"


def candidate_dict(candidate: R.Candidate) -> dict:
    return {"site": candidate.record.site, "site_id": candidate.record.site_id,
            "grade": candidate.grade, "score": candidate.score, "record": asdict(candidate.record)}


def record_from_dict(data: dict) -> R.Record:
    brands = tuple(R.SiteBrand(b["site"], b.get("site_id"), b.get("name") or "", tuple(b.get("aliases") or ()))
                   for b in data.get("brands") or ())
    tags = tuple(R.SiteTag(t["site"], t["tag_key"], t.get("text") or "", t.get("suggestion") or "", t.get("category"))
                 for t in data.get("tags") or ())
    return R.Record(data["site"], data["site_id"], data.get("url") or "", data.get("title") or "",
                    data.get("translated_title"), tuple(data.get("alt_titles") or ()), brands,
                    data.get("release_date"), data.get("summary"), data.get("cover_url"), tags,
                    int(data.get("skipped_tags") or 0), data.get("extra") or "", bool(data.get("complete")),
                    data.get("payload") or "{}", data.get("cover_fallback"))


def skipped_notice(skipped) -> str:
    names = [R.SITE_LABEL[s] for s in R.SITES if s in skipped]
    if not names:
        return ""
    return (f"{'、'.join(names)} 连续 {UNREACHABLE_LIMIT} 部连不上，这一轮不再搜它。"
            "检查网络或代理后，暂停再继续会重新尝试。")


class Reachability:
    def __init__(self, limit: int = UNREACHABLE_LIMIT):
        self.limit = limit
        self._streak = {site: 0 for site in R.SITES}
        self._skipped: set[str] = set()

    @property
    def skipped(self) -> frozenset[str]:
        return frozenset(self._skipped)

    def sites(self) -> tuple[str, ...]:
        return tuple(site for site in R.SITES if site not in self._skipped)

    def record(self, item: dict) -> list[str]:
        asked = list(item.get("sites") or [])
        bad = set(item.get("unreachable") or [])
        newly = []
        for site in R.SITES:
            if site not in asked:
                continue
            if site in bad:
                self._streak[site] = self._streak.get(site, 0) + 1
                if self._streak[site] >= self.limit and site not in self._skipped:
                    self._skipped.add(site)
                    newly.append(site)
            else:
                self._streak[site] = 0
        return newly


class QueueStore:
    def __init__(self, lib_path: str, library_uuid: str):
        self.lib_path = lib_path
        self.library_uuid = library_uuid
        self.path = queue_path(lib_path)
        self._lock = threading.Lock()

    def load(self) -> dict | None:
        with self._lock:
            data = self._read()
            return copy.deepcopy(data) if data is not None else None

    def start(self, work_ids: list[int], refs=None) -> None:
        data = {"version": 1, "library_uuid": self.library_uuid, "created_at": utc_now(),
                "state": "running", "todo": list(work_ids), "items": []}
        if refs:
            data["refs"] = {str(key): value for key, value in refs.items()}
        with self._lock:
            self._write(data)

    def next_todo(self) -> int | None:
        with self._lock:
            data = self._read()
            if not data or not data.get("todo"):
                return None
            return data["todo"][0]

    def put_item(self, item: dict) -> None:
        with self._lock:
            data = self._read()
            if data is None:
                return
            wid = item["work_id"]
            data["todo"] = [w for w in data["todo"] if w != wid]
            data["items"] = [x for x in data["items"] if x.get("work_id") != wid]
            data["items"].append(item)
            self._write(data)

    def set_status(self, work_id: int, status: str, note: str = "") -> None:
        with self._lock:
            data = self._read()
            if data is None:
                return
            for item in data["items"]:
                if item.get("work_id") == work_id:
                    item["status"] = status
                    item["note"] = note
            self._write(data)

    def drop(self, work_ids) -> None:
        gone = set(work_ids)
        with self._lock:
            data = self._read()
            if data is None:
                return
            data["todo"] = [w for w in data["todo"] if w not in gone]
            data["items"] = [x for x in data["items"] if x.get("work_id") not in gone]
            refs = data.get("refs")
            if isinstance(refs, dict):
                for wid in gone:
                    refs.pop(str(wid), None)
            self._write(data)

    def set_state(self, state: str) -> None:
        with self._lock:
            data = self._read()
            if data is None:
                return
            data["state"] = state
            self._write(data)

    def snapshot(self) -> dict:
        with self._lock:
            data = self._read()
            if data is None:
                return {"version": 1, "library_uuid": self.library_uuid, "created_at": "",
                        "state": "done", "todo": [], "items": []}
            return copy.deepcopy(data)

    def _read(self) -> dict | None:
        if not os.path.exists(self.path):
            return None
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            self._sideline()
            return None
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("items"), list) \
                or not isinstance(data.get("todo"), list):
            self._sideline()
            return None
        if data.get("library_uuid") != self.library_uuid:
            return None
        return data

    def _sideline(self) -> None:
        if not os.path.exists(self.path):
            return
        dest = f"{self.path}.bad-{time.time_ns()}"
        os.replace(self.path, dest)

    def _write(self, data: dict) -> None:
        directory = str(Path(self.path).parent)
        fd, temp = tempfile.mkstemp(prefix=".m2queue-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
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


def _code_ref(title: str, paths: list[str]):
    found = R.find_code(title)
    if found:
        return found
    for path in paths:
        found = R.find_code(path)
        if found:
            return found
    return None


def _status(candidates: list[dict], errors: dict) -> str:
    grades = [c["grade"] for c in candidates]
    if "ok" in grades:
        return "ok"
    if "near" in grades:
        return "near"
    if all(site in errors for site in R.SITES):
        return "error"
    return "none"


def _pack(status: str, candidates: list[dict], errors: dict, sites: list[str], unreachable: list[str]) -> dict:
    return {"status": status, "candidates": candidates, "errors": errors,
            "sites": sites, "unreachable": unreachable}


def _top_per_site(ranked: list[R.Candidate]) -> list[dict]:
    kept, seen = [], set()
    for candidate in ranked:
        if candidate.record.site in seen:
            continue
        seen.add(candidate.record.site)
        kept.append(candidate_dict(candidate))
    return kept


def search_one(client, title: str, paths: list[str], sites=R.SITES, ref=None) -> dict:
    selected = tuple(sites)
    if ref is None:
        ref = _code_ref(title, paths)
    if ref:
        if ref.site not in selected:
            return _pack("error", [], {ref.site: SKIPPED_TEXT}, [], [])
        try:
            record = client.fetch(ref)
        except Cancelled:
            raise
        except SiteUnreachable as error:
            return _pack("error", [], {ref.site: str(error)}, [ref.site], [ref.site])
        except SiteError as error:
            return _pack("error", [], {ref.site: str(error)}, [ref.site], [])
        found = candidate_dict(R.Candidate(record, "ok", 1.0))
        return _pack("ok", [found], {}, [ref.site], [])

    asked = [site for site in R.SITES if site in selected]
    errors = {site: SKIPPED_TEXT for site in R.SITES if site not in selected}
    if not asked:
        return _pack("error", [], errors, [], [])
    missed: set[str] = set()
    try:
        ranked, site_errors = client.search_all(R.search_text(title), sites=asked, unreachable=missed)
    except Cancelled:
        raise
    except SiteUnreachable as error:
        site = error.site if error.site in asked else None
        if site:
            errors[site] = str(error)
        return _pack("error", [], errors, [site] if site else [], [site] if site else [])
    errors.update(site_errors)
    candidates = _top_per_site(ranked)
    unreachable = [site for site in asked if site in missed]
    return _pack(_status(candidates, errors), candidates, errors, asked, unreachable)


def confirm_set(item: dict) -> list[dict]:
    if item.get("status") in ("none", "error"):
        return []
    candidates = list(item.get("candidates") or [])
    first: dict[str, dict] = {}
    for candidate in candidates:
        first.setdefault(candidate["site"], candidate)
    oks = [candidate for candidate in first.values() if candidate["grade"] == "ok"]
    if oks:
        return [candidate for candidate in candidates if candidate["grade"] == "ok" and first.get(candidate["site"]) is candidate]
    if candidates and candidates[0]["grade"] == "near":
        return [candidates[0]]
    return []


def is_sure(item: dict) -> bool:
    if item.get("status") != "ok":
        return False
    counts: dict[str, int] = {}
    for candidate in item.get("candidates") or []:
        if candidate["grade"] == "ok":
            counts[candidate["site"]] = counts.get(candidate["site"], 0) + 1
    return bool(counts) and all(n <= 1 for n in counts.values())
