"""Decoder feedback for the free run: commands read line by line from a stream.

A decoder writes one command per line:

    k          put the cue box on target k (-1 clears it); the first one ends --hold-still
    card k     show profile card k (index into the sorted --cards PNGs)
    flash k    light target k's box in its flash colour for [card] flash_s seconds
    {"text": "..."}   the status line (--status stdin; "" clears it)
    {"labels": ["...", ...]}   one label per target, replacing the config's (--labels)

Only the integer lines count as "heard": card, flash and text lines may arrive while the
targets are still held still. Bad lines are ignored with a note. EOF ends the free run.
The stream is read on a daemon thread; the display polls ``current``, ``card`` and
``flash`` once per frame, so a chatty decoder never stalls the flicker.

A cued session with ``--status stdin`` reads ``{"text": "..."}`` JSON lines instead
(StatusReader): the latest text is shown under the targets between flickers.
"""
from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

NONE = -1
CUE, CARD, FLASH = "cue", "card", "flash"


@dataclass(frozen=True)
class Command:
    """One parsed feedback line: ``kind`` is cue, card or flash; ``value`` its index."""

    kind: str
    value: int


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


def _index(kind: str, text: str, n: int, noun: str) -> int:
    try:
        k = int(text)
    except ValueError:
        raise ValueError(f"{kind} index not an integer: {text!r}") from None
    if not 0 <= k < n:
        span = f"{noun} 0..{n - 1}" if n > 0 else f"no {noun} loaded"
        raise ValueError(f"{kind} {k} out of range ({span})")
    return k


def parse_json(line: str, n_targets: int) -> tuple[str, str | tuple[str, ...]]:
    """A ``{"text": "..."}`` or ``{"labels": [...]}`` JSON line as ``("text", str)`` or
    ``("labels", tuple)`` (one string per target); ValueError for anything else."""
    try:
        msg = json.loads(line)
    except ValueError:
        raise ValueError(f"not a JSON object: {line.strip()!r}") from None
    if isinstance(msg, dict) and isinstance(msg.get("text"), str):
        return "text", msg["text"]
    if isinstance(msg, dict) and isinstance(msg.get("labels"), list):
        labels = msg["labels"]
        if len(labels) != n_targets or not all(isinstance(s, str) for s in labels):
            raise ValueError(f'"labels" needs {n_targets} strings, got {labels!r}')
        return "labels", tuple(labels)
    raise ValueError('expected {"text": "..."} or {"labels": [...]}')


def parse_command(line: str, n_targets: int, n_cards: int = 0) -> Command:
    """The command on one feedback line (see the module docstring); ValueError otherwise."""
    words = line.split()
    if len(words) == 2 and words[0] == CARD:
        return Command(CARD, _index(CARD, words[1], n_cards, "cards"))
    if len(words) == 2 and words[0] == FLASH:
        return Command(FLASH, _index(FLASH, words[1], n_targets, "targets"))
    if words and words[0] in (CARD, FLASH):
        raise ValueError(f"expected '{words[0]} <index>', got {line.strip()!r}")
    return Command(CUE, parse_line(line, n_targets))


def _stderr_note(msg: str) -> None:
    print(f"feedback: {msg}", file=sys.stderr, flush=True)


class FeedbackReader:
    """Holds the latest cue, card and flash read from ``stream``; ``done`` once it hits EOF.

    ``n_cards`` is how many card images the display has loaded (0: none, so every card
    line is refused).
    """

    def __init__(self, stream: TextIO, n_targets: int,
                 note: Callable[[str], None] = _stderr_note, n_cards: int = 0) -> None:
        if n_targets < 1:
            raise ValueError(f"need at least one target, got {n_targets}")
        if n_cards < 0:
            raise ValueError(f"n_cards must be >= 0, got {n_cards}")
        self.stream = stream
        self.n_targets = n_targets
        self.n_cards = n_cards
        self._note = note
        self._current = NONE
        self._card = 0 if n_cards > 0 else NONE
        # one tuple, replaced whole, so the display never reads a target from one flash
        # with the serial of another
        self._flash = (NONE, 0)
        self._text = ""
        self._labels: tuple[str, ...] | None = None
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
    def card(self) -> int:
        """Latest valid card index: 0 until a card line arrives, -1 with no cards."""
        return self._card

    @property
    def flash(self) -> tuple[int, int]:
        """(target, serial) of the latest flash; serial counts valid flash lines."""
        return self._flash

    @property
    def text(self) -> str:
        """The latest {"text": ...} line's text ("" until one arrives)."""
        return self._text

    @property
    def labels(self) -> tuple[str, ...] | None:
        """The latest {"labels": [...]} line, one string per target; None until one arrives."""
        return self._labels

    @property
    def done(self) -> bool:
        return self._eof.is_set()

    @property
    def heard(self) -> bool:
        """True once a valid cue line has arrived (or the stream ended)."""
        return self._heard.is_set() or self._eof.is_set()

    def run(self) -> None:
        """Read until EOF (blocking); called on the reader thread by start()."""
        try:
            for line in iter(self.stream.readline, ""):
                self.n_lines += 1
                try:
                    if line.lstrip().startswith("{"):
                        kind, value = parse_json(line, self.n_targets)
                        if kind == "text":
                            self._text = str(value)
                        else:
                            self._labels = tuple(value)
                        continue
                    cmd = parse_command(line, self.n_targets, self.n_cards)
                except ValueError as exc:
                    self.n_ignored += 1
                    self._note(f"ignored line {self.n_lines}: {exc}")
                    continue
                if cmd.kind == CARD:
                    self._card = cmd.value
                elif cmd.kind == FLASH:
                    self._flash = (cmd.value, self._flash[1] + 1)
                else:
                    self._current = cmd.value
                    self._heard.set()
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


class StatusReader:
    """Holds the latest status text a cued session shows under its targets.

    Each line on ``stream`` is a JSON object ``{"text": "..."}`` (newlines inside the
    string make several screen lines); it replaces the text shown. Other lines are ignored
    with a note. EOF sets ``done``. Read on a daemon thread, like FeedbackReader.
    """

    def __init__(self, stream: TextIO, note: Callable[[str], None] = _stderr_note) -> None:
        self.stream = stream
        self._note = note
        self._text = ""
        self._eof = threading.Event()
        self._thread: threading.Thread | None = None
        self.n_lines = 0

    @property
    def text(self) -> str:
        return self._text

    @property
    def done(self) -> bool:
        return self._eof.is_set()

    def run(self) -> None:
        try:
            for line in iter(self.stream.readline, ""):
                self.n_lines += 1
                try:
                    msg = json.loads(line)
                except ValueError:
                    msg = None
                if not isinstance(msg, dict) or not isinstance(msg.get("text"), str):
                    self._note(f"ignored status line {self.n_lines}: expected "
                               '{"text": "..."}')
                    continue
                self._text = msg["text"]
        except (OSError, ValueError) as exc:  # closed stream
            self._note(f"stopped reading: {exc}")
        finally:
            self._eof.set()

    def start(self) -> StatusReader:
        if self._thread is None:
            self._thread = threading.Thread(target=self.run, name="status-reader", daemon=True)
            self._thread.start()
        return self

    def wait(self, timeout: float | None = None) -> bool:
        """Block until EOF or timeout; True when EOF was reached."""
        return self._eof.wait(timeout)
