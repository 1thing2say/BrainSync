"""The c-VEP fixture for the iPhone app's tests (ios/BrainSyncKit, CodeVep and FrameClock).

Every expected value comes from the reference implementations, so the Swift port is checked
against the real code rather than against a copy of it:

- the bits and the lit / dark frames 0..189 of lags 0, 21, 42 from ``brainsync.vep.live``
  (``BITS``, ``frame_bit``), checked equal to ``clients/js/cvep.js`` (``bit``);
- the confidence text from ``clients/js/cvep.js`` (``bar``, ``labelLine``, ``statusLine``) and
  ``ConfidenceState`` in ``clients/js/brainsync-client.js`` (decision, training and trained
  lines), run with ``node``. The Python helpers (``brainsync.server.client.decision_lines``,
  ``trained_text``) must agree on every case not marked ``tie``: on an exact tie (0.125, a bar
  of 0.5 cells) Python rounds to even and JavaScript up, and the phone follows JavaScript;
- the frame counting of ``FlickerState`` (frame index, onset, cycle messages, dropped count)
  on timestamp sequences with jitter and dropped frames, for ``FrameClock``.

Non-finite numbers are written as null.

    python tools/ios_cvep_fixture.py           # write the fixture
    python tools/ios_cvep_fixture.py --check   # exit 1 if the committed fixture is stale
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # python tools/ios_cvep_fixture.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainsync.server.client import decision_lines, trained_text  # noqa: E402
from brainsync.vep import live  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "ios/BrainSyncKit/Tests/BrainSyncKitTests/Fixtures/cvep_frames.json"
JS_DIR = REPO_ROOT / "clients/js"
FRAMES = 190
LAGS = (0, 21, 42)

BARS = [-0.5, 0.0, 0.06, 0.0625, 0.1875, 0.3125, 0.42, 0.5, 0.99, 1.0, 1.7, None]
BAR_TIES = {0.0625, 0.1875, 0.3125}

LABEL_LINES = [  # label, score, best, width
    ("YES", 0.42, True, 4), ("NO", 0.11, False, 4), ("HELP", -0.03, False, 4),
    ("UP", 0.125, False, None), ("DOWN", -0.125, True, 6), ("LIKE", -0.001, False, 4),
    ("NEXT", None, False, 4), ("A", 0.005, True, 1), ("SHUTTER", 0.995, False, 7),
    ("GALLERY", 1.5, True, 3),
]

STATUS_LINES = [  # lead, committed, changed
    (0.31, "YES", True), (0.31, "YES", False), (None, None, False), (0.125, "NO", False),
    (0.0, "HELP", True), (1.234, None, True), (0.005, "LIKE", False),
]

DECISIONS = [  # slots (label = slot), scores, raw, committed, lead, changed; tie
    (["yes", "no", "help"], {"yes": 0.42, "no": 0.11, "help": -0.03}, "yes", "yes", 0.31, True, False),
    (["yes", "no", "help"], {"yes": 0.42, "no": 0.11, "help": -0.03}, "yes", "yes", 0.31, False, False),
    (["up", "like", "down"], {"up": 0.05, "like": 0.2, "down": 0.18}, "like", None, 0.02, False, False),
    (["shutter", "flip", "gallery"], {"shutter": 0.7, "flip": -0.2, "gallery": 0.1}, "shutter",
     "gallery", 0.6, True, False),
    (["t0", "t1", "t2"], {"t0": 0.0625, "t1": 0.125, "t2": 0.0}, "t1", "t1", 0.0625, False, True),
]

TRAINING = [  # cue slot, its label, cycles in the phase, progress (cycle, of) or None
    ("yes", "yes", 30, (12, 30)), ("yes", "yes", 30, None), ("t0", "camera", 30, (30, 30)),
    ("up", "up", 6, (1, 6)),
]

TRAINED = [  # accuracy, chance, tie
    (0.83, 1 / 3, False), (0.125, 0.333, True), (None, 0.333, False), (1.0, 0.5, False),
    (0.835, 1 / 3, False), (0.0, None, False),
]


def clock_sequences() -> list[dict]:
    """Display-link timestamps (ms) at 60 Hz with jitter and dropped frames."""
    p = 1000.0 / 60.0
    base = 5000.0
    jitter = [0.0, 0.4, -0.3, 0.2, -0.5, 0.1]

    def at(k: int) -> float:
        return round(base + k * p + jitter[k % len(jitter)], 6)

    steady = [at(k) for k in range(200)]
    # Frame 63 (cycle 1's start) dropped, 64 drawn: reported at its due time. Frames 126-128
    # dropped, 129 drawn (3 late): cycle 2 is not reported. A two-frame hole at 150-151.
    drops = [at(k) for k in range(200) if k not in (63, 126, 127, 128, 150, 151)]
    # Cycle 1 start dropped with the next drawn exactly 2 frames later: still reported.
    late2 = [at(k) for k in range(140) if k not in (63, 64)]
    return [
        {"name": "steady", "stamps": steady},
        {"name": "drops", "stamps": drops},
        {"name": "late_two", "stamps": late2},
    ]


NODE_SCRIPT = r"""
const path = require("path");
const dir = process.argv[1];
const K = require(path.join(dir, "cvep.js"));
const C = require(path.join(dir, "brainsync-client.js"));
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const num = (v) => (v === null ? NaN : v);
const out = {};
out.bits = Array.from(K.BITS);
out.frames = {};
for (const lag of cases.lags) {
  let s = "";
  for (let n = 0; n < cases.frames; n++) s += String(K.bit(lag / K.LAG_FRAMES, n));
  out.frames[lag] = s;
}
out.bars = cases.bars.map((v) => K.bar(num(v)));
out.label_lines = cases.label_lines.map(([l, s, b, w]) => K.labelLine(l, num(s), b, w === null ? undefined : w));
out.status_lines = cases.status_lines.map(([lead, c, ch]) => K.statusLine(num(lead), c, ch));
function layoutOf(slots, labels) {
  return { type: "layout", layout_id: 7, targets: slots.map((s, i) => ({ slot: s, label: labels ? labels[i] : s })) };
}
out.decisions = cases.decisions.map(([slots, scores, raw, committed, lead, changed]) => {
  const st = new C.ConfidenceState();
  st.handle({ type: "phase", phase: "run", flicker: true });
  st.handle(layoutOf(slots));
  st.handle({ type: "decision", layout_id: 7, scores, raw, committed, lead, changed });
  return st.lines().map((x) => x.line).concat([st.status()]);
});
out.training = cases.training.map(([cue, label, cycles, prog]) => {
  const st = new C.ConfidenceState();
  st.handle(layoutOf([cue, "x1", "x2"], [label, "a", "b"]));
  st.handle({ type: "phase", phase: "train", flicker: true, cue, cycles });
  if (prog) st.handle({ type: "train", cycle: prog[0], of: prog[1] });
  return st.status();
});
out.trained = cases.trained.map(([acc, chance]) => {
  const st = new C.ConfidenceState();
  st.handle({ type: "trained", accuracy: acc, chance, cycles: 30 });
  return st.status();
});
out.clock = cases.clock.map(({ name, stamps }) => {
  const fs = new C.FlickerState({ wall: (t) => t / 1000 });
  fs.handle({ type: "welcome", refresh_hz: 60, refresh_tolerance_hz: 1, refresh_rates: [60],
              paradigm: "code", code: { bits: Array.from(K.BITS), lag_frames: 21, cycle_frames: 63 } });
  fs.accept();
  fs.setOpen(true);
  fs.handle({ type: "phase", phase: "run", flicker: true });
  fs.handle(layoutOf(["t0", "t1", "t2"]));
  const ticks = stamps.map((t) => {
    const frame = fs.frame(t);
    const onset = fs.onsetDue(t);
    const cycles = fs.cycleDue(t).map((c) => [c.n, c.t_client]);
    return { frame, onset: onset ? onset.t_client : null, cycles, dropped: fs.dropped };
  });
  return { name, ticks, missed_cycles: fs.missedCycles };
});
process.stdout.write(JSON.stringify(out));
"""


def _finite(v):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v


def run_js(cases: dict) -> dict:
    proc = subprocess.run(["node", "-e", NODE_SCRIPT, str(JS_DIR)], input=json.dumps(cases),
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def build() -> dict:
    sequences = clock_sequences()
    cases = {
        "lags": list(LAGS), "frames": FRAMES, "bars": BARS,
        "label_lines": [list(c) for c in LABEL_LINES],
        "status_lines": [list(c) for c in STATUS_LINES],
        "decisions": [list(c[:6]) for c in DECISIONS],
        "training": [[c[0], c[1], c[2], list(c[3]) if c[3] else None] for c in TRAINING],
        "trained": [list(c[:2]) for c in TRAINED],
        "clock": sequences,
    }
    js = run_js(cases)

    # The code and the frame rule: Python and JavaScript must agree exactly.
    bits = [int(b) for b in live.BITS]
    if js["bits"] != bits:
        raise SystemExit("clients/js/cvep.js BITS differ from brainsync.vep.live.BITS")
    frames = {}
    for lag in LAGS:
        py = "".join(str(live.frame_bit(lag // live.LAG_FRAMES, n)) for n in range(FRAMES))
        if py != js["frames"][str(lag)]:
            raise SystemExit(f"frame rule differs between Python and JS at lag {lag}")
        frames[str(lag)] = py

    # The text: JavaScript is the reference; Python must agree except on exact ties.
    for v, expect in zip(BARS, js["bars"]):
        py = live.bar(math.nan if v is None else v)
        if v not in BAR_TIES and py != expect:
            raise SystemExit(f"bar({v}): Python {py!r} != JS {expect!r}")
    for case, lines in zip(DECISIONS, js["decisions"]):
        slots, scores, raw, committed, lead, changed, tie = case
        py = decision_lines({"scores": scores, "raw": raw, "committed": committed, "lead": lead,
                             "changed": changed})
        if not tie and py != lines:
            raise SystemExit(f"decision {scores}: Python {py!r} != JS {lines!r}")
    for (acc, chance, tie), line in zip(TRAINED, js["trained"]):
        py = trained_text({"accuracy": acc, "chance": chance})
        if not tie and py != line:
            raise SystemExit(f"trained {acc}: Python {py!r} != JS {line!r}")

    return {
        "source": "tools/ios_cvep_fixture.py: brainsync/vep/live.py, clients/js/cvep.js, "
                  "clients/js/brainsync-client.js",
        "bits": bits,
        "lag_frames": live.LAG_FRAMES,
        "cycle_frames": live.CYCLE_FRAMES,
        "lit": frames,
        "bars": [{"value": _finite(v), "bar": b} for v, b in zip(BARS, js["bars"])],
        "label_lines": [{"label": l, "score": _finite(s), "best": b, "width": w, "line": line}
                        for (l, s, b, w), line in zip(LABEL_LINES, js["label_lines"])],
        "status_lines": [{"lead": _finite(lead), "committed": c, "changed": ch, "line": line}
                         for (lead, c, ch), line in zip(STATUS_LINES, js["status_lines"])],
        "decisions": [{"slots": c[0], "scores": c[1], "raw": c[2], "committed": c[3],
                       "lead": c[4], "changed": c[5], "tie": c[6], "lines": lines}
                      for c, lines in zip(DECISIONS, js["decisions"])],
        "training": [{"cue_label": c[1].upper(), "cycles": c[2],
                      "cycle": c[3][0] if c[3] else None, "of": c[3][1] if c[3] else None,
                      "line": line} for c, line in zip(TRAINING, js["training"])],
        "trained": [{"accuracy": a, "chance": ch, "tie": tie, "line": line}
                    for (a, ch, tie), line in zip(TRAINED, js["trained"])],
        "clock": [{"name": s["name"], "stamps_ms": s["stamps"], "ticks": r["ticks"],
                   "missed_cycles": r["missed_cycles"]}
                  for s, r in zip(sequences, js["clock"])],
    }


def render() -> str:
    """One top-level key per line, each value compact (the clock tables are long)."""
    data = build()
    body = ",\n".join(f" {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}"
                      for k, v in data.items())
    return "{\n" + body + "\n}\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/ios_cvep_fixture.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="exit 1 if the fixture is missing or stale")
    p.add_argument("--out", type=Path, default=FIXTURE, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    text = render()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != text:
            print(f"stale: {args.out} (run python tools/ios_cvep_fixture.py)", file=sys.stderr)
            return 1
        print(f"up to date: {args.out}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
