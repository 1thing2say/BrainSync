"""Drive the app's own PsychoPyRenderer (two_targets and swipe with a card) for a few frames."""
import numpy as np
from PIL import Image
from brainsync.stimulus.config import load_config
from brainsync.stimulus.display import PsychoPyRenderer
Image.new("RGB", (400, 600), (90, 30, 30)).save("/tmp/c.png")
for name, cards in (("two_targets", ()), ("swipe", ("/tmp/c.png",))):
    cfg = load_config(name)
    r = PsychoPyRenderer(cfg, cards=cards)
    print(name, "refresh(Hz, Xvfb no vsync)", round(r.measure_refresh(n=30, warmup=5), 1))
    for k in range(30):
        r.draw(np.full(len(cfg.targets), k % 2, float), cue=0, card=0 if cards else -1, flash=1)
        r.flip()
    print(name, "escape?", r.escape_pressed())
    r.close()
print("renderer ok")
