"""JSONL event log: one file per session, one JSON object per line, flushed per event.

Each line is ``{"t_unix": <clock()>, "event": <name>, **fields}``. Field values are
converted to plain JSON (pydantic models via ``model_dump(mode="json")``, dataclasses,
sets, paths, enums, ...) and anything unknown falls back to ``str()``, so logging an
odd value never raises.

Envelope protection: a field named ``t_unix`` or ``event`` is not allowed to overwrite
the envelope. It is kept under a ``field_`` prefix instead (``field_t_unix``), because
messages such as ``stim_onset`` carry their own ``t_unix`` and callers will often log a
whole message with ``**msg.model_dump()``. Only if that prefixed name is also taken
does ``log`` raise ``ValueError``.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
RESERVED_KEYS = ("t_unix", "event")
RESERVED_PREFIX = "field_"


def _jsonable(value: Any) -> Any:
    """Convert ``value`` to something ``json.dumps`` accepts, never raising."""
    try:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            # NaN/inf would produce invalid JSON lines.
            return value if math.isfinite(value) else str(value)
        if hasattr(value, "model_dump") and not isinstance(value, type):
            return _jsonable(value.model_dump(mode="json"))
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return _jsonable(dataclasses.asdict(value))
        if isinstance(value, enum.Enum):
            return _jsonable(value.value)
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (set, frozenset)):
            items = [_jsonable(v) for v in value]
            try:
                return sorted(items)
            except TypeError:
                return items
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        return str(value)
    except Exception:  # a broken model_dump or __str__ must not break logging
        try:
            return str(value)
        except Exception:
            return f"<unprintable {type(value).__name__}>"


class EventLog:
    """Append-only JSONL log for one session. Thread-safe; use as a context manager."""

    def __init__(
        self,
        log_dir: str | Path = DEFAULT_LOG_DIR,
        session_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.clock = clock
        self.session_id = session_id or secrets.token_hex(3)
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(clock()))
        self.path = log_dir / f"{stamp}_{self.session_id}.jsonl"
        self._file = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._closed = False

    def log(self, event: str, /, **fields: Any) -> dict:
        """Write one event line, flush it, and return the record that was written."""
        record: dict[str, Any] = {"t_unix": self.clock(), "event": event}
        for key, value in fields.items():
            if key in RESERVED_KEYS:
                key = RESERVED_PREFIX + key
                if key in fields:
                    raise ValueError(f"field {key!r} collides with a renamed envelope field")
            record[key] = _jsonable(value)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            if self._closed:
                raise ValueError(f"EventLog {self.path} is closed")
            self._file.write(line + "\n")
            self._file.flush()
        return record

    def close(self) -> None:
        """Close the file. Safe to call more than once."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._file.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: str | Path) -> list[dict]:
    """Read every event from a JSONL log file, skipping blank lines.

    A malformed last line is skipped: it is what a crash in the middle of a write leaves
    behind, and the events before it are still good. A malformed line anywhere else
    still raises ``json.JSONDecodeError``, since that means the file is really corrupt.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [line for line in f if line.strip()]
    events = []
    for i, line in enumerate(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return events
