"""Tests for the server Session and SlotsMode on a clock-less FakeBoard in virtual time.

The transport is a FakeTransport defined here that queues Event-like objects (kind, conn_id,
role, msg, hello, t, close_code), so these tests never open a socket. The session's clock is
the board's own (board.now), so time only moves when a test advances the board.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from brainsync.board import FS, FakeBoard
from brainsync.server import protocol as P
from brainsync.server.decoding import DecoderBank
from brainsync.server.session import LONG_PAUSE_S, RECALIBRATE_AFTER_S, Session, SlotsMode

FREQS = (15.0, 17.0, 12.0, 20.0)
T0 = 1_000.0
CAL_S = 4.0
DT = 0.05


@dataclass(frozen=True)
class Event:
    kind: str
    conn_id: int
    role: str
    msg: dict | None
    hello: P.Hello | None
    t: float
    close_code: int | None = None


class FakeTransport:
    """Records what the session sends; tests push events with connect()/message()/drop()."""

    def __init__(self) -> None:
        self.queue: list[Event] = []
        self.sent: list[dict] = []                   # broadcasts
        self.sent_to: list[tuple[int, dict]] = []
        self.n_viewers = 0

    def events(self) -> list[Event]:
        out, self.queue = self.queue, []
        return out

    def send(self, msg: dict) -> None:
        P.dumps(msg)                                 # must be valid JSON without NaN
        self.sent.append(msg)

    def send_to(self, conn_id: int, msg: dict) -> None:
        P.dumps(msg)
        self.sent_to.append((conn_id, msg))

    @property
    def controller(self) -> int | None:
        return None

    # helpers
    def of(self, type_: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == type_]

    def to(self, conn_id: int, type_: str | None = None) -> list[dict]:
        return [m for c, m in self.sent_to
                if c == conn_id and (type_ is None or m["type"] == type_)]

    def phases(self) -> list[str]:
        return [m["phase"] for m in self.of("phase")]


def hello(role: str = "controller", mode: str | None = "slots", slots=("yes", "no"),
          refresh_hz: float | None = 60.0, **kw: Any) -> P.Hello:
    msg = {"type": "hello", "protocol": 3, "role": role, "mode": mode,
           "slots": list(slots) if slots is not None and mode == "slots" else None,
           "refresh_hz": refresh_hz, "accepted": role == "controller", **kw}
    return P.parse_hello(msg, max_slots=4)


class Rig:
    """A session on a virtual-time FakeBoard with helpers to drive it."""

    def __init__(self, *, fake: bool = True, point_factory=None, seed: int = 0,
                 calibrate_s: float = CAL_S, log: list | None = None, **kw: Any) -> None:
        self.board = FakeBoard(FREQS, seed=seed, t0=T0)
        self.tr = FakeTransport()
        self.bank = DecoderBank()
        self.records: list[tuple[str, dict]] = [] if log is None else log
        self.session = Session(self.tr, self.board, self.bank, FREQS, fake=fake,
                               point_factory=point_factory, calibrate_s=calibrate_s,
                               clock=lambda: self.board.now,
                               log=lambda k, f: self.records.append((k, f)), **kw)

    @property
    def now(self) -> float:
        return self.board.now

    def push(self, kind: str, conn_id: int, msg: dict | None = None, *, role="controller",
             h: P.Hello | None = None, close_code: int | None = None) -> None:
        self.tr.queue.append(Event(kind, conn_id, role, msg, h, self.now, close_code))

    def connect(self, conn_id: int = 1, h: P.Hello | None = None, step: bool = True) -> list[dict]:
        h = h or hello()
        first = self.session.catchup(conn_id, h.role, h)
        self.push("connect", conn_id, h.to_dict(), role=h.role, h=h)
        if step:
            self.session.step()
        return first

    def message(self, msg: dict, conn_id: int = 1, step: bool = True) -> None:
        self.push("message", conn_id, P.validate_client(msg))
        if step:
            self.session.step()

    def drop(self, conn_id: int = 1, code: int = 1000) -> None:
        self.push("disconnect", conn_id, close_code=code)
        self.session.step()

    def advance(self, seconds: float) -> None:
        for _ in range(int(round(seconds / DT))):
            self.board.advance(DT)
            self.session.step()

    def until(self, pred, limit: float = 30.0) -> None:
        end = self.now + limit
        while not pred():
            assert self.now < end, "condition not reached in time"
            self.board.advance(DT)
            self.session.step()

    def run_slots(self, conn_id: int = 1, **kw: Any) -> dict:
        """Connect a slots controller and calibrate; returns the running layout."""
        self.connect(conn_id, hello(**kw))
        self.until(lambda: self.session.phase == "run")
        return self.session.layout

    def selections(self) -> list[dict]:
        return self.tr.of("selection")

    def kinds(self) -> list[str]:
        return [k for k, _ in self.records]


class StubMode:
    """A point-like Mode that returns scripted messages and records its calls."""

    def __init__(self, start=None, on_selection=None, tick=None, on_message=None) -> None:
        self.calls: list[tuple] = []
        self.stopped = False
        self._start = start
        self._on_selection = on_selection
        self._tick = tick
        self._on_message = on_message

    def _out(self, f, *args):
        if f is None:
            return []
        return f(*args) if callable(f) else list(f)

    def start(self, now):
        self.calls.append(("start", now))
        return self._out(self._start, now)

    def on_selection(self, slot, now):
        self.calls.append(("on_selection", slot))
        return self._out(self._on_selection, slot, now)

    def on_message(self, msg, now):
        self.calls.append(("on_message", msg["type"]))
        return self._out(self._on_message, msg, now)

    def tick(self, now):
        self.calls.append(("tick", now))
        return self._out(self._tick, now)

    def stop(self):
        self.stopped = True


def point_layout(slots=("b1", "more")) -> dict:
    rects = [P.Rect(0.1, 0.1, 0.2, 0.2), P.Rect(0.7, 0.7, 0.2, 0.2), P.Rect(0.1, 0.7, 0.2, 0.2)]
    kinds = {"more": "more"}
    targets = [P.Target(s, FREQS[i], 0.0, s, kinds.get(s, "button"), (rects[i],))
               for i, s in enumerate(slots)]
    return P.layout(99, "pick", targets)


# -- construction -------------------------------------------------------------------------

@pytest.mark.parametrize("kw, match", [
    ({"freqs": ()}, "at least one frequency"),
    ({"freqs": (15.0, 15.0)}, "unique"),
    ({"freqs": (15.0, 200.0)}, "lie in"),
    ({"freqs": ("a",)}, "numbers"),
    ({"calibrate_s": 3.0}, "calibrate_s must be at least"),
    ({"calibrate_s": math.nan}, "calibrate_s"),
    ({"refresh_hz": 0.0}, "refresh_hz"),
    ({"refresh_tolerance_hz": -1.0}, "refresh_tolerance_hz"),
    ({"waveform": ""}, "waveform"),
])
def test_constructor_rejects_bad_arguments(kw, match):
    board = FakeBoard(FREQS, t0=T0)
    args = {"freqs": FREQS, "calibrate_s": CAL_S, **kw}
    freqs = args.pop("freqs")
    with pytest.raises(ValueError, match=match):
        Session(FakeTransport(), board, DecoderBank(), freqs, fake=True, point_factory=None,
                clock=lambda: board.now, **args)


def test_fake_needs_set_target_and_matching_rates():
    class Plain:
        fs = FS
        ch_names = FakeBoard(FREQS).ch_names

        def read(self):
            return np.empty((8, 0)), np.empty(0)

    with pytest.raises(ValueError, match="set_target"):
        Session(FakeTransport(), Plain(), DecoderBank(), FREQS, fake=True, point_factory=None,
                calibrate_s=CAL_S)
    Session(FakeTransport(), Plain(), DecoderBank(), FREQS, fake=False, point_factory=None,
            calibrate_s=CAL_S)
    with pytest.raises(ValueError, match="500 Hz"):
        Session(FakeTransport(), FakeBoard(FREQS, fs=500.0), DecoderBank(), FREQS, fake=True,
                point_factory=None, calibrate_s=CAL_S)


def test_fake_board_starts_with_its_tone_off():
    rig = Rig()
    assert rig.board.active_target() == -1


# -- SlotsMode ----------------------------------------------------------------------------

def test_slots_mode_layout():
    mode = SlotsMode(("yes", "no"), FREQS)
    out = mode.start(0.0)
    assert len(out) == 1 and out[0]["stage"] == "slots"
    assert [(t["slot"], t["freq"], t["phase"], t["kind"], t["rects"]) for t in out[0]["targets"]] \
        == [("yes", 15.0, 0.0, "slot", []), ("no", 17.0, 0.0, "slot", [])]
    assert mode.on_selection("yes", 0.0) == [] and mode.tick(0.0) == []
    assert mode.on_message({"type": "scan"}, 0.0) == []
    mode.stop()


@pytest.mark.parametrize("slots, freqs, match", [
    ((), FREQS, "at least one slot"),
    (("a", "b", "c"), (15.0, 17.0), "3 slots need 3 frequencies"),
    (("a", "a"), FREQS, "unique slots"),
    (("x" * 33,), FREQS, "1-32 characters"),
    (("a", "b"), (15.0, 15.0), "unique frequencies"),
])
def test_slots_mode_rejects_bad_slots(slots, freqs, match):
    with pytest.raises(ValueError, match=match):
        SlotsMode(slots, freqs)


# -- catchup and health -------------------------------------------------------------------

def test_catchup_sends_welcome_then_phase():
    rig = Rig()
    out = rig.session.catchup(5, "viewer", hello("viewer", mode=None))
    assert [m["type"] for m in out] == ["welcome", "phase"]
    w = out[0]
    assert w["role"] == "viewer" and w["mode"] is None and w["fake"] is True
    assert w["freqs"] == list(FREQS) and w["max_slots"] == 4 and w["detect"] is False
    assert w["refresh_hz"] == 60.0 and w["waveform"] == "sine"
    assert out[1]["phase"] == "wait" and out[1]["flicker"] is False
    out[1]["phase"] = "mutated"                       # the session keeps its own copy
    assert rig.session.catchup(6, "viewer", hello("viewer", mode=None))[1]["phase"] == "wait"


def test_catchup_includes_the_layout_and_status_once_running():
    rig = Rig(point_factory=lambda: StubMode())
    layout = rig.run_slots()
    out = rig.session.catchup(7, "viewer", hello("viewer", mode=None))
    assert [m["type"] for m in out] == ["welcome", "phase", "layout", "status"]
    assert out[0]["detect"] is True
    assert out[1]["phase"] == "run" and out[1]["flicker"] is True
    assert out[2] == layout
    assert out[3]["board"] == "streaming"


def test_health():
    rig = Rig()
    rig.tr.n_viewers = 2
    assert rig.session.health() == {"ok": True, "board": "unknown", "controller": False,
                                    "viewers": 2}
    rig.connect()
    rig.advance(0.2)
    assert rig.session.health() == {"ok": True, "board": "streaming", "controller": True,
                                    "viewers": 2}


# -- calibration flow ---------------------------------------------------------------------

def test_controller_calibrates_then_runs_slots_mode():
    rig = Rig()
    rig.connect()
    assert rig.tr.phases() == ["calibrate"]
    cal = rig.tr.of("phase")[0]
    assert cal["seconds"] == CAL_S and cal["flicker"] is False and cal["text"]
    rig.message({"type": "gaze", "slot": "yes"})
    assert rig.board.active_target() == -1            # the tone stays off while calibrating
    rig.advance(CAL_S - 0.5)
    assert rig.session.phase == "calibrate" and not rig.bank.calibrated
    t_start = rig.now
    rig.until(lambda: rig.session.phase == "run", limit=2.0)
    assert rig.bank.calibrated and rig.now - t_start <= 1.0
    assert rig.tr.phases() == ["calibrate", "run"]
    layouts = rig.tr.of("layout")
    assert len(layouts) == 1 and layouts[0]["stage"] == "slots"
    assert [(t["slot"], t["freq"]) for t in layouts[0]["targets"]] == [("yes", 15.0), ("no", 17.0)]
    # the layout came before the run phase, and the bank runs that layout
    assert rig.tr.sent.index(layouts[0]) < rig.tr.sent.index(rig.tr.of("phase")[1])
    assert rig.bank.active == (15.0, 17.0)
    assert rig.board.active_target() == 0             # the stored gaze applies now
    assert {"calibrate_start", "calibrated", "layout", "phase"} <= set(rig.kinds())


def test_fresh_calibration_is_reused_and_stale_one_redone():
    rig = Rig()
    rig.run_slots()
    rig.drop()
    assert rig.session.phase == "wait"
    rig.tr.sent.clear()
    rig.connect(2)
    assert rig.tr.phases() == ["run"]                 # no second calibration
    rig.drop(2)
    rig.session._calibrated_at = rig.now - RECALIBRATE_AFTER_S - 1
    rig.tr.sent.clear()
    rig.connect(3)
    assert rig.tr.phases() == ["calibrate"]


def test_failed_calibration_restarts(monkeypatch):
    rig = Rig()
    calls = []

    def boom(X):
        calls.append(X.shape)
        if len(calls) == 1:
            raise ValueError("only 1 of 9 still windows passed artifact rejection")
        return DecoderBank.calibrate_from(rig.bank, X)

    monkeypatch.setattr(rig.bank, "calibrate_from", boom)
    rig.connect()
    rig.until(lambda: rig.session.phase == "run", limit=3 * CAL_S)
    assert len(calls) == 2 and calls[0] == (8, int(CAL_S * FS))
    errs = rig.tr.to(1, "error")
    assert errs and errs[0]["code"] == "internal" and "Calibration failed" in errs[0]["message"]


# -- refusals -----------------------------------------------------------------------------

def test_refresh_rejection_keeps_the_session_waiting():
    rig = Rig()
    rig.connect(1, hello(refresh_hz=50.0))
    errs = rig.tr.to(1, "error")
    assert len(errs) == 1 and errs[0]["code"] == "refresh" and errs[0]["fatal"] is False
    assert "50.00 Hz" in errs[0]["message"]
    rig.advance(CAL_S + 1)
    assert rig.session.phase == "wait" and not rig.bank.calibrated
    phases = rig.tr.of("phase")
    assert phases and phases[-1]["phase"] == "wait" and "50.00 Hz" in phases[-1]["text"]
    assert rig.tr.of("layout") == []
    # a new hello with a good rate gets going
    rig.drop()
    rig.connect(2, hello(refresh_hz=60.4))
    assert rig.session.phase == "calibrate"


def test_ignore_refresh():
    rig = Rig(ignore_refresh=True)
    rig.connect(1, hello(refresh_hz=144.0))
    assert rig.tr.to(1, "error") == [] and rig.session.phase == "calibrate"


@pytest.mark.parametrize("hz", [60.0, 59.3, 90.4, 119.97, 120.8, 144.0])
def test_each_default_refresh_rate_is_accepted(hz):
    rig = Rig()
    first = rig.connect(1, hello(refresh_hz=hz))
    assert rig.tr.to(1, "error") == [] and rig.session.phase == "calibrate"
    w = first[0]
    assert w["refresh_hz"] == 60.0 and w["refresh_tolerance_hz"] == 1.0
    assert w["refresh_rates"] == [60.0, 90.0, 120.0, 144.0]
    assert "refresh_ignored" not in rig.kinds()


@pytest.mark.parametrize("hz", [50.0, 75.0, 100.0, 165.0])
def test_a_rate_outside_the_set_is_refused_with_the_rates_listed(hz):
    rig = Rig()
    rig.connect(1, hello(refresh_hz=hz))
    errs = rig.tr.to(1, "error")
    assert [e["code"] for e in errs] == ["refresh"] and errs[0]["fatal"] is False
    assert f"{hz:.2f} Hz" in errs[0]["message"]
    assert "one of 60, 90, 120 or 144 Hz (± 1 Hz)" in errs[0]["message"]
    assert rig.session.phase == "wait"


def test_refresh_rates_can_be_narrowed_and_the_nominal_rate_is_always_accepted():
    rig = Rig(refresh_rates=(60.0,))
    assert rig.session.refresh_rates == (60.0,)
    rig.connect(1, hello(refresh_hz=120.0))
    msg = rig.tr.to(1, "error")[0]["message"]
    assert "needs 60 ± 1 Hz. Set the display to 60 Hz" in msg
    rig2 = Rig(refresh_hz=120.0, refresh_rates=(144.0, 120.0, 144.0))
    assert rig2.session.refresh_rates == (120.0, 144.0)
    w = rig2.connect(1, hello(refresh_hz=143.5))[0]
    assert w["refresh_hz"] == 120.0 and w["refresh_rates"] == [120.0, 144.0]
    assert rig2.tr.to(1, "error") == []
    rig3 = Rig(refresh_hz=120.0, refresh_rates=(144.0,))
    rig3.connect(1, hello(refresh_hz=60.0))
    assert "one of 120 or 144 Hz" in rig3.tr.to(1, "error")[0]["message"]


@pytest.mark.parametrize("kw, match", [
    ({"refresh_rates": ("fast",)}, "refresh_rates must be numbers"),
    ({"refresh_rates": 60}, "refresh_rates must be numbers"),
    ({"refresh_rates": (60.0, math.nan)}, "positive"),
    ({"refresh_rates": (60.0, 0.0)}, "positive"),
    ({"refresh_rates": (40.0,)}, "a 40 Hz display cannot show 20 Hz flicker"),
    ({"refresh_hz": 41.0, "refresh_rates": (60.0,)}, "a 41 Hz display cannot show 20 Hz"),
])
def test_constructor_rejects_bad_refresh_rates(kw, match):
    board = FakeBoard(FREQS, t0=T0)
    with pytest.raises(ValueError, match=match):
        Session(FakeTransport(), board, DecoderBank(), FREQS, fake=True, point_factory=None,
                calibrate_s=CAL_S, clock=lambda: board.now, **kw)


def test_point_mode_without_a_detector_is_unavailable():
    rig = Rig(point_factory=None)
    rig.connect(1, hello(mode="point", slots=None))
    errs = rig.tr.to(1, "error")
    assert [e["code"] for e in errs] == ["detect_unavailable"] and not errs[0]["fatal"]
    rig.advance(CAL_S + 1)
    assert rig.session.phase == "wait" and not rig.bank.calibrated


def test_point_factory_failure_is_unavailable():
    def broken():
        raise RuntimeError("no API key")

    rig = Rig(point_factory=broken)
    rig.connect(1, hello(mode="point", slots=None))
    errs = rig.tr.to(1, "error")
    assert errs[0]["code"] == "detect_unavailable" and "no API key" in errs[0]["message"]
    assert rig.session.phase == "wait"


# -- onset gating and selections ----------------------------------------------------------

def onset_and_hold(rig: Rig, slot: str = "yes", t_client: float | None = None) -> float:
    layout = rig.session.layout
    rig.message({"type": "gaze", "slot": slot}, step=False)
    t_on = rig.now
    rig.message({"type": "onset", "layout_id": layout["layout_id"],
                 "t_client": t_on if t_client is None else t_client})
    return t_on


def test_first_selection_comes_after_onset_plus_latency_plus_window():
    log: list = []
    rig = Rig(log=log)
    rig.run_slots()
    t_on = onset_and_hold(rig)
    assert rig.bank.gate == pytest.approx(t_on + P.LATENCY_S)
    rig.until(lambda: rig.selections(), limit=5.0)
    sel = rig.selections()[0]
    assert sel["slot"] == "yes" and sel["layout_id"] == rig.session.layout["layout_id"]
    assert set(sel["scores"]) == {"yes", "no"} and sel["score"] == sel["scores"]["yes"]
    dec = rig.bank.decoder
    t_decision = next(f["t_decision"] for k, f in log if k == "selection")
    # the commit needs `votes` windows that all start at or after onset + latency
    earliest = t_on + P.LATENCY_S + dec.window_s + (dec.votes - 1) * dec.n_step / FS
    assert t_decision >= earliest - 1e-9
    assert sel["t_server"] - t_on == pytest.approx(2.5, abs=0.3)   # measured: 2.50 s


def test_pre_onset_gaze_never_votes():
    """The user stared at "yes" during the layout's first seconds, before an onset that came
    late (as with a slow first frame): no window from before the onset may count."""
    log: list = []
    rig = Rig(log=log)
    rig.run_slots()
    rig.message({"type": "gaze", "slot": "yes"})
    rig.advance(1.0)                                  # within the 2 s onset timeout
    t_on = rig.now
    rig.message({"type": "onset", "layout_id": rig.session.layout["layout_id"],
                 "t_client": t_on})
    assert rig.bank.gate == pytest.approx(t_on + P.LATENCY_S)
    rig.until(lambda: rig.selections(), limit=5.0)
    t_decision = next(f["t_decision"] for k, f in log if k == "selection")
    dec = rig.bank.decoder
    assert t_decision >= t_on + P.LATENCY_S + dec.window_s + (dec.votes - 1) * dec.n_step / FS


def test_without_an_onset_the_gate_is_the_activation():
    rig = Rig()
    rig.run_slots()
    t_run = next(f["t"] for k, f in rig.records if k == "phase" and f["phase"] == "run")
    assert rig.bank.gate == pytest.approx(t_run + P.LATENCY_S)
    rig.message({"type": "gaze", "slot": "no"})
    rig.until(lambda: rig.selections(), limit=5.0)
    assert rig.selections()[0]["slot"] == "no"


def test_onset_is_ignored_for_another_layout_or_late():
    rig = Rig()
    layout = rig.run_slots()
    gate = rig.bank.gate
    rig.message({"type": "onset", "layout_id": layout["layout_id"] + 1, "t_client": rig.now})
    assert rig.bank.gate == gate
    rig.advance(P.ONSET_TIMEOUT_S + 0.1)
    rig.message({"type": "onset", "layout_id": layout["layout_id"], "t_client": rig.now})
    assert rig.bank.gate == gate
    why = [f["why"] for k, f in rig.records if k == "onset_ignored"]
    assert why == ["not the running layout", "late"]


def test_onset_uses_the_ping_offset_and_is_clamped():
    rig = Rig()
    layout = rig.run_slots()
    t_run = rig.bank.gate - P.LATENCY_S
    skew = 3600.0                                     # the client's clock is an hour behind
    rig.message({"type": "ping", "ping_id": 1, "t_client": rig.now - skew})
    assert rig.session.offset == pytest.approx(skew)
    t = rig.now
    rig.message({"type": "onset", "layout_id": layout["layout_id"], "t_client": t - skew - 0.01})
    assert rig.bank.gate == pytest.approx(max(t - 0.01, t_run) + P.LATENCY_S)


def test_onset_from_the_future_is_clamped_to_its_receipt():
    rig = Rig()
    layout = rig.run_slots()
    t = rig.now
    rig.message({"type": "onset", "layout_id": layout["layout_id"], "t_client": t + 500.0})
    assert rig.bank.gate == pytest.approx(t + P.LATENCY_S)   # no offset known: receipt time


def test_offset_is_the_median_of_the_last_eight_pings():
    rig = Rig()
    rig.run_slots()
    for i, off in enumerate([100.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]):
        rig.message({"type": "ping", "ping_id": i, "t_client": rig.now - off}, step=False)
    rig.session.step()
    assert rig.session.offset == pytest.approx(4.5)


def test_dwell_repeats_after_the_cooldown():
    rig = Rig()
    rig.run_slots()
    t_on = onset_and_hold(rig, "yes")
    rig.advance(12.0)
    sels = rig.selections()
    assert len(sels) >= 3 and {s["slot"] for s in sels} == {"yes"}
    times = np.array([s["t_server"] for s in sels])
    gaps = np.diff(times)
    assert gaps.min() >= P.COOLDOWN_S
    assert np.median(gaps) == pytest.approx(2.75, abs=0.3)   # cooldown + re-arm + 4 votes
    assert [s["selection_id"] for s in sels] == list(range(1, len(sels) + 1))
    assert sels[0]["t_server"] > t_on


def test_dwell_cooldown_holds_with_large_board_reads():
    """Reads of a whole second hold several decisions scored before the session sees them;
    votes cast before the re-arm must not fire a selection early."""
    rig = Rig()
    rig.run_slots()
    onset_and_hold(rig, "yes")
    for _ in range(14):
        rig.board.advance(1.0)
        rig.session.step()
    sels = rig.selections()
    assert len(sels) >= 3 and {s["slot"] for s in sels} == {"yes"}
    t_decisions = [f["t_decision"] for k, f in rig.records if k == "selection"]
    dec = rig.bank.decoder
    # re-arm decision + `votes` fresh windows after the cooldown
    assert np.diff(t_decisions).min() >= P.COOLDOWN_S + dec.votes * dec.n_step / FS - 1e-9


def test_switching_gaze_selects_the_other_slot():
    rig = Rig()
    rig.run_slots()
    onset_and_hold(rig, "yes")
    rig.until(lambda: rig.selections(), limit=5.0)
    rig.message({"type": "gaze", "slot": "no"})
    rig.until(lambda: rig.selections()[-1]["slot"] == "no", limit=6.0)


def test_no_selection_outside_run():
    rig = Rig()
    rig.run_slots()
    rig.message({"type": "gaze", "slot": "yes"})
    rig.message({"type": "pause"})
    assert rig.session.phase == "pause" and rig.bank.active is None
    rig.advance(5.0)
    assert rig.selections() == []


# -- gaze ---------------------------------------------------------------------------------

def test_gaze_maps_slots_to_the_fake_board():
    rig = Rig()
    rig.run_slots(slots=("left", "right", "up"))
    for slot, target in (("right", 1), ("up", 2), ("left", 0), (None, -1), ("nope", -1)):
        rig.message({"type": "gaze", "slot": slot})
        assert rig.board.active_target() == target, slot


def test_gaze_needs_a_fake_board():
    rig = Rig(fake=False)
    rig.board.set_target(0)
    rig.run_slots()
    rig.message({"type": "gaze", "slot": "no"})
    errs = rig.tr.to(1, "error")
    assert [e["code"] for e in errs] == ["not_allowed"]
    assert rig.board.active_target() == 0


def test_gaze_is_released_when_the_controller_leaves():
    rig = Rig()
    rig.run_slots()
    rig.message({"type": "gaze", "slot": "no"})
    assert rig.board.active_target() == 1
    rig.drop()
    assert rig.board.active_target() == -1


# -- leaving ------------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["disconnect", "bye"])
def test_controller_leaving_returns_to_wait(how):
    mode = StubMode(start=lambda now: [point_layout()])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    assert rig.bank.active is not None
    n_layouts = len(rig.tr.of("layout"))
    if how == "bye":
        rig.message({"type": "bye"})
    else:
        rig.drop(1, code=1001)
    assert mode.stopped and rig.session.phase == "wait" and rig.session.controller is None
    assert rig.bank.active is None and rig.session.layout is None
    layouts = rig.tr.of("layout")
    assert len(layouts) == n_layouts + 1 and layouts[-1]["stage"] == "idle"
    assert layouts[-1]["targets"] == []
    # the wait phase went out before the idle layout (flicker off first)
    assert rig.tr.sent.index(rig.tr.of("phase")[-1]) < rig.tr.sent.index(layouts[-1])
    catchup = rig.session.catchup(9, "viewer", hello("viewer", mode=None))
    assert "layout" not in [m["type"] for m in catchup]
    rig.advance(1.0)                                  # board keeps streaming
    assert rig.session.health()["board"] == "streaming"
    assert rig.tr.of("phase")[-1]["phase"] == "wait"


def test_takeover_stops_the_old_mode_and_ignores_its_late_events():
    modes: list[StubMode] = []

    def factory():
        modes.append(StubMode(start=lambda now: [point_layout()]))
        return modes[-1]

    rig = Rig(point_factory=factory)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.connect(2, hello(mode="point", slots=None, takeover=True))
    assert modes[0].stopped and rig.session.controller == 2
    assert rig.session.phase == "run" and len(modes) == 2 and not modes[1].stopped
    rig.message({"type": "pause"}, conn_id=1)         # the old connection is ignored
    rig.drop(1, code=P.CLOSE_REPLACED)
    assert rig.session.phase == "run" and rig.session.controller == 2


def test_viewer_events_change_nothing():
    rig = Rig()
    rig.run_slots()
    rig.connect(5, hello("viewer", mode=None))
    rig.message({"type": "ping", "ping_id": 1, "t_client": 0.0}, conn_id=5)
    rig.message({"type": "bye"}, conn_id=5)
    rig.drop(5)
    assert rig.session.phase == "run" and rig.session.controller == 1
    assert rig.session.offset is None


# -- pause, hold and the mode -------------------------------------------------------------

def test_user_pause_and_short_resume():
    rig = Rig()
    rig.run_slots()
    rig.message({"type": "pause"})
    rig.message({"type": "pause"})                    # twice: one phase message
    assert rig.tr.phases()[-1] == "pause" and rig.tr.phases().count("pause") == 1
    rig.advance(5.0)
    t = rig.now
    rig.message({"type": "resume"})
    assert rig.session.phase == "run" and rig.bank.gate == pytest.approx(t + P.LATENCY_S)
    assert "calibrate" not in rig.tr.phases()[1:]


def test_long_pause_recalibrates_on_resume():
    rig = Rig()
    layout = rig.run_slots()
    rig.message({"type": "pause"})
    rig.session._paused_at -= LONG_PAUSE_S            # pretend two minutes passed
    rig.message({"type": "resume"})
    assert rig.session.phase == "calibrate"
    rig.until(lambda: rig.session.phase == "run", limit=CAL_S + 1)
    assert rig.session.layout == layout and len(rig.tr.of("layout")) == 1   # mode not restarted
    assert rig.bank.active == (15.0, 17.0)


def test_mode_hold_pauses_and_release_runs():
    state = {"release": False}

    def on_selection(slot, now):
        return [P.click(1, 0.2, 0.2, {"id": 0, "label": slot}),
                P.phase("pause", False, text="Clicking")]

    def tick(now):
        if state["release"]:
            state["release"] = False
            return [P.layout(0, "idle", []), P.phase("run", True)]
        return []

    mode = StubMode(start=lambda now: [P.scan_request(1), point_layout()],
                    on_selection=on_selection, tick=tick)
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    assert rig.tr.to(1, "scan_request") == [{"type": "scan_request", "scan_id": 1,
                                             "settle_ms": P.SETTLE_MS}]
    rig.message({"type": "gaze", "slot": "b1"})
    rig.until(lambda: rig.session.phase == "pause", limit=6.0)
    assert ("on_selection", "b1") in mode.calls
    assert rig.selections()[-1]["slot"] == "b1"
    assert rig.tr.of("click")[0]["element"]["label"] == "b1"   # broadcast: viewers see it too
    assert rig.tr.to(1, "click") == []
    ph = rig.tr.of("phase")[-1]
    assert ph["phase"] == "pause" and ph["text"] == "Clicking" and rig.bank.active is None
    n = len(rig.selections())
    rig.advance(3.0)
    assert len(rig.selections()) == n                 # nothing selected while held
    state["release"] = True
    rig.advance(DT)
    assert rig.session.phase == "run" and rig.session.layout["stage"] == "idle"
    assert rig.bank.active is None                    # idle layout: nothing to decode


def test_point_selection_stops_at_a_new_layout():
    """A point selection that brings a new layout must not let the old layout's remaining
    decisions in the same board read select anything."""
    def on_selection(slot, now):
        return [point_layout(("b1", "b2", "more"))]

    mode = StubMode(start=lambda now: [point_layout()], on_selection=on_selection)
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    first = rig.session.layout["layout_id"]
    rig.message({"type": "gaze", "slot": "b1"})
    rig.until(lambda: rig.selections(), limit=6.0)
    assert rig.session.layout["layout_id"] == first + 1
    assert len(rig.selections()) == 1 and rig.bank.active == (15.0, 17.0, 12.0)


def test_layout_ids_are_stamped_by_the_session():
    mode = StubMode(start=lambda now: [point_layout()],
                    on_message=lambda msg, now: [point_layout(), P.layout(5, "idle", [])])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.message({"type": "clicked", "click_id": 1, "ok": True})
    ids = [m["layout_id"] for m in rig.tr.of("layout")]
    assert ids == [1, 2, 3]


def test_scan_and_clicked_reach_a_point_mode():
    mode = StubMode(start=lambda now: [point_layout()],
                    on_message=lambda msg, now: [P.scan_result(1, True, 12.0, [], 0, "ok")])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.message({"type": "clicked", "click_id": 1, "ok": True})
    assert ("on_message", "clicked") not in mode.calls          # not started while calibrating
    rig.until(lambda: rig.session.phase == "run")
    rig.message({"type": "clicked", "click_id": 1, "ok": True})
    assert ("on_message", "clicked") in mode.calls
    assert rig.tr.of("scan_result")[0]["summary"] == "ok"       # broadcast


def test_scan_from_a_slots_controller_is_refused():
    rig = Rig()
    rig.run_slots()
    rig.message({"type": "clicked", "click_id": 1, "ok": True})
    errs = rig.tr.to(1, "error")
    assert [e["code"] for e in errs] == ["not_allowed"] and "point mode" in errs[0]["message"]


def test_a_failing_mode_reports_internal_and_the_session_survives(capsys):
    def tick(now):
        raise RuntimeError("detector exploded")

    mode = StubMode(start=lambda now: [point_layout(), {"type": "phase", "phase": "stop"}],
                    tick=tick)
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.advance(0.2)
    errs = rig.tr.to(1, "error")
    assert errs and all(e["code"] == "internal" for e in errs)
    assert any("run\" or \"pause" in e["message"] for e in errs)
    assert any("detector exploded" in e["message"] for e in errs)
    assert "detector exploded" in capsys.readouterr().err
    assert rig.session.phase == "run"


# -- status, log, close, run --------------------------------------------------------------

def test_status_about_once_a_second_and_on_change():
    rig = Rig()
    rig.session.step()
    assert rig.tr.of("status")[-1]["board"] == "no-data"
    rig.advance(3.0)
    st = rig.tr.of("status")
    streaming = [s for s in st if s["board"] == "streaming"]
    assert 3 <= len(streaming) <= 4 and streaming[-1]["ok"] is True
    assert streaming[-1]["text"].startswith("posterior RMS ") and streaming[-1]["rtt_ms"] is None
    # the board stops: no-data after a second
    clock = [rig.now]
    rig.session._clock = lambda: clock[0]
    clock[0] += 1.5
    rig.session.step()
    assert rig.tr.of("status")[-1]["board"] == "no-data"
    assert rig.tr.of("status")[-1]["ok"] is False


DROP_TEXT = "the display dropped {}% of frames; close other apps or plug in"


def frames(rig: Rig, dropped: int, hz: float = 60.0) -> dict:
    """Send a frames report and return the status that is current afterwards."""
    rig.message({"type": "frames", "refresh_hz": hz, "dropped": dropped})
    return rig.tr.of("status")[-1]


def test_heavy_dropped_frames_make_status_not_ok_until_a_later_report_is_fine():
    rig = Rig()
    rig.run_slots()
    rig.advance(1.0)
    assert frames(rig, 7)["ok"] is True               # the first report is only the baseline
    rig.advance(5.0)
    n = len(rig.tr.of("status"))
    st = frames(rig, 7 + 25)                          # 25 of 300 expected: 8 %, fine
    assert st["ok"] is True and len(rig.tr.of("status")) == n
    rig.advance(5.0)
    n = len(rig.tr.of("status"))
    st = frames(rig, 32 + 60)                         # 60 of 300: 20 %
    assert len(rig.tr.of("status")) == n + 1          # sent at once, not at the next second
    assert st == {"type": "status", "board": "streaming", "ok": False,
                  "text": DROP_TEXT.format(20), "rtt_ms": None}
    rig.advance(2.0)                                  # the periodic status keeps saying so
    assert rig.tr.of("status")[-1]["ok"] is False
    assert rig.tr.of("status")[-1]["text"] == DROP_TEXT.format(20)
    assert rig.session.phase == "run"                 # decoding goes on
    rig.advance(3.0)
    n = len(rig.tr.of("status"))
    st = frames(rig, 92 + 5)                          # 5 of 300: cleared
    assert len(rig.tr.of("status")) == n + 1
    assert st["ok"] is True and "dropped" not in st["text"]
    rig.advance(1.0)
    assert rig.tr.of("status")[-1]["text"].startswith("posterior RMS ")
    logged = [f for k, f in rig.records if k == "dropped_frames"]
    assert [f["ok"] for f in logged] == [False, True]
    assert logged[0]["share"] == pytest.approx(0.2, rel=0.02)


def test_dropped_frames_at_120_hz_use_the_reported_refresh():
    rig = Rig()
    rig.run_slots(refresh_hz=120.0)
    frames(rig, 0, hz=119.9)
    rig.advance(5.0)
    assert frames(rig, 50, hz=119.9)["ok"] is True    # 50 of 600: 8 %
    rig.advance(5.0)
    st = frames(rig, 150, hz=119.9)                   # 100 of 600: 17 %
    assert st["ok"] is False and st["text"] == DROP_TEXT.format(17)


def test_dropped_frames_after_a_long_gap_set_a_new_baseline_and_a_restart_is_a_baseline():
    rig = Rig()
    rig.run_slots()
    frames(rig, 0)
    rig.advance(12.0)                                 # e.g. a pause: no reports meanwhile
    st = frames(rig, 45)                              # not judged: the gap holds two stretches
    assert st["ok"] is True
    rig.advance(5.0)
    st = frames(rig, 45 + 45)                         # 45 over 5 s of 60 Hz: 15 %
    assert st["ok"] is False and st["text"] == DROP_TEXT.format(15)
    rig.advance(5.0)
    assert frames(rig, 3)["ok"] is False              # a smaller count restarted: no judgement
    rig.advance(5.0)
    assert frames(rig, 4)["ok"] is True


def test_dropped_frames_from_before_a_pause_raise_no_false_warning():
    # A display steadily dropping 6 % (18 per 5 s): 4 s of flicker after a report (+14), a
    # 30 s pause, then 5 s more (+18). Divided by 5 s that read 32/300 = 11 %.
    rig = Rig()
    rig.run_slots()
    frames(rig, 0)
    rig.advance(5.0)
    assert frames(rig, 18)["ok"] is True
    rig.advance(4.0 + 30.0 + 5.0)
    assert frames(rig, 18 + 14 + 18)["ok"] is True
    rig.advance(5.0)
    assert frames(rig, 50 + 18)["ok"] is True         # judged again: 6 %
    rig.advance(5.0)
    st = frames(rig, 68 + 60)                         # and a real problem still shows
    assert st["ok"] is False and st["text"] == DROP_TEXT.format(20)
    rig.advance(40.0)
    st = frames(rig, 128 + 5)                         # after a long gap the warning stays
    assert st["ok"] is False and st["text"] == DROP_TEXT.format(20)
    rig.advance(5.0)
    assert frames(rig, 133 + 5)["ok"] is True
    logged = [f["ok"] for k, f in rig.records if k == "dropped_frames"]
    assert logged == [False, True]


def test_dropped_frames_clear_when_the_controller_leaves_and_viewers_cannot_report():
    rig = Rig()
    rig.run_slots()
    frames(rig, 0)
    rig.advance(5.0)
    assert frames(rig, 100)["ok"] is False
    rig.drop()
    rig.advance(0.2)
    assert rig.tr.of("status")[-1]["ok"] is True
    rig.connect(2, hello())
    rig.push("message", 9, P.validate_client({"type": "frames", "refresh_hz": 60.0,
                                              "dropped": 0}), role="viewer")
    rig.session.step()
    rig.advance(5.0)
    rig.push("message", 9, P.validate_client({"type": "frames", "refresh_hz": 60.0,
                                              "dropped": 300}), role="viewer")
    rig.session.step()
    assert rig.tr.of("status")[-1]["ok"] is True       # only the controller's reports count


def test_board_errors_become_status_error(capsys):
    rig = Rig()

    def broken():
        raise RuntimeError("serial port gone")

    rig.board.read = broken
    rig.session.step()
    rig.session.step()
    st = rig.tr.of("status")[-1]
    assert st["board"] == "error" and "serial port gone" in st["text"]
    assert capsys.readouterr().err.count("serial port gone") == 1
    assert rig.session.health()["board"] == "error"


def test_a_raising_log_hook_does_not_stop_the_session(capsys):
    def bad_log(kind, fields):
        raise OSError("disk full")

    board = FakeBoard(FREQS, t0=T0)
    tr = FakeTransport()
    s = Session(tr, board, DecoderBank(), FREQS, fake=True, point_factory=None,
                calibrate_s=CAL_S, clock=lambda: board.now, log=bad_log)
    h = hello()
    tr.queue.append(Event("connect", 1, "controller", h.to_dict(), h, board.now))
    s.step()
    assert s.phase == "calibrate" and "disk full" in capsys.readouterr().err


def test_log_records_never_hold_the_token():
    rig = Rig()
    h = P.parse_hello({"type": "hello", "protocol": 3, "role": "controller", "mode": "slots",
                       "slots": ["yes"], "refresh_hz": 60.0, "accepted": True,
                       "token": "s3cret-token"}, 4)
    rig.connect(1, h)
    rig.advance(CAL_S + 3)
    assert "s3cret-token" not in repr(rig.records)


def test_close_broadcasts_stop_once():
    mode = StubMode(start=lambda now: [point_layout()])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.session.close("Bye now")
    rig.session.close()
    stops = [m for m in rig.tr.of("phase") if m["phase"] == "stop"]
    assert len(stops) == 1 and stops[0]["text"] == "Bye now" and stops[0]["flicker"] is False
    assert mode.stopped and rig.bank.active is None
    assert rig.session.catchup(4, "viewer", hello("viewer", mode=None))[1]["phase"] == "stop"
    rig.session.step()                                # later events cannot leave "stop"
    assert rig.session.phase == "stop"


def test_run_steps_until_stopped_then_closes():
    board = FakeBoard(FREQS, t0=T0, clock=None)
    tr = FakeTransport()
    s = Session(tr, board, DecoderBank(), FREQS, fake=True, point_factory=None,
                calibrate_s=CAL_S, clock=lambda: board.now)
    stop = threading.Event()
    steps = []
    real = s.step

    def counted():
        steps.append(1)
        real()
        if len(steps) == 3:
            stop.set()
        if len(steps) == 2:
            raise RuntimeError("one bad step")

    s.step = counted
    s.run(stop, poll_s=0.001)
    assert len(steps) == 3
    assert tr.of("phase")[-1]["phase"] == "stop"
    with pytest.raises(ValueError, match="poll_s"):
        s.run(stop, poll_s=0.0)


def test_too_many_slots_for_the_frequencies():
    board = FakeBoard((15.0, 17.0), t0=T0)
    tr = FakeTransport()
    s = Session(tr, board, DecoderBank(), (15.0, 17.0), fake=True, point_factory=None,
                calibrate_s=CAL_S, clock=lambda: board.now)
    h = hello(slots=("a", "b", "c"))
    tr.queue.append(Event("connect", 1, "controller", h.to_dict(), h, board.now))
    s.step()
    assert [m["code"] for _, m in tr.sent_to] == ["too_many_slots"]
    assert s.phase == "wait"


# -- review fixes ---------------------------------------------------------------------------

def test_click_is_broadcast_and_scan_request_goes_to_the_controller_only():
    """PROTOCOL-v3 section 1: viewers get every broadcast, click included."""
    mode = StubMode(start=lambda now: [P.scan_request(1), point_layout()],
                    on_selection=lambda slot, now: [P.click(1, 0.2, 0.2, {"id": 0, "label": slot}),
                                                    P.phase("pause", False)])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.connect(2, hello(role="viewer", mode=None, slots=None, refresh_hz=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.message({"type": "gaze", "slot": "b1"})
    rig.until(lambda: rig.tr.of("click"), limit=6.0)
    assert [m["element"]["label"] for m in rig.tr.of("click")] == ["b1"]
    assert rig.tr.of("scan_request") == [] and len(rig.tr.to(1, "scan_request")) == 1


def _moved(slots=("b1", "more")) -> dict:
    """point_layout with the b1 patch somewhere else (the next page's top candidate)."""
    lay = point_layout(slots)
    lay["targets"][0]["rects"] = [{"x": 0.5, "y": 0.1, "w": 0.2, "h": 0.2}]
    lay["targets"][0]["label"] = "Other"
    return lay


def test_gaze_does_not_follow_a_slot_name_to_another_element():
    """A gaze on b1 must not carry to the next layout's b1, which is a different element."""
    state = {"next": None}
    mode = StubMode(start=lambda now: [point_layout()],
                    tick=lambda now: [state.pop("next")] if state.get("next") else [])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.message({"type": "gaze", "slot": "b1"})
    assert rig.board.active_target() == 0
    state["next"] = point_layout()                     # same place: the gaze stays
    rig.advance(DT)
    assert rig.session._gaze == "b1" and rig.board.active_target() == 0
    state["next"] = _moved()                           # b1 is now another element
    rig.advance(DT)
    assert rig.session._gaze is None and rig.board.active_target() == -1
    rig.message({"type": "gaze", "slot": "b1"})        # the client says it again
    assert rig.board.active_target() == 0


def test_gaze_is_dropped_by_an_idle_layout_and_not_revived():
    state = {"next": None}
    mode = StubMode(start=lambda now: [point_layout()],
                    tick=lambda now: [state.pop("next")] if state.get("next") else [])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.message({"type": "gaze", "slot": "b1"})
    state["next"] = P.layout(0, "idle", [])
    rig.advance(DT)
    state["next"] = point_layout()                     # a fresh pick layout after the rescan
    rig.advance(DT)
    assert rig.session._gaze is None and rig.board.active_target() == -1


def test_gaze_sent_before_the_layout_still_applies():
    rig = Rig()
    rig.connect(1, hello())
    rig.message({"type": "gaze", "slot": "yes"})      # while calibrating, no layout yet
    rig.until(lambda: rig.session.phase == "run")
    assert rig.session._gaze == "yes" and rig.board.active_target() == 0


def test_ignore_refresh_accepts_an_off_rate_display_and_says_frequencies_stay(capsys):
    rig = Rig(ignore_refresh=True)
    rig.connect(1, hello(refresh_hz=75.0))
    assert rig.tr.to(1, "error") == []
    err = capsys.readouterr().err
    assert "75.0 Hz display" in err and "frequencies stay [15.0, 17.0, 12.0, 20.0] Hz" in err
    assert "at or above 37.5 Hz is drawn still" in err
    assert "refresh_ignored" in rig.kinds()
    rig2 = Rig()
    rig2.connect(1, hello(refresh_hz=75.0))
    assert [e["code"] for e in rig2.tr.to(1, "error")] == ["refresh"]


# -- learning from confirmed picks (Phase 4) ---------------------------------------------------

def test_learn_from_teaches_the_bank_on_a_confirmed_pick():
    """A button pick is remembered with its gate and commit time; the confirm selection that
    follows teaches the bank that window for the picked frequency; back forgets it; no trained
    model: nothing. It receives the target of the layout the selection was made in."""
    from types import SimpleNamespace

    calls: list[tuple[float, float, float]] = []
    logs: list[tuple[str, dict]] = []

    class Bank:
        trained = SimpleNamespace(n_train=np.array([3, 4]))

        def learn(self, freq, t_start, t_end):
            calls.append((freq, t_start, t_end))
            return True

    button = {"slot": "b2", "kind": "button", "freq": 17.0}
    confirm = {"slot": "ok", "kind": "confirm", "freq": 15.0}
    back = {"slot": "no", "kind": "back", "freq": 17.0}
    me = SimpleNamespace(_bank=Bank(), _last_pick=None,
                         _log=lambda kind, fields: logs.append((kind, fields)))
    Session._learn_from(me, button, 123.0, 125.5, 7)       # pick b2 (17 Hz), gate 123, commit 125.5
    assert me._last_pick == (17.0, 123.0, 125.5, 7) and calls == []
    Session._learn_from(me, confirm, 130.0, 132.0, 8)      # confirmed (its own gate is irrelevant)
    assert calls == [(17.0, 123.0, 125.5)] and me._last_pick is None
    assert logs == [("learned", {"freq": 17.0, "t_start": 123.0, "t_end": 125.5, "ok": True,
                                 "n_train": [3, 4]})]
    Session._learn_from(me, button, 140.0, 142.0, 9)
    Session._learn_from(me, back, 150.0, 152.0, 10)        # back: forgotten, nothing learnt
    assert me._last_pick is None and len(calls) == 1
    Session._learn_from(me, confirm, 160.0, 162.0, 11)     # confirm without a pick: nothing
    assert len(calls) == 1
    me._bank.trained = None                                # FBCCA: never learns
    Session._learn_from(me, button, 170.0, 172.0, 12)
    Session._learn_from(me, confirm, 180.0, 182.0, 13)
    assert len(calls) == 1 and me._last_pick is None

    class Failing(Bank):
        def learn(self, freq, t_start, t_end):
            raise ValueError("boom")

    me._bank = Failing()
    Session._learn_from(me, button, 190.0, 192.0, 14)
    Session._learn_from(me, confirm, 200.0, 202.0, 15)
    assert logs[-1][0] == "learned" and logs[-1][1]["ok"] is False


def test_select_reads_the_pick_layout_before_the_mode_replaces_it():
    """_select must hand _learn_from the selected target and gate of the layout the decision
    was made in, before on_selection may swap in a new layout (an idle one has no targets)."""
    from types import SimpleNamespace
    from brainsync.online import Decision

    seen: list[tuple] = []

    class Bank:
        gate = 55.5
        trained = None

    def mode_call(name, slot, now):
        me._targets = []                                   # the mode moved to an idle layout
        me._bank.gate = math.inf

    me = SimpleNamespace(_targets=[{"slot": "b1", "kind": "button", "freq": 15.0},
                                   {"slot": "more", "kind": "more", "freq": 17.0}],
                         _bank=Bank(), _selection_seq=0,
                         _transport=SimpleNamespace(send=lambda msg: None),
                         _log=lambda kind, fields: None, _mode_call=mode_call,
                         _learn_from=lambda target, gate, t_commit, layout_id:
                             seen.append((target["slot"], gate, t_commit, layout_id)),
                         paradigm="freq")
    me._target_slots = lambda: Session._target_slots(me)    # the real slot and score helpers
    me._scores = lambda dec: Session._scores(me, dec)
    d = Decision(time=57.0, scores=np.array([0.1, 0.4]), raw=1, committed=1, changed=True)
    Session._select(me, d, 3, 57.1)                        # "more" on the last page -> idle
    assert seen == [("more", 55.5, 57.0, 3)]


# -- streaming gate (v3.1) ------------------------------------------------------------------

def stall(rig: Rig, seconds: float) -> None:
    """The board delivers nothing for ``seconds``, then all of it in one read."""
    rig.board.advance(seconds)
    rig.session.step()


def test_a_pick_after_a_board_stall_is_dropped_logged_and_rearmed(capsys):
    rig = Rig()
    rig.run_slots()
    onset_and_hold(rig, "yes")
    rig.advance(1.0)
    stall(rig, 3.0)                                   # the commit falls inside the backlog
    assert rig.selections() == []
    dropped = [f for k, f in rig.records if k == "selection_dropped"]
    assert dropped and dropped[0]["slot"] == "yes"
    assert "no samples for 3.0 s" in dropped[0]["why"]
    assert "dropped a pick of 'yes'" in capsys.readouterr().err
    rig.until(lambda: rig.selections(), limit=5.0)   # the held gaze commits again
    assert rig.selections()[0]["slot"] == "yes"


def test_no_click_while_the_board_is_not_streaming():
    mode = StubMode(start=lambda now: [point_layout()],
                    on_selection=lambda slot, now: [P.click(1, 0.2, 0.2, {"id": 0, "label": slot}),
                                                    P.phase("pause", False)])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.message({"type": "gaze", "slot": "b1"})
    rig.advance(0.5)
    stall(rig, 4.0)
    assert rig.tr.of("click") == [] and rig.selections() == []
    assert ("on_selection", "b1") not in mode.calls
    assert any(k == "selection_dropped" for k, _ in rig.records)
    rig.until(lambda: rig.tr.of("click"), limit=6.0)
    assert rig.tr.of("click")[0]["element"]["label"] == "b1"


def test_steady_streaming_never_drops_a_pick():
    rig = Rig()
    rig.run_slots()
    onset_and_hold(rig, "no")
    rig.until(lambda: rig.selections(), limit=5.0)
    assert not any(k == "selection_dropped" for k, _ in rig.records)


# -- rescan and resume in point mode (v3.1) -------------------------------------------------

def test_rescan_reaches_the_point_mode_and_slots_mode_refuses_it():
    mode = StubMode(start=lambda now: [point_layout()],
                    on_message=lambda msg, now: [])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.message({"type": "rescan"})
    assert ("on_message", "rescan") in mode.calls
    slots = Rig()
    slots.run_slots()
    slots.message({"type": "rescan"})
    assert [m["code"] for m in slots.tr.to(1, "error")] == ["not_allowed"]


def test_resume_in_point_mode_asks_the_mode_for_a_fresh_scan():
    mode = StubMode(start=lambda now: [point_layout()],
                    on_message=lambda msg, now: [P.scan_request(9, 300)]
                    if msg["type"] == "resume" else [])
    rig = Rig(point_factory=lambda: mode)
    rig.connect(1, hello(mode="point", slots=None))
    rig.until(lambda: rig.session.phase == "run")
    rig.message({"type": "resume"})                   # not paused: nothing to resume
    assert rig.tr.to(1, "scan_request") == []
    rig.message({"type": "pause"})
    rig.message({"type": "resume"})
    assert rig.session.phase == "run"
    assert [m["scan_id"] for m in rig.tr.to(1, "scan_request")] == [9]


def test_resume_in_slots_mode_does_not_reach_the_mode():
    rig = Rig()
    rig.run_slots()
    rig.message({"type": "pause"})
    rig.message({"type": "resume"})
    assert rig.session.phase == "run" and rig.tr.to(1, "error") == []


# -- code mode (c-VEP, docs/CVEP-STANDARD.md section 4.2) -----------------------------------

TRAIN = 6          # training cycles in these tests (the standard records 30)
CODE_SLOTS = ("yes", "no", "help")


class CodeRig(Rig):
    """A code-mode session (CodeBank) on a virtual-time coded fake board."""

    def __init__(self, *, fake: bool = True, point_factory=None, seed: int = 0,
                 log: list | None = None, train_cycles: int = TRAIN, **kw: Any) -> None:
        from brainsync.server.coding import CodeBank
        from brainsync.stimulus import load_config
        from brainsync.stimulus.coded import CodeSequence
        from brainsync.vep.fake import SequenceFakeBoard
        self.board = SequenceFakeBoard(CodeSequence(load_config("cvep_3")), t0=T0, amp=3.0,
                                       noise=8.0, seed=seed)
        self.tr = FakeTransport()
        self.bank = CodeBank(self.board.fs, ch_names=self.board.ch_names,
                             train_cycles=train_cycles)
        self.records: list[tuple[str, dict]] = [] if log is None else log
        self.session = Session(self.tr, self.board, self.bank, fake=fake,
                               point_factory=point_factory, clock=lambda: self.board.now,
                               log=lambda k, f: self.records.append((k, f)), **kw)

    def run_code(self, conn_id: int = 1, slots=CODE_SLOTS, **kw: Any) -> dict:
        """Connect a slots controller and train; returns the running layout."""
        self.connect(conn_id, code_hello(slots=slots, **kw))
        self.until(lambda: self.session.phase == "run", limit=30.0)
        return self.session.layout


def code_hello(**kw: Any) -> P.Hello:
    kw.setdefault("slots", CODE_SLOTS)
    h = hello(**kw)
    return h


def test_code_welcome_carries_the_code_and_three_slots():
    rig = CodeRig()
    w = rig.session.catchup(1, "controller", code_hello())[0]
    assert w["paradigm"] == "code" and w["freqs"] == [] and w["max_slots"] == 3
    assert w["refresh_rates"] == [60.0]            # one bit a frame: the nominal rate only
    code = w["code"]
    assert code["lag_frames"] == 21 and code["cycle_frames"] == 63 == len(code["bits"])
    assert code["train_cycles"] == TRAIN and code["fill"] == "#ffffff"
    from brainsync.vep.live import BITS
    assert code["bits"] == list(BITS)


def test_code_slots_layout_has_lags_and_trains_on_the_first_slot():
    rig = CodeRig()
    rig.connect(1, code_hello())
    lay = rig.tr.of("layout")[0]
    assert lay["stage"] == "slots"
    assert [(t["slot"], t["lag"], t["freq"]) for t in lay["targets"]] == \
        [("yes", 0, None), ("no", 21, None), ("help", 42, None)]
    ph = rig.tr.of("phase")[-1]
    assert ph == {"type": "phase", "phase": "train", "flicker": True, "seconds": None,
                  "text": "Look at the YES square", "cue": "yes", "cycles": TRAIN}
    assert rig.board.fixated == 0 and rig.board.flickering   # the fake user follows the cue
    rig.until(lambda: rig.session.phase == "run", limit=30.0)
    progress = [m["cycle"] for m in rig.tr.of("train")]
    assert progress[0] == 0 and progress[-1] == TRAIN and progress == sorted(progress)
    assert all(m["of"] == TRAIN for m in rig.tr.of("train"))
    (done,) = rig.tr.of("trained")
    assert done["cycles"] == TRAIN and done["chance"] == 0.333 and done["accuracy"] >= 0.8
    assert "calibrate" not in rig.tr.phases()
    assert rig.tr.phases()[-1] == "run" and rig.board.fixated == -1
    kinds = rig.kinds()
    assert kinds.index("train_start") < kinds.index("trained")
    # the train phase came before the first run, and the layout did not change
    assert rig.tr.phases().index("train") < rig.tr.phases().index("run")
    assert len(rig.tr.of("layout")) == 1


def test_code_gaze_gives_decisions_and_a_selection_of_that_slot():
    rig = CodeRig()
    lay = rig.run_code()
    trained_at = rig.now
    rig.message({"type": "gaze", "slot": "help"})
    assert rig.board.fixated == 2                 # lag 42 is decoder target 2
    rig.until(lambda: rig.selections(), limit=15.0)
    sel = rig.selections()[0]
    assert sel["slot"] == "help" and sel["layout_id"] == lay["layout_id"]
    assert set(sel["scores"]) == set(CODE_SLOTS) and sel["score"] == sel["scores"]["help"]
    assert sel["score"] > 0.5 and -1.0 <= min(sel["scores"].values())   # correlations
    assert sel["t_server"] >= trained_at + P.COOLDOWN_S
    decisions = rig.tr.of("decision")
    assert decisions and all(d["layout_id"] == lay["layout_id"] for d in decisions)
    last = decisions[-1]
    assert set(last["scores"]) == set(CODE_SLOTS) and last["raw"] == "help"
    assert last["committed"] == "help" and isinstance(last["lead"], float)
    assert any(d["changed"] for d in decisions)
    # a gaze held on help selects it again after the cooldown (dwell to repeat)
    rig.until(lambda: len(rig.selections()) >= 2, limit=15.0)
    assert [s["slot"] for s in rig.selections()] == ["help", "help"]


def test_code_decisions_come_about_once_a_cycle_without_a_gaze():
    # Without a gaze the fake EEG is noise: the decisions still come (one per 1.05 s cycle),
    # with low scores. (The standard's decoder has no margin, so noise may commit too.)
    rig = CodeRig()
    rig.run_code()
    n = len(rig.tr.of("decision"))
    rig.advance(6.3)
    new = rig.tr.of("decision")[n:]
    assert 5 <= len(new) <= 7
    assert all(max(d["scores"].values()) < 0.5 for d in new)


def test_code_decisions_and_selections_only_for_the_running_layout():
    rig = CodeRig()
    rig.run_code()
    rig.message({"type": "gaze", "slot": "no"})
    rig.message({"type": "pause"})
    n = len(rig.tr.of("decision"))
    rig.advance(5.0)
    assert len(rig.tr.of("decision")) == n and rig.selections() == []
    assert not rig.board.flickering and not rig.bank.active
    rig.message({"type": "resume"})               # a short pause keeps the template
    assert rig.session.phase == "run" and rig.tr.of("trained").__len__() == 1
    rig.until(lambda: rig.selections(), limit=15.0)
    assert rig.selections()[0]["slot"] == "no"


def test_code_two_slots_never_select_the_unused_lag():
    rig = CodeRig()
    lay = rig.run_code(slots=("yes", "no"))
    assert [t["lag"] for t in lay["targets"]] == [0, 21]
    rig.message({"type": "gaze", "slot": "no"})
    rig.until(lambda: rig.selections(), limit=15.0)
    assert rig.selections()[0]["slot"] == "no"
    assert all(set(d["scores"]) == {"yes", "no"} for d in rig.tr.of("decision"))


def test_code_too_many_slots():
    rig = CodeRig()
    h = P.parse_hello({"type": "hello", "protocol": 3, "role": "controller", "mode": "slots",
                       "slots": ["a", "b", "c", "d"], "refresh_hz": 60.0, "accepted": True},
                      max_slots=4)
    rig.connect(1, h)
    (err,) = rig.tr.to(1, "error")
    assert err["code"] == "too_many_slots" and "3 targets" in err["message"]
    assert rig.session.phase == "wait"


def test_code_pause_during_training_restarts_it_on_resume():
    rig = CodeRig()
    rig.connect(1, code_hello())
    rig.until(lambda: rig.tr.of("train")[-1]["cycle"] >= 3)
    rig.message({"type": "pause"})
    assert rig.session.phase == "pause" and not rig.board.flickering
    rig.advance(2.0)
    rig.message({"type": "resume"})
    assert rig.session.phase == "train" and rig.tr.of("train")[-1]["cycle"] == 0
    assert rig.kinds().count("train_start") == 2
    rig.until(lambda: rig.session.phase == "run", limit=30.0)
    assert len(rig.tr.of("trained")) == 1


def test_code_long_pause_trains_again():
    rig = CodeRig()
    rig.run_code()
    rig.message({"type": "pause"})
    rig.advance(LONG_PAUSE_S + 1)
    rig.message({"type": "resume"})
    assert rig.session.phase == "train"
    rig.until(lambda: rig.session.phase == "run", limit=30.0)
    assert len(rig.tr.of("trained")) == 2


def test_code_template_is_kept_for_a_new_controller_and_sent_on_catchup():
    rig = CodeRig()
    rig.run_code()
    rig.drop(1)
    first = rig.connect(2, code_hello())
    assert [m["type"] for m in first][-1] == "trained"
    assert rig.session.phase == "run" and rig.kinds().count("train_start") == 1
    viewer = rig.session.catchup(3, "viewer", hello(role="viewer", mode=None, slots=None))
    assert viewer[-1]["type"] == "trained"


def test_code_template_older_than_ten_minutes_is_trained_again():
    rig = CodeRig()
    rig.run_code()
    rig.drop(1)
    rig.advance(RECALIBRATE_AFTER_S + 1)
    rig.connect(2, code_hello())
    assert rig.session.phase == "train"


def test_code_training_catchup_sends_the_progress():
    rig = CodeRig()
    rig.connect(1, code_hello())
    rig.until(lambda: rig.tr.of("train")[-1]["cycle"] >= 2)
    out = rig.session.catchup(2, "viewer", hello(role="viewer", mode=None, slots=None))
    assert out[1]["phase"] == "train" and out[-1]["type"] == "train"
    assert out[-1]["cycle"] >= 2


def test_code_controller_leaving_mid_training_drops_it():
    rig = CodeRig()
    rig.connect(1, code_hello())
    rig.until(lambda: rig.tr.of("train")[-1]["cycle"] >= 2)
    rig.drop(1)
    assert not rig.session.training and rig.bank.training and rig.bank.progress[0] == 0
    assert rig.board.fixated == -1 and not rig.board.flickering
    rig.connect(2, code_hello())
    assert rig.session.phase == "train" and rig.tr.of("train")[-1]["cycle"] == 0


class _NoEventsBoard:
    """A coded board for fake=False: noise only, never any cycle events of its own."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.fs, self.ch_names = inner.fs, inner.ch_names

    def read(self):
        return self.inner.read()


def test_code_cycles_are_mapped_to_the_board_clock_with_the_ping_offset():
    rig = CodeRig(fake=False)
    rig.session._board = _NoEventsBoard(rig.board)
    rig.connect(1, code_hello())
    lay = rig.session.layout
    rig.advance(0.5)
    rig.message({"type": "cycle", "layout_id": lay["layout_id"], "n": 0,
                 "t_client": rig.now - 0.2})       # no ping yet: the receipt time is used
    rec = [f for k, f in rig.records if k == "cycle"]
    assert rec[-1]["t_cycle"] == pytest.approx(rig.now)
    rig.message({"type": "ping", "ping_id": 1, "t_client": rig.now - 5.0})   # offset +5 s
    t_client = rig.now - 5.0 - 0.3
    rig.message({"type": "cycle", "layout_id": lay["layout_id"], "n": 1, "t_client": t_client})
    rec = [f for k, f in rig.records if k == "cycle"]
    assert rec[-1]["t_cycle"] == pytest.approx(t_client + 5.0) and rec[-1]["n"] == 1
    # never later than its receipt
    rig.message({"type": "cycle", "layout_id": lay["layout_id"], "n": 2,
                 "t_client": rig.now - 5.0 + 3.0})
    assert [f for k, f in rig.records if k == "cycle"][-1]["t_cycle"] == pytest.approx(rig.now)
    # another layout's cycle is not decoded
    rig.message({"type": "cycle", "layout_id": lay["layout_id"] + 5, "n": 3,
                 "t_client": rig.now - 5.0})
    assert [f["why"] for k, f in rig.records if k == "cycle_ignored"] == \
        ["not the running layout"]


def test_code_client_cycles_train_the_decoder_on_a_real_board():
    """fake=False: only the controller's cycle messages time the epochs. The coded board
    still makes the EEG (its flicker started by hand), and the client reports the board's
    own cycle times, as a display on the same clock would."""
    rig = CodeRig(fake=False)
    rig.connect(1, code_hello())
    lay = rig.session.layout
    rig.board.set_target(0)
    rig.board.start_flicker()
    rig.message({"type": "ping", "ping_id": 1, "t_client": rig.now})   # offset 0

    def step_with_cycles() -> None:
        rig.board.advance(DT)
        for t, kind, n in rig.board.read_events():
            if kind == "cycle":
                rig.push("message", 1, P.validate_client(
                    {"type": "cycle", "layout_id": lay["layout_id"], "n": n, "t_client": t}))
        rig.session.step()

    end = rig.now + 30.0
    while rig.session.phase != "run":
        assert rig.now < end
        step_with_cycles()
    assert rig.tr.of("trained")[0]["accuracy"] >= 0.8


def test_code_freq_server_ignores_cycles():
    rig = Rig()
    lay = rig.run_slots()
    rig.message({"type": "cycle", "layout_id": lay["layout_id"], "n": 0, "t_client": rig.now})
    assert [f["why"] for k, f in rig.records if k == "cycle_ignored"] == \
        ["the server runs --paradigm freq"]
    w = rig.session.catchup(2, "viewer", hello(role="viewer", mode=None, slots=None))[0]
    assert w["paradigm"] == "freq" and "code" not in w


def test_code_point_mode_trains_on_three_centred_patches_then_starts_the_mode():
    pick = P.layout(0, "pick", [
        P.Target("b1", 1.0, 0.0, "Sign in", "button", (P.Rect(0.1, 0.1, 0.2, 0.2),)),
        P.Target("b2", 2.0, 0.0, "Help", "button", (P.Rect(0.4, 0.1, 0.2, 0.2),)),
        P.Target("more", 3.0, 0.0, "More", "more", (P.Rect(0.7, 0.7, 0.2, 0.2),))])
    mode = StubMode(start=lambda now: [pick, P.phase("run", True)])
    rig = CodeRig(point_factory=lambda **kw: mode)
    rig.connect(1, hello(mode="point", slots=None, viewport={"w": 1600, "h": 900, "dpr": 1}))
    lay = rig.tr.of("layout")[0]
    assert lay["stage"] == "train" and [t["slot"] for t in lay["targets"]] == ["t0", "t1", "t2"]
    assert [t["lag"] for t in lay["targets"]] == [0, 21, 42]
    rects = [t["rects"][0] for t in lay["targets"]]
    assert len({r["y"] for r in rects}) == 1                       # one row
    assert abs(rects[0]["x"] + rects[2]["x"] + rects[2]["w"] - 1.0) < 1e-3   # centred
    assert rects[0]["w"] * 1600 == pytest.approx(rects[0]["h"] * 900, abs=2)  # square in px
    ph = rig.tr.of("phase")[-1]
    assert ph["phase"] == "train" and ph["cue"] == "t0" and ph["text"] == "Look at the LEFT square"
    assert mode.calls == []                         # the mode waits for the template
    rig.until(lambda: rig.session.phase == "run", limit=30.0)
    assert mode.calls[0][0] == "start"
    lay = rig.tr.of("layout")[-1]
    assert lay["stage"] == "pick"
    assert [(t["slot"], t["lag"], t["freq"]) for t in lay["targets"]] == \
        [("b1", 0, None), ("b2", 21, None), ("more", 42, None)]
    rig.message({"type": "gaze", "slot": "b2"})
    rig.until(lambda: rig.selections(), limit=15.0)
    assert rig.selections()[0]["slot"] == "b2" and ("on_selection", "b2") in mode.calls


def test_code_point_mode_trains_on_the_modes_own_train_targets():
    """BrainPoint's c-VEP PointMode: built with viewport=(w, h), trains on its
    train_targets() (b1, b2, more), cue b1, and its later layouts carry lags already."""
    rects = [P.Rect(0.2 + 0.25 * k, 0.4, 0.1, 0.2) for k in range(3)]
    kinds = ("button", "button", "more")
    train = [P.Target(s, None, 0.0, s.upper(), kinds[k], (rects[k],), lag=21 * k)
             for k, s in enumerate(("b1", "b2", "more"))]
    pick = P.layout(0, "pick", [P.Target("b1", None, 0.0, "Sign in", "button", (rects[0],),
                                         lag=0),
                                P.Target("more", None, 0.0, "More", "more", (rects[2],),
                                         lag=42)])

    class LagMode(StubMode):
        def train_targets(self):
            return train

    mode = LagMode(start=lambda now: [pick, P.phase("run", True)])
    made: list[dict] = []

    def factory(**kw):
        made.append(kw)
        return mode

    rig = CodeRig(point_factory=factory)
    rig.connect(1, hello(mode="point", slots=None, viewport={"w": 1600, "h": 900, "dpr": 2}))
    assert made == [{"viewport": (1600.0, 900.0)}]
    lay = rig.tr.of("layout")[0]
    assert lay["stage"] == "train" and [t["slot"] for t in lay["targets"]] == ["b1", "b2", "more"]
    ph = rig.tr.of("phase")[-1]
    assert ph["phase"] == "train" and ph["cue"] == "b1" and ph["text"] == "Look at the B1 square"
    rig.until(lambda: rig.session.phase == "run", limit=30.0)
    lay = rig.tr.of("layout")[-1]
    assert lay["stage"] == "pick" and [t["lag"] for t in lay["targets"]] == [0, 42]
    rig.message({"type": "gaze", "slot": "more"})
    assert rig.board.fixated == 2
    rig.until(lambda: rig.selections(), limit=15.0)
    assert rig.selections()[0]["slot"] == "more"


def test_code_failed_training_says_why_and_starts_again():
    rig = CodeRig()
    rig.connect(1, code_hello())
    rig.until(lambda: rig.tr.of("train")[-1]["cycle"] >= 2)
    rig.bank.retrain()
    rig.bank.train_error = "only 0 training cycle(s) could be cut"
    rig.advance(DT)
    (err,) = rig.tr.to(1, "error")
    assert err["code"] == "internal" and "Training failed" in err["message"]
    assert rig.tr.of("train")[-1]["cycle"] == 0 and rig.session.phase == "train"
    assert ("train_failed" in rig.kinds()) and rig.bank.train_error == ""
    rig.until(lambda: rig.session.phase == "run", limit=30.0)


def test_code_onset_is_only_logged():
    rig = CodeRig()
    lay = rig.run_code()
    rig.message({"type": "onset", "layout_id": lay["layout_id"], "t_client": rig.now})
    assert [k for k, _ in rig.records].count("onset") == 1
