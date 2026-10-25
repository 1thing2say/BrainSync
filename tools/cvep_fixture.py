"""The c-VEP frame fixture for the iPhone app's tests (ios/BrainSyncKit, CVEPCodeTests).

The phone draws the code itself (INTERFACE-v2-draft.md section 9), and the bridge decodes against
``brainsync.vep.codes.lagged_codes``, so the app's ``CVEPCode`` must make exactly the same frames.
This writes, for each case, the server's ``stimulus`` message (app/schemas.py ``Stimulus``) and the
bright (1) / dark (0) bit of every slot on trial frames 0..139 from ``lagged_codes``; the Swift test
decodes the stimulus and compares every frame. Cases: cvep_2 (``m_sequence(63)``, s1 lag 0, s2 lag
31, one frame per bit) and a short code held two frames per bit with three slots, one of them
lagged by an odd number of frames (mid-bit).

    python tools/cvep_fixture.py           # write the fixture
    python tools/cvep_fixture.py --check   # exit 1 if the committed fixture is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # python tools/cvep_fixture.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas import Stimulus  # noqa: E402
from brainsync.vep.codes import lagged_codes, m_sequence  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "ios/BrainSyncKit/Tests/BrainSyncKitTests/Fixtures/cvep_stimulus_frames.json"
FRAMES = 140  # two full cvep_2 cycles and part of a third

# (name, code length, frames per bit, lag between neighbouring slots, number of slots, depth)
CASES = [
    ("cvep_2", 63, 1, 31, 2, 1.0),       # brainsync/stimulus/configs/cvep_2.toml
    ("short_fpb2", 15, 2, 11, 3, 0.8),   # 30-frame cycle; lags 0, 11, 22
]


def case(name: str, length: int, frames_per_bit: int, lag_frames: int, n_slots: int,
         depth: float) -> dict:
    seq = m_sequence(length)
    codes = lagged_codes(seq, n_slots, lag_frames, frames_per_bit)
    cycle_frames = codes.shape[1]
    slots = [f"s{k + 1}" for k in range(n_slots)]
    stimulus = Stimulus(refresh_hz=60.0, frames_per_bit=frames_per_bit,
                        bits="".join(str(int(b)) for b in seq),
                        lags={s: k * lag_frames for k, s in enumerate(slots)}, depth=depth)
    bright = {s: "".join(str(int(codes[k, n % cycle_frames])) for n in range(FRAMES))
              for k, s in enumerate(slots)}
    return {"name": name, "stimulus": stimulus.model_dump(mode="json"),
            "cycle_frames": int(cycle_frames), "frames": FRAMES, "bright": bright}


def build() -> dict:
    return {"cases": [case(*c) for c in CASES]}


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/cvep_fixture.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="exit 1 if the fixture is missing or stale")
    p.add_argument("--out", type=Path, default=FIXTURE, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    text = render()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != text:
            print(f"stale: {args.out} (run python tools/cvep_fixture.py)", file=sys.stderr)
            return 1
        print(f"up to date: {args.out}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
