"""Offline metadata. Every mutation is atomic and composes with Library savepoints."""
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
import unicodedata
from typing import Iterable

from .domain import DuplicateName, WriteConflict, utc_now

NO_BRAND = 0
IGNORE_WORDS = frozenset({"windows","win","pc","mac","linux","android","ios","x64","x86",
                        "64bit","32bit","data","game","demo","trial","full","hd","fhd","uhd","hdr",
                        "steam","dlsite","fanza","dmm","体験版","体验版","汉化","汉化版","中文",
                        "中文版","日文","官中","生肉","熟肉","无修","無修正","rpg","adv","slg","avg"})


@dataclass(frozen=True)
class Brand:
    id: int
    name: str
    aliases: tuple[str, ...]
    work_count: int


@dataclass(frozen=True)
class TagGroup:
    id: int
    name: str
    position: int


@dataclass(frozen=True)
class Tag:
    id: int
    name: str
    group_id: int
    work_count: int


@dataclass(frozen=True)
class WorkMeta:
    work_id: int
    brand_ids: tuple[int, ...]
    tag_ids: tuple[int, ...]


@dataclass(frozen=True)
class MetaFilter:
    brands_in: frozenset[int] = frozenset()
    brands_out: frozenset[int] = frozenset()
    tags_in: frozenset[int] = frozenset()
    tags_out: frozenset[int] = frozenset()

    def is_empty(self) -> bool:
        return not (self.brands_in or self.brands_out or self.tags_in or self.tags_out)


@dataclass(frozen=True)
class DeveloperText:
    text_key: str
    text: str
    cleaned: str
    work_ids: tuple[int, ...]
    suggestion: str
    brand_id: int | None
    decided: str | None
    decided_brand_id: int | None


@dataclass(frozen=True)
class DevDecision:
    text_key: str
    action: str
    brand_name: str | None = None
    brand_id: int | None = None


@dataclass(frozen=True)
class ApplyResult:
    created_brands: int
    linked_works: int
    ignored_texts: int
    aliases_added: int


def name_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def _name(text):
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > 100 or \
            any(ord(ch) < 32 for ch in text):
        raise ValueError("Expected a nonempty name of at most 100 characters without controls")
    return " ".join(text.split())


@contextmanager
def _write(lib):
    with lib.transaction() as c:
        if lib.schema_version < 3:
            raise ValueError("Library needs version 3")
        yield c


def _row(c, table, item_id):
    r = c.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if r is None:
        raise KeyError(item_id)
    return r


def _brand_owner(c, key):
    r = c.execute("SELECT id FROM brands WHERE name_key=? UNION ALL "
                  "SELECT brand_id FROM brand_aliases WHERE alias_key=?", (key, key)).fetchone()
    return r[0] if r else None


def _claim(c, key, allowed=()):
    owner = _brand_owner(c, key)
    if owner is not None and owner not in allowed:
        raise DuplicateName("Name or alias already exists", existing_id=owner)


def _aliases(c, brand_id, name, aliases):
    unique = {}
    for alias in aliases:
        alias = _name(alias)
        key = name_key(alias)
        if key != name_key(name):
            unique.setdefault(key, alias)
    for key in unique:
        _claim(c, key, (brand_id,))
    return unique


def list_brands(lib) -> list[Brand]:
    if lib.schema_version < 3:
        return []
    with lib.connection() as c:
        aliases = defaultdict(list)
        for r in c.execute("SELECT brand_id,alias FROM brand_aliases ORDER BY alias_key"):
            aliases[r[0]].append(r[1])
        return [Brand(r[0], r[1], tuple(aliases[r[0]]), r[2]) for r in c.execute(
            "SELECT b.id,b.name,count(w.work_id) AS n FROM brands b "
            "LEFT JOIN work_brands w ON w.brand_id=b.id GROUP BY b.id ORDER BY n DESC,b.name_key")]


def find_brand(lib, text: str) -> Brand | None:
    if lib.schema_version < 3:
        return None
    with lib.connection() as c:
        item_id = _brand_owner(c, name_key(text))
    return next((b for b in list_brands(lib) if b.id == item_id), None)


def create_brand(lib, name: str, aliases: Iterable[str] = ()) -> int:
    with _write(lib) as c:
        name = _name(name)
        _claim(c, name_key(name))
        # Validate the entire set before any insert, including cross-table ownership.
        cleaned = _aliases(c, None, name, aliases)
        item_id = c.execute("INSERT INTO brands(name,name_key,created_at) VALUES(?,?,?)",
                            (name, name_key(name), utc_now())).lastrowid
        c.executemany("INSERT INTO brand_aliases VALUES(?,?,?)",
                      [(item_id, a, k) for k, a in cleaned.items()])
        return item_id


def rename_brand(lib, brand_id: int, name: str) -> None:
    with _write(lib) as c:
        _row(c, "brands", brand_id)
        name = _name(name)
        key = name_key(name)
        _claim(c, key, (brand_id,))
        c.execute("DELETE FROM brand_aliases WHERE brand_id=? AND alias_key=?", (brand_id, key))
        c.execute("UPDATE brands SET name=?,name_key=? WHERE id=?", (name, key, brand_id))


def set_brand_aliases(lib, brand_id: int, aliases: Iterable[str]) -> None:
    with _write(lib) as c:
        b = _row(c, "brands", brand_id)
        cleaned = _aliases(c, brand_id, b["name"], aliases)
        c.execute("DELETE FROM brand_aliases WHERE brand_id=?", (brand_id,))
        c.executemany("INSERT INTO brand_aliases VALUES(?,?,?)", [(brand_id, a, k) for k, a in cleaned.items()])


def _link(c, work_id, brand_id):
    if c.execute("SELECT 1 FROM work_brands WHERE work_id=? AND brand_id=?", (work_id, brand_id)).fetchone():
        return False
    pos = c.execute("SELECT coalesce(max(position),-1)+1 FROM work_brands WHERE work_id=?", (work_id,)).fetchone()[0]
    c.execute("INSERT INTO work_brands VALUES(?,?,?)", (work_id, brand_id, pos))
    return True


def merge_brands(lib, source_id: int, target_id: int) -> None:
    with _write(lib) as c:
        if source_id == target_id:
            raise ValueError("Cannot merge a brand into itself")
        source, target = _row(c, "brands", source_id), _row(c, "brands", target_id)
        aliases = [(source["name"], source["name_key"])] + [(r[0], r[1]) for r in c.execute(
            "SELECT alias,alias_key FROM brand_aliases WHERE brand_id=?", (source_id,))]
        for _, key in aliases:
            _claim(c, key, (source_id, target_id))
        for r in c.execute("SELECT work_id FROM work_brands WHERE brand_id=?", (source_id,)).fetchall():
            _link(c, r[0], target_id)
        c.execute("DELETE FROM brand_aliases WHERE brand_id=?", (source_id,))
        for alias, key in aliases:
            if key != target["name_key"]:
                c.execute("INSERT OR IGNORE INTO brand_aliases VALUES(?,?,?)", (target_id, alias, key))
        c.execute("UPDATE brand_decisions SET brand_id=? WHERE brand_id=?", (target_id, source_id))
        if lib.schema_version >= 4:
            c.execute("UPDATE brand_links SET brand_id=? WHERE brand_id=?", (target_id, source_id))
        c.execute("DELETE FROM brands WHERE id=?", (source_id,))


def delete_brand(lib, brand_id: int) -> None:
    with _write(lib) as c:
        _row(c, "brands", brand_id)
        if c.execute("SELECT 1 FROM work_brands WHERE brand_id=?", (brand_id,)).fetchone():
            raise ValueError("Brand still has works")
        c.execute("DELETE FROM brands WHERE id=?", (brand_id,))


def list_tag_groups(lib) -> list[TagGroup]:
    if lib.schema_version < 3:
        return []
    with lib.connection() as c:
        return [TagGroup(*r) for r in c.execute("SELECT id,name,position FROM tag_groups ORDER BY position")]


def list_tags(lib) -> list[Tag]:
    if lib.schema_version < 3:
        return []
    with lib.connection() as c:
        return [Tag(*r) for r in c.execute("SELECT t.id,t.name,t.group_id,count(w.work_id) AS n FROM tags t "
                 "JOIN tag_groups g ON g.id=t.group_id LEFT JOIN work_tags w ON w.tag_id=t.id "
                 "GROUP BY t.id ORDER BY g.position,n DESC,t.name_key")]


def _tag_claim(c, key, own=None):
    r = c.execute("SELECT id FROM tags WHERE name_key=?", (key,)).fetchone()
    if r and r[0] != own:
        raise DuplicateName("Tag already exists", existing_id=r[0])


def create_tag(lib, name: str, group_id: int) -> int:
    with _write(lib) as c:
        _row(c, "tag_groups", group_id)
        name = _name(name)
        _tag_claim(c, name_key(name))
        return c.execute("INSERT INTO tags(group_id,name,name_key,created_at) VALUES(?,?,?,?)",
                         (group_id, name, name_key(name), utc_now())).lastrowid


def rename_tag(lib, tag_id: int, name: str) -> None:
    with _write(lib) as c:
        _row(c, "tags", tag_id)
        name = _name(name)
        _tag_claim(c, name_key(name), tag_id)
        c.execute("UPDATE tags SET name=?,name_key=? WHERE id=?", (name, name_key(name), tag_id))


def set_tag_group(lib, tag_id: int, group_id: int) -> None:
    with _write(lib) as c:
        _row(c, "tags", tag_id)
        _row(c, "tag_groups", group_id)
        c.execute("UPDATE tags SET group_id=? WHERE id=?", (group_id, tag_id))


def merge_tags(lib, source_id: int, target_id: int) -> None:
    with _write(lib) as c:
        if source_id == target_id:
            raise ValueError("Cannot merge a tag into itself")
        _row(c, "tags", source_id)
        _row(c, "tags", target_id)
        c.execute("INSERT OR IGNORE INTO work_tags SELECT work_id,? FROM work_tags WHERE tag_id=?",
                  (target_id, source_id))
        if lib.schema_version >= 4:
            c.execute("UPDATE source_tags SET tag_id=? WHERE tag_id=?", (target_id, source_id))
            c.execute("INSERT OR IGNORE INTO work_tag_blocks SELECT work_id,? FROM work_tag_blocks WHERE tag_id=?", (target_id, source_id))
        c.execute("DELETE FROM tags WHERE id=?", (source_id,))


def delete_tag(lib, tag_id: int) -> None:
    with _write(lib) as c:
        _row(c, "tags", tag_id)
        if lib.schema_version >= 4:
            c.execute("UPDATE source_tags SET action=NULL,tag_id=NULL,decided_at=NULL WHERE tag_id=?", (tag_id,))
        c.execute("DELETE FROM tags WHERE id=?", (tag_id,))


def list_work_meta(lib) -> dict[int, WorkMeta]:
    with lib.connection() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM works ORDER BY id")]
        bs, ts = defaultdict(list), defaultdict(list)
        if lib.schema_version >= 3:
            for r in c.execute("SELECT work_id,brand_id FROM work_brands ORDER BY work_id,position"):
                bs[r[0]].append(r[1])
            for r in c.execute("SELECT w.work_id,t.id FROM work_tags w JOIN tags t ON t.id=w.tag_id "
                               "JOIN tag_groups g ON g.id=t.group_id ORDER BY w.work_id,g.position,t.name_key"):
                ts[r[0]].append(r[1])
        return {i: WorkMeta(i, tuple(bs[i]), tuple(ts[i])) for i in ids}


def set_work_meta(lib, work_id: int, expected_revision: int, brand_ids, tag_ids) -> int:
    with _write(lib) as c:
        w = _row(c, "works", work_id)
        bs, ts = list(dict.fromkeys(brand_ids)), list(dict.fromkeys(tag_ids))
        for i in bs:
            _row(c, "brands", i)
        for i in ts:
            _row(c, "tags", i)
        if w["revision"] != expected_revision:
            raise WriteConflict("Work changed or was removed")
        c.execute("DELETE FROM work_brands WHERE work_id=?", (work_id,))
        c.execute("DELETE FROM work_tags WHERE work_id=?", (work_id,))
        c.executemany("INSERT INTO work_brands VALUES(?,?,?)", [(work_id, i, p) for p, i in enumerate(bs)])
        c.executemany("INSERT INTO work_tags VALUES(?,?)", [(work_id, i) for i in ts])
        now = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        c.execute("UPDATE works SET revision=revision+1,updated_at=? WHERE id=?", (now, work_id))
        return expected_revision + 1


def matches(m: WorkMeta, f: MetaFilter) -> bool:
    bs, ts = set(m.brand_ids) or {NO_BRAND}, set(m.tag_ids)
    return (not f.brands_in or bool(bs & f.brands_in)) and not (bs & f.brands_out) and \
        f.tags_in <= ts and not (ts & f.tags_out)


def facet_counts(metas: Iterable[WorkMeta], f: MetaFilter) -> tuple[dict[int, int], dict[int, int]]:
    bc, tc = Counter(), Counter()
    brand_filter = replace(f, brands_in=frozenset())
    for m in metas:
        if matches(m, brand_filter):
            bc.update(set(m.brand_ids) or {NO_BRAND})
        if matches(m, f):
            tc.update(set(m.tag_ids))
    return dict(bc), dict(tc)


def clean_developer(text: str) -> str:
    s = " ".join(text.split())
    pairs = (("[", "]"), ("【", "】"), ("(", ")"), ("（", "）"))
    for left, right in pairs:
        if s.startswith(left) and s.endswith(right):
            s = s[1:-1].strip()
            break
    for left, right in (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】")):
        p = s.rfind(left)
        if p >= 0 and right not in s[p + 1:]:
            s = s[:p].rstrip(" -_・")
    return s


def developer_texts(lib) -> list[DeveloperText]:
    if lib.schema_version < 3:
        return []
    with lib.connection() as c:
        grouped = defaultdict(list)
        for r in c.execute("SELECT id,developer FROM works ORDER BY id"):
            if r[1].strip():
                grouped[name_key(clean_developer(r[1]))].append((r[0], r[1].strip()))
        decisions = {r[0]: (r[1], r[2]) for r in c.execute("SELECT text_key,action,brand_id FROM brand_decisions")}
        brands = {}
        for b in list_brands(lib):
            for n in (b.name, *b.aliases):
                brands[name_key(n)] = b.id
        result = []
        for key, rows in grouped.items():
            counts = Counter(tx for _, tx in rows)
            text = max(rows, key=lambda r: (counts[r[1]], -r[0]))[1]
            cleaned = clean_developer(text)
            ignore = len(key) < 2 or key in IGNORE_WORDS or \
                re.fullmatch(r"v?\d+([._]\d+)*[a-z]?", key) or re.fullmatch(r"\d+[kp]", key)
            target = None if ignore else brands.get(name_key(cleaned))
            action, decided_id = decisions.get(key, (None, None))
            result.append(DeveloperText(key, text, cleaned, tuple(r[0] for r in rows),
                          "ignore" if ignore else "merge" if target is not None else "new",
                          target, action, decided_id))
        return sorted(result, key=lambda t: (-len(t.work_ids), t.text_key))


def apply_developer_decisions(lib, decisions: list[DevDecision]) -> ApplyResult:
    with _write(lib) as c:
        texts = {t.text_key: t for t in developer_texts(lib)}
        seen, created = set(), {}
        nb = nw = ni = na = 0
        for d in decisions:
            if d.text_key not in texts or d.text_key in seen:
                raise ValueError("Unknown or repeated developer text")
            seen.add(d.text_key)
            t = texts[d.text_key]
            now = utc_now()
            if d.action == "ignore":
                c.execute("INSERT OR REPLACE INTO brand_decisions VALUES(?,?,'ignore',NULL,?)",
                          (t.text_key, t.text, now))
                ni += 1
                continue
            if d.action == "new":
                name = _name(d.brand_name)
                key = name_key(name)
                if key in created:
                    target = created[key]
                else:
                    target = create_brand(lib, name)
                    created[key] = target
                    nb += 1
            elif d.action == "merge":
                target = d.brand_id
                _row(c, "brands", target)
            else:
                raise ValueError("Unknown developer decision")
            b = _row(c, "brands", target)
            if t.cleaned and name_key(t.cleaned) != b["name_key"]:
                key = name_key(t.cleaned)
                _claim(c, key, (target,))
                if not c.execute("SELECT 1 FROM brand_aliases WHERE alias_key=?", (key,)).fetchone():
                    alias = _name(t.cleaned)
                    c.execute("INSERT INTO brand_aliases VALUES(?,?,?)", (target, alias, key))
                    na += 1
            for wid in t.work_ids:
                nw += _link(c, wid, target)
                c.execute("UPDATE works SET updated_at=? WHERE id=?", (now, wid))
            c.execute("INSERT OR REPLACE INTO brand_decisions VALUES(?,?,'brand',?,?)",
                      (t.text_key, t.text, target, now))
        return ApplyResult(nb, nw, ni, na)
