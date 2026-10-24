"""Tests for tools/fake_decoder.py against an in-process stub server playing the server role."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve

from app.schemas import (
    PROTOCOL,
    Baseline,
    BaselineDone,
    DecoderHello,
    DecoderStatus,
    HelloAck,
    Message,
    NoSelection,
    Pause,
    ProtocolError,
    Selection,
    StimOnset,
    StimStart,
    StimStop,
    parse_decoder_to_server,
    to_json,
)
from tools.fake_decoder import FakeDecoder, load_script, parse_args

SLOTS = ["s1", "s2", "s3", "s4"]  # active_slots the stub sends; the fake answers any
WAIT = 2.0  # generous upper bound for anything that should happen


class StubServer:
    """Plays the UI server: acks the bridge hello (or rejects it) and records what it sends.

    Answers (selection / no_selection) go to ``received``; onsets and baseline_done are kept apart.
    """

    def __init__(self) -> None:
        self.hellos: asyncio.Queue[DecoderHello] = asyncio.Queue()
        self.received: asyncio.Queue[Message] = asyncio.Queue()
        self.onsets: list[StimOnset] = []
        self.baselines: asyncio.Queue[BaselineDone] = asyncio.Queue()
        self.statuses: list[DecoderStatus] = []
        self.reject_with: str | None = None  # set to send error instead of hello_ack
        self.conn: ServerConnection | None = None
        self.url = ""

    async def handler(self, ws: ServerConnection) -> None:
        self.conn = ws
        async for raw in ws:
            msg = parse_decoder_to_server(raw)
            if isinstance(msg, DecoderHello):
                if self.reject_with:
                    await ws.send(to_json(ProtocolError(message=self.reject_with)))
                else:
                    await ws.send(to_json(HelloAck(
                        protocol=PROTOCOL, roles={"s1": "say", "s2": "next"},
                        labels={"s1": "SAY", "s2": "NEXT"},
                    )))
                await self.hellos.put(msg)
            elif isinstance(msg, DecoderStatus):
                self.statuses.append(msg)
            elif isinstance(msg, StimOnset):
                self.onsets.append(msg)
            elif isinstance(msg, BaselineDone):
                await self.baselines.put(msg)
            else:
                await self.received.put(msg)

    async def send(self, msg: Message | dict | str) -> None:
        assert self.conn is not None
        if isinstance(msg, Message):
            msg = to_json(msg)
        elif isinstance(msg, dict):
            msg = json.dumps(msg)
        await self.conn.send(msg)

    async def start_trial(self, trial_id: int, active: list[str] | None = None) -> None:
        await self.send(StimStart(trial_id=trial_id, active_slots=active or SLOTS))

    async def stop_trial(self, trial_id: int, reason: str = "timeout") -> None:
        await self.send(StimStop(trial_id=trial_id, reason=reason))

    async def next(self, timeout: float = WAIT) -> Message:
        return await asyncio.wait_for(self.received.get(), timeout)

    async def nothing_within(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.received.get(), seconds)
        except TimeoutError:
            return True
        return False


@pytest.fixture
async def server() -> AsyncIterator[StubServer]:
    stub = StubServer()
    async with serve(stub.handler, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        stub.url = f"ws://127.0.0.1:{port}/ws/decoder"
        yield stub


@pytest.fixture
async def start_decoder(server: StubServer) -> AsyncIterator:
    """Start a FakeDecoder against the stub and wait for its hello; cancels it at teardown."""
    tasks: list[asyncio.Task] = []

    async def start(*argv: str) -> tuple[FakeDecoder, asyncio.Task]:
        args = parse_args(["--url", server.url, "--seed", "1", "--reconnect-s", "0.05", *argv])
        decoder = FakeDecoder(args, stdin=None)  # keyboard tests feed decoder.keys directly
        task = asyncio.create_task(decoder.run())
        tasks.append(task)
        await asyncio.wait_for(server.hellos.get(), WAIT)
        return decoder, task

    yield start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def wait_until(predicate, timeout: float = WAIT) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


async def test_hello_exchange(server: StubServer) -> None:
    args = parse_args(["--url", server.url])
    decoder = FakeDecoder(args, stdin=None)
    task = asyncio.create_task(decoder.run())
    try:
        hello = await asyncio.wait_for(server.hellos.get(), WAIT)
        # A v2 hello from config/default.yaml: the server's check passes.
        assert hello.protocol == PROTOCOL
        assert hello.name == "fake-decoder"
        assert hello.refresh_hz == 60.0 and hello.waveform == "sine"
        assert [(s.slot, s.freq_hz) for s in hello.slots] == [("s1", 15.0), ("s2", 17.0)]
        await wait_until(lambda: decoder.acked)
        assert decoder.ack.roles == {"s1": "say", "s2": "next"}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_status_is_sent(server: StubServer, start_decoder) -> None:
    await start_decoder("--status-interval", "0.05")
    await wait_until(lambda: len(server.statuses) >= 2)
    status = server.statuses[0]
    assert status.ok and status.board_ok and status.display_ok
    assert server.statuses[0].message == "fake decoder"


async def test_random_mode_answers_with_active_slot(server: StubServer, start_decoder) -> None:
    await start_decoder("--random", "--latency", "0.02")
    for trial_id, active in [(7, SLOTS), (8, ["s2", "s3"]), (9, ["s4"])]:
        await server.start_trial(trial_id, active)
        msg = await server.next()
        assert isinstance(msg, Selection)
        assert msg.trial_id == trial_id
        assert msg.slot in active
        assert msg.score is None and msg.scores is None


async def test_scores_attached(server: StubServer, start_decoder) -> None:
    await start_decoder("--latency", "0.02", "--scores")
    await server.start_trial(1, ["s1", "s2"])
    msg = await server.next()
    assert isinstance(msg, Selection)
    assert set(msg.scores) == {"s1", "s2"}
    assert msg.score == msg.scores[msg.slot] == max(msg.scores.values())


async def test_fail_rate_one_gives_no_selection(server: StubServer, start_decoder) -> None:
    await start_decoder("--latency", "0.02", "--fail-rate", "1.0")
    await server.start_trial(3)
    msg = await server.next()
    assert isinstance(msg, NoSelection)
    assert msg.trial_id == 3


async def test_stim_stop_cancels_answer(server: StubServer, start_decoder) -> None:
    await start_decoder("--latency", "0.3")
    await server.start_trial(1)
    await server.stop_trial(1, "context_changed")
    assert await server.nothing_within(0.6)
    # Still alive: the next trial is answered.
    await server.start_trial(2)
    msg = await server.next()
    assert msg.trial_id == 2


async def test_stale_trial_never_answered(server: StubServer, start_decoder) -> None:
    await start_decoder("--latency", "0.2")
    await server.start_trial(1)
    await server.start_trial(2)
    msg = await server.next()
    assert msg.trial_id == 2
    assert await server.nothing_within(0.4)
    # A stim_stop for the old trial does not cancel the current one.
    await server.start_trial(3)
    await server.stop_trial(2)
    msg = await server.next()
    assert msg.trial_id == 3


async def test_script_mode_follows_steps_and_loops(
    server: StubServer, start_decoder, tmp_path: Path
) -> None:
    script = tmp_path / "s.yaml"
    script.write_text(
        "- slot: s2\n"
        "- no_selection: low_signal\n"
        "- timeout: true\n"
        "  latency: 0.01\n"
        "- slot: s1\n"
        "  latency: 0.01\n",
        encoding="utf-8",
    )
    await start_decoder("--script", str(script), "--latency", "0.02")

    await server.start_trial(1)
    msg = await server.next()
    assert isinstance(msg, Selection) and (msg.trial_id, msg.slot) == (1, "s2")

    await server.start_trial(2)
    msg = await server.next()
    assert isinstance(msg, NoSelection) and (msg.trial_id, msg.reason) == (2, "low_signal")

    await server.start_trial(3)
    assert await server.nothing_within(0.2)
    await server.stop_trial(3)

    await server.start_trial(4)
    msg = await server.next()
    assert isinstance(msg, Selection) and (msg.trial_id, msg.slot) == (4, "s1")

    await server.start_trial(5)  # loops back to the first step
    msg = await server.next()
    assert isinstance(msg, Selection) and (msg.trial_id, msg.slot) == (5, "s2")


async def test_script_slot_not_active_sends_no_selection(
    server: StubServer, start_decoder, tmp_path: Path
) -> None:
    script = tmp_path / "s.yaml"
    script.write_text("- slot: s4\n", encoding="utf-8")
    await start_decoder("--script", str(script), "--latency", "0.01")
    await server.start_trial(1, ["s1", "s2"])
    msg = await server.next()
    assert isinstance(msg, NoSelection) and msg.reason == "slot_not_active"


@pytest.mark.parametrize(
    "content",
    ["[]\n", "- {}\n", "- slot: s1\n  timeout: true\n", "- slot: x9\n", "- bogus: 1\n"],
)
def test_load_script_rejects_bad_steps(tmp_path: Path, content: str) -> None:
    script = tmp_path / "bad.yaml"
    script.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_script(script)


def test_load_script_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read script"):
        load_script(tmp_path / "does_not_exist.yaml")


def test_load_script_rejects_malformed_yaml(tmp_path: Path) -> None:
    script = tmp_path / "broken.yaml"
    script.write_text("- slot: [s1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read script"):
        load_script(script)


def test_demo_script_is_valid() -> None:
    steps = load_script(Path(__file__).resolve().parents[1] / "tools" / "scripts" / "demo.yaml")
    assert steps


async def test_unknown_and_invalid_messages_ignored(server: StubServer, start_decoder) -> None:
    await start_decoder("--latency", "0.02")
    await server.send({"type": "mystery", "x": 1})
    await server.send({"type": "display", "text": "hi", "labels": ["a", "b"]})
    await server.send({"type": "baseline", "seconds": 2.0})
    await server.send({"type": "stim_start", "trial_id": "not-a-number"})
    await server.send({"no_type": True})
    await server.send("this is not json")
    await server.send("[1, 2]")
    await server.start_trial(5, ["s1", "s2"])
    msg = await server.next()
    assert isinstance(msg, Selection) and msg.trial_id == 5


async def test_keyboard_mode(server: StubServer, start_decoder) -> None:
    decoder, _ = await start_decoder("--keyboard")
    decoder.keys.put_nowait("2\n")  # typed between trials: dropped
    await server.start_trial(1, ["s3", "s4"])
    await asyncio.sleep(0.05)
    assert await server.nothing_within(0.1)
    decoder.keys.put_nowait("9\n")  # not a choice: ignored
    decoder.keys.put_nowait("2\n")
    msg = await server.next()
    assert isinstance(msg, Selection) and (msg.trial_id, msg.slot) == (1, "s4")

    await server.start_trial(2, ["s3", "s4"])
    await asyncio.sleep(0.05)
    decoder.keys.put_nowait("n\n")
    msg = await server.next()
    assert isinstance(msg, NoSelection) and msg.trial_id == 2


async def test_once_exits_after_first_answer(server: StubServer, start_decoder) -> None:
    decoder, task = await start_decoder("--once", "--latency", "0.02")
    await server.start_trial(1)
    await server.next()
    await asyncio.wait_for(task, WAIT)
    assert decoder.answered == 1 and decoder.finished


async def test_reconnects_after_drop(server: StubServer, start_decoder) -> None:
    await start_decoder("--latency", "0.02")
    await server.conn.close()
    hello = await asyncio.wait_for(server.hellos.get(), WAIT)  # hello again after reconnect
    assert hello.name == "fake-decoder"
    await server.start_trial(11)
    msg = await server.next()
    assert msg.trial_id == 11


async def test_no_reconnect_exits_on_drop(server: StubServer, start_decoder) -> None:
    _, task = await start_decoder("--no-reconnect")
    await server.conn.close()
    await asyncio.wait_for(task, WAIT)


# ---------------------------------------------------------------------------
# Protocol 2 behaviour (INTERFACE-v2-draft.md)
# ---------------------------------------------------------------------------


async def test_hello_overrides_for_mismatch_tests(server: StubServer, start_decoder) -> None:
    decoder, _ = await start_decoder("--refresh-hz", "120", "--slots", "s1=15,s2=12",
                                     "--waveform", "square")
    hello = decoder.hello()
    assert (hello.refresh_hz, hello.waveform) == (120.0, "square")
    assert [(s.slot, s.freq_hz) for s in hello.slots] == [("s1", 15.0), ("s2", 12.0)]


async def test_onset_comes_before_the_answer(server: StubServer, start_decoder) -> None:
    await start_decoder("--latency", "0.05")
    await server.start_trial(3, ["s1", "s2"])
    msg = await server.next()
    assert isinstance(msg, Selection) and msg.trial_id == 3
    assert [o.trial_id for o in server.onsets] == [3]
    assert server.onsets[0].t_unix <= msg.t_unix


async def test_baseline_done_echoes_the_id(server: StubServer, start_decoder) -> None:
    await start_decoder("--baseline-s", "0.02")
    await server.send(Baseline(baseline_id=7, seconds=10.0))
    done = await asyncio.wait_for(server.baselines.get(), WAIT)
    assert (done.baseline_id, done.ok) == (7, True)


async def test_baseline_waits_for_the_requested_seconds(server: StubServer, start_decoder) -> None:
    await start_decoder()
    await server.send(Baseline(baseline_id=1, seconds=0.3))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.baselines.get(), 0.15)
    done = await asyncio.wait_for(server.baselines.get(), WAIT)
    assert done.baseline_id == 1


async def test_no_flicker_before_hello_ack(server: StubServer, start_decoder) -> None:
    server.reject_with = "display refresh is 120 Hz"
    decoder, _ = await start_decoder("--latency", "0.02")
    await asyncio.sleep(0.05)
    assert not decoder.acked
    await server.start_trial(1, ["s1", "s2"])
    assert await server.nothing_within(0.2)
    assert server.onsets == []


async def test_pause_cancels_the_pending_answer_and_the_baseline(server: StubServer, start_decoder) -> None:
    await start_decoder("--latency", "0.2", "--baseline-s", "0.2")
    await server.start_trial(1, ["s1", "s2"])
    await server.send(Pause(reason="stop_button"))
    assert await server.nothing_within(0.4)
    await server.send(Baseline(baseline_id=2, seconds=1.0))
    await server.send(Pause(reason="stop_button"))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.baselines.get(), 0.4)


async def test_scores_set_score_kind(server: StubServer, start_decoder) -> None:
    decoder, _ = await start_decoder("--scores")
    assert decoder.hello().score_kind is not None
