"""Run the SSVEP stimulus, always full screen.

    python -m brainsync.stimulus --plan                         # targets + trial order, no display
    python -m brainsync.stimulus                                # cued session, two_targets
    python -m brainsync.stimulus -c one_target_15 --no-wait     # single-target trials
    python -m brainsync.stimulus --free --feedback stdin        # live demo: cue box from a decoder
    python -m brainsync.stimulus --free --feedback stdin --hold-still   # still until the first line
    python -m brainsync.stimulus -c four_diamond --free --feedback stdin --hold-still --markers unix
                                                                # page mode, driven by the bridge
    python -m brainsync.stimulus -c swipe --free --feedback stdin --cards DIR   # BrainSwipe feed
    python -m brainsync.stimulus -c cvep_2 --free --feedback stdin --labels   # labelled squares
    python -m brainsync.stimulus --conditions square:1,sine:1,sine:0.5 --rate   # flicker A/B

Markers go to stderr as "[marker <seconds>] cue:<k>|stim_on:<k>|stim_off:<k>" (a [code] or
[time] config adds cycle:<n> on each m-sequence cycle's first frame and flash:<k> on each
flash onset), printed and flushed right after the flip that shows the event. Once the window is up and its measured
refresh has passed the check against [display] refresh_hz (or --refresh auto adopted it),
"measured refresh: <x> Hz" goes to stderr; a refused rate exits with code 2 before that
line. Space starts, Escape quits.

With --cards DIR the free run shows DIR/*.png (sorted by name) as a profile card between
the targets, card 0 from the first frame; stdin lines "card <k>" swap it and "flash <k>"
lights target k's box (see brainsync.stimulus.feedback).

A bridge drives the free run with JSON lines on stdin next to the integer ones
(brainsync.stimulus.feedback): {"labels": [...]} puts a sentence beside each square,
{"text": "..."} a line at the centre, {"active": [0, 3]} picks the targets that flicker
and {"flicker": true} / {"flicker": false} turns their flicker on and off. Once a JSON line
has arrived only {"flicker": true} starts the flicker, so with --hold-still the targets
stand still until then. "[marker <unix seconds>] flicker_on" is printed right after the
flip of the first flickering frame, "[marker <unix seconds>] flicker_off" after the first
still frame. Without --seconds the free run lasts until EOF on stdin or Escape. With
--feedback stdin the lines are read from the start screen on: they apply at once (the pipe
never fills while nobody presses space), and EOF there ends the run with code 0.

--conditions mixes flicker conditions in one cued session (see brainsync.ab, which runs
it with the headset): each block cues every (condition, target) pair once, the still
screen is mid-grey for all of them, each trial's first marker is "cond:<j>" (j indexes the
list, printed as "conditions: ..." before the first trial), and --rate asks for a 1-5
comfort rating after each rest ("rate:<n>"). --status stdin shows the latest
{"text": "..."} line under the targets between flickers, and holds the end screen until
space, Escape or EOF.

In a free run, --labels writes each target's config label beside its square (signage for
the audience) until a {"labels": [...]} line replaces them. The labels and the centre line
stay up on flicker frames unless [stimulus] labels_during_flicker = false: the timing
summary's dropped-frame count says whether the machine keeps up.

A [code] (c-VEP) or [time] (t-VEP) config runs its m-sequence or flash schedule in place of
the frequency flicker, in a cued session and in the free run alike; in the free run each
{"flicker": true} restarts it from frame 0.
"""
from __future__ import annotations

import argparse
import dataclasses
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .config import (CARD_ASPECT, DEFAULT_CONFIG, Card, StimulusConfig, Trial,
                     config_warnings, load_config, override, parse_conditions, session_trials)
from ..vep.codes import flash_rate_hz, min_lag_distance
from .coded import coded_cued_frames, coded_free_frames, sequence_for
from .display import (MARKER_CLOCKS, Aborted, MarkerWriter, card_paths, cued_frames,
                      free_frames, play, refresh_mismatch, still_luminances)
from .flicker import frames_per_cycle, phase_pi


def _refresh_arg(text: str) -> str | float:
    if text == "auto":
        return text
    try:
        hz = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected 'auto' or a rate in Hz, got {text!r}") from None
    if hz <= 0:
        raise argparse.ArgumentTypeError(f"refresh must be positive, got {text!r}")
    return hz


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.stimulus", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                   help=f"shipped config name or path to a .toml file (default {DEFAULT_CONFIG})")
    p.add_argument("--plan", action="store_true",
                   help="dry run: list targets and the trial order on stdout, open nothing")
    p.add_argument("--free", action="store_true",
                   help="no trials: every target flickers until Escape, --seconds or EOF")
    p.add_argument("--seconds", type=float, help="free run length in seconds")
    p.add_argument("--feedback", default="none", choices=("none", "stdin"),
                   help="free run cue source: 'stdin' reads a target index per line "
                        "(-1 = no box); EOF stops the run")
    p.add_argument("--hold-still", action="store_true",
                   help="with --feedback stdin: show the targets still (no flicker) until "
                        "the first {\"flicker\": true} line (from a decoder that sends only "
                        "integer lines: its first one), e.g. while a decoder calibrates")
    p.add_argument("--labels", action="store_true",
                   help="free run: write each target's config label beside its square, "
                        "until a {\"labels\": [...]} line on the feedback stdin replaces them")
    p.add_argument("--cards", metavar="DIR",
                   help="free run: show DIR/*.png (sorted by name) as a profile card between "
                        "the targets; 'card <k>' and 'flash <k>' lines on stdin drive it")
    p.add_argument("--markers", choices=sorted(MARKER_CLOCKS), default="unix",
                   help="clock stamped on stderr event markers, or 'none' to print none "
                        "(default unix)")
    session = p.add_argument_group("cued session timing (replaces the config's [session])")
    session.add_argument("--blocks", type=int, help="blocks; each cues every target once")
    session.add_argument("--cue-s", type=float, help="seconds the cue box shows before flicker")
    session.add_argument("--stim-s", type=float, help="seconds of flicker per trial")
    session.add_argument("--rest-s", type=float, help="still seconds after each trial")
    session.add_argument("--hold-cue", action=argparse.BooleanOptionalAction, default=None,
                         help="leave the cue box up while the cued target flickers "
                              "(default: as the config says)")
    p.add_argument("--depth", type=float,
                   help="flicker modulation depth in (0, 1]: luminance swings 0.5 +/- depth/2, "
                        "1 is full black-white (default: as the config says)")
    p.add_argument("--conditions", metavar="SPECS",
                   help="cued session: mix flicker conditions per trial, comma-separated "
                        "<waveform>:<depth>, e.g. square:1,sine:1,sine:0.5")
    p.add_argument("--rate", action="store_true",
                   help="cued session: ask for a 1-5 comfort rating (keys 1-5) after each "
                        "trial's rest, marked rate:<n>")
    p.add_argument("--status", default="none", choices=("none", "stdin"),
                   help="cued session: 'stdin' shows the latest {\"text\": ...} JSON line "
                        "under the targets between flickers, and waits at the end until "
                        "space, Escape or EOF")
    p.add_argument("--seed", type=int, help="seed for the shuffled order (random if omitted)")
    p.add_argument("--no-wait", action="store_true", help="skip the press-space start screen")
    p.add_argument("--refresh", type=_refresh_arg,
                   help="display rate in Hz, or 'auto' to adopt the measured rate "
                        "(default: [display] refresh_hz)")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="warn instead of exiting when the measured rate disagrees")
    return p


def _plan_lines(cfg: StimulusConfig, seed: int, free: bool,
                cards: Sequence[Path] = ()) -> list[str]:
    hz = cfg.refresh
    flicker = (f"conditions {', '.join(c.spec for c in cfg.conditions)} mixed per trial"
               if cfg.conditions else "c-VEP m-sequence" if cfg.code is not None
               else "t-VEP flashes" if cfg.time is not None else f"{cfg.waveform} flicker")
    lines = [
        f"config {cfg.name}" + (f" ({cfg.path})" if cfg.path else ""),
        f"  {cfg.n_targets} target(s), {flicker}, refresh {hz:g} Hz, "
        f"side {cfg.size:g} screen heights, cue held through flicker: "
        f"{'yes' if cfg.hold_cue else 'no'}"
        + ("" if cfg.conditions else f", flicker depth {cfg.depth:g}")
        + (f", fixation marks {cfg.fixation_size:g} wide" if cfg.fixation else ""),
    ]
    if cfg.code is not None:
        c = cfg.code
        apart = min_lag_distance(cfg.n_targets, c.lag_frames, c.cycle_frames)
        lines.append(f"  code: {c.length}-bit m-sequence, {c.frames_per_bit} frame(s) per "
                     f"bit, cycle {c.cycle_frames} frames = {c.cycle_frames / hz:.3f} s, "
                     f"lag {c.lag_frames} frames = {1000 * c.lag_frames / hz:.0f} ms per "
                     f"target, nearest two targets {apart} frames apart")
    if cfg.time is not None:
        t = cfg.time
        rate = flash_rate_hz(cfg.n_targets, t.flash_frames, t.gap_frames, hz)
        lines.append(f"  time: {t.flash_frames}-frame flashes ({1000 * t.flash_frames / hz:.0f} "
                     f"ms) in {t.slot_frames}-frame slots, {t.order} order, each target "
                     f"{rate:.2f} flashes/s")
    for t in cfg.targets:
        x, y = t.pos
        if cfg.code is not None:
            lag = t.index * cfg.code.lag_frames
            how = f"lag {lag} frames = {1000 * lag / hz:.0f} ms"
        elif cfg.time is not None:
            how = "flashes in turn"
        else:
            how = (f"{t.freq:.3f} Hz  phase {phase_pi(t.phase):.2f} pi  "
                   f"{frames_per_cycle(t.freq, hz):.3f} frames/cycle")
        lines.append(f"  target {t.index}  label {t.label!r}  {how}  centre "
                     f"({x:+.3f}, {y:+.3f})")
    if cards:
        c = cfg.card_spec
        flash = f"flash {c.flash_s:g} s" + (" in the cue colour" if not c.flash_colors else "")
        lines.append(f"cards: {len(cards)} card(s) from {cards[0].parent}, centred, "
                     f"{c.height:g} screen heights high ({c.height * CARD_ASPECT:.3f} wide "
                     f"at 2:3), {flash}")
    if free:
        lines.append("free run: every target flickers until Escape, --seconds or EOF on "
                     "--feedback stdin")
        return lines
    s = cfg.session
    trials = session_trials(cfg, seed)
    per_block = cfg.n_targets * max(1, len(cfg.conditions))
    total = round(len(trials) * (s.cue_s + s.stim_s + s.rest_s))
    pairs = f"{cfg.n_targets} x {len(cfg.conditions)} condition(s)" if cfg.conditions \
        else f"{cfg.n_targets}"
    lines.append(f"session: {s.blocks} block(s) x {pairs} = {len(trials)} trial(s) of "
                 f"cue {s.cue_s:g} + stim {s.stim_s:g} + rest {s.rest_s:g} s, "
                 f"{total // 60} min {total % 60:02d} s, seed {seed}")
    width = len(str(s.blocks))

    def name(t: Trial) -> str:
        return f"{t.target}/{cfg.conditions[t.condition].spec}" if cfg.conditions \
            else str(t.target)

    for start in range(0, len(trials), per_block):
        chunk = trials[start:start + per_block]
        lines.append(f"  block {chunk[0].block + 1:>{width}}/{s.blocks}: "
                     + " ".join(map(name, chunk)))
    return lines


def print_plan(cfg: StimulusConfig, seed: int, free: bool, out: TextIO | None = None,
               cards: Sequence[Path] = ()) -> None:
    """Targets, any cards and (for a cued session) the trial order, to stdout."""
    stream = sys.stdout if out is None else out
    stream.write("\n".join(_plan_lines(cfg, seed, free, cards)) + "\n")


def _configure(args: argparse.Namespace) -> StimulusConfig:
    cfg = load_config(args.config)
    hz = args.refresh if isinstance(args.refresh, float) else None
    conditions = parse_conditions(args.conditions) if args.conditions is not None else None
    return override(cfg, blocks=args.blocks, cue_s=args.cue_s, stim_s=args.stim_s,
                    rest_s=args.rest_s, hold_cue=args.hold_cue, depth=args.depth,
                    conditions=conditions, refresh_hz=hz)


def _check_refresh(cfg: StimulusConfig, args: argparse.Namespace,
                   measured: float) -> StimulusConfig | None:
    """The config to run with, or None when the refresh mismatch is fatal."""
    if args.refresh == "auto":
        adopted = override(cfg, refresh_hz=float(round(measured)))
        print(f"using refresh {adopted.refresh:g} Hz", file=sys.stderr)
        return adopted
    problem = refresh_mismatch(measured, cfg.refresh, cfg.display.refresh_tolerance_hz)
    if problem is None:
        return cfg
    if args.ignore_refresh:
        print(f"warning: {problem}; continuing (--ignore-refresh)", file=sys.stderr)
        return cfg
    print(f"error: {problem}", file=sys.stderr)
    return None


def _run_display(cfg: StimulusConfig, args: argparse.Namespace, seed: int,
                 cards: Sequence[Path] = ()) -> int:
    from .display import PsychoPyRenderer
    from .feedback import FeedbackReader, StatusReader, initial_control

    renderer = PsychoPyRenderer(cfg, cards=cards) if cards else PsychoPyRenderer(cfg)
    try:
        if cards:
            print(f"loaded {len(cards)} card(s) from {cards[0].parent}", file=sys.stderr,
                  flush=True)
        measured = renderer.measure_refresh()
        checked = _check_refresh(cfg, args, measured)
        if checked is None:
            return 2
        cfg = checked
        # launchers wait for this line, so it comes only after the refresh check passed
        print(f"measured refresh: {measured:.2f} Hz", file=sys.stderr, flush=True)
        markers = MarkerWriter(args.markers)
        # The free run's reader starts before the start screen, so a driver's lines never fill
        # the stdin pipe while it waits for space (the screen stays still meanwhile), and a
        # driver that closes stdin ends the start screen too instead of leaving the window up.
        reader = (FeedbackReader(sys.stdin, cfg.n_targets, n_cards=len(cards),
                                 labels=cfg.labels if args.labels else ()).start()
                  if args.free and args.feedback == "stdin" else None)
        if not args.no_wait:
            try:
                if reader is None:
                    renderer.wait_for_start(still_luminances(cfg))
                else:
                    renderer.wait_for_start(still_luminances(cfg), stop=lambda: reader.done)
            except Aborted:
                print("aborted before start", file=sys.stderr)
                return 1
            if reader is not None and reader.done:
                print("stdin closed before start", file=sys.stderr)
                return 0
        if args.free:
            # without feedback nothing can swap the card, so card 0 stays up
            card = (None if not cards else (lambda: 0) if reader is None
                    else (lambda: reader.card))
            # --labels without a feedback stream: the config's labels, never replaced
            start = initial_control(cfg.n_targets, cfg.labels)
            control = (None if reader is None and not args.labels
                       else (lambda: start) if reader is None else (lambda: reader.control))
            cue = None if reader is None else (lambda: reader.current)
            stop = None if reader is None else (lambda: reader.done)
            hold = (None if reader is None or not args.hold_still
                    else (lambda: not reader.heard))
            if cfg.coded:
                frames = coded_free_frames(cfg, sequence_for(cfg, seed), args.seconds,
                                           cue=cue, stop=stop, hold=hold, control=control)
            else:
                frames = free_frames(cfg, args.seconds, cue=cue, stop=stop, hold=hold,
                                     card=card,
                                     flash=None if reader is None else (lambda: reader.flash),
                                     control=control)
            timing = play(frames, renderer, markers, cfg.refresh, measured_refresh=measured)
            print(timing.summary(), flush=True)
            return 0
        trials = session_trials(cfg, seed)
        print(f"cued session: {len(trials)} trials, seed {seed}", file=sys.stderr)
        if cfg.conditions:
            print("conditions: " + " ".join(c.spec for c in cfg.conditions), file=sys.stderr,
                  flush=True)
        status = StatusReader(sys.stdin).start() if args.status == "stdin" else None
        finish = (None if status is None
                  else (lambda: status.done or renderer.close_pressed()))
        if cfg.coded:
            frames = coded_cued_frames(
                cfg, trials, sequence_for(cfg, seed),
                status=None if status is None else (lambda: status.text), finish=finish)
        else:
            frames = cued_frames(
                cfg, trials,
                status=None if status is None else (lambda: status.text),
                rate=renderer.rating if args.rate else None,
                finish=finish)
        timing = play(frames, renderer, markers, cfg.refresh,
                      trial_targets={t.index: t.target for t in trials},
                      measured_refresh=measured)
        print(timing.summary(), flush=True)
        return 1 if timing.aborted else 0
    finally:
        renderer.close()


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not args.free and (args.seconds is not None or args.feedback != "none"):
        p.error("--seconds and --feedback need --free")
    if args.hold_still and args.feedback != "stdin":
        p.error("--hold-still needs --free --feedback stdin")
    if args.cards is not None and not args.free:
        p.error("--cards needs --free")
    if args.labels and not args.free:
        p.error("--labels needs --free")
    if args.seconds is not None and args.seconds <= 0:
        p.error(f"--seconds must be positive, got {args.seconds}")
    if args.free and (args.conditions is not None or args.rate or args.status != "none"):
        p.error("--conditions, --rate and --status are for a cued session, not --free")
    try:
        cfg = _configure(args)
        cards = card_paths(args.cards) if args.cards is not None else []
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if cfg.coded and (cards or cfg.conditions or args.rate):
        print(f"error: --cards, --conditions and --rate are for a flicker config, not the "
              f"[{cfg.paradigm}] config {cfg.name!r}", file=sys.stderr)
        return 2
    if cards and cfg.card is None:
        # cards in use: default [card] settings, so the overlap check below covers them
        cfg = dataclasses.replace(cfg, card=Card())
    for w in config_warnings(cfg):
        print(f"warning: {w}", file=sys.stderr)
    seed = args.seed if args.seed is not None else secrets.randbelow(2**31)
    if args.plan:
        print_plan(cfg, seed, args.free, cards=cards)
        return 0
    try:
        return _run_display(cfg, args, seed, cards)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
