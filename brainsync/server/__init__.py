"""BrainSync server: owns the EEG headset and the decoder, and talks to drawing clients.

    python -m brainsync.server --board fake --no-auth     # then open http://127.0.0.1:8765/

The wire contract is PROTOCOL-v3.md at the repo root; ``protocol`` holds its constants,
parsers and message builders. This package file imports nothing, so importing one submodule
(say the pure ``protocol``) never pulls in websockets, numpy or the board.
"""
from __future__ import annotations
