import threading
from PySide6.QtCore import QThread, Signal


class Job(QThread):
    result = Signal(object)
    failed = Signal(object)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            value = self.task(self.cancel_event.is_set)
        except Exception as e:
            self.failed.emit(e)
        else:
            self.result.emit(value)


class NetJob(Job):
    """Cancelable network-only task; does not hold the Window's library job slot."""
    pass
