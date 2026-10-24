"""Scroll Instagram Reels in Chrome by looking at one of two flickering squares.

    python -m brainsync.reels                                     # Cerelog X8 + Chrome extension
    python -m brainsync.reels --fake --fast --no-browser          # synthetic board, scrolls printed
    python -m brainsync.reels --manual                            # u/d on stdin, to test the extension

The Chrome extension under extension/ draws two squares over instagram.com/reels: target
0, "up" (15 Hz), goes to the previous reel and target 1, "down" (17 Hz), to the next
(config ``reels``). This program decodes the EEG and drives the extension over a
localhost WebSocket (ws://127.0.0.1:8765, protocol in brainsync/bridge.py).

Sequence: connect to the board, start the WebSocket server and wait (--wait-browser s,
default 120) for the extension to say hello, which it does once the viewer clicked Start
on its photosensitivity warning and it measured the display refresh. A refresh more than
the config's tolerance from 60 Hz stops the run unless --ignore-refresh. Then calibrate
the rest baseline for --calibrate s (default 30). By default the squares flicker while the
viewer watches the reel (--calibrate-on watch), because "not choosing" here means watching
the video with flicker in the periphery, not a still screen; --calibrate-on still holds
the squares still instead. Then decode for --seconds (default 600). The cue box follows
the decoder's committed target. A new commit scrolls once, decisions are ignored for
--cooldown s (default 1.5), and then only the decoder's votes are reset, so dwelling on
one square scrolls about every 2.5-2.7 s (brainsync/scroll.py).

One status line per window goes to stderr, each scroll and a summary to stdout. --log
writes JSON lines (one per decision, scroll, re-arm and browser report) for tuning.
The run ends on time, Ctrl-C, Escape in the browser ("viewer quit") or the board going
silent for 5 s. SIGTERM (kill, a process manager) and SIGHUP (the terminal window closed)
count as Ctrl-C, so on every exit Python lives through the overlay is told to stop; for
the rest (SIGKILL, a crash) the overlay holds the squares still once its socket closes.
Exit code 0 for a run that ended by time, Ctrl-C or the viewer (also Escape before
calibration); 1 when no browser said hello, the refresh was wrong, the port was taken,
calibration failed or the board stalled; 2 for bad arguments or config; 130 when
interrupted before decoding started.

--manual needs neither board nor decoder: after the hello it reads stdin lines, u
scrolls up, d down, q or end of input quits.
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from .board import CerelogBoard, FakeBoard
from .bridge import (HOST, PORT, Bridge, config_message, cue_message, phase_message,
                     scroll_message)
from .classify import ABSTAIN, make
from .demo import CALIBRATE_S, STALL_S, Tally, _channels, _names, _status, run
from .launch import speak
from .online import TUNED, Decision, Decoder, calibrate, threshold_arg
from .recordings import MONTAGE, POSTERIOR
from .scroll import COOLDOWN_S, Action, ScrollGate
from .stimulus import load_config
from .stimulus.config import StimulusConfig

WAIT_BROWSER_S = 120.0
ACK_GRACE_S = 1.0   # after the run, how long to wait for the browser's last scroll acks
POLL_S = 0.1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.reels", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="reels",
                   help="stimulus config name or .toml path; labels must be up/down "
                        "(default reels)")
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
                   help=f"seconds of rest baseline (default {CALIBRATE_S:g})")
    p.add_argument("--calibrate-on", choices=("watch", "still"), default="watch",
                   help="baseline while watching the reel with the squares flickering "
                        "(watch, default) or with them held still (still)")
    p.add_argument("--cooldown", type=float, default=COOLDOWN_S,
                   help=f"seconds after a scroll in which decisions are ignored "
                        f"(default {COOLDOWN_S:g})")
    p.add_argument("--seconds", type=float, default=600.0,
                   help="decoding run length in seconds (default 600)")
    p.add_argument("--names", type=_names, default=list(MONTAGE),
                   help="comma-separated names of the 8 board channels, in order")
    p.add_argument("--channels", type=_channels, default=list(POSTERIOR),
                   help="analysis channels, names or indices (default " + ",".join(POSTERIOR) + ")")
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
    web = p.add_argument_group("browser")
    web.add_argument("--host", default=HOST, help=f"WebSocket host (default {HOST})")
    web.add_argument("--ws-port", type=int, default=PORT,
                     help=f"WebSocket port (default {PORT})")
    web.add_argument("--wait-browser", type=float, default=WAIT_BROWSER_S,
                     help="seconds to wait for the extension's hello "
                          f"(default {WAIT_BROWSER_S:g})")
    web.add_argument("--ignore-refresh", action="store_true",
                     help="run even when the browser's measured refresh is not the config's "
                          "60 Hz (refused by default)")
    web.add_argument("--no-browser", action="store_true",
                     help="no WebSocket server: scrolls are only printed")
    web.add_argument("--manual", action="store_true",
                     help="no board or decoder: stdin lines u/d scroll up/down, q quits")
    fake = p.add_argument_group("synthetic board")
    fake.add_argument("--fake", action="store_true", help="FakeBoard instead of the Cerelog X8")
    fake.add_argument("--fast", action="store_true",
                      help="with --fake: virtual time, as fast as the decoder runs")
    fake.add_argument("--switch", type=float, default=10.0,
                      help="with --fake: seconds before the attended target changes (default 10)")
    fake.add_argument("--seed", type=int, default=0, help="with --fake: noise seed")
    p.add_argument("--log", type=Path, default=None,
                   help="write JSON lines (decisions, scrolls, browser reports) to this file")
    p.add_argument("--no-voice", action="store_true", help="no spoken prompts")
    return p


def _check(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.fast and not args.fake:
        p.error("--fast needs --fake")
    if args.manual and (args.no_browser or args.fake):
        p.error("--manual drives the browser by hand; it takes neither --no-browser nor --fake")
    for flag, value in (("--seconds", args.seconds), ("--calibrate", args.calibrate),
                        ("--window", args.window), ("--step", args.step),
                        ("--switch", args.switch), ("--wait-browser", args.wait_browser)):
        if not value > 0:
            p.error(f"{flag} must be positive, got {value}")
    if not (math.isfinite(args.cooldown) and args.cooldown >= 0):
        p.error(f"--cooldown must be >= 0, got {args.cooldown}")
    if args.votes < 1:
        p.error(f"--votes must be >= 1, got {args.votes}")
    if args.margin < 0 or args.pad < 0:
        p.error(f"--margin and --pad must be >= 0, got {args.margin}, {args.pad}")
    if len(args.names) != len(MONTAGE):
        p.error(f"--names needs {len(MONTAGE)} names, got {len(args.names)}: {args.names}")
    if not 0 <= args.ws_port <= 65535:
        p.error(f"--ws-port must be 0..65535, got {args.ws_port}")


# -- the browser side of a run --------------------------------------------------------------

class JsonLog:
    """JSON lines, one object per event with its "kind"; a no-op without a path."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._file: IO[str] | None = (path.open("w", encoding="utf-8")
                                      if path is not None else None)

    def write(self, kind: str, **fields: Any) -> None:
        if self._file is None:
            return
        self._file.write(json.dumps({"kind": kind, "wall": round(time.time(), 3), **fields})
                         + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


class ViewerQuit(Exception):
    """The viewer pressed Escape before calibration started."""


@dataclass
class Link:
    """The overlay as the run sees it: the bridge (None with --no-browser) and its reports."""

    bridge: Bridge | None
    log: JsonLog
    cfg: StimulusConfig | None = None      # to check the refresh of every later hello
    ignore_refresh: bool = False
    scrolls: list[Action] = field(default_factory=list)
    # scroll id -> ok. Every client gets every scroll, so a second Instagram tab or the
    # mock page left open answers each id too: an id counts once, ok if any client moved.
    acks: dict[int, bool] = field(default_factory=dict)
    hello: dict[str, Any] | None = None    # the first hello
    frames: dict[str, Any] | None = None   # last frames report (or the hello)
    quit: bool = False                     # the viewer pressed Escape
    stop_text: str | None = None           # overlay text for the final stop phase

    def send(self, msg: dict[str, Any]) -> None:
        if self.bridge is not None:
            self.bridge.send(msg)

    def scroll(self, action: Action) -> None:
        self.scrolls.append(action)
        self.send(scroll_message(action.dir, action.id))
        print(f"{action.time:7.2f} s  scroll {action.dir} #{action.id}", flush=True)
        self.log.write("scroll", t=action.time, dir=action.dir, target=action.target,
                       id=action.id)

    @property
    def acks_ok(self) -> int:
        return sum(self.acks.values())

    @property
    def acks_failed(self) -> int:
        return len(self.acks) - self.acks_ok

    @property
    def unanswered(self) -> int:
        return len(self.scrolls) - len(self.acks)

    def _ack(self, ev: dict[str, Any]) -> None:
        sid, ok = ev.get("id"), ev.get("ok") is True
        self.log.write("ack", id=sid, ok=ev.get("ok"), method=ev.get("method"))
        if isinstance(sid, bool) or not isinstance(sid, int) or not 1 <= sid <= len(self.scrolls):
            print(f"browser acked scroll {sid!r}, which was never sent; ignored",
                  file=sys.stderr)
        elif sid in self.acks:   # another client answered first
            self.acks[sid] = self.acks[sid] or ok
        else:
            self.acks[sid] = ok
            if not ok:
                print(f"warning: the browser could not scroll #{sid} "
                      f"(method {ev.get('method')!r})", file=sys.stderr)

    def poll(self) -> str | None:
        """Handle what the browser sent; "viewer quit" once it said bye."""
        if self.bridge is None:
            return None
        for ev in self.bridge.events():
            kind = ev["type"]
            if kind == "bye":
                self.quit = True
                self.log.write("bye")
            elif kind == "scrolled":
                self._ack(ev)
            elif kind == "frames":
                self.frames = ev
                self.log.write("frames", refresh_hz=ev.get("refresh_hz"),
                               dropped=ev.get("dropped"))
            elif kind == "hello":
                if self.hello is None:
                    self.hello = ev
                else:   # a reloaded tab, or a second agent
                    print(f"browser said hello again ({ev.get('agent')}, refresh "
                          f"{_hz(ev.get('refresh_hz'))})", file=sys.stderr)
                    # The run goes on: the overlay itself holds its squares still while
                    # its refresh is off, and the first connection may still be fine.
                    problem = refresh_problem(ev, self.cfg) if self.cfg is not None else None
                    if problem:
                        print(f"warning: the new connection cannot flicker: {problem}"
                              + (" (ignored)" if self.ignore_refresh else ""), file=sys.stderr)
                self.frames = self.frames or ev
                self.log.write("hello", hello=ev)
        return "viewer quit" if self.quit else None

    def drain_acks(self, grace_s: float | None = None) -> None:
        """Wait up to ``grace_s`` (default ACK_GRACE_S) for acks of scrolls still in flight."""
        if self.bridge is None:
            return
        end = time.monotonic() + (ACK_GRACE_S if grace_s is None else grace_s)
        self.poll()
        while self.unanswered > 0 and self.bridge.n_clients and time.monotonic() < end:
            time.sleep(0.02)
            self.poll()


def _hz(value: Any) -> str:
    return f"{value:.2f} Hz" if _is_number(value) else "unknown"


def _is_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def refresh_problem(hello: dict[str, Any], cfg: StimulusConfig) -> str | None:
    """Why the browser's measured refresh rules the run out, or None when it is fine."""
    hz = hello.get("refresh_hz")
    want, tol = cfg.display.refresh_hz, cfg.display.refresh_tolerance_hz
    if not _is_number(hz):
        return f"the browser did not report a refresh rate (got {hz!r})"
    if abs(hz - want) > tol:
        return (f"the browser measured a {hz:.2f} Hz refresh but config {cfg.name!r} needs "
                f"{want:g} +/- {tol:g} Hz, so the squares would not flicker at their "
                "frequencies; set System Settings > Displays > Refresh rate to 60 Hz "
                "(ProMotion off), or pass --ignore-refresh")
    return None


def _await_browser(link: Link, args: argparse.Namespace, cfg: StimulusConfig) -> None:
    """Start the server, wait for the hello and check its refresh; RuntimeError on failure,
    ViewerQuit when the viewer pressed Escape first."""
    bridge = link.bridge
    assert bridge is not None
    bridge.start()
    print(f"WebSocket server on {bridge.url}", file=sys.stderr)
    link.send(phase_message("wait", False, None,
                            "Click Start on the warning to connect to BrainSync."))
    print(f"waiting up to {args.wait_browser:g} s for the browser: open "
          "https://www.instagram.com/reels/ in Chrome with the BrainSync extension and "
          "click Start...", file=sys.stderr)
    deadline = time.monotonic() + args.wait_browser
    while True:
        # A bye can come before the hello (Escape within the ~2 s refresh measurement after
        # Start) or with it; either way the viewer is gone and calibration must not start.
        if link.poll():
            raise ViewerQuit
        if link.hello is not None:
            break
        left = deadline - time.monotonic()
        if left <= 0:
            link.stop_text = "No browser connected in time."
            raise RuntimeError(f"no browser said hello within {args.wait_browser:g} s; is "
                               f"the extension loaded and pointed at {bridge.url}?")
        bridge.wait_hello(min(POLL_S, left))   # returns as soon as a hello is queued
    hello = link.hello
    print(f"browser connected: {hello.get('agent', '?')} at {hello.get('url', '?')}, "
          f"refresh {_hz(hello.get('refresh_hz'))}, {hello.get('dropped', '?')} dropped "
          f"frames", file=sys.stderr)
    problem = refresh_problem(hello, cfg)
    if problem and args.ignore_refresh:
        print(f"warning: {problem} (ignored)", file=sys.stderr)
    elif problem:
        link.stop_text = (f"Display refresh is {_hz(hello.get('refresh_hz'))}, not "
                          f"{cfg.display.refresh_hz:g} Hz. Set the display to 60 Hz "
                          "(ProMotion off) and restart.")
        raise RuntimeError(problem)


def _print_summary(lines: list[str]) -> None:
    try:
        print("\n".join(lines))
    except OSError:   # SIGHUP: the terminal is gone; the overlay was already told to stop
        pass


def _interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def _stop_on_signals() -> Callable[[], None]:
    """Make SIGTERM and SIGHUP raise KeyboardInterrupt, as Ctrl-C does; returns the undo.

    Python's default action for both ends the process without unwinding, so the finally
    block that tells the overlay to stop never ran and the squares went on flickering.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None   # signal.signal works only there (the tests run main on threads)
    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous[sig] = signal.signal(sig, _interrupt)

    def restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)

    return restore


def _finish(link: Link, ended: str) -> None:
    """Tell the overlay to stop and close the server."""
    if link.bridge is None:
        return
    link.send(phase_message("stop", False, None,
                            link.stop_text or f"BrainSync stopped: {ended}."))
    link.bridge.stop()


# -- summary --------------------------------------------------------------------------------

def _summary(tally: Tally, link: Link, labels: tuple[str, ...], ended: str,
             manual: bool = False) -> list[str]:
    up = sum(a.dir == "up" for a in link.scrolls)
    down = len(link.scrolls) - up
    head = "reels:"
    if not manual:
        rate = f"{100 * tally.abstained / tally.windows:.0f}%" if tally.windows else "n/a"
        head += (f" {tally.seconds:.1f} s decoded, {tally.windows} windows, {rate} "
                 "abstained,")
    lines = [f"{head} {len(link.scrolls)} scrolls (up {up}, down {down}); ended: {ended}"]
    if link.scrolls:
        lines.append("  scrolls: " + "  ".join(f"{a.time:.1f}s->{a.dir}" for a in link.scrolls))
    if link.bridge is not None:
        lines.append(f"  browser acks: {link.acks_ok} ok, {link.acks_failed} failed, "
                     f"{link.unanswered} unanswered")
        if link.frames is not None:
            lines.append(f"  browser frames: refresh {_hz(link.frames.get('refresh_hz'))}, "
                         f"{link.frames.get('dropped', '?')} dropped (last report)")
    if tally.truth_windows:
        lines.append(f"  fake board: the committed target was the true one on "
                     f"{100 * tally.truth_hits / tally.truth_windows:.0f}% of "
                     f"{tally.truth_windows} windows")
    return lines


# -- manual mode ----------------------------------------------------------------------------

MANUAL_KEYS = {"u": "up", "up": "up", "d": "down", "down": "down"}


def _read_lines(stream: IO[str], out: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            out.put(line)
    finally:
        out.put(None)


def _manual(args: argparse.Namespace, cfg: StimulusConfig, link: Link) -> int:
    ended = "not started"
    code = 0
    try:
        _await_browser(link, args, cfg)
        link.send(phase_message("run", True, None,
                                "Manual test: scrolls come from the BrainSync terminal."))
        print("manual: type u (up), d (down) or q (quit), then Enter", file=sys.stderr)
        ended = "started"
        lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=_read_lines, args=(sys.stdin, lines), daemon=True).start()
        began = time.monotonic()
        while True:
            quit_reason = link.poll()
            if quit_reason:
                ended = quit_reason
                break
            try:
                line = lines.get(timeout=POLL_S)
            except queue.Empty:
                continue
            if line is None:
                ended = "end of input"
                break
            word = line.strip().lower()
            if word in ("q", "quit"):
                ended = "quit"
                break
            if word in MANUAL_KEYS:
                elapsed = time.monotonic() - began
                link.scroll(Action(dir=MANUAL_KEYS[word], target=-1, id=len(link.scrolls) + 1,
                                   time=elapsed))
            elif word:
                print(f"manual: ignored {word!r}; type u, d or q", file=sys.stderr)
        link.drain_acks()
    except ViewerQuit:
        ended = "viewer quit"
    except KeyboardInterrupt:
        if ended == "not started":
            print("interrupted before the browser connected", file=sys.stderr)
            code = 130
        else:
            ended = "interrupted"
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 1
    finally:
        _finish(link, ended)
        link.log.write("end", ended=ended, scrolls=len(link.scrolls))
        link.log.close()
    if ended != "not started":
        _print_summary(_summary(Tally(), link, cfg.labels, ended, manual=True))
    return code


# -- decoding run ---------------------------------------------------------------------------

def _calibration_prompt(on: str, seconds: float) -> tuple[str, str]:
    """(overlay text, spoken prompt) for the baseline."""
    if on == "watch":
        return (f"Calibrating for {seconds:g} s: watch the reel and keep still. Do not "
                "look at the squares.",
                "Watch the reel and keep still. Do not look at the squares.")
    return (f"Calibrating for {seconds:g} s: keep still and watch the reel. The squares "
            "stay still.", "Keep still and look at the middle of the screen.")


def main(argv: list[str] | None = None) -> int:
    restore = _stop_on_signals()
    try:
        return _main(argv)
    finally:
        restore()


def _main(argv: list[str] | None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    _check(p, args)
    voice = not args.no_voice
    try:
        cfg = load_config(args.config)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        gate = ScrollGate(cfg.labels, cooldown_s=args.cooldown, votes=args.votes)
    except ValueError as exc:
        print(f"error: config {cfg.name!r}: {exc}", file=sys.stderr)
        return 2
    if not args.no_browser:
        try:
            import websockets  # noqa: F401
        except ImportError:
            print('error: the browser bridge needs websockets: pip install -e ".[reels]" '
                  "(or pass --no-browser)", file=sys.stderr)
            return 2
    try:
        log = JsonLog(args.log)
    except OSError as exc:
        print(f"error: cannot write --log: {exc}", file=sys.stderr)
        return 2
    try:
        bridge = None if args.no_browser else Bridge(args.host, args.ws_port, config_message(cfg))
    except ValueError as exc:
        log.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    link = Link(bridge, log, cfg=cfg, ignore_refresh=args.ignore_refresh)
    log.write("start", config=cfg.name, argv=list(sys.argv[1:] if argv is None else argv))
    if args.manual:
        return _manual(args, cfg, link)

    try:
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
        log.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    labels = cfg.labels
    fast = args.fake and args.fast
    sleep: Callable[[float], None] = board.advance if fast else time.sleep
    clock: Callable[[], float] = (lambda: board.elapsed) if fast else time.monotonic
    print(f"{'fake board' if args.fake else 'Cerelog X8'} at {board.fs:g} Hz; {clf!r}; "
          f"channels {','.join(decoder.channel_names)}; window {args.window:g} s, step "
          f"{args.step:g} s, {args.votes} votes; cooldown {args.cooldown:g} s", file=sys.stderr)
    if args.calibrate < CALIBRATE_S:
        print(f"warning: a {args.calibrate:g} s baseline is noisy; rest decisions may lean "
              f"towards one target (default {CALIBRATE_S:g} s)", file=sys.stderr)
    log.write("settings", classifier=args.classifier, window_s=args.window, step_s=args.step,
              votes=args.votes, margin=args.margin, threshold=args.threshold, pad_s=args.pad,
              harmonics=args.harmonics, channels=list(decoder.channel_names),
              cooldown_s=args.cooldown, calibrate_s=args.calibrate,
              calibrate_on=args.calibrate_on, labels=list(labels),
              freqs=cfg.freqs.tolist(), fake=args.fake)

    tally = Tally()
    ended = "not started"
    code = 0
    try:
        if not args.fake:
            print("connecting to the Cerelog X8 (can take 15 s)...", file=sys.stderr)
        with board:
            if bridge is not None:
                _await_browser(link, args, cfg)
            text, prompt = _calibration_prompt(args.calibrate_on, args.calibrate)
            link.send(phase_message("calibrate", args.calibrate_on == "watch",
                                    args.calibrate, text))
            speak(prompt, voice)
            if args.fake:
                board.set_target(ABSTAIN)   # the viewer watches the video, not a square
            cal = calibrate(decoder, board, args.calibrate,
                            sleep=board.advance if fast else None)
            if args.fake:
                board.set_target(None)
            print(f"baseline from {cal.n_windows} windows ({cal.n_rejected} rejected, "
                  f"squares {'flickering' if args.calibrate_on == 'watch' else 'still'}): "
                  + " ".join(f"{lab} {m:.4f}" for lab, m in zip(labels, cal.mean))
                  + f", spread {cal.spread:.4f}", file=sys.stderr)
            log.write("calibration", n_windows=cal.n_windows, n_rejected=cal.n_rejected,
                      mean=cal.mean.tolist(), spread=cal.spread, on=args.calibrate_on)

            link.send(phase_message("run", True, None,
                                    "Look at the top square for the previous reel, the "
                                    "bottom square for the next. Escape quits."))
            speak("Look at the top square to go back, the bottom square for the next reel.",
                  voice)
            board.read()  # drop what arrived during the prompt
            began = decoder.n_seen
            ended = "started"

            stale_end = -1   # decisions that end at or before this sample predate the re-arm

            def rearm() -> None:
                nonlocal stale_end
                # votes only: decoder.reset() would also empty the buffer (3.5 s blind)
                decoder.voter.reset()
                # Decoder.feed scores a whole board read before the first on_decision, so
                # the rest of this read's decisions carry the votes just thrown away.
                stale_end = decoder.n_seen
                link.send(cue_message(ABSTAIN))
                log.write("rearm")

            gate.rearm = rearm

            def on_decision(d: Decision) -> bool:
                elapsed = (d.end - began) / decoder.fs
                truth = board.active_target() if args.fake else None
                tally.add(d, elapsed, truth)
                stale = d.end <= stale_end
                print(_status(d, elapsed, labels, truth), file=sys.stderr, flush=True)
                log.write("decision", t=round(elapsed, 4), time=d.time,
                          scores=[round(float(s), 5) for s in d.scores], raw=d.raw,
                          committed=d.committed, changed=d.changed, gap=round(d.gap, 4),
                          level=round(d.level, 4), truth=truth, stale=stale)
                if stale:   # neither redraws the cue the re-arm cleared nor reaches the gate
                    return True
                if d.changed:
                    link.send(cue_message(d.committed))
                action = gate.push(d, elapsed)
                if action is not None:
                    link.scroll(action)
                return True

            try:
                ended = run(decoder, board, args.seconds, on_decision, sleep=sleep,
                            clock=clock, stop=link.poll, poll_s=POLL_S)
            except KeyboardInterrupt:
                ended = "interrupted"
            if ended == "board stalled":
                print(f"error: the board sent nothing for {STALL_S:g} s", file=sys.stderr)
                link.stop_text = "The EEG board stopped sending data."
                code = 1
            link.drain_acks()
    except ViewerQuit:
        ended = "viewer quit"
    except KeyboardInterrupt:
        if ended == "not started":   # connecting, waiting for the browser or calibrating
            print("interrupted before decoding started", file=sys.stderr)
            code = 130
        else:
            ended = "interrupted"
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        ended = "error"
        code = 1
    finally:
        _finish(link, ended)
        log.write("end", ended=ended, seconds=tally.seconds, windows=tally.windows,
                  scrolls=len(link.scrolls), acks_ok=link.acks_ok,
                  acks_failed=link.acks_failed)
        log.close()
    if ended != "not started":
        _print_summary(_summary(tally, link, labels, ended))
    return code


if __name__ == "__main__":
    sys.exit(main())
