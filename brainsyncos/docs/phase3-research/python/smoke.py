"""Exercise every psychopy call BrainSync's renderers make (display.py, brainchess/stimulus.py)."""
import sys, time
import numpy as np
from PIL import Image
from psychopy import event, visual

png = "/tmp/card.png"
Image.new("RGB", (200, 300), (200, 50, 50)).save(png)
win = visual.Window(fullscr=True, screen=0, units="height", color=(-1, -1, -1), checkTiming=False)
win.setMouseVisible(False)
print("win.size", tuple(win.size))
sq = visual.Rect(win, size=(0.2, 0.2), pos=(-0.3, 0), lineColor=None, fillColor=(0, 0, 0))
box = visual.Rect(win, size=(0.3, 0.3), pos=(-0.3, 0), fillColor=None, lineColor="white", lineWidth=4)
txt = visual.TextStim(win, text="Like", pos=(0, -0.4), height=0.045, color=(0.5, 0.5, 0.5))
img = visual.ImageStim(win, image=png, pos=(0, 0), size=(0.2, 0.3), interpolate=True)
ea = visual.ElementArrayStim(win, units="height", nElements=64, sizes=0.05, xys=np.random.rand(64, 2) - 0.5,
                             elementTex=None, elementMask=None, colors=np.zeros((64, 3)), colorSpace="rgb")
arrow = visual.ShapeStim(win, units="height", vertices=[(0, 0), (0.1, 0.05), (0, 0.1)], fillColor="red", lineColor=None)
stamps = []
for k in range(60):
    sq.fillColor = (k % 2 * 2 - 1,) * 3
    for s in (img, sq, box, txt, ea, arrow):
        s.draw()
    stamps.append(win.flip())
event.clearEvents()
print("keys", event.getKeys(keyList=["escape", "space"]))
d = np.diff(stamps)
print(f"flips ok: {len(stamps)}, median interval {np.median(d)*1000:.2f} ms")
win.close()
mods = sorted(m.split(".")[0] for m in sys.modules if not m.startswith("_"))
print("top-level modules loaded:", sorted(set(mods) - set(sys.stdlib_module_names)))
