"""Demo integrity on target Windows, plus portable cover/replay checks."""
import importlib.util
import os
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from galpanel2.core.domain import SiteError
from galpanel2.core import binding, meta, play, records, repository, shelf, sites, store

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('make_demo_library', ROOT / 'tools/make_demo_library.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_demo_cover_and_offline_candidates(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from galpanel2.ui.theme import load_fonts
    load_fonts()
    a, b = tmp_path / 'a.png', tmp_path / 'b.png'
    demo.paint_cover(a, 1)
    demo.paint_cover(b, 1)
    assert a.read_bytes() == b.read_bytes()
    from PySide6.QtGui import QImage
    image = QImage(str(a))
    assert (image.width(), image.height()) == (600, 900)
    # Log beside a temporary copy, never beside the source fixture.
    monkeypatch.syspath_prepend(str(ROOT / 'tools'))
    from take_screenshots import prepare_sites
    prepare_sites(tmp_path / 'sites')
    rules = tmp_path / 'sites/rules.json'
    client = sites.SiteClient(sites.FileTransport(str(rules)))
    candidates = client.search('vndb', demo.TITLES[1])
    assert candidates[0].title == demo.TITLES[1]
    assert candidates[0].site_id == 'vDEMO002'
    assert candidates[0].cover_url
    raw = client.image(candidates[0].cover_url)
    assert raw == a.read_bytes()
    assert not QImage.fromData(raw).isNull()
    record = client.fetch(records.Ref(candidates[0].site, candidates[0].site_id))
    assert record.cover_url == candidates[0].cover_url
    with pytest.raises(SiteError, match='没有假数据'):
        client.transport.request('GET', 'https://example.invalid/unmatched', {}, None, 1)


def test_demo_capture_is_scoped_and_blocks_actions(tmp_path, monkeypatch):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(ROOT / 'tools'))
    import capture_demo_screen as capture
    from galpanel2 import __main__ as gui
    from galpanel2.ui.window import Window

    db = tmp_path / 'demo.galdb'
    db.write_bytes(b'owned demo test')
    (tmp_path / 'demo.json').write_text(json.dumps({'fictional': True, 'count': 24}))
    calls = []

    @contextmanager
    def fake_open(path, readonly=False, *, recover=True):
        calls.append((path, readonly, recover))
        yield object()

    monkeypatch.setattr(store, 'open_library', fake_open)
    monkeypatch.setattr(repository, 'list_works', lambda lib: [SimpleNamespace(title=t) for t in demo.TITLES])
    monkeypatch.setattr(repository, 'list_sources', lambda lib: [SimpleNamespace(path=str(tmp_path / '日系'))])

    def fake_gui(arguments):
        with store.open_library(str(db), readonly=True):
            pass
        assert calls[-1] == (str(db), False, False)
        with pytest.raises(ValueError, match='Only the generated demo'):
            store.open_library(str(tmp_path / 'real.galdb'))
        with pytest.raises(RuntimeError, match='cannot write'):
            store.Library.transaction(None)
        with pytest.raises(RuntimeError, match='cannot write'):
            Window.start_game(None, 1)
        return 0

    monkeypatch.setattr(gui, 'main', fake_gui)
    assert capture.capture(tmp_path, ['--screenshot', 'test.png']) == 0
    assert store.open_library is fake_open
    assert db.read_bytes() == b'owned demo test'
    monkeypatch.setattr(repository, 'list_sources', lambda lib: [SimpleNamespace(path=str(tmp_path.parent))])
    with pytest.raises(ValueError, match='inside the demo'):
        capture.capture(tmp_path, [])


def test_capture_pipeline_paths_and_screens(tmp_path, monkeypatch):
    import json
    monkeypatch.syspath_prepend(str(ROOT / 'tools'))
    import take_screenshots as capture
    (tmp_path / 'demo.json').write_text(json.dumps({'fictional': True, 'detail': 1, 'find': 2}))
    calls = []

    def fake_run(command, *, cwd, env, check, timeout):
        assert check
        assert command[1].endswith('capture_demo_screen.py')
        assert env['GALPANEL2_DEFAULT_LIBRARY'] == str(Path(tmp_path.anchor) / 'GalPanel2/library.galdb')
        assert 'gp2-shots-' not in env['GALPANEL2_DEFAULT_LIBRARY']
        rules = Path(env['GALPANEL2_FAKE_SITES'])
        assert (rules.parent / 'vDEMO002.png').is_file()
        target = Path(command[command.index('--screenshot') + 1])
        target.write_bytes(b'capture test' * 200)
        calls.append(target.name)

    monkeypatch.setattr(capture.subprocess, 'run', fake_run)
    capture.capture(tmp_path, tmp_path / 'shots')
    assert calls == ['library.png', 'detail.png', 'filter.png', 'find.png', 'tidy.png',
                     'title.png', 'library-day.png', 'library-night.png']


@pytest.mark.skipif(os.name != 'nt', reason='Core source identities require absolute Windows paths')
def test_demo_library_integrity_and_determinism(tmp_path):
    out = tmp_path / 'demo'
    as_of = date.today()
    db = demo.build(out, as_of)
    first = db.read_bytes()
    with store.open_library(str(db), readonly=True) as lib:
        works = repository.list_works(lib)
        assert len(works) == 24
        assert {w.title for w in works} == set(demo.TITLES)
        assert len(repository.list_sources(lib)) == 3
        assert len(meta.list_brands(lib)) == 8
        assert all(w.has_cover for w in works)
        assert sum(w.availability == 'missing' for w in works) == 1
        assert sum(w.kinds == ('archive',) for w in works) == 1
        assert sum(w.availability == 'ok' and w.kinds == ('folder',) for w in works) == 22
        assert set(x.category for x in shelf.list_shelf(lib).values()) == set(shelf.CATEGORIES)
        assert set(x.status for x in shelf.list_shelf(lib).values()) == set(shelf.STATUSES)
        assert len(binding.list_links(lib)) == 8
        assert len(play.list_sessions(lib)) >= 8
        from galpanel2.core import launchopts, tidy
        from galpanel2.ui.library import _played_recently
        times = play.list_playtimes(lib)
        assert sum(_played_recently(p, today=as_of) for p in times.values()) == 13
        detail_id = next(w.id for w in works if w.title == demo.TITLES[0])
        assert times[detail_id].total_seconds == 65 * 60
        assert times[detail_id].last_played_at
        assert not tidy.suspect_report(lib)
        for work in works:
            detail = repository.get_work(lib, work.id)
            assert not detail.notes
            copy_path = Path(detail.copies[0].path)
            i = demo.TITLES.index(work.title)
            assert copy_path.parent.name == demo.BRANDS[i % 8]
            assert copy_path.stem == work.title
            assert Path(detail.cover_path).is_file()
            if work.availability == 'ok' and work.kinds == ('folder',):
                assert (Path(detail.copies[0].path) / 'play.exe').is_file()
                spec = launchopts.resolve_launch(lib, detail.copies[0].id)
                assert Path(spec.exe_path).stat().st_size > 0
    with pytest.raises(FileExistsError):
        demo.build(out)
    assert db.read_bytes() == first
    # Same absolute --out, recreated only inside this test's owned temporary tree.
    import shutil
    shutil.rmtree(out)
    demo.build(out, as_of)
    assert db.read_bytes() == first
