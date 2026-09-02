"""
SovNode — Async ThreadPool Executor
===================================
Ejecución asíncrona no bloqueante para la UI PyQt6.
"""

from __future__ import annotations

from typing import Callable, Any
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

class WorkerSignals(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

class AsyncTaskRunnable(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.signals.finished.emit(result)
        except Exception as exc:
            self.signals.error.emit(str(exc))

class AsyncExecutor:
    def __init__(self, max_threads: int = 4) -> None:
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max_threads)

    def submit_task(
        self,
        task_fn: Callable[..., Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
        *args: Any,
        **kwargs: Any
    ) -> None:
        runnable = AsyncTaskRunnable(task_fn, *args, **kwargs)
        runnable.signals.finished.connect(on_success)
        if on_error:
            runnable.signals.error.connect(on_error)
        self.pool.start(runnable)