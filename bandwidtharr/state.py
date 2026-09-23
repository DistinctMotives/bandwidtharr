import json
import logging
import os
import threading
import time
from collections import deque

from bandwidtharr.link_detector import PRIMARY

log = logging.getLogger("bandwidtharr.state")


class SharedState:
    """Thread-safe snapshot of the latest poll cycle, read by the web server
    and written by the main polling loop."""

    def __init__(
        self,
        history_len: int = 600,
        link_events_len: int = 50,
        link_events_file: str | None = None,
    ):
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
        self._active_link = PRIMARY
        self._link_ok = True
        self._link_error: str | None = None
        self._last_link_check_at: float | None = None
        self._next_link_check: float | None = None
        self._downloading = False
        self._history: deque = deque(maxlen=history_len)
        self._link_events_file = link_events_file
        # deque(iterable, maxlen=N) keeps only the last N items of whatever
        # was loaded, so a file with more than link_events_len entries (e.g.
        # from a run with a larger cap) is trimmed automatically.
        self._link_events: deque = deque(self._load_link_events(), maxlen=link_events_len)

    def _load_link_events(self) -> list:
        if not self._link_events_file:
            return []
        try:
            with open(self._link_events_file) as f:
                events = [tuple(e) for e in json.load(f)]
            log.info(
                "state: loaded %d persisted failover log entries from %s",
                len(events), self._link_events_file,
            )
            return events
        except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
            return []

    def _save_link_events(self) -> None:
        if not self._link_events_file:
            return
        try:
            # write-then-fsync-then-rename, so neither a process crash nor a
            # host power loss (the rename can be journaled before the tmp
            # file's data reaches disk without the fsync) can leave a
            # truncated log file behind -- _load_link_events does handle a
            # corrupt file (starts empty rather than crashing), but this
            # keeps a rare event log from being silently lost outright.
            tmp_path = self._link_events_file + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(list(self._link_events), f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._link_events_file)
        except OSError as e:
            log.warning("state: failed to persist failover log to %s: %s", self._link_events_file, e)

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
        active_link: str = PRIMARY,
        link_ok: bool = True,
        link_error: str | None = None,
        last_link_check_at: float | None = None,
        next_link_check: float | None = None,
        downloading: bool = False,
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
            self._last_link_check_at = last_link_check_at
            self._next_link_check = next_link_check
            self._downloading = downloading
            self._history.append((time.time(), qbit_speed, sab_speed))
            if link_event is not None:
                self._link_events.append(link_event)
                self._save_link_events()

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
                "last_link_check_at": self._last_link_check_at,
                "next_link_check": self._next_link_check,
                "downloading": self._downloading,
                "history": list(self._history),
                "link_events": list(self._link_events),
            }
