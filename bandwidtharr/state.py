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
        # What the dashboard sees before the first poll cycle lands.
        self._latest: dict = dict(
            total=0.0, qbit_speed=0.0, qbit_limit=0.0, sab_speed=0.0, sab_limit=0.0,
            qbit_ok=False, sab_ok=False, qbit_error=None, sab_error=None,
            link_enabled=False, active_link=PRIMARY, link_ok=True, link_error=None,
            last_link_check_at=None, next_link_check=None,
        )
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
        except (ValueError, OSError):
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

    def update(self, link_event: tuple | None = None, **fields) -> None:
        """`fields` are the per-cycle values the dashboard shows (the keys
        in `_latest`); `link_event` is appended to the persisted failover
        log when given."""
        with self._lock:
            self._latest.update(fields)
            self._history.append((time.time(), fields["qbit_speed"], fields["sab_speed"]))
            if link_event is not None:
                self._link_events.append(link_event)
                self._save_link_events()

    def snapshot(self) -> dict:
        with self._lock:
            return {**self._latest, "history": list(self._history), "link_events": list(self._link_events)}
