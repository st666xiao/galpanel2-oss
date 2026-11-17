"""meta-m1 fix-1: tidy merge validation, duplicate-name hints and cover cleanup; data lives in tmp_path."""
from pathlib import Path
import uuid
import pytest
from PySide6.QtGui import QImage, QColor
from galpanel2.core import store, meta as M, repository as R
from galpanel2.ui.library import Editor
from test_ui import app, window, wait
from test_meta_core import snapshot
from test_meta_ui import meta_library, opened


def merge_combo(t, key):
    return t.table.cellWidget(t.widgets[key][0], 3)


def open_tidy(window, app, fx):
    opened(window, app, fx);window.open_brand_tidy();wait(app, window)
    return window.brand_tidy


def test_r1_merge_prefix_text_disables_apply(window, app, meta_library):
    t = open_tidy(window, app, meta_library)
    t.set_row('other', 'merge', brand_id=meta_library[1])
    combo = merge_combo(t, 'other');combo.setEditText('Samp')
    assert t.choices['other'].action == 'merge' and t.choices['other'].brand_id is None
    assert not t.apply_button.isEnabled()
    note = t.table.item(t.widgets['other'][0], 4).text()
    assert note.split('；')[0] == '请从列表里选一个品牌'
    assert '还没选品牌' in t.summary.text()


def test_r1_merge_full_name_key_resolves(window, app, meta_library):
    _, a, b, *_ = meta_library
    t = open_tidy(window, app, meta_library)
    t.set_row('other', 'merge', brand_id=a)
    combo = merge_combo(t, 'other')
    combo.setEditText('Samp');assert t.choices['other'].brand_id is None
    combo.setEditText('ＳＡＭＰＬＥ');assert t.choices['other'].brand_id == a
    assert t.apply_button.isEnabled() and '还没选品牌' not in t.summary.text()
    assert '请从列表里选一个品牌' not in t.table.item(t.widgets['other'][0], 4).text()
    alias = next(x for x in M.list_brands(window.lib) if x.id == b).aliases[0]
    combo.setEditText(alias);assert t.choices['other'].brand_id == b
    combo.setEditText('');assert t.choices['other'].brand_id is None and not t.apply_button.isEnabled()
    combo.setCurrentIndex(combo.findData(a));assert t.choices['other'].brand_id == a and t.apply_button.isEnabled()


def test_r1_no_brands_merge_blocks_apply(window, app, tmp_path, monkeypatch):
    path = tmp_path / 'nobrand.galdb'
    with store.create_library(str(path)) as lib:
        with lib.transaction() as c:
            c.execute('INSERT INTO works(id,uuid,title,developer,notes,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                      (1, str(uuid.uuid4()), 'Story', 'Foo', '', 0, '2020-01-01', '2020-01-01'))
    window.open_library_path(str(path));wait(app, window)
    window.open_brand_tidy();wait(app, window);t = window.brand_tidy
    key = t.texts[0].text_key
    t.set_row(key, 'merge')
    assert t.choices[key].brand_id is None and not t.apply_button.isEnabled()
    jobs = []
    original = window.run_job
    monkeypatch.setattr(window, 'run_job', lambda *a, **k: (jobs.append(a), original(*a, **k))[1])
    before = snapshot(window.lib);t.apply();wait(app, window)
    assert not jobs and snapshot(window.lib) == before
    assert snapshot(window.lib)['brand_decisions'] == [] and '还没选品牌' in t.message.text()
    t.close()


def test_r1_valid_batch_still_applies(window, app, meta_library):
    _, a, *_ = meta_library
    t = open_tidy(window, app, meta_library)
    keys = list(t.choices)
    for k in keys:t.set_row(k, 'ignore')
    t.set_row('other', 'new', 'Other');t.set_row('sample', 'merge', brand_id=a)
    assert t.apply_button.isEnabled() and not t.invalid_count()
    t.apply();wait(app, window)
    assert M.find_brand(window.lib, 'Other') is not None and '挂上了品牌' in t.message.text()
    t.close()


def test_r2_duplicate_tag_message_names_tag(window, app, meta_library):
    opened(window, app, meta_library);tags = {x.id: x.name for x in M.list_tags(window.lib)}
    _, a, b, fan, school, pix = meta_library
    e = Editor(window, R.get_work(window.lib, 4));e.show()
    e.title.setText('Rollback');e.brand_input.add_new(tags[school]);e.tag_input.add_new(tags[fan], 1)
    before = snapshot(window.lib);e.save();wait(app, window)
    assert e.isVisible() and e.message.text() == f'「{tags[fan]}」已经有了，请从候选里选'
    assert snapshot(window.lib) == before
    e.close()


def test_r2_duplicate_brand_message_names_brand(window, app, meta_library):
    opened(window, app, meta_library);_, a, *_ = meta_library
    alias = next(x for x in M.list_brands(window.lib) if x.id == a).aliases[0]
    e = Editor(window, R.get_work(window.lib, 4));e.show()
    e.title.setText('Rollback');e.brand_input.add_new(alias);e.tag_input.add_new('Fresh tag', 1)
    before = snapshot(window.lib);e.save();wait(app, window)
    assert e.isVisible() and e.message.text() == f'「{alias}」已经有了，请从候选里选'
    assert snapshot(window.lib) == before
    e.close()


@pytest.fixture
def cover_lib(lib):
    with lib.transaction() as c:
        for i in (1, 2):
            c.execute('INSERT INTO works(id,uuid,title,developer,hidden,added_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                      (i, str(uuid.uuid4()), f'Work {i}', '', 0, '2020-01-01', '2020-01-01'))
    return lib


def png(tmp_path, name='c.png', color='red'):
    img = QImage(8, 8, QImage.Format.Format_RGB32);img.fill(QColor(color))
    path = tmp_path / name;assert img.save(str(path), 'PNG')
    return str(path)


def revision(lib, work_id):
    with lib.connection() as c:
        return c.execute('SELECT revision FROM works WHERE id=?', (work_id,)).fetchone()[0]


def covers_dir(lib):
    d = Path(lib.path + '.covers');return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_r3_cover_file_removed_when_insert_fails(cover_lib, tmp_path, monkeypatch):
    lib = cover_lib;image = png(tmp_path);rev = revision(lib, 1)
    def boom(*a, **k):raise RuntimeError('injected get_work failure')
    monkeypatch.setattr(R, 'get_work', boom)
    with pytest.raises(RuntimeError, match='injected'):R.set_cover(lib, 1, rev, image)
    monkeypatch.undo()
    assert revision(lib, 1) == rev and covers_dir(lib) == []
    with lib.connection() as c:assert c.execute('SELECT COUNT(*) FROM covers').fetchone()[0] == 0


def test_r3_shared_cover_file_kept_when_insert_fails(cover_lib, tmp_path, monkeypatch):
    lib = cover_lib;image = png(tmp_path)
    R.set_cover(lib, 1, revision(lib, 1), image);files = covers_dir(lib);assert len(files) == 1
    rev = revision(lib, 2)
    def boom(*a, **k):raise RuntimeError('injected get_work failure')
    monkeypatch.setattr(R, 'get_work', boom)
    with pytest.raises(RuntimeError, match='injected'):R.set_cover(lib, 2, rev, image)
    monkeypatch.undo()
    assert covers_dir(lib) == files and revision(lib, 2) == rev
    assert Path(R.get_work(lib, 1).cover_path).exists()
