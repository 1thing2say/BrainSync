"""BrainSwipe: doomscroll an endless feed of invented dating profiles by looking at a square.

    python -m brainsync.swipe                                 # c-VEP: Cerelog X8 + full-screen stimulus
    python -m brainsync.swipe --fake --fast --no-stimulus     # synthetic board, faster than real time
    python -m brainsync.swipe --paradigm freq                 # the old two-square 15 / 17 Hz feed

c-VEP, the default (the BrainSync standard, docs/CVEP-STANDARD.md; config swipe_cvep):
three white squares around the profile at the centre of the screen, Pass (target 0, left),
Super like (target 1, above) and Like (target 2, right), each showing the standard's
63-bit m-sequence 21 frames later than the one before. The run is brainsync.demo's c-VEP
session: training first, with the cue box on Pass ("Look at the Pass square") for
--train-cycles cycles, then one decision per cycle with each square's confidence bar and
score under it (the display's {"labels": ...} lines). The centre line ({"text": ...})
shows the profile on screen (name, age, detail and one answer) above the decoder's lead
and choice. A commit (--votes agreeing decisions, default 2) swipes: the cue box lands on
that square, the next profile comes up and the swipe is spoken: "Pass", "Liked <name>"
(a match with chance --match-rate) or, for a Super like, "Super liked <name>. It's a
match!": a Super like is a Like that always matches. ``CodeSwiper`` makes every swipe
count only cycles that began --cooldown s after the card changed, and resets the
decoder's buffered cycles and votes on every card. The coded stimulus draws no profile
cards yet (its free run refuses --cards for a [code] config), so the profile is text.

f-VEP (--paradigm freq, config swipe): the stimulus draws a profile card between the
two squares. The left square (target 0, 15 Hz) is Pass and the right one (target 1,
17 Hz) is Like. Render the deck (--deck, default the bundled invented profiles) to PNG
cards in a temporary folder, then run the live demo's session (see brainsync.demo) with
the swipe config and --cards: the first profile is on the still screen while the rest
baseline is fit (keep still and read it), then the squares flicker and decoding runs for
--seconds. Two fresh windows in a row on a square put the cue box on it; a swipe flashes
that square's box, shows the next card and speaks "Pass", "Liked <name>" or a match (on
a Like, with chance --match-rate).

Both: every swipe changes the card, and holding your gaze on one square keeps swiping.
--seed shuffles the feed (and, with --fake, the noise); it defaults to a fresh random seed
on the real board and to 0 with --fake, and the deck line names it. With --fake the
board's --switch interval stands in for the user changing their mind. One status line
per decision goes to stderr, a summary to stdout. Exit codes and shutdown (Ctrl-C,
Escape, a silent board, a crashed stimulus) are the demo's.

``Swiper`` (f-VEP) turns the decoder's sliding-window decisions into swipes. Windows
overlap (1.5 s at a 0.25 s hop), so right after a swipe the decoder still scores EEG
recorded while the user looked at the previous card, and the eyes need a moment to
settle on the new one. A decision is therefore stale, and ignored, when its window
started before the card changed plus ``cooldown_s``. Fresh decisions feed the Swiper's
own ``Voter`` (from ``Decision.raw``; ``Decision.committed`` comes from the decoder's
voter, which also counted stale windows). ``votes`` consecutive fresh windows naming the
same target fire a swipe, which changes the card and starts the next fresh period.
Swipes are thus at least cooldown + window + (votes - 1) x step apart (0.5 + 1.5 + 0.75
= 2.75 s with the demo defaults), and each is decoded only from EEG recorded while its
card was settled.

``Feed`` deals the cards: an endless shuffled order, reshuffled when every card has been
shown, never the same card twice in a row, and on a Like a match with chance
``match_rate`` (always, on a Super like). Both are pure logic; main() wires up the board,
stimulus and voice.
"""
from __future__ import annotations

import argparse
import math
import secrets
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import demo
from .classify import ABSTAIN
from .launch import Stimulus, speak
from .online import Decision, Decoder, Voter
from .profiles import Profile, load_deck, render_deck
from .recordings import Event
from .stimulus import StimulusConfig
from .vep import live

__all__ = ["ABSTAIN", "PASS", "SUPER_LIKE", "LIKE", "FREQ_PASS", "FREQ_LIKE", "ACTIONS",
           "CodeSwiper", "Swiper", "Outcome", "Feed"]

# c-VEP, the standard (docs/CVEP-STANDARD.md section 5): Pass, Super like, Like
PASS = 0         # left square, the training target
SUPER_LIKE = 1   # above the card
LIKE = 2         # right square
ACTIONS = ("Pass", "Super like", "Like")
CODE_CONFIG = "swipe_cvep"
# f-VEP (--paradigm freq, config swipe): two squares
FREQ_PASS = 0    # left square, 15 Hz
FREQ_LIKE = 1    # right square, 17 Hz
FREQ_CONFIG = "swipe"
PROMPTS = ("Keep still and read the first profile.", "Look at Like or Pass.")
CODE_PROMPTS = ("Look at the {label} square and keep looking at it.",
                "Read the profile, then look at Pass, Super like or Like.")
ANSWER_CHARS = 64   # one profile answer on the centre line, cut to this
# fresh windows in a row on one target before the cue box shows it: one window alone is
# often noise, and the box moving on every stray window would pull the eyes around
CUE_STREAK = 2


class Swiper:
    """Fires a swipe once ``votes`` fresh consecutive windows name the same target.

    ``votes`` defaults to the decoder's. The card on screen when the Swiper is made
    counts as changed at ``decoder.n_seen``; call ``card_changed`` whenever the card
    changes for another reason (the Swiper calls it itself on every swipe).
    """

    def __init__(self, decoder: Decoder, votes: int | None = None, cooldown_s: float = 0.5) -> None:
        if not math.isfinite(cooldown_s) or cooldown_s < 0:
            raise ValueError(f"cooldown_s must be finite and >= 0, got {cooldown_s}")
        self.decoder = decoder
        self.cooldown_s = float(cooldown_s)
        self.cooldown = int(round(self.cooldown_s * decoder.fs))   # samples
        self.voter = Voter(decoder.votes if votes is None else votes)
        self.stale = 0     # decisions ignored because their window started too early
        self.fired = 0     # swipes
        self._switch = 0
        self._pending = ABSTAIN
        self._streak = 0
        self.card_changed()

    @property
    def votes(self) -> int:
        return self.voter.votes

    @property
    def switch(self) -> int:
        """Stream position (decoder samples) at which the current card appeared."""
        return self._switch

    @property
    def fresh_from(self) -> int:
        """Earliest window start that counts: the card change plus the settle delay."""
        return self._switch + self.cooldown

    @property
    def pending(self) -> int:
        """Target the current run of fresh windows names, or ABSTAIN."""
        return self._pending

    @property
    def streak(self) -> int:
        """Length of that run (0 while pending is ABSTAIN); a swipe fires at ``votes``."""
        return self._streak

    def card_changed(self, at: int | None = None) -> None:
        """The card changed at stream position ``at`` (default: samples seen so far)."""
        self._switch = int(self.decoder.n_seen if at is None else at)
        self.voter.reset()
        self._pending, self._streak = ABSTAIN, 0

    def is_stale(self, d: Decision) -> bool:
        """True when ``d``'s window started before the current card had settled."""
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
        # the voter is reset on every swipe, so its commitment is ABSTAIN between swipes
        # and a change can only be a target reaching ``votes`` agreeing windows
        if not self.voter.push(raw) or self.voter.committed == ABSTAIN:
            return ABSTAIN
        target = self.voter.committed
        self.fired += 1
        self.card_changed(d.end)
        return target


class CodeSwiper:
    """Turns the c-VEP decoder's commits into swipes, one per card.

    The decoder (``vep.live.CodeVepDecoder``) decides once per code cycle from the mean
    of its last cycles and commits after ``votes`` agreeing decisions. A card change
    (``card_changed``) resets it, so no cycle seen on the old card is averaged or voted
    on the new one, and every cycle that began before the change plus ``cooldown_s`` (the
    eyes leaving the old answer for a square) is dropped, counted in ``stale``. ``feed``
    takes every board read in place of ``decoder.feed``: the first committed decision
    after a change fires (``fires``), the rest of that read's decisions are dropped (EEG
    from the old card) and the card counts as changed at the read's newest sample. While
    the decoder trains, every cycle goes to it and nothing fires.
    """

    def __init__(self, decoder: live.CodeVepDecoder, cooldown_s: float = 0.5) -> None:
        if not math.isfinite(cooldown_s) or cooldown_s < 0:
            raise ValueError(f"cooldown_s must be finite and >= 0, got {cooldown_s}")
        self.decoder = decoder
        self.cooldown_s = float(cooldown_s)
        self.stale = 0     # cycles dropped because they began before the card settled
        self.fired = 0     # swipes
        self.fresh_from = -math.inf          # board time (Unix s) of the first cycle that counts
        self.last_t: float | None = None     # newest sample time fed

    @property
    def votes(self) -> int:
        return self.decoder.votes

    def card_changed(self, at: float | None = None) -> None:
        """The card changed at board time ``at`` (default: the newest sample fed)."""
        at = self.last_t if at is None else float(at)
        self.fresh_from = -math.inf if at is None else at + self.cooldown_s
        self.decoder.reset()

    @staticmethod
    def fires(d: Decision) -> int:
        """The target ``d`` swipes (it moved the commitment onto one), or ABSTAIN."""
        return int(d.committed) if d.changed and d.committed != ABSTAIN else ABSTAIN

    def feed(self, X: np.ndarray, t: np.ndarray, events: Sequence[Event] = ()) -> list[Decision]:
        """One board read; the decisions to act on (only the last one can fire)."""
        t = np.asarray(t, dtype=float).reshape(-1)
        if t.size:
            self.last_t = float(t[-1])
        cycles = [e for e in events if e[1] == "cycle"]
        if self.decoder.training:
            return self.decoder.feed(X, t, cycles)
        fresh = [e for e in cycles if e[0] >= self.fresh_from]
        self.stale += len(cycles) - len(fresh)
        out: list[Decision] = []
        for d in self.decoder.feed(X, t, fresh):
            out.append(d)
            if self.fires(d) != ABSTAIN:
                self.fired += 1
                self.card_changed()
                break
        return out


@dataclass(frozen=True)
class Outcome:
    """What happened to one swiped card (a super like is also ``liked``)."""

    card: int
    liked: bool
    match: bool
    super_like: bool = False


class Feed:
    """Endless order of ``n_cards`` card indices with made-up matches on Likes.

    Each round is a fresh permutation; when one round ends the next is reshuffled so its
    first card differs from the last one shown (unless there is only one card). Order
    and matches draw from separate generators spawned from ``seed``, so the card order
    does not depend on the user's choices. A super like is a like that always matches.
    """

    def __init__(self, n_cards: int, match_rate: float = 0.3, seed: int = 0) -> None:
        if isinstance(n_cards, bool) or int(n_cards) != n_cards or n_cards < 1:
            raise ValueError(f"n_cards must be a positive integer, got {n_cards}")
        if not 0.0 <= match_rate <= 1.0:   # also rejects NaN
            raise ValueError(f"match_rate must lie in [0, 1], got {match_rate}")
        self.n_cards = int(n_cards)
        self.match_rate = float(match_rate)
        order_seed, match_seed = np.random.SeedSequence(seed).spawn(2)
        self._order_rng = np.random.default_rng(order_seed)
        self._match_rng = np.random.default_rng(match_seed)
        self._round = self._order_rng.permutation(self.n_cards)
        self._pos = 0
        self.likes = 0         # super likes included
        self.super_likes = 0
        self.passes = 0
        self.matches = 0

    @property
    def swipes(self) -> int:
        return self.likes + self.passes

    @property
    def current(self) -> int:
        """Index of the card on screen."""
        return int(self._round[self._pos])

    def swipe(self, liked: bool, super_like: bool = False) -> Outcome:
        """Swipe the current card and move to the next; the outcome for the swiped card.
        ``super_like`` is a like that always matches."""
        card = self.current
        liked = bool(liked) or bool(super_like)
        # one draw per swipe, liked or not, keeps the match sequence tied to swipe count
        hit = bool(self._match_rng.random() < self.match_rate)
        match = bool(super_like) or (liked and hit)
        if liked:
            self.likes += 1
            self.super_likes += bool(super_like)
        else:
            self.passes += 1
        self.matches += match
        self._advance(card)
        return Outcome(card, liked, match, bool(super_like))

    def _advance(self, last: int) -> None:
        self._pos += 1
        if self._pos < self.n_cards:
            return
        self._round = self._order_rng.permutation(self.n_cards)
        self._pos = 0
        if self.n_cards > 1 and self._round[0] == last:
            # swap the repeat with a random later card so the round stays a permutation
            j = int(self._order_rng.integers(1, self.n_cards))
            self._round[[0, j]] = self._round[[j, 0]]


# -- command line -------------------------------------------------------------------------

def card_order(n_cards: int, first: int) -> list[int]:
    """Deck index drawn as each card image: deck order with ``first`` swapped into image 0.

    The stimulus shows image 0 from its first frame, before any feedback line, so the
    feed's first card must be image 0. A swap is its own inverse: the same list also maps
    a deck index to its image.
    """
    order = list(range(n_cards))
    order[0], order[first] = order[first], order[0]
    return order


def voice_line(profile: Profile, outcome: Outcome) -> str:
    """What is said for one swipe."""
    if not outcome.liked:
        return "Pass"
    if outcome.super_like:
        return f"Super liked {profile.name}. It's a match!"
    if outcome.match:
        return f"{profile.name} liked you back. It's a match!"
    return f"Liked {profile.name}"


@dataclass
class Tally:
    """What happened during a run."""

    windows: int = 0
    seconds: float = 0.0   # decoded so far, to the newest window's end
    swipes: list[tuple[float, int]] = field(default_factory=list)
    truth_swipes: int = 0
    truth_hits: int = 0

    def add(self, elapsed: float, fired: int, truth: int | None) -> None:
        self.windows += 1
        self.seconds = elapsed
        if fired == ABSTAIN:
            return
        self.swipes.append((elapsed, fired))
        if truth is not None:
            self.truth_swipes += 1
            self.truth_hits += fired == truth


def _status(d: Decision, elapsed: float, labels: tuple[str, ...], swiper: Swiper,
            stale: bool, fired: int, truth: int | None) -> str:
    def name(k: int) -> str:
        return "-" if k == ABSTAIN else labels[k]

    scores = "  ".join(f"{lab} {s:+.3f}" for lab, s in zip(labels, d.scores))
    line = (f"{elapsed:7.2f} s  {scores}  lead {d.gap:4.1f}  level {d.level:+4.1f}  "
            f"window {name(d.raw):>4}  ")
    if fired != ABSTAIN:
        line += f"swipe {name(fired)}  <- next card"
    elif stale:
        line += "stale (card settling)"
    else:
        line += f"streak {name(swiper.pending):>4} {swiper.streak}/{swiper.votes}"
    if truth is not None:
        line += f"  (true {name(truth)})"
    return line


def _summary(tally: Tally, feed: Feed, swiper: Swiper | CodeSwiper, labels: tuple[str, ...],
             ended: str) -> list[str]:
    rate = f"{60 * feed.swipes / tally.seconds:.1f}" if tally.seconds > 0 else "n/a"
    supers = f" ({feed.super_likes} super)" if len(labels) > 2 else ""
    stale = "cycles" if isinstance(swiper, CodeSwiper) else "windows"
    lines = [f"swipe: {tally.seconds:.1f} s decoded, {feed.swipes} swipes ({rate} per minute): "
             f"{feed.likes} likes{supers}, {feed.passes} passes, {feed.matches} matches; "
             f"{swiper.stale} stale {stale} ignored; ended: {ended}"]
    if tally.swipes:
        lines.append("  swipes: " + "  ".join(f"{t:.1f}s->{labels[k]}" for t, k in tally.swipes))
    if tally.truth_swipes:
        lines.append(f"  fake board: {tally.truth_hits} of {tally.truth_swipes} swipes "
                     f"({100 * tally.truth_hits / tally.truth_swipes:.0f}%) went to the "
                     f"true target")
    return lines


def card_text(profile: Profile, chars: int = ANSWER_CHARS) -> str:
    """The profile on the centre line: "Priya, 29 · Product designer · Oakland" and, on a
    second line, its first answer in quotes, cut to ``chars``."""
    head = f"{profile.name}, {profile.age}" + (f" · {profile.detail}" if profile.detail else "")
    answer = profile.prompts[0][1] if profile.prompts else ""
    if len(answer) > chars:
        answer = answer[:chars - 1].rstrip() + "\u2026"
    return head + (f"\n\u201c{answer}\u201d" if answer else "")


def _code_status(d: Decision, elapsed: float, labels: Sequence[str], fired: int,
                 truth: int | None) -> str:
    def name(k: int) -> str:
        return "-" if k == ABSTAIN else labels[k]

    scores = "  ".join(f"{lab} {s:+.3f}" for lab, s in zip(labels, d.scores))
    lead = "  -  " if not np.isfinite(d.gap) else f"{d.gap:.3f}"
    line = f"{elapsed:7.2f} s  {scores}  lead {lead}  best {name(d.raw):>10}  "
    line += (f"swipe {name(fired)}  <- next card" if fired != ABSTAIN
             else f"committed {name(d.committed)}")
    if truth is not None:
        line += f"  (true {name(truth)})"
    return line


def _play_code(args: argparse.Namespace, cfg: StimulusConfig, board: Any,
               decoder: live.CodeVepDecoder, deck: Sequence[Profile], feed: Feed) -> int:
    """One c-VEP session (brainsync.demo.code_session) with the profile as the centre text."""
    labels = cfg.labels
    voice = not args.no_voice
    swiper = CodeSwiper(decoder, cooldown_s=args.cooldown)
    tally = Tally()
    stim: Stimulus | None = None
    cue = ABSTAIN          # the box sits on the swiped square until the next decision

    def centre(status: str) -> str:
        return f"{card_text(deck[feed.current])}\n{status}"

    def on_start(started: Stimulus | None) -> bool:
        nonlocal stim
        stim = started
        swiper.card_changed()    # the eyes leave the Pass square for the profile
        return demo.send_text(stim, centre(CODE_PROMPTS[1]))

    def on_decision(d: Decision, elapsed: float) -> bool:
        nonlocal cue
        truth = board.fixated if args.fake else None
        fired = swiper.fires(d)
        tally.add(elapsed, fired, truth)
        print(_code_status(d, elapsed, labels, fired, truth), file=sys.stderr, flush=True)
        ok = demo.send_labels(stim, live.confidence_labels(d, labels))
        if fired == ABSTAIN:
            if cue != ABSTAIN:
                cue = ABSTAIN
                ok = ok and demo.send(stim, ABSTAIN)
            return ok and demo.send_text(stim, centre(live.confidence_line(d, labels)))
        outcome = feed.swipe(liked=fired != PASS, super_like=fired == SUPER_LIKE)
        said = voice_line(deck[outcome.card], outcome)
        cue = fired
        ok = ok and demo.send(stim, fired) and demo.send_text(stim, centre(said))
        speak(said, voice)
        return ok

    ended, code = demo.code_session(
        args, cfg, board, decoder, stimulus_args=demo.code_stimulus_args(args, cfg),
        on_decision=on_decision, on_start=on_start, feed=swiper.feed, labels=labels,
        prompts=CODE_PROMPTS)
    if ended != "not started":
        lines = _summary(tally, feed, swiper, labels, ended)
        if math.isfinite(decoder.train_accuracy):
            lines.insert(1, "  " + demo.trained_text(decoder.train_accuracy, decoder.chance))
        print("\n".join(lines))
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.swipe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    demo.add_live_arguments(p, config=FREQ_CONFIG, seconds=300.0, code_config=CODE_CONFIG)
    for action in p._actions:
        if action.dest == "seed":   # the demo's flag, which here also shuffles the feed
            action.default = None
            action.help = ("card order and matches, and with --fake the noise (default: "
                           "random on the real board, 0 with --fake)")
    p.add_argument("--cooldown", type=float, default=0.5,
                   help="seconds after a card change before a window (f-VEP) or a cycle "
                        "(c-VEP) may vote: the eyes settle back on a square (default 0.5)")
    p.add_argument("--deck", metavar="PATH", default=None,
                   help="profiles TOML (default: the bundled deck of invented profiles)")
    p.add_argument("--match-rate", type=float, default=0.3,
                   help="chance that a Like is a match (default 0.3; a Super like always "
                        "is)")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """The demo's checks plus the swipe flags; errors exit 2 through ``p.error``."""
    demo.check_arguments(p, args)
    if not (math.isfinite(args.cooldown) and args.cooldown >= 0):
        p.error(f"--cooldown must be >= 0, got {args.cooldown}")
    if not 0.0 <= args.match_rate <= 1.0:   # also rejects NaN
        p.error(f"--match-rate must lie in [0, 1], got {args.match_rate}")
    if args.seed is not None and args.seed < 0:   # numpy refuses negative seeds
        p.error(f"--seed must be >= 0, got {args.seed}")


def feed_seed(seed: int | None, fake: bool) -> int:
    """The seed to use: ``seed`` if given, else 0 with --fake (reproducible runs) and a
    fresh one on the real board, so each live session deals a new order."""
    if seed is not None:
        return seed
    return 0 if fake else secrets.randbelow(2**31)


def _play(args: argparse.Namespace, cfg: StimulusConfig, board: Any, decoder: Decoder,
          deck: Sequence[Profile], feed: Feed, extra: Sequence[str]) -> int:
    """One session; ``extra`` is appended to the stimulus command line (the cards)."""
    labels = cfg.labels
    voice = not args.no_voice
    order = card_order(len(deck), feed.current)
    swiper = Swiper(decoder, cooldown_s=args.cooldown)
    tally = Tally()
    stim: Stimulus | None = None
    cue = ABSTAIN   # the session's start line clears the box

    def on_flicker(started: Stimulus | None) -> bool:
        nonlocal stim
        stim = started
        swiper.card_changed()   # the eyes leave the card for a square from here on
        return True

    def on_decision(d: Decision, elapsed: float) -> bool:
        nonlocal cue
        truth = board.active_target() if args.fake else None
        stale = swiper.stale
        fired = swiper.push(d)
        tally.add(elapsed, fired, truth)
        print(_status(d, elapsed, labels, swiper, swiper.stale > stale, fired, truth),
              file=sys.stderr, flush=True)
        if fired == ABSTAIN:
            want = swiper.pending if swiper.streak >= CUE_STREAK else ABSTAIN
            if want == cue:
                return True
            cue = want
            return demo.send(stim, cue)
        # every decision from this board read was decoded before the card line below can
        # go out, so the card changes at the end of the read, not at this window's end:
        # the rest of a backlog burst is EEG from the old card and must count as stale
        swiper.card_changed()
        outcome = feed.swipe(liked=fired == FREQ_LIKE)
        ok = demo.send(stim, f"flash {fired}")
        if cue != ABSTAIN:
            cue = ABSTAIN
            ok = ok and demo.send(stim, ABSTAIN)
        ok = ok and demo.send(stim, f"card {order[feed.current]}")
        speak(voice_line(deck[outcome.card], outcome), voice)
        return ok

    ended, code = demo.session(args, cfg, board, decoder,
                               stimulus_args=demo.stimulus_args(args, extra),
                               on_flicker=on_flicker, on_decision=on_decision,
                               prompts=PROMPTS)
    if ended != "not started":
        print("\n".join(_summary(tally, feed, swiper, labels, ended)))
    return code


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    args.seed = feed_seed(args.seed, args.fake)   # before build: the fake board reads it too
    try:
        deck = load_deck(args.deck)
    except (ValueError, OSError) as exc:   # OSError: missing, a folder, unreadable
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        cfg = demo.resolve(args)
        if args.paradigm == "code":
            board, decoder = demo.build_code(args, cfg)
        else:
            cfg, board, decoder = demo.build(args)
    except demo.BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    want = 3 if args.paradigm == "code" else 2
    if cfg.n_targets != want:
        names = "Pass, Super like, Like" if want == 3 else "Pass, Like"
        print(f"error: BrainSwipe needs {want} targets ({names}); config {cfg.name!r} has "
              f"{cfg.n_targets}", file=sys.stderr)
        return 2
    print(f"deck: {len(deck)} profiles from {args.deck or 'the bundled deck'}; "
          f"cooldown {args.cooldown:g} s, match rate {args.match_rate:g}, seed {args.seed}",
          file=sys.stderr)
    feed = Feed(len(deck), match_rate=args.match_rate, seed=args.seed)
    if args.paradigm == "code":   # the coded stimulus draws no cards: the profile is text
        return _play_code(args, cfg, board, decoder, deck, feed)
    if args.no_stimulus:   # nothing shows the cards, so nothing to render (no Pillow needed)
        return _play(args, cfg, board, decoder, deck, feed, extra=())
    # the stimulus loads the PNGs as textures at startup; the folder goes after the session
    with tempfile.TemporaryDirectory(prefix="brainswipe_cards_") as tmp:
        try:
            render_deck([deck[i] for i in card_order(len(deck), feed.current)], tmp)
        except ImportError as exc:
            print(f"error: drawing the cards needs Pillow ({exc}); pip install pillow, "
                  "or run with --no-stimulus", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: cannot write the cards: {exc}", file=sys.stderr)
            return 2
        return _play(args, cfg, board, decoder, deck, feed, extra=["--cards", tmp])


if __name__ == "__main__":
    sys.exit(main())
