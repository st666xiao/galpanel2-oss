"""Atomic metadata binding and explicit, reusable source-tag decisions."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

from . import meta, repository
from .domain import DuplicateLink, DuplicateName, SiteError, WriteConflict, utc_now
from .records import Record

# Steam/F95 are a fallback for doujin titles with no VNDB/DLsite/Bangumi entry.
# Existing weights are multiplied by four so the 0.5 F95 slot stays an integer
# and both new sites stay strictly below vndb/dlsite/bangumi.
SUMMARY_PRIORITY = {"user": 36, "bangumi": 12, "dlsite": 8, "vndb": 4, "steam": 2, "f95": 1}
RELEASE_PRIORITY = {"vndb": 12, "dlsite": 8, "bangumi": 4, "steam": 2, "f95": 1}
GROUP_FOR_CATEGORY = {"ero": 4, "tech": 3, "cont": 1}

@dataclass(frozen=True)
class WorkInfo:
    work_id: int
    summary: str | None
    summary_source: str | None
    release_date: str | None
    release_source: str | None
    cover_source: str | None
    titles: tuple[tuple[str, str, str], ...]
    links: dict[str, str]

@dataclass(frozen=True)
class BindPlan:
    work_id: int
    site: str
    site_id: str
    replaces: str | None
    taken_by: int | None
    title_options: tuple[str, ...]
    brands: tuple[tuple[str, int | None, str], ...]
    release: str
    summary: str
    cover: str
    tags_mapped: tuple[int, ...]
    tags_pending: int
    tags_ignored: int
    tags_blocked: int
    skipped: int

@dataclass(frozen=True)
class SourceTag:
    site: str
    tag_key: str
    text: str
    suggestion: str
    category: str | None
    status: str
    tag_id: int | None
    work_count: int
    suggested_action: str
    suggested_tag_id: int | None
    suggested_group: int

@dataclass(frozen=True)
class TagDecision:
    site: str
    tag_key: str
    action: str
    tag_id: int | None = None
    name: str | None = None
    group_id: int | None = None

@dataclass(frozen=True)
class MapResult:
    mapped: int
    created_tags: int
    ignored: int
    works_tagged: int


def cover_urls(record: Record) -> tuple[str, ...]:
    """Primary cover first, then Steam's header image as a fallback."""
    urls = [record.cover_url] if record.cover_url else []
    fallback = getattr(record, "cover_fallback", None)
    if fallback and fallback not in urls:
        urls.append(fallback)
    return tuple(urls)


def download_cover(client, record: Record) -> tuple[bytes | None, str | None]:
    """Download the first working cover; a total failure still allows binding."""
    error = None
    for url in cover_urls(record):
        try:
            return client.image(url), None
        except SiteError as exc:
            error = str(exc)
    return None, error


@contextmanager
def _write(lib):
    with lib.transaction() as c:
        if lib.schema_version < 4:
            raise ValueError("Library needs version 4")
        yield c


def _valid(name):
    try:
        return meta._name(name)
    except ValueError:
        return None


def _titles(c, wid):
    return tuple(tuple(r) for r in c.execute(
        "SELECT title,kind,site FROM work_titles WHERE work_id=? ORDER BY "
        "CASE kind WHEN 'original' THEN 0 WHEN 'translated' THEN 1 ELSE 2 END,site,rowid", (wid,)))


def get_info(lib, work_id: int) -> WorkInfo:
    with lib.connection() as c:
        meta._row(c, "works", work_id)
        if lib.schema_version < 4:
            return WorkInfo(work_id, None, None, None, None, None, (), {})
        r = c.execute("SELECT summary,summary_source,release_date,release_source,cover_source FROM work_info WHERE work_id=?", (work_id,)).fetchone()
        links = dict(c.execute("SELECT site,site_id FROM external_links WHERE work_id=? ORDER BY site", (work_id,)))
        return WorkInfo(work_id, *(tuple(r) if r else (None,)*5), _titles(c, work_id), links)


def list_titles(lib) -> dict[int, tuple[str, ...]]:
    if lib.schema_version < 4:
        return {}
    with lib.connection() as c:
        return {r[0]: tuple(t[0] for t in _titles(c, r[0])) for r in c.execute("SELECT DISTINCT work_id FROM work_titles ORDER BY work_id")}


def list_links(lib) -> dict[int, dict[str, str]]:
    if lib.schema_version < 4:
        return {}
    with lib.connection() as c:
        out = {}
        for wid, site, sid in c.execute("SELECT work_id,site,site_id FROM external_links ORDER BY work_id,site"):
            out.setdefault(wid, {})[site] = sid
        return out


def _brand_choices(c, work_id, record):
    owners = dict(c.execute("SELECT name_key,id FROM brands"))
    owners.update(c.execute("SELECT alias_key,brand_id FROM brand_aliases"))
    links = {(r[0], r[1]): r[2] for r in c.execute("SELECT site,site_id,brand_id FROM brand_links")}
    names = dict(c.execute("SELECT id,name FROM brands"))
    attached = {r[0] for r in c.execute("SELECT brand_id FROM work_brands WHERE work_id=?", (work_id,))}
    result, seen = [], set()
    for b in record.brands:
        name = _valid(b.name)
        if not name:
            continue
        bid = links.get((b.site, b.site_id)) if b.site_id else None
        status = "linked" if bid is not None else "existing"
        if bid is None:
            bid = next((owners[meta.name_key(n)] for n in (name, *b.aliases) if _valid(n) and meta.name_key(n) in owners), None)
        if bid is None:
            bid = -(len(result) + 1)
            names[bid] = name
            status = "new"
        for n in (name, *b.aliases):
            if _valid(n):
                owners.setdefault(meta.name_key(n), bid)
        if b.site_id:
            links.setdefault((b.site, b.site_id), bid)
        if bid in seen:
            continue
        seen.add(bid)
        result.append((b, names[bid], bid if bid >= 0 else None, "attached" if bid in attached else status))
    return result


def _plan(lib, c, work_id, record):
    if not record.complete:
        raise ValueError("record is incomplete")
    w = meta._row(c, "works", work_id)
    info = get_info(lib, work_id)
    taken = c.execute("SELECT work_id FROM external_links WHERE site=? AND site_id=? AND work_id!=?", (record.site, record.site_id, work_id)).fetchone()
    old = info.links.get(record.site)
    options, keys = [], {meta.name_key(w["title"])}
    for title in (record.title, record.translated_title):
        if title and title.strip() and meta.name_key(title) not in keys:
            keys.add(meta.name_key(title))
            options.append(title)
    release = "none" if record.release_date is None else "set" if not info.release_date or RELEASE_PRIORITY[record.site] >= RELEASE_PRIORITY.get(info.release_source, 0) else "keep"
    summary = "none" if record.summary is None else "locked" if info.summary_source == "user" else "set" if not info.summary or SUMMARY_PRIORITY[record.site] >= SUMMARY_PRIORITY.get(info.summary_source, 0) else "keep"
    cover = "keep" if c.execute("SELECT 1 FROM covers WHERE work_id=?", (work_id,)).fetchone() else "none" if record.cover_url is None else "set"
    existing = {r[0] for r in c.execute("SELECT tag_id FROM work_tags WHERE work_id=?", (work_id,))}
    blocked = {r[0] for r in c.execute("SELECT tag_id FROM work_tag_blocks WHERE work_id=?", (work_id,))}
    mapped, pending, ignored, blocks, seen = [], 0, 0, set(), set()
    for t in record.tags:
        if (t.site, t.tag_key) in seen:
            continue
        seen.add((t.site, t.tag_key))
        r = c.execute("SELECT action,tag_id FROM source_tags WHERE site=? AND tag_key=?", (t.site, t.tag_key)).fetchone()
        if not r or r[0] is None or (r[0] == "map" and r[1] is None):
            pending += 1
        elif r[0] == "ignore":
            ignored += 1
        elif r[1] in existing:
            continue
        elif r[1] in blocked:
            blocks.add(r[1])
        elif r[1] not in mapped:
            mapped.append(r[1])
    brands = tuple((name, bid, status) for _, name, bid, status in _brand_choices(c, work_id, record))
    return BindPlan(work_id, record.site, record.site_id, old if old and old != record.site_id else None,
                    taken[0] if taken else None, tuple(options), brands, release, summary, cover,
                    tuple(mapped), pending, ignored, len(blocks), record.skipped_tags)


def plan_bind(lib, work_id: int, record: Record) -> BindPlan:
    if lib.schema_version < 4:
        raise ValueError("Library needs version 4")
    with lib.connection() as c:
        return _plan(lib, c, work_id, record)


def _info_row(c, work_id, now):
    c.execute("INSERT OR IGNORE INTO work_info(work_id,updated_at) VALUES(?,?)", (work_id, now))
    c.execute("UPDATE work_info SET updated_at=? WHERE work_id=?", (now, work_id))


def apply_bind(lib, work_id: int, expected_revision: int, record: Record,
               new_title: str | None = None, cover_bytes: bytes | None = None) -> int:
    with _write(lib) as c:
        plan = _plan(lib, c, work_id, record)
        if meta._row(c, "works", work_id)["revision"] != expected_revision:
            raise WriteConflict("Work changed or was removed")
        if plan.taken_by is not None:
            raise DuplicateLink("This entry is already bound to another work", work_id=plan.taken_by)
        if new_title is not None and new_title not in plan.title_options:
            raise ValueError("Title is not an option")
        now = utc_now()
        if plan.replaces:
            c.execute("DELETE FROM work_source_tags WHERE work_id=? AND site=?", (work_id, record.site))
        c.execute("INSERT INTO external_links VALUES(?,?,?,?) ON CONFLICT(work_id,site) DO UPDATE SET site_id=excluded.site_id,bound_at=excluded.bound_at", (work_id, record.site, record.site_id, now))
        c.execute("INSERT INTO metadata_snapshots(site,site_id,fetched_at,payload) VALUES(?,?,?,?)", (record.site, record.site_id, now, record.payload))
        if new_title is not None:
            c.execute("UPDATE works SET title=? WHERE id=?", (new_title, work_id))
        for kind, titles in (("original", (record.title,)), ("translated", (record.translated_title,)), ("alias", record.alt_titles)):
            for title in titles:
                cleaned = _valid(title)
                if cleaned:
                    c.execute("INSERT OR IGNORE INTO work_titles VALUES(?,?,?,?,?)", (work_id, cleaned, meta.name_key(cleaned), kind, record.site))
        # Resolve each brand against the current transaction, including earlier additions.
        for b in record.brands:
            name = _valid(b.name)
            if not name:
                continue
            row = c.execute("SELECT brand_id FROM brand_links WHERE site=? AND site_id=?", (b.site, b.site_id)).fetchone() if b.site_id else None
            bid = row[0] if row else next((owner for n in (name, *b.aliases) if _valid(n) and (owner := meta._brand_owner(c, meta.name_key(n))) is not None), None)
            if bid is None:
                aliases = [n for n in b.aliases if _valid(n) and meta._brand_owner(c, meta.name_key(n)) is None]
                bid = meta.create_brand(lib, name, aliases)
            stored = meta._row(c, "brands", bid)
            for alias in (name, *b.aliases):
                alias = _valid(alias)
                if alias and meta.name_key(alias) != stored["name_key"] and meta._brand_owner(c, meta.name_key(alias)) is None:
                    c.execute("INSERT INTO brand_aliases VALUES(?,?,?)", (bid, alias, meta.name_key(alias)))
            if b.site_id:
                c.execute("INSERT OR IGNORE INTO brand_links VALUES(?,?,?)", (b.site, b.site_id, bid))
            meta._link(c, work_id, bid)
        _info_row(c, work_id, now)
        if plan.release == "set":
            c.execute("UPDATE work_info SET release_date=?,release_source=? WHERE work_id=?", (record.release_date, record.site, work_id))
        if plan.summary == "set":
            c.execute("UPDATE work_info SET summary=?,summary_source=? WHERE work_id=?", (record.summary, record.site, work_id))
        if plan.cover == "set" and cover_bytes is not None:
            repository.image_extension(cover_bytes)
            fd, temp = tempfile.mkstemp(prefix=".gp2-bind-", dir=Path(lib.path).parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(cover_bytes)
                name, digest = repository.copy_cover(temp, lib.path + ".covers")
                c.execute("INSERT INTO covers VALUES(?,?,?,'user')", (work_id, name, digest))
                c.execute("UPDATE work_info SET cover_source=? WHERE work_id=?", (record.site, work_id))
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        for t in record.tags:
            c.execute("INSERT INTO source_tags(site,tag_key,text,suggestion,category) VALUES(?,?,?,?,?) ON CONFLICT(site,tag_key) DO UPDATE SET text=excluded.text,suggestion=excluded.suggestion,category=excluded.category", (t.site, t.tag_key, t.text, t.suggestion, t.category))
            c.execute("INSERT OR IGNORE INTO work_source_tags VALUES(?,?,?)", (work_id, t.site, t.tag_key))
        c.executemany("INSERT OR IGNORE INTO work_tags VALUES(?,?)", [(work_id, tid) for tid in plan.tags_mapped])
        c.execute("UPDATE works SET revision=revision+1,updated_at=? WHERE id=?", (now, work_id))
        return expected_revision + 1


def unbind(lib, work_id: int, site: str) -> None:
    with _write(lib) as c:
        if not c.execute("DELETE FROM external_links WHERE work_id=? AND site=?", (work_id, site)).rowcount:
            raise KeyError((work_id, site))
        c.execute("DELETE FROM work_source_tags WHERE work_id=? AND site=?", (work_id, site))


def set_summary(lib, work_id: int, text: str | None) -> None:
    with _write(lib) as c:
        meta._row(c, "works", work_id)
        _info_row(c, work_id, utc_now())
        c.execute("UPDATE work_info SET summary=?,summary_source='user' WHERE work_id=?", (text.strip() or None if text is not None else None, work_id))


def _blocks(lib, work_id, tag_ids, add):
    with _write(lib) as c:
        meta._row(c, "works", work_id)
        for tid in dict.fromkeys(tag_ids):
            meta._row(c, "tags", tid)
            if add:
                c.execute("INSERT OR IGNORE INTO work_tag_blocks VALUES(?,?)", (work_id, tid))
            else:
                c.execute("DELETE FROM work_tag_blocks WHERE work_id=? AND tag_id=?", (work_id, tid))


def block_tags(lib, work_id: int, tag_ids) -> None:
    _blocks(lib, work_id, tag_ids, True)


def unblock_tags(lib, work_id: int, tag_ids) -> None:
    _blocks(lib, work_id, tag_ids, False)


def list_source_tags(lib) -> list[SourceTag]:
    if lib.schema_version < 4:
        return []
    with lib.connection() as c:
        tags = dict(c.execute("SELECT name_key,id FROM tags"))
        result = []
        for r in c.execute("SELECT s.*,count(w.work_id) AS n FROM source_tags s LEFT JOIN work_source_tags w ON w.site=s.site AND w.tag_key=s.tag_key GROUP BY s.site,s.tag_key"):
            suggested = next((tags[meta.name_key(t)] for t in (r["suggestion"], r["text"]) if meta.name_key(t) in tags), None)
            status = r["action"] or "pending"
            if status == "map" and r["tag_id"] is None:
                status = "pending"
            result.append(SourceTag(r["site"], r["tag_key"], r["text"], r["suggestion"], r["category"], status,
                          r["tag_id"], r["n"], "map" if suggested is not None else "new", suggested,
                          GROUP_FOR_CATEGORY.get(r["category"], 5) if r["site"] == "vndb" else 5))
        return sorted(result, key=lambda r: (r.status != "pending", -r.work_count, r.site, r.tag_key))


def apply_tag_decisions(lib, decisions: list[TagDecision]) -> MapResult:
    with _write(lib) as c:
        seen, created = set(), {}
        mapped = ignored = works = 0
        for d in decisions:
            key = (d.site, d.tag_key)
            if key in seen:
                raise ValueError("Repeated source tag")
            seen.add(key)
            if not c.execute("SELECT 1 FROM source_tags WHERE site=? AND tag_key=?", key).fetchone():
                raise KeyError(key)
            tid, now = d.tag_id, utc_now()
            if d.action == "new":
                name = meta._name(d.name)
                meta._row(c, "tag_groups", d.group_id)
                nk = meta.name_key(name)
                if nk in created:
                    tid = created[nk]
                else:
                    tid = meta.create_tag(lib, name, d.group_id)
                    created[nk] = tid
            if d.action in ("map", "new"):
                meta._row(c, "tags", tid)
                c.execute("UPDATE source_tags SET action='map',tag_id=?,decided_at=? WHERE site=? AND tag_key=?", (tid, now, *key))
                for (wid,) in c.execute("SELECT work_id FROM work_source_tags WHERE site=? AND tag_key=?", key).fetchall():
                    if not c.execute("SELECT 1 FROM work_tag_blocks WHERE work_id=? AND tag_id=?", (wid, tid)).fetchone():
                        count = c.execute("INSERT OR IGNORE INTO work_tags VALUES(?,?)", (wid, tid)).rowcount
                        works += count
                        if count:
                            c.execute("UPDATE works SET updated_at=? WHERE id=?", (now, wid))
                mapped += 1
            elif d.action == "ignore":
                c.execute("UPDATE source_tags SET action='ignore',tag_id=NULL,decided_at=? WHERE site=? AND tag_key=?", (now, *key))
                ignored += 1
            elif d.action == "pending":
                c.execute("UPDATE source_tags SET action=NULL,tag_id=NULL,decided_at=NULL WHERE site=? AND tag_key=?", key)
            else:
                raise ValueError("Unknown tag decision")
        return MapResult(mapped, len(created), ignored, works)
