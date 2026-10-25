"""Scroll Instagram Reels in Chrome by looking at one of three coded squares (c-VEP).

    python -m brainsync.reels                                     # Cerelog X8 + Chrome extension
    python -m brainsync.reels --fake --fast --no-browser          # synthetic board, actions printed
    python -m brainsync.reels --manual                            # u/d/p on stdin, to test the extension
    python -m brainsync.reels --paradigm freq                     # the old two-square f-VEP pair

The Chrome extension under extension/ draws the c-VEP standard's three white squares
(docs/CVEP-STANDARD.md) in the right margin of instagram.com/reels, config ``reels_cvep``:
target 0 at the top, "up", goes to the previous reel; target 1 in the middle, "pause",
pauses or plays the reel showing; target 2 at the bottom, "down", goes to the next. All
three show one 63-bit m-sequence, 21 frames (350 ms) apart. This program decodes the EEG
with ``vep.live.CodeVepDecoder`` and drives the extension over a localhost WebSocket
(ws://127.0.0.1:8765, protocol in brainsync/bridge.py).

Code mode (--paradigm code, the default) trains itself: after the hello the squares
flicker with the cue box on the top square and the viewer looks at it for --train-cycles
cycles (default 30, about 32 s); the overlay counts them ("cycle 12/30") and then shows
the template's accuracy on held-out cycles against chance (33 %). From then on every
cycle (1.05 s) gives a decision: the overlay writes each square's label, a confidence bar
and its score under it, and a lead / choice line. A commit after --votes (2) agreeing
decisions acts once (scroll up, scroll down, or pause / play), --cooldown s follow, then
only the votes are reset: dwelling on up or down scrolls every 4 cycles (4.2 s), while
pause toggles once per look (brainsync/scroll.py). --template FILE reuses a template
saved with --save-template (same wearer, same headset fit) and skips training. The cycle
times are the browser's: it sends the Unix time of every cycle's first flip, which goes to
the decoder unchanged, because the board's sample times are Unix time on the same
machine's clock (see CycleFeed). --fake plays the standard's fake board (vep.live): the
fake viewer looks at the top square while training, then at down, up and pause in turn
for --switch s each, and the cycle times come from that board.

--paradigm freq keeps the two-square f-VEP pair (config ``reels``): target 0, "up"
(15 Hz), goes to the previous reel and target 1, "down" (17 Hz), to the next. The rest of
this text describes that mode where they differ; --calibrate, --calibrate-on, --classifier,
--window, --step, --threshold and --harmonics apply to it only.

Sequence: start the WebSocket server and look for the board's USB receiver. Without one
the terminal and the overlay both say "No EEG device connected" and the program waits
(--wait-device s, default 120) for it to be plugged in, then exits 1. Connect to the board
and wait up to 5 s for its first samples; the overlay's EEG line follows each step (not
connected, connecting, receiving data, no data). Then wait (--wait-browser s, default 120)
for the extension to say hello, which it does once the viewer clicked Start on its
photosensitivity warning and it measured the display refresh. A refresh more than the
config's tolerance from 60 Hz stops the run unless --ignore-refresh. Then calibrate the
rest baseline for --calibrate s (default 30). By default the squares flicker while the
viewer watches the reel (--calibrate-on watch), because "not choosing" here means watching
the video with flicker in the periphery, not a still screen; --calibrate-on still holds
the squares still instead. Then decode for --seconds (default 600). The cue box follows
the decoder's committed target. A new commit scrolls once, decisions are ignored for
--cooldown s (default 1.5), and then only the decoder's votes are reset, so dwelling on
one square scrolls about every 2.5-2.7 s (brainsync/scroll.py).

Decisions count only while the squares are in view. The overlay reports when its tab is
hidden (a background tab, another Space, a covered window, the viewer reading this
terminal) and when it is back; meanwhile decisions are still scored and logged but never
scroll, because with no flicker on screen the decoder drifts to a target (a first headset
run committed 15 times in such spells, 12 of them "down"). When the squares are back the
votes are reset, so a commit needs fresh windows. The summary says how long the squares
were out of view.

One status line per window goes to stderr, each scroll and a summary to stdout. --log
writes JSON lines (one per decision, scroll, re-arm and browser report) for tuning.
The run ends on time, Ctrl-C, Escape in the browser ("viewer quit") or the board going
silent for 5 s. SIGTERM (kill, a process manager) and SIGHUP (the terminal window closed)
count as Ctrl-C, so on every exit Python lives through the overlay is told to stop; for
the rest (SIGKILL, a crash) the overlay holds the squares still once its socket closes.
Exit code 0 for a run that ended by time, Ctrl-C or the viewer (also Escape before
calibration); 1 when no EEG device was found or it would not connect, no browser said
hello, the refresh was wrong, the port was taken, calibration failed or the board stalled;
2 for bad arguments or config; 130 when interrupted before decoding started.

--manual needs neither board nor decoder: after the hello it reads stdin lines, u
scrolls up, d down, p pauses or plays (with a pause target), q or end of input quits.
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

import numpy as np

from .board import FS, CerelogBoard, FakeBoard, detect_serial_port, serial_search_note
from .bridge import (HOST, PORT, Bridge, config_message, cue_message, decision_message,
                     eeg_message, phase_message, scroll_message, toggle_message,
                     train_message, trained_message)
from .classify import ABSTAIN, make
from .demo import CALIBRATE_S, STALL_S, Tally, _channels, _names, _status, run
from .launch import speak
from .online import TUNED, Decision, Decoder, calibrate, threshold_arg
from .recordings import MONTAGE, POSTERIOR
from .scroll import COOLDOWN_S, Action, ScrollGate
from .stimulus import load_config
from .stimulus.config import StimulusConfig
from .vep.cvep import Template
from .vep.live import (CYCLES as CODE_CYCLES, LAG_FRAMES, LENGTH, N_TARGETS, TRAIN_CYCLES,
                       VOTES as CODE_VOTES, CodeVepDecoder, confidence, confidence_line,
                       standard_fake_board)

PARADIGMS = ("code", "freq")
DEFAULT_CONFIGS = {"code": "reels_cvep", "freq": "reels"}
CODE_PAD_S = 0.5     # vep.live's default: filter pad around each cycle
FREQ_PAD_S = 2.0
FAKE_GAZE = (2, 0, 1)   # code mode --fake: down, up, pause in turn, --switch s each
CYCLE_SKEW_S = 5.0   # a browser cycle this far from the board's newest sample is dropped
CYCLE_QUIET_S = 10.0  # no cycle for this long while the squares are in view: say so
FOLLOW_S = 3.0       # another tab's cycles are taken once the followed one is quiet this long
WAIT_BROWSER_S = 120.0
WAIT_DEVICE_S = 120.0
DEVICE_POLL_S = 0.5
NO_DEVICE = "No EEG device connected. Plug in the Cerelog X8 USB receiver."
FIRST_DATA_S = 5.0   # after connecting, how long the board may take to send its first samples
ACK_GRACE_S = 1.0   # after the run, how long to wait for the browser's last scroll acks
POLL_S = 0.1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.reels", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--paradigm", choices=PARADIGMS, default="code",
                   help="code: the c-VEP standard's three squares, self-trained (default); "
                        "freq: the two-square f-VEP pair at 15 / 17 Hz")
    p.add_argument("-c", "--config", default=None,
                   help="stimulus config name or .toml path; every label must be up, down "
                        "or pause (default " + ", ".join(f"{k}: {v}" for k, v in
                                                         DEFAULT_CONFIGS.items()) + ")")
    p.add_argument("--votes", type=int, default=None,
                   help="consecutive agreeing decisions needed to commit (default: code "
                        f"{CODE_VOTES}, freq {TUNED.votes})")
    p.add_argument("--margin", type=float, default=None,
                   help="abstain unless the best target leads the runner-up by this much "
                        "(code: correlation, default 0, the standard; freq: rest spreads, "
                        f"default {TUNED.margin:g})")
    p.add_argument("--pad", type=float, default=None,
                   help="seconds filtered around each window or cycle and dropped (default: "
                        f"code {CODE_PAD_S:g}, freq {FREQ_PAD_S:g})")
    code = p.add_argument_group("c-VEP (--paradigm code)")
    code.add_argument("--train-cycles", type=int, default=TRAIN_CYCLES,
                      help="cycles recorded while looking at the top square before decoding "
                           f"(default {TRAIN_CYCLES}, about 32 s; at least 2)")
    code.add_argument("--cycles", type=int, default=CODE_CYCLES,
                      help=f"cycles averaged per decision (default {CODE_CYCLES})")
    code.add_argument("--template", type=Path, default=None,
                      help="a saved template (same wearer, same headset fit) instead of "
                           "training")
    code.add_argument("--save-template", type=Path, default=None,
                      help="save the trained template to this .npz file")
    freq = p.add_argument_group("f-VEP (--paradigm freq)")
    freq.add_argument("--classifier", default=TUNED.classifier, choices=("cca", "fbcca"),
                      help=f"default {TUNED.classifier}")
    freq.add_argument("--window", type=float, default=TUNED.window_s,
                      help=f"window seconds (default {TUNED.window_s:g})")
    freq.add_argument("--step", type=float, default=TUNED.step_s,
                      help=f"seconds between decisions (default {TUNED.step_s:g})")
    freq.add_argument("--threshold", type=threshold_arg, default=TUNED.threshold,
                      help="abstain unless the best target is this many rest spreads above "
                           f"its rest level, or 'none' (default {TUNED.threshold})")
    freq.add_argument("--harmonics", type=int, default=3,
                      help="template harmonics (default 3)")
    freq.add_argument("--calibrate", type=float, default=CALIBRATE_S,
                      help=f"seconds of rest baseline (default {CALIBRATE_S:g})")
    freq.add_argument("--calibrate-on", choices=("watch", "still"), default="watch",
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
    p.add_argument("--wait-device", type=float, default=WAIT_DEVICE_S,
                   help="seconds to wait for the board's USB receiver to be plugged in "
                        f"(default {WAIT_DEVICE_S:g})")
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
                     help="no board or decoder: stdin lines u/d scroll up/down, p pauses or "
                          "plays (code configs), q quits")
    fake = p.add_argument_group("synthetic board")
    fake.add_argument("--fake", action="store_true", help="FakeBoard instead of the Cerelog X8")
    fake.add_argument("--fast", action="store_true",
                      help="with --fake: virtual time, as fast as the decoder runs")
    fake.add_argument("--switch", type=float, default=10.0,
                      help="with --fake: seconds before the fake viewer looks at another "
                           "square (default 10); code mode looks at down, up, pause in turn")
    fake.add_argument("--seed", type=int, default=0, help="with --fake: noise seed")
    p.add_argument("--log", type=Path, default=None,
                   help="write JSON lines (decisions, scrolls, browser reports) to this file")
    p.add_argument("--no-voice", action="store_true", help="no spoken prompts")
    return p


def _resolve(args: argparse.Namespace) -> None:
    """Fill the paradigm-dependent defaults (config, votes, margin, pad)."""
    code = args.paradigm == "code"
    if args.config is None:
        args.config = DEFAULT_CONFIGS[args.paradigm]
    if args.votes is None:
        args.votes = CODE_VOTES if code else TUNED.votes
    if args.margin is None:
        args.margin = 0.0 if code else TUNED.margin
    if args.pad is None:
        args.pad = CODE_PAD_S if code else FREQ_PAD_S


def _check(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    _resolve(args)
    if args.fast and not args.fake:
        p.error("--fast needs --fake")
    if args.paradigm == "code" and args.no_browser and not args.fake and not args.manual:
        p.error("--paradigm code needs the browser: the cycle times come from its flicker "
                "(or use --fake)")
    if args.paradigm != "code" and (args.template is not None
                                    or args.save_template is not None):
        p.error("--template and --save-template are for --paradigm code")
    if args.template is not None and args.save_template is not None:
        p.error("--save-template needs a training stage; drop --template")
    if args.train_cycles < 2:
        p.error(f"--train-cycles must be >= 2 (leave-one-out needs two), got "
                f"{args.train_cycles}")
    if args.cycles < 1:
        p.error(f"--cycles must be >= 1, got {args.cycles}")
    if args.manual and (args.no_browser or args.fake):
        p.error("--manual drives the browser by hand; it takes neither --no-browser nor --fake")
    for flag, value in (("--seconds", args.seconds), ("--calibrate", args.calibrate),
                        ("--window", args.window), ("--step", args.step),
                        ("--switch", args.switch), ("--wait-browser", args.wait_browser),
                        ("--wait-device", args.wait_device)):
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
    scrolls: list[Action] = field(default_factory=list)   # every action: scrolls, toggles
    # scroll id -> ok. Every client gets every scroll, so a second Instagram tab or the
    # mock page left open answers each id too: an id counts once, ok if any client moved.
    acks: dict[int, bool] = field(default_factory=dict)
    hello: dict[str, Any] | None = None    # the first hello
    frames: dict[str, Any] | None = None   # last frames report (or the hello)
    quit: bool = False                     # the viewer pressed Escape
    stop_text: str | None = None           # overlay text for the final stop phase
    hidden_at: float | None = None         # decision time the squares went out of view
    hidden_s: float = 0.0                  # decoding seconds with the squares out of view
    hidden_spells: int = 0
    # code mode: (t, "cycle", n) of the followed tab, waiting for take_cycles()
    cycles: list[tuple[float, str, int]] = field(default_factory=list)
    cycle_client: int | None = None        # the tab whose cycles are followed
    cycle_heard: float | None = None       # time.monotonic() of its last cycle
    cycles_seen: int = 0                   # every valid cycle report, any tab
    cycles_other: int = 0                  # reports from another tab, not used
    _cycle_t: float = -math.inf            # newest cycle time taken from the followed tab

    @property
    def showing(self) -> bool:
        """True while some tab shows the squares (always without a browser)."""
        return self.bridge is None or self.bridge.showing

    def track_view(self, t: float) -> str | None:
        """Follow the overlay's view at decision time ``t``: "hidden" when the squares just
        went out of view, "back" when they just came back, else None."""
        showing = self.showing
        if not showing and self.hidden_at is None:
            self.hidden_at = t
            self.hidden_spells += 1
            print(f"{t:7.2f} s  squares out of view: decisions ignored until they are back",
                  file=sys.stderr)
            self.log.write("pause", t=round(t, 4))
            return "hidden"
        if showing and self.hidden_at is not None:
            print(f"{t:7.2f} s  squares back in view after {t - self.hidden_at:.1f} s",
                  file=sys.stderr)
            self.end_view(t)
            self.log.write("resume", t=round(t, 4))
            return "back"
        return None

    def end_view(self, t: float) -> None:
        """Close a spell out of view at decision time ``t`` (the run's end)."""
        if self.hidden_at is not None:
            self.hidden_s += max(0.0, t - self.hidden_at)
            self.hidden_at = None

    def send(self, msg: dict[str, Any]) -> None:
        if self.bridge is not None:
            self.bridge.send(msg)

    def scroll(self, action: Action) -> None:
        """Send one action: a scroll up or down, or a pause / play toggle ("pause")."""
        self.scrolls.append(action)
        if action.dir == "pause":
            self.send(toggle_message(action.id))
            print(f"{action.time:7.2f} s  play/pause #{action.id}", flush=True)
        else:
            self.send(scroll_message(action.dir, action.id))
            print(f"{action.time:7.2f} s  scroll {action.dir} #{action.id}", flush=True)
        self.log.write("scroll", t=action.time, dir=action.dir, target=action.target,
                       id=action.id)

    def _cycle(self, ev: dict[str, Any]) -> None:
        """Follow one tab's cycle reports: the first to send, until it is quiet for
        FOLLOW_S, so two open tabs never mix their frame grids."""
        self.cycles_seen += 1
        client, now = ev.get("_client"), time.monotonic()
        if client != self.cycle_client:
            quiet = self.cycle_heard is None or now - self.cycle_heard >= FOLLOW_S
            if not quiet:
                self.cycles_other += 1
                if self.cycles_other == 1:
                    print("warning: two tabs report flicker cycles; following the first",
                          file=sys.stderr)
                return
            if self.cycle_client is not None:
                print("following another tab's flicker cycles", file=sys.stderr)
            self.cycle_client = client
            self._cycle_t = -math.inf
        self.cycle_heard = now
        t = float(ev["t"])
        if t <= self._cycle_t:   # repeated or out of order: a cycle is reported once
            return
        self._cycle_t = t
        self.cycles.append((t, "cycle", int(ev["n"])))

    def take_cycles(self) -> list[tuple[float, str, int]]:
        """The followed tab's cycle events since the last call (Unix flip times)."""
        out, self.cycles = self.cycles, []
        return out

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
        what = "pause or play" if ev["type"] == "toggled" else "scroll"
        self.log.write("ack", id=sid, ok=ev.get("ok"), method=ev.get("method"),
                       **({"playing": ev.get("playing")} if ev["type"] == "toggled" else {}))
        if isinstance(sid, bool) or not isinstance(sid, int) or not 1 <= sid <= len(self.scrolls):
            print(f"browser acked scroll {sid!r}, which was never sent; ignored",
                  file=sys.stderr)
        elif sid in self.acks:   # another client answered first
            self.acks[sid] = self.acks[sid] or ok
        else:
            self.acks[sid] = ok
            if not ok:
                print(f"warning: the browser could not {what} #{sid} "
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
            elif kind in ("scrolled", "toggled"):
                self._ack(ev)
            elif kind == "cycle":
                self._cycle(ev)
            elif kind == "view":
                self.log.write("view", showing=ev.get("showing"))
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


def _start_bridge(link: Link) -> None:
    """Start the WebSocket server once (OSError when the port is taken)."""
    bridge = link.bridge
    if bridge is None or bridge.running:
        return
    bridge.start()
    print(f"WebSocket server on {bridge.url}", file=sys.stderr)


def _await_device(link: Link, args: argparse.Namespace, voice: bool) -> str:
    """The board's USB serial port, waiting up to --wait-device s for it to be plugged in.

    Without a receiver BrainFlow only fails after its own search with a driver error, so
    say plainly in the terminal and on the overlay what is missing. RuntimeError on
    timeout, ViewerQuit when the viewer pressed Escape meanwhile.
    """
    port = detect_serial_port()
    if port:
        return port
    print(f"no EEG device connected: plug in the Cerelog X8 USB receiver (looked for "
          f"{serial_search_note()}); waiting up to {args.wait_device:g} s...",
          file=sys.stderr)
    link.send(phase_message("wait", False, None, NO_DEVICE))
    link.send(eeg_message("absent", "plug in the Cerelog X8 USB receiver"))
    speak("No EEG device connected.", voice)
    deadline = time.monotonic() + args.wait_device
    while not port:
        if link.poll():
            raise ViewerQuit
        if time.monotonic() >= deadline:
            link.stop_text = ("No EEG device connected. Plug in the Cerelog X8 USB receiver "
                              "and restart BrainSync.")
            link.send(eeg_message("absent", f"none found in {args.wait_device:g} s"))
            raise RuntimeError(f"no EEG device connected after {args.wait_device:g} s")
        time.sleep(DEVICE_POLL_S)
        port = detect_serial_port()
    print(f"EEG device found on {port}", file=sys.stderr)
    return port


def _await_samples(board: Any, link: Link, port: str, timeout: float | None = None) -> None:
    """Wait for the board's first samples after connecting; RuntimeError if none come.

    A connected receiver with the headset switched off opens fine and then sends nothing,
    so "connected" is only reported as "receiving data" once samples arrive. The samples
    read here are dropped; calibration starts from the next read.
    """
    timeout = FIRST_DATA_S if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while True:
        X, _ = board.read()
        if X.shape[1]:
            break
        if time.monotonic() >= deadline:
            link.send(eeg_message("no-data", f"no samples in {timeout:g} s on {port}"))
            link.stop_text = ("The EEG board is connected but sends no data. Is the headset "
                              "switched on? Restart BrainSync to try again.")
            raise RuntimeError(f"the Cerelog X8 on {port} connected but sent no data for "
                               f"{timeout:g} s; is the headset switched on?")
        time.sleep(POLL_S)
    print(f"EEG board connected on {port}, receiving data", file=sys.stderr)
    link.send(eeg_message("streaming", f"{port}, {board.fs:g} Hz"))


def _open_cerelog(link: Link, args: argparse.Namespace, voice: bool) -> Any:
    """Find, open and check the Cerelog X8 (it is opened only once its USB receiver is
    there, see _await_device); the open board, streaming."""
    port = args.port or _await_device(link, args, voice)
    print(f"connecting to the Cerelog X8 on {port} (can take 15 s)...", file=sys.stderr)
    link.send(phase_message("wait", False, None, f"Connecting to the EEG board on {port}..."))
    link.send(eeg_message("connecting", port))
    board = CerelogBoard(serial_port=port, ch_names=args.names, verbose=args.verbose)
    try:
        board.open()
    except RuntimeError:
        link.stop_text = ("Could not connect to the EEG board. Is the headset switched "
                          "on? Restart BrainSync to try again.")
        link.send(eeg_message("error", port))
        raise
    link.send(eeg_message("connected", port))
    try:
        _await_samples(board, link, port)
    except BaseException:
        board.close()
        raise
    return board


def _await_browser(link: Link, args: argparse.Namespace, cfg: StimulusConfig) -> None:
    """Start the server, wait for the hello and check its refresh; RuntimeError on failure,
    ViewerQuit when the viewer pressed Escape first."""
    bridge = link.bridge
    assert bridge is not None
    _start_bridge(link)
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
             manual: bool = False, unit: str = "windows") -> list[str]:
    up = sum(a.dir == "up" for a in link.scrolls)
    down = sum(a.dir == "down" for a in link.scrolls)
    pauses = sum(a.dir == "pause" for a in link.scrolls)
    head = "reels:"
    if not manual:
        rate = f"{100 * tally.abstained / tally.windows:.0f}%" if tally.windows else "n/a"
        head += (f" {tally.seconds:.1f} s decoded, {tally.windows} {unit}, {rate} "
                 "abstained,")
    acts = f"{up + down} scrolls (up {up}, down {down})"
    if "pause" in labels:
        acts += f", {pauses} play/pause"
    lines = [f"{head} {acts}; ended: {ended}"]
    if link.scrolls:
        lines.append("  actions: " if "pause" in labels else "  scrolls: ")
        lines[-1] += "  ".join(f"{a.time:.1f}s->{a.dir}" for a in link.scrolls)
    if link.hidden_spells:
        lines.append(f"  squares out of view {link.hidden_s:.1f} s in {link.hidden_spells} "
                     f"spell{'s' if link.hidden_spells != 1 else ''}: decisions then were "
                     "ignored")
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

MANUAL_KEYS = {"u": "up", "up": "up", "d": "down", "down": "down", "p": "pause",
               "pause": "pause"}


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
        _start_bridge(link)
        link.send(eeg_message("unused", "manual mode"))
        _await_browser(link, args, cfg)
        link.send(phase_message("run", True, None,
                                "Manual test: scrolls come from the BrainSync terminal."))
        keys = "u (up), d (down)" + (", p (pause / play)" if "pause" in cfg.labels else "")
        print(f"manual: type {keys} or q (quit), then Enter", file=sys.stderr)
        ended = "started"
        lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=_read_lines, args=(sys.stdin, lines), daemon=True).start()
        began = time.monotonic()
        while True:
            quit_reason = link.poll()
            link.take_cycles()   # nothing decodes in manual mode
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
            if word in MANUAL_KEYS and MANUAL_KEYS[word] in cfg.labels:
                elapsed = time.monotonic() - began
                link.scroll(Action(dir=MANUAL_KEYS[word], target=-1, id=len(link.scrolls) + 1,
                                   time=elapsed))
            elif word:
                print(f"manual: ignored {word!r}; type {keys} or q", file=sys.stderr)
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


# -- c-VEP run ------------------------------------------------------------------------------

ACTION_WORDS = {"up": "for the previous reel", "pause": "to pause or play",
                "down": "for the next reel"}
ACTION_SPOKEN = {"up": "to go back", "pause": "to pause or play", "down": "for the next reel"}


def _shown(label: str) -> str:
    """A label as the overlay writes it under its square."""
    return label.upper()


def _listing(parts: list[str]) -> str:
    if len(parts) <= 2:
        return " or ".join(parts)
    return ", ".join(parts[:-1]) + ", or " + parts[-1]


def code_prompts(cfg: StimulusConfig, train_cycles: int) -> dict[str, str]:
    """Overlay texts and spoken prompts for the c-VEP training and run phases."""
    first = _shown(cfg.labels[0])
    cycle_s = cfg.code.cycle_frames / cfg.refresh if cfg.code is not None else 1.05
    return {
        "train": (f"Training: look at the {first} square and keep looking at it "
                  f"({train_cycles} cycles, about {train_cycles * cycle_s:.0f} s)."),
        "train_spoken": f"Look at the {cfg.labels[0]} square and keep looking at it.",
        "run": ("Look at " + _listing([f"{_shown(lab)} {ACTION_WORDS[lab]}"
                                       for lab in cfg.labels]) + ". Escape quits."),
        "run_spoken": ("Look at " + _listing([f"{lab} {ACTION_SPOKEN[lab]}"
                                              for lab in cfg.labels]) + "."),
    }


def _code_status(d: Decision, elapsed: float, labels: tuple[str, ...],
                 truth: int | None) -> str:
    scores = "  ".join(f"{lab} {s:+.3f}" for lab, s in zip(labels, d.scores))
    raw = "-" if d.raw == ABSTAIN else labels[d.raw]
    line = f"{elapsed:7.2f} s  {scores}  raw {raw:<5}  {confidence_line(d, labels)}"
    if truth is not None:
        line += f"  (true {'-' if truth == ABSTAIN else labels[truth]})"
    return line


def _check_standard(cfg: StimulusConfig) -> None:
    """--fake plays the standard's codes (vep.live.standard_fake_board): the config must
    show them too."""
    code = cfg.code
    if (code is None or cfg.n_targets != N_TARGETS or code.length != LENGTH
            or code.lag_frames != LAG_FRAMES or code.frames_per_bit != 1):
        raise ValueError(f"--fake plays the c-VEP standard ({N_TARGETS} targets, a "
                         f"{LENGTH}-bit code, {LAG_FRAMES}-frame lags); config {cfg.name!r} "
                         "differs")


class CycleFeed:
    """One poll of a c-VEP run: the browser's messages, a board read and the cycle events.

    The cycle times are the browser's flip times (``performance`` time mapped to Unix
    seconds in the overlay), handed to the decoder as they are: the board's samples carry
    Unix times from the same machine's clock (BrainFlow timestamps), the same assumption
    the f-VEP bridge already makes when it times scrolls. A constant offset between the
    two (the flip lands a frame after its rAF timestamp, the board's USB latency) is
    learnt into the template during training, so only jitter costs accuracy. A cycle more
    than CYCLE_SKEW_S from the board's newest sample is on another clock and dropped.
    With --fake the fake board's own cycle events are used and the browser's ignored (its
    virtual time is not the browser's).
    """

    def __init__(self, board: Any, link: Link, fake: bool, sleep: Callable[[float], None],
                 clock: Callable[[], float]) -> None:
        self.board, self.link, self.fake = board, link, fake
        self.sleep, self.clock = sleep, clock
        self.heard = self.cycle_at = clock()
        self.board_t: float | None = None   # Unix time of the newest sample
        self.n = 0                          # samples read
        self.cycles = 0                     # cycle events handed to the decoder
        self.skewed = 0                     # dropped: on another clock
        self.quiet = False                  # warned about missing cycles

    def step(self) -> tuple[np.ndarray, np.ndarray, list[tuple[float, str, int]]] | str:
        """(X, t, cycle events), or why the run must end ("viewer quit", "board stalled")."""
        why = self.link.poll()
        if why:
            return why
        self.sleep(POLL_S)
        X, t = self.board.read()
        now = self.clock()
        if X.shape[1]:
            self.heard = now
            self.board_t = float(t[-1])
            self.n += X.shape[1]
        elif now - self.heard >= STALL_S:
            return "board stalled"
        browser = self.link.take_cycles()
        events = ([e for e in self.board.read_events() if e[1] == "cycle"] if self.fake
                  else browser)
        events = self._on_clock(events)
        if events:
            self.cycle_at, self.quiet = now, False
        elif (not self.quiet and self.link.showing and self.link.bridge is not None
              and now - self.cycle_at >= CYCLE_QUIET_S):
            self.quiet = True
            print(f"warning: no flicker cycles from the browser for {CYCLE_QUIET_S:g} s while "
                  "its squares are in view; is the overlay's refresh off?", file=sys.stderr)
        self.cycles += len(events)
        return X, t, events

    def _on_clock(self, events: list[tuple[float, str, int]]) -> list[tuple[float, str, int]]:
        if self.board_t is None:
            return events
        kept = [e for e in events if abs(e[0] - self.board_t) <= CYCLE_SKEW_S]
        if len(kept) < len(events):
            if not self.skewed:
                bad = next(e for e in events if e not in kept)
                print(f"warning: a cycle time is {bad[0] - self.board_t:+.1f} s from the "
                      f"board's clock; cycles more than {CYCLE_SKEW_S:g} s off are dropped "
                      "(both must be Unix time on this machine)", file=sys.stderr)
            self.skewed += len(events) - len(kept)
        return kept


def _code_main(args: argparse.Namespace, cfg: StimulusConfig, link: Link, gate: ScrollGate,
               voice: bool) -> int:
    """The c-VEP run: train on target 0 (unless --template), then decode."""
    log = link.log
    labels = cfg.labels
    assert cfg.code is not None
    board: Any = None
    try:
        template = Template.load(args.template) if args.template is not None else None
        if args.fake:
            _check_standard(cfg)
            board = standard_fake_board(fixated=0, seed=args.seed, refresh=cfg.refresh,
                                        clock=None if args.fast else time.time)
        fs = board.fs if args.fake else FS
        dec = CodeVepDecoder(fs, n_targets=cfg.n_targets, lag_frames=cfg.code.lag_frames,
                             cycle_frames=cfg.code.cycle_frames, refresh=cfg.refresh,
                             channels=args.channels, ch_names=args.names, votes=args.votes,
                             cycles=args.cycles, margin=args.margin, pad_s=args.pad,
                             train_cycles=args.train_cycles, train_target=0,
                             template=template)
    except (ValueError, KeyError, FileNotFoundError, OSError) as exc:
        log.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    fast = args.fake and args.fast
    sleep: Callable[[float], None] = board.advance if fast else time.sleep
    clock: Callable[[], float] = (lambda: board.elapsed) if fast else time.monotonic
    channels = [dec.ch_names[i] for i in dec.rows]
    print(f"{'fake board' if args.fake else 'Cerelog X8'} at {fs:g} Hz; c-VEP, "
          f"{cfg.n_targets} targets {cfg.code.lag_frames} frames apart in a "
          f"{cfg.code.cycle_frames}-frame cycle; channels {','.join(channels)}; "
          f"{args.cycles} cycles per decision, {args.votes} votes, margin {args.margin:g}; "
          f"cooldown {args.cooldown:g} s", file=sys.stderr)
    log.write("settings", paradigm="code", lag_frames=cfg.code.lag_frames,
              cycle_frames=cfg.code.cycle_frames, votes=args.votes, cycles=args.cycles,
              margin=args.margin, pad_s=args.pad, channels=channels,
              cooldown_s=args.cooldown, train_cycles=args.train_cycles,
              template=None if args.template is None else str(args.template),
              labels=list(labels), fake=args.fake)
    prompts = code_prompts(cfg, args.train_cycles)

    tally = Tally()
    ended = "not started"
    code = 0
    feed: CycleFeed | None = None
    trained: str | None = None
    try:
        _start_bridge(link)   # first, so the overlay can show a missing device
        if not args.fake:
            board = _open_cerelog(link, args, voice)
        else:
            link.send(eeg_message("streaming", "simulated board (--fake)"))
        with board:
            if link.bridge is not None:
                _await_browser(link, args, cfg)
            feed = CycleFeed(board, link, args.fake, sleep, clock)
            link.take_cycles()   # reported before this run's flicker: they time nothing
            if args.fake:
                board.set_target(0)       # the fake viewer looks at the training square
                board.start_flicker()
            if dec.training:
                link.send(cue_message(0))
                link.send(phase_message("train", True, None, prompts["train"]))
                speak(prompts["train_spoken"], voice)
                print(f"training: look at the {labels[0]} square for {args.train_cycles} "
                      "cycles", file=sys.stderr)
                trained = _code_train(dec, feed, link)
            else:
                trained = f"loaded from {args.template}"
                print(f"template {trained}", file=sys.stderr)
                link.send(trained_message(math.nan, dec.chance, dec.template.n_cycles))
            if args.save_template is not None:
                path = dec.template.save(args.save_template)
                print(f"template saved to {path}", file=sys.stderr)
                log.write("template", path=str(path))

            link.send(cue_message(ABSTAIN))
            link.send(phase_message("run", True, None, prompts["run"]))
            speak(prompts["run_spoken"], voice)
            run_t0 = feed.board_t if feed.board_t is not None else time.time()
            start_n = feed.n
            limit = int(round(args.seconds * fs))
            ended = "started"
            stale_end = -1   # decisions scored at or before this many samples predate a reset
            decoder = dec.decoder
            assert decoder is not None

            def rearm() -> None:
                nonlocal stale_end
                decoder.voter.reset()   # votes only: the cycle buffer stays
                stale_end = decoder.n_seen
                link.send(cue_message(ABSTAIN))
                log.write("rearm")

            gate.rearm = rearm

            def on_decision(d: Decision) -> None:
                elapsed = d.time - run_t0
                truth = board.fixated if args.fake else None
                tally.add(d, elapsed, truth)
                hidden = link.hidden_at is not None
                stale = d.end <= stale_end
                print(_code_status(d, elapsed, labels, truth), file=sys.stderr, flush=True)
                link.send(decision_message(confidence(d, labels)))
                log.write("decision", t=round(elapsed, 4), time=d.time,
                          scores=[round(float(x), 5) for x in d.scores], raw=d.raw,
                          committed=d.committed, changed=d.changed,
                          lead=round(d.gap, 4) if math.isfinite(d.gap) else None,
                          truth=truth, stale=stale, hidden=hidden)
                if stale or hidden:   # neither draws a cue nor reaches the gate
                    return
                if d.changed:
                    link.send(cue_message(d.committed))
                action = gate.push(d, elapsed)
                if action is not None:
                    link.scroll(action)

            try:
                ended = "time up"
                while feed.n - start_n < limit:
                    now_s = (feed.board_t or run_t0) - run_t0
                    if args.fake:
                        board.set_target(FAKE_GAZE[int(now_s // args.switch) % len(FAKE_GAZE)])
                    view = link.track_view(now_s)
                    if view == "hidden":
                        link.send(cue_message(ABSTAIN))
                    elif view == "back":
                        # votes cast before the squares went away: start afresh
                        decoder.voter.reset()
                        stale_end = decoder.n_seen
                    step = feed.step()
                    if isinstance(step, str):
                        ended = step
                        break
                    for d in dec.feed(*step):
                        on_decision(d)
            except KeyboardInterrupt:
                ended = "interrupted"
            link.end_view(tally.seconds)
            if ended == "board stalled":
                print(f"error: the board sent nothing for {STALL_S:g} s", file=sys.stderr)
                link.stop_text = "The EEG board stopped sending data."
                link.send(eeg_message("no-data", f"nothing for {STALL_S:g} s"))
                code = 1
            link.drain_acks()
    except ViewerQuit:
        ended = "viewer quit"
    except KeyboardInterrupt:
        if ended == "not started":   # connecting, waiting for the browser or training
            print("interrupted before decoding started", file=sys.stderr)
            code = 130
        else:
            ended = "interrupted"
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
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
        lines = _summary(tally, link, labels, ended, unit="decisions")
        if trained is not None:
            lines.append(f"  template: {trained}")
        if feed is not None and link.bridge is not None:
            extra = (f", {link.cycles_other} from another tab ignored"
                     if link.cycles_other else "")
            extra += f", {feed.skewed} off the board's clock dropped" if feed.skewed else ""
            lines.append(f"  browser cycles: {link.cycles_seen} reported{extra}"
                         + (" (not used with --fake)" if args.fake else ""))
        _print_summary(lines)
    return code


def _code_train(dec: CodeVepDecoder, feed: CycleFeed, link: Link) -> str:
    """Record cycles until the template is trained; its accuracy line. ViewerQuit when the
    viewer pressed Escape, RuntimeError when the board went quiet or training failed."""
    shown = -1
    while dec.training:
        step = feed.step()
        if step == "viewer quit":
            raise ViewerQuit
        if isinstance(step, str):
            link.stop_text = "The EEG board stopped sending data during training."
            link.send(eeg_message("no-data", f"nothing for {STALL_S:g} s"))
            raise RuntimeError(f"the board sent nothing for {STALL_S:g} s during training")
        try:
            dec.feed(*step)
        except RuntimeError:
            link.stop_text = ("Training failed: the cycle times did not match the EEG. "
                              "Restart BrainSync to try again.")
            raise
        k, of = dec.progress
        if k != shown:
            shown = k
            link.send(train_message(k, of))
            if k % 5 == 0 or k == of:
                print(f"training: cycle {k}/{of}", file=sys.stderr)
    acc, chance = dec.train_accuracy, dec.chance
    line = (f"{100 * acc:.0f} % on held-out training cycles (chance {100 * chance:.0f} %)"
            if math.isfinite(acc) else "trained (no held-out accuracy)")
    print(f"template ready: {line}", file=sys.stderr)
    if math.isfinite(acc) and acc <= chance:
        print("warning: the template does no better than chance; check the headset contact "
              "and that the viewer looked at the top square", file=sys.stderr)
    link.send(trained_message(acc, chance, dec.train_cycles))
    link.log.write("trained", accuracy=round(acc, 4) if math.isfinite(acc) else None,
                   chance=round(chance, 4), cycles=dec.train_cycles)
    return line


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
    if cfg.paradigm != args.paradigm:
        print(f"error: config {cfg.name!r} is a {cfg.paradigm} config, not "
              f"--paradigm {args.paradigm} (its default config is "
              f"{DEFAULT_CONFIGS[args.paradigm]})", file=sys.stderr)
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
        bridge = None if args.no_browser else Bridge(
            args.host, args.ws_port, config_message(cfg, train_cycles=args.train_cycles))
    except ValueError as exc:
        log.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    link = Link(bridge, log, cfg=cfg, ignore_refresh=args.ignore_refresh)
    log.write("start", config=cfg.name, argv=list(sys.argv[1:] if argv is None else argv))
    if args.manual:
        return _manual(args, cfg, link)
    if args.paradigm == "code":
        return _code_main(args, cfg, link, gate, voice)

    # The Cerelog X8 is opened only once its USB receiver is there (see _await_device).
    board: Any = None
    try:
        if args.fake:
            board = FakeBoard(cfg.freqs, switch_s=args.switch, seed=args.seed,
                              clock=None if args.fast else time.time, ch_names=args.names)
        fs = board.fs if args.fake else FS
        clf = make(args.classifier, freqs=cfg.freqs, fs=fs, harmonics=args.harmonics,
                   phases=cfg.phases, margin=args.margin, threshold=args.threshold)
        decoder = Decoder(clf, fs, window_s=args.window, step_s=args.step,
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
    print(f"{'fake board' if args.fake else 'Cerelog X8'} at {fs:g} Hz; {clf!r}; "
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
        _start_bridge(link)   # first, so the overlay can show a missing device
        if not args.fake:
            board = _open_cerelog(link, args, voice)
        else:
            link.send(eeg_message("streaming", "simulated board (--fake)"))
        with board:
            if bridge is not None:
                _await_browser(link, args, cfg)
            text, prompt = _calibration_prompt(args.calibrate_on, args.calibrate)
            link.send(phase_message("calibrate", args.calibrate_on == "watch",
                                    args.calibrate, text))
            speak(prompt, voice)
            if args.fake:
                board.set_target(ABSTAIN)   # the viewer watches the video, not a square
            try:
                cal = calibrate(decoder, board, args.calibrate,
                                sleep=board.advance if fast else None)
            except ValueError:   # too little data: the board went quiet while calibrating
                link.send(eeg_message("no-data", "too little data to calibrate"))
                link.stop_text = "The EEG board stopped sending data during calibration."
                raise
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
                nonlocal stale_end
                elapsed = (d.end - began) / decoder.fs
                truth = board.active_target() if args.fake else None
                tally.add(d, elapsed, truth)
                view = link.track_view(elapsed)
                if view == "hidden":
                    link.send(cue_message(ABSTAIN))
                elif view == "back":
                    # the votes were cast with no squares on screen: start them afresh,
                    # and drop the rest of this read, scored with those votes
                    decoder.voter.reset()
                    stale_end = decoder.n_seen
                hidden = link.hidden_at is not None
                stale = d.end <= stale_end
                print(_status(d, elapsed, labels, truth), file=sys.stderr, flush=True)
                log.write("decision", t=round(elapsed, 4), time=d.time,
                          scores=[round(float(s), 5) for s in d.scores], raw=d.raw,
                          committed=d.committed, changed=d.changed, gap=round(d.gap, 4),
                          level=round(d.level, 4), truth=truth, stale=stale, hidden=hidden)
                if stale or hidden:   # neither draws a cue nor reaches the gate
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
            link.end_view(tally.seconds)
            if ended == "board stalled":
                print(f"error: the board sent nothing for {STALL_S:g} s", file=sys.stderr)
                link.stop_text = "The EEG board stopped sending data."
                link.send(eeg_message("no-data", f"nothing for {STALL_S:g} s"))
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
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
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
