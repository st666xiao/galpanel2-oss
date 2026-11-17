"""Tidy-b core: dismiss sidecar, search refs, and missing-work ids."""
import json
from pathlib import Path

import pytest

from galpanel2.core import batch, domain as D, records as R, store, tidy
from test_tidy import NOW, _work


def _empty():
    return {"junk": set(), "titles": set(), "duplicates": set()}


def _record(ref):
    return R.Record(ref.site, ref.site_id, "", "T", None, (), (), None, None, None, (), 0, "", True, "{}")


class _Stub:
    def __init__(self):
        self.fetched = []
        self.searched = []

    def fetch(self, ref):
        self.fetched.append(ref)
        return _record(ref)

    def search_all(self, text, sites=(), unreachable=None):
        self.searched.append(text)
        return [], {}


def test_dismiss_store_roundtrip_and_uuid(lib):
    assert tidy.dismiss_path(lib.path) == lib.path + ".tidy-dismissed.json"
    side = tidy.DismissStore(lib.path, lib.uuid)
    assert side.load() == _empty()
    path = Path(tidy.dismiss_path(lib.path))
    assert not path.exists()
    side.add("junk", 3)
    side.add("junk", 1)
    side.add("junk", 1)
    side.add("titles", 2)
    side.add("duplicates", [9, 8])
    side.add("duplicates", (2, 1))
    side.add("duplicates", (8, 9))
    got = tidy.DismissStore(lib.path, lib.uuid).load()
    assert got["junk"] == {1, 3} and got["titles"] == {2}
    assert got["duplicates"] == {(1, 2), (8, 9)}
    assert all(type(item) is int for item in got["junk"] | got["titles"])
    assert all(isinstance(item, tuple) and all(type(n) is int for n in item) for item in got["duplicates"])
    raw_text = path.read_text(encoding="utf-8")
    assert "\r" not in raw_text
    raw = json.loads(raw_text)
    assert raw["version"] == 1 and raw["library_uuid"] == lib.uuid
    assert raw["junk"] == [1, 3] and raw["titles"] == [2]
    assert raw["duplicates"] == [[1, 2], [8, 9]]
    before = path.read_text(encoding="utf-8")
    assert tidy.DismissStore(lib.path, "other-uuid").load() == _empty()
    assert path.read_text(encoding="utf-8") == before


def test_dismiss_store_drop_works(lib):
    side = tidy.DismissStore(lib.path, lib.uuid)
    side.drop_works([1])
    assert side.load() == _empty()
    assert not Path(tidy.dismiss_path(lib.path)).exists()
    side.add("junk", 1)
    side.add("junk", 2)
    side.add("titles", 2)
    side.add("titles", 3)
    side.add("duplicates", [1, 4])
    side.add("duplicates", [5, 6])
    side.drop_works([2, 5])
    got = side.load()
    assert got["junk"] == {1} and got["titles"] == {3} and got["duplicates"] == {(1, 4)}
    side.drop_works([999])
    assert side.load()["duplicates"] == {(1, 4)}
    side.drop_works([])
    assert side.load()["junk"] == {1}
    side.drop_works([True, "2"])
    assert side.load()["junk"] == {1}


def test_dismiss_store_bad_kind_and_corrupt_file(lib):
    side = tidy.DismissStore(lib.path, lib.uuid)
    for kind in ("duplicate", "Duplicates", "", "junks"):
        with pytest.raises(ValueError):
            side.add(kind, 1)
    with pytest.raises(ValueError):
        side.add("junk", True)
    with pytest.raises(ValueError):
        side.add("duplicates", "12")
    assert side.load() == _empty()
    path = Path(tidy.dismiss_path(lib.path))
    side.add("junk", 4)
    path.write_text("{not json", encoding="utf-8")
    assert tidy.DismissStore(lib.path, lib.uuid).load() == _empty()
    side.add("titles", 9)
    assert tidy.DismissStore(lib.path, lib.uuid).load()["titles"] == {9}
    path.write_text("[]", encoding="utf-8")
    assert side.load() == _empty()
    path.write_text(json.dumps({"version": 2, "library_uuid": lib.uuid, "junk": [1],
                                "titles": [], "duplicates": []}), encoding="utf-8")
    assert side.load() == _empty()
    assert not path.exists()
    assert list(path.parent.glob(path.name + ".bad-*"))


def test_search_one_uses_given_ref():
    params = __import__("inspect").signature(batch.search_one).parameters
    assert params["ref"].default is None
    title = "Hello RJ123456"
    paths = [r"C:\gp2-tidy-b\batch\RJ999999\game"]
    given = R.Ref("vndb", "v9")
    stub = _Stub()
    item = batch.search_one(stub, title, paths, ref=given)
    assert stub.fetched == [given] and stub.searched == []
    assert item["status"] == "ok" and item["candidates"][0]["site_id"] == "v9"
    stub = _Stub()
    batch.search_one(stub, title, paths)
    assert stub.fetched == [R.Ref("dlsite", "RJ123456")] and stub.searched == []
    stub = _Stub()
    batch.search_one(stub, title, paths, ref=None)
    assert stub.fetched == [R.Ref("dlsite", "RJ123456")]
    stub = _Stub()
    plain = batch.search_one(stub, "Plain Title", [])
    assert stub.fetched == [] and stub.searched == [R.search_text("Plain Title")]
    assert plain["status"] in ("none", "error", "near", "ok")
    stub = _Stub()
    skipped = batch.search_one(stub, title, paths, sites=("dlsite",), ref=given)
    assert stub.fetched == [] and skipped["status"] == "error"


def test_queue_refs_saved_dropped_and_old_files(lib):
    params = __import__("inspect").signature(batch.QueueStore.start).parameters
    assert params["refs"].default is None
    queue = batch.QueueStore(lib.path, lib.uuid)
    queue.start([4, 5])
    raw = json.loads(Path(queue.path).read_text(encoding="utf-8"))
    assert "refs" not in raw and raw["todo"] == [4, 5]
    queue.start([1, 2, 3], refs={1: "vndb:v10", 3: "dlsite:RJ000001"})
    raw = json.loads(Path(queue.path).read_text(encoding="utf-8"))
    assert raw["refs"] == {"1": "vndb:v10", "3": "dlsite:RJ000001"}
    assert 1 not in raw["refs"]
    queue.drop([1])
    raw = json.loads(Path(queue.path).read_text(encoding="utf-8"))
    assert raw["todo"] == [2, 3] and raw["refs"] == {"3": "dlsite:RJ000001"}
    old = {"version": 1, "library_uuid": lib.uuid, "created_at": NOW, "state": "running",
           "todo": [7, 8], "items": [{"work_id": 8, "status": "none"}]}
    Path(queue.path).write_text(json.dumps(old), encoding="utf-8")
    loaded = queue.load()
    assert loaded["todo"] == [7, 8] and loaded["items"][0]["work_id"] == 8
    assert not loaded.get("refs")
    queue.drop([7])
    assert queue.load()["todo"] == [8]


def test_missing_work_ids(lib):
    assert tidy.missing_work_ids(lib) == set()
    root = r"C:\gp2-tidy-b\c3"
    a, _ = _work(lib, "缺甲", [{"path": root + r"\Alpha\a", "availability": "missing"}])
    b, _ = _work(lib, "缺乙", [{"path": root + r"\Alpha\b", "availability": "missing"}])
    c, _ = _work(lib, "缺丙", [{"path": root + r"\Beta\c", "availability": "missing"}])
    d, _ = _work(lib, "还在", [{"path": root + r"\here", "availability": "ok"}])
    groups = tidy.missing_groups(lib)
    want = set()
    for group in groups:
        want.update(group["work_ids"])
    assert tidy.missing_work_ids(lib) == want == {a, b, c}
    assert d not in want
    assert D.path_key(root + r"\Alpha\a").startswith("c:\\")


def test_use_legacy_summary_unescapes_without_rewriting_legacy_fields(lib):
    from test_tidy import _leg
    raw = 'She said &quot;hi&quot; &amp; left'
    wid, _ = _work(lib, "实体简介", [{"path": r"C:\gp2-tidy-b\c3\entity"}])
    _leg(lib, wid, "description", "text", raw)
    with lib.connection() as c:
        before = c.execute(
            "SELECT value_json FROM legacy_fields WHERE work_id=? AND column_name='description'", (wid,)).fetchone()[0]
    assert "&quot;" in before and "&amp;" in before
    rows = {row["work_id"]: row for row in tidy.legacy_report(lib)}
    assert rows[wid]["summary"] == raw
    assert tidy.legacy_summary_text(raw) == 'She said "hi" & left'
    assert tidy.legacy_summary_text("有简介") == "有简介"
    assert tidy.use_legacy_summary(lib, wid) is True
    with lib.connection() as c:
        after = c.execute(
            "SELECT value_json FROM legacy_fields WHERE work_id=? AND column_name='description'", (wid,)).fetchone()[0]
        stored = c.execute("SELECT summary, summary_source FROM work_info WHERE work_id=?", (wid,)).fetchone()
    assert after == before
    assert (stored[0], stored[1]) == ('She said "hi" & left', "user")
