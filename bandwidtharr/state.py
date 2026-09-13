import threading
import time
from collections import deque


class SharedState:
    """Thread-safe snapshot of the latest poll cycle, read by the web server
    and written by the main polling loop."""

    def __init__(self, history_len: int = 600):
        self._lock = threading.Lock()
        self._total = 0.0
        self._qbit_speed = 0.0
        self._qbit_limit = 0.0
        self._sab_speed = 0.0
        self._sab_limit = 0.0
        self._history: deque = deque(maxlen=history_len)

    def update(self, total: float, qbit_speed: float, qbit_limit: float, sab_speed: float, sab_limit: float) -> None:
        with self._lock:
            self._total = total
            self._qbit_speed = qbit_speed
            self._qbit_limit = qbit_limit
            self._sab_speed = sab_speed
            self._sab_limit = sab_limit
            self._history.append((time.time(), qbit_speed, sab_speed))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total": self._total,
                "qbit_speed": self._qbit_speed,
                "qbit_limit": self._qbit_limit,
                "sab_speed": self._sab_speed,
                "sab_limit": self._sab_limit,
                "history": list(self._history),
            }
