"""Live scalp map of the user's own EEG from the Cerelog X8's 8 electrodes.

``python -m brainsync.brainmap`` reads the board (or a fake board, or a replayed recording),
computes per-electrode band power against a rest baseline, 15/17 Hz SSVEP SNR and contact
quality every 0.25 s, and streams them over Server-Sent Events to extension/brainmap/, which
draws an interpolated head map. Eight scalp electrodes cannot locate sources inside the
cortex, so this is a scalp map, never a brain scan. ``layout`` (electrode positions),
``features`` (one window's numbers), ``stream`` (board to frames) and ``server`` (HTTP + SSE)
use numpy, scipy and the standard library only, so the same code runs on the Jetson.
Importing this package loads none of them.
"""
from __future__ import annotations
