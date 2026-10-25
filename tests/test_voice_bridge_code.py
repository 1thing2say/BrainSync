"""brainsync.voice_bridge --paradigm code: c-VEP for the phone (INTERFACE-v2-draft.md section 9).

The engine tests drive brainsync.vep.phone.CodeEngine (on vep.live.CodeVepDecoder) and MarkerFakeBoard in virtual time (a
fake clock, ``step`` called by hand, markers handed in as the server would relay them), so
training and twenty decoded trials take well under a second of real time. The scripted-server
tests run a Bridge with a stub engine (the hello, not_trained, train / train_done routing, the
trained flag). The last test runs the real bridge (fake board, real decoder, real time)
against tools/contract_check.py playing the server and the phone.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import numpy as np
import pytest

from app.config import load_config
from app.schemas import DecoderHello, hello_problems
from brainsync import voice_bridge as vb
from brainsync.stimulus import load_config as load_stimulus
from brainsync.vep.codes import m_sequence
from brainsync.vep.cvep import Template
from brainsync.vep.phone import (CodeEngine, MarkerFakeBoard, code_bits, code_hello,
                                 cycle_samples, load_template)
from tests.test_voice_bridge import Peer, with_server
from tools.contract_check import CODE_CONFIG, Report, build_parser, serve_once

CVEP = load_stimulus("cvep_2")
CYCLE_S = 63 / 60
PHONE = load_config(CODE_CONFIG)


# ---------------------------------------------------------------------------
# The hello
# ---------------------------------------------------------------------------


def test_code_hello_shape_and_server_check():
    hello = code_hello(CVEP, "cvep-template", {"window_s": 2.1, "step_s": 1.05, "votes": 2})
    parsed = DecoderHello.model_validate(hello)
    assert parsed.paradigm == "code" and parsed.waveform == "square" and parsed.refresh_hz == 60.0
    assert parsed.code.length == 63 and parsed.code.frames_per_bit == 1
    assert parsed.code.bits == "".join(str(b) for b in m_sequence(63)) == code_bits(CVEP)
    assert [(s.slot, s.lag_frames, s.freq_hz) for s in parsed.slots] == [("s1", 0, None),
                                                                         ("s2", 31, None)]
    assert all("freq_hz" not in s for s in hello["slots"])
    assert hello["score_kind"] == "cvep_correlation"
    code = PHONE.stim.code
    assert hello_problems(parsed, PHONE.stim.slots, PHONE.stim.waveform,
                          PHONE.display.expected_refresh_hz, paradigm="code",
                          code_length=code.length, frames_per_bit=code.frames_per_bit) == []


def test_code_names_defaults_and_bad_arguments(capsys):
    p = vb.build_parser()
    assert vb.bridge_name(p.parse_args(["--paradigm", "code", "--fake"])) == "fake-cvep-template"
    assert vb.bridge_name(p.parse_args(["--paradigm", "code"])) == "cvep-template"
    assert vb.config_name(p.parse_args(["--paradigm", "code"])) == "cvep_2"
    assert vb.config_name(p.parse_args([])) == "four_diamond"
    for argv in (["--paradigm", "code", "--fake", "--fast"], ["--template", "x.npz"],
                 ["--paradigm", "code", "--cycles", "0"], ["--paradigm", "code", "--code-votes", "0"]):
        with pytest.raises(SystemExit) as exc:
            vb.main(argv)
        assert exc.value.code == 2
    # a config of the other paradigm, a missing template: exit 2 before anything starts
    assert vb.main(["--paradigm", "code", "--fake", "-c", "four_diamond"]) == 2
    assert vb.main(["--fake", "--no-stimulus", "-c", "cvep_2"]) == 2
    assert vb.main(["--paradigm", "code", "--fake", "--template", "/no/such.npz"]) == 2
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The engine in virtual time
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class NowLoop:
    """call_soon_threadsafe stand-in: the engine's results arrive at once."""

    def call_soon_threadsafe(self, fn, *args) -> None:
        fn(*args)


class Rig:
    """A CodeEngine on a MarkerFakeBoard in virtual time, playing the phone's markers."""

    def __init__(self, tmp_path: Path, template: Template | None = None, latency: float = 0.3,
                 seed: int = 1) -> None:
        self.clock = Clock()
        self.board = MarkerFakeBoard(CVEP, clock=self.clock, seed=seed).open()
        self.engine = CodeEngine(self.board, CVEP, template=template, fake=True, seed=seed + 1,
                                 save_dir=tmp_path)
        self.out: list[tuple] = []
        self.engine.attach(NowLoop(), lambda *a: self.out.append(("commit", *a)),
                           lambda *a: self.out.append(("baseline", *a)),
                           lambda *a: self.out.append(("train", *a)))
        self.latency = latency
        self.advance(1.0)

    def advance(self, seconds: float) -> None:
        for _ in range(int(round(seconds / 0.05))):
            self.clock.t += 0.05
            self.engine.step()

    def trial(self, trial_id: int, active: list[int], train: tuple[int, int, int] | None = None,
              max_s: float = 12.0, stop_after: float | None = None,
              markers: bool = True) -> tuple[list[tuple], float]:
        """Run one trial until the engine answers (or ``max_s`` / ``stop_after``); what it
        answered and how long it took (virtual seconds)."""
        self.engine.start_trial(trial_id, active, self.clock())
        if train is not None:
            self.engine.start_training(train[0], trial_id, train[1], train[2])
        t0, k, began, n0 = self.clock() + 0.1, 0, self.clock(), len(self.out)
        limit = max_s if stop_after is None else stop_after
        while self.clock() - began < limit and len(self.out) == n0:
            self.clock.t += 0.05
            while markers and t0 + k * CYCLE_S + self.latency <= self.clock():
                self.engine.add_markers(trial_id, [(k, t0 + k * CYCLE_S)])
                k += 1
            self.engine.step()
        took = self.clock() - began
        self.engine.stop()
        self.engine.step()
        return self.out[n0:], took


def test_engine_trains_then_decodes_both_targets(tmp_path):
    rig = Rig(tmp_path)
    assert not rig.engine.trained
    rig.engine.start_baseline(5, 10.0)   # c-VEP needs no rest baseline: ok at once
    rig.advance(0.1)
    assert rig.out.pop() == ("baseline", 5, True, "")
    got, took = rig.trial(1, [0, 1], train=(7, 0, 12), max_s=30)
    (kind, train_id, ok, acc, cycles, message), = got
    assert (kind, train_id, ok, cycles) == ("train", 7, True, 12)
    assert acc >= 0.9 and "leave-one-out" in message
    assert 12 * CYCLE_S < took < 12 * CYCLE_S + 4   # the cycles, the pad and the board's lag
    assert rig.engine.trained and rig.engine.template_path is not None
    saved = list(tmp_path.glob("*_cvep_template.npz"))
    assert saved == [rig.engine.template_path]
    hits = 0
    for i in range(20):
        got, took = rig.trial(10 + i, [0, 1])
        assert [g[:2] for g in got] == [("commit", 10 + i)], got
        hits += got[0][2] == rig.board.fixated
        assert took < 8.0
        assert set(np.round(got[0][3], 3)) and got[0][3].shape == (2,)
    assert hits >= 18


def test_a_saved_template_loads_and_decodes(tmp_path):
    rig = Rig(tmp_path)
    rig.trial(1, [0, 1], train=(1, 1, 10), max_s=30)   # cued on s2: shifted back to s1
    tpl = load_template(rig.engine.template_path, CVEP, rig.board.fs)
    assert tpl.n_samples == cycle_samples(CVEP, rig.board.fs) and tpl.n_targets == 2
    fresh = Rig(tmp_path, template=tpl, seed=5)
    assert fresh.engine.trained
    hits = 0
    for i in range(1, 9):
        got, _ = fresh.trial(i, [0, 1])
        hits += bool(got) and got[0][2] == fresh.board.fixated
    assert hits >= 7
    with pytest.raises(ValueError, match="does not fit"):
        load_template(rig.engine.template_path, load_stimulus("cvep_4"), rig.board.fs)


def test_markers_a_cycle_late_still_line_up(tmp_path):
    rig = Rig(tmp_path, latency=1.1, seed=7)
    (_, _, ok, acc, _, _), = rig.trial(1, [0, 1], train=(1, 0, 10), max_s=30)[0]
    assert ok and acc >= 0.9
    hits = 0
    for i in range(8):
        got, _ = rig.trial(10 + i, [0, 1])
        hits += bool(got) and got[0][2] == rig.board.fixated
    assert hits >= 7


def test_one_active_target_is_the_only_answer(tmp_path):
    rig = Rig(tmp_path)
    rig.trial(1, [0, 1], train=(1, 0, 10), max_s=30)
    for i in range(4):
        got, _ = rig.trial(20 + i, [1])
        assert got and got[0][2] == 1


def test_training_stopped_early_fails_with_the_reason(tmp_path):
    rig = Rig(tmp_path)
    got, _ = rig.trial(1, [0, 1], train=(3, 0, 20), stop_after=6.0)
    (kind, train_id, ok, acc, cycles, message), = got
    assert (kind, train_id, ok, acc) == ("train", 3, False, None)
    assert 0 < cycles < 20 and "stopped after" in message
    assert not rig.engine.trained


def test_train_for_a_trial_that_is_not_running_fails(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.start_training(4, 99, 0, 10)
    rig.advance(0.1)
    assert rig.out == [("train", 4, False, None, 0, "trial 99 is not running")]


def test_no_markers_no_answer_and_other_trials_markers_ignored(tmp_path):
    rig = Rig(tmp_path)
    rig.trial(1, [0, 1], train=(1, 0, 10), max_s=30)
    got, _ = rig.trial(2, [0, 1], max_s=6.0, markers=False)
    assert got == []
    # markers of another trial never reach this one
    n0 = len(rig.out)
    rig.engine.start_trial(3, [0, 1], rig.clock())
    for k in range(6):
        rig.engine.add_markers(2, [(k, rig.clock() + 0.1 + k * CYCLE_S)])
    rig.advance(8.0)
    assert rig.out[n0:] == []


# ---------------------------------------------------------------------------
# The bridge with a stub engine
# ---------------------------------------------------------------------------


class StubCodeEngine:
    """Stands in for CodeEngine: a baseline finishes at once; a training trial trains at once
    after its train arrives; a trained trial commits ``commit_to`` once its first markers came."""

    decoder_params = {"window_s": 2.1, "step_s": 1.05, "votes": 2}
    score_kind = "cvep_correlation"

    def __init__(self, trained: bool = False, commit_to: int | None = 0) -> None:
        self.trained = trained
        self.commit_to = commit_to
        self.calls: list[tuple] = []
        self.trial: tuple[int, list[int]] | None = None

    def attach(self, loop, on_commit, on_baseline, on_train) -> None:
        self.loop, self.on_commit, self.on_baseline, self.on_train = (loop, on_commit,
                                                                      on_baseline, on_train)

    def start(self) -> None:
        pass

    def start_baseline(self, baseline_id: int, seconds: float) -> None:
        self.loop.call_soon_threadsafe(self.on_baseline, baseline_id, True, "")

    def start_trial(self, trial_id: int, targets: list[int], t_start: float) -> None:
        self.calls.append(("trial", trial_id, list(targets)))
        self.trial = (trial_id, list(targets))

    def add_markers(self, trial_id: int, markers) -> None:
        self.calls.append(("markers", trial_id, list(markers)))
        if self.trained and self.commit_to is not None and self.trial and self.trial[0] == trial_id:
            k = self.trial[1][self.commit_to]
            self.loop.call_soon_threadsafe(self.on_commit, trial_id, k, np.array([0.3, 0.1]))

    def start_training(self, train_id: int, trial_id: int, target: int, cycles: int) -> None:
        self.calls.append(("train", train_id, trial_id, target, cycles))
        self.trained = True
        self.loop.call_soon_threadsafe(self.on_train, train_id, True, 0.95, cycles, "fitted")

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.trial = None

    def board_ok(self) -> bool:
        return True

    @property
    def message(self) -> str:
        return "stub c-VEP board"

    def close(self) -> None:
        pass


def code_bridge(url: str, engine: StubCodeEngine) -> vb.Bridge:
    args = vb.build_parser().parse_args(["--url", url, "--paradigm", "code", "--fake",
                                         "--reconnect-s", "0.1", "--status-interval", "0.2"])
    return vb.Bridge(args, CVEP, engine, vb.PhoneDisplay(CVEP.refresh))


async def code_handshake(peer: Peer) -> dict:
    hello = await peer.recv("hello")
    await peer.send(type="hello_ack", protocol=2, roles={"s1": "left", "s2": "right"},
                    labels={"s1": "Left", "s2": "Right"})
    await peer.send(type="baseline", baseline_id=1, seconds=0.1)
    assert (await peer.recv("baseline_done"))["ok"] is True
    return hello


def marker(trial_id: int, cycle: int, t: float) -> dict:
    return {"type": "stim_markers", "trial_id": trial_id, "markers": [{"cycle": cycle, "t_unix": t}]}


async def test_code_bridge_untrained_then_trained(tmp_path):
    engine = StubCodeEngine()

    async def script(peer: Peer) -> None:
        hello = await code_handshake(peer)
        assert hello["name"] == "fake-cvep-template" and hello["paradigm"] == "code"
        assert hello["decoder"] == StubCodeEngine.decoder_params
        status = await peer.recv("status")
        assert status["trained"] is False and "the phone draws" in status["message"]
        # untrained: not_trained, but only at the trial's first marker (never before it)
        await peer.send(type="stim_start", trial_id=1, active_slots=["s1", "s2"])
        await peer.quiet(0.3)
        await peer.send(**marker(1, 0, 1000.0))
        assert await peer.recv("no_selection") == {"type": "no_selection", "trial_id": 1,
                                                   "reason": "not_trained"}
        await peer.send(type="stim_stop", trial_id=1, reason="no_selection")
        # a training trial: train right after stim_start -> train_done, then trained
        await peer.send(type="stim_start", trial_id=2, active_slots=["s1", "s2"])
        await peer.send(type="train", train_id=1, trial_id=2, slot="s1", cycles=20)
        done = await peer.recv("train_done")
        assert done == {"type": "train_done", "train_id": 1, "ok": True, "accuracy": 0.95,
                        "cycles": 20, "message": "fitted"}
        assert (await peer.recv("status"))["trained"] is True
        await peer.send(**marker(2, 0, 1001.0))   # a training trial never answers
        await peer.quiet(0.3)
        await peer.send(type="stim_stop", trial_id=2, reason="selected")
        # trained: the first commit after the first marker, no stim_onset ever
        await peer.send(type="stim_start", trial_id=3, active_slots=["s1", "s2"])
        await peer.send(**marker(99, 0, 1002.0))   # another trial's markers: ignored
        await peer.quiet(0.2)
        await peer.send(**marker(3, 0, 1003.0))
        sel = await peer.recv("selection")
        assert sel["trial_id"] == 3 and sel["slot"] == "s1" and sel["scores"] == {"s1": 0.3, "s2": 0.1}
        await peer.send(type="stim_stop", trial_id=3, reason="selected")
        # train for a trial that is not running, or a slot that is not active: ok false
        await peer.send(type="train", train_id=2, trial_id=9, slot="s1", cycles=20)
        done = await peer.recv("train_done")
        assert (done["ok"], done["message"]) == (False, "trial 9 is not running")
        await peer.send(type="stim_start", trial_id=4, active_slots=["s2"])
        await peer.send(type="train", train_id=3, trial_id=4, slot="s1", cycles=20)
        assert "not active" in (await peer.recv("train_done"))["message"]
        await peer.quiet(0.2)
        await peer.ws.close(4000, "test over")

    bridge, code = await with_server(script, lambda url: code_bridge(url, engine))
    assert code == 0
    assert ("train", 1, 2, 0, 20) in engine.calls
    assert ("markers", 99, [(0, 1002.0)]) not in engine.calls


async def test_code_bridge_ignores_freq_only_and_keeps_markers_from_late_trials(tmp_path):
    engine = StubCodeEngine(trained=True)

    async def script(peer: Peer) -> None:
        await code_handshake(peer)
        await peer.send(type="stim_start", trial_id=1, active_slots=["s1", "s2"])
        await peer.send(type="stim_markers", trial_id=1, markers=[{"cycle": "x", "t_unix": 1.0}])
        await peer.quiet(0.2)   # nothing valid: no onset, no answer
        await peer.send(type="stim_markers", trial_id=1,
                        markers=[{"cycle": 1, "t_unix": 1001.05}, {"cycle": 0, "t_unix": 1000.0}])
        sel = await peer.recv("selection")
        assert sel["t_unix"] >= 1000.0
        await peer.ws.close(4000, "test over")

    _, code = await with_server(script, lambda url: code_bridge(url, engine))
    assert code == 0


# ---------------------------------------------------------------------------
# The real bridge passes tools/contract_check.py in code mode
# ---------------------------------------------------------------------------


CODE_CFG = PHONE.model_copy(update={
    "stim": PHONE.stim.model_copy(update={"cue_s": 0.02, "read_s_per_word": 0.0,
                                          "confirm_cue_s": 0.02, "rest_s": 0.02,
                                          "max_trial_s": 8.0}),
})


async def test_fake_code_bridge_passes_contract_check():
    out = io.StringIO()
    report = Report(out=out)
    args = build_parser(CODE_CFG).parse_args([
        "--host", "127.0.0.1", "--port", "0", "--trials", "2", "--baseline-s", "0.3",
        "--baseline-timeout", "5", "--hello-timeout", "10", "--quiet-s", "0.5", "--wait", "80",
        "--train-cycles", "6"])
    bound: asyncio.Queue[int] = asyncio.Queue()
    check = asyncio.create_task(serve_once(CODE_CFG, args, report, bound.put_nowait))
    port = await asyncio.wait_for(bound.get(), 5)
    bridge_args = vb.build_parser().parse_args([
        "--url", f"ws://127.0.0.1:{port}/ws/decoder", "--paradigm", "code", "--fake",
        "--no-reconnect", "--status-interval", "0.2", "--seed", "3"])
    client = asyncio.create_task(vb.serve(bridge_args))
    await asyncio.wait_for(check, 90)
    code = await asyncio.wait_for(client, 10)
    text = out.getvalue()
    assert report.ok, text
    assert code == 0
    assert "PASS  hello matches the code config" in text
    assert "PASS  trial 1: untrained, no_selection not_trained" in text
    assert "PASS  train 1: template fitted" in text
    assert "PASS  status says trained after train_done" in text
    for trial in (3, 4):
        assert f"PASS  trial {trial}: selection is an active slot" in text, text
    assert "PASS  nothing answers the paused trial" in text
