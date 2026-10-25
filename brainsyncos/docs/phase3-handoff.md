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
| `bsos/3d-llama` | All WIP. `scripts/build-llama.sh` and `llama/llama.env` (llama.cpp v0.7.0, all CPU variants, CUDA sm_87), the `brainsyncos-llama` packages, the sandboxed `brainsyncos-llama.service` and `85-llama.sh`. On the laptop the CPU part cross-compiled (408 steps, 653 s at 4 jobs). The CUDA part was stopped mid-compile. Nothing packaged or tested. |
| `bsos/build-speed` | A native tools container for packing, `.pyc` and ISO work on x86-64 hosts. Stopped early. Not needed on the Mac. |
| `bsos/3c-board`, `bsos/3d-llm-backends` | Already merged. |

The three 3d branches start from `bsos/3b-runtime` at `de467a6`, so merging
them brings in 3b too.

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

- **llama.cpp natively.** The laptop compiled through an amd64 cross-builder (`scripts/builder.sh --cross`, `docker/Dockerfile.cross-builder`). On the Mac that image would itself be emulated. Make `build-llama.sh` use the arm64 builder on an arm64 host, with NVIDIA's aarch64 `cuda-nvcc-13-2=13.2.86-1`, `cuda-cudart-dev-13-2` and `libcublas-dev-13-2` (build only, never shipped). Keep the cross path for x86-64 hosts.
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

## Status on the Mac, end of 2026-10-25 (branch `bsos/jetson`)

Phase 1 of this handoff (the merge) and most of Phase 2 are done. Phase 3's
scripts exist and Phase 4's checks pass. Pick up from "Next" below.

**Done and verified on the M4:**
- `bsos/jetson` = `custom-operating-brain-seperate` + `bsos/3b-runtime`,
  `bsos/3d-cuda`, `bsos/3d-model`, `bsos/3d-llama`, merged in that order.
  Every conflict kept both sides. Install steps: 00-50, 60, 70, 75, 80, 85, 86.
- llama.cpp compiles natively on arm64: `scripts/builder.sh --llama`
  (`docker/Dockerfile.llama-builder`, NVIDIA's aarch64 nvcc 13.2.86 from the
  Jetson repository, signed-by the committed key). About 160 s CPU and 450-560 s
  CUDA at 6 jobs. The check passes: 8 tokens on `libggml-cpu-armv8.2_2.so`,
  `/v1/messages` answers, the CUDA backend resolves against NVIDIA's runtime
  and falls back to the CPU with no GPU. x86-64 hosts keep the cross-builder.
- All 9 Jetson packages build, and `build/repo/jetson` signs (14 packages with
  the 5 mirrored NVIDIA debs, 1.7 GB).
- `tests/test_nocompile.sh` (`scripts/build.sh test nocompile`): all 5 checks
  pass. No compiler, `-dev`, dkms or pip in any Depends, maintainer script or
  step; ggml's scoring picks `armv8.2_2` (dotprod + fp16 only) for the A78AE;
  under `qemu-aarch64 -cpu cortex-a76` (now in the builder image) llama-server
  loads `armv8.2_2` and generates 8 tokens.

**Fixed on the way (from two reviews of the merged tree and one of the bundle):**
- The model package wrote `BSOS_LLAMA_ARGS`, which the llama unit never read,
  so the server would have run at 8192 context. `model.conf` now has
  `BSOS_LLAMA_MODEL`, `BSOS_LLAMA_ALIAS` and `BSOS_LLAMA_CTX_SIZE=4096`
  (`MODEL_CTX_SIZE` in `config/model.env`).
- Merge seams: phase 2 and VM hook checks wanted a comments-only `secrets.env`
  although 85 adds the key; `c_llama` expected 85 to create `secrets.env`,
  which 70 now does. 85 no longer creates it.
- `curl` and `qemu-user` added to the arm64 builder; `mirror_names` no longer
  swallows a malformed `mirror.env`; TLS verification back on for the pip
  download; Cartesia keys in the staged-secret scan.
- llama check: test servers run with `-v` (0.7.0 logs the loaded CPU backend
  only then); `libggml-cuda.so` is ldd'ed from `lib/ggml/`.
- Bundle installer: its test no longer shares `make-bundle.sh`'s staging dir;
  login options with an existing account and system accounts are refused; the
  steps wait up to 10 min for the dpkg lock and never remove a package.

**Not run yet:**
- `tests/test_bundle.sh` (`scripts/build.sh test bundle`): the install bundle
  has never been built or installed end to end. Run this first.
- `scripts/build.sh test phase3`, `test phase2`, `test vm` after the merge.
- The Jetson ISO (`scripts/build.sh iso --edition jetson`).
- Anything on an Orin.

**Next, in order:**
1. `scripts/build.sh test bundle`, then fix what it finds.
2. `scripts/build.sh test phase3`.
3. `scripts/build.sh iso --edition jetson` (about 7 GB in `build/out/`).
4. On an Orin with stock JetPack 7.2.1: the bundle, `sudo ./install.sh --user <first-boot user>`.

**On the next computer:**
- Not in git: the base ISO (`BASE_ISO=/path/to/jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso`),
  `config/authorized_keys`, `config/brainsync-password.hash`, and the repository signing key.
- The M4's key is `B377FE47DE0B4A568ABDD925976D2C017DAE3672`. No device is installed yet, so a new key
  (`scripts/build.sh key`) costs nothing. After the first install, keep that one key.
- Rebuild in order: `wheels`, `mirror`, `llama`, `packages`, `repo` (or `all`). The arm64 llama
  builder image is 4.3 GB; on x86-64 the cross-builder is used instead.
- Disk: the M4 ran out twice. Budget at least 25 GB free: the repository 1.7 GB, the bundle test
  several GB at its peak, the ISO about 7 GB, Docker's images and cache about 15 GB.
- Use `BSOS_BUILD_CPUS=6 BSOS_BUILD_JOBS=6 BSOS_BUILD_MEMORY=5g` on a 10-core Mac. Memory must
  exceed `BUILD_SCRATCH_SIZE` (3g) by 1g.
- Never edit `scripts/builder.sh` or `scripts/lib.sh` while a build runs: bash reads them as it
  goes, and an edit mid-run fails the build with nonsense errors.

**Open questions:**
- Two native CUDA compiles of the same inputs differ (`libggml-cuda.so` 39727376 vs 39661840
  bytes), so that build is not reproducible yet.
- espeak-ng comes over plain http with a hand-checked sha256, not from a signed index like the
  NVIDIA mirror; it must stay out of public release files.
- The CUDA drop-in sets `PrivateDevices=no`; replace it with `DeviceAllow=` lines once
  `ls -l /dev/nvhost* /dev/nvmap /dev/nvgpu/igpu0/` has been read on an Orin.
- An NVIDIA kernel update removes `brainsyncos-ch341-tegra` (exact-version Depends): hold
  `nvidia-l4t-kernel` or rebuild for the new kernel.
- No cloudflared tunnel service is in the image; only `brainsyncos-llama` (localhost) is enabled.
