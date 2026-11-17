"""Close, switch, and removed-work fixes for the batch queue. No real network."""
import json
from dataclasses import replace
from pathlib import Path
import threading
import time
import uuid

import pytest

from galpanel2.core import batch as BT, binding as B, records as R, repository as repo, sites as S, store
from test_m2_core import Clock, data
from test_ui import app, window, wait


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected real transport")
    monkeypatch.setattr(S.UrllibTransport, "request", forbidden)


class HoldTransport:
    def __init__(self):
        self.gate = threading.Event()
        self.entered = threading.Event()
        self._lock = threading.Lock()
        self._held = False

    def request(self, method, url, headers, body, timeout):
        with self._lock:
            hold = not self._held
            self._held = True
        if hold:
            self.entered.set()
            self.gate.wait()
        if "vndb.org" in url:
            payload = b'{"results":[]}'
        elif "bgm.tv" in url:
            payload = b'{"data":[]}'
        else:
            payload = b'{"work":[]}'
        return S.Response(200, payload, {})


class DialogMarker:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def pump(app, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def drain(app, window, hold=None):
    if hold is not None:
        hold.gate.set()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        app.processEvents()
        runner = window.batch_runner
        if (runner is None or not runner.isRunning()) and not window.net_jobs:
            return
        time.sleep(0.01)


def make_library(path, titles):
    with store.create_library(str(path)) as lib:
        with lib.transaction() as c:
            for i, title in enumerate(titles, 1):
                c.execute(
                    "INSERT INTO works(id,uuid,title,added_at,updated_at) VALUES(?,?,?,'2020-01-01','2020-01-01')",
                    (i, str(uuid.uuid4()), title))
        return lib.uuid


def queue_item(work_id, title, status="none", candidates=None):
    return {"work_id": work_id, "title": title, "path": "", "status": status, "match_status": status,
            "candidates": candidates or [], "errors": {}, "sites": [], "unreachable": [],
            "searched_at": "2026-10-12T00:00:00Z", "note": ""}


def install_hold(monkeypatch):
    hold = HoldTransport()
    monkeypatch.setattr(S, "default_transport", lambda: hold)
    clock = Clock()
    monkeypatch.setattr(S, "DEFAULT_LIMITER", S.RateLimiter(clock.clock, clock.sleep))
    return hold


def launch_stuck(window, app, path, uid, hold):
    qs = BT.QueueStore(str(path), uid)
    qs.start([1, 2, 3])
    window.open_library_path(str(path))
    wait(app, window)
    window._launch_runner(qs)
    assert pump(app, hold.entered.is_set), "batch did not reach the transport"
    assert window.batch_runner is not None and window.batch_runner.isRunning()
    return qs


def remove_work(window, work_id):
    revision = repo.get_work(window.lib, work_id).revision
    repo.remove_work(window.lib, work_id, revision)


def instant_search(_client, _title, _paths, sites=R.SITES):
    return {"status": "none", "candidates": [], "errors": {}, "sites": list(sites), "unreachable": []}


def sure_item(work_id, title, site_id):
    record = replace(R.parse_vndb(data("vndb_vn.json"))[0], cover_url=None, site_id=site_id)
    candidate = BT.candidate_dict(R.Candidate(record, "ok", 1.0))
    return queue_item(work_id, title, "ok", [candidate])


def test_r1_close_waits_for_stuck_batch(window, app, tmp_path, monkeypatch):
    hold = install_hold(monkeypatch)
    path = tmp_path / "a.galdb"
    uid = make_library(path, ["甲", "乙", "丙"])
    try:
        qs = launch_stuck(window, app, path, uid, hold)
        window.close()
        assert window.isVisible()
        assert window.closing is True
        assert "正在等后台找资料停下" in window.status.message.text()
        assert window.batch_runner in window.net_jobs
        assert not window.batch_runner._cancel.is_set()
        assert qs.snapshot()["state"] != "cancelled"
        hold.gate.set()
        assert pump(app, lambda: not window.isVisible())
        snap = qs.snapshot()
        assert snap["state"] == "paused"
        assert 2 in snap["todo"] and 3 in snap["todo"]
    finally:
        drain(app, window, hold)


def test_r1_switch_library_refused_while_stuck(window, app, tmp_path, monkeypatch):
    hold = install_hold(monkeypatch)
    path_a = tmp_path / "a.galdb"
    path_b = tmp_path / "b.galdb"
    uid_a = make_library(path_a, ["甲", "乙", "丙"])
    uid_b = make_library(path_b, ["丁"])
    try:
        launch_stuck(window, app, path_a, uid_a, hold)
        lib = window.lib
        plan = {"stay": True}
        window.scan_page.plan = plan
        dialog = DialogMarker()
        window.find_dialog = dialog
        window.tag_mapping = dialog
        window.open_library_path(str(path_b))
        assert window.lib is lib
        assert "后台找资料还在停下" in window.last_message
        assert window.scan_page.plan is plan and not dialog.closed
        window.show_title()
        assert window.lib is lib
        assert "后台找资料还在停下" in window.last_message
        assert window.scan_page.plan is plan and not dialog.closed
        hold.gate.set()
        assert pump(app, lambda: window.batch_runner is None or not window.batch_runner.isRunning())
        window.open_library_path(str(path_b))
        wait(app, window)
        assert window.lib is not None and window.lib.uuid == uid_b
    finally:
        drain(app, window, hold)


def test_r1_stop_net_does_not_cancel_batch(window, app, tmp_path, monkeypatch):
    hold = install_hold(monkeypatch)
    path = tmp_path / "a.galdb"
    uid = make_library(path, ["甲", "乙", "丙"])
    try:
        qs = launch_stuck(window, app, path, uid, hold)
        runner = window.batch_runner
        called = []
        original = runner.cancel

        def spy():
            called.append(1)
            original()

        runner.cancel = spy
        assert window._stop_net() is True
        assert called == []
        assert not runner._cancel.is_set()
        assert runner in window.net_jobs and runner.isRunning()
        assert qs.snapshot()["state"] != "cancelled"
    finally:
        drain(app, window, hold)


def test_r2_note_bound_uses_current_library_queue(window, app, tmp_path):
    path_a = tmp_path / "a.galdb"
    path_b = tmp_path / "b.galdb"
    uid_a = make_library(path_a, ["甲"])
    make_library(path_b, ["乙"])
    qs = BT.QueueStore(str(path_a), uid_a)
    qs.start([1])
    qs.put_item(queue_item(1, "甲", "ok"))
    qs.set_state("paused")
    before = Path(qs.path).read_bytes()
    window.open_library_path(str(path_a))
    wait(app, window)
    window.show_review()
    window.review_page.checked.add(1)
    assert any(item.get("work_id") == 1 for item in window.review_page.snap.get("items") or [])
    window.open_library_path(str(path_b))
    wait(app, window)
    assert window.review_page.store is None
    assert window.review_page.checked == set()
    assert window.review_page.snap.get("items") == [] and window.review_page.snap.get("todo") == []
    window.review_page.note_bound(1)
    assert Path(qs.path).read_bytes() == before
    assert not Path(BT.queue_path(str(path_b))).exists()


def test_r2_note_bound_marks_current_queue(window, app, tmp_path):
    path_b = tmp_path / "b.galdb"
    uid_b = make_library(path_b, ["乙"])
    qs = BT.QueueStore(str(path_b), uid_b)
    qs.start([1])
    qs.put_item(queue_item(1, "乙", "near"))
    qs.set_state("paused")
    window.open_library_path(str(path_b))
    wait(app, window)
    window.review_page.note_bound(1)
    item = next(x for x in qs.snapshot()["items"] if x["work_id"] == 1)
    assert item["status"] == "bound"


def test_r3_resume_drops_removed_todo(window, app, tmp_path, monkeypatch):
    monkeypatch.setattr(BT, "search_one", instant_search)
    path = tmp_path / "r.galdb"
    uid = make_library(path, ["甲", "乙", "丙"])
    qs = BT.QueueStore(str(path), uid)
    qs.start([1, 2, 3])
    qs.set_state("paused")
    window.open_library_path(str(path))
    wait(app, window)
    remove_work(window, 2)
    window.reload_library()
    wait(app, window)
    assert 2 not in window.library_page.details
    window.review_page.resume()
    assert window.batch_runner is not None
    assert "1 部已经不在书架上" in window.status.message.text()
    snap = qs.snapshot()
    assert 2 not in snap["todo"] and all(item.get("work_id") != 2 for item in snap["items"])
    assert pump(app, lambda: window.batch_runner is None)
    snap = qs.snapshot()
    assert snap["state"] == "done"
    assert 2 not in snap["todo"] and all(item.get("work_id") != 2 for item in snap["items"])


def test_r3_resume_all_removed_marks_done(window, app, tmp_path):
    path = tmp_path / "r.galdb"
    uid = make_library(path, ["甲", "乙", "丙"])
    qs = BT.QueueStore(str(path), uid)
    qs.start([1, 2, 3])
    qs.set_state("paused")
    window.open_library_path(str(path))
    wait(app, window)
    for work_id in (1, 2, 3):
        remove_work(window, work_id)
    window.reload_library()
    wait(app, window)
    window.review_page.resume()
    assert window.batch_runner is None
    assert qs.snapshot()["state"] == "done"
    assert qs.snapshot()["todo"] == []


def test_r3_confirm_skips_removed_work(window, app, tmp_path):
    path = tmp_path / "r.galdb"
    uid = make_library(path, ["甲", "乙"])
    qs = BT.QueueStore(str(path), uid)
    qs.start([1, 2])
    qs.put_item(sure_item(1, "甲", "v10001"))
    qs.put_item(sure_item(2, "乙", "v10002"))
    qs.set_state("paused")
    window.open_library_path(str(path))
    wait(app, window)
    window.show_review()
    assert [item["work_id"] for item in window.review_page.snap["items"]] == [1, 2]
    assert all(BT.is_sure(item) for item in window.review_page.snap["items"])
    remove_work(window, 1)
    window.review_page.confirm_sure()
    wait(app, window)
    assert window.status.message.text() == "绑定了 1 部，0 部没成功（原因写在各行里），1 部已经不在书架上"
    snap = qs.snapshot()
    assert all(item.get("work_id") != 1 for item in snap["items"]) and 1 not in snap["todo"]
    assert B.get_info(window.lib, 2).links.get("vndb") == "v10002"
    assert next(item["status"] for item in snap["items"] if item["work_id"] == 2) == "bound"


def test_r3_not_game_on_removed_work(window, app, tmp_path):
    path = tmp_path / "r.galdb"
    uid = make_library(path, ["甲"])
    qs = BT.QueueStore(str(path), uid)
    qs.start([1])
    qs.put_item(queue_item(1, "甲", "none"))
    qs.set_state("paused")
    window.open_library_path(str(path))
    wait(app, window)
    window.show_review()
    remove_work(window, 1)
    window.review_page.not_game(1)
    wait(app, window)
    snap = qs.snapshot()
    assert window.fatal_error is None
    assert all(item.get("work_id") != 1 for item in snap["items"]) and 1 not in snap["todo"]


def test_drop_is_atomic_and_noop_without_file(tmp_path):
    absent = tmp_path / "missing.galdb"
    missing = BT.QueueStore(str(absent), "uuid-missing")
    missing.drop([1, 2])
    assert not Path(missing.path).exists()
    path = tmp_path / "queue.galdb"
    qs = BT.QueueStore(str(path), "uuid-1")
    qs.start([1, 2, 3])
    kept = queue_item(2, "乙", "ok")
    kept["note"] = "keep"
    qs.put_item(queue_item(1, "甲", "none"))
    qs.put_item(kept)
    before = qs.snapshot()
    qs.drop([1, 3])
    data_text = Path(qs.path).read_text(encoding="utf-8")
    loaded = json.loads(data_text)
    assert loaded["todo"] == []
    assert loaded["items"] == [kept]
    assert loaded["state"] == before["state"] == "running"
    assert loaded["library_uuid"] == before["library_uuid"] == "uuid-1"
    assert loaded["created_at"] == before["created_at"]
    assert loaded["version"] == 1
