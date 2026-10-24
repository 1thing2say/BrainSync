"""Live scalp map of your own EEG in the browser.

    python -m brainsync.brainmap                          # fake board, http://127.0.0.1:8140/
    python -m brainsync.brainmap --open                   # same, and open the page
    python -m brainsync.brainmap --board cerelog          # the Cerelog X8 (port auto-detected)
    python -m brainsync.brainmap --board replay --recording run_cued.npz --speed 2 --loop
    python -m brainsync.brainmap --host 0.0.0.0           # headless Jetson, viewed over the LAN
    python -m brainsync.brainmap --host 0.0.0.0 --allow-host jetson.local

Every 0.25 s the last 2 s of EEG become one frame: band power per electrode (z-scored
against the first --baseline-s seconds, which should be calm rest with the eyes open),
15/17 Hz SSVEP SNR and contact quality. The page draws them on a head; it is a map of 8
scalp electrodes, not an image of the brain. The fake board stays quiet through the
baseline, then puts 10, 15 and 17 Hz tones on the posterior channels in turn (10 s each),
so posterior alpha lights up first and the SSVEP views after. A replay plays a *_cued.npz
or *_trials.npz recording at --speed (at most 16) and stops at its end unless --loop.

Only one process can hold the board, so close any other BrainSync tool using the headset
first. --host other than 127.0.0.1 lets anyone on that network watch the stream. The page
then opens by IP address (http://192.168.1.20:8140/) or by a name given with --allow-host;
other names are refused, which keeps a web page from rebinding its domain to the stream.

Exit status: 0 done, 1 failed, 2 bad arguments, 130 interrupted (Ctrl-C).
"""
from __future__ import annotations

import argparse
import collections
import errno
import math
import signal
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .server import DEFAULT_HOST, DEFAULT_PORT, PAGE_DIR, MapServer, port_in_use

BOARDS = ("fake", "cerelog", "replay")
FAKE_FREQS = (10.0, 15.0, 17.0)   # alpha-ish, then the two SSVEP targets
FAKE_SWITCH_S = 10.0
FAKE_AMP_UV = 5.0                 # over the 8 µV noise: posterior alpha z well above 2
MAX_SPEED = 16.0                  # 64 frames/s; faster outruns a page and its client queue
FRAMES_PER_POLL = 2               # newest frames published per board poll, older ones dropped
END_GRACE_S = 2.0                 # keep serving after "ended"/"error" so pages show it


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.brainmap", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--board", choices=BOARDS, default="fake",
                   help="EEG source (default fake: simulated, no headset needed)")
    p.add_argument("--recording", type=Path, default=None, metavar="PATH",
                   help="the .npz recording to play (needed for --board replay)")
    p.add_argument("--speed", type=float, default=None,
                   help=f"replay speed, 1 = real time (default 1, at most {MAX_SPEED:g})")
    p.add_argument("--loop", action="store_true", help="replay: start over at the end")
    p.add_argument("--serial-port", default=None, metavar="PORT",
                   help="cerelog: serial device (default: auto-detect a USB serial port)")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"address to listen on (default {DEFAULT_HOST}: this machine only)")
    p.add_argument("--allow-host", action="append", default=None, metavar="NAME",
                   help="also accept this Host name, e.g. jetson.local (repeatable; IP "
                        "addresses are always accepted when --host exposes the server)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"port (default {DEFAULT_PORT}; 0 picks a free one)")
    p.add_argument("--baseline-s", type=float, default=30.0,
                   help="seconds of rest the z-scores are measured against (default 30)")
    p.add_argument("--seconds", type=float, default=None,
                   help="stop after this many seconds (default: run until Ctrl-C)")
    p.add_argument("--open", action="store_true", help="open the page in the web browser")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject bad values through ``p.error`` (exit code 2)."""
    if args.board == "replay" and args.recording is None:
        p.error("--board replay needs --recording PATH")
    if args.board != "replay":
        for flag, given in (("--recording", args.recording is not None),
                            ("--speed", args.speed is not None), ("--loop", args.loop)):
            if given:
                p.error(f"{flag} only applies to --board replay")
    if args.serial_port is not None and args.board != "cerelog":
        p.error("--serial-port only applies to --board cerelog")
    if args.speed is not None and not (math.isfinite(args.speed) and 0 < args.speed <= MAX_SPEED):
        p.error(f"--speed must lie in (0, {MAX_SPEED:g}], got {args.speed}")
    for name in args.allow_host or ():
        if not name.strip() or any(c.isspace() or c in "/@?#" for c in name):
            p.error(f"--allow-host needs a host name such as jetson.local, got {name!r}")
    if not 0 <= args.port <= 65535:
        p.error(f"--port must lie in 0..65535, got {args.port}")
    if not (math.isfinite(args.baseline_s) and args.baseline_s > 0):
        p.error(f"--baseline-s must be positive, got {args.baseline_s}")
    if args.seconds is not None and not (math.isfinite(args.seconds) and args.seconds > 0):
        p.error(f"--seconds must be positive, got {args.seconds}")


class FakeDemo:
    """A FakeBoard that holds no tone for ``rest_s`` seconds, then steps through its tones.

    The z-scores compare against the first ``--baseline-s`` seconds, so a tone during them
    would be part of "rest" and barely stand out afterwards. FakeBoard's own schedule counts
    from its first sample, so the target is set here before each read (and each ``advance``
    of a virtual board) from the seconds generated so far: the switch lands within one poll.
    """

    def __init__(self, board: Any, rest_s: float, switch_s: float = FAKE_SWITCH_S) -> None:
        self.board = board
        self.rest_s = float(rest_s)
        self.switch_s = float(switch_s)
        self._retarget()

    fs = property(lambda self: self.board.fs)
    ch_names = property(lambda self: self.board.ch_names)
    virtual = property(lambda self: self.board.virtual)
    elapsed = property(lambda self: self.board.elapsed)

    def target_at(self, elapsed_s: float) -> int:
        """The tone index due ``elapsed_s`` seconds in; -1 (none) during the rest."""
        into = elapsed_s - self.rest_s
        if into < -1e-9:
            return -1
        return int(math.floor(into / self.switch_s + 1e-9)) % len(self.board.freqs)

    def _retarget(self) -> None:
        self.board.set_target(self.target_at(self.board.elapsed))

    def advance(self, seconds: float) -> None:
        self._retarget()
        self.board.advance(seconds)

    def read(self) -> tuple[Any, Any]:
        self._retarget()
        return self.board.read()

    def open(self) -> FakeDemo:
        self.board.open()
        return self

    def close(self) -> None:
        self.board.close()

    def __enter__(self) -> FakeDemo:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()


def make_board(args: argparse.Namespace,
               clock: Callable[[], float] | None = time.time) -> Any:
    """The EEG source the arguments name (ImportError, OSError, ValueError, KeyError).

    ``clock`` drives the fake board; None makes it virtual (time moves only as run() polls),
    which tests use to play a minute of the demo in a second.
    """
    if args.board == "fake":
        from ..board import FakeBoard
        board = FakeBoard(freqs=FAKE_FREQS, clock=clock, switch_s=FAKE_SWITCH_S,
                          amp=FAKE_AMP_UV)
        return FakeDemo(board, rest_s=args.baseline_s)
    if args.board == "cerelog":
        from ..board import CerelogBoard
        board = CerelogBoard(serial_port=args.serial_port)
        port = board.serial_port or "BrainFlow's default ports (no USB serial device found)"
        print(f"connecting to the Cerelog X8 on {port} (can take 15 s)...", file=sys.stderr)
        return board
    from ..recordings import load_recording
    from .stream import ReplayBoard
    try:
        rec = load_recording(args.recording)
    except Exception as exc:   # numpy and zipfile raise many kinds for a damaged file
        raise ValueError(f"cannot read the recording {args.recording}: {exc}") from exc
    return ReplayBoard(rec, speed=1.0 if args.speed is None else args.speed, loop=args.loop)


class Coalesce:
    """Passes messages on, but at most ``keep`` frames per board poll: the newest ones.

    run() emits every frame one read produced back to back and then calls stop() once per
    poll. A replay at 16x makes three or four frames a poll, more than a page redraws and
    enough to fill a client's queue and get it dropped; a live board that delivers a
    backlog at once is the same. Only frames are held back: ``flush`` (called from the stop
    check and before any other message) sends them in order, so statuses stay in place.
    The frames dropped are display only: the stream's smoothing and baseline saw them all.
    """

    def __init__(self, publish: Callable[[dict], None], keep: int = FRAMES_PER_POLL) -> None:
        if keep < 1:
            raise ValueError(f"keep must be >= 1, got {keep}")
        self.publish = publish
        self.dropped = 0
        self._held: collections.deque[dict] = collections.deque()
        self._keep = int(keep)

    def emit(self, msg: dict) -> None:
        if msg.get("type") == "frame":
            self._held.append(msg)
            if len(self._held) > self._keep:
                self._held.popleft()
                self.dropped += 1
            return
        self.flush()
        self.publish(msg)

    def flush(self) -> None:
        while self._held:
            self.publish(self._held.popleft())


def stream_board(board: Any, stream: Any, publish: Callable[[dict], None], *,
                 stop: Callable[[], bool] = lambda: False, **run_kwargs: Any) -> str:
    """``stream.run`` inside the board's context, publishing through a ``Coalesce``.

    Returns what run() returns. Errors propagate, except that a board which also fails to
    close after Ctrl-C (a BrainFlowError from release_session, say) is reported on stderr
    and the KeyboardInterrupt goes on: the user asked to stop, and did.
    """
    from .stream import run

    out = Coalesce(publish)

    def stop_and_flush() -> bool:
        out.flush()
        return stop()

    board.__enter__()
    try:
        why = run(board, stream, out.emit, stop=stop_and_flush, **run_kwargs)
    except KeyboardInterrupt:
        try:
            board.__exit__(*sys.exc_info())
        except Exception as exc:
            print(f"error: closing the board failed: {exc or type(exc).__name__}",
                  file=sys.stderr)
        raise
    except BaseException:
        board.__exit__(*sys.exc_info())
        raise
    finally:
        out.flush()
    board.__exit__(None, None, None)
    return why


def _serve(args: argparse.Namespace, server: MapServer) -> int:
    from .stream import MapStream

    board = make_board(args)
    stream = MapStream(board.fs, board.ch_names, baseline_s=args.baseline_s, source=args.board)
    server.set_hello(stream.hello())
    server.publish({"type": "status", "state": "starting", "text": f"starting the {args.board} board"})
    server.start()
    print(f"brain map at {server.url} ({args.board} board, {args.baseline_s:g} s baseline); "
          "Ctrl-C stops", file=sys.stderr, flush=True)
    if server.exposed:
        print(f"listening on {args.host or 'every address'}: anyone on this network can watch; "
              f"other devices use http://<this machine's address>:{server.port}/",
              file=sys.stderr)
        print(f"accepted Host names: {', '.join(server.accepted_hosts)} and any IP address "
              "(add one with --allow-host NAME)", file=sys.stderr)
    if not (server.root / "index.html").is_file():
        print(f"warning: no page at {server.root / 'index.html'}; only /events works",
              file=sys.stderr)
    if args.open:
        webbrowser.open(server.url)
    began = time.monotonic()
    stop = (lambda: False) if args.seconds is None else (
        lambda: time.monotonic() - began >= args.seconds)
    try:
        why = stream_board(board, stream, server.publish, stop=stop)
    except Exception as exc:
        # run() publishes this itself for a failure while streaming; this one also covers
        # opening the board (a 15 s Cerelog connect), which fails before run() starts
        server.publish({"type": "status", "state": "error",
                        "text": str(exc) or type(exc).__name__})
        time.sleep(END_GRACE_S)
        raise
    if why == "ended":
        print("the replay ended", file=sys.stderr)
        time.sleep(END_GRACE_S)
    elif not stream.samples:
        # BrainFlow can log a failed handshake and still open the session; then the run
        # ends on --seconds having shown nothing at all, which is a failure, not a run
        text = (f"no samples arrived from the {args.board} board in "
                f"{time.monotonic() - began:.0f} s; is it switched on and streaming?")
        server.publish({"type": "status", "state": "error", "text": text})
        print(f"error: {text}", file=sys.stderr)
        time.sleep(END_GRACE_S)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    if args.board == "replay" and not args.recording.is_file():
        print(f"error: recording not found: {args.recording}", file=sys.stderr)
        return 2
    # bind before touching the board: a busy port should fail before a 15 s connect
    busy = f"error: port {args.port} is in use; try --port 0"
    if port_in_use(args.host, args.port):
        print(busy, file=sys.stderr)
        return 1
    try:
        server = MapServer(args.host, args.port, PAGE_DIR, allowed_hosts=args.allow_host)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(busy, file=sys.stderr)
        else:
            print(f"error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    # `kill` (SIGTERM) stops like Ctrl-C. SIGINT is set too: a shell starts a background
    # job with SIGINT ignored, and Python keeps an inherited SIG_IGN
    previous = {sig: _on_signal(sig, h) for sig, h in
                ((signal.SIGTERM, _interrupt), (signal.SIGINT, signal.default_int_handler))}
    try:
        return _serve(args, server)
    except Exception as exc:   # a BrainFlowError from read() is none of the usual kinds
        print(f"error: {exc or type(exc).__name__}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
    finally:
        server.stop()
        for sig, handler in previous.items():
            _on_signal(sig, handler)


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _on_signal(sig: int, handler: Any) -> Any:
    """Install a handler for ``sig``; the previous one (None off the main thread, where
    Python allows no signal handlers, e.g. main() called from a test thread)."""
    if handler is None or threading.current_thread() is not threading.main_thread():
        return None
    try:
        return signal.signal(sig, handler)
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())
