"""Build an offline, fictional Windows demo. Never point this at an existing library.

Core APIs own all database writes. IDs, clocks, covers and scan order are fixed;
absolute source paths necessarily depend on --out. EXEs are nonfunctional props.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import uuid
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TITLES = (
    '雾港来信与第七盏灯', '星屑の郵便庭', 'The Amber Observatory', '纸月航线',
    '硝子雲の停留所', 'Letters Beneath Copper Skies', '雨音收藏室', '白砂の時計塔',
    'A Map of Quiet Tides', '眠于风中的电车', '遠灯りの栞', 'Velvet Compass Letters',
    '折纸海岸的星期八', '花暦の小さな駅', 'The Orchard of Lost Signals', '暮色气象簿',
    '薄明の鍵束', 'Lanterns Beyond the Attic', '蓝墨水与旧渡口', '月舟の切符',
    'A Pocketful of Echo Maps', '最后一页晴空', '虹影の観測帖', 'The Unsent Aurora Ledger',
)
BRANDS = ('雾栈邮局制作室', '星屑便箋工房', 'Copper Finch Atelier', '折光航线社',
          '白砂灯台室', 'Quiet Loom Bureau', '纸鲸观测社', '薄明切符工房')
PAYLOAD_SIZE = 20 * 1024 * 1024


def paint_cover(path: Path, index: int):
    """Consistent geometric jackets; no third-party artwork or characters."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen
    from galpanel2.ui.theme import font
    palettes = (('#152b48', '#628ab0'), ('#39263f', '#c28a99'), ('#19403e', '#77a493'),
                ('#613d2b', '#d6ac73'), ('#29294e', '#9491bb'), ('#43362a', '#b6b086'))
    dark, light = palettes[index % len(palettes)]
    img = QImage(600, 900, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QLinearGradient(0, 0, 600, 900)
        g.setColorAt(0, QColor(dark)); g.setColorAt(1, QColor(light))
        p.fillRect(img.rect(), g)
        p.setPen(Qt.PenStyle.NoPen)
        layout = index % 4
        p.setBrush(QColor(255, 241, 208, 45))
        if layout == 0:  # Tidal disc and diagonal coast.
            p.drawEllipse(QRectF(190, 140, 360, 360))
            p.drawPolygon([QPointF(x, y) for x, y in
                           ((0, 640), (600, 350), (600, 900), (0, 900))])
        elif layout == 1:  # Postage window and an ascending constellation.
            p.drawRoundedRect(QRectF(80, 155, 440, 320), 190, 190)
            for n in range(5):
                p.drawEllipse(QRectF(110 + n * 80, 430 - n * 54, 16, 16))
        elif layout == 2:  # Observatory rings, with a lower title panel.
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 241, 208, 95), 3))
            for radius in (95, 150, 205):
                p.drawEllipse(QPointF(300, 310), radius, radius)
            p.drawLine(65, 310, 535, 310)
            p.drawLine(300, 95, 300, 525)
            p.fillRect(QRectF(30, 545, 540, 325), QColor(dark))
        else:  # Offset paper tiles, a left rail and a high title.
            for n in range(4):
                p.drawRect(180 + n * 60, 400 + n * 65, 200, 200)
            p.fillRect(QRectF(60, 160, 8, 560), QColor('#e8dcc3'))
        p.setPen(QPen(QColor('#e8dcc3'), 1))
        p.drawRect(30, 30, 540, 840)
        p.setFont(font('mono', 18)); p.drawText(60, 90, f'LETTERS & TIDES  /  {index + 1:02d}')
        p.setFont(font('body', 40, bold=True))
        title_rect = QRectF(100, 170, 430, 220) if layout == 3 else QRectF(60, 555, 480, 235)
        p.drawText(title_rect, Qt.TextFlag.TextWordWrap, TITLES[index])
        p.setFont(font('body', 18))
        p.drawText(QRectF(60, 805, 480, 40), Qt.TextFlag.TextWordWrap, BRANDS[index % 8])
    finally:
        p.end()
    if not img.save(str(path), 'PNG'):
        raise OSError(f'Could not save {path}')


def build(out: Path, as_of: date | None = None) -> Path:
    if os.name != 'nt':
        raise RuntimeError('Demo library requires Windows drive-absolute source paths')
    from PySide6.QtWidgets import QApplication
    from galpanel2.ui.theme import load_fonts
    from galpanel2.core import store, repository as repo, scanning, meta, binding, records, shelf, play
    as_of = as_of or date.today()
    clock = datetime.combine(as_of, time(12), timezone.utc)
    stamp = clock.isoformat().replace('+00:00', 'Z')
    out = out.resolve()
    # Refuse any populated directory, including prior failed output. No destructive reset.
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('Use a new or empty --out directory')
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    load_fonts()
    roots = [out / name for name in ('日系', '同人', '欧美')]
    for root in roots:
        root.mkdir()
    for i in range(24):
        target = roots[i % 3] / BRANDS[i % 8] / TITLES[i]
        target.parent.mkdir(parents=True, exist_ok=True)
        if i == 23:
            with ZipFile(target.with_suffix('.zip'), 'w') as archive:
                archive.writestr(ZipInfo('DEMO-ONLY.txt', (2026, 1, 12, 12, 0, 0)), 'Fictional placeholder')
                with archive.open(ZipInfo('assets.dat', (2026, 1, 12, 12, 0, 0)), 'w') as data:
                    for _ in range(20):
                        data.write(bytes(1024 * 1024))
        else:
            target.mkdir()
            (target / 'play.exe').write_bytes(b'MZ\0\0GalPanel2 fictional screenshot prop; not executable.\r\n')
            # Sparse scenery payload: real file size clears scan/tidy heuristics.
            with (target / 'assets.dat').open('wb') as data:
                data.truncate(PAYLOAD_SIZE)
    covers = out / 'jackets'
    covers.mkdir()
    path = out / 'demo.galdb'
    # Production APIs generate timestamps and UUIDs internally. Scope deterministic
    # replacements to this single-threaded offline build, without any SQL writes.
    counter = iter(range(1, 1000))
    with ExitStack() as stack:
        for module in (store, repo, scanning, meta, binding, shelf, play):
            stack.enter_context(patch.object(module, 'utc_now', return_value=stamp))
        # Metadata CAS revisions use a microsecond datetime directly.
        meta_clock = stack.enter_context(patch.object(meta, 'datetime'))
        meta_clock.now.return_value = clock
        stack.enter_context(patch.object(uuid, 'uuid4', side_effect=lambda: uuid.UUID(int=next(counter))))
        with store.create_library(str(path)) as lib:
            for root, label in zip(roots, ('东岸书架', '独立小屋', '远洋书架')):
                sid = repo.add_source(lib, str(root))
                repo.set_source_label(lib, sid, label)
                plan = scanning.plan_scan(lib, sid, min_size_bytes=0)
                if plan.errors:
                    raise RuntimeError(plan.errors)
                scanning.commit_scan(lib, plan)
            brands = [meta.create_brand(lib, name) for name in BRANDS]
            tags = [meta.create_tag(lib, name, 1) for name in ('书信', '旅途', '群像', '日常', '幻想', '悬念')]
            ids = {}
            for work in repo.list_works(lib):
                detail = repo.get_work(lib, work.id)
                i = TITLES.index(Path(detail.copies[0].path).stem)
                ids[i] = work.id
                detail = repo.edit_work(lib, work.id, detail.revision, title=TITLES[i], notes='')

                jacket = covers / f'{i + 1:02d}.png'
                paint_cover(jacket, i)
                detail = repo.set_cover(lib, work.id, detail.revision, str(jacket))
                meta.set_work_meta(lib, work.id, detail.revision,
                                   [brands[i % 8]], [tags[i % 6], tags[(i + 2) % 6]])
                shelf.set_category(lib, work.id, shelf.CATEGORIES[i % 4])
                shelf.set_status(lib, work.id, shelf.STATUSES[i % 4])
                summary = ('【虚构演示】在一座依潮汐调整时钟的港城，邮局收到了一封寄往明天的信。'
                           '沿着旧地图上的灯光，旅人记录车站、庭院与陌生人的约定。'
                           '每一次回信，都让这座安静的小城多出一条通往晴空的路。')
                binding.set_summary(lib, work.id, summary)
                if i % 3 == 0:
                    record = records.Record(
                        'vndb', f'vDEMO{i + 1:03d}', 'https://example.invalid/demo',
                        TITLES[i], None, (), (), '2025-06-12', summary,
                        None, (), 0, 'Fictional demo', True, '{"fictional":true}')
                    binding.apply_bind(lib, work.id, repo.get_work(lib, work.id).revision, record)
                if i == 0 or i % 4 in (1, 2):
                    play.add_manual_session(lib, work.id, (as_of - timedelta(days=2)).isoformat(), 40 + i * 9)
                    # Only tracked sessions populate last_played_at / 最近玩.
                    started = clock - timedelta(days=1)
                    minutes = 25 + i * 4
                    times = [started, started + timedelta(minutes=minutes)]
                    with patch.object(play, 'utc_now', side_effect=[
                            t.isoformat().replace('+00:00', 'Z') for t in times]):
                        session = play.start_session(lib, work.id, detail.copies[0].id,
                                                     (as_of - timedelta(days=1)).isoformat())
                        play.end_session(lib, session, minutes * 60, 'exited')
            missing = repo.get_work(lib, ids[22]).copies[0]
            (Path(missing.path) / 'play.exe').unlink()
            (Path(missing.path) / 'assets.dat').unlink()
            Path(missing.path).rmdir()
            scanning.check_availability(lib)
            manifest = {'detail': ids[0], 'find': ids[1], 'missing': ids[22], 'archive': ids[23],
                        'count': len(ids), 'fictional': True, 'as_of': as_of.isoformat()}
            (out / 'demo.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--as-of', type=date.fromisoformat, help='Freeze demo dates (default: today)')
    args = parser.parse_args()
    print(build(args.out, args.as_of))


if __name__ == '__main__':
    main()
