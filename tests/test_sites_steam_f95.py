"""Steam / F95zone core, binding and schema v6 tests (offline fixtures only)."""
from dataclasses import replace
import json
import re
from pathlib import Path
import sqlite3
import uuid

import pytest

from galpanel2.core import binding as B, records as R, sites as S, store
from galpanel2.core import domain as D

FIX = Path(__file__).parent / "fixtures" / "sites"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def fixture_text(name):
    return (FIX / name).read_text(encoding="utf-8")


def make_work(lib, wid=1, title="测试作品"):
    with lib.transaction() as c:
        c.execute("INSERT INTO works(id,uuid,title,developer,added_at,updated_at) "
                  "VALUES(?,?,?,'',?,?)", (wid, str(uuid.uuid4()), title, "x", "x"))


def make_rules(tmp_path, rules):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": rules}, ensure_ascii=False), encoding="utf-8")
    return path


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(url, headers)
        return item


def fake_client(*responses, token=None):
    transport = FakeTransport(responses)
    return S.SiteClient(transport, token=token,
                        limiter=S.RateLimiter(lambda: 1_000.0, lambda delay: None)), transport


def test_sites_and_labels_include_steam_f95():
    assert R.SITES == ("dlsite", "vndb", "bangumi", "steam", "f95")
    assert R.SITE_LABEL["steam"] == "Steam" and R.SITE_LABEL["f95"] == "F95zone"
    assert R.SITE_ORDER.index("steam") > R.SITE_ORDER.index("dlsite")
    assert R.SITE_ORDER.index("f95") > R.SITE_ORDER.index("steam")
    assert R.site_url("steam", "1144400") == "https://store.steampowered.com/app/1144400/"
    assert R.site_url("f95", "281831") == "https://f95zone.to/threads/281831/"


@pytest.mark.parametrize("value,site,sid", [
    ("steam:1144400", "steam", "1144400"),
    ("https://store.steampowered.com/app/1144400/", "steam", "1144400"),
    ("https://store.steampowered.com/app/1144400/%E5%8D%83%E6%81%8B/", "steam", "1144400"),
    ("f95:281831", "f95", "281831"),
    ("https://f95zone.to/threads/281831/", "f95", "281831"),
    ("https://f95zone.to/threads/forbidden-thoughts-ch-2-up-6-ren3d.281831/",
     "f95", "281831"),
])
def test_parse_ref_steam_f95(value, site, sid):
    assert R.parse_ref(value) == R.Ref(site, sid)


@pytest.mark.parametrize("value", ["steam:", "steam:abc", "https://store.steampowered.com/app/x/",
                                   "f95:", "https://f95zone.to/threads/abc/"])
def test_parse_ref_rejects_bad_steam_f95(value):
    assert R.parse_ref(value) is None


def test_steam_appdetails_real_fixture():
    record = R.parse_steam_appdetails(fixture("steam_appdetails_1144400.json"), "1144400")
    assert record.site == "steam" and record.site_id == "1144400"
    assert record.title == "千恋＊万花" and record.complete
    assert record.brands[0].name == "YUZUSOFT" and record.brands[0].site_id is None
    assert record.release_date == "2020-02-14"
    assert any(t.text == "冒险" and t.tag_key == "25" for t in record.tags)
    assert record.cover_url == (
        "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1144400/library_600x900.jpg")
    assert "header_schinese.jpg" in record.cover_fallback
    assert record.url == "https://store.steampowered.com/app/1144400/"
    assert "<" not in record.summary and "Yuzusoft" in record.summary
    assert json.loads(record.payload)["name"] == "千恋＊万花"


@pytest.mark.parametrize("raw,want", [
    ("2020 年 2 月 14 日", "2020-02-14"),
    ("14 Feb, 2020", "2020-02-14"),
    ("Feb 14, 2020", "2020-02-14"),
    ("2020-02-14", "2020-02-14"),
    ("coming soon", None),
])
def test_steam_release_date_formats(raw, want):
    data = fixture("steam_appdetails_1144400.json")
    data["1144400"]["data"]["release_date"]["date"] = raw
    assert R.parse_steam_appdetails(data, "1144400").release_date == want


def test_steam_appdetails_false_and_fallback_summary():
    with pytest.raises(D.SiteNotFound):
        R.parse_steam_appdetails({"1144400": {"success": False}}, "1144400")
    data = fixture("steam_appdetails_1144400.json")
    info = data["1144400"]["data"]
    info["short_description"] = ""
    info["about_the_game"] = "<p>第一段</p><p>第二段 &amp; 更多</p>" + "x" * 1200
    record = R.parse_steam_appdetails(data, "1144400")
    assert record.summary.startswith("第一段\n第二段 & 更多")
    assert "<p>" not in record.summary and len(record.summary) == 1000
    info["short_description"] = info["about_the_game"] = None
    assert R.parse_steam_appdetails(data, "1144400").summary is None


def test_steam_search_real_fixture():
    rows = R.parse_steam_search(fixture("steam_search_senren.json"))
    assert len(rows) == 1
    record = rows[0]
    assert record.site == "steam" and record.site_id == "1144400"
    assert record.title == "千恋＊万花" and not record.complete
    assert "shared.akamai.steamstatic.com" in record.cover_url
    assert record.url == "https://store.steampowered.com/app/1144400/"


def test_f95_search_real_fixture():
    rows = R.parse_f95_search(fixture("f95_search_forbidden_thoughts.json"))
    assert len(rows) == 1
    record = rows[0]
    assert record.site == "f95" and record.site_id == "281831"
    assert record.title == "Forbidden Thoughts" and not record.complete
    assert record.brands[0].name == "REN3D"
    assert record.extra == "Ch.2 Up.6"
    assert record.cover_url.startswith("https://preview.f95zone.to/")
    assert record.url == "https://f95zone.to/threads/281831/"


def test_f95_thread_real_fixture():
    record = R.parse_f95_thread(fixture_text("f95_thread_281831.html"), "281831")
    assert record.site == "f95" and record.site_id == "281831" and record.complete
    assert record.title == "Forbidden Thoughts"
    assert record.brands[0].name == "REN3D" and record.brands[0].site_id is None
    assert record.extra == "Ch.2 Up.6" and record.release_date == "2026-10-25"
    assert record.cover_url.startswith("https://attachments.f95zone.to/")
    assert record.tags and any(t.text == "3dcg" for t in record.tags)
    assert record.summary.startswith("Forbidden Thoughts is a story-driven")
    assert len(record.summary) <= 1000 and "<" not in record.summary
    payload = json.loads(record.payload)
    assert payload["title"] == "Forbidden Thoughts" and payload["version"] == "Ch.2 Up.6"
    assert "bbWrapper" not in record.payload and "<html" not in record.payload


def test_f95_thread_without_og_title_is_not_found():
    with pytest.raises(D.SiteNotFound):
        R.parse_f95_thread("<html><body>login</body></html>", "1")


def test_sites_search_uses_real_fixtures(tmp_path, monkeypatch):
    rules = make_rules(tmp_path, [
        {"method": "GET", "url_prefix": "https://store.steampowered.com/api/storesearch/",
         "body_file": str(FIX / "steam_search_senren.json")},
        {"method": "GET", "url_prefix": "https://f95zone.to/sam/latest_alpha/latest_data.php",
         "body_file": str(FIX / "f95_search_forbidden_thoughts.json")},
    ])
    monkeypatch.setenv("GALPANEL2_FAKE_SITES", str(rules))
    client = S.SiteClient(limiter=S.RateLimiter(lambda: 1_000.0, lambda delay: None))
    steam = client.search("steam", "Senren Banka")
    f95 = client.search("f95", "Forbidden Thoughts")
    assert steam[0].title == "千恋＊万花" and f95[0].title == "Forbidden Thoughts"
    log = Path(str(rules) + ".log").read_text(encoding="utf-8").splitlines()
    urls = [json.loads(line)["url"] for line in log]
    assert "https://store.steampowered.com/api/storesearch/?term=Senren%20Banka&l=schinese&cc=US" in urls
    assert any("search=Forbidden%20Thoughts" in url for url in urls)


def test_sites_fetch_steam_and_f95_real_fixtures(tmp_path, monkeypatch):
    rules = make_rules(tmp_path, [
        {"method": "GET", "url_prefix": "https://store.steampowered.com/api/appdetails",
         "body_file": str(FIX / "steam_appdetails_1144400.json")},
        {"method": "GET", "url_prefix": "https://f95zone.to/threads/281831/",
         "body_file": str(FIX / "f95_thread_281831.html")},
    ])
    monkeypatch.setenv("GALPANEL2_FAKE_SITES", str(rules))
    client = S.SiteClient(limiter=S.RateLimiter(lambda: 1_000.0, lambda delay: None))
    steam = client.fetch(R.Ref("steam", "1144400"))
    f95 = client.fetch(R.Ref("f95", "281831"))
    assert steam.title == "千恋＊万花" and steam.complete
    assert f95.title == "Forbidden Thoughts" and f95.complete
    log = Path(str(rules) + ".log").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["url"] == "https://f95zone.to/threads/281831/" for line in log)


def test_f95_fetch_reads_html_and_sends_html_accept():
    checked = []

    def respond(url, headers):
        checked.append(headers["Accept"])
        return S.Response(200, fixture_text("f95_thread_281831.html").encode("utf-8"), {})

    client, _ = fake_client(respond)
    record = client.fetch(R.Ref("f95", "281831"))
    assert record.title == "Forbidden Thoughts"
    assert checked[0].startswith("text/html")


def test_f95_forbidden_and_unreachable_errors():
    client, _ = fake_client(S.Response(403, b"denied", {}))
    with pytest.raises(D.SiteError) as err:
        client.fetch(R.Ref("f95", "1"))
    assert str(err.value) == "F95zone 拒绝了请求，稍后再试" and err.value.site == "f95"

    client, _ = fake_client(OSError("down"))
    with pytest.raises(D.SiteUnreachable) as err:
        client.fetch(R.Ref("f95", "1"))
    assert str(err.value) == "连不上 F95zone：连接失败"


def test_search_all_can_select_only_new_sites():
    def respond(url, headers):
        if "storesearch" in url:
            return S.Response(200, json.dumps(fixture("steam_search_senren.json")).encode(), {})
        return S.Response(200, json.dumps(fixture("f95_search_forbidden_thoughts.json")).encode(), {})

    client, transport = fake_client(respond)
    rows, errors = client.search_all("Forbidden Thoughts", sites=("steam", "f95"))
    assert not errors and {row.record.site for row in rows} == {"steam", "f95"}
    assert len(transport.calls) == 2


def test_cover_urls_and_steam_header_fallback():
    record = R.parse_steam_appdetails(fixture("steam_appdetails_1144400.json"), "1144400")
    assert len(B.cover_urls(record)) == 2
    assert B.cover_urls(record)[1] == record.cover_fallback

    class CoverClient:
        def __init__(self):
            self.urls = []

        def image(self, url):
            self.urls.append(url)
            if "library_600x900" in url:
                raise D.SiteError("图片下载失败")
            return PNG

    client = CoverClient()
    data, error = B.download_cover(client, record)
    assert data == PNG and error is None
    assert client.urls[-1] == record.cover_fallback


def test_bind_steam_writes_all_metadata(lib):
    make_work(lib)
    record = R.parse_steam_appdetails(fixture("steam_appdetails_1144400.json"), "1144400")
    B.apply_bind(lib, 1, 1, record, cover_bytes=PNG)
    info = B.get_info(lib, 1)
    assert info.links["steam"] == "1144400"
    assert info.summary_source == "steam" and info.release_source == "steam"
    assert info.release_date == "2020-02-14" and info.cover_source == "steam"
    assert ("千恋＊万花", "original", "steam") in info.titles
    with lib.connection() as c:
        assert c.execute("SELECT name FROM brands").fetchone()[0] == "YUZUSOFT"
        assert c.execute("SELECT count(*) FROM work_brands WHERE work_id=1").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM covers WHERE work_id=1").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM source_tags WHERE site='steam'").fetchone()[0] == 2
        assert c.execute("SELECT summary_source FROM work_info WHERE work_id=1").fetchone()[0] == "steam"


def test_bind_f95_writes_all_metadata(lib):
    make_work(lib)
    record = R.parse_f95_thread(fixture_text("f95_thread_281831.html"), "281831")
    B.apply_bind(lib, 1, 1, record, cover_bytes=PNG)
    info = B.get_info(lib, 1)
    assert info.links["f95"] == "281831"
    assert info.summary_source == "f95" and info.release_source == "f95"
    assert info.release_date == "2026-10-25" and info.cover_source == "f95"
    assert ("Forbidden Thoughts", "original", "f95") in info.titles
    with lib.connection() as c:
        assert c.execute("SELECT name FROM brands").fetchone()[0] == "REN3D"
        assert c.execute("SELECT count(*) FROM work_brands WHERE work_id=1").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM covers WHERE work_id=1").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM source_tags WHERE site='f95'").fetchone()[0] == 24
        assert c.execute("SELECT count(*) FROM work_source_tags WHERE site='f95'").fetchone()[0] == 24


def test_bind_without_cover_still_binds(lib):
    make_work(lib)
    record = R.parse_f95_thread(fixture_text("f95_thread_281831.html"), "281831")
    B.apply_bind(lib, 1, 1, record)
    info = B.get_info(lib, 1)
    assert info.links["f95"] == "281831"
    with lib.connection() as c:
        assert c.execute("SELECT count(*) FROM covers").fetchone()[0] == 0
        assert c.execute("SELECT cover_source FROM work_info WHERE work_id=1").fetchone()[0] is None


def _v5_library(path):
    path = Path(path)
    with sqlite3.connect(path) as c:
        for n in range(1, 6):
            name = "schema.sql" if n == 1 else f"schema_v{n}.sql"
            c.executescript(Path(store.__file__).with_name(name).read_text(encoding="utf-8"))
        c.execute(f"PRAGMA application_id={store.APPLICATION_ID}")
        c.execute("PRAGMA user_version=5")
        c.execute("INSERT INTO library_info VALUES(?,?)", ("library_uuid", str(uuid.uuid4())))
        c.execute("INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(1,?,'旧作','x','x')",
                  (str(uuid.uuid4()),))
        c.execute("INSERT INTO work_info VALUES(1,'旧简介','vndb','2020-01-02','vndb','vndb','x')")
        c.execute("INSERT INTO work_titles VALUES(1,'旧作','旧作','original','vndb')")
        c.execute("INSERT INTO external_links VALUES(1,'vndb','v42','x')")
        c.execute("INSERT INTO metadata_snapshots VALUES(1,'vndb','v42','x','{\"id\":\"v42\"}')")
        c.execute("INSERT INTO brands(id,name,name_key,created_at) VALUES(1,'品牌','品牌','x')")
        c.execute("INSERT INTO brand_links VALUES('vndb','v42',1)")
        c.execute("INSERT INTO tags(id,group_id,name,name_key,created_at) VALUES(1,1,'标签','标签','x')")
        c.execute("INSERT INTO source_tags VALUES('vndb','tag1','标签','标签',NULL,'map',1,'x')")
        c.execute("INSERT INTO work_source_tags VALUES(1,'vndb','tag1')")
    return path


def test_v5_upgrade_keeps_every_row_and_accepts_new_sites(tmp_path):
    path = _v5_library(tmp_path / "v5.galdb")
    before = path.read_bytes()
    with pytest.raises(D.NeedsUpgrade) as err:
        store.open_library(str(path))
    assert err.value.version == 5 and path.read_bytes() == before
    with store.open_library(str(path), readonly=True) as ro:
        assert ro.schema_version == 5
    result = store.upgrade_library(str(path))
    assert (result.from_version, result.to_version) == (5, 6)
    tables = ("work_info", "work_titles", "external_links", "metadata_snapshots",
              "brand_links", "source_tags", "work_source_tags")
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("PRAGMA user_version").fetchone()[0] == 6
        assert c.execute("SELECT * FROM external_links").fetchall() == [(1, "vndb", "v42", "x")]
        assert c.execute("SELECT * FROM work_source_tags").fetchall() == [(1, "vndb", "tag1")]
        # Every site-constrained table accepts the two new sites.
        c.execute("UPDATE work_info SET summary_source='steam',release_source='f95',cover_source='steam'")
        c.execute("INSERT INTO work_titles VALUES(1,'新标题','新标题','alias','f95')")
        c.execute("INSERT INTO external_links VALUES(1,'steam','1144400','x')")
        c.execute("INSERT INTO external_links VALUES(1,'f95','281831','x')")
        c.execute("INSERT INTO metadata_snapshots(site,site_id,fetched_at,payload) "
                  "VALUES('f95','281831','x','{}')")
        c.execute("INSERT INTO brand_links VALUES('steam','dev',1)")
        c.execute("INSERT INTO source_tags(site,tag_key,text,suggestion) "
                  "VALUES('f95','tag2','Tag2','Tag2')")
        c.execute("INSERT INTO work_source_tags VALUES(1,'f95','tag2')")
        c.commit()
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        counts = {name: c.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in tables}
        assert counts == {"work_info": 1, "work_titles": 2, "external_links": 3,
                          "metadata_snapshots": 2, "brand_links": 2, "source_tags": 2,
                          "work_source_tags": 2}


def test_new_library_is_v6(tmp_path):
    with store.create_library(str(tmp_path / "new.galdb")) as lib:
        assert lib.schema_version == 6
        with lib.connection() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 6
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
            assert c.execute("SELECT name FROM sqlite_master WHERE name='idx_snapshots_item'").fetchone()
            assert c.execute("SELECT name FROM sqlite_master WHERE name='idx_work_source_tags_tag'").fetchone()
    with pytest.raises(ValueError, match="already version 6"):
        store.upgrade_library(str(tmp_path / "new.galdb"))


def test_ui_site_colors_cover_new_sites():
    from galpanel2.ui.externaldialogs import SITE_COLORS
    from galpanel2.ui.review import SITE_COLOR
    for site in ("steam", "f95"):
        assert site in SITE_COLORS and site in SITE_COLOR


def _thread_without_attachments():
    return re.sub(r"https://attachments\.f95zone\.to/[^\s\"'<>]+", "",
                  fixture_text("f95_thread_281831.html"))


def test_f95_fetch_uses_search_cover_when_thread_needs_login():
    html = _thread_without_attachments()
    client, transport = fake_client(
        S.Response(200, html.encode("utf-8"), {}),
        S.Response(200, json.dumps(fixture("f95_search_forbidden_thoughts.json")).encode("utf-8"), {}),
    )
    record = client.fetch(R.Ref("f95", "281831"))
    assert record.cover_url == "https://preview.f95zone.to/2026/07/6256969_output.gif"
    assert record.title == "Forbidden Thoughts"
    assert len(transport.calls) == 2
    assert "latest_data.php" in transport.calls[1]["url"]


def test_f95_fetch_without_matching_search_row_keeps_no_cover():
    html = _thread_without_attachments()
    payload = fixture("f95_search_forbidden_thoughts.json")
    payload["msg"]["data"][0]["thread_id"] = 999999
    client, transport = fake_client(
        S.Response(200, html.encode("utf-8"), {}),
        S.Response(200, json.dumps(payload).encode("utf-8"), {}),
    )
    record = client.fetch(R.Ref("f95", "281831"))
    assert record.cover_url is None
    assert record.title == "Forbidden Thoughts"
    assert len(transport.calls) == 2


F95_TITLE_PREFIXES = (
    "VN", "Ren'Py", "RenPy", "Unity", "RPGM", "RPG Maker", "HTML", "Others", "Other",
    "Completed", "Abandoned", "Onhold", "On hold", "Unreal Engine", "Flash", "QSP",
    "Java", "ADRIFT", "WebGL", "Wolf RPG", "Tyrano", "TyranoBuilder", "RAGS", "Godot",
    "Collection", "SiNVR", "Cheat Mod", "Mod", "Tool", "Tools", "Unchecked",
    "Real Porn", "Turn based",
)


def _thread_page(raw_title):
    return ('<html><head><meta property="og:title" content="' + raw_title + '" /></head>'
            '<body><article class="message-body"><div class="bbWrapper">Overview: 简介</div>'
            '</article></body></html>')


@pytest.mark.parametrize("prefix", F95_TITLE_PREFIXES)
def test_f95_title_strips_known_prefix(prefix):
    record = R.parse_f95_thread(_thread_page(f"{prefix} - Sample Title [v0.1] [Dev]"), "1")
    assert record.title == "Sample Title"
    assert record.extra == "v0.1"
    assert record.brands[0].name == "Dev"


@pytest.mark.parametrize("raw,want", [
    ("Unity - Happy Island Fantasy [v0.3] [xxx]", "Happy Island Fantasy"),
    ("Ren&#039;Py - Polyturnon [v1.2] [Dev]", "Polyturnon"),
    ("VN - Ren&#039;Py - RPG Maker - Stacked Title [v2] [Dev]", "Stacked Title"),
    ("HTML - Other - Completed - Done [1.0] [Dev]", "Done"),
])
def test_f95_title_strips_prefixes_after_html_unescape(raw, want):
    record = R.parse_f95_thread(_thread_page(raw), "1")
    assert record.title == want


def test_f95_fetch_prefers_search_cover_over_thread_attachment():
    thread_page = fixture_text("f95_thread_281831.html")
    parsed = R.parse_f95_thread(thread_page, "281831")
    assert parsed.cover_url.startswith("https://attachments.f95zone.to/")
    client, transport = fake_client(
        S.Response(200, thread_page.encode("utf-8"), {}),
        S.Response(200, json.dumps(fixture("f95_search_forbidden_thoughts.json")).encode("utf-8"), {}),
    )
    record = client.fetch(R.Ref("f95", "281831"))
    assert record.cover_url == "https://preview.f95zone.to/2026/07/6256969_output.gif"
    assert len(transport.calls) == 2
    assert "latest_data.php" in transport.calls[1]["url"]
