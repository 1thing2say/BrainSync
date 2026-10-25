"""Write the flicker fixture that extension/test/flicker.test.mjs checks flicker.js against.

    .venv/bin/python extension/test/make_fixture.py            # rewrites flicker_fixture.json
    .venv/bin/python extension/test/make_fixture.py --check    # exit 1 if the file is stale

Every value comes from brainsync.stimulus.flicker.luminance, the function the PsychoPy
stimulus draws with: 15, 17 and 12 Hz, phases 0 and pi/2, frames 0..240 at 60 Hz, both
waveforms, modulation depths 1 and 0.5. tests/test_extension.py rebuilds it in memory and
compares with the file. Values are compared within TOLERANCE, not bit for bit: the maths
library rounds the last bit differently on macOS and Linux (up to about 1e-16), and the
fixture must pass on both.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

from brainsync.stimulus.flicker import WAVEFORMS, luminance

FIXTURE = Path(__file__).with_name("flicker_fixture.json")
FREQS = (15.0, 17.0, 12.0)
PHASES = (0.0, 0.5 * math.pi)
DEPTHS = (1.0, 0.5)   # full swing first, so the depth-1 cases keep their old place
REFRESH = 60.0
N_FRAMES = 241   # frames 0..240: 4 s at 60 Hz, 60 cycles at 15 Hz
TOLERANCE = 1e-12   # far below anything flicker.js could get wrong, far above libm rounding


def build() -> dict:
    """The fixture as a JSON-ready dict."""
    frames = np.arange(N_FRAMES)
    cases = []
    for depth in DEPTHS:
        for waveform in WAVEFORMS:
            for freq in FREQS:
                for phase in PHASES:
                    values = luminance(freq, phase, frames, REFRESH, waveform, depth)
                    cases.append({"waveform": waveform, "freq": freq, "phase": phase,
                                  "depth": depth, "refresh": REFRESH,
                                  "values": [float(v) for v in np.asarray(values)]})
    return {"source": "brainsync.stimulus.flicker.luminance", "frames": N_FRAMES,
            "cases": cases}


def dumps(data: dict) -> str:
    return json.dumps(data, indent=1) + "\n"


def same(a: object, b: object, tol: float = TOLERANCE) -> bool:
    """True when two JSON values match, numbers within ``tol`` (bools and strings exactly)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k], tol) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y, tol) for x, y in zip(a, b))
    return a == b


def matches(path: Path, data: dict) -> bool:
    """True when the fixture at ``path`` holds ``data`` (within TOLERANCE)."""
    try:
        return same(json.loads(path.read_text()), data)
    except (OSError, ValueError):
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python extension/test/make_fixture.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="compare with the file, write nothing")
    p.add_argument("--out", type=Path, default=FIXTURE, help=f"default {FIXTURE.name}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data = build()
    text = dumps(data)
    if args.check:
        if not matches(args.out, data):
            print(f"error: {args.out} is stale; rerun without --check", file=sys.stderr)
            return 1
        print(f"{args.out.name}: up to date")
        return 0
    args.out.write_text(text)
    print(f"wrote {args.out} ({len(data['cases'])} cases x {N_FRAMES} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
