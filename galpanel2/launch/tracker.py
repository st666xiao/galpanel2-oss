"""One monotonic timer; checkpoint failure never resets accumulated time."""
import os
import threading
import time

from galpanel2.core import play
from galpanel2.core.launchopts import LaunchSpec
from . import process

TICK_SECONDS = 1.0
MAX_TICK_GAP = 5.0
CHECKPOINT_SECONDS = 30


def accumulate(total: float, delta: float) -> float:
    return total + min(max(delta, 0.0), MAX_TICK_GAP)


def _checkpoint_interval():
    try:
        value = int(os.environ.get("GALPANEL2_TEST_CHECKPOINT_SECONDS", ""))
        if 1 <= value <= 60:
            return value
    except ValueError:
        pass
    return CHECKPOINT_SECONDS


def _observe(callback, value):
    if callback is not None:
        try:
            callback(value)
        except Exception:
            # An observer must not interrupt the durable timer's lifecycle.
            pass


class Tracker:
    def __init__(self, lib, spec: LaunchSpec, *, on_tick=None, on_end=None):
        self.lib, self.spec = lib, spec
        self.work_id, self.copy_id = spec.work_id, spec.copy_id
        self.session_id = None
        self.process = None
        self.on_tick, self.on_end = on_tick, on_end
        self._total = 0.0
        self._stop = threading.Event()
        self._thread = None

    @property
    def seconds(self) -> int:
        return int(self._total)

    def start(self) -> None:
        if self.session_id is not None:
            raise ValueError("Tracker was already started")
        self.session_id = play.start_session(self.lib, self.work_id, self.copy_id)
        try:
            self.process = process.launch(self.spec)
        except BaseException:
            play.discard_session(self.lib, self.session_id)
            self.session_id = None
            raise
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="GalPanel2 timer", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self.process.release()
            play.end_session(self.lib, self.session_id, 0, "panel_closed")
            raise

    def _run(self):
        previous = last_checkpoint = self._started
        interval = _checkpoint_interval()
        outcome = None
        try:
            while True:
                self._stop.wait(TICK_SECONDS)
                now = time.monotonic()
                self._total = accumulate(self._total, now - previous)
                previous = now
                _observe(self.on_tick, self.seconds)
                if self._stop.is_set():
                    outcome = play.end_session(self.lib, self.session_id, self.seconds, "panel_closed")
                    break
                if not self.process.alive():
                    outcome = play.end_session(self.lib, self.session_id, self.seconds, "exited")
                    break
                if now - last_checkpoint >= interval:
                    try:
                        play.checkpoint_session(self.lib, self.session_id, self.seconds)
                    except Exception:
                        pass
                    last_checkpoint = now
        except Exception as e:
            outcome = e
        finally:
            self.process.release()
            _observe(self.on_end, outcome)

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
