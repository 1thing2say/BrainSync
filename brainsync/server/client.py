"""A small synchronous BrainSync client, for tests and headless use (PROTOCOL-v3).

    with Client("ws://127.0.0.1:8765/ws", token, slots=("yes", "no")) as c:
        print(c.welcome["freqs"])                  # connect() ran on entering the block
        if c.fake:
            c.gaze("yes")                          # the fake board now carries yes's tone
        sel = c.until("selection", timeout=30.0)   # {"slot": "yes", "score": ..., ...}

    python -m brainsync.server.client --url ws://127.0.0.1:8765/ws --token-file TOKEN \\
        --slots yes,no --gaze yes --count 3

The client speaks the client side of PROTOCOL-v3 over websockets' blocking API, so a test or
a script can drive the server without an event loop. It draws nothing. With ``auto_onset`` (the
default) a controller still sends the ``onset`` a drawing client would send: once per layout,
as soon as that layout has targets and the last phase is "run" with flicker on. Without it the
server waits 2 s and then gates the decoder on the time it sent the layout (rule 2).

Code mode (v3.2, ``welcome.paradigm`` "code"): the onset also goes out in the "train" phase,
which flickers too, and the client keeps the last ``train`` progress, ``trained`` result and
``decision`` it received. It sends no ``cycle`` messages, since it draws no frames: on a real
headset a drawing client must, and the fake board reports its own cycle times.
``decision_lines`` renders a decision as the standard's confidence bars (``--decisions``).

connect() sends the hello and returns the welcome. An ``error`` or a close before the welcome
raises ServerError, whose ``code`` is the protocol error code ("bad_token", "busy", ...) and
whose ``close_code`` is the WebSocket close code (4001, 4002, ...) when the socket closed.
After that, recv() returns every message the server sends, including errors and the keepalive
pings, and raises ServerError once the socket closes. A client closed with 4000 was replaced by
another controller and must not reconnect by itself; this one never reconnects at all.

The auth token is sent in the hello and nowhere else: it is not printed, logged or kept in an
exception message. Notes go to stderr. The CLI exits 0 when it is done (``--count`` reached,
``--seconds`` over, Ctrl-C, or the server stopped), 1 when the server refused or dropped the
connection, and 2 for bad arguments.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as ws_connect
from websockets.uri import parse_uri

from .protocol import (DEFAULT_PORT, FLICKER_PHASES, MAX_FRAME, MODES, PROTOCOL, ROLES,
                       SLOT_CHARS, dumps)

NAME = "brainsync-python"
VERSION = "0.1.0"
URL = f"ws://127.0.0.1:{DEFAULT_PORT}/ws"

# What the close codes of PROTOCOL-v3 section 1 mean, and what to do about each.
CLOSE_TEXT = {
    1009: "a message was larger than 4 MB",
    4000: "another controller took over (takeover); do not reconnect by yourself",
    4001: "the server refused the token; pass the token the server printed at startup",
    4002: "another controller is connected; connect as a viewer, or take over with takeover",
    4003: "the server expected hello as the first message",
    4008: "the server got no hello within 10 s",
}


class ServerError(RuntimeError):
    """The server refused or ended the connection.

    ``code`` is the protocol error code when an ``error`` message said why, else None.
    ``close_code`` and ``reason`` come from the WebSocket close, when the socket closed.
    """

    def __init__(self, message: str, *, code: str | None = None, close_code: int | None = None,
                 reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.close_code = close_code
        self.reason = reason


def _note(text: str) -> None:
    print(f"client: {text}", file=sys.stderr, flush=True)


def _check_slot(slot: Any, name: str) -> str:
    if not isinstance(slot, str) or not 1 <= len(slot) <= SLOT_CHARS:
        raise ValueError(f"{name} must be a string of 1-{SLOT_CHARS} characters, got {slot!r}")
    return slot


def _check_url(url: Any) -> str:
    if not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
        raise ValueError(f"url must be a WebSocket URL such as {URL}, got {url!r}")
    try:
        parse_uri(url)
    except InvalidURI as exc:
        raise ValueError(f"url {url!r} is not a valid WebSocket URL ({exc}); use the form "
                         f"{URL}") from None
    return url


def _closed_error(exc: ConnectionClosed, before_welcome: bool) -> ServerError:
    """A ServerError for a closed socket, saying what the close code means."""
    close = exc.rcvd
    code = None if close is None else int(close.code)
    reason = "" if close is None else close.reason
    when = "before the welcome" if before_welcome else ""
    text = f"the server closed the connection {when}".rstrip()
    if code is None:
        text += " without a close frame"
    else:
        text += f" (code {code}{', ' + reason if reason else ''})"
        if code in CLOSE_TEXT:
            text += f": {CLOSE_TEXT[code]}"
    return ServerError(text, close_code=code, reason=reason)


def _parse(data: str | bytes) -> dict[str, Any] | None:
    """A server frame as a dict with a string "type", or None (noted) for anything else."""
    if not isinstance(data, str):
        _note(f"ignored a binary frame of {len(data)} bytes")
        return None
    try:
        msg = json.loads(data)
    except ValueError:
        _note(f"ignored a message that is not JSON: {data[:60]!r}")
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        _note(f"ignored a message without a string \"type\": {data[:60]!r}")
        return None
    return msg


class Client:
    """One connection to a BrainSync server, as a controller or a viewer.

    ``token`` None suits a server started with --no-auth. ``mode`` may be None for a viewer
    only. ``slots`` are used by a slots-mode controller and ignored otherwise. A viewer may
    leave ``refresh_hz`` as None. ``accepted`` must be true for a controller (the server
    answers bad_hello otherwise); it is passed through so tests can check that answer.
    """

    def __init__(self, url: str = URL, token: str | None = None, *, role: str = "controller",
                 mode: str | None = "slots", slots: Iterable[str] | None = ("yes", "no"),
                 refresh_hz: float | None = 60.0, accepted: bool = True,
                 takeover: bool = False, name: str = NAME, auto_onset: bool = True) -> None:
        self.url = _check_url(url)
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("token must be a non-empty string; leave it out (None) for a "
                             "server started with --no-auth")
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {role!r}")
        if mode is None and role == "controller":
            raise ValueError("a controller needs mode \"slots\" or \"point\"; only a viewer "
                             "may leave it out")
        if mode is not None and mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        clean: tuple[str, ...] | None = None
        if role == "controller" and mode == "slots":
            if slots is None or isinstance(slots, str):
                raise ValueError("slots must be a list of names such as (\"yes\", \"no\"), "
                                 f"got {slots!r}")
            clean = tuple(_check_slot(s, f"slots[{i}]") for i, s in enumerate(slots))
            if not clean:
                raise ValueError("slots mode needs at least one slot, such as (\"yes\", \"no\")")
            if len(set(clean)) != len(clean):
                raise ValueError(f"slots must not repeat a name, got {list(clean)}")
        if refresh_hz is None:
            if role == "controller":
                raise ValueError("a controller must report refresh_hz, the display refresh "
                                 "rate in Hz (60.0 for a client that draws nothing)")
        elif (isinstance(refresh_hz, bool) or not isinstance(refresh_hz, (int, float))
              or not math.isfinite(refresh_hz) or refresh_hz <= 0):
            raise ValueError(f"refresh_hz must be a positive number of Hz, got {refresh_hz!r}")
        if not isinstance(name, str) or not name:
            raise ValueError(f"name must be a non-empty string, got {name!r}")
        self._token = token
        self.role = role
        self.mode = mode
        self.slots = clean
        self.refresh_hz = None if refresh_hz is None else float(refresh_hz)
        self.accepted = bool(accepted)
        self.takeover = bool(takeover)
        self.name = name[:64]
        self.instance = uuid.uuid4().hex   # hello.client.instance: the same on every connect()
        self.auto_onset = bool(auto_onset)
        self.welcome: dict[str, Any] | None = None
        self.phase: dict[str, Any] | None = None     # the last "phase" message
        self.layout: dict[str, Any] | None = None    # the last "layout" message
        self.train: dict[str, Any] | None = None     # code mode: the last "train" progress
        self.trained: dict[str, Any] | None = None   # code mode: the last "trained" result
        self.decision: dict[str, Any] | None = None  # code mode: the last "decision"
        self.onsets = 0                               # onsets sent by auto_onset
        self._onset_layout: int | None = None         # the layout the last onset was for
        self._conn: ClientConnection | None = None

    def __repr__(self) -> str:   # never shows the token
        state = "connected" if self.connected else "not connected"
        return f"<Client {self.role} {self.mode} {self.url} {state}>"

    def __enter__(self) -> Client:
        if not self.connected:
            self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def fake(self) -> bool:
        """True when the server runs --board fake, so gaze() moves the simulated tone."""
        return bool(self.welcome and self.welcome.get("fake"))

    @property
    def paradigm(self) -> str | None:
        """"code" (c-VEP, v3.2) or "freq"; an older server's welcome has none ("freq")."""
        if self.welcome is None:
            return None
        return "code" if self.welcome.get("paradigm") == "code" else "freq"

    def hello(self) -> dict[str, Any]:
        """The hello this client sends (PROTOCOL-v3 section 2), token included."""
        return {"type": "hello", "protocol": PROTOCOL, "token": self._token, "role": self.role,
                "takeover": self.takeover,
                "client": {"name": self.name, "kind": "python", "version": VERSION,
                           "instance": self.instance},
                "mode": self.mode, "slots": None if self.slots is None else list(self.slots),
                "refresh_hz": self.refresh_hz, "accepted": self.accepted, "viewport": None}

    # -- connection -----------------------------------------------------------------------

    def connect(self, timeout: float = 5.0) -> dict[str, Any]:
        """Open the socket, send the hello and return the welcome.

        ServerError when the server answers with an ``error`` or closes before the welcome
        (wrong token, busy, bad hello), or refuses the HTTP upgrade (wrong origin, wrong
        path). TimeoutError when no welcome arrives within ``timeout`` seconds. OSError when
        nothing listens at the URL.
        """
        if self._conn is not None:
            raise RuntimeError("this client is already connected; close() it first")
        if not (isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0):
            raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")
        deadline = time.monotonic() + timeout
        try:
            conn = ws_connect(self.url, open_timeout=timeout, max_size=MAX_FRAME)
            # websockets >= 17 warns about a connection used outside a with block; entering
            # it here returns the same connection (and is a no-op in older versions).
            conn.__enter__()
        except InvalidHandshake as exc:   # e.g. HTTP 403 for a refused origin
            raise ServerError(f"the server refused the WebSocket upgrade at {self.url}: "
                              f"{exc}") from None
        self.welcome = self.phase = self.layout = None
        self.train = self.trained = self.decision = None
        self._onset_layout = None
        try:
            conn.send(dumps(self.hello()))
            welcome = self._await_welcome(conn, deadline, timeout)
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        self.welcome = welcome
        return welcome

    def _await_welcome(self, conn: ClientConnection, deadline: float,
                       timeout: float) -> dict[str, Any]:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no welcome from {self.url} within {timeout:g} s")
            try:
                data = conn.recv(timeout=left)
            except TimeoutError:
                raise TimeoutError(f"no welcome from {self.url} within {timeout:g} s") from None
            except ConnectionClosed as exc:
                raise _closed_error(exc, before_welcome=True) from None
            msg = _parse(data)
            if msg is None:
                continue
            kind = msg["type"]
            if kind == "error":
                raise self._refused(conn, msg, deadline)
            if kind != "welcome":
                continue   # e.g. a keepalive ping; nothing else comes before the welcome
            if msg.get("protocol") != PROTOCOL:
                raise ServerError(f"the server speaks protocol {msg.get('protocol')!r}, this "
                                  f"client speaks {PROTOCOL}; update one of them")
            return msg

    @staticmethod
    def _refused(conn: ClientConnection, msg: Mapping[str, Any], deadline: float) -> ServerError:
        """A ServerError for an error before the welcome, with the close code if one follows."""
        code = msg.get("code") if isinstance(msg.get("code"), str) else None
        text = f"the server refused the hello: {code}: {msg.get('message', '')}"
        close_code, reason = None, ""
        if msg.get("fatal") is True:   # the close follows at once; read its code
            try:
                conn.recv(timeout=max(0.0, min(1.0, deadline - time.monotonic())))
            except ConnectionClosed as exc:
                if exc.rcvd is not None:
                    close_code, reason = int(exc.rcvd.code), exc.rcvd.reason
            except TimeoutError:
                pass
        if close_code is not None:
            text += f" (close {close_code})"
        return ServerError(text, code=code, close_code=close_code, reason=reason)

    def close(self) -> None:
        """Say bye and close the socket; safe to call twice or after the server closed it."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.send(dumps({"type": "bye"}))
        except ConnectionClosed:
            pass
        conn.close()

    def _need(self) -> ClientConnection:
        if self._conn is None:
            raise RuntimeError("this client is not connected; call connect() first")
        return self._conn

    # -- messages -------------------------------------------------------------------------

    def send(self, msg: Mapping[str, Any]) -> None:
        """Send one message. ValueError for a non-message or NaN (browsers cannot parse it);
        ServerError when the socket is closed."""
        if not isinstance(msg, Mapping) or not isinstance(msg.get("type"), str):
            raise ValueError(f"a message must be a dict with a string \"type\", got {msg!r}")
        text = dumps(msg)
        try:
            self._need().send(text)
        except ConnectionClosed as exc:
            raise _closed_error(exc, before_welcome=False) from None

    def gaze(self, slot: str | None) -> None:
        """Make the fake board carry ``slot``'s tone; None means looking at nothing.

        The server accepts gaze only with --board fake (``self.fake``) and answers an error
        otherwise.
        """
        if slot is not None:
            _check_slot(slot, "gaze slot")
        self.send({"type": "gaze", "slot": slot})

    def recv(self, timeout: float | None = 5.0) -> dict[str, Any] | None:
        """The next message, or None when none arrives within ``timeout`` seconds.

        None as ``timeout`` waits forever. Frames that are not JSON objects with a string
        "type" are noted on stderr and skipped. ServerError once the socket has closed.
        """
        conn = self._need()
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                data = conn.recv(timeout=left)
            except TimeoutError:
                return None
            except ConnectionClosed as exc:
                raise _closed_error(exc, before_welcome=False) from None
            msg = _parse(data)
            if msg is None:
                continue
            self._track(msg)
            return msg

    def until(self, type_: str, timeout: float = 10.0,
              pred: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
        """Receive until a message of ``type_`` (for which ``pred`` holds, if given) arrives.

        Other messages are dropped, after they update ``phase`` and ``layout`` and trigger
        onsets. TimeoutError when none arrives within ``timeout`` seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            msg = self.recv(timeout=left) if left > 0 else None
            if msg is None:
                raise TimeoutError(f"no {type_!r} message within {timeout:g} s")
            if msg["type"] == type_ and (pred is None or pred(msg)):
                return msg

    def _track(self, msg: dict[str, Any]) -> None:
        """Remember the phase, the layout and (code mode) the training and the last decision,
        and send the onset a drawing client would send."""
        kind = msg["type"]
        if kind == "train":
            self.train = msg
            return
        if kind == "trained":
            self.trained = msg
            return
        if kind == "decision":
            self.decision = msg
            return
        if kind == "phase":
            self.phase = msg
        elif kind == "layout":
            self.layout = msg
        else:
            return
        if self.auto_onset and self.role == "controller":
            self._maybe_onset()

    def _maybe_onset(self) -> None:
        lay, ph = self.layout, self.phase
        if lay is None or ph is None or not lay.get("targets"):
            return
        if ph.get("phase") not in FLICKER_PHASES or ph.get("flicker") is not True:
            return
        layout_id = lay.get("layout_id")
        if isinstance(layout_id, bool) or not isinstance(layout_id, int) or layout_id < 0:
            _note(f"no onset for a layout without a valid layout_id: {layout_id!r}")
            return
        if layout_id == self._onset_layout:
            return
        self._onset_layout = layout_id
        self.send({"type": "onset", "layout_id": layout_id, "t_client": time.time()})
        self.onsets += 1


# -- command line -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.server.client", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=URL, help=f"the server's WebSocket URL (default {URL})")
    auth = p.add_mutually_exclusive_group()
    auth.add_argument("--token", default=None,
                      help="the token the server printed at startup (visible to other users "
                           "in the process list; prefer --token-file)")
    auth.add_argument("--token-file", metavar="PATH", default=None,
                      help="read the token from this file (the server's --token-file); "
                           "leave both out for a server started with --no-auth")
    p.add_argument("--role", choices=ROLES, default="controller",
                   help="controller (owns the selections) or viewer (only watches); "
                        "default controller")
    p.add_argument("--slots", default="yes,no",
                   help="comma-separated slot names for a controller (default yes,no)")
    p.add_argument("--refresh-hz", type=float, default=60.0,
                   help="display refresh rate to report; this client draws nothing, so the "
                        "default 60 passes the server's check")
    p.add_argument("--takeover", action="store_true",
                   help="replace a controller that is already connected")
    p.add_argument("--gaze", metavar="SLOT", default=None,
                   help="with --board fake on the server: look at this slot the whole time")
    p.add_argument("--count", type=int, default=None,
                   help="exit after this many selections (default: run until Ctrl-C)")
    p.add_argument("--seconds", type=float, default=None,
                   help="exit after this many seconds (default: run until Ctrl-C)")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="seconds to wait for the welcome (default 5)")
    p.add_argument("--json", action="store_true",
                   help="print each selection as a JSON line instead of text")
    p.add_argument("--decisions", action="store_true",
                   help="code mode: also print every decision as the confidence bars")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject bad values through ``p.error`` (exit code 2) and fill in args.slot_names."""
    try:
        _check_url(args.url)
    except ValueError as exc:
        p.error(f"--url: {exc}")
    names = [s.strip() for s in args.slots.split(",")]
    if any(not s for s in names):
        p.error(f"--slots needs comma-separated names without empty ones, such as yes,no; "
                f"got {args.slots!r}")
    if any(len(s) > SLOT_CHARS for s in names):
        p.error(f"--slots names must be at most {SLOT_CHARS} characters long")
    if len(set(names)) != len(names):
        p.error(f"--slots must not repeat a name, got {args.slots!r}")
    args.slot_names = tuple(names)
    if not (math.isfinite(args.refresh_hz) and args.refresh_hz > 0):
        p.error(f"--refresh-hz must be a positive number of Hz, got {args.refresh_hz}")
    if args.count is not None and args.count < 1:
        p.error(f"--count must be at least 1, got {args.count}")
    if args.seconds is not None and not (math.isfinite(args.seconds) and args.seconds > 0):
        p.error(f"--seconds must be positive, got {args.seconds}")
    if not (math.isfinite(args.timeout) and args.timeout > 0):
        p.error(f"--timeout must be positive, got {args.timeout}")
    if args.role == "viewer":
        if args.gaze is not None:
            p.error("--gaze needs --role controller; a viewer may only watch")
        if args.takeover:
            p.error("--takeover needs --role controller; viewers never replace anyone")
    if args.gaze is not None and args.gaze not in names:
        p.error(f"--gaze must name one of the slots ({args.slots}), got {args.gaze!r}")
    if args.token is not None and not args.token:
        p.error("--token must not be empty; leave it out for a server started with --no-auth")


def read_token(path: str) -> str:
    """The token in ``path`` without surrounding whitespace; ValueError when it is empty."""
    token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"the token file {path} is empty; copy the server's token file, or "
                         f"start the server with --no-auth and leave --token-file out")
    return token


def _show_selection(msg: Mapping[str, Any], as_json: bool) -> str:
    if as_json:
        return json.dumps(msg, separators=(",", ":"))
    score = msg.get("score")
    shown = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
    return f"{msg.get('slot')}  score {shown}  layout {msg.get('layout_id')}"


def _bar(value: Any, cells: int = 8) -> str:
    """The standard's confidence bar: ``cells`` characters filled for a score in 0..1."""
    v = float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0
    n = int(round(min(1.0, max(0.0, v)) * cells))
    return "█" * n + "░" * (cells - n)


def decision_lines(msg: Mapping[str, Any]) -> list[str]:
    """A "decision" as the standard shows it (docs/CVEP-STANDARD.md section 3): one line per
    slot ("YES  ███░░░░░ +0.42 <") and the status line ("lead 0.31   choice: YES")."""
    scores = msg.get("scores") or {}
    width = max((len(str(s)) for s in scores), default=0)
    lines = []
    for slot, score in scores.items():
        shown = f"{score:+.2f}" if isinstance(score, (int, float)) else "  -  "
        best = " <" if slot == msg.get("raw") else ""
        lines.append(f"{str(slot).upper():<{width}} {_bar(score)} {shown}{best}")
    lead = msg.get("lead")
    gap = f"lead {lead:.2f}" if isinstance(lead, (int, float)) else "lead  -  "
    committed = msg.get("committed")
    choice = ("no choice yet" if committed is None else
              f"choice: {str(committed).upper()}" + (" *" if msg.get("changed") else ""))
    lines.append(f"{gap}   {choice}")
    return lines


def trained_text(msg: Mapping[str, Any]) -> str:
    """"template ready: 83 % on held-out cycles (chance 33 %)"."""
    acc, chance = msg.get("accuracy"), msg.get("chance")
    shown = "?" if not isinstance(acc, (int, float)) else f"{round(100 * acc)}"
    ch = "?" if not isinstance(chance, (int, float)) else f"{round(100 * chance)}"
    return f"template ready: {shown} % on held-out cycles (chance {ch} %)"


def _loop(client: Client, args: argparse.Namespace) -> int:
    """Print selections until --count, --seconds, Ctrl-C or the server stops."""
    end = None if args.seconds is None else time.monotonic() + args.seconds
    count = 0
    stopping = False
    while True:
        left = 1.0 if end is None else end - time.monotonic()
        if left <= 0:
            return 0
        try:
            msg = client.recv(timeout=min(left, 1.0))
        except ServerError as exc:
            if stopping:
                _note("the server stopped")
                return 0
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if msg is None:
            continue
        kind = msg["type"]
        if kind == "selection":
            print(_show_selection(msg, args.json), flush=True)
            count += 1
            if args.count is not None and count >= args.count:
                return 0
        elif kind == "phase":
            stopping = msg.get("phase") == "stop"
            text = msg.get("text") or ""
            _note(f"phase {msg.get('phase')}{': ' + text if text else ''}")
        elif kind == "train":
            cue = (client.phase or {}).get("cue") or "the first"
            _note(f"training: keep looking at {str(cue).upper()}   cycle "
                  f"{msg.get('cycle')}/{msg.get('of')}")
        elif kind == "trained":
            _note(trained_text(msg))
        elif kind == "decision" and args.decisions:
            print("\n".join(decision_lines(msg)), flush=True)
        elif kind == "error":
            fatal = " (fatal)" if msg.get("fatal") is True else ""
            _note(f"error {msg.get('code')}{fatal}: {msg.get('message', '')}")


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    token = args.token
    if args.token_file is not None:
        try:
            token = read_token(args.token_file)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            print(f"error: cannot use --token-file: {exc}", file=sys.stderr)
            return 2
    controller = args.role == "controller"
    client = Client(args.url, token, role=args.role, mode="slots" if controller else None,
                    slots=args.slot_names if controller else None,
                    refresh_hz=args.refresh_hz, takeover=args.takeover)
    try:
        try:
            welcome = client.connect(timeout=args.timeout)
        except ServerError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:   # refused, unreachable, or TimeoutError
            print(f"error: cannot connect to {args.url}: {exc}", file=sys.stderr)
            return 1
        board = "fake board" if client.fake else "real board"
        stim = ("c-VEP code" if client.paradigm == "code"
                else f"freqs {welcome.get('freqs')}")
        _note(f"connected to {args.url} as {args.role} ({board}, {stim})")
        if args.gaze is not None:
            if client.fake:
                try:
                    client.gaze(args.gaze)
                except ServerError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
            else:
                _note("--gaze ignored: the server does not run --board fake")
        return _loop(client, args)
    except KeyboardInterrupt:
        _note("stopped")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
