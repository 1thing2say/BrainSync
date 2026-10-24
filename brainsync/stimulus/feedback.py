"""Decoder feedback for the free run: target indices read line by line from a stream.

A decoder writes one integer per line: ``k`` puts the cue box on target ``k``, ``-1``
clears it. Bad lines are ignored with a note. EOF ends the free run. The stream is read
on a daemon thread; the display polls ``current`` once per frame, so a chatty decoder
never stalls the flicker.
"""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import TextIO

NONE = -1


def parse_line(line: str, n_targets: int) -> int:
    """The target index on one feedback line (-1 clears); ValueError for anything else."""
    text = line.strip()
    if not text:
        raise ValueError("blank line")
    try:
        k = int(text)
    except ValueError:
        raise ValueError(f"not an integer: {text!r}") from None
    if k != NONE and not 0 <= k < n_targets:
        raise ValueError(f"target {k} out of range 0..{n_targets - 1} (or -1)")
    return k


def _stderr_note(msg: str) -> None:
    print(f"feedback: {msg}", file=sys.stderr, flush=True)


class FeedbackReader:
    """Holds the latest target index read from ``stream``; ``done`` once it hits EOF."""

    def __init__(self, stream: TextIO, n_targets: int,
                 note: Callable[[str], None] = _stderr_note) -> None:
        if n_targets < 1:
            raise ValueError(f"need at least one target, got {n_targets}")
        self.stream = stream
        self.n_targets = n_targets
        self._note = note
        self._current = NONE
        self._eof = threading.Event()
        self._heard = threading.Event()
        self._thread: threading.Thread | None = None
        self.n_lines = 0
        self.n_ignored = 0

    @property
    def current(self) -> int:
        """Latest valid target index, or -1 for no cue."""
        return self._current

    @property
    def done(self) -> bool:
        return self._eof.is_set()

    @property
    def heard(self) -> bool:
        """True once a valid line has arrived (or the stream ended)."""
        return self._heard.is_set() or self._eof.is_set()

    def run(self) -> None:
        """Read until EOF (blocking); called on the reader thread by start()."""
        try:
            for line in iter(self.stream.readline, ""):
                self.n_lines += 1
                try:
                    self._current = parse_line(line, self.n_targets)
                    self._heard.set()
                except ValueError as exc:
                    self.n_ignored += 1
                    self._note(f"ignored line {self.n_lines}: {exc}")
        except (OSError, ValueError) as exc:  # closed stream
            self._note(f"stopped reading: {exc}")
        finally:
            self._eof.set()

    def start(self) -> FeedbackReader:
        if self._thread is None:
            self._thread = threading.Thread(target=self.run, name="feedback-reader", daemon=True)
            self._thread.start()
        return self

    def wait(self, timeout: float | None = None) -> bool:
        """Block until EOF or timeout; True when EOF was reached."""
        return self._eof.wait(timeout)
