"""Files deleted outside the program: quiet detail-open checks and stale cards."""
import shutil
import threading
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage

from galpanel2.core import repository as repo
from galpanel2.core import scanning as s
from galpanel2.core import store
from galpanel2.core.domain import WorkSummary, path_key
from galpanel2.ui import library as L
from test_ui import app, wait, window  # noqa: F401  (shared fixtures)


def _key(path):
    try:
        return path_key(str(path))
    except ValueError:
        return str(path).replace("\\", "/").casefold()


def _work(lib, wid, title):
    with lib.transaction() as c:
        c.execute("INSERT INTO works(id,uuid,title,developer,notes,hidden,added_at,updated_at)"
                  " VALUES(?,?,?,?,'',0,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                  (wid, str(uuid.uuid4()), title, "Dev"))


def _copy(lib, wid, path, kind="folder", *, availability="ok",
          checked_at="2000-01-01T00:00:00Z"):
    with lib.transaction() as c:
        cur = c.execute("INSERT INTO copies(work_id,source_id,kind,path,path_key,exe_relpath,"
                        "size_bytes,availability,checked_at,origin,added_at) "
                        "VALUES(?,NULL,?,?,?,?,10,?,?, 'scan','2020-01-01T00:00:00Z')",
                        (wid, kind, str(path), _key(path),
                         "game.exe" if kind == "folder" else None, availability, checked_at))
        return cur.lastrowid


def _row(lib, copy_id):
    with lib.connection() as c:
        return dict(c.execute("SELECT availability,checked_at FROM copies WHERE id=?", (copy_id,)).fetchone())


def _make_ui_lib(tmp_path):
    """Three works covering folder, archive, all-missing and partial states."""
    root = tmp_path / "games"
    root.mkdir()
    alpha = root / "Alpha"
    bravo1 = root / "Bravo1"
    bravo2 = root / "Bravo2"
    for folder in (alpha, bravo1, bravo2):
        folder.mkdir()
        (folder / "game.exe").write_bytes(b"MZ")
    alpha_zip = root / "Alpha.7z"
    alpha_zip.write_bytes(b"archive")
    charlie_zip = root / "Charlie.7z"
    charlie_zip.write_bytes(b"archive")

    cover = tmp_path / "cover.png"
    image = QImage(60, 90, QImage.Format.Format_RGB32)
    image.fill(QColor("#C03060"))
    assert image.save(str(cover))

    path = tmp_path / "gone.galdb"
    ids = {}
    with store.create_library(str(path)) as lib:
        _work(lib, 1, "Alpha")
        _work(lib, 2, "Bravo")
        _work(lib, 3, "Charlie")
        ids["alpha_folder"] = _copy(lib, 1, alpha)
        ids["alpha_archive"] = _copy(lib, 1, alpha_zip, "archive")
        ids["bravo1"] = _copy(lib, 2, bravo1)
        ids["bravo2"] = _copy(lib, 2, bravo2)
        ids["charlie"] = _copy(lib, 3, charlie_zip, "archive")
        repo.set_cover(lib, 1, repo.get_work(lib, 1).revision, str(cover))
    return path, root, ids


def _open(window, application, path):
    window.open_library_path(str(path))
    wait(application, window)
    window.show_library()
    wait(application, window)
    window.resize(1440, 860)
    application.processEvents()
    return window.library_page


def _summary(page, work_id):
    return next(w for w in page.model.works if w.id == work_id)


# ------------------------------------------------------------------ core
def test_core_marks_folder_missing_and_restores_it(lib, tmp_path):
    folder = tmp_path / "Alpha"
    folder.mkdir()
    (folder / "game.exe").write_bytes(b"MZ")
    _work(lib, 1, "Alpha")
    copy_id = _copy(lib, 1, folder)

    shutil.rmtree(folder)
    states = s.check_work_copies(lib, 1)
    assert states == {copy_id: "missing"}
    row = _row(lib, copy_id)
    assert row["availability"] == "missing"
    assert row["checked_at"] != "2000-01-01T00:00:00Z"

    folder.mkdir()
    states = s.check_work_copies(lib, 1)
    assert states == {copy_id: "ok"}
    assert _row(lib, copy_id)["availability"] == "ok"


def test_core_unchanged_state_does_not_write_checked_at(lib, tmp_path):
    folder = tmp_path / "Alpha"
    folder.mkdir()
    (folder / "game.exe").write_bytes(b"MZ")
    _work(lib, 1, "Alpha")
    copy_id = _copy(lib, 1, folder)
    before = _row(lib, copy_id)

    assert s.check_work_copies(lib, 1) == {copy_id: "ok"}
    assert _row(lib, copy_id) == before


def test_core_only_updates_the_checked_work(lib, tmp_path):
    first = tmp_path / "First"
    second = tmp_path / "Second"
    for folder in (first, second):
        folder.mkdir()
        (folder / "game.exe").write_bytes(b"MZ")
    _work(lib, 1, "First")
    _work(lib, 2, "Second")
    first_id = _copy(lib, 1, first)
    second_id = _copy(lib, 2, second)
    shutil.rmtree(second)

    assert s.check_work_copies(lib, 1) == {first_id: "ok"}
    assert _row(lib, second_id)["availability"] == "ok"
    assert _row(lib, second_id)["checked_at"] == "2000-01-01T00:00:00Z"


def test_core_uses_isdir_for_folders_and_isfile_for_archives(lib, tmp_path):
    folder = tmp_path / "Alpha"
    archive = tmp_path / "Beta.7z"
    folder.mkdir()
    (folder / "game.exe").write_bytes(b"MZ")
    archive.write_bytes(b"archive")
    _work(lib, 1, "Mixed")
    folder_id = _copy(lib, 1, folder, "folder")
    archive_id = _copy(lib, 1, archive, "archive")

    # A file where the folder used to be is not a usable folder copy; a
    # directory with the archive's name is not a usable archive.
    shutil.rmtree(folder)
    folder.write_bytes(b"not a directory")
    archive.unlink()
    archive.mkdir()
    assert s.check_work_copies(lib, 1) == {folder_id: "missing", archive_id: "missing"}


def test_core_without_copies_returns_empty_and_readonly_does_not_write(lib, tmp_path):
    _work(lib, 1, "Empty")
    assert s.check_work_copies(lib, 1) == {}

    folder = tmp_path / "Alpha"
    folder.mkdir()
    (folder / "game.exe").write_bytes(b"MZ")
    _work(lib, 2, "Readonly")
    copy_id = _copy(lib, 2, folder)
    readonly = store.open_library(lib.path, readonly=True)
    try:
        shutil.rmtree(folder)
        assert s.check_work_copies(readonly, 2) == {copy_id: "missing"}
        assert _row(lib, copy_id)["availability"] == "ok"
    finally:
        readonly.close()


# ------------------------------------------------------------------ cards
def test_file_tag_puts_deleted_before_unpacked_and_partial_labels(window, app):
    missing_archive = WorkSummary(1, "A", "", False, "", False, 1, ("archive",), "missing")
    partial_archive = WorkSummary(2, "B", "", False, "", False, 2, ("folder", "archive"), "partial")
    healthy_archive = WorkSummary(3, "C", "", False, "", False, 1, ("archive",), "ok")
    hidden = WorkSummary(4, "D", "", True, "", False, 1, ("folder",), "ok")
    assert L.file_tag(missing_archive) == "已失效"
    assert L.file_tag(partial_archive) == "部分失效"
    assert L.file_tag(healthy_archive) == "未解压"
    assert L.file_tag(hidden) == "已隐藏"
    assert L.poster_is_gray(missing_archive) is True
    assert L.poster_is_gray(partial_archive) is False


def test_detail_open_all_missing_grays_card_and_each_copy_row(window, app, tmp_path):
    path, root, _ids = _make_ui_lib(tmp_path)
    page = _open(window, app, path)
    order = [w.id for w in page.model.works]
    shutil.rmtree(root / "Alpha")
    (root / "Alpha.7z").unlink()

    page.open_detail(1)
    wait(app, window)
    w = _summary(page, 1)
    assert w.availability == "missing"
    assert L.file_tag(w) == "已失效"
    assert L.poster_is_gray(w) is True
    assert [x.id for x in page.model.works] == order
    assert page.detail.aggregate == "missing"

    label = page.detail.findChild(type(page.detail.copy_folders), "detailCopyFolders")
    assert label.textFormat() == Qt.TextFormat.RichText
    assert label.text().count("文件已不在（程序外删除或移走）") == 2
    assert label.text().count("<span") == 2

    pix = page.cache.get(1, L.poster_is_gray(w))
    color = pix.toImage().pixelColor(5, 5)
    assert color.red() == color.green() == color.blue()


def test_detail_open_partial_missing_keeps_color(window, app, tmp_path):
    path, root, _ids = _make_ui_lib(tmp_path)
    page = _open(window, app, path)
    shutil.rmtree(root / "Bravo2")

    page.open_detail(2)
    wait(app, window)
    w = _summary(page, 2)
    assert w.availability == "partial"
    assert L.file_tag(w) == "部分失效"
    assert L.poster_is_gray(w) is False
    assert page.detail.aggregate == "partial"
    text = page.detail.copy_folders.text()
    assert text.count("文件已不在（程序外删除或移走）") == 1
    assert "<span" in text


def test_detail_open_deleted_archive_still_says_gone_not_unpacked(window, app, tmp_path):
    path, root, _ids = _make_ui_lib(tmp_path)
    page = _open(window, app, path)
    (root / "Charlie.7z").unlink()

    page.open_detail(3)
    wait(app, window)
    w = _summary(page, 3)
    assert w.availability == "missing"
    assert L.file_tag(w) == "已失效"
    assert "未解压" not in L.file_tag(w)


# ------------------------------------------------------------------ scheduling
def test_same_work_is_not_checked_twice_within_60_seconds(window, app, tmp_path, monkeypatch):
    path, _root, _ids = _make_ui_lib(tmp_path)
    page = _open(window, app, path)
    calls = []
    monkeypatch.setattr(page.owner, "check_work_files",
                        lambda wid, callback=None: calls.append(wid) or True)

    page.open_detail(1)
    assert calls == [1]
    page.close_detail()
    page.open_detail(1)
    assert calls == [1]

    page._file_check_at[1] -= L.WORK_CHECK_INTERVAL + 1
    page.close_detail()
    page.open_detail(1)
    assert calls == [1, 1]
    page.close_detail()
    page.open_detail(2)
    assert calls == [1, 1, 2]


def test_startup_does_not_check_any_work(window, app, tmp_path, monkeypatch):
    path, _root, _ids = _make_ui_lib(tmp_path)
    calls = []
    monkeypatch.setattr(s, "check_work_copies",
                        lambda lib, wid: calls.append(wid) or {})
    page = _open(window, app, path)
    wait(app, window)
    assert calls == []
    assert page._file_check_at == {}

    # Entering a detail page is what starts the check.
    page.open_detail(1)
    wait(app, window)
    assert calls == [1]


def test_detail_check_skips_while_another_job_runs(window, app, tmp_path, monkeypatch):
    path, _root, _ids = _make_ui_lib(tmp_path)
    page = _open(window, app, path)
    calls = []
    monkeypatch.setattr(s, "check_work_copies",
                        lambda lib, wid: calls.append(wid) or {})
    window.active_job = object()
    try:
        page.open_detail(1)
    finally:
        window.active_job = None
    wait(app, window)
    assert calls == []


def test_detail_check_is_quiet_and_does_not_take_busy(window, app, tmp_path, monkeypatch):
    path, _root, _ids = _make_ui_lib(tmp_path)
    page = _open(window, app, path)
    started = threading.Event()
    release = threading.Event()

    def slow(lib, wid):
        started.set()
        assert release.wait(10)
        return {}

    monkeypatch.setattr(s, "check_work_copies", slow)
    page.open_detail(1)
    assert started.wait(5)
    assert window.busy is False
    assert window.active_job is None
    release.set()
    wait(app, window)
