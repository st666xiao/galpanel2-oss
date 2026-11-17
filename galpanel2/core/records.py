"""Pure, offline records and conservative title matching."""
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Iterable
import difflib
import html as html_module
import json
import re
import unicodedata

from . import names, meta
from .domain import SiteNotFound

SITES = ("dlsite", "vndb", "bangumi", "steam", "f95")
SITE_LABEL = {"dlsite": "DLsite", "vndb": "VNDB", "bangumi": "Bangumi",
              "steam": "Steam", "f95": "F95zone"}
SITE_ORDER = ("vndb", "bangumi", "dlsite", "steam", "f95")
STEAM_COVER = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900.jpg"

@dataclass(frozen=True)
class SiteBrand:
    site: str
    site_id: str | None
    name: str
    aliases: tuple[str, ...]

@dataclass(frozen=True)
class SiteTag:
    site: str
    tag_key: str
    text: str
    suggestion: str
    category: str | None

@dataclass(frozen=True)
class Record:
    site: str
    site_id: str
    url: str
    title: str
    translated_title: str | None
    alt_titles: tuple[str, ...]
    brands: tuple[SiteBrand, ...]
    release_date: str | None
    summary: str | None
    cover_url: str | None
    tags: tuple[SiteTag, ...]
    skipped_tags: int
    extra: str
    complete: bool
    payload: str
    cover_fallback: str | None = None

@dataclass(frozen=True)
class Candidate:
    record: Record
    grade: str
    score: float

    @property
    def site(self) -> str:
        return self.record.site

@dataclass(frozen=True)
class Ref:
    site: str
    site_id: str


def _text(value):
    return str(value).strip() or None if value is not None else None


def _date(value):
    s = str(value or "")
    return s if re.fullmatch(r"\d{4}-\d\d-\d\d", s) else None


def _unique(values, excluded=()):
    seen = {meta.name_key(v) for v in excluded if v}
    result = []
    for v in values:
        v = _text(v)
        if v and meta.name_key(v) not in seen:
            seen.add(meta.name_key(v))
            result.append(v)
    return tuple(result)


def parse_dlsite_suggest(data) -> list[Record]:
    return [Record("dlsite", x["workno"], f'https://www.dlsite.com/maniax/work/=/product_id/{x["workno"]}.html',
                   x["work_name"], None, (), (SiteBrand("dlsite", x["maker_id"], x["maker_name"], ()),),
                   None, _text(x.get("intro_s")), None, (), 0, x.get("work_type") or "", False,
                   json.dumps(x, ensure_ascii=False)) for x in data.get("work", [])]


def parse_dlsite_product(data) -> Record:
    if not data:
        raise SiteNotFound("DLsite 上没有这个条目", "dlsite")
    x = data[0]
    cover = (x.get("image_main") or {}).get("url")
    if cover and cover.startswith("//"):
        cover = "https:" + cover
    return Record("dlsite", x["workno"],
                  f'https://www.dlsite.com/{x.get("site_id") or "maniax"}/work/=/product_id/{x["workno"]}.html',
                  x["work_name"], None, (),
                  (SiteBrand("dlsite", x["maker_id"], x["maker_name"], _unique([x.get("maker_name_en")], [x["maker_name"]])),),
                  _date(str(x.get("regist_date") or "")[:10]), _text(x.get("intro_s")), cover,
                  tuple(SiteTag("dlsite", str(g["id"]), g["name"], g["name"], None) for g in x.get("genres", []) or []),
                  0, x.get("work_type_string") or "", True, json.dumps(x, ensure_ascii=False))


def clean_bbcode(text: str) -> str:
    return re.sub(r"\[/?(?:url(?:=[^\]]*)?|b|i|u|s|spoiler|quote|code|raw)\]", "", text or "", flags=re.I).strip()


@lru_cache(maxsize=1)
def _translations():
    try:
        return json.loads((Path(__file__).parent.parent / "resources/vndb-tags-translation.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def tag_translation(name: str) -> str:
    return _translations().get(name, name)


def parse_vndb(data) -> list[Record]:
    result = []
    for r in data.get("results", []) or []:
        title = r.get("alttitle") or r["title"]
        titles = r.get("titles") or []
        translated = next((_text(t.get("title")) for lang in ("zh-Hans", "zh-Hant") for t in titles
                           if t.get("lang") == lang and _text(t.get("title"))), None)
        raw_tags = r.get("tags") or []
        tags = [t for t in raw_tags if t.get("spoiler") == 0 and (t.get("rating") or 0) >= 1.5]
        tags.sort(key=lambda t: (-t["rating"], int(t["id"][1:])))
        result.append(Record("vndb", r["id"], f'https://vndb.org/{r["id"]}', title, translated,
                      _unique([r["title"], *(t.get("title") for t in titles)], [title, translated]),
                      tuple(SiteBrand("vndb", d["id"], d["name"],
                            _unique([d.get("original"), *(d.get("aliases") or [])], [d["name"]]))
                            for d in r.get("developers") or []),
                      _date(r.get("released")), _text(clean_bbcode(r.get("description"))),
                      (r.get("image") or {}).get("url"),
                      tuple(SiteTag("vndb", t["id"], t["name"], tag_translation(t["name"]), t.get("category")) for t in tags),
                      len(raw_tags) - len(tags), "", True, json.dumps(r, ensure_ascii=False)))
    return result


def parse_bangumi_subject(x) -> Record:
    info = {i["key"]: i.get("value") for i in x.get("infobox") or []}
    alt = info.get("别名") or []
    if isinstance(alt, str):
        alt = [alt]
    else:
        alt = [a.get("v") for a in alt]
    brand = info.get("开发") or info.get("发行") or ""
    translated = _text(x.get("name_cn"))
    return Record("bangumi", str(x["id"]), f'https://bgm.tv/subject/{x["id"]}', x["name"], translated,
                  _unique(alt, [x["name"], translated]),
                  tuple(SiteBrand("bangumi", None, s.strip(), ()) for s in re.split(r"[、，,/／]", brand) if s.strip()),
                  _date(x.get("date")), _text((x.get("summary") or "").replace("\r\n", "\n")),
                  (x.get("images") or {}).get("large"), (), 0, "", True, json.dumps(x, ensure_ascii=False))


def parse_bangumi_search(data) -> list[Record]:
    return [parse_bangumi_subject(x) for x in data.get("data") or []]


def site_url(site: str, site_id: str) -> str:
    if site == "dlsite":
        return f"https://www.dlsite.com/maniax/work/=/product_id/{site_id}.html"
    if site == "vndb":
        return f"https://vndb.org/{site_id}"
    if site == "bangumi":
        return f"https://bgm.tv/subject/{site_id}"
    if site == "steam":
        return f"https://store.steampowered.com/app/{site_id}/"
    if site == "f95":
        return f"https://f95zone.to/threads/{site_id}/"
    return ""


def _https(url):
    if not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return url or None


def _plain_html(value) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|h[1-6]|tr|article|section|blockquote)\s*>", "\n", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = html_module.unescape(text).replace("\u200b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


_MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _steam_date(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", text)
        if m:
            mo = _MONTHS.get(m[1][:3].casefold())
            y, d = int(m[3]), int(m[2])
        else:
            m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", text)
            if not m:
                try:
                    return date.fromisoformat(text[:10]).isoformat()
                except ValueError:
                    return None
            d, mo = int(m[1]), _MONTHS.get(m[2][:3].casefold())
            y = int(m[3])
    try:
        return date(y, mo, d).isoformat() if mo else None
    except ValueError:
        return None


def _brands(site, names_) -> tuple[SiteBrand, ...]:
    return tuple(SiteBrand(site, None, name, ()) for name in _unique(names_))


def parse_steam_appdetails(data, appid) -> Record:
    key = str(appid)
    entry = data.get(key) if isinstance(data, dict) else None
    if entry is None and isinstance(data, dict) and "name" in data:
        entry = {"success": True, "data": data}
    if not isinstance(entry, dict) or not entry.get("success"):
        raise SiteNotFound("Steam 上没有这个条目", "steam")
    info = entry.get("data")
    if not isinstance(info, dict):
        raise SiteNotFound("Steam 上没有这个条目", "steam")
    site_id = str(info.get("steam_appid") or appid)
    summary = _plain_html(info.get("short_description")) or None
    if not summary:
        summary = _plain_html(info.get("about_the_game"))[:1000] or None
    release = info.get("release_date")
    release_date = _steam_date(release.get("date") if isinstance(release, dict) else release)
    developers = [x for x in info.get("developers") or [] if isinstance(x, str) and x.strip()]
    publishers = [x for x in info.get("publishers") or [] if isinstance(x, str) and x.strip()]
    brands = _brands("steam", developers or publishers)
    tags = tuple(SiteTag("steam", str(g.get("id")), str(g.get("description") or ""),
                         str(g.get("description") or ""), None)
                 for g in info.get("genres") or []
                 if isinstance(g, dict) and g.get("id") is not None and str(g.get("description") or "").strip())
    cover = STEAM_COVER.format(appid=site_id)
    fallback = _https(info.get("header_image"))
    return Record("steam", site_id, site_url("steam", site_id), str(info.get("name") or "").strip(),
                  None, (), brands, release_date, summary, cover, tags, 0, "", True,
                  json.dumps(info, ensure_ascii=False), fallback)


def parse_steam_search(data) -> list[Record]:
    result = []
    for item in (data.get("items") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        site_id = str(item["id"])
        result.append(Record("steam", site_id, site_url("steam", site_id),
                             str(item.get("name") or "").strip(), None, (), (), None, None,
                             _https(item.get("tiny_image")), (), 0, "", False,
                             json.dumps(item, ensure_ascii=False)))
    return result


def parse_f95_search(data) -> list[Record]:
    msg = data.get("msg") if isinstance(data, dict) else None
    rows = msg.get("data") if isinstance(msg, dict) else None
    result = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("thread_id") is None:
            continue
        site_id = str(row["thread_id"])
        creator = row.get("creator")
        if isinstance(creator, dict):
            creator = creator.get("name")
        brands = _brands("f95", [creator]) if isinstance(creator, str) and creator.strip() else ()
        result.append(Record("f95", site_id, site_url("f95", site_id),
                             str(row.get("title") or "").strip(), None, (), brands, None, None,
                             _https(row.get("cover")), (), 0, str(row.get("version") or ""), False,
                             json.dumps(row, ensure_ascii=False)))
    return result


def _f95_og_title(html: str) -> str | None:
    for match in re.finditer(r"<meta\b[^>]*>", html or "", re.I):
        tag = match.group(0)
        if not re.search(r"""\bproperty\s*=\s*["']og:title["']""", tag, re.I):
            continue
        content = re.search(r"""\bcontent\s*=\s*(["'])(.*?)\1""", tag, re.I | re.S)
        if content:
            return html_module.unescape(content.group(2)).strip()
    return None


_F95_TITLE_PREFIXES = (
    "VN", "Ren'Py", "RenPy", "Unity", "RPGM", "RPG Maker", "HTML", "Others", "Other",
    "Completed", "Abandoned", "Onhold", "On hold", "Unreal Engine", "Flash", "QSP",
    "Java", "ADRIFT", "WebGL", "Wolf RPG", "Tyrano", "TyranoBuilder", "RAGS", "Godot",
    "Collection", "SiNVR", "Cheat Mod", "Mod", "Tool", "Tools", "Unchecked",
    "Real Porn", "Turn based",
)
_F95_TITLE_PREFIX = re.compile(
    r"^(?:" + "|".join(re.escape(prefix) for prefix in
                       sorted(_F95_TITLE_PREFIXES, key=len, reverse=True)) + r")\s*-\s*",
    re.I,
)


def _f95_heading(title: str) -> tuple[str, str, str]:
    """Return (title, version, author) from an F95 thread heading."""
    rest = html_module.unescape(title or "").strip()
    while True:
        prefix = _F95_TITLE_PREFIX.match(rest)
        if not prefix:
            break
        rest = rest[prefix.end():].lstrip()
    match = re.fullmatch(r"(.*?)\s*\[([^\]]*)\]\s*\[([^\]]*)\]\s*", rest, re.S)
    if match:
        return match[1].strip(), match[2].strip(), match[3].strip()
    match = re.fullmatch(r"(.*?)\s*\[([^\]]*)\]\s*", rest, re.S)
    if match:
        return match[1].strip(), "", match[2].strip()
    return rest.strip(), "", ""


def parse_f95_thread(html: str, thread_id) -> Record:
    og_title = _f95_og_title(html)
    if not og_title:
        raise SiteNotFound("F95zone 上没有这个帖子", "f95")
    title, version, author = _f95_heading(og_title)
    article = re.search(r'<article\b[^>]*class="[^"]*message-body[^"]*"[^>]*>.*?</article>',
                        html, re.I | re.S)
    if article:
        # Attachments sit in a <section> right after the article, still part of post one.
        following = re.search(r"<article\b", html[article.end():], re.I)
        end = article.end() + following.start() if following else len(html)
        first_post = html[article.start():end]
    else:
        first_post = html
    text = _plain_html(first_post)
    overview = re.search(
        r"Overview\s*:\s*(.*?)(?=\n\s*(?:Thread Updated|Release Date|Developer|Censored|"
        r"Version|OS|Language|Genre|Installation)\b|$)", text, re.I | re.S)
    summary = overview[1].strip()[:1000] or None if overview else None
    release = re.search(r"Release Date\s*:\s*(\d{4}-\d{2}-\d{2})", text, re.I)
    cover = re.search(r"https://attachments\.f95zone\.to/[^\s\"'<>]+", first_post, re.I)
    if not cover:
        # Logged-out first posts hide their attachments; the first visible one is
        # still a real attachment rather than the site-logo og:image.
        cover = re.search(r"https://attachments\.f95zone\.to/[^\s\"'<>]+", html or "", re.I)
    tags, seen = [], set()
    for href, label in re.findall(r"""href\s*=\s*["']/tags/([^"'/]+)/["'][^>]*>(.*?)</a>""",
                                  html or "", re.I | re.S):
        if href in seen:
            continue
        seen.add(href)
        text_label = _plain_html(label)
        if text_label:
            tags.append(SiteTag("f95", href, text_label, text_label, None))
    site_id = str(thread_id)
    payload = {"site": "f95", "site_id": site_id, "url": site_url("f95", site_id),
               "title": title, "version": version, "author": author,
               "release_date": release[1] if release else None, "summary": summary,
               "cover_url": _https(cover.group(0)) if cover else None,
               "tags": [dict(tag_key=t.tag_key, text=t.text) for t in tags]}
    brands = _brands("f95", [author]) if author else ()
    return Record("f95", site_id, site_url("f95", site_id), title, None, (), brands,
                  release[1] if release else None, summary,
                  _https(cover.group(0)) if cover else None, tuple(tags), 0, version, True,
                  json.dumps(payload, ensure_ascii=False))


def _code(text, prefixes):
    m = re.search(r"(?<![A-Za-z0-9])(" + prefixes + r")\d{6,8}(?![0-9])", text, re.I)
    return Ref("dlsite", m[0].upper()) if m else None


def parse_ref(text: str) -> Ref | None:
    text = text.strip()
    code = _code(text, "RJ|VJ|BJ|RE")
    if code:
        return code
    m = re.search(r"vndb\.org/(v\d+)", text, re.I) or re.fullmatch(r"v\d+", text, re.I)
    if m:
        return Ref("vndb", (m[1] if m.lastindex else m[0]).lower())
    m = re.search(r"store\.steampowered\.com/app/(\d+)", text, re.I) or \
        re.fullmatch(r"steam\s*:\s*(\d+)", text, re.I)
    if m:
        return Ref("steam", m[1])
    m = re.search(r"f95zone\.to/threads/(?:[^/?#]+\.)?(\d+)(?:[/?#]|$)", text, re.I) or \
        re.fullmatch(r"f95\s*:\s*(\d+)", text, re.I)
    if m:
        return Ref("f95", m[1])
    m = re.search(r"(?:bgm\.tv|bangumi\.tv|chii\.in)/subject/(\d+)", text, re.I) or re.fullmatch(r"bgm\s*:?\s*(\d+)", text, re.I)
    return Ref("bangumi", m[1]) if m else None


def find_code(text: str) -> Ref | None:
    return _code(text, "RJ|VJ")


def search_text(title: str) -> str:
    s = names.parse(title)[0]
    s = re.sub(r"[\s_\-]*(pc|win|windows|体験版|体验版|trial|demo)$", "", s, flags=re.I).strip()
    return s or title.strip()


def match_key(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if not c.isspace() and unicodedata.category(c)[0] not in "PSZ")


def grade(query: str, record: Record) -> tuple[str, float]:
    q = match_key(search_text(query))
    keys = [match_key(s) for s in (record.title, record.translated_title, *record.alt_titles) if s and match_key(s)]
    if not q:
        return "no", 0.0
    if q in keys:
        return "ok", 1.0
    score = max((difflib.SequenceMatcher(None, q, k).ratio() for k in keys), default=0.0)
    if any((q in k or k in q) and min(len(q), len(k)) >= 4 for k in keys):
        score = max(score, 0.9)
    return ("near" if score >= 0.6 else "no"), round(score, 4)


def rank(query: str, records: Iterable[Record]) -> list[Candidate]:
    result = [Candidate(r, *grade(query, r)) for r in records]
    return sorted(result, key=lambda c: ({"ok": 0, "near": 1, "no": 2}[c.grade], -c.score, SITE_ORDER.index(c.record.site)))
