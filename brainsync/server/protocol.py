"""The BrainSync client/server wire protocol (v3): constants, parsers and message builders.

    msg = parse_frame(text)                       # ProtocolError("protocol") if malformed
    if msg["type"] == "hello":
        hello = parse_hello(msg, max_slots=4)     # ProtocolError("bad_hello" | "too_many_slots")
    else:
        msg = validate_client(msg)                # a cleaned copy, or ProtocolError("protocol")
    send(dumps(layout(7, "slots", [Target("yes", 15.0, 0.0, "Yes", "slot")])))

PROTOCOL-v3.md at the repo root is the contract and this module follows it field by field.
The module is pure: no IO, no threads, nothing beyond the standard library, so the transport,
the session, the Python client and the tests all share one definition of every message.

Client messages are checked strictly, because they come from the network. A field of the
wrong type, a bool where an integer belongs, NaN or infinity, a missing required field or an
unknown type raises ProtocolError, whose ``code`` is the error code to send back and whose
message says what to fix. Those messages never repeat the auth token and show at most a short
excerpt of a bad value. Unknown fields are dropped from the cleaned copy.

The builders return a fresh dict per call, exactly as PROTOCOL-v3 section 3 shows. They raise
ValueError for arguments that would break the contract (an unknown phase or error code,
flicker outside "run", a NaN score, overlapping patches), since those are bugs on the server
side and must not reach a client. ``dumps`` encodes with allow_nan=False for the same reason:
browsers cannot parse NaN.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import numbers
import operator
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PROTOCOL = 3
DEFAULT_PORT = 8765
MAX_FRAME = 4 * 1024 * 1024
CLOSE_REPLACED = 4000        # another controller took over; do not reconnect by yourself
CLOSE_BAD_TOKEN = 4001
CLOSE_BUSY = 4002
CLOSE_HELLO_FIRST = 4003
CLOSE_HELLO_TIMEOUT = 4008

PHASES = ("wait", "calibrate", "train", "run", "pause", "stop")
FLICKER_PHASES = ("train", "run")    # the phases whose flicker may be true
STAGES = ("slots", "pick", "confirm", "idle", "train")
KINDS = ("slot", "button", "more", "confirm", "back")
BOARD_STATES = ("unknown", "connecting", "streaming", "no-data", "error")
ROLES = ("controller", "viewer")
MODES = ("slots", "point")
# The stimulus paradigm (docs/CVEP-STANDARD.md): "code" (c-VEP: one m-sequence, a lag per
# target; the default) or "freq" (f-VEP: a frequency per target, as PROTOCOL-v3 began).
PARADIGMS = ("code", "freq")
CODE_MAX_SLOTS = 3           # the c-VEP standard's three targets
CODE_FILL = "#ffffff"        # the colour of a lit (bit 1) square; bit 0 is black
MEDIA_TYPES = ("image/jpeg", "image/png")
ERROR_CODES = frozenset({"bad_token", "busy", "bad_hello", "refresh", "too_many_slots",
                         "protocol", "not_allowed", "detect_unavailable", "scan_failed",
                         "internal"})
CLIENT_TYPES = frozenset({"hello", "onset", "cycle", "frames", "ping", "pause", "resume", "bye",
                          "gaze", "scan", "clicked", "rescan"})
VIEWER_TYPES = frozenset({"ping", "bye"})

LATENCY_S = 0.14
ONSET_TIMEOUT_S = 2.0
COOLDOWN_S = 1.5
SETTLE_MS = 800
# Display refresh rates a controller may report, each within REFRESH_TOLERANCE_HZ; the first is
# the nominal one (welcome.refresh_hz). The luminance is frame-indexed, so a display at any of
# them shows every frequency in time as long as its measured refresh is used.
REFRESH_RATES = (60.0, 90.0, 120.0, 144.0)
REFRESH_TOLERANCE_HZ = 1.0

MAX_HINTS = 200
# scan.source (v3.1): "screenshot" (an image, the default) or "ax" (a desktop client's
# accessibility tree, image optional).
SCAN_SOURCES = ("screenshot", "ax")
MAX_HINT_ACTIONS = 8         # an ax hint's AX action names, each cut to HINT_ACTION_CHARS
HINT_ACTION_CHARS = 40
# hello.client fields kept (strings, cut to 64 characters). "instance" is a random id the client
# keeps for its lifetime, so the server can tell the same client reconnecting from another one.
CLIENT_FIELDS = ("name", "kind", "version", "instance")
HINT_TEXT_CHARS = 120
SLOT_CHARS = 32
MAX_IMAGE_PX = 8192          # sanity bound; clients scale the long edge down to 1280
EPS = 1e-6                   # float slack for rects that end exactly at the viewport edge

# Which target kinds each layout stage may hold.
_STAGE_KINDS = {"slots": ("slot",), "pick": ("button", "more"), "confirm": ("confirm", "back"),
                "idle": (), "train": ("slot", "button", "more")}
_STAGE_KINDS_TEXT = {k: ", ".join(v) for k, v in _STAGE_KINDS.items()}
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_MAGIC = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}


class ProtocolError(ValueError):
    """A client message that breaks PROTOCOL-v3; ``code`` is the error code to answer with."""

    def __init__(self, code: str, message: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"error code must be one of {sorted(ERROR_CODES)}, got {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message


# -- value checks -----------------------------------------------------------------------------

def _show(value: Any) -> str:
    """A short repr for error messages, so a huge bad value is not echoed back whole."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _finite(value: Any) -> float | None:
    """``value`` as a float when it is a finite JSON number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except OverflowError:              # an integer with hundreds of digits
        return None
    return f if math.isfinite(f) else None


def _need(obj: Mapping[str, Any], key: str, where: str, code: str) -> Any:
    if key not in obj:
        raise ProtocolError(code, f"{where} needs a \"{key}\" field")
    return obj[key]


def _int(value: Any, name: str, code: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(code, f"{name} must be an integer, got {_show(value)}")
    if minimum is not None and value < minimum:
        raise ProtocolError(code, f"{name} must be at least {minimum}, got {value}")
    return value


def _num(value: Any, name: str, code: str, *, positive: bool = False,
         minimum: float | None = None) -> float:
    f = _finite(value)
    if f is None:
        raise ProtocolError(code, f"{name} must be a finite number, got {_show(value)}")
    if positive and f <= 0:
        raise ProtocolError(code, f"{name} must be positive, got {f:g}")
    if minimum is not None and f < minimum:
        raise ProtocolError(code, f"{name} must be at least {minimum:g}, got {f:g}")
    return f


def _bool(value: Any, name: str, code: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(code, f"{name} must be true or false, got {_show(value)}")
    return value


def _str(value: Any, name: str, code: str, *, clip: int | None = None) -> str:
    """A string field; None counts as "" (optional text), longer text is cut to ``clip``."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProtocolError(code, f"{name} must be a string, got {_show(value)}")
    return value if clip is None else value[:clip]


def _dict(value: Any, name: str, code: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(code, f"{name} must be an object, got {_show(value)}")
    return value


def _slot_name(value: Any, name: str, code: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= SLOT_CHARS:
        raise ProtocolError(code, f"{name} must be a string of 1-{SLOT_CHARS} characters, "
                                  f"got {_show(value)}")
    return value


def _viewport(value: Any, name: str, code: str) -> dict[str, float]:
    """{"w", "h", "dpr"} in CSS pixels; dpr defaults to 1 when the client leaves it out."""
    v = _dict(value, name, code)
    return {"w": _num(_need(v, "w", name, code), f"{name}.w", code, positive=True),
            "h": _num(_need(v, "h", name, code), f"{name}.h", code, positive=True),
            "dpr": _num(v.get("dpr", 1.0), f"{name}.dpr", code, positive=True)}


# -- client -> server -------------------------------------------------------------------------

def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse_frame(text: str | bytes) -> dict[str, Any]:
    """A client frame as a dict with a string "type"; ProtocolError("protocol") otherwise."""
    if isinstance(text, (bytes, bytearray, memoryview)):
        raise ProtocolError("protocol", f"binary frames are ignored ({len(text)} bytes); "
                                        f"send each message as a JSON text frame")
    if not isinstance(text, str):
        raise ProtocolError("protocol", f"a frame must be JSON text, got {type(text).__name__}")
    try:
        msg = json.loads(text, parse_constant=_no_constants)
    except (ValueError, RecursionError):   # RecursionError: arrays nested ~100000 deep
        raise ProtocolError("protocol", f"the message is not JSON (NaN and Infinity are not "
                                        f"allowed either): {text[:40]!r}") from None
    if not isinstance(msg, dict):
        raise ProtocolError("protocol", "a message must be a JSON object with a string "
                                        "\"type\"")
    if not isinstance(msg.get("type"), str):
        raise ProtocolError("protocol", "the message has no string \"type\" field")
    return msg


@dataclass(frozen=True)
class Hello:
    """A checked hello. ``token`` is None when the client sent none (servers run --no-auth)."""

    role: str
    token: str | None
    mode: str | None
    slots: tuple[str, ...] | None
    refresh_hz: float | None
    accepted: bool
    takeover: bool
    client: dict
    viewport: dict | None

    def to_dict(self) -> dict[str, Any]:
        """The hello as a message dict without the token, for events and logs."""
        return {"type": "hello", "protocol": PROTOCOL, "role": self.role,
                "takeover": self.takeover, "client": dict(self.client), "mode": self.mode,
                "slots": None if self.slots is None else list(self.slots),
                "refresh_hz": self.refresh_hz, "accepted": self.accepted,
                "viewport": None if self.viewport is None else dict(self.viewport)}


def parse_hello(msg: Mapping[str, Any], max_slots: int) -> Hello:
    """Check a hello (PROTOCOL-v3 section 2). The token is only type-checked, not compared.

    ProtocolError("too_many_slots") when a slots-mode hello names more than ``max_slots``
    slots, ProtocolError("bad_hello") for anything else wrong.
    """
    if isinstance(max_slots, bool) or not isinstance(max_slots, int) or max_slots < 1:
        raise ValueError(f"max_slots must be a positive integer, got {max_slots!r}")
    code = "bad_hello"
    if not isinstance(msg, Mapping) or msg.get("type") != "hello":
        raise ProtocolError(code, "the first message must be a hello")
    version = msg.get("protocol")
    if isinstance(version, bool) or version != PROTOCOL:
        raise ProtocolError(code, f"this server speaks protocol {PROTOCOL} but the hello says "
                                  f"protocol {_show(version)}; update the client")
    role = msg.get("role")
    if role not in ROLES:
        raise ProtocolError(code, f"hello.role must be \"controller\" or \"viewer\", "
                                  f"got {_show(role)}")
    token = msg.get("token")
    if token is not None and not isinstance(token, str):
        raise ProtocolError(code, "hello.token must be a string (paste the token the server "
                                  "printed at startup)")
    takeover = _bool(msg.get("takeover", False), "hello.takeover", code)
    raw_client = msg.get("client")
    client = {} if raw_client is None else _dict(raw_client, "hello.client", code)
    client = {k: v[:64] for k, v in client.items()
              if k in CLIENT_FIELDS and isinstance(v, str)}
    controller = role == "controller"

    mode = msg.get("mode")
    if mode is None and controller:
        raise ProtocolError(code, "a controller's hello needs \"mode\": \"slots\" or \"point\"")
    if mode is not None and mode not in MODES:
        raise ProtocolError(code, f"hello.mode must be \"slots\" or \"point\", got {_show(mode)}")

    raw_slots = msg.get("slots")
    slots: tuple[str, ...] | None = None
    if mode == "slots" and (raw_slots is not None or controller):
        if not isinstance(raw_slots, list) or not raw_slots:
            raise ProtocolError(code, "slots mode needs \"slots\": a list of 1 to "
                                      f"{max_slots} names, such as [\"yes\", \"no\"]")
        if len(raw_slots) > max_slots:
            raise ProtocolError("too_many_slots",
                                f"this server has {max_slots} targets, so a client may "
                                f"name at most {max_slots} slots, not {len(raw_slots)}")
        slots = tuple(_slot_name(s, f"hello.slots[{i}]", code) for i, s in enumerate(raw_slots))
        if len(set(slots)) != len(slots):
            raise ProtocolError(code, "hello.slots must not repeat a name")
    elif raw_slots is not None:
        raise ProtocolError(code, f"hello.slots is used in slots mode only; send \"slots\": "
                                  f"null with mode {_show(mode)}")

    raw_hz = msg.get("refresh_hz")
    if raw_hz is None and not controller:
        refresh_hz = None
    elif raw_hz is None:
        raise ProtocolError(code, "a controller's hello needs \"refresh_hz\", the measured "
                                  "display refresh rate")
    else:
        refresh_hz = _num(raw_hz, "hello.refresh_hz", code, positive=True)

    accepted = _bool(msg.get("accepted", False), "hello.accepted", code)
    if controller and not accepted:
        raise ProtocolError(code, "a controller must show the photosensitivity warning and "
                                  "send \"accepted\": true only after the user accepts it")
    raw_viewport = msg.get("viewport")
    viewport = None if raw_viewport is None else _viewport(raw_viewport, "hello.viewport", code)
    return Hello(role=role, token=token, mode=mode, slots=slots, refresh_hz=refresh_hz,
                 accepted=accepted, takeover=takeover, client=client, viewport=viewport)


def _image(value: Any) -> dict[str, Any]:
    code = "protocol"
    img = _dict(value, "scan.image", code)
    media_type = _need(img, "media_type", "scan.image", code)
    if media_type not in MEDIA_TYPES:
        raise ProtocolError(code, f"scan.image.media_type must be one of "
                                  f"{', '.join(MEDIA_TYPES)}, got {_show(media_type)}")
    data = _need(img, "data", "scan.image", code)
    if not isinstance(data, str) or not data:
        raise ProtocolError(code, "scan.image.data must be a non-empty base64 string")
    if data.startswith("data:"):
        raise ProtocolError(code, "scan.image.data must be bare base64; drop the "
                                  "\"data:image/...;base64,\" prefix of the data URL")
    if len(data) % 4 or _BASE64.fullmatch(data) is None:
        raise ProtocolError(code, "scan.image.data is not valid base64 (standard alphabet, "
                                  "padded, no line breaks)")
    try:
        head = base64.b64decode(data[:16], validate=True)
    except (binascii.Error, ValueError):
        raise ProtocolError(code, "scan.image.data is not valid base64") from None
    magic = _MAGIC[media_type]
    n = min(len(head), len(magic))
    if head[:n] != magic[:n]:
        raise ProtocolError(code, f"scan.image.data does not hold a {media_type} image; "
                                  f"set media_type to match the encoded bytes")
    w = _int(_need(img, "w", "scan.image", code), "scan.image.w", code, minimum=1)
    h = _int(_need(img, "h", "scan.image", code), "scan.image.h", code, minimum=1)
    if max(w, h) > MAX_IMAGE_PX:
        raise ProtocolError(code, f"scan.image is {w}x{h} px; scale it so the long edge is at "
                                  f"most 1280 px")
    return {"media_type": media_type, "data": data, "w": w, "h": h}


def _hint_actions(value: Any, where: str, code: str) -> list[str]:
    """An ax hint's AX action names: the first MAX_HINT_ACTIONS, each cut to 40 characters."""
    if not isinstance(value, list):
        raise ProtocolError(code, f"{where}.actions must be a list of AX action names such as "
                                  f"[\"AXPress\"], got {_show(value)}")
    out = []
    for i, name in enumerate(value[:MAX_HINT_ACTIONS]):
        if not isinstance(name, str):
            raise ProtocolError(code, f"{where}.actions[{i}] must be a string, got "
                                      f"{_show(name)}")
        out.append(name[:HINT_ACTION_CHARS])
    return out


def _hints(value: Any, ax: bool = False) -> list[dict[str, Any]]:
    """Up to MAX_HINTS checked hints in viewport CSS px (window points for an ax scan); later
    ones are dropped. Only ax hints keep ``actions`` and ``enabled``."""
    code = "protocol"
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProtocolError(code, f"scan.hints must be a list, got {_show(value)}")
    hints: list[dict[str, Any]] = []
    seen: set[int] = set()
    for i, raw in enumerate(value[:MAX_HINTS]):
        where = f"scan.hints[{i}]"
        h = _dict(raw, where, code)
        hint_id = _int(_need(h, "id", where, code), f"{where}.id", code, minimum=0)
        if hint_id in seen:
            raise ProtocolError(code, f"{where}.id {hint_id} is used twice; hint ids must be "
                                      f"unique")
        seen.add(hint_id)
        hints.append({
            "id": hint_id,
            "x": _num(_need(h, "x", where, code), f"{where}.x", code),
            "y": _num(_need(h, "y", where, code), f"{where}.y", code),
            "w": _num(_need(h, "w", where, code), f"{where}.w", code, minimum=0.0),
            "h": _num(_need(h, "h", where, code), f"{where}.h", code, minimum=0.0),
            "role": _str(h.get("role"), f"{where}.role", code, clip=40),
            "text": _str(h.get("text"), f"{where}.text", code, clip=HINT_TEXT_CHARS),
            "tag": _str(h.get("tag"), f"{where}.tag", code, clip=40)})
        if h.get("submit") is True:        # optional: the hint is a form's submit control
            hints[-1]["submit"] = True
        if ax:
            if h.get("actions") is not None:
                hints[-1]["actions"] = _hint_actions(h["actions"], where, code)
            if h.get("enabled") is not None:
                hints[-1]["enabled"] = _bool(h["enabled"], f"{where}.enabled", code)
    return hints


def _check_onset(m: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {"type": "onset",
            "layout_id": _int(_need(m, "layout_id", "onset", code), "onset.layout_id", code,
                              minimum=0),
            "t_client": _num(_need(m, "t_client", "onset", code), "onset.t_client", code)}


def _check_cycle(m: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {"type": "cycle",
            "layout_id": _int(_need(m, "layout_id", "cycle", code), "cycle.layout_id", code,
                              minimum=0),
            "n": _int(_need(m, "n", "cycle", code), "cycle.n", code, minimum=0),
            "t_client": _num(_need(m, "t_client", "cycle", code), "cycle.t_client", code)}


def _check_frames(m: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {"type": "frames",
            "refresh_hz": _num(_need(m, "refresh_hz", "frames", code), "frames.refresh_hz",
                               code, positive=True),
            "dropped": _int(_need(m, "dropped", "frames", code), "frames.dropped", code,
                            minimum=0)}


def _check_ping(m: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {"type": "ping",
            "ping_id": _int(_need(m, "ping_id", "ping", code), "ping.ping_id", code),
            "t_client": _num(_need(m, "t_client", "ping", code), "ping.t_client", code)}


def _check_gaze(m: Mapping[str, Any], code: str) -> dict[str, Any]:
    slot = _need(m, "slot", "gaze", code)
    return {"type": "gaze",
            "slot": None if slot is None else _slot_name(slot, "gaze.slot", code)}


def _scan_app(value: Any, code: str) -> dict[str, Any]:
    """An ax scan's front app: {bundle_id, name, pid}; the strings may be empty."""
    app = _dict(value, "scan.app", code)
    return {"bundle_id": _str(_need(app, "bundle_id", "scan.app", code), "scan.app.bundle_id",
                              code, clip=255),
            "name": _str(_need(app, "name", "scan.app", code), "scan.app.name", code, clip=200),
            "pid": _int(_need(app, "pid", "scan.app", code), "scan.app.pid", code, minimum=0)}


def _check_scan(m: Mapping[str, Any], code: str) -> dict[str, Any]:
    source = m.get("source", "screenshot")
    if source not in SCAN_SOURCES:
        raise ProtocolError(code, f"scan.source must be \"screenshot\" or \"ax\", got "
                                  f"{_show(source)}")
    ax = source == "ax"
    msg: dict[str, Any] = {"type": "scan"}
    msg["scan_id"] = _int(_need(m, "scan_id", "scan", code), "scan.scan_id", code, minimum=0)
    if ax:
        msg["source"] = "ax"
        msg["image"] = None if m.get("image") is None else _image(m["image"])
    else:
        msg["image"] = _image(_need(m, "image", "scan", code))
    msg["viewport"] = _viewport(_need(m, "viewport", "scan", code), "scan.viewport", code)
    msg["url"] = _str(m.get("url"), "scan.url", code, clip=2048)
    msg["title"] = _str(m.get("title"), "scan.title", code, clip=200)
    msg["hints"] = _hints(m.get("hints"), ax=ax)
    if ax:
        msg["app"] = _scan_app(_need(m, "app", "scan (source \"ax\")", code), code)
        window = _dict(_need(m, "window", "scan (source \"ax\")", code), "scan.window", code)
        msg["window"] = {"title": _str(_need(window, "title", "scan.window", code),
                                       "scan.window.title", code, clip=200)}
    return msg


def _check_clicked(m: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {"type": "clicked",
            "click_id": _int(_need(m, "click_id", "clicked", code), "clicked.click_id", code,
                             minimum=0),
            "ok": _bool(_need(m, "ok", "clicked", code), "clicked.ok", code),
            "method": _str(m.get("method"), "clicked.method", code, clip=64),
            "detail": _str(m.get("detail"), "clicked.detail", code, clip=500)}


def _check_bare(m: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {"type": m["type"]}


_CHECKS = {"onset": _check_onset, "cycle": _check_cycle, "frames": _check_frames, "ping": _check_ping,
           "pause": _check_bare, "resume": _check_bare, "bye": _check_bare,
           "gaze": _check_gaze, "scan": _check_scan, "clicked": _check_clicked,
           "rescan": _check_bare}


def validate_client(msg: Mapping[str, Any]) -> dict[str, Any]:
    """A checked, cleaned copy of a client message other than hello (PROTOCOL-v3 section 2).

    Only the documented fields are kept. Scan hints beyond 200 are dropped and their text is
    cut to 120 characters. A scan with ``source`` "ax" (v3.1) comes back with "source": "ax",
    its ``app`` and ``window`` and an ``image`` that may be None, and its hints keep
    ``actions`` and ``enabled``; a screenshot scan comes back exactly as in v3.
    ProtocolError("protocol") for an unknown type or a bad field.
    """
    code = "protocol"
    if not isinstance(msg, Mapping) or not isinstance(msg.get("type"), str):
        raise ProtocolError(code, "a message must be a JSON object with a string \"type\"")
    kind = msg["type"]
    if kind == "hello":
        raise ProtocolError(code, "hello is only valid as the first message of a connection; "
                                  "reconnect to send a new one")
    check = _CHECKS.get(kind)
    if check is None:
        raise ProtocolError(code, f"unknown message type {_show(kind)}; clients may send "
                                  f"{', '.join(sorted(CLIENT_TYPES))}")
    return check(msg, code)


def check_refresh(refresh_hz: Any, want: float | Sequence[float], tol: float) -> str | None:
    """None when ``refresh_hz`` is within ``tol`` of ``want`` (one rate, or any of several),
    else a sentence saying what to do."""
    hz = _finite(refresh_hz)
    if hz is None or hz <= 0:
        return ("The client did not report a usable display refresh rate; measure it and "
                "send it as refresh_hz in hello.")
    rates = [float(want)] if isinstance(want, numbers.Real) else [float(r) for r in want]
    if any(abs(hz - r) <= tol + 1e-9 for r in rates):
        return None
    if len(rates) == 1:
        return (f"The display refreshes at {hz:.2f} Hz, but the flicker needs {rates[0]:g} ± "
                f"{tol:g} Hz. Set the display to {rates[0]:g} Hz and reconnect, or start the "
                f"server with --ignore-refresh.")
    names = [f"{r:g}" for r in rates]
    return (f"The display refreshes at {hz:.2f} Hz, but the flicker needs one of "
            f"{', '.join(names[:-1])} or {names[-1]} Hz (± {tol:g} Hz). Set the display to one "
            f"of those rates and reconnect, or start the server with --ignore-refresh.")


# -- layout pieces ----------------------------------------------------------------------------

def _builder_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{name} must be a number, got {_show(value)}")
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a number, got {_show(value)}") from None
    if not math.isfinite(f):
        raise ValueError(f"{name} must be finite, got {f}; check the decoder output")
    return f


def _builder_int(value: Any, name: str, minimum: int | None = 0) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    try:
        n = operator.index(value)        # accepts numpy integers, refuses floats
    except TypeError:
        raise ValueError(f"{name} must be an integer, got {_show(value)}") from None
    if minimum is not None and n < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {n}")
    return n


@dataclass(frozen=True)
class Rect:
    """A rectangle in normalized viewport coordinates: x right, y down, all inside [0, 1]."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "w", "h"):
            v = _builder_float(getattr(self, name), f"Rect.{name}")
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"Rect.{name} must be in [0, 1] (normalized to the "
                                 f"viewport), got {v:g}; clamp the patch into the viewport")
            object.__setattr__(self, name, v)
        if self.x + self.w > 1.0 + EPS or self.y + self.h > 1.0 + EPS:
            raise ValueError(f"Rect {self.to_dict()} reaches past the viewport; clamp it so "
                             f"x + w <= 1 and y + h <= 1")

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def overlaps(self, other: Rect, gap: float = 0.0) -> bool:
        """True when the rects overlap or come closer than ``gap``; touching is not overlap.

        Distances within EPS of the limit count as far enough, so float rounding in a packing
        (0.1 + 0.2 is 0.30000000000000004) does not turn touching patches into overlapping ones.
        """
        if not math.isfinite(gap) or gap < 0:
            raise ValueError(f"gap must be a non-negative number, got {gap!r}")
        g = gap - EPS
        return (self.x < other.x + other.w + g and other.x < self.x + self.w + g
                and self.y < other.y + other.h + g and other.y < self.y + self.h + g)

    def to_dict(self) -> dict[str, float]:
        return {"x": round(self.x, 4), "y": round(self.y, 4), "w": round(self.w, 4),
                "h": round(self.h, 4)}


@dataclass(frozen=True)
class Target:
    """One flickering target of a layout; ``rects`` is empty in slots mode.

    A frequency target has ``freq`` (Hz) and no ``lag``; a code target (c-VEP, v3.2) has
    ``freq`` None and ``lag``, the frames its copy of the m-sequence is delayed by.
    """

    slot: str
    freq: float | None
    phase: float
    label: str
    kind: str
    rects: tuple[Rect, ...] = ()
    lag: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.slot, str) or not 1 <= len(self.slot) <= SLOT_CHARS:
            raise ValueError(f"Target.slot must be a string of 1-{SLOT_CHARS} characters, "
                             f"got {self.slot!r}")
        if self.lag is not None:
            if self.freq is not None:
                raise ValueError(f"Target {self.slot!r} has both a frequency and a lag; a code "
                                 f"target has freq None")
            object.__setattr__(self, "lag", _builder_int(self.lag, "Target.lag"))
        else:
            freq = _builder_float(self.freq, "Target.freq")
            if freq <= 0:
                raise ValueError(f"Target.freq must be positive, got {freq:g}")
            object.__setattr__(self, "freq", freq)
        object.__setattr__(self, "phase", _builder_float(self.phase, "Target.phase"))
        if not isinstance(self.label, str):
            raise ValueError(f"Target.label must be a string, got {self.label!r}")
        if self.kind not in KINDS:
            raise ValueError(f"Target.kind must be one of {KINDS}, got {self.kind!r}")
        rects = tuple(self.rects)
        if not all(isinstance(r, Rect) for r in rects):
            raise ValueError("Target.rects must hold Rect objects")
        object.__setattr__(self, "rects", rects)

    def to_dict(self) -> dict[str, Any]:
        out = {"slot": self.slot, "freq": self.freq, "phase": self.phase, "label": self.label,
               "kind": self.kind, "rects": [r.to_dict() for r in self.rects]}
        if self.lag is not None:
            out["lag"] = self.lag
        return out


# -- server -> client -------------------------------------------------------------------------

PING: dict = {"type": "ping"}   # never mutate; send dict(PING)


def dumps(msg: Mapping[str, Any]) -> str:
    """One compact JSON text frame; ValueError for NaN or infinity, which browsers reject."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def welcome(role: str, mode: str | None, *, fake: bool, waveform: str, refresh_hz: float,
            refresh_tolerance_hz: float, freqs: Sequence[float], max_slots: int, detect: bool,
            version: str = "0.1.0",
            refresh_rates: Sequence[float] | None = None, paradigm: str = "freq",
            code: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The answer to a valid hello; ``mode`` is None for a viewer that named none.

    ``refresh_rates`` (the accepted display rates, nominal first) adds the "refresh_rates"
    field; None leaves it out. ``paradigm`` "code" (v3.2) needs ``code``, the object
    ``code_spec`` builds, and has no frequencies; "freq" has no ``code``.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    if mode is not None and mode not in MODES:
        raise ValueError(f"mode must be one of {MODES} or None, got {mode!r}")
    if paradigm not in PARADIGMS:
        raise ValueError(f"paradigm must be one of {PARADIGMS}, got {paradigm!r}")
    if (paradigm == "code") != (code is not None):
        raise ValueError("a code welcome needs the code object, and a freq welcome has none")
    if paradigm == "code" and list(freqs):
        raise ValueError("a code welcome has no frequencies (freqs [])")
    msg = {"type": "welcome", "protocol": PROTOCOL, "role": role, "mode": mode,
           "paradigm": paradigm,
           "server": {"name": "brainsync", "version": str(version)}, "fake": bool(fake),
           "waveform": str(waveform), "refresh_hz": _builder_float(refresh_hz, "refresh_hz"),
           "refresh_tolerance_hz": _builder_float(refresh_tolerance_hz,
                                                  "refresh_tolerance_hz"),
           "freqs": [_builder_float(f, "freqs[i]") for f in freqs],
           "max_slots": _builder_int(max_slots, "max_slots", minimum=1),
           "detect": bool(detect)}
    if refresh_rates is not None:
        rates = [_builder_float(r, "refresh_rates[i]") for r in refresh_rates]
        if not rates or min(rates) <= 0:
            raise ValueError(f"refresh_rates must be positive numbers of Hz, got {rates}")
        msg["refresh_rates"] = rates
    if code is not None:
        msg["code"] = copy_code(code)
    return msg


def code_spec(bits: Sequence[int], lag_frames: int, cycle_frames: int, train_cycles: int,
              fill: str = CODE_FILL) -> dict[str, Any]:
    """The welcome's "code" object (v3.2): the m-sequence, one bit per frame, the lag between
    targets, the frames per cycle, the training cycles and the lit colour."""
    b = [_builder_int(v, "bits[i]") for v in bits]
    if not b or any(v not in (0, 1) for v in b):
        raise ValueError(f"bits must be a non-empty list of 0 and 1, got {_show(list(bits))}")
    lag = _builder_int(lag_frames, "lag_frames", minimum=1)
    cycle = _builder_int(cycle_frames, "cycle_frames", minimum=1)
    if cycle != len(b):
        raise ValueError(f"cycle_frames {cycle} must equal the {len(b)} bits (one bit a frame)")
    if not isinstance(fill, str) or not fill:
        raise ValueError(f"fill must be a CSS colour such as \"#ffffff\", got {fill!r}")
    return {"bits": b, "lag_frames": lag, "cycle_frames": cycle,
            "train_cycles": _builder_int(train_cycles, "train_cycles", minimum=2),
            "fill": fill}


def copy_code(code: Mapping[str, Any]) -> dict[str, Any]:
    """A checked copy of a code object (``code_spec`` again)."""
    return code_spec(code["bits"], code["lag_frames"], code["cycle_frames"],
                     code["train_cycles"], code.get("fill", CODE_FILL))


def error(code: str, message: str, fatal: bool = False) -> dict[str, Any]:
    """An "error"; with ``fatal`` the server closes the connection after sending it."""
    if code not in ERROR_CODES:
        raise ValueError(f"error code must be one of {sorted(ERROR_CODES)}, got {code!r}")
    return {"type": "error", "code": code, "message": str(message), "fatal": bool(fatal)}


def phase(name: str, flicker: bool, seconds: float | None = None,
          text: str = "", *, cue: str | None = None,
          cycles: int | None = None) -> dict[str, Any]:
    """A "phase"; only "run" and "train" may flicker (PROTOCOL-v3 section 3 table).

    "train" (v3.2) names the ``cue`` slot the user looks at and the ``cycles`` it records;
    the other phases have neither field.
    """
    if name not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {name!r}")
    if flicker and name not in FLICKER_PHASES:
        raise ValueError(f"phase {name!r} must not flicker; only \"run\" and \"train\" have "
                         f"flicker true")
    if (name == "train") != (cue is not None) or (cue is None) != (cycles is None):
        raise ValueError("a train phase names its cue slot and cycles; other phases neither")
    msg = {"type": "phase", "phase": name, "flicker": bool(flicker),
           "seconds": None if seconds is None else _builder_float(seconds, "seconds"),
           "text": str(text)}
    if cue is not None:
        msg["cue"] = str(cue)
        msg["cycles"] = _builder_int(cycles, "cycles", minimum=1)
    return msg


def layout(layout_id: int, stage: str, targets: Sequence[Target]) -> dict[str, Any]:
    """A "layout" that replaces the previous one; checks the stage's rules before sending.

    Slots must be unique and so must frequencies (two targets on one frequency cannot be told
    apart). "idle" has no targets, "slots" targets have no rects, and in point mode every
    target has rects that do not overlap any other target's.
    """
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}, got {stage!r}")
    targets = list(targets)
    if not all(isinstance(t, Target) for t in targets):
        raise ValueError("layout targets must be Target objects")
    if stage == "idle" and targets:
        raise ValueError("an idle layout has no targets; use stage \"pick\" to show some")
    if stage != "idle" and not targets:
        raise ValueError(f"a {stage!r} layout needs at least one target; use stage \"idle\" "
                         f"for nothing to show")
    for t in targets:
        if t.kind not in _STAGE_KINDS[stage]:
            raise ValueError(f"stage {stage!r} holds targets of kind "
                             f"{_STAGE_KINDS_TEXT[stage]}, not {t.kind!r} ({t.slot})")
        if stage == "slots" and t.rects:
            raise ValueError(f"slots-mode target {t.slot!r} must have no rects; the client "
                             f"places it")
        if stage != "slots" and not t.rects:
            raise ValueError(f"point-mode target {t.slot!r} needs at least one rect")
    if len({t.slot for t in targets}) != len(targets):
        raise ValueError("layout targets must have unique slots")
    coded = [t.lag is not None for t in targets]
    if any(coded) and not all(coded):
        raise ValueError("a layout's targets are all code targets (lag) or all frequency "
                         "targets (freq), not a mix")
    if all(coded):
        if len({t.lag for t in targets}) != len(targets):
            raise ValueError("layout targets must have unique lags")
    elif len({t.freq for t in targets}) != len(targets):
        raise ValueError("layout targets must have unique frequencies")
    for i, a in enumerate(targets):
        for b in targets[i + 1:]:
            if any(ra.overlaps(rb) for ra in a.rects for rb in b.rects):
                raise ValueError(f"the patches of {a.slot!r} and {b.slot!r} overlap; targets "
                                 f"of one layout must not overlap")
    return {"type": "layout", "layout_id": _builder_int(layout_id, "layout_id"),
            "stage": stage, "targets": [t.to_dict() for t in targets]}


def selection(selection_id: int, layout_id: int, slot: str, t_server: float, score: float,
              scores: Mapping[str, float]) -> dict[str, Any]:
    """A committed decoder pick; scores are baseline-corrected, not probabilities."""
    return {"type": "selection", "selection_id": _builder_int(selection_id, "selection_id"),
            "layout_id": _builder_int(layout_id, "layout_id"), "slot": str(slot),
            "t_server": _builder_float(t_server, "t_server"),
            "score": _builder_float(score, "score"),
            "scores": {str(k): _builder_float(v, f"scores[{k!r}]") for k, v in scores.items()}}


def train_progress(cycle: int, of: int) -> dict[str, Any]:
    """c-VEP training progress (v3.2): ``cycle`` complete cycles recorded of ``of``."""
    n = _builder_int(of, "of", minimum=1)
    c = _builder_int(cycle, "cycle")
    if c > n:
        raise ValueError(f"cycle {c} is past the {n} training cycles")
    return {"type": "train", "cycle": c, "of": n}


def trained(accuracy: float | None, chance: float, cycles: int) -> dict[str, Any]:
    """The c-VEP template is ready (v3.2): its leave-one-cycle-out ``accuracy`` (None when it
    could not be measured) against ``chance``, from ``cycles`` training cycles."""
    acc = None
    if accuracy is not None and math.isfinite(float(accuracy)):
        acc = round(_builder_float(accuracy, "accuracy"), 3)
        if not 0.0 <= acc <= 1.0:
            raise ValueError(f"accuracy must lie in [0, 1], got {acc:g}")
    return {"type": "trained", "accuracy": acc,
            "chance": round(_builder_float(chance, "chance"), 3),
            "cycles": _builder_int(cycles, "cycles", minimum=1)}


def decision(layout_id: int, scores: Mapping[str, float], raw: str | None,
             committed: str | None, lead: float | None, changed: bool) -> dict[str, Any]:
    """One c-VEP decision for the confidence bars (v3.2). ``scores`` are correlations per slot
    (not probabilities), ``raw`` this decision's best slot, ``committed`` the slot the votes
    hold (None: no choice yet), ``lead`` the best score's lead over the runner-up."""
    clean = {str(k): round(_builder_float(v, f"scores[{k!r}]"), 4) for k, v in scores.items()}
    for name, value in (("raw", raw), ("committed", committed)):
        if value is not None and value not in clean:
            raise ValueError(f"decision {name} {value!r} is not one of the scored slots")
    return {"type": "decision", "layout_id": _builder_int(layout_id, "layout_id"),
            "scores": clean, "raw": raw, "committed": committed,
            "lead": None if lead is None else round(_builder_float(lead, "lead"), 4),
            "changed": bool(changed)}


def scan_request(scan_id: int, settle_ms: int = SETTLE_MS) -> dict[str, Any]:
    """Ask the controller for a screenshot after waiting ``settle_ms`` for the page to settle."""
    return {"type": "scan_request", "scan_id": _builder_int(scan_id, "scan_id"),
            "settle_ms": _builder_int(settle_ms, "settle_ms")}


def scan_result(scan_id: int, ok: bool, ms: float, elements: Sequence[Mapping[str, Any]],
                n_candidates: int, summary: str = "", *, cached: bool = False) -> dict[str, Any]:
    """What a detection found, for the HUD and logs; ``n_elements`` is len(elements).

    ``cached`` (v3.1) is true when the ranking came from the server's rank cache instead of
    a model call.
    """
    elements = [dict(e) for e in elements]
    return {"type": "scan_result", "scan_id": _builder_int(scan_id, "scan_id"), "ok": bool(ok),
            "ms": int(round(_builder_float(ms, "ms"))), "n_elements": len(elements),
            "n_candidates": _builder_int(n_candidates, "n_candidates"),
            "summary": str(summary), "elements": elements, "cached": bool(cached)}


def click(click_id: int, x: float, y: float, element: Mapping[str, Any]) -> dict[str, Any]:
    """Ask the controller to click at the normalized viewport point (``x``, ``y``)."""
    xf, yf = _builder_float(x, "x"), _builder_float(y, "y")
    if not (0.0 <= xf <= 1.0 and 0.0 <= yf <= 1.0):
        raise ValueError(f"click point ({xf:g}, {yf:g}) must be inside the viewport, [0, 1]")
    return {"type": "click", "click_id": _builder_int(click_id, "click_id"), "x": xf, "y": yf,
            "element": dict(element)}


def status(board: str, ok: bool, text: str = "", rtt_ms: float | None = None) -> dict[str, Any]:
    """The board's state for the HUD, about once a second; ``rtt_ms`` None when unknown."""
    if board not in BOARD_STATES:
        raise ValueError(f"board must be one of {BOARD_STATES}, got {board!r}")
    return {"type": "status", "board": board, "ok": bool(ok), "text": str(text),
            "rtt_ms": None if rtt_ms is None else int(round(_builder_float(rtt_ms, "rtt_ms")))}


def pong(ping_id: int, t_client: float, t_server: float) -> dict[str, Any]:
    """The answer to a client ping, echoing its id and time next to the server's time."""
    return {"type": "pong", "ping_id": _builder_int(ping_id, "ping_id", minimum=None),
            "t_client": _builder_float(t_client, "t_client"),
            "t_server": _builder_float(t_server, "t_server")}
