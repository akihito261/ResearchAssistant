from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


LOGGER = logging.getLogger(__name__)


class WorkerSignals(QObject):
    result = Signal(object)
    progress = Signal(object)
    error = Signal(object)
    finished = Signal()


class FunctionWorker(QRunnable):
    """Run one callable in QThreadPool and report its result to the UI thread."""

    def __init__(
        self,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function(*self.args, **self.kwargs)
        except Exception as error:
            LOGGER.exception("Background task failed")
            self._emit_if_alive(self.signals.error, error)
        else:
            self._emit_if_alive(self.signals.result, result)
        finally:
            self._emit_if_alive(self.signals.finished)

    @staticmethod
    def _emit_if_alive(signal: Any, *values: Any) -> bool:
        """Ignore delivery only when Qt is already tearing down signal objects."""
        try:
            signal.emit(*values)
        except RuntimeError as error:
            if "deleted" not in str(error).lower():
                raise
            LOGGER.debug("Background result discarded during Qt shutdown: %s", error)
            return False
        return True
