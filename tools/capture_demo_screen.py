"""Render writable UI states for a generated demo, with writes/launches blocked.

The ordinary application screenshot CLI remains read-only. This wrapper is only
used by the fictional demo pipeline; no interactions are performed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def capture(demo: Path, arguments: list[str]) -> int:
    from galpanel2 import __main__ as gui
    from galpanel2.core import repository, store
    from galpanel2.ui.window import Window
    from make_demo_library import TITLES

    demo = demo.resolve()
    db = demo / 'demo.galdb'
    manifest = json.loads((demo / 'demo.json').read_text(encoding='utf-8'))
    if manifest.get('fictional') is not True or manifest.get('count') != len(TITLES):
        raise ValueError('Expected a generated fictional demo')
    # Refuse a real library, including a copied manifest beside another database.
    with store.open_library(str(db), readonly=True) as lib:
        works = repository.list_works(lib)
        if len(works) != len(TITLES) or {w.title for w in works} != set(TITLES):
            raise ValueError('Unexpected demo contents')
        for source in repository.list_sources(lib):
            if not Path(source.path).resolve().is_relative_to(demo):
                raise ValueError('Demo sources must be inside the demo directory')
    digest = hashlib.sha256(db.read_bytes()).digest()
    original_open = store.open_library
    original_grab = Window.grab
    screen = arguments[arguments.index('--screen') + 1] if '--screen' in arguments else 'title'

    def demo_open(path, readonly=False, *, recover=True):
        if Path(path).resolve() != db:
            raise ValueError('Only the generated demo may be opened')
        return original_open(path, readonly=False, recover=False)

    def blocked(*args, **kwargs):
        raise RuntimeError('Demo capture cannot write data or launch games')

    def checked_grab(window, *args, **kwargs):
        from galpanel2.core import play
        from galpanel2.ui.library import _played_recently
        if screen != 'title':
            if not window.lib or window.lib.readonly:
                raise RuntimeError('Demo did not open in its normal UI state')
            if not any(_played_recently(p) for p in play.list_playtimes(window.lib).values()):
                raise RuntimeError('Demo has no recent play history; regenerate it for today')
        if screen == 'detail':
            detail = window.library_page.detail
            if not detail.start_button.isEnabled() or not play.playtime(window.lib, detail.work.id).last_played_at:
                raise RuntimeError('Detail must show an enabled start button and play history')
        if screen == 'tidy':
            if window.tidy_page.banner.isVisible() or window.tidy_page.snap['junk']:
                raise RuntimeError('Tidy must have no read-only banner or suspect demo props')
        if screen == 'find':
            dialog = window.find_dialog
            record = dialog.current_record
            if not record or not dialog.images.get(record.cover_url) or not dialog.bind_button.isEnabled():
                raise RuntimeError('Find preview must have its cover and an enabled bind button')
        return original_grab(window, *args, **kwargs)

    try:
        with patch.object(store, 'open_library', demo_open), \
                patch.object(store.Library, 'transaction', blocked), \
                patch.object(Window, 'start_game', blocked), \
                patch.object(Window, 'grab', checked_grab):
            return gui.main(arguments)
    finally:
        if hashlib.sha256(db.read_bytes()).digest() != digest:
            raise RuntimeError('Demo database changed during capture')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', type=Path, required=True)
    args, rest = parser.parse_known_args()
    if '--screenshot' not in rest:
        parser.error('Only --screenshot mode is supported')
    raise SystemExit(capture(args.demo, rest))
