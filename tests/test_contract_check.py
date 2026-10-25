"""tools/contract_check.py: our fake decoder passes it, and misbehaving bridges fail it."""

from __future__ import annotations

import asyncio
import io
import json
import time

import pytest
import yaml
from websockets.asyncio.client import connect

from app.config import load_config
from app.schemas import (
    PROTOCOL,
    BaselineDone,
    CodeSpec,
    DecoderHello,
    DecoderStatus,
    Selection,
    SlotSpec,
    StimOnset,
    TrainDone,
    to_json,
)
from tools.contract_check import CODE_CONFIG, Report, build_parser, serve_once
from tools.fake_decoder import FakeDecoder, default_code_bits, parse_args

BASE = load_config()
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
                           "--status-interval", "0.2", *argv])
        await FakeDecoder(args, stdin=None).run()

    return run


async def test_fake_decoder_passes():
    report, out = await run_check(fake())
    assert report.ok, out
    assert "PASS  hello matches config/default.yaml" in out
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


# ---------------------------------------------------------------------------
# Paradigm code (draft section 9): the check plays the phone too
# ---------------------------------------------------------------------------

PHONE = load_config(CODE_CONFIG)
FAST = {"cue_s": 0.02, "read_s_per_word": 0.0, "confirm_cue_s": 0.02, "rest_s": 0.02,
        "max_trial_s": 2.0}
CODE_CFG = PHONE.model_copy(update={"stim": PHONE.stim.model_copy(update=FAST)})
CODE_FAKE = ("--paradigm", "code", "--lags", "s1=0,s2=31", "--waveform", "square")


async def run_code_check(bridge, cfg=CODE_CFG, *extra: str) -> tuple[Report, str]:
    out = io.StringIO()
    report = Report(out=out)
    args = build_parser(cfg).parse_args([
        "--host", "127.0.0.1", "--port", "0", "--trials", "2", "--baseline-s", "0.1",
        "--baseline-timeout", "2", "--hello-timeout", "3", "--quiet-s", "0.3", "--wait", "30",
        "--train-cycles", "4", "--marker-latency", "0.05", "--code-onset-s", "0.05", *extra])
    bound: asyncio.Queue[int] = asyncio.Queue()
    check = asyncio.create_task(serve_once(cfg, args, report, bound.put_nowait))
    port = await asyncio.wait_for(bound.get(), 5)
    client = asyncio.create_task(bridge(f"ws://127.0.0.1:{port}/ws/decoder"))
    await asyncio.wait_for(check, 40)
    client.cancel()
    await asyncio.gather(client, return_exceptions=True)
    return report, out.getvalue()


async def test_fake_decoder_passes_in_code_mode():
    report, out = await run_code_check(fake(*CODE_FAKE))
    assert report.ok, out
    assert "PASS  hello matches the code config" in out
    assert "PASS  trial 1: untrained, no_selection not_trained" in out
    assert "PASS  train 1: template fitted: fake template" in out
    assert "PASS  status says trained after train_done" in out
    assert "PASS  trial 3: selection is an active slot" in out
    assert "PASS  trial 4: selection time after cycle 0" in out
    assert "PASS  nothing answers the paused trial" in out


async def test_a_code_hello_switches_to_the_code_config(tmp_path):
    # Against the default (freq) config the check loads --code-config for a code hello.
    raw = yaml.safe_load(CODE_CONFIG.read_text(encoding="utf-8"))
    raw["stim"].update(FAST)
    path = tmp_path / "phone_fast.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    report, out = await run_code_check(fake(*CODE_FAKE, "--trained"), CFG, "--code-config", str(path))
    assert report.ok, out
    assert f"INFO  the bridge speaks paradigm code: checking against {path}" in out
    assert "INFO  the bridge is trained already" in out


async def test_a_code_hello_with_the_wrong_lag_fails():
    report, out = await run_code_check(fake("--paradigm", "code", "--lags", "s1=0,s2=30",
                                            "--waveform", "square"))
    assert not report.ok
    assert any("s2 lags 30 frames, expected 31" in f for f in report.failures), out


def scripted_code_bridge(*, onset: bool = False, early: bool = False):
    """A trained code bridge that answers each trial at its first marker (or breaks a rule)."""
    hello = DecoderHello(protocol=PROTOCOL, name="test-code", refresh_hz=60.0, waveform="square",
                         paradigm="code", slots=[SlotSpec(slot="s1", lag_frames=0),
                                                 SlotSpec(slot="s2", lag_frames=31)],
                         code=CodeSpec(length=63, bits=default_code_bits()))
    status = DecoderStatus(ok=True, board_ok=True, display_ok=True, trained=True)

    async def run(url: str) -> None:
        async with connect(url) as ws:
            await ws.send(to_json(hello))
            await ws.send(to_json(status))
            answered: set[int] = set()
            async for raw in ws:
                msg = json.loads(raw)
                tid = msg.get("trial_id")
                if msg["type"] == "baseline":
                    await asyncio.sleep(msg["seconds"])
                    await ws.send(to_json(BaselineDone(baseline_id=msg["baseline_id"], ok=True)))
                elif msg["type"] == "train":
                    answered.add(tid)
                    await ws.send(to_json(TrainDone(train_id=msg["train_id"], ok=True, accuracy=1.0,
                                                    cycles=msg["cycles"])))
                elif msg["type"] == "stim_start" and (onset or early):
                    if onset:
                        await ws.send(to_json(StimOnset(trial_id=tid, t_unix=time.time())))
                    if early:
                        answered.add(tid)
                        await ws.send(to_json(Selection(trial_id=tid, slot="s1", t_unix=time.time())))
                elif msg["type"] == "stim_markers" and tid not in answered:
                    answered.add(tid)
                    await ws.send(to_json(Selection(trial_id=tid, slot="s2", t_unix=time.time())))
                await ws.send(to_json(status))

    return run


async def test_a_well_behaved_code_bridge_passes():
    report, out = await run_code_check(scripted_code_bridge())
    assert report.ok, out


@pytest.mark.parametrize("kwargs, fragment", [
    ({"onset": True}, "no stim_onset in paradigm code"),
    ({"early": True}, "selection only after the trial's first stim_markers (rule 8)"),
])
async def test_rule_breaking_code_bridges_fail(kwargs, fragment):
    report, out = await run_code_check(scripted_code_bridge(**kwargs))
    assert not report.ok
    assert any(fragment in f for f in report.failures), out
