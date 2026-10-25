"""Decoder feedback for the free run: commands read line by line from a stream.

A decoder writes one command per line:

    k          put the cue box on target k (-1 clears it); the first one ends --hold-still
    card k     show profile card k (index into the sorted --cards PNGs)
    flash k    light target k's box in its flash colour for [card] flash_s seconds

A bridge that owns the screen (protocol 2, INTERFACE-v2-draft.md section 7) also writes
JSON objects, one per line, next to the integer lines:

    {"labels": ["...", "...", "...", "..."]}   one sentence per target, beside its square
                                                ("" clears that one); one entry per target
    {"text": "..."}       one line at the centre of the screen ("" clears it)
    {"active": [0, 3]}    which targets flicker on the next flicker-on; the others stay
                          drawn still (all of them until the first "active")
    {"flicker": true}     start flickering the active targets, phase restarting at 0
    {"flicker": false}    hold every target still

One object may carry several of these keys; "active" then applies before "flicker".
A line with an unknown key or a bad value is ignored whole, with a note. From the first
JSON line on (a rejected one counts too: any line starting with "{"), only
{"flicker": ...} turns the flicker on or off: integer lines then only move the cue box
(see ``Control.used``). A decoder that sends only integer lines keeps the
old behaviour, where its first integer line ends --hold-still.

Only the integer lines count as "heard": card and flash lines may arrive while the
targets are still held still. Bad lines are ignored with a note. EOF ends the free run.
The stream is read on a daemon thread; the display polls ``current``, ``card``,
``flash`` and ``control`` once per frame, so a chatty decoder never stalls the flicker.

A cued session with ``--status stdin`` reads ``{"text": "..."}`` JSON lines instead
(StatusReader): the latest text is shown under the targets between flickers.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

NONE = -1
CUE, CARD, FLASH, CONTROL = "cue", "card", "flash", "control"
# the JSON keys a bridge may send, in the order they are applied within one line
CONTROL_KEYS = ("labels", "text", "active", "flicker")


@dataclass(frozen=True)
class Command:
    """One parsed feedback line: ``kind`` is cue, card, flash or control; ``value`` the
    cue, card or flash index; ``fields`` the checked JSON keys of a control line."""

    kind: str
    value: int
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Control:
    """What the JSON lines have set so far; replaced whole by the reader on every line.

    ``used`` turns true with the first JSON line (even one that is rejected), and from then on
    the flicker follows ``flicker`` only. ``serial`` counts {"flicker": true} lines, so the
    display sees every flicker-on (a repeated one restarts the phase), and ``flickering`` is
    the active set captured by the latest one. ``active`` is the set for the next flicker-on.
    """

    used: bool = False
    labels: tuple[str, ...] = ()
    text: str = ""
    active: tuple[int, ...] = ()
    flicker: bool = False
    serial: int = 0
    flickering: tuple[int, ...] = ()


def initial_control(n_targets: int) -> Control:
    """Before any JSON line: no text, every target active, nothing flickering."""
    return Control(labels=("",) * n_targets, active=tuple(range(n_targets)))


def parse_control(text: str, n_targets: int) -> dict[str, Any]:
    """The checked keys of one JSON control line; ValueError for anything else."""
    try:
        msg = json.loads(text)
    except ValueError:
        raise ValueError(f"not valid JSON: {text.strip()!r}") from None
    if not isinstance(msg, dict) or not msg:
        raise ValueError(f"expected a JSON object with one of {list(CONTROL_KEYS)}, "
                         f"got {text.strip()!r}")
    unknown = sorted(set(msg) - set(CONTROL_KEYS))
    if unknown:
        raise ValueError(f"unknown key(s) {unknown}; known: {list(CONTROL_KEYS)}")
    out: dict[str, Any] = {}
    if "labels" in msg:
        labels = msg["labels"]
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise ValueError(f"labels must be a list of strings, got {labels!r}")
        if len(labels) != n_targets:
            raise ValueError(f"labels has {len(labels)} entries for {n_targets} targets")
        out["labels"] = tuple(labels)
    if "text" in msg:
        if not isinstance(msg["text"], str):
            raise ValueError(f"text must be a string, got {msg['text']!r}")
        out["text"] = msg["text"]
    if "active" in msg:
        active = msg["active"]
        if (not isinstance(active, list)
                or any(isinstance(k, bool) or not isinstance(k, int) for k in active)):
            raise ValueError(f"active must be a list of target indices, got {active!r}")
        bad = [k for k in active if not 0 <= k < n_targets]
        if bad:
            raise ValueError(f"active target {bad[0]} out of range 0..{n_targets - 1}")
        if len(set(active)) != len(active):
            raise ValueError(f"active lists a target twice: {active!r}")
        out["active"] = tuple(active)
    if "flicker" in msg:
        if not isinstance(msg["flicker"], bool):
            raise ValueError(f"flicker must be true or false, got {msg['flicker']!r}")
        out["flicker"] = msg["flicker"]
    return out


def apply_control(state: Control, fields: Mapping[str, Any]) -> Control:
    """``state`` after one control line's ``fields`` (labels, text, active, then flicker)."""
    changes: dict[str, Any] = {"used": True}
    for key in ("labels", "active"):
        if key in fields:
            changes[key] = tuple(fields[key])
    if "text" in fields:
        changes["text"] = fields["text"]
    if "flicker" in fields:
        changes["flicker"] = fields["flicker"]
        if fields["flicker"]:
            changes["serial"] = state.serial + 1
            changes["flickering"] = changes.get("active", state.active)
    return dataclasses.replace(state, **changes)


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


def parse_command(line: str, n_targets: int, n_cards: int = 0) -> Command:
    """The command on one feedback line (see the module docstring); ValueError otherwise."""
    if line.lstrip().startswith("{"):
        return Command(CONTROL, NONE, parse_control(line, n_targets))
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
        self._control = initial_control(n_targets)
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
    def control(self) -> Control:
        """The state the JSON lines have set (labels, text, active targets, flicker)."""
        return self._control

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
                    if line.lstrip().startswith("{") and not self._control.used:
                        # fail safe: a driver that speaks JSON at all (even a rejected or
                        # reserved line such as {"scores": ...}) never gets flicker from an
                        # integer line, only from {"flicker": true}
                        self._control = dataclasses.replace(self._control, used=True)
                    continue
                if cmd.kind == CONTROL:
                    self._control = apply_control(self._control, cmd.fields)
                elif cmd.kind == CARD:
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
