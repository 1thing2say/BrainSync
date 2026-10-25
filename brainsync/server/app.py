"""The BrainSync server: the headset, the decoder and every client, in one process.

    python -m brainsync.server --board fake --no-auth        # try it all without a headset
    python -m brainsync.server --board cerelog               # the Cerelog X8 on USB serial
    python -m brainsync.server --origin 'https://ripe-banana\\.trycloudflare\\.com'  # behind cloudflared
    python -m brainsync.server --board fake --env-file .env  # point mode, key from a file

It wires the pieces of PROTOCOL-v3.md together: the board (a real-time FakeBoard with
``--board fake``, else the Cerelog X8 through the BrainFlow fork) feeds a DecoderBank, the
Session runs the phases, calibration, onset gating and the client's mode, and the Transport
serves WebSocket and HTTP on one port. ``GET /`` is the reference page from ``clients/web/``
(with ``clients/js/`` beside it), ``GET /health`` the board and client state, and
``GET /api/speech?text=...`` a WAV of the text in the Cartesia voice of brainsync.voice
(``speech_clip``; 503 without CARTESIA_API_KEY or with BRAINSYNC_TTS=off), so a desktop
client can speak status changes without holding the key.

Startup prints, once, the address of the reference page with the token in its fragment
(``#token=...``), so it can be opened or pasted into the BrainPoint extension's options. The
token comes from ``--token-file`` (default ~/.config/brainsync/token); on the first run that
file is created with mode 0600 and holds ``secrets.token_urlsafe(24)``. ``--no-auth`` turns
the token off and is allowed only on a loopback ``--host``.

Point mode (BrainPoint) needs Claude Haiku to find the clickable elements of a screenshot. It
is on when ANTHROPIC_API_KEY is set (``--env-file`` reads it from a KEY=VALUE file without
overriding the environment) and off with ``--no-detect``. Without it the server still runs:
a point-mode controller gets ``error detect_unavailable`` and slots mode works as before.

Rules:
- Ctrl-C or SIGTERM sends ``phase stop`` to every client, closes the connections and the
  board, and exits 0. Exit 1 is a runtime failure (port in use, board missing), exit 2 a bad
  argument.
- ``--log PATH`` appends one JSON object per line ({"kind", "t", ...}) for connections,
  phases, layouts, onsets, selections, scans and clicks. ``--log-scans DIR`` also keeps each
  screenshot with its page data in the format of tools/screen_capture.md; without it no
  screenshot is ever written to disk.
- The token and the API key are never logged. The token is printed once at startup, for the
  user to open the page with.
- Notes go to stderr.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import os
import secrets
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..classify import SPATIAL_FILTERS
from ..envfile import load_env_file
from ..eog import GazeDecoder
from ..online import TRAIN_WINDOW_S, trained_from_recording
from ..recordings import EOG_MONTAGE, POSTERIOR
from . import protocol as P
from .decoding import DecoderBank
from .session import Session
from .transport import Transport, is_loopback

NAME = "python -m brainsync.server"
DEFAULT_FREQS = (15.0, 17.0, 12.0, 20.0)
TOKEN_FILE = Path("~/.config/brainsync/token")
CALIBRATE_S = 10.0
ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "clients" / "web"
JS_DIR = ROOT / "clients" / "js"
WEB_FILES = ("index.html", "style.css", "app.js")
JS_FILES = ("flicker.js", "brainsync-client.js")
IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png"}
SPEECH_TIMEOUT_S = 10.0     # one /api/speech synthesis (Cartesia's own limit is 8 s a try)


def _note(text: str) -> None:
    print(f"server: {text}", file=sys.stderr, flush=True)


# -- pieces -------------------------------------------------------------------------------

def parse_freqs(text: str) -> tuple[float, ...]:
    """"15,17,12,20" as a tuple of Hz: at least two, unique, each in (0, 125)."""
    try:
        freqs = tuple(float(part) for part in str(text).split(","))
    except ValueError:
        raise ValueError(f"--freqs must be numbers of Hz separated by commas, such as "
                         f"15,17,12,20, got {text!r}") from None
    if len(freqs) < 2:
        raise ValueError(f"--freqs needs at least two frequencies (point mode shows K-1 "
                         f"buttons plus \"more\"), got {text!r}")
    if not all(math.isfinite(f) and 0 < f < 125 for f in freqs):
        raise ValueError(f"--freqs must lie in (0, 125) Hz, half the board's 250 Hz, got "
                         f"{text!r}")
    if len(set(freqs)) != len(freqs):
        raise ValueError(f"--freqs must not repeat a frequency, got {text!r}")
    return freqs


def parse_refresh_rates(text: str) -> tuple[float, ...]:
    """"60,90,120,144" as a tuple of Hz: at least one, unique, each in (0, 1000]."""
    try:
        rates = tuple(float(part) for part in str(text).split(","))
    except ValueError:
        raise ValueError(f"--refresh-rates must be display refresh rates in Hz separated by "
                         f"commas, such as 60,90,120,144, got {text!r}") from None
    if not all(math.isfinite(r) and 0 < r <= 1000 for r in rates):
        raise ValueError(f"--refresh-rates must lie in (0, 1000] Hz, got {text!r}")
    if len(set(rates)) != len(rates):
        raise ValueError(f"--refresh-rates must not repeat a rate, got {text!r}")
    return rates


def read_or_create_token(path: str | Path) -> tuple[str, bool]:
    """The token in ``path``, creating the file (mode 0600) when it is missing.

    Returns (token, created). ValueError when the file exists but is empty or unreadable. A
    token file that others can read gets a warning on stderr, never its content.
    """
    path = Path(path).expanduser()
    if path.exists():
        try:
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read the token file {path}: {type(exc).__name__}; fix "
                             f"its permissions or delete it to make a new token") from None
        if not token:
            raise ValueError(f"the token file {path} is empty; delete it to make a new token")
        if any(c.isspace() for c in token):
            raise ValueError(f"the token in {path} contains whitespace; delete the file to make "
                             f"a new token")
        try:
            if path.stat().st_mode & 0o077:
                _note(f"the token file {path} can be read by other users; run "
                      f"chmod 600 {path}")
        except OSError:
            pass
        return token, False
    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
    except OSError as exc:
        raise ValueError(f"cannot create the token file {path}: {exc.strerror or exc}; pass "
                         f"--token-file with a writable path") from None
    return token, True


def static_files(web_dir: Path = WEB_DIR, js_dir: Path = JS_DIR) -> dict[str, Path]:
    """The reference page's files by served name; missing ones are left out with a note."""
    files: dict[str, Path] = {}
    for folder, names in ((web_dir, WEB_FILES), (js_dir, JS_FILES)):
        for name in names:
            path = folder / name
            if path.is_file():
                files[name] = path
            else:
                _note(f"{path} is missing, so the reference page is incomplete")
    return files


def browser_url(host: str, port: int, token: str | None) -> str:
    """The reference page's address, with the token in the fragment (never sent to a server)."""
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else ("[::1]" if host == "::" else host)
    if ":" in shown and not shown.startswith("["):
        shown = f"[{shown}]"
    return f"http://{shown}:{port}/" + ("" if token is None else f"#token={token}")


def _plain(value: Any) -> Any:
    """JSON-safe copy: non-finite floats become None, unknown objects their str()."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    try:
        f = float(value)          # numpy scalars
    except (TypeError, ValueError):
        return str(value)
    return f if math.isfinite(f) else None


class JsonLog:
    """``log(kind, fields)`` hook that appends JSON lines to a file (created 0600).

    Thread-safe. A write that fails is noted on stderr once and the session goes on.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        try:
            if self.path.parent != Path(""):
                self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            raise ValueError(f"cannot open the log {self.path}: {exc.strerror or exc}; pass "
                             f"--log with a writable path") from None
        self._file = os.fdopen(fd, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._failed = False

    def __call__(self, kind: str, fields: dict) -> None:
        record = {"kind": kind, **_plain(dict(fields))}
        if "t" not in record:
            record["t"] = time.time()
        line = json.dumps(record, ensure_ascii=False, allow_nan=False)
        with self._lock:
            if self._file.closed:
                return
            try:
                self._file.write(line + "\n")
                self._file.flush()
            except OSError as exc:
                if not self._failed:
                    _note(f"cannot write the log {self.path}: {exc}")
                    self._failed = True

    def prefixed(self, prefix: str) -> Callable[[str, dict], None]:
        """A hook that logs ``prefix + kind`` (point mode's records become "point.scan")."""
        return lambda kind, fields: self(prefix + kind, fields)

    def close(self) -> None:
        with self._lock:
            self._file.close()


class ScanArchive:
    """Keeps each scan as ``<stamp>-<scan_id>.jpg`` (or .png) plus ``.json`` page data.

    The JSON has the fields of tools/screen_capture.md ({url, title, viewport, image, hints}),
    so a folder of kept scans can be scored with tools/screen_eval.py. The folder is created
    with mode 0700, because screenshots can show private pages.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"cannot create the --log-scans folder {self.directory}: "
                             f"{exc.strerror or exc}") from None
        if not self.directory.is_dir():
            raise ValueError(f"--log-scans {self.directory} is not a folder")
        self.saved = 0

    def save(self, msg: dict) -> Path | None:
        """Write one scan message; returns the image path, or None (noted) when it failed.
        An ax scan without an image (v3.1) has nothing to keep and is skipped quietly."""
        if msg.get("image") is None and msg.get("source") == "ax":
            return None
        try:
            image = msg["image"]
            ext = IMAGE_EXT[image["media_type"]]
            data = base64.b64decode(image["data"], validate=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            stem = self.directory / f"{stamp}-{int(msg['scan_id'])}"
            meta = {"url": msg.get("url", ""), "title": msg.get("title", ""),
                    "viewport": msg.get("viewport"), "image": {"w": image["w"], "h": image["h"]},
                    "hints": list(msg.get("hints") or [])}
            stem.with_suffix(ext).write_bytes(data)
            stem.with_suffix(".json").write_text(json.dumps(_plain(meta), ensure_ascii=False),
                                                 encoding="utf-8")
        except (KeyError, TypeError, ValueError, binascii.Error, OSError) as exc:
            _note(f"could not keep scan {msg.get('scan_id')!r}: {type(exc).__name__}: {exc}")
            return None
        self.saved += 1
        return stem.with_suffix(ext)


class ArchivingMode:
    """A Mode that hands every ``scan`` to a ScanArchive before the wrapped mode sees it."""

    def __init__(self, mode: Any, archive: ScanArchive) -> None:
        self.mode = mode
        self.archive = archive

    def start(self, now: float) -> list[dict]:
        return self.mode.start(now)

    def on_selection(self, slot: str, now: float) -> list[dict]:
        return self.mode.on_selection(slot, now)

    def on_message(self, msg: dict, now: float) -> list[dict]:
        if isinstance(msg, dict) and msg.get("type") == "scan":
            self.archive.save(msg)
        return self.mode.on_message(msg, now)

    def tick(self, now: float) -> list[dict]:
        return self.mode.tick(now)

    def stop(self) -> None:
        self.mode.stop()


def make_detector(args: argparse.Namespace) -> Any | None:
    """The Detector for point mode, or None (noted on stderr) when it is off."""
    if args.no_detect:
        _note("point mode is off (--no-detect); slots mode works")
        return None
    from ..screen.detect import Detector

    detector = Detector(args.model)
    if not detector.enabled:
        _note(f"point mode is off: {detector.why_disabled}")
        return None
    return detector


def make_point_factory(detector: Any, freqs: Sequence[float], *, min_p_button: float,
                       log: Callable[[str, dict], None] | None = None,
                       archive: ScanArchive | None = None,
                       **options: Any) -> Callable[[], Any]:
    """A factory of PointMode (one per controller), checked once now so a bad setting fails at
    startup instead of at the first controller."""
    from ..screen.controller import PointMode

    def factory() -> Any:
        mode = PointMode(detector, freqs, min_p_button=min_p_button, log=log, **options)
        return mode if archive is None else ArchivingMode(mode, archive)

    factory().stop()
    return factory


def speech_clip(text: str) -> bytes | None:
    """The WAV of ``text`` in the shared Cartesia voice (brainsync.voice), for
    ``GET /api/speech``; None when there is no voice (BRAINSYNC_TTS=off, no
    CARTESIA_API_KEY, or the synthesis failed). Blocking: the transport runs it on a thread."""
    from .. import voice

    speaker = voice.default_speaker()
    if speaker is None:
        return None
    clip = speaker.clip(text, timeout=SPEECH_TIMEOUT_S)
    return None if clip is None else clip.read_bytes()


def make_board(args: argparse.Namespace, freqs: Sequence[float]) -> Any:
    """The board, not yet opened. ImportError when BrainFlow is missing."""
    from ..board import CerelogBoard, FakeBoard

    names = EOG_MONTAGE if getattr(args, "eog", False) else None
    if args.board == "fake":
        return FakeBoard(freqs, clock=time.time, ch_names=names)
    return CerelogBoard(serial_port=args.serial_port, ch_names=names)


# -- command line -------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=NAME, description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1: this machine only; 0.0.0.0 "
                        "for the LAN). Other machines reach it through a cloudflared tunnel "
                        "(deploy/jetson/cloudflare/)")
    p.add_argument("--port", type=int, default=P.DEFAULT_PORT,
                   help=f"port for HTTP and WebSocket (default {P.DEFAULT_PORT}; 0 picks a "
                        f"free one)")
    auth = p.add_mutually_exclusive_group()
    auth.add_argument("--token-file", metavar="PATH", default=str(TOKEN_FILE),
                      help=f"the auth token, created with mode 0600 on the first run "
                           f"(default {TOKEN_FILE})")
    auth.add_argument("--no-auth", action="store_true",
                      help="no token (only with a loopback --host)")
    p.add_argument("--board", choices=("fake", "cerelog"), default="cerelog",
                   help="fake: a simulated board that follows the page's gaze messages; "
                        "cerelog: the Cerelog X8 (default)")
    p.add_argument("--serial-port", metavar="DEV", default=None,
                   help="the Cerelog's serial device (default: the first USB serial device)")
    p.add_argument("--eog", action="store_true",
                   help="the C3/C4 leads sit on gel pads at the left and right temple (channels "
                        f"{','.join(EOG_MONTAGE)}): the eye's horizontal and vertical EOG "
                        "becomes a gaze prior added to the SSVEP scores for layouts whose "
                        "target positions are known (point mode); brainsync.eog")
    p.add_argument("--freqs", default=",".join(f"{f:g}" for f in DEFAULT_FREQS),
                   help="stimulus frequencies in Hz, best first (default 15,17,12,20); point "
                        "mode shows len-1 buttons plus \"more\"")
    p.add_argument("--decoder-model", metavar="FILE", default=None,
                   help="trained SSVEP classifier saved by brainsync.calibrate, instead of FBCCA; "
                        "every layout's targets must be the ones it was trained for (same "
                        "frequencies and phases), or activating that layout fails")
    p.add_argument("--calibrate-s", type=float, default=CALIBRATE_S,
                   help=f"rest-baseline seconds for each new controller (default "
                        f"{CALIBRATE_S:g}; at least about 3.75)")
    p.add_argument("--trained", metavar="CUED_NPZ", type=Path, default=None,
                   help="decode with a spatial filter fitted on this cued recording (its "
                        "targets must include every --freqs frequency, same phases and rate) "
                        "instead of the training-free FBCCA")
    p.add_argument("--trained-method", default="etrca-sf", choices=list(SPATIAL_FILTERS),
                   help="which spatial filter --trained fits (default etrca-sf; a template "
                        "classifier is saved by brainsync.calibrate and loaded with "
                        "--decoder-model instead)")
    p.add_argument("--trained-window", type=float, default=TRAIN_WINDOW_S, metavar="S",
                   help=f"training window per trial in seconds, clipped to the recorded one "
                        f"(default {TRAIN_WINDOW_S:g}: short enough that a confirmed pick's "
                        f"window, gate to commit, can be learnt during the session)")
    p.add_argument("--refresh-rates", metavar="HZ,...",
                   default=",".join(f"{r:g}" for r in P.REFRESH_RATES),
                   help=f"display refresh rates a controller may have, each within "
                        f"{P.REFRESH_TOLERANCE_HZ:g} Hz (default 60,90,120,144); the first is "
                        f"the nominal one, and a client at another computes the flicker at its "
                        f"own measured refresh, so the frequencies stay the same")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="accept clients whose display is at none of --refresh-rates; such a "
                        "client computes the flicker at its own measured refresh, so the "
                        "frequencies stay the same, and draws still any target at or above "
                        "half its refresh")
    p.add_argument("--origin", metavar="REGEX", action="append", default=[],
                   help="also allow browser pages whose Origin fully matches REGEX "
                        "(repeatable), e.g. 'https://ripe-banana\\.trycloudflare\\.com'")
    p.add_argument("--model", default=None,
                   help="the Claude model for point mode (default claude-haiku-5-5)")
    p.add_argument("--no-detect", action="store_true",
                   help="no element detection: point mode is off, slots mode works")
    p.add_argument("--env-file", metavar="PATH", default=None,
                   help="KEY=VALUE file read into the environment without overriding it "
                        "(for ANTHROPIC_API_KEY)")
    p.add_argument("--min-p-button", type=float, default=0.5,
                   help="point mode shows elements whose button probability is at least "
                        "this (default 0.5)")
    p.add_argument("--log", metavar="PATH", default=None,
                   help="append a JSON-lines log of the session to PATH")
    p.add_argument("--log-scans", metavar="DIR", default=None,
                   help="keep every point-mode screenshot and its page data in DIR (off by "
                        "default: screenshots are never stored)")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject bad values through ``p.error`` (exit code 2)."""
    if not 0 <= args.port <= 65535:
        p.error(f"--port must lie in 0..65535, got {args.port}")
    if args.no_auth and not is_loopback(args.host):
        p.error(f"--no-auth is allowed only with a loopback --host (127.0.0.1, ::1 or "
                f"localhost), not {args.host}; drop --no-auth and use the token")
    try:
        args.freqs = parse_freqs(args.freqs)
        args.refresh_rates = parse_refresh_rates(args.refresh_rates)
    except ValueError as exc:
        p.error(str(exc))
    lowest, top = min(args.refresh_rates), max(args.freqs)
    if lowest - P.REFRESH_TOLERANCE_HZ <= 2 * top:
        p.error(f"--refresh-rates {lowest:g} cannot show the {top:g} Hz of --freqs (a display "
                f"shows less than half its refresh); drop that rate or lower --freqs")
    if not (math.isfinite(args.calibrate_s) and args.calibrate_s > 0):
        p.error(f"--calibrate-s must be a positive number of seconds, got {args.calibrate_s}")
    if not 0.0 <= args.min_p_button <= 1.0:   # also rejects NaN
        p.error(f"--min-p-button must lie in [0, 1], got {args.min_p_button}")
    if args.serial_port is not None and args.board != "cerelog":
        p.error("--serial-port is for --board cerelog only")
    if args.model is not None and not args.model.strip():
        p.error("--model must be a model id such as claude-haiku-5-5")
    if args.model is None:
        from ..screen.detect import MODEL
        args.model = MODEL


def _on_signal(sig: int, handler: Any) -> Any:
    """Install a handler for ``sig``; the previous one (None off the main thread, where
    Python allows no signal handlers, e.g. main() called from a test thread)."""
    if handler is None:
        return None
    try:
        return signal.signal(sig, handler)
    except ValueError:
        return None


def serve(args: argparse.Namespace, stop: threading.Event | None = None,
          ready: Callable[[Any], None] | None = None, *, detector: Any | None = None) -> int:
    """Run the server until ``stop`` is set; returns the exit code.

    ``args`` are parsed and checked (build_parser, check_arguments). ``ready(transport)`` is
    called once the port is bound and the board is open (tests use it to learn the port).
    ``detector`` replaces the Detector made from the environment (tests pass one whose
    ``create`` is a stub, so no test reaches the API).
    """
    if stop is None:
        stop = threading.Event()
    if args.env_file is not None:
        try:
            names = load_env_file(args.env_file)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _note(f"read {len(names)} variable(s) from {args.env_file}"
              + (f": {', '.join(names)}" if names else ""))
    token: str | None = None
    if not args.no_auth:
        try:
            token, created = read_or_create_token(args.token_file)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if created:
            _note(f"made a new token in {Path(args.token_file).expanduser()} (mode 0600)")
    log: JsonLog | None = None
    if args.log is not None:
        try:
            log = JsonLog(args.log)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        return _serve(args, token, log, stop, ready, detector)
    finally:
        if log is not None:
            log.close()


def _serve(args: argparse.Namespace, token: str | None, log: JsonLog | None,
           stop: threading.Event, ready: Callable[[Any], None] | None,
           detector: Any | None) -> int:
    freqs = args.freqs
    try:
        board = make_board(args, freqs)
    except ImportError as exc:
        print(f"error: {exc}. On a Jetson, build and install the Linux aarch64 wheel as "
              f"deploy/jetson/brainflow/INSTALL.md says, or run --board fake", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        print(f"error: cannot set up the board: {exc}", file=sys.stderr)
        return 1
    archive: ScanArchive | None = None
    try:
        if args.trained is not None and args.decoder_model is not None:
            raise ValueError("--trained and --decoder-model both replace FBCCA; pass one")
        trained = None
        if args.trained is not None:
            trained = trained_from_recording(args.trained, args.trained_method, channels=POSTERIOR,
                                             window_s=args.trained_window)
            if abs(trained.fs - board.fs) > 1e-6:
                raise ValueError(f"{args.trained}: recorded at {trained.fs:g} Hz, the board runs "
                                 f"at {board.fs:g} Hz")
            trained.for_freqs(freqs)        # every layout frequency must be a trained target
            print(f"trained {trained.name} from {args.trained.name}: {int(trained.n_train.sum())} "
                  f"trials of {trained.templates.shape[-1] / trained.fs:g} s over "
                  f"{', '.join(f'{f:g}' for f in trained.freqs)} Hz, channels "
                  f"{','.join(POSTERIOR)}" + (", periodic templates" if trained.cycle is not None
                                               else ""), file=sys.stderr)
        gaze = GazeDecoder(board.fs, board.ch_names) if args.eog else None
        bank = DecoderBank(board.fs, ch_names=board.ch_names, model=args.decoder_model,
                           gaze=gaze, trained=trained)
        if gaze is not None:
            _note("EOG gaze prior on: channels LO and RO at the temples (brainsync.eog)")
        if detector is None:
            detector = make_detector(args)
        elif args.no_detect:
            detector = None
        point_factory = None
        if detector is not None:
            if args.log_scans is not None:
                archive = ScanArchive(args.log_scans)
            point_factory = make_point_factory(
                detector, freqs, min_p_button=args.min_p_button, archive=archive,
                log=None if log is None else log.prefixed("point."))
        # The transport and the session need each other; the session is made right below,
        # before the transport starts, so these lambdas never see None.
        session: Any = None
        transport = Transport(args.host, args.port, token=token, origins=args.origin,
                              static_files=static_files(),
                              health=lambda: session.health(),
                              catchup=lambda conn_id, role, hello:
                                  session.catchup(conn_id, role, hello),
                              speech=speech_clip, max_slots=len(freqs))
        session = Session(transport, board, bank, freqs, fake=args.board == "fake",
                          point_factory=point_factory, calibrate_s=args.calibrate_s,
                          refresh_hz=args.refresh_rates[0], refresh_rates=args.refresh_rates,
                          ignore_refresh=args.ignore_refresh, clock=time.time, log=log)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {msg}", file=sys.stderr)
        return 2
    try:
        transport.start()
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        try:
            board.open()
        except Exception as exc:   # BrainFlow errors, a missing serial device
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            what = "fake board" if args.board == "fake" else "Cerelog X8"
            detect = "point mode on" if point_factory is not None else "point mode off"
            print(f"BrainSync server on {transport.url} ({what}, {detect}, "
                  f"freqs {','.join(f'{f:g}' for f in freqs)})", flush=True)
            print(f"open {browser_url(args.host, transport.port, token)}", flush=True)
            if ready is not None:
                ready(transport)
            session.run(stop)      # broadcasts phase "stop" when it returns
        finally:
            board.close()
    finally:
        transport.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    stop = threading.Event()

    def _stop(signum: int, frame: object) -> None:
        stop.set()

    # SIGINT is set too: a shell starts a background job with SIGINT ignored, and Python
    # keeps an inherited SIG_IGN, so Ctrl-C or `kill -INT` would do nothing.
    previous = {sig: _on_signal(sig, _stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        code = serve(args, stop)
    finally:
        for sig, handler in previous.items():
            _on_signal(sig, handler)
    if stop.is_set():
        print("stopped", file=sys.stderr)
    return code
