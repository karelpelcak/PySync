import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal
from pysync_client_cli.engine import friendly_error


class Worker(QThread):
    completed = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, key: str, operation: Callable[[], Any], parent=None):
        super().__init__(parent)
        self.key, self.operation = key, operation

    def run(self) -> None:
        try:
            result = self.operation()
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            self.completed.emit(self.key, result)
        except Exception as error:
            self.failed.emit(self.key, friendly_error(error))
