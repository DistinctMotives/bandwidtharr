import threading
import time
from collections import deque


class SharedState:
    """Thread-safe snapshot of the latest poll cycle, read by the web server
    and written by the main polling loop."""

    def __init__(self, history_len: int = 600, link_events_len: int = 50):
        self._lock = threading.Lock()
        self._total = 0.0
        self._qbit_speed = 0.0
        self._qbit_limit = 0.0
        self._sab_speed = 0.0
        self._sab_limit = 0.0
        self._qbit_ok = False
        self._sab_ok = False
        self._qbit_error: str | None = None
        self._sab_error: str | None = None
        self._link_enabled = False
        self._active_link = "primary"
        self._link_ok = True
        self._link_error: str | None = None
        self._link_detail = ""
        self._last_link_check_at: float | None = None
        self._next_link_check: float | None = None
        self._history: deque = deque(maxlen=history_len)
        self._link_events: deque = deque(maxlen=link_events_len)

    def update(
        self,
        total: float,
        qbit_speed: float,
        qbit_limit: float,
        sab_speed: float,
        sab_limit: float,
        qbit_ok: bool,
        sab_ok: bool,
        qbit_error: str | None = None,
        sab_error: str | None = None,
        link_enabled: bool = False,
        active_link: str = "primary",
        link_ok: bool = True,
        link_error: str | None = None,
        link_detail: str = "",
        last_link_check_at: float | None = None,
        next_link_check: float | None = None,
        link_event: tuple | None = None,
    ) -> None:
        with self._lock:
            self._total = total
            self._qbit_speed = qbit_speed
            self._qbit_limit = qbit_limit
            self._sab_speed = sab_speed
            self._sab_limit = sab_limit
            self._qbit_ok = qbit_ok
            self._sab_ok = sab_ok
            self._qbit_error = qbit_error
            self._sab_error = sab_error
            self._link_enabled = link_enabled
            self._active_link = active_link
            self._link_ok = link_ok
            self._link_error = link_error
            self._link_detail = link_detail
            self._last_link_check_at = last_link_check_at
            self._next_link_check = next_link_check
            self._history.append((time.time(), qbit_speed, sab_speed))
            if link_event is not None:
                self._link_events.append(link_event)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total": self._total,
                "qbit_speed": self._qbit_speed,
                "qbit_limit": self._qbit_limit,
                "sab_speed": self._sab_speed,
                "sab_limit": self._sab_limit,
                "qbit_ok": self._qbit_ok,
                "sab_ok": self._sab_ok,
                "qbit_error": self._qbit_error,
                "sab_error": self._sab_error,
                "link_enabled": self._link_enabled,
                "active_link": self._active_link,
                "link_ok": self._link_ok,
                "link_error": self._link_error,
                "link_detail": self._link_detail,
                "last_link_check_at": self._last_link_check_at,
                "next_link_check": self._next_link_check,
                "history": list(self._history),
                "link_events": list(self._link_events),
            }
