"""候选资料搜索：VNDB / DLsite / Steam / F95。

网络只经过可注入的 transport，测试里用假 transport；同一站点请求之间限速，
响应按 sha1(site + "\\n" + query) 落盘缓存。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT}
DLSITE_HEADERS = {**HEADERS, "Cookie": "adultchecked=1; locale=ja-jp"}

_VNDB_FIELDS = ("id,title,alttitle,titles.title,titles.lang,titles.official,released,olang,"
                "developers.name,developers.original,developers.type,image.url,image.sexual,"
                "rating,votecount,description")
_DLSITE_CODE_RE = re.compile(r"^(RJ|VJ|BJ|RE)\d{6,8}$", re.IGNORECASE)
_DLSITE_LINK_RE = re.compile(
    r'<a href="https://www\.dlsite\.com/(\w+)/work/=/product_id/((?:RJ|VJ)\d+)\.html"[^>]*title="([^"]+)"')
_APPID_RE = re.compile(r"^(?:steam:)?(\d+)$", re.IGNORECASE)
_VNDB_ID_RE = re.compile(r"^(?:vndb:)?(v\d+)$", re.IGNORECASE)


def _default_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc


def _https(url) -> str:
    if not isinstance(url, str):
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def _short(text, limit: int = 300) -> str:
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\[[^\]]*\]", " ", text)  # VNDB 的 BBCode 标签
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _release(value) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value.strip())
    if m:
        return m.group(1)
    return value.strip() or None


def _candidate(ref: str, site: str, title, *, alt_titles=None, zh_title=None, brand="",
               release=None, url="", summary="", image="") -> dict:
    return {
        "ref": ref,
        "site": site,
        "title": title if isinstance(title, str) else "",
        "alt_titles": [t for t in (alt_titles or []) if isinstance(t, str)],
        "zh_title": zh_title if isinstance(zh_title, str) and zh_title else None,
        "brand": brand if isinstance(brand, str) else "",
        "release": release if isinstance(release, str) and release else None,
        "url": url if isinstance(url, str) else "",
        "summary": summary if isinstance(summary, str) else "",
        "image": image if isinstance(image, str) else "",
    }


class Searcher:
    def __init__(self, transport: Transport | None = None, cache_dir: str | None = None,
                 sleep=time.sleep):
        self.transport: Transport = transport or _default_transport
        self.cache_dir = cache_dir
        self.sleep = sleep
        self.errors: list[str] = []
        self._errors_lock = threading.Lock()
        self._site_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._last_request: dict[str, float] = {}
        self._cache_lock = threading.Lock()
        if cache_dir:
            try:
                os.makedirs(cache_dir, exist_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------ 基础设施
    def _error(self, message: str) -> None:
        with self._errors_lock:
            self.errors.append(message)

    def _site_lock(self, site: str) -> threading.Lock:
        with self._locks_lock:
            return self._site_locks.setdefault(site, threading.Lock())

    def _cache_path(self, site: str, query: str) -> str | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha1(f"{site}\n{query}".encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"{digest}.json")

    def _cache_get(self, site: str, query: str) -> bytes | None:
        path = self._cache_path(site, query)
        if not path:
            return None
        with self._cache_lock:
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                return None
        body = raw.get("body")
        if not isinstance(body, str):
            return None
        return body.encode("utf-8")

    def _cache_put(self, site: str, query: str, body: bytes) -> None:
        path = self._cache_path(site, query)
        if not path:
            return
        payload = {"site": site, "query": query,
                   "body": body.decode("utf-8", "replace")}
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _fetch(self, site: str, query: str, method: str, url: str, headers: dict,
               body: bytes | None = None) -> bytes | None:
        cached = self._cache_get(site, query)
        if cached is not None:
            return cached

        interval = 6.0 if site == "dlsite" else 1.5
        with self._site_lock(site):
            now = time.monotonic()
            last = self._last_request.get(site)
            if last is not None:
                delay = interval - (now - last)
                if delay > 0:
                    self.sleep(delay)
            self._last_request[site] = time.monotonic()

        try:
            status, data = self.transport(method, url, headers, body)
        except Exception as exc:  # noqa: BLE001
            self._error(f"{site} {query!r}: {type(exc).__name__}: {exc}")
            return None
        if status != 200:
            self._error(f"{site} {query!r}: HTTP {status}")
            return None
        self._cache_put(site, query, data)
        return data

    def _parse(self, site: str, query: str, data: bytes):
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except ValueError as exc:
            self._error(f"{site} {query!r}: JSON 解析失败: {exc}")
            return None

    # ------------------------------------------------------------ VNDB
    def vndb(self, query: str) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        m = _VNDB_ID_RE.match(query)
        if m:
            vn_id = m.group(1).lower()
            filters = ["id", "=", vn_id]
            sort = "id"
        else:
            vn_id = None
            filters = ["search", "=", query]
            sort = "searchrank"
        body = json.dumps({"filters": filters, "fields": _VNDB_FIELDS,
                           "results": 5, "sort": sort}).encode("utf-8")
        data = self._fetch("vndb", query, "POST", "https://api.vndb.org/kana/vn",
                           {**HEADERS, "Content-Type": "application/json"}, body)
        if data is None:
            return []
        payload = self._parse("vndb", query, data)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            self._error(f"vndb {query!r}: 响应格式不对")
            return []

        out: list[dict] = []
        for item in payload["results"][:5]:
            if not isinstance(item, dict):
                continue
            vid = item.get("id") or vn_id
            if not isinstance(vid, str):
                continue
            title = item.get("title") or item.get("alttitle") or ""
            titles = [t for t in (item.get("titles") or []) if isinstance(t, dict)]
            zh = None
            for lang in ("zh-Hans", "zh-Hant"):
                zh = next((t.get("title") for t in titles
                           if t.get("lang") == lang and isinstance(t.get("title"), str)), None)
                if zh:
                    break
            alt: list[str] = []
            if item.get("alttitle"):
                alt.append(item["alttitle"])
            for t in titles:
                name = t.get("title")
                if isinstance(name, str) and name and name not in alt and name != title:
                    alt.append(name)
            devs = [d.get("name") for d in (item.get("developers") or [])
                    if isinstance(d, dict) and isinstance(d.get("name"), str)]
            image = item.get("image") or {}
            out.append(_candidate(
                f"vndb:{vid}", "vndb", title,
                alt_titles=alt,
                zh_title=zh,
                brand=" / ".join(devs),
                release=_release(item.get("released")),
                url=f"https://vndb.org/{vid}",
                summary=_short(item.get("description"), 300),
                image=_https(image.get("url") if isinstance(image, dict) else ""),
            ))
        return out

    # ------------------------------------------------------------ DLsite
    def dlsite(self, code_or_query: str) -> list[dict]:
        value = (code_or_query or "").strip()
        if not value:
            return []
        if value.lower().startswith("dlsite:"):
            value = value.split(":", 1)[1].strip()
        is_code = bool(_DLSITE_CODE_RE.match(value))

        if is_code:
            code = value.upper()
            url = ("https://www.dlsite.com/maniax/api/=/product.json?workno="
                   + urllib.parse.quote(code))
            data = self._fetch("dlsite", code_or_query, "GET", url, DLSITE_HEADERS)
            if data is None:
                return []
            payload = self._parse("dlsite", code_or_query, data)
            if isinstance(payload, dict):
                payload = [payload]
            if not isinstance(payload, list):
                self._error(f"dlsite {code_or_query!r}: 响应格式不对")
                return []
            out: list[dict] = []
            for item in payload[:5]:
                if not isinstance(item, dict):
                    continue
                workno = item.get("workno") or code
                image = item.get("image_main")
                image_url = image.get("url") if isinstance(image, dict) else ""
                out.append(_candidate(
                    f"dlsite:{str(workno).upper()}", "dlsite", item.get("work_name"),
                    brand=item.get("maker_name") or "",
                    release=_release(item.get("regist_date")),
                    url=("https://www.dlsite.com/maniax/work/=/product_id/"
                         f"{str(workno).upper()}.html"),
                    summary=_short(item.get("intro_s"), 300),
                    image=_https(image_url),
                ))
            return out

        url = ("https://www.dlsite.com/maniax/fsr/=/language/jp/keyword/"
               + urllib.parse.quote(value, safe="") + "/per_page/30")
        data = self._fetch("dlsite", code_or_query, "GET", url, DLSITE_HEADERS)
        if data is None:
            return []
        text = data.decode("utf-8", "replace")
        out = []
        seen: set[str] = set()
        for m in _DLSITE_LINK_RE.finditer(text):
            code = m.group(2)
            if code in seen:
                continue
            seen.add(code)
            maker_match = re.search(r'maker_name[^>]*>\s*<a[^>]*>([^<]+)<',
                                    text[m.end():m.end() + 3000])
            out.append(_candidate(
                f"dlsite:{code}", "dlsite", html.unescape(m.group(3)),
                brand=html.unescape(maker_match.group(1)) if maker_match else "",
                url=("https://www.dlsite.com/maniax/work/=/product_id/"
                     f"{code}.html"),
            ))
            if len(out) >= 5:
                break
        return out

    # ------------------------------------------------------------ Steam
    def steam(self, query_or_appid: str) -> list[dict]:
        value = (query_or_appid or "").strip()
        if not value:
            return []
        m = _APPID_RE.match(value)
        if m:
            appid = m.group(1)
            url = ("https://store.steampowered.com/api/appdetails?appids="
                   + urllib.parse.quote(appid) + "&l=schinese")
            data = self._fetch("steam", query_or_appid, "GET", url, HEADERS)
            if data is None:
                return []
            payload = self._parse("steam", query_or_appid, data)
            if not isinstance(payload, dict):
                return []
            entry = payload.get(appid) or payload.get(str(appid))
            if not isinstance(entry, dict) or not entry.get("success"):
                return []
            info = entry.get("data") or {}
            if not isinstance(info, dict):
                return []
            devs = [d for d in (info.get("developers") or []) if isinstance(d, str)]
            release = info.get("release_date") or {}
            return [_candidate(
                f"steam:{appid}", "steam", info.get("name"),
                brand=" / ".join(devs),
                release=_release(release.get("date") if isinstance(release, dict) else None),
                url=f"https://store.steampowered.com/app/{appid}/",
                summary=_short(info.get("short_description"), 300),
                image=_https(info.get("header_image")),
            )]

        url = ("https://store.steampowered.com/api/storesearch/?term="
               + urllib.parse.quote(value, safe="") + "&l=schinese&cc=us")
        data = self._fetch("steam", query_or_appid, "GET", url, HEADERS)
        if data is None:
            return []
        payload = self._parse("steam", query_or_appid, data)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            self._error(f"steam {query_or_appid!r}: 响应格式不对")
            return []
        out: list[dict] = []
        for item in payload["items"][:5]:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            appid = str(item["id"])
            out.append(_candidate(
                f"steam:{appid}", "steam", item.get("name"),
                url=f"https://store.steampowered.com/app/{appid}/",
                image=_https(item.get("tiny_image") or item.get("header_image")),
            ))
        return out

    # ------------------------------------------------------------ F95
    def f95(self, query: str) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        url = ("https://f95zone.to/sam/latest_alpha/latest_data.php?cmd=list&cat=games&search="
               + urllib.parse.quote(query, safe=""))
        data = self._fetch("f95", query, "GET", url, HEADERS)
        if data is None:
            return []
        payload = self._parse("f95", query, data)
        msg = payload.get("msg") if isinstance(payload, dict) else None
        rows = msg.get("data") if isinstance(msg, dict) else None
        if not isinstance(rows, list):
            self._error(f"f95 {query!r}: 响应格式不对")
            return []
        out: list[dict] = []
        for row in rows[:5]:
            if not isinstance(row, dict) or row.get("thread_id") is None:
                continue
            thread_id = str(row["thread_id"])
            creator = row.get("creator")
            if isinstance(creator, dict):
                creator = creator.get("name")
            out.append(_candidate(
                f"f95:{thread_id}", "f95", row.get("title"),
                brand=creator if isinstance(creator, str) else "",
                url=f"https://f95zone.to/threads/{thread_id}/",
            ))
        return out

    # ------------------------------------------------------------ 汇总
    def candidates(self, hints: dict, extract: dict) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()

        def add(items) -> None:
            for cand in items or []:
                if not isinstance(cand, dict):
                    continue
                ref = cand.get("ref")
                if not isinstance(ref, str) or ref in seen:
                    continue
                seen.add(ref)
                out.append(cand)
                if len(out) >= 20:
                    return

        codes = list((hints or {}).get("codes") or []) + list((extract or {}).get("codes") or [])
        for code in codes:
            if not isinstance(code, str):
                continue
            lowered = code.lower()
            if lowered.startswith("dlsite:"):
                add(self.dlsite(code))
            elif lowered.startswith("vndb:"):
                add(self.vndb(code))
            elif lowered.startswith("steam:"):
                add(self.steam(code))
            # getchu: 没有公开接口，跳过
            if len(out) >= 20:
                return out[:20]

        queries = [q for q in ((extract or {}).get("queries") or [])
                   if isinstance(q, str) and q.strip()]
        for query in queries:
            add(self.vndb(query))
            if len(out) >= 20:
                return out[:20]
            add(self.dlsite(query))
            if len(out) >= 20:
                return out[:20]

        need_steam_f95 = (extract or {}).get("category_guess") == "欧美" or len(out) < 3
        if need_steam_f95:
            for query in queries:
                add(self.steam(query))
                if len(out) >= 20:
                    return out[:20]
                add(self.f95(query))
                if len(out) >= 20:
                    return out[:20]
        return out[:20]
