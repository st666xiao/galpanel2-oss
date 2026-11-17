"""search.py 的单元测试：假 transport、缓存、限速、候选汇总。"""
from __future__ import annotations

import json

from galpanel2.identify import search


class FakeTransport:
    def __init__(self):
        self.urls = []

    def __call__(self, method, url, headers, body):
        self.urls.append((method, url, headers, body))
        if "api.vndb.org" in url:
            q = json.loads(body)
            if q["filters"][0] == "id":
                return 200, json.dumps({"results": [self.vn("v26054", "催眠", "POISON")]}).encode()
            if q["filters"][-1] == "nothing":
                return 200, json.dumps({"results": []}).encode()
            return 200, json.dumps({"results": [self.vn(f"v{i}", f"R{i}", "Dev")
                                                for i in range(1, 8)]}).encode()
        if "product.json" in url:
            return 200, json.dumps([{
                "workno": "RJ01112222", "work_name": "同人作", "maker_name": "サークル",
                "regist_date": "2024-03-29 00:00:00", "intro_s": "简介",
                "image_main": {"url": "//img/main.jpg"}}]).encode()
        if "dlsite.com/maniax/fsr" in url:
            if "bad" in url:
                return 503, b"busy"
            page = ('<a href="https://www.dlsite.com/maniax/work/=/product_id/RJ02223333.html" '
                    'title="关键词作品&amp;名">x</a>'
                    '<span class="maker_name"><a href="/x">关键词社团</a></span>')
            return 200, page.encode("utf-8")
        if "storesearch" in url:
            return 200, json.dumps({"items": [{"id": 777, "name": "Western Game"}]}).encode()
        if "appdetails" in url:
            return 200, json.dumps({"1234560": {"success": True, "data": {
                "name": "S", "short_description": "d" * 500, "developers": ["Dev"],
                "release_date": {"date": "2024-02-03"}, "header_image": "h"}}}).encode()
        if "f95zone" in url:
            return 200, json.dumps({"status": "ok", "msg": {"data": [
                {"thread_id": 42, "title": "AVN", "creator": "Me"}]}}).encode()
        return 404, b""

    @staticmethod
    def vn(vid, title, dev):
        return {"id": vid, "title": title, "alttitle": None,
                "titles": [{"title": "中文名", "lang": "zh-Hans", "official": True}],
                "released": "2020-01-01", "olang": "ja",
                "developers": [{"name": dev, "original": None, "type": "co"}],
                "image": {"url": "u", "sexual": 0}, "rating": 70, "votecount": 10,
                "description": "d" * 500}


def make(tmp_path, transport=None, slept=None):
    return search.Searcher(transport=transport or FakeTransport(), cache_dir=str(tmp_path / "cache"),
                           sleep=(slept.append if slept is not None else (lambda x: None)))


def test_vndb_search_maps_candidate_and_is_cached(tmp_path):
    transport = FakeTransport()
    s = make(tmp_path, transport)
    rows = s.vndb("催眠")
    assert len(rows) == 5
    first = rows[0]
    assert first["ref"] == "vndb:v1" and first["site"] == "vndb"
    assert first["zh_title"] == "中文名" and first["brand"] == "Dev"
    assert first["url"] == "https://vndb.org/v1" and len(first["summary"]) <= 300
    assert {"ref", "site", "title", "alt_titles", "zh_title", "brand", "release",
            "url", "summary", "image"} <= set(first)
    assert json.loads(transport.urls[-1][3])["filters"] == ["search", "=", "催眠"]

    n = len(transport.urls)
    assert s.vndb("催眠")[0]["ref"] == "vndb:v1"
    assert len(transport.urls) == n


def test_vndb_id_lookup_uses_id_filter(tmp_path):
    transport = FakeTransport()
    s = make(tmp_path, transport)
    assert s.vndb("v26054")[0]["ref"] == "vndb:v26054"
    assert json.loads(transport.urls[-1][3])["filters"] == ["id", "=", "v26054"]


def test_new_searcher_reads_shared_cache(tmp_path):
    first = make(tmp_path)
    first.vndb("催眠")
    second = search.Searcher(
        transport=lambda *a: (_ for _ in ()).throw(AssertionError("不该联网")),
        cache_dir=str(tmp_path / "cache"), sleep=lambda x: None)
    assert second.vndb("催眠")[0]["ref"] == "vndb:v1"


def test_dlsite_code_lookup_and_cookie(tmp_path):
    transport = FakeTransport()
    s = make(tmp_path, transport)
    rows = s.dlsite("RJ01112222")
    assert rows and rows[0]["ref"] == "dlsite:RJ01112222"
    assert rows[0]["brand"] == "サークル"
    assert rows[0]["release"] == "2024-03-29"
    assert rows[0]["image"] == "https://img/main.jpg"
    dl = [u for u in transport.urls if "dlsite" in u[1]]
    assert "adultchecked=1" in json.dumps(dl[-1][2])


def test_dlsite_keyword_parses_search_page(tmp_path):
    s = make(tmp_path)
    rows = s.dlsite("关键词")
    assert rows and rows[0]["ref"] == "dlsite:RJ02223333"
    assert rows[0]["title"] == "关键词作品&名" and rows[0]["brand"] == "关键词社团"


def test_dlsite_non_200_returns_empty_and_records_error(tmp_path):
    s = make(tmp_path)
    assert s.dlsite("bad") == []
    assert s.errors and "503" in s.errors[-1]


def test_steam_appid_and_keyword(tmp_path):
    s = make(tmp_path)
    app = s.steam("1234560")
    assert app[0]["ref"] == "steam:1234560" and app[0]["brand"] == "Dev"
    assert app[0]["release"] == "2024-02-03" and len(app[0]["summary"]) <= 300
    key = s.steam("Western")
    assert key[0]["ref"] == "steam:777" and key[0]["title"] == "Western Game"


def test_f95_maps_thread(tmp_path):
    s = make(tmp_path)
    rows = s.f95("AVN")
    assert rows[0]["ref"] == "f95:42" and rows[0]["brand"] == "Me"
    assert rows[0]["url"] == "https://f95zone.to/threads/42/"


def test_rate_limit_uses_injected_sleep_per_site(tmp_path):
    slept = []
    s = make(tmp_path, slept=slept)
    s.dlsite("RJ01112222")
    s.dlsite("关键词")
    assert any(delay > 5.5 for delay in slept), slept


def test_transport_exception_is_recorded_not_raised(tmp_path):
    def broken(*args):
        raise RuntimeError("offline")

    s = search.Searcher(transport=broken, cache_dir=str(tmp_path / "c"), sleep=lambda x: None)
    assert s.vndb("x") == []
    assert s.errors and "offline" in s.errors[-1]


def test_candidates_orders_codes_then_queries_and_dedupes(tmp_path):
    transport = FakeTransport()
    s = make(tmp_path, transport)
    hints = {"codes": ["getchu:1265724", "dlsite:RJ01112222", "vndb:v26054"]}
    extract = {"is_gal": True, "codes": ["steam:1234560", "vndb:v26054"],
               "queries": ["a", "b"], "category_guess": "日系"}
    rows = s.candidates(hints, extract)
    refs = [r["ref"] for r in rows]
    assert refs[:3] == ["dlsite:RJ01112222", "vndb:v26054", "steam:1234560"]
    assert len(refs) == len(set(refs)) and len(refs) <= 20
    assert not any("getchu" in u[1] for u in transport.urls)
    assert not any("storesearch" in u[1] or "f95zone" in u[1] for u in transport.urls)


def test_candidates_western_forces_steam_and_f95(tmp_path):
    transport = FakeTransport()
    s = make(tmp_path, transport)
    s.candidates({"codes": []}, {"is_gal": True, "codes": [], "queries": ["w"],
                                 "category_guess": "欧美"})
    assert any("storesearch" in u[1] for u in transport.urls)
    assert any("f95zone" in u[1] for u in transport.urls)


def test_candidates_few_results_also_searches_steam_and_f95(tmp_path):
    transport = FakeTransport()
    s = make(tmp_path, transport)
    s.candidates({"codes": []}, {"is_gal": True, "codes": [], "queries": ["nothing"],
                                 "category_guess": "日系"})
    assert any("storesearch" in u[1] for u in transport.urls)
