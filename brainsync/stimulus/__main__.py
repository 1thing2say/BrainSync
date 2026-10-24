"""Run the SSVEP stimulus, always full screen.

    python -m brainsync.stimulus --plan                         # targets + trial order, no display
    python -m brainsync.stimulus                                # cued session, two_targets
    python -m brainsync.stimulus -c one_target_15 --no-wait     # single-target trials
    python -m brainsync.stimulus --free --feedback stdin        # live demo: cue box from a decoder
    python -m brainsync.stimulus --free --feedback stdin --hold-still   # still until the first line
    python -m brainsync.stimulus -c swipe --free --feedback stdin --cards DIR   # BrainSwipe feed

Markers go to stderr as "[marker <seconds>] cue:<k>|stim_on:<k>|stim_off:<k>", printed and
flushed right after the flip that shows the event. Once the window is up and its measured
refresh has passed the check against [display] refresh_hz (or --refresh auto adopted it),
"measured refresh: <x> Hz" goes to stderr; a refused rate exits with code 2 before that
line. Space starts, Escape quits.

With --cards DIR the free run shows DIR/*.png (sorted by name) as a profile card between
the targets, card 0 from the first frame; stdin lines "card <k>" swap it and "flash <k>"
lights target k's box (see brainsync.stimulus.feedback).
"""
from __future__ import annotations

import argparse
import dataclasses
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .config import (CARD_ASPECT, DEFAULT_CONFIG, Card, StimulusConfig, config_warnings,
                     load_config, override, session_trials)
from .display import (MARKER_CLOCKS, Aborted, MarkerWriter, card_paths, cued_frames,
                      free_frames, play, refresh_mismatch)
from .flicker import frame_luminances, frames_per_cycle, phase_pi


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
                        "the first feedback line, e.g. while a decoder calibrates")
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
    lines = [
        f"config {cfg.name}" + (f" ({cfg.path})" if cfg.path else ""),
        f"  {cfg.n_targets} target(s), {cfg.waveform} flicker, refresh {hz:g} Hz, "
        f"side {cfg.size:g} screen heights, cue held through flicker: "
        f"{'yes' if cfg.hold_cue else 'no'}",
    ]
    for t in cfg.targets:
        x, y = t.pos
        lines.append(f"  target {t.index}  label {t.label!r}  {t.freq:.3f} Hz  phase "
                     f"{phase_pi(t.phase):.2f} pi  {frames_per_cycle(t.freq, hz):.3f} "
                     f"frames/cycle  centre ({x:+.3f}, {y:+.3f})")
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
    total = round(len(trials) * (s.cue_s + s.stim_s + s.rest_s))
    lines.append(f"session: {s.blocks} block(s) x {cfg.n_targets} = {len(trials)} trial(s) of "
                 f"cue {s.cue_s:g} + stim {s.stim_s:g} + rest {s.rest_s:g} s, "
                 f"{total // 60} min {total % 60:02d} s, seed {seed}")
    width = len(str(s.blocks))
    for start in range(0, len(trials), cfg.n_targets):
        chunk = trials[start:start + cfg.n_targets]
        lines.append(f"  block {chunk[0].block + 1:>{width}}/{s.blocks}: "
                     + " ".join(map(str, (t.target for t in chunk))))
    return lines


def print_plan(cfg: StimulusConfig, seed: int, free: bool, out: TextIO | None = None,
               cards: Sequence[Path] = ()) -> None:
    """Targets, any cards and (for a cued session) the trial order, to stdout."""
    stream = sys.stdout if out is None else out
    stream.write("\n".join(_plan_lines(cfg, seed, free, cards)) + "\n")


def _configure(args: argparse.Namespace) -> StimulusConfig:
    cfg = load_config(args.config)
    hz = args.refresh if isinstance(args.refresh, float) else None
    return override(cfg, blocks=args.blocks, cue_s=args.cue_s, stim_s=args.stim_s,
                    rest_s=args.rest_s, hold_cue=args.hold_cue, refresh_hz=hz)


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
    from .feedback import FeedbackReader

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
        if not args.no_wait:
            try:
                renderer.wait_for_start(frame_luminances(cfg.freqs, cfg.phases, 0,
                                                         cfg.refresh, cfg.waveform))
            except Aborted:
                print("aborted before start", file=sys.stderr)
                return 1
        if args.free:
            reader = (FeedbackReader(sys.stdin, cfg.n_targets, n_cards=len(cards)).start()
                      if args.feedback == "stdin" else None)
            # without feedback nothing can swap the card, so card 0 stays up
            card = (None if not cards else (lambda: 0) if reader is None
                    else (lambda: reader.card))
            frames = free_frames(cfg, args.seconds,
                                 cue=None if reader is None else (lambda: reader.current),
                                 stop=None if reader is None else (lambda: reader.done),
                                 hold=None if reader is None or not args.hold_still
                                 else (lambda: not reader.heard),
                                 card=card,
                                 flash=None if reader is None else (lambda: reader.flash))
            timing = play(frames, renderer, markers, cfg.refresh, measured_refresh=measured)
            print(timing.summary(), flush=True)
            return 0
        trials = session_trials(cfg, seed)
        print(f"cued session: {len(trials)} trials, seed {seed}", file=sys.stderr)
        timing = play(cued_frames(cfg, trials), renderer, markers, cfg.refresh,
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
    if args.seconds is not None and args.seconds <= 0:
        p.error(f"--seconds must be positive, got {args.seconds}")
    try:
        cfg = _configure(args)
        cards = card_paths(args.cards) if args.cards is not None else []
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
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
