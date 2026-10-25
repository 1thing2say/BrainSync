"""brainsync.voice_bridge in code mode (c-VEP, the default; docs/CVEP-STANDARD.md).

The pure helpers (the code hello, the bar lines under the squares), the voice_cvep3 stimulus
config, the CodeEngine on the fake board in virtual time (it trains, then commits to the code the
fake user looks at), the real bridge against tools/contract_check.py, and a Bridge with a stub
code engine against scripted servers for the display lines and the train / decision rules.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import random
import time

import numpy as np
import pytest

from app.config import load_config
from app.schemas import DecoderHello, hello_problems, parse_decoder_to_server
from brainsync import voice_bridge as vb
from brainsync.stimulus import load_config as load_stimulus
from brainsync.stimulus.display import text_overlaps
from tests.test_voice_bridge import Peer, with_server
from tools.contract_check import Report, build_parser, serve_once

ON, OFF = vb.FLICKER_ON, vb.FLICKER_OFF
CFG = load_config()  # the default: c-VEP


# ---------------------------------------------------------------------------
# Pure helpers and the stimulus config
# ---------------------------------------------------------------------------


def test_voice_cvep3_is_the_standard_with_room_for_sentences():
    cfg = load_stimulus(vb.DEFAULT_CODE_CONFIG)
    assert cfg.paradigm == "code" and cfg.n_targets == 3
    assert (cfg.code.length, cfg.code.lag_frames, cfg.code.cycle_frames) == (63, 21, 63)
    assert cfg.fill == (1.0, 1.0, 1.0) and cfg.refresh == 60.0
    assert text_overlaps(cfg) == []  # a sentence (and its bar) under each square, clear of the rest
    # The same lags as config/default.yaml, so the bridge's hello passes the server's check.
    assert [k * cfg.code.lag_frames for k in range(3)] == [s.lag_frames for s in CFG.stim.slots]


def test_code_hello_matches_the_default_server_config():
    cfg = load_stimulus(vb.DEFAULT_CODE_CONFIG)
    hello = vb.code_hello_message(cfg, 59.94, "brainsync-cvep",
                                  {"window_s": 2.1, "step_s": 1.05, "votes": 2}, 30)
    assert hello["paradigm"] == "code" and "waveform" not in hello
    assert hello["slots"] == [{"slot": "s1", "lag_frames": 0}, {"slot": "s2", "lag_frames": 21},
                              {"slot": "s3", "lag_frames": 42}]
    assert hello["code"] == {"length": 63, "cycle_frames": 63, "train_cycles": 30}
    assert hello["score_kind"] == "correlation"
    parsed = DecoderHello.model_validate(hello)
    assert hello_problems(parsed, CFG.stim.slots, CFG.stim.waveform,
                          CFG.display.expected_refresh_hz, paradigm="code",
                          code=CFG.stim.code) == []
    with pytest.raises(ValueError):
        vb.code_hello_message(load_stimulus("four_diamond"), 60.0, "x", None, 30)


def test_bar_lines_and_the_status_line():
    assert vb.score_line(0.42, True) == "███░░░░░ +0.42 <"
    assert vb.score_line(-0.03, False) == "░░░░░░░░ -0.03"
    assert vb.score_line(math.nan, False) == "░░░░░░░░   -  "
    labels = ["Yes, let's go outside.", "Not right now.", "More options"]
    lines = vb.scored_labels(labels, np.array([0.42, 0.11, -0.03]), [0, 1, 2], raw=0)
    assert lines == ["Yes, let's go outside.\n███░░░░░ +0.42 <", "Not right now.\n█░░░░░░░ +0.11",
                     "More options\n░░░░░░░░ -0.03"]
    # The confirm screen: only the active squares get a bar; an empty label gets the bar alone.
    lines = vb.scored_labels(["Say it", "", "Back"], np.array([0.5, 0.0, 0.1]), [0, 2], raw=0)
    assert lines == ["Say it\n████░░░░ +0.50 <", "", "Back\n█░░░░░░░ +0.10"]
    assert vb.status_line(0.31, 0, labels) == "lead 0.31   choice: Yes, let's go outside."
    assert vb.status_line(None, vb.ABSTAIN, labels) == "lead  -     no choice yet"


def test_code_is_the_default_paradigm_and_follows_the_config():
    p = vb.build_parser()
    args = vb.resolve(p.parse_args([]))
    assert (args.paradigm, args.config) == ("code", "voice_cvep3")
    assert vb.resolve(p.parse_args(["--paradigm", "freq"])).config == "four_diamond"
    assert vb.resolve(p.parse_args(["-c", "four_diamond"])).paradigm == "freq"
    assert vb.resolve(p.parse_args(["-c", "cvep_3"])).paradigm == "code"
    assert vb.stimulus_args(p.parse_args([]))[:2] == ["-c", "voice_cvep3"]


@pytest.mark.parametrize("argv", [["--cycles", "0"], ["--train-cycles", "1"],
                                  ["--code-votes", "0"], ["--code-margin", "-1"]])
def test_bad_code_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        vb.main(argv)
    assert exc.value.code == 2


def test_a_headless_code_bridge_on_the_headset_is_allowed():
    # The session page draws the code (display.flicker: browser) and the server relays its
    # cycles, so --no-stimulus no longer needs --fake. Only the arguments are checked here:
    # nothing opens the board.
    p = vb.build_parser()
    args = p.parse_args(["--no-stimulus"])
    vb.check_arguments(p, args)
    assert args.paradigm == "code" and not args.fake


def test_a_freq_config_with_paradigm_code_exits_2(capsys):
    assert vb.main(["--fake", "--no-stimulus", "--paradigm", "code", "-c", "four_diamond"]) == 2
    assert "no [code] table" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The CodeEngine on the fake board, in virtual time
# ---------------------------------------------------------------------------


async def test_code_engine_trains_then_commits_to_the_looked_at_active_target():
    args = vb.resolve(vb.build_parser().parse_args(["--fake", "--fast", "--seed", "2"]))
    cfg, board, decoder = vb.build_code(args)
    engine = vb.CodeEngine(board, decoder, fake=True, fast=True, seed=7, refresh=cfg.refresh)
    assert engine.decoder_params == {"window_s": 2.1, "step_s": 1.05, "votes": 2}
    assert engine.score_kind == "correlation" and not engine.trained
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    engine.attach(loop, lambda *a: events.put_nowait(("commit", *a)))
    engine.attach_code(lambda *a: events.put_nowait(("progress", *a)),
                       lambda *a: events.put_nowait(("trained", *a)),
                       lambda *a: events.put_nowait(("decision", *a)))
    engine.start()
    try:
        engine.start_train(1, 0, 10, True, time.time())
        cycles = []
        while True:
            ev = await asyncio.wait_for(events.get(), 30)
            if ev[0] == "trained":
                break
            assert ev[:2] == ("progress", 1)
            cycles.append(ev[2])
        _, train_id, ok, accuracy, chance, n, message = ev
        assert (train_id, ok, n, message) == (1, True, 10, "")
        assert accuracy >= 0.5 and chance == pytest.approx(1 / 3)
        assert cycles == sorted(cycles) and cycles[-1] == 10
        assert engine.trained
        # A train without retrain is answered at once, from the template.
        engine.start_train(2, 0, 10, False, time.time())
        ev = await asyncio.wait_for(events.get(), 5)
        assert ev[:3] == ("trained", 2, True)
        # Trials: the fake user looks at a random active target (the engine's own rng).
        rng = random.Random(7)
        for trial_id, active in ((3, [0, 1, 2]), (4, [0, 2]), (5, [0, 1, 2]), (6, [0, 2])):
            looked = rng.choice(active)
            engine.start_trial(trial_id, active, time.time())
            decisions = []
            while True:
                ev = await asyncio.wait_for(events.get(), 30)
                if ev[0] == "commit":
                    break
                assert ev[0] == "decision" and ev[1] == trial_id
                decisions.append(ev)
            assert ev[1:3] == (trial_id, looked), (active, looked, decisions)
            assert decisions and decisions[-1][4] == looked and decisions[-1][6] is True
            assert all(d[3] in active or d[3] == vb.ABSTAIN for d in decisions)
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# The real code bridge passes tools/contract_check.py
# ---------------------------------------------------------------------------

CHECK_CFG = CFG.model_copy(update={
    "stim": CFG.stim.model_copy(update={"cue_s": 0.02, "rest_s": 0.02, "max_trial_s": 8.0}),
})


async def test_fake_code_bridge_passes_contract_check():
    out = io.StringIO()
    report = Report(out=out)
    args = build_parser(CHECK_CFG).parse_args([
        "--host", "127.0.0.1", "--port", "0", "--trials", "4", "--train-cycles", "8",
        "--train-timeout", "30", "--hello-timeout", "10", "--quiet-s", "0.5", "--wait", "30",
    ])
    bound: asyncio.Queue[int] = asyncio.Queue()
    check = asyncio.create_task(serve_once(CHECK_CFG, args, report, bound.put_nowait))
    port = await asyncio.wait_for(bound.get(), 5)
    bridge_args = vb.build_parser().parse_args([
        "--url", f"ws://127.0.0.1:{port}/ws/decoder", "--fake", "--fast", "--no-stimulus",
        "--no-reconnect", "--status-interval", "0.2", "--seed", "3"])
    client = asyncio.create_task(vb.serve(bridge_args))
    await asyncio.wait_for(check, 90)
    code = await asyncio.wait_for(client, 10)
    text = out.getvalue()
    assert report.ok, text
    assert code == 0
    assert "PASS  hello matches config/default.yaml" in text
    assert "PASS  train 1 trained" in text and "PASS  train 2 trained" in text
    assert "PASS  train 1: train_progress while recording" in text
    assert "INFO  trial 2: confirm trial: only s1, s3 flicker" in text
    for trial in range(1, 5):
        assert f"PASS  trial {trial}: selection is an active slot" in text, text
        assert f"PASS  trial {trial}: a decision before the selection (code mode)" in text, text
        assert f"PASS  trial {trial}: decision scores the active slots" in text, text
    assert "PASS  nothing answers the paused trial" in text


# ---------------------------------------------------------------------------
# Scripted servers against a Bridge with a stub code engine
# ---------------------------------------------------------------------------


class StubCodeEngine:
    """Stands in for CodeEngine: a training reports two cycles and its template (or waits for
    ``finish_train``); a trial sends one decision and commits ``commit_to`` (an index into its
    active targets) at once."""

    paradigm = "code"
    decoder_params = {"window_s": 2.1, "step_s": 1.05, "votes": 2}
    score_kind = "correlation"
    cycle_s = 1.05

    def __init__(self, commit_to: int | None = 0, hold_train: bool = False) -> None:
        self.commit_to = commit_to
        self.hold_train = hold_train
        self.calls: list[tuple] = []
        self.trained = False
        self.decoder = type("D", (), {"train_cycles": 30})()

    def attach(self, loop, on_commit, on_baseline=None) -> None:
        self.loop, self.on_commit = loop, on_commit

    def attach_code(self, on_progress, on_trained, on_decision) -> None:
        self.on_progress, self.on_trained, self.on_decision = on_progress, on_trained, on_decision

    def start(self) -> None:
        pass

    def start_train(self, train_id, target, cycles, retrain, t_start) -> None:
        self.calls.append(("train", train_id, target, cycles, retrain))
        if not retrain and self.trained:
            self.loop.call_soon(self.on_trained, train_id, True, 0.9, 1 / 3, cycles, "")
            return
        self.loop.call_soon(self.on_progress, train_id, 1, cycles)
        self.loop.call_soon(self.on_progress, train_id, 2, cycles)
        if not self.hold_train:
            self.loop.call_soon(self.finish_train, train_id, cycles)

    def finish_train(self, train_id, cycles) -> None:
        self.trained = True
        self.on_trained(train_id, True, 0.9, 1 / 3, cycles, "")

    def start_trial(self, trial_id, targets, t_start) -> None:
        self.calls.append(("trial", trial_id, list(targets)))
        if self.commit_to is None:
            return
        k = targets[self.commit_to]
        scores = np.full(3, 0.05)
        scores[k] = 0.42
        # one decision a little after the onset, then the commit
        self.loop.call_later(0.05, self.on_decision, trial_id, scores, k, vb.ABSTAIN, 0.37, False)
        self.loop.call_later(0.1, self.on_decision, trial_id, scores, k, k, 0.37, True)
        self.loop.call_later(0.1, self.on_commit, trial_id, k, scores)

    def stop(self) -> None:
        self.calls.append(("stop",))

    def add_event(self, *event) -> None:
        self.calls.append(("event", *event))

    def board_ok(self) -> bool:
        return True

    @property
    def message(self) -> str:
        return "stub board"

    def close(self) -> None:
        pass


def make_code_bridge(url: str, engine: StubCodeEngine | None = None,
                     display: vb.HeadlessDisplay | None = None, *extra: str) -> vb.Bridge:
    args = vb.build_parser().parse_args(["--url", url, "--fake", "--no-stimulus",
                                         "--reconnect-s", "0.1", "--status-interval", "0.2", *extra])
    cfg = load_stimulus(vb.DEFAULT_CODE_CONFIG)
    return vb.Bridge(args, cfg, engine or StubCodeEngine(), display or vb.HeadlessDisplay(cfg.refresh))


async def next_of(peer: Peer, *types: str, timeout: float = 3.0) -> dict:
    """The next message of one of ``types``, skipping (but schema-checking) every other one."""
    deadline = time.monotonic() + timeout
    while True:
        raw = await asyncio.wait_for(peer.ws.recv(), max(deadline - time.monotonic(), 0.01))
        parse_decoder_to_server(raw)
        msg = json.loads(raw)
        if msg["type"] in types:
            return msg


ROLES = {"s1": "option", "s2": "option", "s3": "next"}
LABELS = {"s1": "Yes, please.", "s2": "No, thank you.", "s3": "More options"}


async def code_handshake(peer: Peer, train: bool = True) -> dict:
    hello = await peer.recv("hello")
    await peer.send(type="hello_ack", protocol=2, roles=ROLES, labels=LABELS)
    if train:
        await peer.send(type="train", train_id=1, slot="s1", label="left", cycles=30, retrain=True)
        assert (await peer.recv("train_progress"))["cycle"] == 1
        assert (await peer.recv("train_progress"))["cycle"] == 2
        done = await peer.recv("trained")
        assert done == {"type": "trained", "train_id": 1, "ok": True, "cycles": 30, "message": "",
                        "accuracy": 0.9, "chance": 0.3333}
    return hello


async def test_code_hello_training_and_its_display_lines():
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        hello = await code_handshake(peer)
        assert hello["paradigm"] == "code" and hello["name"] == "fake-brainsync-cvep"
        assert [s["lag_frames"] for s in hello["slots"]] == [0, 21, 42]
        assert hello["decoder"] == StubCodeEngine.decoder_params
        await peer.ws.close(4000, "done")

    bridge, code = await with_server(script, lambda url: make_code_bridge(url, display=display))
    assert code == 0
    lines = display.lines
    labels = json.dumps({"labels": ["Yes, please.", "No, thank you.", "More options"]})
    start = lines.index('{"active": [0, 1, 2]}')
    assert lines.index(labels) < start  # the hello_ack labels first
    assert lines[start:start + 4] == ['{"active": [0, 1, 2]}', '{"text": "Look at the left square"}',
                                      "0", ON]
    assert '{"text": "training: keep looking at the left square   cycle 2/30"}' in lines
    done = lines.index('{"text": "template ready: 90 % on held-out cycles (chance 33 %)"}')
    assert OFF in lines[start + 4:done] and "-1" in lines[start + 4:done]  # flicker off, box back


async def test_a_trial_sends_decisions_and_draws_bars_under_the_active_squares():
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await code_handshake(peer)
        await peer.send(type="display", text="", labels=LABELS, highlight=None)
        await peer.send(type="stim_start", trial_id=7, active_slots=["s1", "s2", "s3"])
        onset = await peer.recv("stim_onset")
        first = await peer.recv("decision")
        assert first == {"type": "decision", "trial_id": 7,
                         "scores": {"s1": 0.42, "s2": 0.05, "s3": 0.05}, "raw": "s1",
                         "committed": None, "lead": 0.37, "changed": False}
        second = await peer.recv("decision")
        assert second["committed"] == "s1" and second["changed"] is True
        sel = await peer.recv("selection")
        assert sel["slot"] == "s1" and sel["scores"] == {"s1": 0.42, "s2": 0.05, "s3": 0.05}
        assert sel["t_unix"] >= onset["t_unix"]
        await peer.send(type="stim_stop", trial_id=7, reason="selected")
        # The confirm screen: Say it / Back; only those two get bars.
        await peer.send(type="display", text="Yes, please.",
                        labels={"s1": "Say it", "s2": "", "s3": "Back"}, highlight=None)
        await peer.send(type="stim_start", trial_id=8, active_slots=["s1", "s3"])
        await next_of(peer, "decision")
        await next_of(peer, "selection")
        await peer.ws.close(4000, "done")

    await with_server(script, lambda url: make_code_bridge(url, display=display))
    lines = display.lines
    assert json.dumps({"labels": ["Yes, please.\n███░░░░░ +0.42 <", "No, thank you.\n░░░░░░░░ +0.05",
                                  "More options\n░░░░░░░░ +0.05"]}, ensure_ascii=False) in lines
    assert json.dumps({"text": "lead 0.37   no choice yet"}) in lines
    assert json.dumps({"text": "lead 0.37   choice: Yes, please."}) in lines
    assert json.dumps({"labels": ["Say it\n███░░░░░ +0.42 <", "", "Back\n░░░░░░░░ +0.05"]},
                      ensure_ascii=False) in lines
    # On the confirm screen the centre line is the picked sentence: no status line over it.
    after = lines[lines.index('{"text": "Yes, please."}'):]
    assert not any(line.startswith('{"text": "lead') for line in after)


async def test_no_scores_keeps_the_display_plain_but_still_sends_decisions():
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await code_handshake(peer)
        await peer.send(type="stim_start", trial_id=3, active_slots=["s1", "s2", "s3"])
        await next_of(peer, "decision")
        await next_of(peer, "selection")
        await peer.ws.close(4000, "done")

    await with_server(script, lambda url: make_code_bridge(url, None, display, "--no-scores"))
    assert not any("█" in line or "░" in line for line in display.lines)


async def test_trials_wait_for_a_template_and_baselines_are_refused():
    async def script(peer: Peer) -> None:
        await code_handshake(peer, train=False)
        await peer.send(type="stim_start", trial_id=1, active_slots=["s1", "s2", "s3"])
        assert await peer.recv("no_selection") == {"type": "no_selection", "trial_id": 1,
                                                   "reason": "not_ready"}
        await peer.send(type="baseline", baseline_id=4, seconds=0.1)
        assert await peer.recv("baseline_done") == {"type": "baseline_done", "baseline_id": 4,
                                                    "ok": False, "message": "code mode: send train"}
        await peer.send(type="train", train_id=2, slot="s9", cycles=30)
        assert (await peer.recv("trained"))["message"] == "unknown_slot"
        await peer.ws.close(4000, "done")

    await with_server(script, make_code_bridge)


async def test_a_pause_interrupts_training_and_a_later_train_reuses_the_template():
    engine = StubCodeEngine(hold_train=True)

    async def script(peer: Peer) -> None:
        await code_handshake(peer, train=False)
        await peer.send(type="train", train_id=1, slot="s1", label="left", cycles=30, retrain=True)
        await peer.recv("train_progress")
        await peer.send(type="pause", reason="stop_button")
        assert await next_of(peer, "trained") == {"type": "trained", "train_id": 1, "ok": False,
                                              "message": "interrupted"}
        # It trains for real the next time, then a train without retrain answers at once.
        engine.hold_train = False
        await peer.send(type="train", train_id=2, slot="s1", label="left", cycles=30, retrain=True)
        assert (await next_of(peer, "trained"))["ok"] is True
        await peer.send(type="pause", reason="stop_button")
        await peer.send(type="train", train_id=3, slot="s1", label="left", cycles=30)
        again = await next_of(peer, "trained")
        assert again["train_id"] == 3 and again["ok"] is True
        await peer.send(type="stim_start", trial_id=9, active_slots=["s1", "s3"])
        await next_of(peer, "selection")
        await peer.ws.close(4000, "done")

    await with_server(script, lambda url: make_code_bridge(url, engine))
    trains = [c for c in engine.calls if c[0] == "train"]
    assert [c[4] for c in trains] == [True, True, False]


async def test_cycle_markers_go_to_the_engine():
    engine = StubCodeEngine()
    made: list[vb.Bridge] = []

    def factory(url: str) -> vb.Bridge:
        made.append(make_code_bridge(url, engine))
        return made[0]

    async def script(peer: Peer) -> None:
        await code_handshake(peer, train=False)
        made[0]._on_display_event("marker", 1792809201.5, "cycle:12")
        made[0]._on_display_event("marker", 1792809201.6, "cycle:x")  # not a cycle number
        await peer.ws.close(4000, "done")

    await with_server(script, factory)
    assert [c for c in engine.calls if c[0] == "event"] == [("event", 1792809201.5, "cycle", 12)]


# ---------------------------------------------------------------------------
# The session page draws the code: cycles come from the server (draft section 9.7)
# ---------------------------------------------------------------------------


async def test_a_headless_bridge_takes_the_servers_cycles_for_the_training_and_trial_in_progress():
    engine = StubCodeEngine(commit_to=None, hold_train=True)

    async def script(peer: Peer) -> None:
        await code_handshake(peer, train=False)
        await peer.send(type="train", train_id=4, slot="s1", label="left", cycles=30, retrain=True)
        await peer.recv("train_progress")
        await peer.send(type="cycle", train_id=4, n=0, t_unix=1792809201.5)
        await peer.send(type="cycle", train_id=3, n=1, t_unix=1792809202.55)   # an old training
        await peer.send(type="cycle", trial_id=4, n=1, t_unix=1792809202.55)   # no such trial
        await peer.send(type="cycle", train_id=4, n=-1, t_unix=1792809202.55)  # invalid
        await peer.send(type="cycle", train_id=4, trial_id=4, n=2, t_unix=1.0)  # both ids
        await peer.send(type="cycle", train_id=4, n=1, t_unix=1792809202.55)
        await asyncio.sleep(0.05)
        engine.finish_train(4, 30)
        await next_of(peer, "trained")
        await peer.send(type="cycle", train_id=4, n=2, t_unix=1792809203.6)    # after it ended
        await peer.send(type="stim_start", trial_id=9, active_slots=["s1", "s3"])
        await peer.recv("stim_onset")
        await peer.send(type="cycle", trial_id=9, n=0, t_unix=1792809210.0)
        await peer.send(type="cycle", trial_id=8, n=0, t_unix=1792809210.0)    # a stale trial
        await peer.send(type="stim_stop", trial_id=9, reason="timeout")
        await peer.send(type="cycle", trial_id=9, n=1, t_unix=1792809211.05)   # after the stop
        await asyncio.sleep(0.1)
        await peer.ws.close(4000, "done")

    made: list[vb.Bridge] = []

    def factory(url: str) -> vb.Bridge:
        made.append(make_code_bridge(url, engine))
        return made[0]

    await with_server(script, factory)
    assert made[0].server_cycles
    assert [c for c in engine.calls if c[0] == "event"] == [
        ("event", 1792809201.5, "cycle", 0), ("event", 1792809202.55, "cycle", 1),
        ("event", 1792809210.0, "cycle", 0)]
    assert made[0].cycles_heard == 3


async def test_a_bridge_with_its_own_display_ignores_the_servers_cycles():
    engine = StubCodeEngine(hold_train=True)

    def factory(url: str) -> vb.Bridge:
        args = vb.build_parser().parse_args(["--url", url, "--fake", "--reconnect-s", "0.1",
                                             "--status-interval", "0.2"])
        cfg = load_stimulus(vb.DEFAULT_CODE_CONFIG)
        # A PsychoPy display stands in as a headless one, but the bridge was not started with
        # --no-stimulus: its display stamps the cycles itself.
        return vb.Bridge(args, cfg, engine, vb.HeadlessDisplay(cfg.refresh))

    async def script(peer: Peer) -> None:
        await code_handshake(peer, train=False)
        await peer.send(type="train", train_id=1, slot="s1", label="left", cycles=30, retrain=True)
        await peer.recv("train_progress")
        await peer.send(type="cycle", train_id=1, n=0, t_unix=1792809201.5)
        await asyncio.sleep(0.05)
        await peer.ws.close(4000, "done")

    await with_server(script, factory)
    assert not [c for c in engine.calls if c[0] == "event"]


class RelayBoard:
    """The fake board's EEG, but its cycle times reach the engine only through ``add_event``,
    as the session page's do through the server: CodeEngine runs as on the headset (fake
    False). ``look(target)`` moves the fake gaze and restarts the code on the board's thread at
    its next read, as a page starting a new flicker would."""

    def __init__(self, board) -> None:
        self.board = board
        self.fs = board.fs
        self.engine = None
        self.relayed = 0
        self._look: int | None = None

    def look(self, target: int) -> None:
        self._look = target

    def advance(self, seconds: float) -> None:
        self.board.advance(seconds)

    def read(self):
        if self._look is not None:
            self.board.set_target(self._look)
            self.board.start_flicker()
            self._look = None
        X, t = self.board.read()
        for ev in self.board.read_events():
            if ev[1] == "cycle":
                self.engine.add_event(ev[0], "cycle", ev[2])
                self.relayed += 1
        return X, t


async def test_code_engine_trains_and_decodes_from_relayed_cycles():
    args = vb.resolve(vb.build_parser().parse_args(["--fake", "--fast", "--seed", "4"]))
    cfg, board, decoder = vb.build_code(args)
    relay = RelayBoard(board)
    engine = vb.CodeEngine(relay, decoder, fake=False, fast=True, refresh=cfg.refresh)
    relay.engine = engine
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    engine.attach(loop, lambda *a: events.put_nowait(("commit", *a)))
    engine.attach_code(lambda *a: events.put_nowait(("progress", *a)),
                       lambda *a: events.put_nowait(("trained", *a)),
                       lambda *a: events.put_nowait(("decision", *a)))
    relay.look(0)
    engine.start_train(1, 0, 10, True, board.now)
    engine.start()
    try:
        while (ev := await asyncio.wait_for(events.get(), 30))[0] != "trained":
            assert ev[0] == "progress"
        assert ev[1:3] == (1, True) and ev[3] >= 0.5
        for trial_id, looked, active in ((2, 2, [0, 1, 2]), (3, 0, [0, 2])):
            relay.look(looked)
            engine.start_trial(trial_id, active, board.now)
            while (ev := await asyncio.wait_for(events.get(), 30))[0] != "commit":
                assert ev[0] == "decision" and ev[1] == trial_id
            assert ev[1:3] == (trial_id, looked)
        assert relay.relayed > 10
    finally:
        engine.close()
