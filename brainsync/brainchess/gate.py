"""The fresh-window gate that turns sliding-window decisions into BrainChess picks.

Decoder windows overlap (1.5 s at a 0.25 s hop), so right after a pick the decoder still
scores EEG recorded while the user looked at the previous board, where the other group
may have been the one they watched, and the eyes need a moment to find their square in
the new split. A decision is therefore stale, and ignored, when its window started before
the screen changed plus ``cooldown_s``. Fresh decisions feed the gate's own ``Voter``
(from ``Decision.raw``; ``Decision.committed`` comes from the decoder's voter, which also
counted stale windows). ``votes`` consecutive fresh windows naming the same target fire a
pick, and since every pick redraws the board the gate starts the next fresh period
itself. Picks are thus at least cooldown + window + (votes - 1) x step apart (0.5 + 1.5 +
0.75 = 2.75 s with the demo defaults), and each is decoded only from EEG recorded while
its split was on screen and the eyes had settled.

The same logic as BrainSwipe's ``Swiper`` (a card there, a split of the board here).

``CodeGate`` is the same idea for c-VEP (``brainsync.vep.live.CodeVepDecoder``, the
BrainSync standard): decisions come once per code cycle (1.05 s) from the mean of the last
cycles, so instead of windows it drops whole cycles. On every screen change it resets the
decoder (cycles and votes) and from then on passes it only the cycles that began at least
``cooldown_s`` after the change, so a pick is decided from cycles shown entirely on the new
screen with the eyes settled; the decoder's own commitment (``votes`` agreeing decisions)
is the pick. Times are Unix seconds on the board's clock (cycle markers, sample times).
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable

from ..classify import ABSTAIN
from ..online import Decision, Decoder, Voter
from ..recordings import Event

__all__ = ["ABSTAIN", "CodeGate", "MoveGate"]


class MoveGate:
    """Fires a pick once ``votes`` fresh consecutive windows name the same target.

    ``votes`` defaults to the decoder's. The screen shown when the gate is made counts as
    changed at ``decoder.n_seen``; call ``screen_changed`` whenever the board is redrawn
    for another reason (opponent move, BACK). ``push`` calls it itself on every pick.
    """

    def __init__(self, decoder: Decoder, votes: int | None = None, cooldown_s: float = 0.5) -> None:
        if not math.isfinite(cooldown_s) or cooldown_s < 0:
            raise ValueError(f"cooldown_s must be finite and >= 0, got {cooldown_s}")
        self.decoder = decoder
        self.cooldown_s = float(cooldown_s)
        self.cooldown = int(round(self.cooldown_s * decoder.fs))   # samples
        self.voter = Voter(decoder.votes if votes is None else votes)
        self.stale = 0     # decisions ignored because their window started too early
        self.fired = 0     # picks
        self._switch = 0
        self._pending = ABSTAIN
        self._streak = 0
        self.screen_changed()

    @property
    def votes(self) -> int:
        return self.voter.votes

    @property
    def switch(self) -> int:
        """Stream position (decoder samples) at which the current screen appeared."""
        return self._switch

    @property
    def fresh_from(self) -> int:
        """Earliest window start that counts: the screen change plus the settle delay."""
        return self._switch + self.cooldown

    @property
    def pending(self) -> int:
        """Target the current run of fresh windows names, or ABSTAIN."""
        return self._pending

    @property
    def streak(self) -> int:
        """Length of that run (0 while pending is ABSTAIN); a pick fires at ``votes``."""
        return self._streak

    def screen_changed(self, at: int | None = None) -> None:
        """The screen changed at stream position ``at`` (default: samples seen so far)."""
        self._switch = int(self.decoder.n_seen if at is None else at)
        self.voter.reset()
        self._pending, self._streak = ABSTAIN, 0

    def is_stale(self, d: Decision) -> bool:
        """True when ``d``'s window started before the current screen had settled."""
        return d.end - self.decoder.n_window < self.fresh_from

    def stream_time(self, d: Decision) -> float:
        """Seconds of stream fed when ``d`` was made (the trace strip's clock)."""
        return d.end / self.decoder.fs

    def push(self, d: Decision) -> int:
        """Take one decision; the target that fires on it, or ABSTAIN."""
        if self.is_stale(d):
            self.stale += 1
            return ABSTAIN
        raw = int(d.raw)
        if raw == ABSTAIN:
            self._pending, self._streak = ABSTAIN, 0
        elif raw == self._pending:
            self._streak += 1
        else:
            self._pending, self._streak = raw, 1
        # the voter is reset on every pick, so its commitment is ABSTAIN between picks
        # and a change can only be a target reaching ``votes`` agreeing windows
        if not self.voter.push(raw) or self.voter.committed == ABSTAIN:
            return ABSTAIN
        target = self.voter.committed
        self.fired += 1
        self.screen_changed(d.end)
        return target


class CodeGate:
    """Picks from a ``CodeVepDecoder``: its commitment, from settled cycles of this screen.

    ``clock()`` gives the Unix time of "now" on the board's clock (``time.time`` live, the
    fake board's ``now`` in virtual time); a screen change is stamped with it. ``fresh``
    filters the display's cycle events before they reach the decoder; ``push`` turns a
    decision into a pick (0, 1 or 2) or ABSTAIN. ``origin`` (the first sample's Unix time,
    set by ``observe``) turns decision times into the trace strip's stream seconds.
    """

    def __init__(self, decoder, cooldown_s: float = 0.5,
                 clock: Callable[[], float] = time.time) -> None:
        if not math.isfinite(cooldown_s) or cooldown_s < 0:
            raise ValueError(f"cooldown_s must be finite and >= 0, got {cooldown_s}")
        self.decoder = decoder
        self.cooldown_s = float(cooldown_s)
        self.clock = clock
        self.cycle_s = decoder.n_samples / decoder.fs
        self.stale = 0      # decisions ignored: their cycles began before the screen settled
        self.dropped = 0    # cycle events dropped for the same reason
        self.fired = 0      # picks
        self.origin: float | None = None
        self._switch = float(clock())
        self._pending = ABSTAIN
        self._streak = 0

    @property
    def votes(self) -> int:
        return self.decoder.votes

    @property
    def switch(self) -> float:
        """Unix time at which the current screen appeared."""
        return self._switch

    @property
    def fresh_from(self) -> float:
        """Earliest cycle onset that counts: the screen change plus the settle delay."""
        return self._switch + self.cooldown_s

    @property
    def pending(self) -> int:
        """Target the current run of fresh decisions names (``raw``), or ABSTAIN."""
        return self._pending

    @property
    def streak(self) -> int:
        return self._streak

    def observe(self, t) -> None:
        """Note the first sample time seen (the stream's origin)."""
        if self.origin is None and len(t):
            self.origin = float(t[0])

    def stream_time(self, d: Decision) -> float:
        """Stream seconds of ``d``'s end, on the same clock as the trace strip's samples."""
        return d.time - (self.origin if self.origin is not None else 0.0)

    def screen_changed(self, at: float | None = None) -> None:
        """The screen changed at Unix time ``at`` (default: now): reset the decoder's cycles
        and votes (training, if still running, keeps what it has)."""
        self._switch = float(self.clock() if at is None else at)
        self.decoder.reset()
        self._pending, self._streak = ABSTAIN, 0

    def fresh(self, events: Iterable[Event]) -> list[Event]:
        """The cycle events that began on the settled current screen (others dropped)."""
        out = []
        for ev in events:
            if ev[1] != "cycle":
                continue
            if ev[0] < self.fresh_from:
                self.dropped += 1
                continue
            out.append(ev)
        return out

    def is_stale(self, d: Decision) -> bool:
        """True when ``d``'s cycle began before the current screen had settled (a decision
        made in the same batch as a pick, from the screen before it)."""
        return d.time - self.cycle_s < self.fresh_from - 1e-6

    def push(self, d: Decision) -> int:
        """Take one decision; the target that fires on it, or ABSTAIN."""
        if self.is_stale(d):
            self.stale += 1
            return ABSTAIN
        raw = int(d.raw)
        if raw == ABSTAIN:
            self._pending, self._streak = ABSTAIN, 0
        elif raw == self._pending:
            self._streak += 1
        else:
            self._pending, self._streak = raw, 1
        if not d.changed or d.committed == ABSTAIN:
            return ABSTAIN
        self.fired += 1
        target = int(d.committed)
        self.screen_changed()
        return target
