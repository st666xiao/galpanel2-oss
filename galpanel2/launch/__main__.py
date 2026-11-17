"""Line-delimited, flushed tracking events."""
import argparse
import json
import os
import threading
import sys

from galpanel2.core import store, launchopts
from .tracker import Tracker


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("play")
    p.add_argument("library")
    p.add_argument("work_id", type=int)
    p.add_argument("--copy", type=int)
    p.add_argument("--stop-file")
    args = parser.parse_args(argv)
    lib = None
    try:
        lib = store.open_library(args.library)
        cid = args.copy if args.copy is not None else launchopts.default_copy(lib, args.work_id)
        spec = launchopts.resolve_launch(lib, cid)
        if spec.work_id != args.work_id:
            raise ValueError("Copy does not belong to this work")
        done, outcome = threading.Event(), []
        def ended(value):
            outcome.append(value)
            done.set()
        def tick(seconds):
            if args.stop_file and os.path.exists(args.stop_file):
                tracker.stop()
        tracker = Tracker(lib, spec, on_tick=tick, on_end=ended)
        tracker.start()
        emit(dict(event="started", session_id=tracker.session_id, work_id=spec.work_id,
                  copy_id=cid, pid=tracker.process.pid, exe=spec.exe_path))
        try:
            done.wait()
        except KeyboardInterrupt:
            tracker.stop()
        tracker.join()
        if not outcome:
            raise RuntimeError("Tracking ended without a result")
        result = outcome[0]
        if isinstance(result, Exception):
            raise result
        emit(dict(event="ended", session_id=result.id, seconds=result.seconds, end_reason=result.end_reason))
        return 0
    except Exception as e:
        emit(dict(event="error", error=type(e).__name__, message=str(e), reason=getattr(e, "reason", None)))
        return 2
    finally:
        if lib is not None:
            lib.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
