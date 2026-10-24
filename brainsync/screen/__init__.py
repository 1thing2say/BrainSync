"""BrainPoint screen understanding: find the clickable elements in a screenshot.

``detect`` asks Claude Haiku for the elements of a screenshot, ``selector`` turns them into
pages of non-overlapping flicker patches (top-K plus "more", PROTOCOL-v3.md section 4 rule 4),
and ``controller`` runs point mode for the server's session. This package file imports
nothing, so the anthropic SDK loads only when a detection is made.
"""
from __future__ import annotations
