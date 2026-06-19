"""Cooperative cancellation primitives."""

from __future__ import annotations

import threading


class CancellationToken:
    """Thread-safe cancellation signal shared by runtimes and adapters."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    def cancel(self, reason: str = "") -> None:
        with self._lock:
            self._reason = reason or self._reason or "Cancellation requested."
            self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason
