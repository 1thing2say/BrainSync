"""The PROTOCOL-v3 message fixture for the iPhone app's v3 client tests (ios/BrainSyncKit).

The phone is a slots-mode controller of ``brainsync.server`` in code mode (c-VEP). This
builds every server -> client message it has to read, each with the builders of
``brainsync/server/protocol.py`` and the server's own pieces (the m-sequence of
``brainsync.vep.live``, ``SlotsMode``'s lagged targets, ``train_layout``, ``train_text``), so
Swift decodes exactly what Python sends. Variants: a code welcome, a freq welcome, a
viewer's welcome without refresh_rates; each phase (train with its cue and cycles); a
slots layout with lags, a point-mode train layout with rects, a freq layout and an idle
one; decisions with and without a commitment; trained with and without an accuracy; the
server's keepalive ping; and one point-mode message (scan_request) the phone must read as
"unknown".

It also holds every client -> server message the phone sends (hello, onset, cycle, frames,
ping, pause, resume, gaze, bye), each checked here with the server's own parsers
(``parse_hello``, ``validate_client``), so the Swift encoder is held to messages the server
accepts.

    python tools/ios_v3_fixture.py           # write the fixture
    python tools/ios_v3_fixture.py --check   # exit 1 if the committed fixture is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # python tools/ios_v3_fixture.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainsync.server import protocol as P  # noqa: E402
from brainsync.server.session import SlotsMode, train_layout, train_text  # noqa: E402
from brainsync.vep.live import BITS, CYCLE_FRAMES, LAG_FRAMES, TRAIN_CYCLES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "ios/BrainSyncKit/Tests/BrainSyncKitTests/Fixtures/v3_messages.json"

# The phone's three position-only slots (the iOS suite: each app gives them a meaning).
SLOTS = ("t0", "t1", "t2")
LAGS = tuple(k * LAG_FRAMES for k in range(len(SLOTS)))
T0 = 1792809200.0  # a fixed Unix time, so the fixture never changes by itself


def code_object() -> dict:
    return P.code_spec(bits=list(BITS), lag_frames=LAG_FRAMES, cycle_frames=CYCLE_FRAMES,
                       train_cycles=TRAIN_CYCLES)


def server_to_client() -> list[dict]:
    welcome_code = P.welcome("controller", "slots", fake=True, waveform="sine", refresh_hz=60.0,
                             refresh_tolerance_hz=P.REFRESH_TOLERANCE_HZ, freqs=[],
                             max_slots=P.CODE_MAX_SLOTS, detect=False, refresh_rates=[60.0],
                             paradigm="code", code=code_object())
    welcome_freq = P.welcome("controller", "slots", fake=False, waveform="sine", refresh_hz=60.0,
                             refresh_tolerance_hz=P.REFRESH_TOLERANCE_HZ,
                             freqs=[15.0, 17.0, 12.0, 20.0], max_slots=4, detect=True,
                             refresh_rates=list(P.REFRESH_RATES), paradigm="freq")
    welcome_viewer = P.welcome("viewer", None, fake=True, waveform="sine", refresh_hz=60.0,
                               refresh_tolerance_hz=P.REFRESH_TOLERANCE_HZ, freqs=[],
                               max_slots=P.CODE_MAX_SLOTS, detect=False, paradigm="code",
                               code=code_object())   # no refresh_rates: means [refresh_hz]
    errors = [
        P.error("bad_token", "wrong token; paste the token the server printed at startup",
                fatal=True),
        P.error("busy", "another controller is connected; reconnect with \"takeover\": true "
                        "to replace it, or connect as a viewer", fatal=True),
        P.error("refresh", P.check_refresh(120.0, [60.0], P.REFRESH_TOLERANCE_HZ) or ""),
        P.error("too_many_slots", "this server has 3 targets, so a client may name at most 3 "
                                  "slots, not 4", fatal=True),
    ]
    phases = [
        P.phase("wait", False, text="Waiting for a controller to connect"),
        P.phase("calibrate", False, seconds=10.0, text="Look at the screen and relax"),
        P.phase("train", True, text=train_text("t0"), cue="t0", cycles=TRAIN_CYCLES),
        P.phase("run", True),
        P.phase("pause", False, text="Paused; press Resume to go on"),
        P.phase("stop", False, text="The BrainSync server stopped"),
    ]
    assert {m["phase"] for m in phases} == set(P.PHASES)
    slots_layout = P.layout(7, "slots", SlotsMode(SLOTS, lags=LAGS).targets)
    point_train = P.layout(8, "train", train_layout({"w": 390, "h": 844, "dpr": 3}, LAG_FRAMES))
    freq_layout = P.layout(9, "slots", SlotsMode(("yes", "no"), (15.0, 17.0)).targets)
    idle = P.layout(10, "idle", [])
    scores = {"t0": 0.42, "t1": 0.11, "t2": -0.03}
    return [
        welcome_code, welcome_freq, welcome_viewer,
        *errors, *phases,
        slots_layout, point_train, freq_layout, idle,
        P.train_progress(0, TRAIN_CYCLES), P.train_progress(12, TRAIN_CYCLES),
        P.trained(0.83, 1 / 3, TRAIN_CYCLES), P.trained(None, 1 / 3, TRAIN_CYCLES),
        P.decision(7, scores, "t0", None, 0.31, False),
        P.decision(7, scores, "t0", "t0", 0.31, True),
        P.selection(12, 7, "t0", T0 + 4.9, 0.42, scores),
        P.status("streaming", True, "posterior RMS 14 uV", 38),
        P.status("no-data", False, "the headset sends no data"),
        P.pong(3, T0 + 1.0, T0 + 1.02),
        dict(P.PING),
        P.scan_request(4),   # point mode only: the phone reads it as unknown
    ]


def client_to_server() -> list[dict]:
    client = {"name": "brainsync-ios", "kind": "ios", "version": "1.0",
              "instance": "8f14e45fceea167a5a36dedd4bea2543"}
    hello = {"type": "hello", "protocol": P.PROTOCOL, "token": "Zx9-abc_DEF123", "role": "controller",
             "takeover": False, "client": client, "mode": "slots", "slots": list(SLOTS),
             "refresh_hz": 59.98, "accepted": True, "viewport": {"w": 390.0, "h": 844.0, "dpr": 3.0}}
    takeover = dict(hello, takeover=True, token=None, viewport=None)
    viewer = {"type": "hello", "protocol": P.PROTOCOL, "token": "Zx9-abc_DEF123", "role": "viewer",
              "takeover": False, "client": client, "mode": None, "slots": None,
              "refresh_hz": None, "accepted": False, "viewport": None}
    return [
        hello, takeover, viewer,
        {"type": "onset", "layout_id": 7, "t_client": T0 + 1.512},
        {"type": "cycle", "layout_id": 7, "n": 12, "t_client": T0 + 13.512},
        {"type": "frames", "refresh_hz": 59.98, "dropped": 2},
        {"type": "ping", "ping_id": 3, "t_client": T0 + 1.0},
        {"type": "pause"}, {"type": "resume"}, {"type": "bye"},
        {"type": "gaze", "slot": "t1"}, {"type": "gaze", "slot": None},
    ]


def check_client(msg: dict) -> None:
    """Raise if the server would refuse ``msg`` or drop any of its fields."""
    if msg["type"] == "hello":
        parsed = P.parse_hello(msg, max_slots=P.CODE_MAX_SLOTS)
        expect = {k: v for k, v in msg.items() if k != "token"}
        got = parsed.to_dict()
        assert got == expect, f"hello changed by parse_hello: {got} != {expect}"
        return
    cleaned = P.validate_client(msg)
    assert cleaned == msg, f"{msg['type']} changed by validate_client: {cleaned} != {msg}"


def build() -> dict:
    server = server_to_client()
    clients = client_to_server()
    for msg in clients:
        check_client(msg)
    for msg in server:
        P.dumps(msg)  # no NaN
    return {"server_to_client": server, "client_to_server": clients}


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/ios_v3_fixture.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="exit 1 if the fixture is missing or stale")
    p.add_argument("--out", type=Path, default=FIXTURE, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    text = render()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != text:
            print(f"stale: {args.out} (run python tools/ios_v3_fixture.py)", file=sys.stderr)
            return 1
        print(f"up to date: {args.out}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
