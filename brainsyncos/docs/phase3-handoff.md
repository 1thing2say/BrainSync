# BrainSyncOS: handoff to the Mac (2026-10-25)

The rest of Phase 3 moves to the Apple Silicon Mac (M4). On the x86-64 WSL
laptop every arm64 step runs under qemu-user, 5 to 10 times slower: a phase 3
suite took 53 minutes there against about 5 on the Mac, and four builds at once
overloaded it. On the Mac `scripts/builder.sh` runs natively, unchanged.

This file says what was decided, where each part is, and what to do next.
The parts' details stay in [phase3-notes.md](phase3-notes.md).

## Decisions (2026-10-25)

| Topic | Decision |
|---|---|
| Order | Jetson first. The VM edition, its suite and the ISO wait until the Jetson packages work. |
| Compiling | Everything is precompiled for the Orin Nano (Cortex-A78AE, ARMv8.2 with dotprod and fp16; GPU sm_87). Nothing compiles on the device: no compiler in any Depends, no DKMS, no `pip` builds. |
| First delivery | An install bundle for an Orin that already runs stock JetPack 7.2.1 (Phase 3 below), then the ISO, built on the Mac. |
| Public release | Only our own packages on the GitHub release page: `brainsync`, `brainsync-runtime`, the BrainFlow wheel, `brainsyncos-llama` and `-llama-cuda`, `brainsyncos-ch341-tegra`, `brainsync-udev`, the model package, plus standalone llama.cpp archives (`llama.cpp-v0.7.0-linux-aarch64-cpu.tar.xz`, `...-cuda-sm87.tar.xz`), a signed `SHA256SUMS` and the repository key's public part. No ISO and no NVIDIA debs: the repository is public and the ISO redistributes NVIDIA's L4T and CUDA. GitHub caps each file at 2 GiB. Ask before creating a release or pushing a tag. |
| Offline voice | espeak-ng: Cartesia first, then espeak-ng, then printed text. |
| Secrets | `/etc/brainsync/secrets.env`, made by `70-brainsync.sh` as a commented template, `root:<appliance account's group>`, mode 0640, never overwritten. `85-llama.sh` adds `BRAINSYNC_LLAMA_API_KEY`, made on each device at install. |
| NixOS | Looked at, not taken. `jetpack-nixos` supports JetPack 7 on the Orin Nano, but its CUDA builds are not in a public cache, BrainFlow and the CH340 driver would need new Nix packages, and installing means recovery-mode flashing. Worth another look after the demo. |
| Other arm64 builders | GitHub Actions has free arm64 runners for public repositories (`ubuntu-24.04-arm`). Not set up. The signing key must never go there: CI would build unsigned debs, signed locally. |

## Branches (all pushed)

None of the 3b-finish or 3d work was reviewed: the review stage never ran.

| Branch | State |
|---|---|
| `custom-operating-brain-seperate` | 3a, 3c and the 3d app side (BCI Voice tries llama-server, then Claude, then the fixed lists) merged. |
| `bsos/3b-runtime` | `brainsync-runtime`, `brainsync`, `70-brainsync.sh` and the BrainFlow wheel in the venv: done, 12 phase 3 checks passed. Plus one WIP commit, not built: `secrets.env`, the espeak-ng package, checks updated for the new steps. |
| `bsos/3d-cuda` | NVIDIA's CUDA 13.2 runtime mirrored into the Jetson repository (`mirror/nvidia-jetson/`, `scripts/fetch-mirror.sh`, `scripts/mirror.py`, sha256-pinned `debs.lock`), `80-cuda.sh`, and their checks. One WIP commit (docs, `mirror.py`) not re-run. |
| `bsos/3d-model` | The model package (Qwen3.5-2B Q4_K_M from `unsloth/Qwen3.5-2B-GGUF`, pinned in `config/model.env`), `86-model.sh`, and an ISO size limit (`ISO_MAX_BYTES`). One WIP commit (checks, docs); its suite run was stopped. |
| `bsos/3d-llama` | llama.cpp **compiled and checked on the laptop** (see "Prebuilt on the laptop" below): `scripts/build-llama.sh`, `llama/llama.env` (v0.7.0, all CPU variants, CUDA sm_87), `scripts/pack-llama-release.sh`. Still WIP and untested: the `brainsyncos-llama` packages, the sandboxed `brainsyncos-llama.service` and `85-llama.sh`. |
| `bsos/build-speed` | A native tools container for packing, `.pyc` and ISO work on x86-64 hosts. Stopped early. Not needed on the Mac. |
| `bsos/3c-board`, `bsos/3d-llm-backends` | Already merged. |

The three 3d branches start from `bsos/3b-runtime` at `de467a6`, so merging
them brings in 3b too.

## Prebuilt on the laptop (2026-10-25)

The compiles that suit x86-64 were done on the laptop, so the Mac does not
repeat them. `nvcc` and the cross gcc run natively there, and building the
CPU and CUDA parts in one run keeps their `libggml-base` byte-identical.

**`brainsyncos-prebuilt-2026-10-25.tar.xz`**, 23,677,916 bytes, sha256
`732c0a8e36399195750d1fa208c17c741ad1be80260e6ead19e61b0b29112eb0`. It is in
the laptop's `C:\Users\ecart\Downloads` (and `~/BrainSyncOS/brainsyncos/build/out/` in WSL). Copy
it to the Mac by any means: the sha256 above, committed in git, is what
makes the copy trustworthy. It holds:

| Path | What | Pins |
|---|---|---|
| `brainsyncos/build/llama/` | llama.cpp v0.7.0 for aarch64: `cpu/` (the tree for `/opt/brainsyncos/llama`, all 8 CPU variants) and `cuda/libggml-cuda.so` (sm_87, 39.7 MB), with `llama.env` and `check.env` | `SHA256SUMS` sha256 `a0fdca0eb38239dcc2611fefa51c595335af224bc1e97ac9e1dac367f927a76e`; `INPUTS_SHA256=a9cc0075b6460fa7cac538fc399eae9378f93200a8e86b62952f75ee3ea27a66`; `PARTS=cpu,cuda` |
| `brainsyncos/build/wheels/` | the BrainFlow Cerelog wheel from 3c (47 minutes to build under emulation) | `SHA256SUMS` sha256 `29ae543aafe25ddbce5f729dda3f8839aca01b1a53703625fde498fd7a65b736`; wheel sha256 `6ed6d54c54007c44551a3e2170c67af2cf23ecb256450c4ae2d281b72dc46e74` |

How it was built and checked (`bsos/3d-llama`, commit `b0e68aa`):
- Compiled in the amd64 cross-builder: CPU 408 steps in 376 s, CUDA 145 objects in 881 s, 5 jobs, gcc 14.2.0, nvcc 13.2.86.
- `scripts/build-llama.sh check` in the arm64 builder (qemu) passed:
  - all 36 ELF files resolve; they need only glibc 2.38 and libstdc++;
  - `--version` reports build 11669 / `620d222`;
  - 8 tokens from `/completion`, and `/v1/messages` answers;
  - the CUDA backend resolves against NVIDIA's Jetson `cuda-cudart-13-2` and `libcublas-13-2`, never `libcuda.so.1`, and loads with no GPU.
- On emulated Cortex-A76 and Neoverse-N1 cores (Orin-class: ARMv8.2, dotprod, fp16), llama.cpp loads `libggml-cpu-armv8.2_2.so`; on a Cortex-A57, `armv8.0_1`. Not yet in a script: Phase 4 below.

On the Mac, after the merge (Phase 1):

```sh
shasum -a 256 brainsyncos-prebuilt-2026-10-25.tar.xz    # must print 732c0a8e…12eb0
tar -xJf brainsyncos-prebuilt-2026-10-25.tar.xz -C <the BrainSync checkout>
cd <the BrainSync checkout>/brainsyncos
(cd build/llama && shasum -a 256 -c SHA256SUMS) && (cd build/wheels && shasum -a 256 -c SHA256SUMS)
scripts/build.sh llama     # "compiled for these inputs": skips the cross-builder, runs check natively
```

`scripts/build.sh` (`llama`, `all`) now checks on the host whether
`build/llama` is compiled for the current inputs (`llama_compiled`), so the
amd64 cross-builder never starts on the Mac. That holds while `llama/`, the compile part of
`scripts/build-llama.sh` (above its `# --- check` line) and
`docker/Dockerfile.cross-builder` are unchanged. If one of them changes, build/llama
must be rebuilt: on the laptop, or add a native arm64 compile path to
`build-llama.sh`. `build-wheels.sh` likewise keeps the wheel while its inputs
are unchanged.

Release archives, made from `build/llama` by `scripts/pack-llama-release.sh`
(reproducible: two runs gave the same bytes; on macOS it needs `brew install gnu-tar`):
- `llama.cpp-0.7.0-b11669-linux-aarch64-cpu.tar.xz`, 4,708,164 bytes;
- `llama.cpp-0.7.0-b11669-linux-aarch64-cuda-sm87.tar.xz`, 22,264,324 bytes;
- their `SHA256SUMS` hashes to `d55d7a64a4ac3e654c533e33c1a741b655ef7316da2fdd7ad20677ed5734641b`.

A rerun on the Mac should give the same bytes, which also checks GNU tar there.

## On the Mac: setup

```sh
cd BrainSync && git fetch origin
git checkout custom-operating-brain-seperate && git pull
git checkout -b bsos/jetson
cd brainsyncos
```

Inputs that are not in git:
- the base ISO, `jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso`, in `~/Downloads` (sha256 pinned in `config/base-iso.env`);
- `config/authorized_keys`, and optionally `config/brainsync-password.hash`;
- the repository signing key. One exists on the laptop (`~/BrainSyncOS/brainsyncos/config/repo-signing-key.asc` in WSL). Copy it securely (not through git, chat or a cloud drive) or make a new one with `scripts/build.sh key`. No device has been installed yet, so a new key costs nothing. After the first install, keep using that one key.

Raise the build caps to what Docker Desktop has, e.g. `BSOS_BUILD_CPUS=8 BSOS_BUILD_JOBS=8 BSOS_BUILD_MEMORY=12g`.

## Phase 1: merge

Merge into `bsos/jetson`, in this order: `bsos/3b-runtime`, `bsos/3d-cuda`,
`bsos/3d-model`, `bsos/3d-llama`. Expect conflicts in `scripts/build.sh`,
`scripts/builder.sh`, `Makefile`, `tests/test_phase3.sh`,
`tests/phase3-checks.sh`, the phase 2 and VM checks, `README.md` and
`docs/phase3-notes.md`. Each branch adds its own lines; keep all of them.
Install steps after the merge: 00 to 50 (installer identity), 60 repository
key, 70 apps, 75 USB serial, 80 CUDA, 85 llama, 86 model.

## Phase 2: build the Jetson packages once

- **llama.cpp: no compile.** Use the prebuilt `build/llama` (above); `scripts/build.sh llama` only runs its check. Only if its inputs change: rebuild on the laptop, or make `build-llama.sh` compile in the arm64 builder on an arm64 host with NVIDIA's aarch64 `cuda-nvcc-13-2=13.2.86-1`, `cuda-cudart-dev-13-2` and `libcublas-dev-13-2` (build only, never shipped). The amd64 cross-builder would be emulated on the Mac.
- **BrainFlow: no compile.** The prebuilt `build/wheels` goes into `brainsync-runtime`.
- Build: `scripts/build.sh packages --edition jetson`, then the repository. The Jetson edition only for now.
- Check: `scripts/build.sh test phase3`. Then review the merged diff, as the stopped workflow would have: correctness, and trust/packaging (pins, signatures, secrets, sandboxing, maintainer scripts).

## Phase 3: install bundle for a running Orin

For an Orin already on stock JetPack 7.2.1 (Jetson Linux R39.2.1), with no reflash.
- `scripts/make-bundle.sh` writes `build/out/brainsyncos-jetson-<version>.tar`: the signed repository, its public key, the `install.d` steps 60 to 86 with `payload/lib`, and `install.sh`.
- `install.sh` (run as root on the Orin):
  - checks `/etc/nv_tegra_release` says R39.2.1; the CH340 module and CUDA 13.2 are built for exactly that;
  - checks free space;
  - mounts or points `BSOS_REPO_DIR` at the bundle's repository and runs the steps through the same `payload/lib/apt-repo.sh` as `hook.sh`, outside curtin;
  - asks for the account that runs the apps (JetPack's first-boot user, or creates `brainsync`), where 30-user.sh would have made it.
- Check: run `install.sh` in NVIDIA's real target layer the way `phase3-checks.sh` runs the hook. Then on the Orin.

## Phase 4: checks that nothing compiles on the device

- A scan over every deb in the repository and every install step: no compiler, `-dev`, `dkms` or `cmake` in Depends, and no `gcc`, `make`, `dkms` or `pip install` in maintainer scripts or steps.
- llama.cpp under qemu's Cortex-A76 model, the closest to the Orin's A78AE: `QEMU_CPU=cortex-a76`. It must load an `armv8.2` CPU variant and generate a few tokens from the pinned test model (`TEST_MODEL_*` in `llama/llama.env`).

## After that

1. 3e on the Mac: the Jetson ISO end to end (size limit included), the VM ISO, the full suites, a UTM install, and `docs/jetson-acceptance.md`.
2. The release files (decisions above) built into `build/release/`, and shown before anything is published.
3. Hardware: the first Orin install, the CUDA backend loading, `llama-bench`, peak memory, the board as `/dev/cerelog` with BrainFlow streaming, and flicker timing while the LLM runs.

Open questions for you are in phase3-notes.md (3c: V1/V2 boards, Secure Boot, holding `nvidia-l4t-kernel`, the BrainFlow patch upstream).
