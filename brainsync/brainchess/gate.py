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
"""
from __future__ import annotations

import math

from ..classify import ABSTAIN
from ..online import Decision, Decoder, Voter

__all__ = ["ABSTAIN", "MoveGate"]


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
