"""Predicted-brain heatmaps for the Reels demo: Meta TRIBE v2 run offline on our own clips.

Decoration only, never part of decoding. TRIBE v2 predicts average-subject fMRI (fsaverage5
cortex, one row per second, already shifted for the haemodynamic lag) from a clip's video and
audio. ``predict`` runs the model (its own venv: torch, tribev2), ``render`` turns the rows into
PNG frames plus a manifest the extension panel loads, ``manifest`` is the stdlib-only format
both sides share. Importing this package loads neither torch nor tribev2.
"""
from __future__ import annotations
