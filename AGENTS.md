@PLAN.md
@INTERFACE-v2-draft.md

## Teammate's decoder repo (reference it when building)

The SSVEP decoder and the PsychoPy flicker display live in the teammate's repo, cloned at
`/mnt/c/Users/ecart/ssvep` (`../ssvep`, GitHub `1thing2say/ssvep`, branch `feat/spectrum-check`).
It is the other side of the contract in `INTERFACE-v2-draft.md`.

- Before building or changing anything that touches the decoder side (the `/ws/decoder` messages,
  `tools/fake_decoder.py`, `tools/contract_check.py`, timings, slots, refresh, display commands),
  read the relevant code there and check that what we build matches it. Run
  `git -C /mnt/c/Users/ecart/ssvep pull` first so it is current.
- Where to look: `ssvep/online/decoder.py` (SlidingDecoder: window, votes, scores, baseline),
  `ssvep/online/demo.py` (how the decoder drives the display), `ssvep/stimulus/` (PsychoPy display:
  `display.py`, `feedback.py` stdin protocol, `markers.py`, `config.py`, `configs/two_targets.toml`),
  `ssvep/acquisition/board.py` (Cerelog X8, Unix timestamps). PLAN.md section 12 summarizes it.
- It is the teammate's repo: read it, never edit, commit or push there unless the user asks.
  Our code never imports it (separate venv).
