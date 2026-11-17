"""Capture isolated, offline demo screens using the existing application CLI."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from make_demo_library import paint_cover

ROOT = Path(__file__).resolve().parents[1]


def prepare_sites(destination: Path):
    """Copy the offline rules and generate their original geometric artwork."""
    from PySide6.QtWidgets import QApplication
    from galpanel2.ui.theme import load_fonts
    app = QApplication.instance() or QApplication([])
    load_fonts()
    shutil.copytree(ROOT / 'tools/demo_sites', destination)
    paint_cover(destination / 'vDEMO002.png', 1)


def capture(demo: Path, output: Path):
    demo = demo.resolve()
    manifest = json.loads((demo / 'demo.json').read_text(encoding='utf-8'))
    if manifest.get('fictional') is not True:
        raise ValueError('Expected a generated fictional demo')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    screens = [('library', 'library', []),
               ('detail', 'detail', ['--select', str(manifest['detail'])]),
               ('filter', 'library', ['--popover', 'brand']),
               ('find', 'find', ['--select', str(manifest['find'])]),
               ('tidy', 'tidy', ['--tidy-section', 'missing', '--tidy-expand']),
               ('title', 'title', []),
               ('library-day', 'library', []), ('library-night', 'library', [])]
    with tempfile.TemporaryDirectory(prefix='gp2-shots-') as tmp:
        scratch = Path(tmp)
        # FileTransport writes its request log beside rules, never into source files.
        prepare_sites(scratch / 'sites')
        # The wrapper permits writable UI states only for the validated demo;
        # database transactions and game launches remain blocked.
        env = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=str(ROOT),
                   GALPANEL2_SETTINGS_DIR=str(scratch / 'settings'),
                   GALPANEL2_DEFAULT_LIBRARY=str(Path(demo.anchor) / 'GalPanel2/library.galdb'),
                   GALPANEL2_FAKE_SITES=str(scratch / 'sites/rules.json'))
        env.pop('GALPANEL2_BANGUMI_TOKEN', None)
        for name, screen, extra in screens:
            target = output / f'{name}.png'
            target.unlink(missing_ok=True)  # A stale image must never mask a failed capture.
            theme = name.removeprefix('library-') if name.startswith('library-') else 'media'
            command = [sys.executable, str(ROOT / 'tools/capture_demo_screen.py'), '--demo', str(demo),
                       '--screen', screen, '--theme', theme,
                       '--screenshot', str(target)]
            if screen != 'title':
                command += ['--library', str(demo / 'demo.galdb')]
            subprocess.run(command + extra, cwd=ROOT, env=env, check=True, timeout=140)
            if not target.is_file() or target.stat().st_size < 1024:
                raise RuntimeError(f'Missing or empty screenshot: {target}')
            print(target.name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', type=Path, required=True, help='Directory from make_demo_library.py')
    parser.add_argument('--out', type=Path, default=ROOT / 'docs/screenshots')
    args = parser.parse_args()
    capture(args.demo, args.out)


if __name__ == '__main__':
    main()
