# Copyright (c) 2026 Stephen Díaz
# SovNode - Local Desktop AI Application
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
SovNode — Async ThreadPool Executor
===================================
ES: Ejecución asíncrona no bloqueante para la UI PyQt6.
EN: Non-blocking asynchronous execution for the PyQt6 UI.
"""

from __future__ import annotations

from typing import Callable, Any
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

class WorkerSignals(QObject):
    """ES: Señales Qt para reportar resultado, error y progreso de una tarea en background.
    EN: Qt signals to report a background task's result, error, and progress."""
    finished = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

class AsyncTaskRunnable(QRunnable):
    """ES: Envuelve una función arbitraria para ejecutarla en el QThreadPool y emitir su resultado por señales.
    EN: Wraps an arbitrary function to run it on the QThreadPool and emit its result via signals."""
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
    """ES: Pool de hilos compartido para correr tareas pesadas sin bloquear la UI.
    EN: Shared thread pool for running heavy tasks without blocking the UI."""
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
        """ES: Encola task_fn en el pool y conecta los callbacks de éxito/error.
        EN: Queues task_fn on the pool and wires up the success/error callbacks."""
        runnable = AsyncTaskRunnable(task_fn, *args, **kwargs)
        runnable.signals.finished.connect(on_success)
        if on_error:
            runnable.signals.error.connect(on_error)
        self.pool.start(runnable)
