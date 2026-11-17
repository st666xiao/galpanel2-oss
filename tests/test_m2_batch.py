"""Batch queue, unreachable-site skipping, and the review page. No real network."""
import json
from pathlib import Path
import uuid

import pytest

from galpanel2.core import batch as BT, domain as D, records as R, sites as S, store
from galpanel2.ui.window import Window
from test_m2_core import Clock
from test_ui import app, window, wait


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected real transport")
    monkeypatch.setattr(S.UrllibTransport, "request", forbidden)


class Boom:
    def __init__(self, exc):
        self.exc = exc
        self.n = 0

    def request(self, *_args):
        self.n += 1
        raise self.exc


class Status:
    def __init__(self, status):
        self.n = 0
        self.status = status

    def request(self, *_args):
        self.n += 1
        return S.Response(self.status, b"{}", {})


def client(transport):
    clock = Clock()
    return S.SiteClient(transport, limiter=S.RateLimiter(clock.clock, clock.sleep)), transport


def test_connection_failures_are_unreachable():
    message = "连不上 DLsite：连接失败"
    for exc in (TimeoutError("t"), ConnectionRefusedError(10061, "x"), OSError("x")):
        c, transport = client(Boom(exc))
        with pytest.raises(D.SiteUnreachable) as caught:
            c.search("dlsite", "x")
        assert (str(caught.value), caught.value.site) == (message, "dlsite")
        assert transport.n == 1
    c, _transport = client(Boom(D.SiteError("连不上 DLsite：URLError", "dlsite")))
    with pytest.raises(D.SiteError) as caught:
        c.search("dlsite", "x")
    assert not isinstance(caught.value, D.SiteUnreachable)
    c, transport = client(Status(500))
    with pytest.raises(D.SiteError) as caught:
        c.search("dlsite", "x")
    assert not isinstance(caught.value, D.SiteUnreachable) and transport.n == 1


def test_fake_unreachable_is_logged_and_not_retried(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": [
        {"method": "GET", "url_prefix": "https://www.dlsite.com/", "unreachable": True},
        {"method": "POST", "url_prefix": "https://api.vndb.org/", "json": {"results": [], "more": False}},
        {"method": "POST", "url_prefix": "https://api.bgm.tv/", "json": {"data": [], "total": 0}},
    ]}, ensure_ascii=False), encoding="utf-8")
    c, _transport = client(S.FileTransport(str(path)))
    with pytest.raises(D.SiteUnreachable) as caught:
        c.search("dlsite", "x")
    assert str(caught.value) == "连不上 DLsite：连接失败"
    log = [json.loads(line) for line in Path(str(path) + ".log").read_text(encoding="utf-8").splitlines()]
    assert len(log) == 1 and log[0]["url"].startswith("https://www.dlsite.com/")
    seen = set()
    rows, errors = c.search_all("普通书名", unreachable=seen)
    assert seen == {"dlsite"} and errors["dlsite"] == "连不上 DLsite：连接失败" and "vndb" not in errors
    assert rows == [] or all(row.site != "dlsite" for row in rows)


def test_reachability_resets_and_ignores_sites_not_asked():
    assert (BT.UNREACHABLE_LIMIT, BT.SKIPPED_TEXT) == (3, "连不上，已跳过")
    rc = BT.Reachability()
    assert rc.sites() == R.SITES and rc.skipped == frozenset()

    def asked(*sites):
        return {"sites": list(R.SITES), "unreachable": list(sites)}

    assert rc.record(asked("dlsite")) == [] and rc.record(asked("dlsite")) == []
    assert rc.record(asked()) == []
    assert rc.record(asked("dlsite")) == [] and rc.record(asked("dlsite")) == []
    assert rc.record({"sites": ["vndb"], "unreachable": []}) == []
    assert rc.record(asked("dlsite", "vndb")) == ["dlsite"]
    assert rc.skipped == frozenset({"dlsite"}) and rc.sites() == tuple(s for s in R.SITES if s != "dlsite")
    assert rc.record({"sites": ["vndb", "bangumi"], "unreachable": ["vndb"]}) == []
    assert rc.record({"sites": ["vndb", "bangumi"], "unreachable": ["vndb"]}) == ["vndb"]
    assert rc.record({"status": "none", "candidates": [], "errors": {}}) == []
    assert BT.Reachability(limit=1).record(asked("bangumi")) == ["bangumi"]
    assert BT.skipped_notice(frozenset({"dlsite"})) == (
        "DLsite 连续 3 部连不上，这一轮不再搜它。检查网络或代理后，暂停再继续会重新尝试。")
    assert BT.skipped_notice(frozenset()) == ""


def test_skipped_code_and_empty_site_list_do_not_request():
    transport = Boom(AssertionError("should not request"))
    c, transport = client(transport)
    item = BT.search_one(c, "RJ01014447", [], sites=())
    assert item["status"] == "error" and item["sites"] == [] and item["unreachable"] == []
    assert item["errors"] == {"dlsite": BT.SKIPPED_TEXT} and transport.n == 0
    item = BT.search_one(c, "普通书名", [], sites=())
    assert item["status"] == "error" and item["sites"] == [] and item["candidates"] == []
    assert item["errors"] == {site: BT.SKIPPED_TEXT for site in R.SITES} and transport.n == 0
    c, transport = client(Boom(TimeoutError("t")))
    item = BT.search_one(c, "whatever", [r"D:\Library\RJ01014447"])
    assert item["status"] == "error" and item["sites"] == ["dlsite"] and item["unreachable"] == ["dlsite"]
    assert item["errors"]["dlsite"] == "连不上 DLsite：连接失败"
    with pytest.raises(D.Cancelled):
        BT.search_one(S.SiteClient(Boom(AssertionError("x")), limiter=S.RateLimiter(Clock().clock, Clock().sleep),
                                   cancel=lambda: True), "普通书名", [])


def test_queue_store_keeps_a_bad_file(tmp_path):
    path = tmp_path / "queue_test.galdb"
    qs = BT.QueueStore(str(path), "uuid-1")
    assert qs.load() is None and BT.queue_path(str(path)) == str(path) + ".m2queue.json"
    qs.start([3, 1, 2])
    assert qs.next_todo() == 3
    qs.put_item({"work_id": 3, "title": "t", "path": "p", "status": "ok", "candidates": [], "errors": {},
                 "searched_at": "2026-10-12T00:00:00Z", "note": ""})
    qs.set_status(3, "skipped", "n")
    snap = qs.snapshot()
    snap["items"][0]["status"] = "mutated"
    assert qs.snapshot()["items"][0]["status"] == "skipped" and qs.snapshot()["items"][0]["note"] == "n"
    assert BT.QueueStore(str(path), "other").load() is None
    Path(BT.queue_path(str(path))).write_text("{bad json", encoding="utf-8")
    assert BT.QueueStore(str(path), "uuid-1").load() is None
    assert list(path.parent.glob("queue_test.galdb.m2queue.json.bad-*"))


def test_confirm_set_rules():
    def cand(site, grade, sid=None):
        return {"site": site, "site_id": sid or f"{site}-{grade}", "grade": grade, "score": 1.0, "record": {}}
    sure = {"status": "ok", "candidates": [cand("vndb", "ok"), cand("bangumi", "ok"), cand("dlsite", "near")]}
    assert [x["site"] for x in BT.confirm_set(sure)] == ["vndb", "bangumi"] and BT.is_sure(sure)
    mixed = {"status": "ok", "candidates": [cand("vndb", "ok", "v1"), cand("vndb", "ok", "v2"), cand("dlsite", "no")]}
    assert [x["site_id"] for x in BT.confirm_set(mixed)] == ["v1"] and not BT.is_sure(mixed)
    near = {"status": "near", "candidates": [cand("dlsite", "near"), cand("vndb", "no")]}
    assert [x["site"] for x in BT.confirm_set(near)] == ["dlsite"] and not BT.is_sure(near)
    for status in ("none", "error"):
        item = {"status": status, "candidates": [cand("dlsite", "no")]}
        assert BT.confirm_set(item) == [] and not BT.is_sure(item)


def test_resume_asks_skipped_sites_again(window, app, tmp_path, monkeypatch):
    path = tmp_path / "b.galdb"
    with store.create_library(str(path)) as lib:
        with lib.transaction() as c:
            for wid, title in ((1, "甲"), (2, "乙")):
                c.execute("INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(?,?,?,'2020-01-01','2020-01-01')",
                          (wid, str(uuid.uuid4()), title))
        uid = lib.uuid
    qs = BT.QueueStore(str(path), uid)
    qs.start([1, 2])
    qs.put_item({"work_id": 1, "title": "甲", "path": "", "status": "none", "candidates": [], "errors": {},
                 "sites": list(R.SITES), "unreachable": ["dlsite"], "searched_at": "2026-10-12T00:00:00Z",
                 "note": "", "match_status": "none"})
    qs.set_state("paused")
    seen = []

    def fake(_client, _title, _paths, sites=R.SITES):
        seen.append(tuple(sites))
        return {"status": "none", "candidates": [], "errors": {}, "sites": list(sites), "unreachable": []}

    monkeypatch.setattr(BT, "search_one", fake)
    window.open_library_path(str(path))
    wait(app, window)
    window.show_review()
    reach = BT.Reachability()
    assert reach.record({"sites": list(R.SITES), "unreachable": ["dlsite"]}) == []
    assert reach.record({"sites": list(R.SITES), "unreachable": ["dlsite"]}) == []
    assert reach.record({"sites": list(R.SITES), "unreachable": ["dlsite"]}) == ["dlsite"]
    window.review_page.reach = reach
    window.review_page._show_notice()
    assert window.review_page.notice_text() == BT.skipped_notice(frozenset({"dlsite"}))
    window.review_page.resume()
    assert window.review_page.notice_text() == ""
    wait(app, window)
    assert seen == [tuple(R.SITES)]
