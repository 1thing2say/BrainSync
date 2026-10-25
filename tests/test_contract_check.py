"""tools/contract_check.py: our fake decoder passes it, and misbehaving bridges fail it."""

from __future__ import annotations

import asyncio
import io
import json
import time

import pytest
from websockets.asyncio.client import connect

from app.config import FVEP_PAGE_CONFIG_PATH, load_config
from app.schemas import (
    PROTOCOL,
    BaselineDone,
    DecoderHello,
    DecoderStatus,
    Selection,
    SlotSpec,
    StimOnset,
    to_json,
)
from tools.contract_check import Report, build_parser, serve_once
from tools.fake_decoder import FakeDecoder, parse_args

BASE = load_config(FVEP_PAGE_CONFIG_PATH)  # f-VEP page mode; code mode: *_code.py
CFG = BASE.model_copy(update={
    "stim": BASE.stim.model_copy(update={"cue_s": 0.02, "rest_s": 0.02, "max_trial_s": 1.0}),
})
HELLO = DecoderHello(protocol=PROTOCOL, name="test-bridge", refresh_hz=60.0, waveform="sine",
                     slots=[SlotSpec(slot=s.slot, freq_hz=s.freq_hz) for s in BASE.stim.slots])
STATUS = DecoderStatus(ok=True, board_ok=True, display_ok=True)


def check_args(*extra: str):
    return build_parser(CFG).parse_args([
        "--host", "127.0.0.1", "--port", "0", "--trials", "2", "--baseline-s", "0.1",
        "--baseline-timeout", "2", "--hello-timeout", "3", "--quiet-s", "0.3", "--wait", "10",
        *extra,
    ])


async def run_check(bridge, *extra: str) -> tuple[Report, str]:
    """Run contract_check on a free port and ``bridge(url)`` against it; return the report."""
    out = io.StringIO()
    report = Report(out=out)
    bound: asyncio.Queue[int] = asyncio.Queue()
    check = asyncio.create_task(serve_once(CFG, check_args(*extra), report, bound.put_nowait))
    port = await asyncio.wait_for(bound.get(), 5)
    client = asyncio.create_task(bridge(f"ws://127.0.0.1:{port}/ws/decoder"))
    await asyncio.wait_for(check, 20)
    client.cancel()
    await asyncio.gather(client, return_exceptions=True)
    return report, out.getvalue()


def fake(*argv: str):
    async def run(url: str) -> None:
        args = parse_args(["--url", url, "--latency", "0.05", "--seed", "1", "--no-reconnect",
                           "--status-interval", "0.2", *argv], cfg=BASE)
        await FakeDecoder(args, stdin=None).run()

    return run


async def test_fake_decoder_passes():
    report, out = await run_check(fake())
    assert report.ok, out
    assert "PASS  hello matches config/fvep_page.yaml" in out
    assert "PASS  baseline 1 recorded" in out and "PASS  baseline 2 recorded" in out
    assert "INFO  trial 2: confirm trial: only s1, s4 flicker" in out
    assert "PASS  trial 2: selection is an active slot" in out
    assert "PASS  nothing answers the paused trial" in out


async def test_no_selection_and_scores_still_pass():
    report, out = await run_check(fake("--fail-rate", "1"))
    assert report.ok, out
    assert "PASS  trial 1: no_selection" in out
    report, out = await run_check(fake("--scores"))
    assert report.ok, out
    assert "PASS  score_kind in hello when selections carry scores" in out


async def test_promotion_refresh_fails_the_handshake():
    report, out = await run_check(fake("--refresh-hz", "120"))
    assert not report.ok
    assert any("hello matches config" in f and "ProMotion" in f for f in report.failures), out


async def test_no_bridge_within_wait_fails():
    out = io.StringIO()
    report = Report(out=out)
    await serve_once(CFG, check_args("--wait", "0.2"), report)
    assert report.failures == ["a bridge connects: none within 0.2 s"]


def scripted_bridge(*, onset: bool = True, wrong_baseline_id: bool = False, scores: bool = False,
                    v1_status: bool = False, late_answer: bool = False, slot: str = "s1"):
    """A hand-written bridge that breaks one rule at a time (or none)."""

    async def run(url: str) -> None:
        async with connect(url) as ws:
            await ws.send(to_json(HELLO))
            await ws.send(json.dumps({"type": "status", "ok": True}) if v1_status else to_json(STATUS))
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "baseline":
                    await asyncio.sleep(msg["seconds"])
                    bid = msg["baseline_id"] + (1 if wrong_baseline_id else 0)
                    await ws.send(to_json(BaselineDone(baseline_id=bid, ok=True)))
                elif msg["type"] == "stim_start":
                    tid = msg["trial_id"]
                    if onset:
                        await ws.send(to_json(StimOnset(trial_id=tid, t_unix=time.time())))
                    extra = ({"score": 0.5, "scores": {s: 0.5 if s == slot else 0.1 for s in msg["active_slots"]}}
                             if scores else {})
                    await ws.send(to_json(Selection(trial_id=tid, slot=slot, t_unix=time.time(), **extra)))
                elif msg["type"] == "stim_stop" and late_answer:
                    await ws.send(to_json(Selection(trial_id=msg["trial_id"], slot="s2", t_unix=time.time())))
                await ws.send(to_json(STATUS))

    return run


@pytest.mark.parametrize("kwargs, fragment", [
    ({"onset": False}, "stim_onset first: selection came before any stim_onset"),
    ({"wrong_baseline_id": True}, "baseline_done echoes the baseline_id"),
    ({"scores": True}, "score_kind in hello when selections carry scores"),
    ({"v1_status": True}, "message matches the v2 schema"),
    ({"late_answer": True}, "selection only for the current trial (rule 1)"),
    # s2 is an option slot: fine on a page trial, but not active on the confirm trial (s1 / s4).
    ({"slot": "s2"}, "trial 2: selection is an active slot: s2 of s1, s4"),
])
async def test_rule_breaking_bridges_fail(kwargs, fragment):
    report, out = await run_check(scripted_bridge(**kwargs))
    assert not report.ok
    assert any(fragment in f for f in report.failures), out


async def test_a_well_behaved_scripted_bridge_passes():
    report, out = await run_check(scripted_bridge())
    assert report.ok, out
