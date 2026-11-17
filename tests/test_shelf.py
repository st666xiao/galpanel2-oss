"""Tests for galpanel2.core.shelf (E1: category / status storage)."""
import os
import sqlite3
import uuid

import pytest

from galpanel2.core import repository, store, tidy
from galpanel2.core import shelf


def _add(lib, wid, title="标题", hidden=0):
    with lib.transaction() as c:
        c.execute(
            "INSERT INTO works(id,uuid,title,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?)",
            (wid, str(uuid.uuid4()), title, hidden, "2020-01-01", "2020-01-01"),
        )


def test_constants_and_dataclass():
    assert tuple(shelf.CATEGORIES) == ("日系", "同人", "欧美", "其他")
    assert tuple(shelf.STATUSES) == ("想玩", "在玩", "玩过", "不玩了")
    s = shelf.Shelf("日系", None)
    assert (s.category, s.status) == ("日系", None)
    assert s == shelf.Shelf("日系", None)


def test_empty_read_creates_nothing_and_readonly_ok(lib):
    # No table yet: list_shelf returns {} and must not create the table.
    assert shelf.list_shelf(lib) == {}
    path = lib.path
    c = sqlite3.connect(path)
    try:
        assert not c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_shelf'"
        ).fetchone()
        assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        c.close()
    lib.close()
    with store.open_library(path, readonly=True) as ro:
        assert shelf.list_shelf(ro) == {}
    c2 = sqlite3.connect(path)
    try:
        assert not c2.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_shelf'"
        ).fetchone()
    finally:
        c2.close()


def test_roundtrip_and_reopen(lib):
    _add(lib, 1)
    shelf.set_category(lib, 1, "日系")
    assert shelf.list_shelf(lib)[1] == shelf.Shelf("日系", None)
    shelf.set_status(lib, 1, "在玩")
    assert shelf.list_shelf(lib)[1] == shelf.Shelf("日系", "在玩"), "设状态不动分类"
    shelf.set_category(lib, 1, "同人")
    assert shelf.list_shelf(lib)[1] == shelf.Shelf("同人", "在玩"), "设分类不动状态"
    shelf.set_status(lib, 1, None)
    assert shelf.list_shelf(lib)[1] == shelf.Shelf("同人", None)
    shelf.set_category(lib, 1, None)
    assert shelf.list_shelf(lib).get(1, shelf.Shelf(None, None)) == shelf.Shelf(None, None)
    _add(lib, 2, "第二部")
    shelf.set_status(lib, 2, "玩过")
    path = lib.path
    lib.close()
    with store.open_library(path) as again:
        assert again.schema_version == 6
        assert shelf.list_shelf(again)[2].status == "玩过"
    with store.open_library(path, readonly=True) as ro:
        assert shelf.list_shelf(ro)[2].status == "玩过"


def test_invalid_values_and_missing_work(lib):
    _add(lib, 1)
    shelf.set_status(lib, 1, "想玩")
    for fn, bad in (
        (shelf.set_category, "国产"),
        (shelf.set_status, "已通关"),
        (shelf.set_status, ""),
        (shelf.set_category, "日系 "),
    ):
        with pytest.raises(ValueError):
            fn(lib, 1, bad)
    for fn, ok in ((shelf.set_category, "日系"), (shelf.set_status, "想玩")):
        with pytest.raises(KeyError):
            fn(lib, 999, ok)
    assert shelf.list_shelf(lib)[1] == shelf.Shelf(None, "想玩")
    assert 999 not in shelf.list_shelf(lib)


def test_revision_untouched(lib):
    _add(lib, 1)

    def rev():
        with lib.connection() as c:
            return c.execute("SELECT revision FROM works WHERE id=1").fetchone()[0]

    before = rev()
    shelf.set_category(lib, 1, "欧美")
    shelf.set_status(lib, 1, "玩过")
    shelf.set_categories(lib, {1: "日系"}, overwrite=True)
    assert rev() == before


def test_bulk_set_categories(lib):
    for i in (1, 2, 3, 4, 5):
        _add(lib, i)
    shelf.set_category(lib, 1, "日系")
    shelf.set_status(lib, 2, "想玩")
    n = shelf.set_categories(lib, {1: "欧美", 2: "同人", 3: "其他", 999: "日系"})
    assert n == 2
    assert shelf.list_shelf(lib)[1].category == "日系", "overwrite=False 不覆盖"
    assert shelf.list_shelf(lib)[2] == shelf.Shelf("同人", "想玩"), "不动状态"
    assert shelf.list_shelf(lib)[3].category == "其他"
    n = shelf.set_categories(lib, {1: "欧美", 3: "其他"}, overwrite=True)
    assert n == 1 and shelf.list_shelf(lib)[1].category == "欧美"
    with pytest.raises(ValueError):
        shelf.set_categories(lib, {4: "同人", 5: "不存在的分类"})
    assert shelf.list_shelf(lib).get(4, shelf.Shelf(None, None)).category is None
    assert shelf.list_shelf(lib).get(5, shelf.Shelf(None, None)).category is None


def test_cascade_delete_and_backup(lib):
    _add(lib, 1)
    _add(lib, 2)
    shelf.set_category(lib, 1, "日系")
    shelf.set_status(lib, 2, "在玩")
    backup = tidy.backup_library(lib, "emby-test")
    assert backup and os.path.isfile(str(backup))
    with lib.connection() as c:
        rev = c.execute("SELECT revision FROM works WHERE id=1").fetchone()[0]
    repository.remove_work(lib, 1, rev)
    assert 1 not in shelf.list_shelf(lib)
    with lib.connection() as c:
        assert not c.execute("SELECT 1 FROM work_shelf WHERE work_id=1").fetchone()
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert shelf.list_shelf(lib)[2].status == "在玩"


def test_readonly_write_fails_but_read_works(lib):
    _add(lib, 1)
    shelf.set_status(lib, 1, "想玩")
    path = lib.path
    lib.close()
    with store.open_library(path, readonly=True) as ro:
        with pytest.raises(Exception):
            shelf.set_status(ro, 1, "玩过")
        assert shelf.list_shelf(ro)[1].status == "想玩"


def test_guess_category():
    g = shelf.guess_category
    cases = {
        r"D:\Library\Games\欧美\Foo\Bar": "欧美",
        r"D:\Library\games\日系\品牌\标题": "日系",
        "D:/x/Games/同人/a": "同人",
        r"E:\GAMES\其他\x.zip": "其他",
        r"D:\Games\Foo\日系": None,
        r"D:\日系\Foo": None,
        r"D:\Library\Games": None,
        r"D:\Library\Games\日系": "日系",
        "": None,
        r"D:\MyGames\欧美\x": None,
    }
    for p, want in cases.items():
        assert g(p) == want, p


def test_list_shelf_only_existing_works(lib):
    _add(lib, 1)
    _add(lib, 2)
    shelf.set_category(lib, 1, "日系")
    shelf.set_status(lib, 2, "在玩")
    # Insert a dangling row is impossible with the FK, but a deleted work's
    # row must disappear from list_shelf via the JOIN / cascade.
    with lib.connection() as c:
        rev = c.execute("SELECT revision FROM works WHERE id=2").fetchone()[0]
    repository.remove_work(lib, 2, rev)
    got = shelf.list_shelf(lib)
    assert 1 in got and 2 not in got
