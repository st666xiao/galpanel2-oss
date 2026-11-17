"""R1: deterministic launch/close races, fake timers and temporary libraries only."""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from galpanel2.core import store
from galpanel2.core.domain import LaunchError, NotLaunchable
from galpanel2.core.launchopts import LaunchSpec
from galpanel2.ui import window as window_module
from test_ui import app, window, library, wait


def until(app, predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.001)
    assert predicate(), "Timed out waiting for the controlled job"


def query(lib):
    with lib.connection() as db:
        return db.execute("SELECT COUNT(*) FROM works").fetchone()[0]


@pytest.fixture
def blocked_launch(window, app, library, monkeypatch):
    cases = []

    def start(*, error=None, join_timeout=False):
        window.open_library_path(str(library[0]))
        wait(app, window)
        work = next(w for w in window.library_page.details.values()
                    if not w.hidden and any(cp.kind == "folder" for cp in w.copies))
        cp = next(cp for cp in work.copies if cp.kind == "folder")
        window.library_page.select_work(work.id)
        case = SimpleNamespace(entered=threading.Event(), release=threading.Event(),
                               tracker=None, error=error, join_timeout=join_timeout,
                               calls=[], confirms=[], snapshots=[], lib=window.lib,
                               title=work.title, work_id=work.id)
        cases.append(case)

        class FakeTracker:
            def __init__(self, lib, spec, *, on_tick=None, on_end=None):
                self.lib, self.spec = lib, spec
                self.work_id, self.copy_id = spec.work_id, spec.copy_id
                self.on_tick, self.on_end = on_tick, on_end
                self.seconds = 7
                self.stop_calls = 0
                self.alive = True
                self.ended = False
                self._thread = SimpleNamespace(is_alive=lambda: self.alive)
                case.tracker = self

            def start(self):
                case.entered.set()
                if not case.release.wait(10):
                    raise AssertionError("Launch gate was never released")
                if case.error is not None:
                    self.alive = False
                    raise case.error

            def stop(self):
                self.stop_calls += 1
                case.calls.append("stop")

            def join(self, timeout=None):
                case.calls.append(("join", timeout))
                if not case.join_timeout:
                    self.finish()

            def finish(self, result=None, *, thread_done=True):
                self.alive = not thread_done
                if not self.ended:
                    self.ended = True
                    self.on_end(result if result is not None else SimpleNamespace(seconds=self.seconds))

        spec = LaunchSpec(work.id, cp.id, str(library[1] / "unused.exe"), "", cp.path)
        monkeypatch.setattr(window_module, "Tracker", FakeTracker)
        monkeypatch.setattr(window_module.launchopts, "default_copy", lambda lib, wid: cp.id)
        monkeypatch.setattr(window_module.launchopts, "resolve_launch", lambda lib, cid: spec)
        close = case.lib.close

        def close_library():
            case.calls.append(("library-close", case.tracker._thread.is_alive()))
            close()

        monkeypatch.setattr(case.lib, "close", close_library)
        window.show()
        window.start_game(work.id)
        until(app, case.entered.is_set)
        assert window.active_job.label == "启动游戏"
        assert not window.active_job.cancellable
        assert window.tracker is None
        return case

    yield start
    # Always release the QThread, including when testing the unfixed baseline.
    for case in cases:
        case.release.set()
        if case.tracker is not None:
            case.tracker.alive = False
    monkeypatch.setattr(window, "confirm_stop_tracking", lambda *args: True)
    window.closing = False
    wait(app, window)


def confirmation(window, case, monkeypatch, answer):
    def confirm(title, action):
        case.confirms.append((title, action))
        case.snapshots.append((len(window.jobs), window.tracker,
                               window._playing_title, window.status.playing.text(),
                               window.status.playing.isVisible(),
                               window.library_page.detail.start_button.text()))
        return answer
    monkeypatch.setattr(window, "confirm_stop_tracking", confirm)


def request_close(window, case, app):
    window.close()
    app.processEvents()
    assert case.confirms == []
    assert window.isVisible()
    assert window.closing is True
    assert window.tracker is None
    assert case.tracker.stop_calls == 0
    assert window.lib is case.lib


def assert_started_confirmation(case):
    assert case.confirms == [(case.title, "close")]
    jobs, tracker, title, playing, visible, button = case.snapshots[0]
    assert jobs == 0
    assert tracker is case.tracker and title == case.title
    assert case.title in playing and "0:00:07" in playing
    assert visible and button == "游戏中"


def test_r1_close_during_launch_asks_once_after_start(blocked_launch, window, app, monkeypatch):
    case = blocked_launch()
    confirmation(window, case, monkeypatch, False)
    request_close(window, case, app)
    # Repeated requests while the job is blocked must still defer confirmation.
    window.close()
    assert case.confirms == []
    case.release.set()
    wait(app, window)
    assert_started_confirmation(case)
    assert window.isVisible() and window.tracker is case.tracker
    assert case.tracker.stop_calls == 0 and case.tracker._thread.is_alive()
    assert window.closing is False and window.lib is case.lib
    assert query(window.lib) > 0


def test_r1_close_during_launch_confirm_stops_then_closes(blocked_launch, window, app, monkeypatch):
    case = blocked_launch()
    confirmation(window, case, monkeypatch, True)
    request_close(window, case, app)
    case.release.set()
    wait(app, window)
    assert_started_confirmation(case)
    assert case.tracker.stop_calls == 1
    assert case.calls == ["stop", ("join", 3), ("library-close", False)]
    assert not window.isVisible() and window.lib is None
    assert window.tracker is None and window.closing is False


def test_r1_close_during_launch_join_timeout_keeps_window(blocked_launch, window, app, monkeypatch):
    case = blocked_launch(join_timeout=True)
    confirmation(window, case, monkeypatch, True)
    request_close(window, case, app)
    case.release.set()
    wait(app, window)
    assert_started_confirmation(case)
    assert case.tracker.stop_calls == 1
    assert case.calls == ["stop", ("join", 3)]
    assert "正在保存计时，请稍后再试" in window.status.message.text()
    assert window.isVisible() and window.lib is case.lib
    assert window.tracker is case.tracker and case.tracker._thread.is_alive()
    assert query(window.lib) > 0 and window.closing is False
    # A later normal result must still refresh the shelf after the timeout.
    selected = window.library_page.detail.work.id
    window.reload_library(selected)
    wait(app, window)
    assert window.library_page.detail.work.id == selected
    assert case.confirms == [(case.title, "close")]
    assert window.lib is case.lib and query(window.lib) > 0


def test_r1_results_applied_after_close_cancelled(blocked_launch, window, app, monkeypatch, tmp_path):
    case = blocked_launch()
    confirmation(window, case, monkeypatch, False)
    request_close(window, case, app)
    case.release.set()
    wait(app, window)
    assert_started_confirmation(case)
    assert case.tracker.stop_calls == 0 and window.closing is False
    path = tmp_path / "another.galdb"
    with store.create_library(str(path)):
        pass
    after_load = []
    monkeypatch.setattr(window, "confirm_stop_tracking", lambda title, action:
                        case.confirms.append((title, action)) or True)
    window.open_library_path(str(path), after_load=lambda: after_load.append("loaded"))
    wait(app, window)
    assert case.confirms == [(case.title, "close"), (case.title, "library")]
    assert isinstance(window.lib, store.Library) and window.lib is not case.lib
    assert window.lib.path == str(path) and query(window.lib) == 0
    assert window.screen == "library" and window.isVisible()
    assert window.library_page.works == [] and after_load == ["loaded"]
    assert window.closing is False and case.tracker.stop_calls == 1


def test_r1_close_during_launch_already_ended(blocked_launch, window, app, monkeypatch):
    # on_end may precede the launch result and even the timer thread's return.
    # Include a write error to check that closing never opens a modal dialog.
    dialogs = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: dialogs.append(a))
    monkeypatch.setattr(window, "notify", window_module.Window.notify.__get__(window))
    for result, thread_done, timeout in ((None, True, False),
                                        (LaunchError("write failure"), False, False),
                                        (None, False, True)):
        case = blocked_launch(join_timeout=timeout)
        confirmation(window, case, monkeypatch, True)
        request_close(window, case, app)
        case.tracker.finish(result, thread_done=thread_done)
        until(app, lambda: case.tracker in window._early_ends)
        case.release.set()
        wait(app, window)
        assert case.confirms == [] and case.tracker.stop_calls == 0
        if timeout:
            assert window.isVisible() and window.lib is case.lib
            assert window.closing is False and query(window.lib) > 0
            assert "正在保存计时，请稍后再试" in window.status.message.text()
            # Retrying close while on_end's thread still returns must also keep
            # the library open, without asking a playing confirmation.
            window.close()
            assert window.isVisible() and window.lib is case.lib
            assert window.closing is False and query(window.lib) > 0
            assert case.calls == [("join", 3), ("join", 3)]
            case.join_timeout = False
            window.close()
            app.processEvents()
            assert case.confirms == [] and case.tracker.stop_calls == 0
        assert not window.isVisible() and window.lib is None
        assert window.tracker is None and window.closing is False
        assert case.calls[-1] == ("library-close", False)
        if not thread_done:
            assert case.calls[0] == ("join", 3)
        if isinstance(result, Exception):
            assert "计时没能写进库：write failure" in window.last_message
    assert dialogs == []


def test_r1_close_during_launch_failure_no_dialog(blocked_launch, window, app, monkeypatch):
    dialogs, new_jobs = [], []
    monkeypatch.setattr(window_module, "choose_executable", lambda *a, **k: dialogs.append("choose"))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: dialogs.append("file"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: dialogs.append("message"))
    monkeypatch.setattr(window, "notify", window_module.Window.notify.__get__(window))
    run_job = window.run_job

    def record_job(label, *args, **kwargs):
        new_jobs.append(label)
        return run_job(label, *args, **kwargs)

    monkeypatch.setattr(window, "run_job", record_job)
    for error in (NotLaunchable("no_exe"), LaunchError("x"), NotLaunchable("exe_missing")):
        case = blocked_launch(error=error)
        confirmation(window, case, monkeypatch, True)
        request_close(window, case, app)
        jobs_before = list(new_jobs)
        case.release.set()
        wait(app, window)
        assert dialogs == [] and new_jobs == jobs_before
        assert case.confirms == [] and not window.jobs
        assert not window.isVisible() and window.lib is None
        assert window.closing is False and case.tracker.stop_calls == 0
        assert window.status.message.text() == str(error)
        assert case.calls == [("library-close", False)]


def test_r1_close_during_other_job_unchanged(window, app, library, monkeypatch):
    window.open_library_path(str(library[0]))
    wait(app, window)
    lib = window.lib
    entered, release = threading.Event(), threading.Event()
    projection = window_module.load_projection
    confirms, after_load = [], []

    def blocked_projection(lib, cancel):
        entered.set()
        if not release.wait(10):
            raise AssertionError("Reload gate was never released")
        return projection(lib, cancel)

    monkeypatch.setattr(window_module, "load_projection", blocked_projection)
    monkeypatch.setattr(window, "confirm_stop_tracking", lambda *args: confirms.append(args) or True)
    window.after_load = lambda: after_load.append("unexpected callback")
    window.reload_library()
    try:
        until(app, entered.is_set)
        window.close()
        app.processEvents()
        assert window.isVisible() and window.closing is True
        assert window.lib is lib and query(lib) > 0
        assert confirms == []
    finally:
        release.set()
        wait(app, window)
    assert confirms == [] and after_load == []
    assert not window.isVisible() and window.lib is None
    assert window.closing is False and not window.jobs
