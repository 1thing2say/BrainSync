"""Decoder feedback for the free run: commands read line by line from a stream.

A decoder writes one command per line:

    k          put the cue box on target k (-1 clears it); the first one ends --hold-still
    card k     show profile card k (index into the sorted --cards PNGs)
    flash k    light target k's box in its flash colour for [card] flash_s seconds
    lean k p n m   the decoder leans towards target k with confidence p % (0-100), and
               n of the m agreeing windows it needs to commit are in; "lean -1" hides it

Only the integer lines count as "heard": card, flash and lean lines may arrive while the
targets are still held still. Bad lines are ignored with a note. EOF ends the free run.
The stream is read on a daemon thread; the display polls ``current``, ``card`` and
``flash``, ``lean`` once per frame, so a chatty decoder never stalls the flicker.

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
CUE, CARD, FLASH, LEAN = "cue", "card", "flash", "lean"
NO_LEAN = (NONE, 0, 0, 0)


@dataclass(frozen=True)
class Command:
    """One parsed feedback line: ``kind`` is cue, card, flash or lean; ``value`` its index;
    ``extra`` a lean line's (percent, agreeing windows, windows needed)."""

    kind: str
    value: int
    extra: tuple[int, ...] = ()


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


def _lean(words: list[str], n_targets: int) -> Command:
    try:
        nums = [int(w) for w in words[1:]]
    except ValueError:
        raise ValueError(f"lean values not integers: {' '.join(words[1:])!r}") from None
    if nums == [NONE]:
        return Command(LEAN, NONE, NO_LEAN[1:])
    if len(nums) != 4:
        raise ValueError(f"expected 'lean <target> <percent> <n> <m>' or 'lean -1', "
                         f"got {' '.join(words)!r}")
    k, pct, n, m = nums
    if not 0 <= k < n_targets:
        raise ValueError(f"lean {k} out of range (targets 0..{n_targets - 1})")
    if not 0 <= pct <= 100 or m < 1 or not 0 <= n <= m:
        raise ValueError(f"lean needs 0 <= percent <= 100 and 0 <= n <= m >= 1, got {nums[1:]}")
    return Command(LEAN, k, (pct, n, m))


def parse_command(line: str, n_targets: int, n_cards: int = 0) -> Command:
    """The command on one feedback line (see the module docstring); ValueError otherwise."""
    words = line.split()
    if words and words[0] == LEAN:
        return _lean(words, n_targets)
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
        self._lean = NO_LEAN
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
    def lean(self) -> tuple[int, int, int, int]:
        """(target, percent, n, m) of the latest lean line; target -1: nothing to show."""
        return self._lean

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
                    cmd = parse_command(line, self.n_targets, self.n_cards)
                except ValueError as exc:
                    self.n_ignored += 1
                    self._note(f"ignored line {self.n_lines}: {exc}")
                    continue
                if cmd.kind == CARD:
                    self._card = cmd.value
                elif cmd.kind == FLASH:
                    self._flash = (cmd.value, self._flash[1] + 1)
                elif cmd.kind == LEAN:
                    self._lean = (cmd.value, *cmd.extra)
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
