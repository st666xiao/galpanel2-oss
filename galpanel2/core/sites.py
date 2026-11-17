"""Read-only site requests, shared rate limits and local-only credentials."""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import tempfile
import unicodedata
import threading
import time
from typing import Protocol
from urllib.parse import quote, urlsplit

import galpanel2
from . import records as R, repository
from .domain import SiteError, SiteNotFound, SiteUnreachable, check_cancel

USER_AGENT = f"GalPanel2/{galpanel2.__version__} (personal library manager)"
DLSITE_SUGGEST = "https://www.dlsite.com/suggest/?term={q}&site=adult-jp&time={ms}&touch=0"
DLSITE_PRODUCT = "https://www.dlsite.com/{section}/api/=/product.json?workno={id}&locale=zh_CN"
DLSITE_SECTION = {"RJ": "maniax", "RE": "maniax", "VJ": "pro", "BJ": "books"}
VNDB_API = "https://api.vndb.org/kana/vn"
VNDB_FIELDS = ("id,title,alttitle,titles.title,titles.lang,released,image.url,description,"
               "developers.id,developers.name,developers.original,developers.aliases,"
               "tags.id,tags.name,tags.category,tags.rating,tags.spoiler")
BGM_SEARCH = "https://api.bgm.tv/v0/search/subjects?limit=10"
BGM_SUBJECT = "https://api.bgm.tv/v0/subjects/{id}"
STEAM_APPDETAILS = "https://store.steampowered.com/api/appdetails?appids={id}&l=schinese"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/?term={q}&l=schinese&cc=US"
F95_SEARCH = ("https://f95zone.to/sam/latest_alpha/latest_data.php"
              "?cmd=list&cat=games&search={q}")
F95_THREAD = "https://f95zone.to/threads/{id}/"
INTERVALS = {"www.dlsite.com": 1.0, "api.vndb.org": 1.5, "api.bgm.tv": 1.0,
             "store.steampowered.com": 1.0, "f95zone.to": 2.0,
             "shared.akamai.steamstatic.com": 1.0, "attachments.f95zone.to": 1.0,
             "preview.f95zone.to": 1.0}
OTHER_INTERVAL = 0.5
TIMEOUT = 15.0
MAX_IMAGE = 10 * 1024 * 1024

@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> Response: ...


def _label(host):
    site = {"www.dlsite.com": "dlsite", "api.vndb.org": "vndb", "api.bgm.tv": "bangumi",
            "store.steampowered.com": "steam", "f95zone.to": "f95",
            "shared.akamai.steamstatic.com": "steam", "attachments.f95zone.to": "f95",
            "preview.f95zone.to": "f95"}.get(host)
    return site, R.SITE_LABEL.get(site, "图片")


class UrllibTransport:
    def request(self, method, url, headers, body, timeout):
        from urllib.request import Request, build_opener, HTTPRedirectHandler
        from urllib.error import HTTPError, URLError

        class SafeRedirects(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, response_headers, newurl):
                result = super().redirect_request(req, fp, code, msg, response_headers, newurl)
                if result is not None and urlsplit(newurl).hostname != "api.bgm.tv":
                    result.remove_header("Authorization")
                return result

        site, label = _label(urlsplit(url).hostname)
        try:
            req = Request(url, data=body, headers=headers, method=method)
            try:
                response = build_opener(SafeRedirects()).open(req, timeout=timeout)
            except HTTPError as e:
                response = e
            with response:
                return Response(response.code, response.read(MAX_IMAGE + 1) if headers.get("Accept") == "image/*" else response.read(), dict(response.headers))
        except (URLError, TimeoutError, OSError) as e:
            # HTTPError is handled above and still counts as a response.
            # Never expose exception text containing request headers or credentials.
            raise SiteUnreachable(f"连不上 {label}：连接失败", site) from None


class FileTransport:
    _lock = threading.Lock()

    def __init__(self, rules_path: str):
        self.path = Path(rules_path)
        self.rules = json.loads(self.path.read_text(encoding="utf-8-sig"))["rules"]

    def request(self, method, url, headers, body, timeout):
        text = body.decode("utf-8") if body is not None else None
        log = dict(method=method, url=url, auth=any(k.lower() == "authorization" for k in headers),
                   ua=next((v for k, v in headers.items() if k.lower() == "user-agent"), None), body=text)
        with self._lock, open(str(self.path) + ".log", "a", encoding="utf-8") as f:
            f.write(json.dumps(log, ensure_ascii=False) + "\n")
        for rule in self.rules:
            if rule["method"] == method and url.startswith(rule["url_prefix"]) and \
                    ("body_contains" not in rule or rule["body_contains"] in (text or "")):
                if rule.get("unreachable"):
                    site, label = _label(urlsplit(url).hostname)
                    raise SiteUnreachable(f"连不上 {label}：连接失败", site)
                data = ((self.path.parent / rule["body_file"]).read_bytes() if "body_file" in rule else
                        json.dumps(rule["json"], ensure_ascii=False).encode("utf-8"))
                return Response(rule.get("status", 200), data, rule.get("headers", {}))
        raise SiteError(f"没有假数据：{method} {url}")


def default_transport() -> Transport:
    path = os.environ.get("GALPANEL2_FAKE_SITES")
    return FileTransport(path) if path else UrllibTransport()


class RateLimiter:
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self._last = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = self.clock()
            if host in self._last:
                delay = INTERVALS.get(host, OTHER_INTERVAL) - (now - self._last[host])
                if delay > 0:
                    self.sleep(delay)
            self._last[host] = self.clock()

DEFAULT_LIMITER = RateLimiter()


class SiteClient:
    def __init__(self, transport: Transport | None = None, *, token: str | None = None,
                 limiter: RateLimiter | None = None, cancel=None):
        self.transport = transport if transport is not None else default_transport()
        self.token = token
        self.limiter = limiter if limiter is not None else DEFAULT_LIMITER
        self.cancel = cancel

    def _request(self, method, url, obj=None, image=False, text=False):
        host = urlsplit(url).hostname
        site, label = _label(host)
        accept = "image/*" if image else "text/html,application/xhtml+xml" if text else "application/json"
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        body = None
        if obj is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if host == "api.bgm.tv" and self.token:
            headers["Authorization"] = "Bearer " + self.token
        for attempt in range(3):
            check_cancel(self.cancel)
            self.limiter.wait(host)
            check_cancel(self.cancel)
            try:
                response = self.transport.request(method, url, headers, body, TIMEOUT)
            except SiteError as e:
                message = str(e).replace(self.token, "[redacted]") if self.token else str(e)
                # Keep SiteUnreachable distinct from an ordinary SiteError; neither is retried.
                raise type(e)(message, e.site or site) from None
            except (OSError, TimeoutError):
                raise SiteUnreachable(f"连不上 {label}：连接失败", site) from None
            check_cancel(self.cancel)
            if response.status == 200:
                if image:
                    return response.body
                if text:
                    return response.body.decode("utf-8", "replace")
                try:
                    return json.loads(response.body)
                except (ValueError, UnicodeError):
                    raise SiteError(f"{label} 返回的内容看不懂", site) from None
            if response.status == 404:
                raise SiteNotFound(f"{label} 上没有这个条目", site)
            if response.status in (401, 403) and host == "api.bgm.tv":
                raise SiteError("Bangumi token 无效或过期", "bangumi")
            if response.status == 403 and site == "f95":
                raise SiteError("F95zone 拒绝了请求，稍后再试", "f95")
            if response.status != 429:
                raise SiteError(f"{label} 返回 {response.status}", site)
            if attempt < 2:
                retry = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), "5")
                try:
                    delay = max(0, int(retry))
                except (TypeError, ValueError):
                    delay = 5
                self.limiter.sleep(delay)
        raise SiteError(f"{label} 说请求太频繁，请稍后再试", site)

    def search(self, site: str, query: str) -> list[R.Record]:
        if site == "dlsite":
            return R.parse_dlsite_suggest(self._request("GET", DLSITE_SUGGEST.format(q=quote(query), ms=int(time.time()*1000))))
        if site == "vndb":
            return R.parse_vndb(self._request("POST", VNDB_API,
                               {"filters": ["search", "=", query], "fields": VNDB_FIELDS, "results": 10}))
        if site == "bangumi":
            filters = {"type": [4]}
            if self.token:
                filters["nsfw"] = True
            return R.parse_bangumi_search(self._request("POST", BGM_SEARCH, {"keyword": query, "filter": filters}))
        if site == "steam":
            return R.parse_steam_search(self._request("GET", STEAM_SEARCH.format(q=quote(query))))
        if site == "f95":
            return R.parse_f95_search(self._request("GET", F95_SEARCH.format(q=quote(query))))
        raise ValueError("Unknown site")

    def fetch(self, ref: R.Ref) -> R.Record:
        if ref.site == "dlsite":
            return R.parse_dlsite_product(self._request("GET", DLSITE_PRODUCT.format(section=DLSITE_SECTION[ref.site_id[:2]], id=ref.site_id)))
        if ref.site == "vndb":
            rows = R.parse_vndb(self._request("POST", VNDB_API,
                               {"filters": ["id", "=", ref.site_id], "fields": VNDB_FIELDS, "results": 1}))
            if not rows:
                raise SiteNotFound("VNDB 上没有这个条目", "vndb")
            return rows[0]
        if ref.site == "bangumi":
            return R.parse_bangumi_subject(self._request("GET", BGM_SUBJECT.format(id=ref.site_id)))
        if ref.site == "steam":
            data = self._request("GET", STEAM_APPDETAILS.format(id=ref.site_id))
            return R.parse_steam_appdetails(data, ref.site_id)
        if ref.site == "f95":
            html = self._request("GET", F95_THREAD.format(id=ref.site_id), text=True)
            record = R.parse_f95_thread(html, ref.site_id)
            if record.title:
                # Thread attachments need a login; the public search index has the preview cover.
                try:
                    rows = self.search("f95", record.title)
                except SiteError:
                    rows = []
                match = next((row for row in rows if row.site_id == str(ref.site_id)), None)
                cover = match.cover_url if match else None
                if cover is None and match:
                    try:
                        screens = json.loads(match.payload).get("screens") or []
                        screens = screens if isinstance(screens, list) else []
                    except (TypeError, ValueError):
                        screens = []
                    cover = next((url for url in (R._https(x) for x in screens) if url), None)
                if cover:
                    record = replace(record, cover_url=cover)
            return record
        raise ValueError("Unknown site")

    def image(self, url: str) -> bytes:
        if not url.startswith("https://"):
            raise SiteError("图片下载失败")
        data = self._request("GET", url, image=True)
        if len(data) > MAX_IMAGE:
            raise SiteError("图片下载失败")
        try:
            repository.image_extension(data)
        except ValueError:
            raise SiteError("图片下载失败") from None
        return data

    def search_all(self, query: str, sites=R.SITES, unreachable: set[str] | None = None) -> tuple[list[R.Candidate], dict[str, str]]:
        ref = R.parse_ref(query)
        if ref:
            try:
                return [R.Candidate(self.fetch(ref), "ok", 1.0)], {}
            except SiteUnreachable:
                if unreachable is not None:
                    unreachable.add(ref.site)
                raise
        rows, errors = [], {}
        for site in sites:
            try:
                rows.extend(self.search(site, query))
            except SiteUnreachable as e:
                errors[site] = str(e)
                if unreachable is not None:
                    unreachable.add(site)
            except SiteError as e:
                errors[site] = str(e)
        return R.rank(query, rows), errors


def settings_dir() -> Path:
    return Path(os.environ.get("GALPANEL2_SETTINGS_DIR") or
                (str(Path(os.environ["APPDATA"]) / "GalPanel2") if os.environ.get("APPDATA") else str(Path.home() / ".config/GalPanel2")))


def token_path() -> Path:
    return settings_dir() / "bangumi_token.txt"


def read_token() -> str | None:
    try:
        return token_path().read_text(encoding="utf-8-sig").strip() or None
    except (OSError, UnicodeError):
        return None


def valid_token(token: str) -> bool:
    return bool(token) and not any(c.isspace() or unicodedata.category(c).startswith('C') for c in token)


def write_token(token: str) -> None:
    token = token.strip()
    if not valid_token(token):
        raise ValueError("Invalid token format")
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".bangumi-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def clear_token() -> None:
    try:
        token_path().unlink()
    except FileNotFoundError:
        pass
