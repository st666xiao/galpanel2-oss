"""Native application and side-effect-free screenshot CLI."""
import argparse
import os
from pathlib import Path
import sys
import re

SCREENSHOT_TIMEOUT_MS = 110000


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", choices=("title", "library", "detail", "scan", "transfer", "record", "brandtidy", "find", "tagmap", "review", "tidy"), default="title")
    ap.add_argument("--tidy-section", choices=("missing", "junk", "dup", "title", "ignored"), default="missing")
    ap.add_argument("--tidy-expand", action="store_true")
    ap.add_argument("--tidy-confirm", action="store_true")
    ap.add_argument("--theme", choices=("day", "night", "media"))
    ap.add_argument("--screenshot")
    ap.add_argument("--library")
    ap.add_argument("--select", type=int)
    ap.add_argument("--scan-source", type=int)
    ap.add_argument("--legacy")
    ap.add_argument("--meta-filter")
    ap.add_argument("--popover",choices=('brand','tag'))
    args = ap.parse_args(argv)
    if args.screen in ("library", "detail", "scan", "record", "brandtidy", "find", "tagmap", "review", "tidy") and not args.library:
        ap.error("--library is required for this screen")
    if args.screen == "detail" and args.select is None:
        ap.error("--select is required for detail")
    if args.screen == "find":
        if args.select is None:
            ap.error("--select is required for find")
        if not os.environ.get("GALPANEL2_FAKE_SITES"):
            ap.error("--screen find requires GALPANEL2_FAKE_SITES")
    if args.screen == "scan" and args.scan_source is None:
        ap.error("--scan-source is required for scan")
    if args.screen == "transfer" and not args.legacy:
        ap.error("--legacy is required for transfer")
    states=[]
    if (args.meta_filter is not None or args.popover) and args.screen!='library':
        ap.error('--meta-filter and --popover require --screen library')
    if args.meta_filter is not None:
        for part in args.meta_filter.split(','):
            match=re.fullmatch(r'([bt])([+-])(\d+)',part)
            if not match:ap.error('Invalid --meta-filter specification')
            kind,state,item_id=match.groups()
            states.append(('brand' if kind=='b' else 'tag',int(item_id),'in' if state=='+' else 'out'))
    if args.screenshot:
        os.environ["QT_SCALE_FACTOR"] = "1"
        os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
        os.environ["QT_FONT_DPI"] = "96"
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .ui.theme import load_fonts, font
    from .ui.window import Window
    app = QApplication(sys.argv[:1])
    load_fonts()
    app.setFont(font())
    window = Window(args.theme, screenshot_mode=bool(args.screenshot))
    window.show()
    if not args.screenshot and args.screen == "title" and not args.library:
        window.auto_open()
    if args.screen in ("library", "detail", "scan", "record", "brandtidy", "find", "tagmap", "review", "tidy"):
        def library_ready():
            page=window.library_page
            for kind,item_id,state in states:page.set_meta_state(kind,item_id,state)
            if args.select is not None:page.select_work(args.select)
            if args.screen == "detail":page.open_detail(args.select)
            if args.popover:(page.brand_popover if args.popover=='brand' else page.tag_popover).show_for_button()
        def open_tidy():
            window.tidy_page.prepare(args.tidy_section, args.tidy_expand, args.tidy_confirm)
            window.show_tidy()
        after = ((lambda: window.start_scan(args.scan_source)) if args.screen == "scan" else
                 window.show_record if args.screen == "record" else window.open_brand_tidy if args.screen=='brandtidy' else
                 (lambda: window.open_find(args.select)) if args.screen=='find' else
                 window.open_tag_mapping if args.screen=='tagmap' else
                 window.show_review if args.screen=='review' else
                 open_tidy if args.screen=='tidy' else library_ready)
        window.open_library_path(args.library, args.select, after_load=after)
    elif args.screen == "transfer":
        window.show_transfer()
        window.transfer_page.inspect(args.legacy)
    result = [0]
    if args.screenshot:
        window.resize(1440, 860)
        timer = QTimer()
        timer.setInterval(50)
        find_selected = [False]
        def poll():
            if window.fatal_error:
                print(window.fatal_error, file=sys.stderr)
                result[0] = 2
                timer.stop()
                window.close()
                app.quit()
            elif not window.pending_jobs:
                if args.screen == 'find':
                    dialog = window.find_dialog
                    if dialog is None:
                        return
                    if not find_selected[0] and dialog.candidates():
                        find_selected[0] = True
                        dialog.select(0)
                        return
                    if find_selected[0] and dialog.current_plan is None:
                        print(dialog.message.text() or 'Find preview failed', file=sys.stderr)
                        result[0] = 2
                        timer.stop(); window.close(); app.quit(); return
                timer.stop()
                QTimer.singleShot(100, grab)
        def grab():
            try:
                window.resize(1440, 860)
                app.processEvents()
                output = Path(args.screenshot).absolute()
                output.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(output), "PNG"):
                    raise OSError("Could not save screenshot")
            except Exception as e:
                result[0] = 2
                print(f"{type(e).__name__}: {e}", file=sys.stderr)
            finally:
                window.close()
                app.quit()
        timer.timeout.connect(poll)
        timer.start()
        def timeout():
            if window.pending_jobs:
                result[0] = 2
                print("Screenshot loading timed out", file=sys.stderr)
                window.cancel_job()
                window.close()
                # Qt drops quit() when this close is ignored, so hide and exit anyway.
                if window.isVisible():
                    window.hide()
                app.exit(result[0])
        QTimer.singleShot(SCREENSHOT_TIMEOUT_MS, timeout)
    app.exec()
    return result[0]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(2)
