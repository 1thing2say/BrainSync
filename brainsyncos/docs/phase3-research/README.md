# Phase 3 research material

Reference material from the 2026-10-24 research behind Phase 3 parts 3b to
3d. None of it is wired into the build yet. The plan and status are in
[../phase3-notes.md](../phase3-notes.md); the raw findings are in
[findings.md](findings.md).

These are prototypes: the scripts ran once, in throwaway `ubuntu:24.04` arm64
containers, with paths of that session.

| Folder | What it is | Used in |
|---|---|---|
| `brainflow/0001-cerelog-connect_v1-raw-timeout.patch` | the only copy of the uncommitted change to the Cerelog fork's `cerelog.cpp` (raw mode, 1 s read timeout in `connect_v1`), against commit `932b3944` | 3c |
| `brainflow/*.sh` | arm64 build, `.deb` packaging and an offline install test of the fork | 3c |
| `python/` (moved) | the venv's lock inputs and Xvfb smoke tests are now in use: `brainsyncos/python/` (`brainsync.in`, `brainsync.lock.txt`, `psychopy-excludes.txt`) and `brainsyncos/tests/runtime/*.py`; `syslibs.sh` became part of `tests/runtime-checks.sh` | 3b |
| `ch341/ch341.c`, `Kbuild`, `build-ch341.sh` | the CH340 USB-serial driver (Linux 6.8, GPL-2.0) and its out-of-tree build against `nvidia-l4t-kernel-headers` | 3c |
| `cuda/closure.py`, `pk.py` | dependency closure and sizes of NVIDIA's `jetson/common r39.2` packages from its Packages index | 3d |
| `llama/llm.sh`, `llm_probe.py` | llama.cpp CPU build and `llama-server` smoke test (health, auth, JSON schema, grammar, tools) | 3d |
