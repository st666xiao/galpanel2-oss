"""Merge another GalPanel 2 library into this one (read-only on the source).

The public API is ``plan_merge`` (pure preview) and ``merge_library`` (one
transaction, backed up first).  Only the standard library is used.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from . import meta, repository, store, tidy
from .domain import Cancelled, GalPanelError, path_key, utc_now

# ``library_info``, ``scan_runs``, ``tidy_log``, ``ignored_paths``,
# ``brand_decisions`` and the legacy/import tables keep the destination's own
# history and are deliberately not merged (see source_tables_note()).


class MergeError(GalPanelError):
    """A merge was refused; the message can be shown to the user."""


@dataclass(frozen=True)
class MergePlan:
    works_created: int
    copies_attached: int
    copies_skipped: int
    sources_added: int
    covers_copied: int
    merged_into: tuple[tuple[str, str], ...] = ()
    new_sources: tuple[tuple[str, str], ...] = ()  # (path, label)
    covers_missing: int = 0

    # Friendly aliases for callers that use the wording from the task book.
    @property
    def new_works(self) -> int:
        return self.works_created

    @property
    def attached_copies(self) -> int:
        return self.copies_attached

    @property
    def skipped_copies(self) -> int:
        return self.copies_skipped

    @property
    def covers(self) -> int:
        return self.covers_copied


@dataclass(frozen=True)
class MergeResult:
    works_created: int
    copies_attached: int
    copies_skipped: int
    sources_added: int
    covers_copied: int
    backup_path: str
    integrity_check: str
    merged_into: tuple[tuple[str, str], ...] = ()
    new_sources: tuple[tuple[str, str], ...] = ()
    covers_missing: int = 0
    brands_created: int = 0
    tags_created: int = 0

    @property
    def new_works(self) -> int:
        return self.works_created

    @property
    def attached_copies(self) -> int:
        return self.copies_attached

    @property
    def skipped_copies(self) -> int:
        return self.copies_skipped

    @property
    def covers(self) -> int:
        return self.covers_copied


# ------------------------------------------------------------------ source open
@contextmanager
def _source_library(src_path: str):
    """Open the source read-only; an older schema is upgraded on a temp copy."""
    src_path = str(Path(src_path).absolute())
    if not os.path.isfile(src_path):
        raise MergeError(f"找不到要合并的库：{src_path}")
    lib = store.open_library(src_path, readonly=True)
    lib.covers_path = src_path + ".covers"
    if lib.schema_version == store.SCHEMA_VERSION:
        try:
            yield lib
        finally:
            lib.close()
        return
    version = lib.schema_version
    lib.close()
    with tempfile.TemporaryDirectory(prefix=".gp2-merge-") as temp_dir:
        copy = os.path.join(temp_dir, os.path.basename(src_path))
        shutil.copy2(src_path, copy)
        try:
            store.upgrade_library(copy)
        except Exception as error:  # noqa: BLE001 - reported as a merge refusal
            raise MergeError(f"源库是第 {version} 版，临时升级失败：{error}") from error
        temp_lib = store.open_library(copy, readonly=True)
        temp_lib.covers_path = src_path + ".covers"
        try:
            yield temp_lib
        finally:
            temp_lib.close()


def _table_exists(c, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)).fetchone() is not None


def _same_path(a: str, b: str) -> bool:
    def key(path):
        try:
            return path_key(path)
        except (TypeError, ValueError):
            return os.path.normcase(os.path.abspath(str(path)))
    return key(a) == key(b)


# ------------------------------------------------------------------ decisions
@dataclass(frozen=True)
class _TargetState:
    links: dict[tuple[str, str], int]
    work_links: dict[int, dict[str, str]]
    copy_keys: dict[str, tuple[int, int]]
    uuids: set[str]
    titles: dict[int, str]
    source_keys: dict[str, int]


@dataclass(frozen=True)
class _CopyDecision:
    copy: dict
    existing_id: int | None


@dataclass(frozen=True)
class _WorkDecision:
    work: dict
    target_work_id: int | None
    links_to_add: tuple[tuple[str, str, str], ...]
    copies: tuple[_CopyDecision, ...]
    cover: dict | None


def _target_state(lib) -> _TargetState:
    with lib.connection() as c:
        links = {(r[0], r[1]): int(r[2]) for r in
                 c.execute("SELECT site,site_id,work_id FROM external_links")}
        work_links: dict[int, dict[str, str]] = {}
        for site, site_id, work_id in c.execute(
                "SELECT site,site_id,work_id FROM external_links"):
            work_links.setdefault(int(work_id), {})[site] = site_id
        copy_keys = {r[0]: (int(r[1]), int(r[2])) for r in
                     c.execute("SELECT path_key,id,work_id FROM copies")}
        uuids = {r[0] for r in c.execute("SELECT uuid FROM works")}
        titles = {int(r[0]): r[1] for r in c.execute("SELECT id,title FROM works")}
        source_keys = {r[0]: int(r[1]) for r in
                       c.execute("SELECT path_key,id FROM sources")}
    return _TargetState(links, work_links, copy_keys, uuids, titles, source_keys)


def _source_rows(lib, sql: str, params=()):
    with lib.connection() as c:
        return [dict(r) for r in c.execute(sql, params)]


def _source_work_rows(lib):
    return _source_rows(lib, "SELECT id,uuid,title,developer,notes,hidden,added_at,updated_at,"
                            "revision FROM works ORDER BY id")


def _work_links(lib, work_id: int):
    return [(r["site"], r["site_id"], r["bound_at"]) for r in _source_rows(
        lib, "SELECT site,site_id,bound_at FROM external_links WHERE work_id=? "
             "ORDER BY site,site_id", (work_id,))]


def _work_copies(lib, work_id: int):
    return _source_rows(lib, "SELECT id,work_id,source_id,kind,path,path_key,exe_relpath,"
                             "size_bytes,availability,checked_at,origin,added_at FROM copies "
                             "WHERE work_id=? ORDER BY id", (work_id,))


def _work_cover(lib, work_id: int):
    rows = _source_rows(lib, "SELECT work_id,file_name,sha256,origin FROM covers "
                             "WHERE work_id=?", (work_id,))
    return rows[0] if rows else None


def _decisions(dst_lib, src_lib) -> list[_WorkDecision]:
    state = _target_state(dst_lib)
    out: list[_WorkDecision] = []
    for work in _source_work_rows(src_lib):
        wid = int(work["id"])
        links = _work_links(src_lib, wid)
        matches = sorted({state.links[(site, site_id)] for site, site_id, _ in links
                          if (site, site_id) in state.links})
        if len(matches) > 1:
            detail = "、".join(f"{site}:{site_id}" for site, site_id, _ in links)
            raise MergeError(f"源作品《{work['title']}》的外部链接分别属于目标库的不同作品"
                             f"（{detail}），无法安全合并")
        target = matches[0] if matches else None
        if target is None:
            add = tuple(links)
        else:
            existing_sites = state.work_links.get(target, {})
            add = tuple(link for link in links
                        if link[0] not in existing_sites
                        and (link[0], link[1]) not in state.links)
        copies = []
        for copy in _work_copies(src_lib, wid):
            existing = state.copy_keys.get(copy["path_key"])
            copies.append(_CopyDecision(copy, existing[0] if existing else None))
        out.append(_WorkDecision(work, target, add, tuple(copies),
                                 _work_cover(src_lib, wid)))
    return out


def _source_sources(lib):
    return _source_rows(lib, "SELECT id,path,path_key,label,added_at,last_scan_at "
                             "FROM sources ORDER BY id")


def _source_cover_file(lib, cover) -> str:
    directory = getattr(lib, "covers_path", lib.path + ".covers")
    return str(Path(directory) / str(cover["file_name"]))


def _plan_from_decisions(dst_lib, src_lib, decisions) -> MergePlan:
    state = _target_state(dst_lib)
    sources = _source_sources(src_lib)
    new_sources = tuple((s["path"], s["label"]) for s in sources
                        if s["path_key"] not in state.source_keys)
    works_created = sum(d.target_work_id is None for d in decisions)
    copies_skipped = sum(c.existing_id is not None for d in decisions for c in d.copies)
    copies_attached = sum(d.target_work_id is not None and c.existing_id is None
                          for d in decisions for c in d.copies)
    covers_copied = covers_missing = 0
    merged = []
    for d in decisions:
        if d.target_work_id is None:
            if d.cover is not None:
                if os.path.isfile(_source_cover_file(src_lib, d.cover)):
                    covers_copied += 1
                else:
                    covers_missing += 1
        else:
            merged.append((d.work["title"], state.titles.get(d.target_work_id,
                                                             d.work["title"])))
    return MergePlan(works_created, copies_attached, copies_skipped, len(new_sources),
                     covers_copied, tuple(merged), new_sources, covers_missing)


def plan_merge(dst_lib, src_path: str) -> MergePlan:
    """Preview a merge without changing either library."""
    if dst_lib.readonly:
        raise MergeError("这个库是只读打开的，不能合并")
    if _same_path(dst_lib.path, src_path):
        raise MergeError("不能把库合并到它自己")
    with _source_library(src_path) as src:
        return _plan_from_decisions(dst_lib, src, _decisions(dst_lib, src))


# ------------------------------------------------------------------ execution helpers
def _ensure_shelf(c):
    c.execute("""CREATE TABLE IF NOT EXISTS work_shelf(
      work_id INTEGER PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
      category TEXT CHECK(category IS NULL OR category IN ('日系','同人','欧美','其他')),
      status TEXT CHECK(status IS NULL OR status IN ('想玩','在玩','玩过','不玩了')),
      updated_at TEXT NOT NULL
    )""")


def _ensure_storage(c):
    c.execute("""CREATE TABLE IF NOT EXISTS copy_storage(
      copy_id INTEGER PRIMARY KEY REFERENCES copies(id) ON DELETE CASCADE,
      state TEXT NOT NULL CHECK(state IN ('冷藏','已删除')),
      home_path TEXT,
      updated_at TEXT NOT NULL
    )""")


def _brand_owner(c, key):
    row = c.execute("SELECT id FROM brands WHERE name_key=? UNION ALL "
                    "SELECT brand_id FROM brand_aliases WHERE alias_key=?",
                    (key, key)).fetchone()
    return int(row[0]) if row else None


def _resolve_brand(c, brand, cache: dict[int, int], counters: dict[str, int]) -> int:
    source_id = int(brand["id"])
    if source_id in cache:
        return cache[source_id]
    target = None
    for link in brand["links"]:
        row = c.execute("SELECT brand_id FROM brand_links WHERE site=? AND site_id=?",
                        (link[0], link[1])).fetchone()
        if row is not None:
            target = int(row[0])
            break
    if target is None:
        for name in (brand["name"], *brand["aliases"]):
            try:
                key = meta.name_key(name)
            except (TypeError, ValueError):
                continue
            target = _brand_owner(c, key)
            if target is not None:
                break
    if target is None:
        target = c.execute("INSERT INTO brands(name,name_key,created_at) VALUES(?,?,?)",
                           (brand["name"], meta.name_key(brand["name"]),
                            brand["created_at"] or utc_now())).lastrowid
        counters["brands"] += 1
    stored = c.execute("SELECT name_key FROM brands WHERE id=?", (target,)).fetchone()
    stored_key = stored[0] if stored is not None else None
    for alias in brand["aliases"]:
        try:
            key = meta.name_key(alias)
        except (TypeError, ValueError):
            continue
        if key == stored_key or _brand_owner(c, key) is not None:
            continue
        c.execute("INSERT INTO brand_aliases(brand_id,alias,alias_key) VALUES(?,?,?)",
                  (target, alias, key))
    for site, site_id in brand["links"]:
        if c.execute("SELECT 1 FROM brand_links WHERE site=? AND site_id=?",
                     (site, site_id)).fetchone() is None:
            c.execute("INSERT INTO brand_links(site,site_id,brand_id) VALUES(?,?,?)",
                      (site, site_id, target))
    cache[source_id] = int(target)
    return int(target)


def _ensure_group(c, name: str, position: int) -> int:
    row = c.execute("SELECT id FROM tag_groups WHERE name=?", (name,)).fetchone()
    if row is not None:
        return int(row[0])
    if not name.strip():
        row = c.execute("SELECT id FROM tag_groups WHERE name='其他'").fetchone()
        if row is not None:
            return int(row[0])
        name, position = "其他", 99
    return int(c.execute("INSERT INTO tag_groups(name,position) VALUES(?,?)",
                         (name, position)).lastrowid)


def _resolve_tag(c, tag, cache: dict[int, int], counters: dict[str, int]) -> int:
    source_id = int(tag["id"])
    if source_id in cache:
        return cache[source_id]
    row = c.execute("SELECT id FROM tags WHERE name_key=?", (tag["name_key"],)).fetchone()
    if row is not None:
        target = int(row[0])
    else:
        group = _ensure_group(c, tag["group_name"], int(tag["group_position"]))
        target = int(c.execute("INSERT INTO tags(group_id,name,name_key,created_at) "
                               "VALUES(?,?,?,?)",
                               (group, tag["name"], tag["name_key"],
                                tag["created_at"] or utc_now())).lastrowid)
        counters["tags"] += 1
    cache[source_id] = target
    return target


def _copy_cover(src_lib, dst_lib, cover, created_covers: list[Path]) -> tuple[str, str]:
    source = Path(_source_cover_file(src_lib, cover))
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    name = digest + "." + repository.image_extension(data)
    directory = Path(dst_lib.path + ".covers")
    existed = (directory / name).exists()
    name, digest = repository.copy_cover(source, directory)
    if not existed:
        created_covers.append(directory / name)
    return name, digest


def _mapped_tag(cache, tag_id):
    if tag_id is None:
        return None
    return cache.get(int(tag_id))


# ------------------------------------------------------------------ merge
def merge_library(dst_lib, src_path: str, *, cancel=None, progress=None) -> MergeResult:
    """Merge ``src_path`` into ``dst_lib`` in one transaction.

    The destination is backed up first.  The source file is never written; a
    failed merge rolls the destination database back and removes cover files it
    created.  The source library file is left in place.
    """
    if dst_lib.readonly:
        raise MergeError("这个库是只读打开的，不能合并")
    if _same_path(dst_lib.path, src_path):
        raise MergeError("不能把库合并到它自己")

    with _source_library(src_path) as src:
        decisions = _decisions(dst_lib, src)
        total = len(decisions)
        if cancel is not None and cancel():
            raise Cancelled("合并已取消")
        backup = tidy.backup_library(dst_lib, "pre-merge")

        # Read every source-side extra before the destination transaction so a
        # slow source query cannot hold the destination writer lock.
        source_sources = _source_sources(src)
        source_brands = {}
        for r in _source_rows(src, "SELECT id,name,name_key,created_at FROM brands"):
            source_brands[int(r["id"])] = {"id": r["id"], "name": r["name"],
                                           "name_key": r["name_key"],
                                           "created_at": r["created_at"],
                                           "aliases": [], "links": []}
        for r in _source_rows(src, "SELECT brand_id,alias FROM brand_aliases ORDER BY alias_key"):
            brand = source_brands.get(int(r["brand_id"]))
            if brand is not None:
                brand["aliases"].append(r["alias"])
        for r in _source_rows(src, "SELECT site,site_id,brand_id FROM brand_links"):
            brand = source_brands.get(int(r["brand_id"]))
            if brand is not None:
                brand["links"].append((r["site"], r["site_id"]))

        source_tags = {}
        for r in _source_rows(src, "SELECT t.id,t.name,t.name_key,t.created_at,t.group_id,"
                                   "g.name AS group_name,g.position AS group_position "
                                   "FROM tags t JOIN tag_groups g ON g.id=t.group_id"):
            source_tags[int(r["id"])] = r
        source_tag_rows = _source_rows(src, "SELECT site,tag_key,text,suggestion,category,action,"
                                            "tag_id,decided_at FROM source_tags")
        source_storage = {}
        if _table_exists_with(src, "copy_storage"):
            for r in _source_rows(src, "SELECT copy_id,state,home_path,updated_at FROM copy_storage"):
                source_storage[int(r["copy_id"])] = r
        source_launch = {int(r["copy_id"]): r for r in _source_rows(
            src, "SELECT copy_id,exe_relpath,args,updated_at FROM launch_options")}
        source_sessions = _source_rows(src, "SELECT work_id,copy_id,kind,state,local_day,"
                                            "started_at,last_beat_at,ended_at,seconds,end_reason,"
                                            "edited FROM play_sessions ORDER BY id")
        source_snapshots = {}
        for r in _source_rows(src, "SELECT site,site_id,fetched_at,payload FROM metadata_snapshots "
                                   "ORDER BY id"):
            source_snapshots.setdefault((r["site"], r["site_id"]), []).append(r)
        source_shelves = {}
        if _table_exists_with(src, "work_shelf"):
            source_shelves = {int(r["work_id"]): r for r in _source_rows(
                src, "SELECT work_id,category,status,updated_at FROM work_shelf")}
        source_coll = {}
        for r in _source_rows(src, "SELECT work_id,site,tag_key FROM work_source_tags"):
            source_coll.setdefault(int(r["work_id"]), []).append((r["site"], r["tag_key"]))
        source_blocks = {}
        for r in _source_rows(src, "SELECT work_id,tag_id FROM work_tag_blocks"):
            source_blocks.setdefault(int(r["work_id"]), []).append(int(r["tag_id"]))
        source_work_brands = {}
        for r in _source_rows(src, "SELECT work_id,brand_id,position FROM work_brands "
                                   "ORDER BY work_id,position"):
            source_work_brands.setdefault(int(r["work_id"]), []).append(
                (int(r["brand_id"]), int(r["position"])))
        source_work_tags = {}
        for r in _source_rows(src, "SELECT work_id,tag_id FROM work_tags"):
            source_work_tags.setdefault(int(r["work_id"]), []).append(int(r["tag_id"]))
        source_infos = {int(r["work_id"]): r for r in _source_rows(
            src, "SELECT work_id,summary,summary_source,release_date,release_source,"
                 "cover_source,updated_at FROM work_info")}
        source_titles = {}
        for r in _source_rows(src, "SELECT work_id,title,title_key,kind,site FROM work_titles "
                                   "ORDER BY work_id,title_key"):
            source_titles.setdefault(int(r["work_id"]), []).append(r)

        created_covers: list[Path] = []
        counters = {"brands": 0, "tags": 0}
        result = None
        try:
            with dst_lib.transaction() as c:
                state = _target_state(dst_lib)
                source_map = {}
                new_sources = []
                for s in source_sources:
                    key = s["path_key"]
                    if key in state.source_keys:
                        source_map[int(s["id"])] = state.source_keys[key]
                    else:
                        sid = c.execute("INSERT INTO sources(path,path_key,label,added_at,"
                                        "last_scan_at) VALUES(?,?,?,?,?)",
                                        (s["path"], key, s["label"], s["added_at"],
                                         s["last_scan_at"])).lastrowid
                        source_map[int(s["id"])] = int(sid)
                        state.source_keys[key] = int(sid)
                        new_sources.append((s["path"], s["label"]))

                brand_cache: dict[int, int] = {}
                tag_cache: dict[int, int] = {}
                # Resolve source tags once so source-tag decisions keep their
                # target tag even for tags that are not attached to a work.
                for tag in source_tags.values():
                    _resolve_tag(c, tag, tag_cache, counters)
                existing_source_tags = {(r[0], r[1]) for r in
                                        c.execute("SELECT site,tag_key FROM source_tags")}
                for row in source_tag_rows:
                    key = (row["site"], row["tag_key"])
                    if key in existing_source_tags:
                        continue
                    tag_id = _mapped_tag(tag_cache, row["tag_id"])
                    c.execute("INSERT INTO source_tags(site,tag_key,text,suggestion,category,"
                              "action,tag_id,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                              (row["site"], row["tag_key"], row["text"], row["suggestion"],
                               row["category"], row["action"], tag_id, row["decided_at"]))
                    existing_source_tags.add(key)

                work_map: dict[int, int] = {}
                copy_map: dict[int, int] = {}
                merged_into = []
                works_created = copies_attached = copies_skipped = 0
                covers_copied = covers_missing = 0
                snapshot_seen = {(r[0], r[1], r[2]) for r in c.execute(
                    "SELECT site,site_id,fetched_at FROM metadata_snapshots")}

                for index, d in enumerate(decisions, 1):
                    if cancel is not None and cancel():
                        raise Cancelled("合并已取消")
                    work = d.work
                    source_wid = int(work["id"])
                    changed = False
                    if d.target_work_id is None:
                        target_uuid = work["uuid"]
                        if target_uuid in state.uuids:
                            target_uuid = str(uuid.uuid4())
                        state.uuids.add(target_uuid)
                        target_wid = int(c.execute(
                            "INSERT INTO works(uuid,title,developer,notes,hidden,added_at,"
                            "updated_at,revision) VALUES(?,?,?,?,?,?,?,?)",
                            (target_uuid, work["title"], work["developer"], work["notes"],
                             work["hidden"], work["added_at"], work["updated_at"],
                             work["revision"])).lastrowid)
                        works_created += 1
                        if d.cover is not None:
                            source_cover = _source_cover_file(src, d.cover)
                            if os.path.isfile(source_cover):
                                name, digest = _copy_cover(src, dst_lib, d.cover, created_covers)
                                c.execute("INSERT INTO covers(work_id,file_name,sha256,origin) "
                                          "VALUES(?,?,?,?)",
                                          (target_wid, name, digest, d.cover["origin"]))
                                covers_copied += 1
                            else:
                                covers_missing += 1
                        info = source_infos.get(source_wid)
                        if info is not None:
                            c.execute("INSERT INTO work_info(work_id,summary,summary_source,"
                                      "release_date,release_source,cover_source,updated_at) "
                                      "VALUES(?,?,?,?,?,?,?)",
                                      (target_wid, info["summary"], info["summary_source"],
                                       info["release_date"], info["release_source"],
                                       info["cover_source"], info["updated_at"]))
                        for title in source_titles.get(source_wid, ()):
                            c.execute("INSERT OR IGNORE INTO work_titles(work_id,title,title_key,"
                                      "kind,site) VALUES(?,?,?,?,?)",
                                      (target_wid, title["title"], title["title_key"],
                                       title["kind"], title["site"]))
                    else:
                        target_wid = int(d.target_work_id)
                        merged_into.append((work["title"], state.titles.get(target_wid,
                                                                           work["title"])))
                        changed = True
                    work_map[source_wid] = target_wid

                    # External links (unique per site and per site_id).
                    for site, site_id, bound_at in d.links_to_add:
                        if c.execute("SELECT 1 FROM external_links WHERE work_id=? AND site=?",
                                     (target_wid, site)).fetchone() is not None:
                            continue
                        if c.execute("SELECT 1 FROM external_links WHERE site=? AND site_id=?",
                                     (site, site_id)).fetchone() is not None:
                            continue
                        c.execute("INSERT INTO external_links(work_id,site,site_id,bound_at) "
                                  "VALUES(?,?,?,?)",
                                  (target_wid, site, site_id, bound_at))
                        changed = True

                    # Copies, launch options and cold/deleted storage state.
                    for copy_dec in d.copies:
                        source_copy = copy_dec.copy
                        if copy_dec.existing_id is not None:
                            copies_skipped += 1
                            copy_map[int(source_copy["id"])] = copy_dec.existing_id
                            continue
                        source_id = source_copy["source_id"]
                        mapped_source = source_map.get(int(source_id)) if source_id is not None else None
                        new_copy = int(c.execute(
                            "INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,"
                            "size_bytes,availability,checked_at,origin,added_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (target_wid, mapped_source, source_copy["kind"],
                             source_copy["path"], source_copy["path_key"],
                             source_copy["exe_relpath"], source_copy["size_bytes"],
                             source_copy["availability"], source_copy["checked_at"],
                             source_copy["origin"], source_copy["added_at"])).lastrowid)
                        copy_map[int(source_copy["id"])] = new_copy
                        if d.target_work_id is not None:
                            copies_attached += 1
                            changed = True
                        launch = source_launch.get(int(source_copy["id"]))
                        if launch is not None:
                            c.execute("INSERT INTO launch_options(copy_id,exe_relpath,args,"
                                      "updated_at) VALUES(?,?,?,?)",
                                      (new_copy, launch["exe_relpath"], launch["args"],
                                       launch["updated_at"]))
                        storage_row = source_storage.get(int(source_copy["id"]))
                        if storage_row is not None and storage_row["state"] in ("冷藏", "已删除"):
                            _ensure_storage(c)
                            home = storage_row["home_path"] if storage_row["state"] == "冷藏" else None
                            c.execute("INSERT INTO copy_storage(copy_id,state,home_path,updated_at) "
                                      "VALUES(?,?,?,?)",
                                      (new_copy, storage_row["state"], home,
                                       storage_row["updated_at"]))

                    # Brands.
                    for brand_id, position in source_work_brands.get(source_wid, ()):
                        source_brand = source_brands.get(brand_id)
                        if source_brand is None:
                            continue
                        target_brand = _resolve_brand(c, source_brand, brand_cache, counters)
                        if c.execute("SELECT 1 FROM work_brands WHERE work_id=? AND brand_id=?",
                                     (target_wid, target_brand)).fetchone() is not None:
                            continue
                        if d.target_work_id is None:
                            pos = position
                        else:
                            pos = c.execute("SELECT coalesce(max(position),-1)+1 FROM work_brands "
                                            "WHERE work_id=?", (target_wid,)).fetchone()[0]
                        c.execute("INSERT INTO work_brands(work_id,brand_id,position) "
                                  "VALUES(?,?,?)", (target_wid, target_brand, pos))
                        changed = True

                    # Tags.
                    for tag_id in source_work_tags.get(source_wid, ()):
                        source_tag = source_tags.get(tag_id)
                        if source_tag is None:
                            continue
                        target_tag = _resolve_tag(c, source_tag, tag_cache, counters)
                        if c.execute("SELECT 1 FROM work_tags WHERE work_id=? AND tag_id=?",
                                     (target_wid, target_tag)).fetchone() is None:
                            c.execute("INSERT INTO work_tags(work_id,tag_id) VALUES(?,?)",
                                      (target_wid, target_tag))
                            changed = True
                    for (site, tag_key) in source_coll.get(source_wid, ()):
                        if c.execute("SELECT 1 FROM source_tags WHERE site=? AND tag_key=?",
                                     (site, tag_key)).fetchone() is not None:
                            c.execute("INSERT OR IGNORE INTO work_source_tags(work_id,site,tag_key) "
                                      "VALUES(?,?,?)", (target_wid, site, tag_key))
                    for tag_id in source_blocks.get(source_wid, ()):
                        target_tag = tag_cache.get(tag_id)
                        if target_tag is not None:
                            if c.execute("SELECT 1 FROM work_tag_blocks WHERE work_id=? AND tag_id=?",
                                         (target_wid, target_tag)).fetchone() is None:
                                c.execute("INSERT INTO work_tag_blocks(work_id,tag_id) VALUES(?,?)",
                                          (target_wid, target_tag))
                                changed = True

                    # Shelf category/status: target values win per column.
                    shelf_row = source_shelves.get(source_wid)
                    if shelf_row is not None and (shelf_row["category"] is not None
                                                  or shelf_row["status"] is not None):
                        _ensure_shelf(c)
                        current = c.execute("SELECT category,status FROM work_shelf WHERE work_id=?",
                                            (target_wid,)).fetchone()
                        if current is None:
                            c.execute("INSERT INTO work_shelf(work_id,category,status,updated_at) "
                                      "VALUES(?,?,?,?)",
                                      (target_wid, shelf_row["category"], shelf_row["status"],
                                       shelf_row["updated_at"]))
                            changed = True
                        else:
                            category = current[0] if current[0] is not None else shelf_row["category"]
                            status = current[1] if current[1] is not None else shelf_row["status"]
                            if (category, status) != (current[0], current[1]):
                                c.execute("UPDATE work_shelf SET category=?,status=?,updated_at=? "
                                          "WHERE work_id=?",
                                          (category, status, shelf_row["updated_at"], target_wid))
                                changed = True

                    # Snapshots for every source link that is on the target work.
                    for site, site_id, _bound_at in _work_links(src, source_wid):
                        bound = c.execute("SELECT site_id FROM external_links WHERE work_id=? "
                                          "AND site=?", (target_wid, site)).fetchone()
                        if bound is None or bound[0] != site_id:
                            continue
                        for snapshot in source_snapshots.get((site, site_id), ()):
                            key = (site, site_id, snapshot["fetched_at"])
                            if key in snapshot_seen:
                                continue
                            snapshot_seen.add(key)
                            c.execute("INSERT INTO metadata_snapshots(site,site_id,fetched_at,payload) "
                                      "VALUES(?,?,?,?)",
                                      (site, site_id, snapshot["fetched_at"], snapshot["payload"]))

                    if d.target_work_id is not None and changed:
                        c.execute("UPDATE works SET revision=revision+1,updated_at=? WHERE id=?",
                                  (utc_now(), target_wid))
                    if progress is not None:
                        progress(index, total, work["title"])

                # Play sessions: the unique 'running' index is library-wide.
                running = c.execute("SELECT 1 FROM play_sessions WHERE state='running'").fetchone()
                for session in source_sessions:
                    wid = work_map.get(int(session["work_id"]))
                    if wid is None:
                        continue
                    state_value = session["state"]
                    reason = session["end_reason"]
                    ended = session["ended_at"]
                    if state_value == "running":
                        if running is not None:
                            state_value, reason = "recovered", "recovered"
                            ended = ended or session["last_beat_at"]
                        else:
                            running = (1,)
                    copy_id = copy_map.get(int(session["copy_id"])) \
                        if session["copy_id"] is not None else None
                    c.execute("INSERT INTO play_sessions(work_id,copy_id,kind,state,local_day,"
                              "started_at,last_beat_at,ended_at,seconds,end_reason,edited) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (wid, copy_id, session["kind"], state_value,
                               session["local_day"], session["started_at"],
                               session["last_beat_at"], ended, session["seconds"],
                               reason, session["edited"]))

                integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise MergeError(f"合并后完整性检查失败：{integrity}")
                result = MergeResult(
                    works_created, copies_attached, copies_skipped, len(new_sources),
                    covers_copied, backup, str(integrity), tuple(merged_into),
                    tuple(new_sources), covers_missing, counters["brands"], counters["tags"])
        except BaseException:
            for path in created_covers:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
            raise
        return result


def _table_exists_with(lib, name: str) -> bool:
    with lib.connection() as c:
        return _table_exists(c, name)


def source_tables_note() -> dict[str, str]:
    """A report-friendly description of every table's merge policy."""
    return {
        "library_info": "不合并；目标库身份保持",
        "sources": "按 path_key 去重，缺的插入并保留 label/last_scan_at",
        "works": "按 external_links 去重；无匹配则新建，uuid 冲突时新生成",
        "copies": "path_key 已存在则跳过；否则挂到目标作品并映射 source_id",
        "covers": "只为新作品复制，哈希命名；文件缺失/损坏则跳过并计数",
        "launch_options": "随新副本复制",
        "play_sessions": "随作品/副本映射复制；冲突的 running 会话记为 recovered",
        "brands": "先 brand_links(site,site_id)，再名字键；否则新建并带别名",
        "brand_aliases": "并入匹配到的品牌，别名键冲突时跳过",
        "work_brands": "按目标品牌关系补上，保留顺序；已有关系不重复",
        "tag_groups": "按同名分组匹配，缺的创建",
        "tags": "按 name_key 匹配，缺的建到同名分组",
        "work_tags": "按映射后的标签补上",
        "brand_links": "补齐来源品牌链接；已占用则保留目标",
        "work_info": "仅为新作品复制",
        "work_titles": "仅为新作品复制",
        "external_links": "合并到已有作品时，只补目标没有的站点；已有绑定保留",
        "metadata_snapshots": "随新绑定的站点链接复制，按 site/site_id/fetched_at 去重",
        "source_tags": "目标已有的 key 整行保留；缺的插入并映射 tag_id",
        "work_source_tags": "按作品映射补上",
        "work_tag_blocks": "按作品/标签映射补上",
        "work_shelf": "新作品复制；已有作品逐列只补空值",
        "copy_storage": "随新副本复制冷藏/已删除状态与 home_path",
        "scan_runs": "不合并（历史扫描记录）",
        "ignored_paths": "不合并（源库自己的忽略规则）",
        "tidy_log": "不合并（目标库自己的整理日志）",
        "brand_decisions": "不合并（来源库的整理决定）",
        "imports": "跳过（legacy 导入历史）",
        "legacy_fields": "跳过（legacy）",
        "legacy_play_logs": "跳过（legacy）",
        "legacy_sources": "跳过（legacy）",
    }
