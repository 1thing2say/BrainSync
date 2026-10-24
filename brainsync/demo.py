"""Live SSVEP demo: look at a flickering square and the decoder puts the cue box on it.

    python -m brainsync.demo                                  # Cerelog X8 + full-screen stimulus
    python -m brainsync.demo --fake --fast --no-stimulus      # synthetic board, faster than real time

Sequence: connect to the board, start the stimulus in free-run mode with decoder feedback
on its stdin and the squares held still, ask the user to keep still and calibrate the rest
baseline on that still screen (--calibrate s, default 30: shorter baselines misjudge the
per-target rest levels by about the abstain margin), then start the flicker and decode
for --seconds. Every committed change is written to the stimulus (-1 clears the box) and
the target's label is spoken. One status line per window goes to stderr, a summary to
stdout. Ctrl-C, the stimulus exiting, its pipe closing or the board going silent for 5 s
ends the run cleanly; Ctrl-C during calibration ends the program before any decoding.
Exit code 0 for a run that ended by time, Ctrl-C or Escape; 1 when the board stalled, the
stimulus failed or exited with an error; 130 when interrupted during calibration.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .board import Board, CerelogBoard, FakeBoard
from .classify import ABSTAIN, make
from .launch import Stimulus, launch_stimulus, speak
from .online import TUNED, Decision, Decoder, calibrate, threshold_arg
from .recordings import MONTAGE, POSTERIOR
from .stimulus import load_config

STALL_S = 5.0
POLL_S = 0.1
# A 10 s baseline (about 4 independent 1.5 s windows) got the 15-vs-17 Hz rest offset wrong
# by 0.58 spreads (SD over 40 FakeBoard seeds), more than the 0.5 margin; 30 s gives ~0.33.
CALIBRATE_S = 30.0


def _names(text: str) -> list[str]:
    names = [c.strip() for c in text.split(",") if c.strip()]
    if not names:
        raise argparse.ArgumentTypeError(f"expected comma-separated names, got {text!r}")
    return names


def _channels(text: str) -> list[str | int]:
    return [int(c) if c.isdigit() else c for c in _names(text)]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.demo", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="two_targets",
                   help="stimulus config name or .toml path (default two_targets)")
    p.add_argument("--classifier", default=TUNED.classifier, choices=("cca", "fbcca"),
                   help=f"default {TUNED.classifier}")
    p.add_argument("--window", type=float, default=TUNED.window_s,
                   help=f"window seconds (default {TUNED.window_s:g})")
    p.add_argument("--step", type=float, default=TUNED.step_s,
                   help=f"seconds between decisions (default {TUNED.step_s:g})")
    p.add_argument("--votes", type=int, default=TUNED.votes,
                   help=f"consecutive agreeing windows needed to commit (default {TUNED.votes})")
    p.add_argument("--margin", type=float, default=TUNED.margin,
                   help="abstain unless the best target leads by this many rest spreads "
                        f"(default {TUNED.margin:g})")
    p.add_argument("--threshold", type=threshold_arg, default=TUNED.threshold,
                   help="abstain unless the best target is this many rest spreads above its "
                        f"rest level, or 'none' (default {TUNED.threshold})")
    p.add_argument("--pad", type=float, default=2.0,
                   help="seconds filtered before each window and dropped (default 2)")
    p.add_argument("--harmonics", type=int, default=3, help="template harmonics (default 3)")
    p.add_argument("--calibrate", type=float, default=CALIBRATE_S,
                   help=f"still-screen seconds for the rest baseline (default {CALIBRATE_S:g})")
    p.add_argument("--seconds", type=float, default=120.0,
                   help="decoding run length in seconds (default 120)")
    p.add_argument("--names", type=_names, default=list(MONTAGE),
                   help="comma-separated names of the 8 board channels, in order")
    p.add_argument("--channels", type=_channels, default=list(POSTERIOR),
                   help="analysis channels, names or indices (default " + ",".join(POSTERIOR) + ")")
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
    fake = p.add_argument_group("synthetic board")
    fake.add_argument("--fake", action="store_true", help="FakeBoard instead of the Cerelog X8")
    fake.add_argument("--fast", action="store_true",
                      help="with --fake: virtual time, as fast as the decoder runs")
    fake.add_argument("--switch", type=float, default=10.0,
                      help="with --fake: seconds before the attended target changes (default 10)")
    fake.add_argument("--seed", type=int, default=0, help="with --fake: noise seed")
    p.add_argument("--no-stimulus", action="store_true", help="do not start the stimulus window")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="let the stimulus run when its measured refresh is not 60 Hz "
                        "(it refuses by default)")
    p.add_argument("--no-voice", action="store_true", help="no spoken prompts")
    return p


@dataclass
class Tally:
    """What happened during a run."""

    windows: int = 0
    abstained: int = 0
    commits: list[tuple[float, int]] = field(default_factory=list)
    truth_windows: int = 0
    truth_hits: int = 0
    seconds: float = 0.0   # decoded so far, to the newest window's end

    def add(self, d: Decision, elapsed: float, truth: int | None) -> None:
        self.windows += 1
        self.seconds = elapsed
        self.abstained += d.raw == ABSTAIN
        if d.changed:
            self.commits.append((elapsed, d.committed))
        if truth is not None:
            self.truth_windows += 1
            self.truth_hits += d.committed == truth


def run(decoder: Decoder, board: Board, seconds: float,
        on_decision: Callable[[Decision], bool], sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic, stop: Callable[[], str | None] | None = None,
        poll_s: float = POLL_S, stall_s: float = STALL_S) -> str:
    """Decode until ``seconds`` of data have been fed; returns why the run ended.

    ``on_decision`` returning False ends the run ("feedback failed"); ``stop()``
    returning a reason ends it with that reason; no samples for ``stall_s`` by ``clock``
    ends it with "board stalled".
    """
    limit = decoder.n_seen + int(round(seconds * decoder.fs))
    heard = clock()
    while decoder.n_seen < limit:
        reason = stop() if stop is not None else None
        if reason:
            return reason
        sleep(poll_s)
        X, t = board.read()
        now = clock()
        if X.shape[1]:
            heard = now
        elif now - heard >= stall_s:
            return "board stalled"
        for d in decoder.feed(X, t):
            if not on_decision(d):
                return "feedback failed"
    return "time up"


def _status(d: Decision, elapsed: float, labels: tuple[str, ...], truth: int | None) -> str:
    def name(k: int) -> str:
        return "-" if k == ABSTAIN else labels[k]

    scores = "  ".join(f"{lab} {s:+.3f}" for lab, s in zip(labels, d.scores))
    line = (f"{elapsed:7.2f} s  {scores}  lead {d.gap:4.1f}  level {d.level:+4.1f}  "
            f"window {name(d.raw):>3}  "
            f"committed {name(d.committed):>3}")
    if d.changed:
        line += "  <- new"
    if truth is not None:
        line += f"  (true {name(truth)})"
    return line


def _summary(tally: Tally, seconds: float, labels: tuple[str, ...], ended: str) -> list[str]:
    counts = Counter(k for _, k in tally.commits)
    per = ", ".join(f"{'none' if k == ABSTAIN else labels[k]}: {counts[k]}" for k in sorted(counts))
    rate = f"{100 * tally.abstained / tally.windows:.0f}%" if tally.windows else "n/a"
    lines = [f"demo: {seconds:.1f} s decoded, {tally.windows} windows, {rate} abstained, "
             f"{len(tally.commits)} commit changes" + (f" ({per})" if per else "")
             + f"; ended: {ended}"]
    if tally.commits:
        lines.append("  commits: " + "  ".join(
            f"{t:.1f}s->{'none' if k == ABSTAIN else labels[k]}" for t, k in tally.commits))
    if tally.truth_windows:
        lines.append(f"  fake board: the committed target was the true one on "
                     f"{100 * tally.truth_hits / tally.truth_windows:.0f}% of "
                     f"{tally.truth_windows} windows")
    return lines


def _stimulus_args(args: argparse.Namespace) -> list[str]:
    out = ["-c", str(args.config), "--free", "--seconds", f"{args.seconds:g}",
           "--markers", "none", "--feedback", "stdin", "--hold-still", "--no-wait"]
    if args.ignore_refresh:
        out.append("--ignore-refresh")
    return out


def _send(stim: Stimulus | None, target: int) -> bool:
    """Write one feedback line; False when the pipe is gone."""
    if stim is None:
        return True
    pipe: Any = stim.proc.stdin
    try:
        pipe.write(f"{target}\n")
        pipe.flush()
    except (BrokenPipeError, OSError, ValueError):
        return False
    return True


def _close(stim: Stimulus | None) -> int | None:
    """End the stimulus (EOF on its stdin ends a free run); its exit code."""
    if stim is None:
        return None
    try:
        if stim.proc.stdin is not None:
            stim.proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    return stim.finish(timeout=5.0)


def _check(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.fast and not args.fake:
        p.error("--fast needs --fake")
    for flag, value in (("--seconds", args.seconds), ("--calibrate", args.calibrate),
                        ("--window", args.window), ("--step", args.step),
                        ("--switch", args.switch)):
        if not value > 0:
            p.error(f"{flag} must be positive, got {value}")
    if args.votes < 1:
        p.error(f"--votes must be >= 1, got {args.votes}")
    if args.margin < 0 or args.pad < 0:
        p.error(f"--margin and --pad must be >= 0, got {args.margin}, {args.pad}")
    if len(args.names) != len(MONTAGE):
        p.error(f"--names needs {len(MONTAGE)} names, got {len(args.names)}: {args.names}")


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    _check(p, args)
    voice = not args.no_voice
    try:
        cfg = load_config(args.config)
        if args.fake:
            board: Any = FakeBoard(cfg.freqs, switch_s=args.switch, seed=args.seed,
                                   clock=None if args.fast else time.time, ch_names=args.names)
        else:
            board = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
        clf = make(args.classifier, freqs=cfg.freqs, fs=board.fs, harmonics=args.harmonics,
                   phases=cfg.phases, margin=args.margin, threshold=args.threshold)
        decoder = Decoder(clf, board.fs, window_s=args.window, step_s=args.step,
                          votes=args.votes, channels=args.channels, pad_s=args.pad,
                          ch_names=args.names)
    except (ValueError, KeyError, FileNotFoundError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    labels = cfg.labels
    fast = args.fake and args.fast
    sleep: Callable[[float], None] = board.advance if fast else time.sleep
    clock: Callable[[], float] = (lambda: board.elapsed) if fast else time.monotonic
    print(f"{'fake board' if args.fake else 'Cerelog X8'} at {board.fs:g} Hz; {clf!r}; "
          f"channels {','.join(decoder.channel_names)}; window {args.window:g} s, step "
          f"{args.step:g} s, {args.votes} votes", file=sys.stderr)
    if args.calibrate < CALIBRATE_S:
        print(f"warning: a {args.calibrate:g} s baseline is noisy; rest decisions may lean "
              f"towards one target (default {CALIBRATE_S:g} s)", file=sys.stderr)

    stim: Stimulus | None = None
    tally = Tally()
    ended = "not started"
    code = 0
    try:
        if not args.fake:
            print("connecting to the Cerelog X8 (can take 15 s)...", file=sys.stderr)
        with board:
            if not args.no_stimulus:
                # the squares stand still until the first feedback line, so the baseline
                # is fit on the same still screen the replay tuning used
                print("starting the stimulus...", file=sys.stderr)
                stim = launch_stimulus(_stimulus_args(args), stdin=subprocess.PIPE)
            speak("Keep still and look at the middle of the screen.", voice)
            if args.fake:
                board.set_target(ABSTAIN)
            cal = calibrate(decoder, board, args.calibrate,
                            sleep=board.advance if fast else None)
            if args.fake:
                board.set_target(None)
            print(f"baseline from {cal.n_windows} still windows ({cal.n_rejected} rejected): "
                  + " ".join(f"{lab} {m:.4f}" for lab, m in zip(labels, cal.mean))
                  + f", spread {cal.spread:.4f}", file=sys.stderr)

            def stimulus_gone() -> str | None:
                if stim is not None and stim.proc.poll() is not None:
                    return f"stimulus exited (code {stim.proc.returncode})"
                return None

            # the first feedback line starts the flicker
            gone = stimulus_gone() or (None if _send(stim, ABSTAIN) else "stimulus pipe closed")
            if gone:
                ended = f"{gone} before the flicker started"
            else:
                speak("Look at one of the squares.", voice)
                board.read()  # drop what arrived during the prompt
                began = decoder.n_seen
                ended = "started"

                def on_decision(d: Decision) -> bool:
                    elapsed = (d.end - began) / decoder.fs
                    truth = board.active_target() if args.fake else None
                    tally.add(d, elapsed, truth)
                    print(_status(d, elapsed, labels, truth), file=sys.stderr, flush=True)
                    if not d.changed:
                        return True
                    if d.committed != ABSTAIN:
                        speak(labels[d.committed], voice)
                    return _send(stim, d.committed)

                try:
                    ended = run(decoder, board, args.seconds, on_decision, sleep=sleep,
                                clock=clock, stop=stimulus_gone)
                except KeyboardInterrupt:
                    ended = "interrupted"
                if ended == "feedback failed":
                    ended = "stimulus pipe closed"
                if ended == "board stalled":
                    print(f"error: the board sent nothing for {STALL_S:g} s", file=sys.stderr)
                    code = 1
    except KeyboardInterrupt:
        if ended == "not started":   # connecting, launching or calibrating: nothing decoded
            print("interrupted before decoding started", file=sys.stderr)
            code = 130
        else:
            ended = "interrupted"
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        ended = "error"
        code = 1
    finally:
        stim_code = _close(stim)
        if stim is not None and stim_code not in (0, None):
            print(f"error: the stimulus exited with code {stim_code}; see {stim.log}",
                  file=sys.stderr)
            code = code or 1
    if ended != "not started":
        print("\n".join(_summary(tally, tally.seconds, labels, ended)))
    return code


if __name__ == "__main__":
    sys.exit(main())
