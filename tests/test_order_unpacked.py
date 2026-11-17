"""Sort direction toggle and the "未解压" tag for works that also have a folder."""
from galpanel2.core.domain import WorkSummary
from galpanel2.ui import library as L
from test_ui import app, window, wait
from test_lite_b import lite_b_library, opened, btn, click


def summary(kinds):
    return WorkSummary(1, "t", "", False, "2026-01-01T00:00:00Z", True, len(kinds), tuple(sorted(set(kinds))), "ok")


def test_archive_beside_folder_is_not_packed():
    assert L.file_tag(summary(["archive"])) == "未解压"
    assert L.file_tag(summary(["archive", "folder"])) == ""
    assert L.is_packed_only(summary(["archive"])) and not L.is_packed_only(summary(["folder", "archive"]))


def test_added_order_defaults_newest_first_and_flips(app, window, lite_b_library):
    p = opened(app, window, lite_b_library)
    assert p.order.currentIndex() == 0 and p.order_desc
    assert p.visible_work_ids() == [6, 4, 3, 2, 1]
    click(btn(p, "orderDir"))
    assert not p.order_desc and p.visible_work_ids() == [1, 2, 3, 4, 6]
    p.order.setCurrentIndex(1)
    assert not p.order_desc and [w.title for w in p.model.works] == ["Alpha", "Bravo", "Charlie", "Delta", "Foxtrot"]
    click(btn(p, "orderDir"))
    assert [w.title for w in p.model.works] == ["Foxtrot", "Delta", "Charlie", "Bravo", "Alpha"]
    p.order.setCurrentIndex(0)
    assert p.order_desc and p.visible_work_ids()[0] == 6
    click(btn(p, "segRecent"))
    assert not p.order_dir.isEnabled()
