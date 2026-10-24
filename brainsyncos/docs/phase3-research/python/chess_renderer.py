"""Drive BrainChess BoardRenderer for a few frames (matplotlib Agg pieces + ElementArrayStim)."""
import numpy as np
from brainsync.stimulus.config import load_config
from brainsync.brainchess.stimulus import BoardRenderer, BoardState
cfg = load_config("chess")
r = BoardRenderer(cfg)
snap = BoardState().snapshot()
print("chess refresh", round(r.measure_refresh(n=20, warmup=5), 1))
for k in range(20):
    r.draw(snap, np.full(64, k % 2, float), np.full(len(cfg.targets), k % 2, float))
    r.flip()
r.close()
print("chess renderer ok")
