# Phase 3 research material

Reference material from the 2026-10-24 research behind Phase 3 parts 3b to
3d. The 3c inputs moved into the build (see the table); the rest is not
wired in yet. The plan and status are in
[../phase3-notes.md](../phase3-notes.md); the raw findings are in
[findings.md](findings.md).

These are prototypes: the scripts ran once, in throwaway `ubuntu:24.04` arm64
containers, with paths of that session.

| Folder | What it is | Used in |
|---|---|---|
| `brainflow/*.sh` | arm64 build, `.deb` packaging and an offline install test of the fork. The patch they used, the uncommitted change to `cerelog.cpp`, now lives in `wheels/brainflow-cerelog/patches/` | 3c (done) |
| `python/brainsync.in`, `brainsync.lock.txt` | the venv's input and its hash-locked resolution (uv, cp312, `aarch64-manylinux_2_39`, wheels only) | 3b |
| `python/psychopy-excludes.txt` | the PsychoPy dependencies left out (wxPython and others without arm64 wheels) | 3b |
| `python/*.py`, `syslibs.sh` | Xvfb smoke tests: `from psychopy import visual, event`, a pyglet window, every app module imports; the system libraries the venv needs | 3b |
| `ch341/build-ch341.sh` | the out-of-tree build of the CH340 USB-serial driver against `nvidia-l4t-kernel-headers`. `ch341.c` (Linux 6.8.12, GPL-2.0) and `Kbuild` now live in `packages/brainsyncos-ch341-tegra/` | 3c (done) |
| `cuda/closure.py`, `pk.py` | dependency closure and sizes of NVIDIA's `jetson/common r39.2` packages from its Packages index | 3d |
| `llama/llm.sh`, `llm_probe.py` | llama.cpp CPU build and `llama-server` smoke test (health, auth, JSON schema, grammar, tools) | 3d |
