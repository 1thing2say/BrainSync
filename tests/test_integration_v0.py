"""Cross-checks: the config files, schemas and fake decoder agree with each other and PLAN.md."""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve

import tools.fake_decoder as fake_decoder
from app.config import REPO_ROOT, load_config, load_intents
from app.schemas import (
    PROTOCOL,
    DecoderHello,
    DecoderStatus,
    Display,
    HelloAck,
    Option,
    Selection,
    StimOnset,
    StimStart,
    hello_problems,
    parse_decoder_to_server,
    to_json,
    validate_option,
)
from tools.fake_decoder import FakeDecoder, load_script, parse_args

WAIT = 2.0


def test_fake_decoder_hello_passes_the_server_check() -> None:
    cfg = load_config()
    hello = FakeDecoder(parse_args([]), stdin=None).hello()
    assert hello.protocol == PROTOCOL
    assert hello_problems(hello, cfg.stim.slots, cfg.stim.waveform,
                          cfg.display.expected_refresh_hz) == []
    assert [s.slot for s in hello.slots] == list(cfg.stim.roles)


def test_every_fallback_option_is_valid_with_configured_limits() -> None:
    menus = load_config().menus
    intents = load_intents()
    for name, options in intents.fallback_lists.items():
        assert len(options) == menus.option_count, name
        for opt in options:
            plain = Option.model_validate(opt.model_dump())
            problems = validate_option(
                plain, menus.label_max_chars, menus.utterance_max_words, menus.label_max_words
            )
            assert problems == [], f"{name}: {opt.label!r}: {problems}"
            assert opt.intent_id in intents.intent_ids


def test_demo_script_only_uses_configured_slots() -> None:
    slots = set(load_config().stim.roles)
    steps = load_script(REPO_ROOT / "tools" / "scripts" / "demo.yaml")
    assert {s.slot for s in steps if s.slot is not None} <= slots


def test_fake_decoder_defaults_match_plan_and_config() -> None:
    cfg = load_config()
    args = parse_args([])
    assert args.mode == "random"
    assert args.latency == 5.0  # PLAN.md V0 item 4: mimics the real decoder's commit time
    assert args.latency < cfg.stim.max_trial_s  # otherwise every trial times out
    assert args.url == f"ws://{cfg.server.host}:{cfg.server.port}/ws/decoder"


def test_fake_decoder_builds_messages_only_through_schemas() -> None:
    source = inspect.getsource(fake_decoder)
    assert not re.search(r"[\"']type[\"']\s*:", source), "hand-built message dict"
    assert "json.dumps" not in source


async def test_fake_decoder_against_a_v2_stub(tmp_path: Path) -> None:
    """A stub server acks the fake's hello; the fake reports the onset and answers with a config slot."""
    cfg = load_config()
    slots = list(cfg.stim.roles)
    received: asyncio.Queue = asyncio.Queue()
    ack = HelloAck(protocol=PROTOCOL, roles=cfg.stim.roles, labels={"s1": "SAY", "s2": "NEXT"})

    async def handler(ws: ServerConnection) -> None:
        async for raw in ws:
            msg = parse_decoder_to_server(raw)
            if isinstance(msg, DecoderHello):
                await ws.send(to_json(ack))
                await ws.send(to_json(Display(text="Yes, let's go outside", labels={"s1": "SAY", "s2": "NEXT"})))
                await ws.send(to_json(StimStart(trial_id=1, active_slots=slots)))
            elif not isinstance(msg, DecoderStatus):
                await received.put(msg)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        args = parse_args(["--url", f"ws://127.0.0.1:{port}/ws/decoder", "--latency", "0",
                           "--once", "--no-reconnect", "--seed", "1"])
        decoder = FakeDecoder(args, stdin=None)
        await asyncio.wait_for(decoder.run(), WAIT)
        onset = await asyncio.wait_for(received.get(), WAIT)
        answer = await asyncio.wait_for(received.get(), WAIT)

    assert decoder.ack == ack
    assert isinstance(onset, StimOnset) and onset.trial_id == 1
    assert isinstance(answer, Selection)
    assert answer.trial_id == 1 and answer.slot in slots
