"""tools/fake_decoder.py and tools/contract_check.py in code mode (c-VEP, the default config;
INTERFACE-v2-draft.md section 9): the code hello, train / train_progress / trained, decisions
before each selection, and the contract check of a code bridge, which the fake passes and a
misbehaving one fails."""

from __future__ import annotations

import asyncio
import io
import json

from websockets.asyncio.client import connect

from app.config import load_config
from app.schemas import (
    PROTOCOL,
    CodeParams,
    Cycle,
    Decision,
    DecoderHello,
    DecoderStatus,
    Pause,
    Selection,
    SlotSpec,
    StimOnset,
    Train,
    Trained,
    TrainProgress,
    to_json,
)
import pytest

from tests.test_fake_decoder import WAIT, StubServer, server  # noqa: F401 (the stub fixture)
from tools.contract_check import Report, build_parser, serve_once
from tools.fake_decoder import FakeDecoder, parse_args, parse_lags, slots_text

CFG = load_config()
ACTIVE = ["s1", "s2", "s3"]


@pytest.fixture
async def start_decoder(server: StubServer):  # noqa: F811
    """A FakeDecoder on the default (code) config against the stub; cancelled at teardown."""
    tasks: list[asyncio.Task] = []

    async def start(*argv: str) -> tuple[FakeDecoder, asyncio.Task]:
        args = parse_args(["--url", server.url, "--seed", "1", "--reconnect-s", "0.05", *argv])
        decoder = FakeDecoder(args, stdin=None)
        task = asyncio.create_task(decoder.run())
        tasks.append(task)
        hello = await asyncio.wait_for(server.hellos.get(), WAIT)
        assert hello.paradigm == "code"
        return decoder, task

    yield start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def next_of(stub: StubServer, kind: type, timeout: float = WAIT):
    """The next received message of ``kind`` (decisions and progress in between are skipped)."""
    while True:
        msg = await stub.next(timeout)
        if isinstance(msg, kind):
            return msg


def test_code_arguments_follow_the_default_config():
    args = parse_args([])
    assert args.paradigm == "code" and args.latency == 2.6
    assert [(s.slot, s.lag_frames) for s in args.slots] == [("s1", 0), ("s2", 21), ("s3", 42)]
    assert (args.code_length, args.cycle_frames) == (63, 63)
    assert slots_text(CFG) == "s1=0,s2=21,s3=42"
    assert [s.lag_frames for s in parse_lags("s1=0,s2=5")] == [0, 5]
    hello = FakeDecoder(args, stdin=None).hello()
    assert hello.paradigm == "code" and hello.score_kind == "correlation"
    assert hello.code == CodeParams(length=63, cycle_frames=63, train_cycles=30)


async def test_train_progress_then_trained_and_a_kept_template(server: StubServer, start_decoder):
    await start_decoder("--train-s", "0.15")
    await server.send(Train(train_id=1, slot="s1", label="left", cycles=3, retrain=True))
    progress = [await server.next() for _ in range(3)]
    assert progress == [TrainProgress(train_id=1, cycle=k, of=3) for k in (1, 2, 3)]
    done = await server.next()
    assert isinstance(done, Trained) and done.ok and done.train_id == 1 and done.cycles == 3
    assert 0.8 <= done.accuracy <= 0.97 and done.chance == round(1 / 3, 4)
    # Without retrain the template is kept: trained comes at once, with the same accuracy.
    await server.send(Train(train_id=2, slot="s1", cycles=3))
    again = await server.next(0.5)
    assert isinstance(again, Trained) and again.accuracy == done.accuracy and again.train_id == 2


async def test_a_pause_interrupts_the_training(server: StubServer, start_decoder):
    await start_decoder("--train-s", "5")
    await server.send(Train(train_id=4, slot="s1", cycles=30, retrain=True))
    await asyncio.sleep(0.05)
    await server.send(Pause(reason="stop_button"))
    done = await next_of(server, Trained)
    assert done == Trained(train_id=4, ok=False, message="interrupted")


async def test_decisions_come_before_the_selection_and_commit_its_slot(server: StubServer,
                                                                     start_decoder):
    await start_decoder("--latency", "2.2")  # two cycles: two decisions, then the committing one
    await server.start_trial(5, ACTIVE)
    decisions = []
    while True:
        msg = await server.next(5)
        if isinstance(msg, Selection):
            break
        assert isinstance(msg, Decision) and msg.trial_id == 5
        decisions.append(msg)
    assert server.onsets and server.onsets[0].trial_id == 5
    assert len(decisions) == 3
    assert all(d.raw == msg.slot and d.committed is None for d in decisions[:-1])
    last = decisions[-1]
    assert last.committed == msg.slot and last.changed and last.raw == msg.slot
    assert set(last.scores) == set(ACTIVE) and max(last.scores, key=last.scores.get) == msg.slot
    assert msg.scores is not None and msg.score == msg.scores[msg.slot]


async def test_a_stopped_trial_gets_no_more_decisions(server: StubServer, start_decoder):
    await start_decoder("--latency", "3.2")
    await server.start_trial(6, ACTIVE)
    first = await server.next(2)
    assert isinstance(first, Decision)
    await server.stop_trial(6)
    assert await server.nothing_within(2.5)


# ---------------------------------------------------------------------------
# contract_check against code bridges
# ---------------------------------------------------------------------------

CHECK_CFG = CFG.model_copy(update={
    "stim": CFG.stim.model_copy(update={"cue_s": 0.02, "rest_s": 0.02, "max_trial_s": 2.0}),
})


async def run_code_check(bridge, *extra: str) -> tuple[Report, str]:
    out = io.StringIO()
    report = Report(out=out)
    args = build_parser(CHECK_CFG).parse_args([
        "--host", "127.0.0.1", "--port", "0", "--trials", "2", "--train-cycles", "4",
        "--train-timeout", "3", "--hello-timeout", "3", "--quiet-s", "0.3", "--wait", "10", *extra])
    bound: asyncio.Queue[int] = asyncio.Queue()
    check = asyncio.create_task(serve_once(CHECK_CFG, args, report, bound.put_nowait))
    port = await asyncio.wait_for(bound.get(), 5)
    client = asyncio.create_task(bridge(f"ws://127.0.0.1:{port}/ws/decoder"))
    await asyncio.wait_for(check, 30)
    client.cancel()
    await asyncio.gather(client, return_exceptions=True)
    return report, out.getvalue()


def fake(*argv: str):
    async def run(url: str) -> None:
        args = parse_args(["--url", url, "--latency", "1.2", "--seed", "1", "--no-reconnect",
                           "--status-interval", "0.2", "--train-s", "0.2", *argv])
        await FakeDecoder(args, stdin=None).run()

    return run


async def test_the_fake_code_decoder_passes():
    report, out = await run_code_check(fake())
    assert report.ok, out
    assert "PASS  hello matches config/default.yaml" in out
    assert "PASS  train 1 trained" in out and "PASS  train 2 trained" in out
    assert "PASS  train 1: train_progress while recording" in out
    assert "INFO  trial 2: confirm trial: only s1, s3 flicker" in out
    for trial in (1, 2):
        assert f"PASS  trial {trial}: a decision before the selection (code mode)" in out
        assert f"PASS  trial {trial}: decision scores the active slots" in out
    assert "PASS  nothing answers the paused trial" in out


async def test_a_freq_hello_is_checked_against_the_fvep_config():
    async def run(url: str) -> None:
        args = parse_args(["--url", url, "--latency", "0.05", "--seed", "1", "--no-reconnect",
                           "--status-interval", "0.2", "--paradigm", "freq",
                           "--slots", "s1=13,s2=15,s3=17,s4=11"])
        await FakeDecoder(args, stdin=None).run()

    out = io.StringIO()
    report = Report(out=out)
    args = build_parser(CHECK_CFG).parse_args([
        "--host", "127.0.0.1", "--port", "0", "--trials", "2", "--baseline-s", "0.1",
        "--baseline-timeout", "2", "--hello-timeout", "3", "--quiet-s", "0.3", "--wait", "10"])
    bound: asyncio.Queue[int] = asyncio.Queue()
    check = asyncio.create_task(serve_once(CHECK_CFG, args, report, bound.put_nowait))
    port = await asyncio.wait_for(bound.get(), 5)
    client = asyncio.create_task(run(f"ws://127.0.0.1:{port}/ws/decoder"))
    await asyncio.wait_for(check, 30)
    client.cancel()
    await asyncio.gather(client, return_exceptions=True)
    text = out.getvalue()
    assert "INFO  a freq hello: checking against config/fvep_page.yaml" in text
    assert "PASS  hello matches config/fvep_page.yaml" in text
    assert "PASS  baseline 1 recorded" in text


CODE_HELLO = DecoderHello(protocol=PROTOCOL, name="bad-bridge", paradigm="code", refresh_hz=60.0,
                          slots=[SlotSpec(slot=s.slot, lag_frames=s.lag_frames) for s in CFG.stim.slots],
                          code=CodeParams(length=63, cycle_frames=63))
STATUS = DecoderStatus(ok=True, board_ok=True, display_ok=True)


def scripted(on_message):
    """A bridge that sends the code hello and status, then lets ``on_message`` answer."""
    async def run(url: str) -> None:
        async with connect(url) as ws:
            await ws.send(to_json(CODE_HELLO))

            async def status() -> None:
                while True:
                    await ws.send(to_json(STATUS))
                    await asyncio.sleep(0.2)

            beat = asyncio.create_task(status())
            try:
                async for raw in ws:
                    await on_message(ws, json.loads(raw))
            finally:
                beat.cancel()

    return run


async def test_a_bridge_that_never_trains_fails():
    async def on_message(ws, msg) -> None:
        pass

    report, out = await run_code_check(scripted(on_message))
    assert not report.ok
    assert "FAIL  trained for train 1: none within 3 s" in out


async def test_a_selection_without_decisions_and_a_stray_decision_fail():
    async def on_message(ws, msg) -> None:
        if msg["type"] == "train":
            await ws.send(to_json(Trained(train_id=msg["train_id"], ok=True, accuracy=0.9,
                                          chance=0.333, cycles=msg["cycles"])))
        elif msg["type"] == "stim_start":
            trial, active = msg["trial_id"], msg["active_slots"]
            # A decision for another trial, then a decision before the onset, then the onset.
            await ws.send(to_json(Decision(trial_id=trial + 50, scores={s: 0.1 for s in active})))
            await ws.send(to_json(Decision(trial_id=trial, scores={s: 0.1 for s in active})))
            await ws.send(to_json(StimOnset(trial_id=trial, t_unix=1e10)))
            if trial == 1:  # a decision naming a slot that is not active, then a bare selection
                await ws.send(to_json(Decision(trial_id=trial, scores={"s1": 0.1}, raw="s9")))
            await ws.send(to_json(Selection(trial_id=trial, slot=active[0], t_unix=1e10 + 1)))

    report, out = await run_code_check(scripted(on_message))
    assert not report.ok
    assert "FAIL  decision only for the current trial: got trial 51 while trial 1 runs" in out
    assert "FAIL  trial 1: decision after stim_onset: it came first" in out
    assert "FAIL  trial 1: decision scores the active slots" in out
    assert "FAIL  trial 2: a decision before the selection (code mode)" in out


# ---------------------------------------------------------------------------
# The session page's cycles (display.flicker: browser; draft section 9.7)
# ---------------------------------------------------------------------------


async def test_the_fake_decoder_counts_relayed_cycles(server: StubServer, start_decoder):  # noqa: F811
    decoder, _ = await start_decoder("--train-s", "5")
    await server.send(Train(train_id=2, slot="s1", cycles=30, retrain=True))
    for n in range(3):
        await server.send(Cycle(train_id=2, n=n, t_unix=1792809201.5 + 1.05 * n))
    await server.send(Cycle(trial_id=7, n=0, t_unix=1792809210.0))
    for _ in range(100):
        if sum(decoder.cycles.values()) == 4:
            break
        await asyncio.sleep(0.01)
    assert decoder.cycles == {("train", 2): 3, ("trial", 7): 1}


def recording_fake(made: list):
    async def run(url: str) -> None:
        args = parse_args(["--url", url, "--latency", "1.2", "--seed", "1", "--no-reconnect",
                           "--status-interval", "0.2", "--train-s", "0.2"])
        made.append(FakeDecoder(args, stdin=None))
        await made[0].run()

    return run


async def test_the_contract_check_plays_the_page_with_page_cycles():
    made: list[FakeDecoder] = []
    report, out = await run_code_check(recording_fake(made), "--page-cycles")
    assert report.ok, out
    assert "INFO  page cycles sent" in out
    counts = made[0].cycles
    for key in (("train", 1), ("train", 2), ("trial", 1), ("trial", 2)):
        assert counts.get(key, 0) >= 1, counts


async def test_without_page_cycles_the_check_sends_none():
    made: list[FakeDecoder] = []
    report, out = await run_code_check(recording_fake(made))
    assert report.ok, out
    assert made[0].cycles == {} and "page cycles" not in out
