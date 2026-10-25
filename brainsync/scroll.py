"""Turn decoder commits into reel actions: one action per commit, then a cooldown.

    gate = ScrollGate(("up", "down"), cooldown_s=1.5, votes=4, rearm=decoder.voter.reset)
    for d in decoder.feed(X, t):
        action = gate.push(d, elapsed)      # elapsed: seconds of data, the decision clock
        if action is not None:
            scroll(action.dir)              # "up", "down" or "pause"

Each target maps to one action: "up" (previous reel), "down" (next reel) or "pause"
(pause or play the reel showing; the c-VEP layout's middle target). The f-VEP pair has
up and down only.

Rules. A decision that newly commits a target (``changed`` and not ABSTAIN) fires one
scroll in that target's direction. Every decision for ``cooldown_s`` after that is
ignored. The first decision at or past the end of the cooldown re-arms: the gate calls
``rearm``, which should be ``decoder.voter.reset`` and nothing more. ``decoder.reset()``
would also empty the sample buffer, and refilling window + pad (1.5 + 2 s) would blind the
decoder for 3.5 s. With only the voter reset, a viewer who keeps looking at the same
square commits it again after ``votes`` more agreeing windows (4 x 0.25 s): one scroll
about every 1.5 + 1.0 = 2.5 s while they dwell. At 250 Hz the step is 62 samples
(0.248 s), so the re-arm falls on the 7th decision (1.736 s) and the period is 2.728 s
(measured on a FakeBoard locked on one target). Looking at the video abstains, and an
abstention never fires.

After a re-arm a commit counts only once ``votes`` decisions have arrived: the voter
needs that many to commit from empty, so a sooner commit was voted before the reset.
That happens when one board read yields several decisions, which the decoder scores
before the caller sees the first.

The same rules hold for the c-VEP decoder (``vep.live.CodeVepDecoder``: one decision per
1.05 s cycle, 2 votes): ``rearm`` should then reset only its votes
(``decoder.decoder.voter.reset``), and dwelling on a scroll square scrolls every 4
cycles (4.2 s at 60 Hz: the decision 2.1 s after a scroll re-arms, two more commit).
Actions in ``once`` ("pause" by default) do not repeat while the viewer
dwells: toggling pause, play, pause every few seconds is never what they want. After a
"pause" the target is held until a decision's best target (``raw``) is another one or
none. That decision re-arms (resets the votes, as at the end of a cooldown), so looking
back commits the target afresh and toggles again: looking away for one decision is
enough. With margin 0 the c-VEP decoder never abstains, so a stray decision while dwelling
can release the hold too; a --margin above 0 makes that rarer.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .classify import ABSTAIN
from .online import Decision

DIRECTIONS = ("up", "down")            # the f-VEP pair
ACTIONS = ("up", "down", "pause")      # every action a target can have
ONCE = ("pause",)                      # actions that do not repeat while dwelling
COOLDOWN_S = 1.5


@dataclass(frozen=True)
class Action:
    """One action: ``dir`` 'up', 'down' or 'pause', the ``target`` that fired it, a
    run-unique ``id`` (1, 2, ...) and the decision clock ``time`` it fired at."""

    dir: str
    target: int
    id: int
    time: float


class ScrollGate:
    """Maps committed decoder targets to actions with a cooldown and re-arm."""

    def __init__(self, directions: Sequence[str] = DIRECTIONS, cooldown_s: float = COOLDOWN_S,
                 votes: int = 1, rearm: Callable[[], None] | None = None,
                 once: Sequence[str] = ONCE) -> None:
        dirs = tuple(directions)
        if not dirs:
            raise ValueError("need at least one target direction, got none")
        bad = [d for d in dirs if d not in ACTIONS]
        if bad:
            raise ValueError(f"target actions must be up, down or pause, got "
                             f"{bad[0]!r} in {list(dirs)}")
        if len(set(dirs)) != len(dirs):
            raise ValueError(f"every target needs its own action, got {list(dirs)}")
        if not math.isfinite(cooldown_s) or cooldown_s < 0:
            raise ValueError(f"cooldown_s must be >= 0, got {cooldown_s}")
        if isinstance(votes, bool) or int(votes) != votes or votes < 1:
            raise ValueError(f"votes must be a positive integer, got {votes}")
        self.directions = dirs
        self.cooldown_s = float(cooldown_s)
        self.votes = int(votes)
        self.rearm = rearm
        self.once = frozenset(once)
        self.reset()

    def reset(self) -> None:
        """Forget the cooldown and history; ids restart at 1."""
        self.fired = 0              # scrolls fired so far (the last id)
        self.rearms = 0
        self.ignored = 0            # commits dropped by the cooldown or a stale vote
        self._fired_at: float | None = None   # set while cooling down
        self._since_rearm: int | None = None  # decisions since the last re-arm
        self._held: int | None = None         # a fired "once" target, until the gaze leaves
        self._last_t = -math.inf

    @property
    def cooling(self) -> bool:
        """True between a scroll and the re-arm that ends its cooldown."""
        return self._fired_at is not None

    def push(self, decision: Decision, t: float) -> Action | None:
        """Take one decision at decision-clock time ``t`` (seconds); the scroll it fires."""
        if not math.isfinite(t):
            raise ValueError(f"decision time must be finite, got {t}")
        if t < self._last_t:
            raise ValueError(f"decision time went backwards: {t} after {self._last_t}")
        self._last_t = t
        fires = decision.changed and decision.committed != ABSTAIN
        if fires and not 0 <= decision.committed < len(self.directions):
            raise ValueError(f"decision committed target {decision.committed} but the gate "
                             f"maps only {len(self.directions)} target(s)")
        if self._held is not None and decision.raw != self._held:
            # the gaze left the "once" target: it may fire again. Its commit still stands
            # in the voter, so looking back would never be a change: re-arm (outside a
            # cooldown, whose end re-arms anyway) and drop this decision like any re-arm.
            self._held = None
            if self._fired_at is None:
                self._since_rearm = 0
                self.rearms += 1
                self.ignored += fires
                if self.rearm is not None:
                    self.rearm()
                return None
        if self._fired_at is not None:
            if t - self._fired_at < self.cooldown_s:
                self.ignored += fires
                return None
            # cooldown over: this decision re-arms and is dropped, because the voter state
            # it carries was built up during the cooldown
            self._fired_at = None
            self._since_rearm = 0
            self.rearms += 1
            self.ignored += fires
            if self.rearm is not None:
                self.rearm()
            return None
        if self._since_rearm is not None:
            self._since_rearm += 1
            if self._since_rearm >= self.votes:
                self._since_rearm = None
            elif fires:
                self.ignored += 1
                return None
        if not fires:
            return None
        if decision.committed == self._held:
            self.ignored += 1       # still dwelling on a "once" target
            return None
        self.fired += 1
        self._fired_at = t
        if self.directions[decision.committed] in self.once:
            self._held = decision.committed
        return Action(dir=self.directions[decision.committed], target=decision.committed,
                      id=self.fired, time=t)
