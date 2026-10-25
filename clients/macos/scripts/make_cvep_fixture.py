"""Write the c-VEP fixture of the Swift tests from brainsync.vep.live. From the repo root:

    PYTHONPATH=. python clients/macos/scripts/make_cvep_fixture.py \\
        clients/macos/Tests/BrainPointCoreTests/Fixtures/cvep_fixture.json
"""
import json
import sys
from pathlib import Path

from brainsync.vep import live

out = Path(sys.argv[1])
frames = 200
data = {
    "source": "brainsync.vep.live (BITS, frame_bit, bar); regenerate with PYTHONPATH=. python "
              "clients/macos/scripts/make_cvep_fixture.py",
    "bits": "".join(str(b) for b in live.BITS),
    "lag_frames": live.LAG_FRAMES,
    "cycle_frames": live.CYCLE_FRAMES,
    "n_targets": live.N_TARGETS,
    "train_cycles": live.TRAIN_CYCLES,
    "frames": frames,
    "targets": [[int(live.frame_bit(k, n)) for n in range(frames)] for k in range(live.N_TARGETS)],
    "bars": [{"value": v, "bar": live.bar(v)} for v in
             (-0.5, 0.0, 0.0625, 0.07, 0.1, 0.11, 0.42, 0.5, 0.94, 1.0, 1.7)],
}
out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
print(f"wrote {out}: {len(data['bits'])} bits, {frames} frames x {live.N_TARGETS} targets")
