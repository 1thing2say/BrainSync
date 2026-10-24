"""Write the message fixture that extension/brainmap/test/fixture.test.mjs feeds to map.js.

    .venv/bin/python extension/brainmap/test/make_fixture.py            # rewrites frames_fixture.json
    .venv/bin/python extension/brainmap/test/make_fixture.py --check    # exit 1 if the file is stale

The messages are the real output of brainsync.brainmap.stream: 14 s from a seeded FakeBoard
(10, 15 and 17 Hz tones on the posterior channels, 4 s each) replayed through ReplayBoard and
run() in virtual time, so the page is tested against what Python actually sends rather than
against a hand-written copy of the contract. From 10 s on Fp2 goes flat, C3 touches the rail
and P7 turns noisy, so the fixture also carries every bad-contact path the page greys out.
The baseline is 3 s so frames before and after "ready" both appear. tests/test_brainmap.py
rebuilds it in memory and compares with the file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from brainsync.board import ADC_FULL_SCALE_UV, FakeBoard
from brainsync.brainmap.stream import MapStream, ReplayBoard, run
from brainsync.synthetic import SESSION_T0

FIXTURE = Path(__file__).with_name("frames_fixture.json")
FREQS = (10.0, 15.0, 17.0)
SWITCH_S = 4.0
SECONDS = 14.0
BASELINE_S = 3.0
BAD_FROM_S = 10.0
KEEP_EVERY = 6      # every 6th frame (1.5 s apart) plus the frames where something changes
POLL_S = 0.05


def _session() -> tuple[np.ndarray, np.ndarray, float, tuple[str, ...]]:
    board = FakeBoard(freqs=FREQS, switch_s=SWITCH_S, seed=0, t0=SESSION_T0)
    board.advance(SECONDS)
    X, t = board.read()
    late = slice(int(BAD_FROM_S * board.fs), None)
    names = board.ch_names
    X[names.index("Fp2"), late] = 0.0                                    # electrode off
    X[names.index("C3"), late][::125] = ADC_FULL_SCALE_UV                # touches the rail
    rng = np.random.default_rng(1)
    X[names.index("P7"), late] += 300.0 * rng.standard_normal(X[:, late].shape[1])  # loose
    return X, t, board.fs, names


def build() -> dict:
    """The fixture as a JSON-ready dict: the hello, then every status and a subset of frames."""
    X, t, fs, names = _session()
    now = [0.0]
    replay = ReplayBoard((X, t, fs, names), clock=lambda: now[0])
    stream = MapStream(fs, names, baseline_s=BASELINE_S, source="replay")
    out: list[dict] = []

    def sleep(s: float) -> None:
        now[0] += s

    with replay:
        why = run(replay, stream, out.append, poll_s=POLL_S, sleep=sleep, clock=lambda: now[0])
    if why != "ended":
        raise RuntimeError(f"replay stopped with {why!r}, expected 'ended'")

    kept: list[dict] = [stream.hello()]
    prev_state, prev_quality = None, None
    for m in out:
        if m["type"] != "frame":
            kept.append(m)
            continue
        state, quality = m["baseline"]["state"], m["quality"]
        if m["seq"] % KEEP_EVERY == 0 or state != prev_state or quality != prev_quality:
            kept.append(m)
        prev_state, prev_quality = state, quality
    return {"source": "brainsync.brainmap.stream (FakeBoard via ReplayBoard and run)",
            "seconds": SECONDS, "baseline_s": BASELINE_S, "messages": kept}


def dumps(data: dict) -> str:
    # allow_nan=False: the page's JSON.parse would reject NaN, so it must never be written.
    head = {k: v for k, v in data.items() if k != "messages"}
    lines = [json.dumps(m, allow_nan=False, ensure_ascii=False) for m in data["messages"]]
    body = json.dumps(head, allow_nan=False)[:-1]
    return body + ', "messages": [\n' + ",\n".join(lines) + "\n]}\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python extension/brainmap/test/make_fixture.py",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="compare with the file, write nothing")
    p.add_argument("--out", type=Path, default=FIXTURE, help=f"default {FIXTURE.name}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data = build()
    text = dumps(data)
    if args.check:
        if not args.out.exists() or args.out.read_text(encoding="utf-8") != text:
            print(f"error: {args.out} is stale; rerun without --check", file=sys.stderr)
            return 1
        print(f"{args.out.name}: up to date")
        return 0
    args.out.write_text(text, encoding="utf-8")
    kinds = [m["type"] for m in data["messages"]]
    print(f"wrote {args.out} ({kinds.count('frame')} frames, {kinds.count('status')} statuses)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
