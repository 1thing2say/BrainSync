# BrainSyncOS Phase 3: notes and handoff

Status as of 2026-10-24, branch `custom-operating-brain-seperate`. The work so
far ran on an Apple M4 Mac (arm64, Docker Desktop, UTM). It continues on an
x86-64 laptop. This file lists what is done, what the next parts need, and what
changes on x86-64.

## Decisions (approved 2026-10-24)

| Topic | Decision |
|---|---|
| Apps on the image | SSVEP core (demo, record, check, stimulus, analysis tools), BrainSwipe and its browser mock, BrainChess, BCI Voice. Reels is left out: it needs Chrome, instagram.com and a login. |
| CUDA | Runtime only: `cuda-cudart-13-2` and `libcublas-13-2`, about 0.5 GB, Jetson ISO only. No toolkit (3.3 GB), no cuDNN, no TensorRT. `sudo apt install nvidia-jetpack` still works on the device through NVIDIA's repo, which a stock install already configures. |
| llama.cpp | A CPU build for both editions, plus the CUDA backend for the Orin (sm_87). |
| Model | Qwen3.5-2B Q4_K_M (1.3 GB, Apache-2.0) on the Jetson ISO, so the ISO fits an 8 GB stick. The VM gets no model by default. |
| LLM in BCI Voice | Local `llama-server` first, then Claude, then the fixed lists. |
| Who gets the ISO | Your own devices only. This matters for the NVIDIA licence question. |
| VM install | Its install steps may download from Ubuntu's archive (its install already needs the network). The Jetson install stays offline. |
| CH340 driver | Ship it (`brainsyncos-ch341-tegra`). NVIDIA's kernel has `CONFIG_USB_SERIAL_CH341` unset, while `USB_ACM`, `CP210X` and `FTDI_SIO` are modules. |
| Build resources | Builder containers are capped at 2 CPUs, 2 jobs and 6 GiB (`config/build.env`). |

## Done: part 3a (build system and signed repository)

See the README sections "Building BrainSyncOS" and "The package repository
(Phase 3)". In short:
- `scripts/build.sh` (or `make`) is the one entry point. Its commands are `doctor`, `key`, `packages`, `repo`, `iso`, `all`, `test` and `clean`.
- `scripts/builder.sh` applies the resource caps from `config/build.env`.
- Packages live in `packages/NAME/{package.env,build.sh}`. The only one so far is `brainsyncos-archive-keyring`.
- `scripts/build-repo.sh` signs the packages into an apt repository. The ISO builds check it, map it onto the ISO at `/brainsyncos-repo`, and put its public key in the payload.
- `payload/hook.sh` mounts the repository read-only in the target. `payload/lib/apt-repo.sh` installs from it alone, with signature checks. `install.d/60-apt-repo.sh` installs the keyring.
- The product ISO builds now compute their free-space need instead of requiring 20 GiB.

**Not done yet in 3a:**
- No real repository key exists (`config/repo-signing-key.asc`). Create it once with `scripts/build.sh key` on the machine you build on, and back it up.
- The product ISOs in `build/out/` were deleted for disk space. Rebuild them with `scripts/build.sh all`.
- Not yet run end to end in UTM: an install of a 3a ISO, which would exercise the repository mount under real subiquity/curtin. Do it with `scripts/utm-vm.sh install` on a Mac. It erases that VM's disk.

## Next: parts 3b to 3e

Details, versions and evidence are in [phase3-research/findings.md](phase3-research/findings.md). The prototypes the research used are in `phase3-research/` (see its README).

### 3b: the apps (parent repo plus `packages/brainsync*`)

1. **Make the app work from an install** (changes outside `brainsyncos/`). **Done 2026-10-24**, after merging `main` (the protocol v3 `brainsync.server` included):
   - `brainsync/paths.py` resolves every place the apps read or write. Each is a `BRAINSYNC_*` variable if set; otherwise the repo folder in a checkout, or the installed place: config `/etc/brainsync`, UI `/usr/share/brainsync/ui`, the v3 server's clients `/usr/share/brainsync/clients`, secrets `/etc/brainsync/secrets.env`, state (user profile, TTS cache) `$XDG_STATE_HOME/brainsync`, logs `<state>/logs`. `app/config.py`, `app/log.py`, `app/server.py`, `brainsync/server/app.py`, `brainsync/voice.py` and the two TTS tools use it. In a checkout nothing moves.
   - `[project.scripts]`: `brainsync-demo`, `-record`, `-check`, `-stimulus`, `-swipe`, `-swipe-web`, `brainchess`, `brainsync-server` (v3), `brainsync-voice`.
   - BrainChess finds `/usr/games/stockfish` (Ubuntu's package) without `PATH`.
   - Checked: a wheel installed with `pip --target` outside the repo runs `brainsync-voice` (page 200, `/api/health`, log in the state folder) with `BRAINSYNC_CONFIG_DIR` and `BRAINSYNC_UI_DIR` set. `tests/test_install_layout.py` covers the wiring.
   - **Still open:** the speech fallback. `brainsync/voice.py` speaks only through Cartesia; offline it prints. Decide whether to add `espeak-ng` (in the Jetson pool) as an offline voice.
2. **Package the runtime as `brainsync-runtime`**. **Done 2026-10-24** (branch `bsos/3b-runtime`).
   - A venv at `/opt/brainsyncos/venv`, built by `scripts/venv.sh` in the builder at that exact path, from the 57 hash-locked wheels of `python/brainsync.lock.txt` (moved out of the research folder). pip 26.2.2, pinned by sha256 in `python/pip.env`, runs from its own wheel and installs them with `--no-deps --require-hashes`.
   - The lock was not re-made: `pyproject.toml`'s dependencies did not change in the merge. `python/check-lock.py` checks this on every build of `brainsync`.
   - Trimmed (`tests` folders, `psychopy/demos`; `numpy/testing` kept) and compiled to unchecked-hash `.pyc`. 79 MB as an xz deb, 434 MB installed.
   - Depends: `python3.12 (>= 3.12.3, << 3.13)`, `libc6`, `libgcc-s1`, `libstdc++6`, `zlib1g`. The GL and X libraries are Recommends: NVIDIA's package list has them all; the VM target and NVIDIA's minimal layer do not.
   - Wheels are cached in `build/wheelhouse/`; rebuilds are offline.
   - **For 3c:** put the BrainFlow fork's wheel in `build/wheels/local/` and its name (`brainflow`) in `python/local-wheels.txt`. The build fails if a listed wheel is missing.
3. **Package the app itself as `brainsync`**. **Done.** The app wheel in the same venv (installed only long enough to take its files), `/usr/bin` links to the 9 console scripts, the 3 YAMLs as conffiles in `/etc/brainsync`, `ui/` and `clients/` (without tests) under `/usr/share/brainsync`. 1.1 MB. Depends on exactly its `brainsync-runtime` version. `scripts/builder.sh` now mounts the BrainSync repository read-only at `/src` for it.
4. **Secrets** (`ANTHROPIC_API_KEY`, `CARTESIA_API_KEY`) are never put into a build; the `brainsync` build refuses any `.env` file. They go on the device in `/etc/brainsync/secrets.env`. Nothing creates that file yet, or its owner and mode.
5. **Install step:** `70-brainsync.sh`, on both editions. **Done.** It installs both packages from the ISO's repository, checks that the venv imports the apps and that `brainsync-voice --help` runs, and names missing display libraries.

**Verified (tests/test_phase3.sh, all 12 checks passed, x86-64 WSL2, arm64 under qemu, 53 min):**
- Each edition's packages are built in a separate container and are byte-identical, the venv included. A first run differed only in `Installed-Size` (counted with `du`); it is now counted per file.
- In a stock Ubuntu arm64 container with Xvfb: both debs install with `dpkg -i`; all 77 app modules import; PsychoPy's smoke test, the two-target and swipe renderers and the chess renderer draw; the 9 commands answer `--help`; `brainsync-voice` serves its page (200) and `/api/health` offline, with its log in `~/.local/state/brainsync/logs`; nothing is written under `/opt`.
- In NVIDIA's real target layer, the hook's `70-brainsync.sh` installs both and `brainsync-voice` runs there.

**Not done or not verified:**
- `tests/test_phase2.sh` and `tests/test_vm.sh` were not run with the new step. Their hook checks now expect 8 steps and the two packages.
- No real install on a Jetson or in UTM. Display timing only under Xvfb (no vsync).
- The app's pytest suite was not run against the installed wheel (pytest is not in the venv).
- Build time under qemu: about 25 minutes per edition at 2 CPUs, mostly xz on the runtime. zstd was not measured.
- No systemd units, kiosk or display stack: the apps are installed, nothing starts them.
- The speech fallback (`espeak-ng`) is still open.

### 3c: the board

1. **The BrainFlow fork.**
   - Source: `https://github.com/shakimiansky/Shared_brainflow-cerelog`, commit `932b3944bfe6c0f4e29a46b4a34afdeba39edd85` (public, fetchable without credentials).
   - Apply `phase3-research/brainflow/0001-cerelog-connect_v1-raw-timeout.patch`. **It exists nowhere else**: it was an uncommitted change on the Mac.
   - Build with CMake: `BUILD_SYNCHRONI_SDK=OFF BUILD_BLE=OFF BUILD_BLUETOOTH=OFF BUILD_ONNX=OFF`, then `make BoardController DataHandler MLModule`.
   - Install it into the venv (not as a system deb, which the venv could not see). Check: `BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD)` prints 250.
2. **`brainsyncos-ch341-tegra`.**
   - Build `phase3-research/ch341/ch341.c` (GPL-2.0, from Linux 6.8) against `nvidia-l4t-kernel-headers` from the ISO pool; the build takes about 11 s.
   - Install it to `/lib/modules/6.8.12-1021-tegra/updates/` and run `depmod` in its postinst.
   - Depend on `nvidia-l4t-kernel (= 6.8.12-tegra-39.2.1-20260806224157)`.
   - The module is unsigned. It loads only while Secure Boot and module signature enforcement are off.
3. **`brainsync-udev`.**
   - Match `idVendor` `1a86` (V1, CH340) and `303a` (V2, ESP32-S3).
   - Set `GROUP=dialout` and `MODE=0660`, add `SYMLINK+=cerelog`, and set `ID_MM_DEVICE_IGNORE=1` so ModemManager leaves the port alone.
4. **Install step:** `75-usb-serial.sh`. The module part is Jetson only; the step skips it cleanly on the VM.

### 3d: CUDA and llama.cpp

1. **CUDA debs** come from NVIDIA's `https://repo.download.nvidia.com/jetson/common r39.2 main`.
   - The signing key is `jetson-ota-public.asc`, fingerprint `3C6D1FF3100C8C3ABB0869C0E6543461A9996195`.
   - Pins: `cuda-cudart-13-2=13.2.86-1`, `libcublas-13-2=13.4.1.3-1`. For building only: `cuda-nvcc-13-2=13.2.86-1`, `cuda-cudart-dev-13-2`, `libcublas-dev-13-2`.
   - The user-space libraries are NVIDIA's SBSA build, re-hosted in that repo. `libcuda` comes from L4T (`nvidia-l4t-cuda-nvgpu`); never depend on `libcuda.so.1`, or apt may pull in the dGPU `libnvidia-compute`.
2. **llama.cpp v0.7.0**: commit `620d2225cc436ab0b001e3200e590e3b286ef9ab`, tarball sha256 `55d67acf3ad2ad747a9bbeb75f9f0e173dbcc0d1753a471038a90c819421c5e1`.
   - CMake flags: `GGML_BACKEND_DL=ON GGML_CPU_ALL_VARIANTS=ON GGML_NATIVE=OFF BUILD_SHARED_LIBS=ON`, with the install prefix `/opt/brainsyncos/llama` and gcc-14 (gcc-13 fails on the SME variants).
   - CUDA: `GGML_CUDA=ON CMAKE_CUDA_ARCHITECTURES=87-real GGML_CUDA_NO_VMM=ON`.
   - Packages: `brainsyncos-llama` (both editions) and `brainsyncos-llama-cuda` (Jetson).
3. **`brainsyncos-llama.service`**: `llama-server` on `127.0.0.1:9931`.
   - API key from `LoadCredential`.
   - `IPAddressDeny=any`, with `IPAddressAllow=localhost`.
   - Sandboxing and low priority (`Nice=10`, `CPUWeight=50`); memory limits measured on the device.
   - Do not enable `--tools`, `--mcp-*` or router mode.
4. **The model** ships as its own package, pinned by Hugging Face revision and sha256.
5. **BCI Voice**: `llm.backends` in `config/default.yaml`, tried in order: local, then Claude, then the fixed lists.
   - The 4 s timeout will need raising for local inference.
   - llama-server ignores `strict`, so keep the pydantic validation.
6. **Install steps:** `80-cuda.sh` (Jetson only) and `85-llama.sh`.

### 3e: integration

- Build both ISOs end to end and run the suites.
- Do a VM install in UTM, on a Mac.
- Watch the ISO size: the Jetson ISO goes from about 5.0 GB to about 7 GB with CUDA and the 2B model. The build should fail above about 7.8 GB, the limit for an 8 GB stick.
- Write `docs/jetson-acceptance.md`, a checklist for the first Orin:
  - the CUDA backend loads (`nvgpu` libcuda);
  - `llama-bench` tok/s;
  - peak memory;
  - CH340 or CDC-ACM device appears and BrainFlow streams;
  - SSVEP flicker timing while the LLM generates.

## Moving to an x86-64 laptop

The builder is an **arm64** container on purpose: it unpacks and chroots into
the arm64 target filesystems.
- **On Apple Silicon** it runs natively.
- **On x86-64** Docker runs it through qemu-user emulation. That works but is slower: a few times slower for the ISO steps, and much slower for compiling.

### Setup (Linux x86-64; Windows: use WSL2 with Docker Desktop)

```sh
git clone https://github.com/1thing2say/BrainSync && cd BrainSync
git checkout custom-operating-brain-seperate
cd brainsyncos

# arm64 emulation for Docker (once per boot on plain Docker; Docker Desktop has it built in)
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker run --rm --platform linux/arm64 ubuntu:24.04 uname -m    # aarch64

# Inputs that are not in git
cp /path/to/jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso ~/Downloads/   # 5.0 GB, sha256 pinned in config/base-iso.env
cp ~/.ssh/id_ed25519.pub config/authorized_keys      # logins for the brainsync account
scripts/make-password-hash.sh                        # optional console password
scripts/build.sh key                                 # the repository signing key; back it up

scripts/build.sh doctor
scripts/build.sh test phase3                         # about 5 min on the Mac; slower under emulation
scripts/build.sh all                                 # Jetson ISO
scripts/build.sh all --edition vm                    # VM ISO (downloads Ubuntu's 3.6 GB ISO)
```

**Caveats on x86-64:**
- **WSL: clone under `~`, never on `/mnt/c`.** Windows drives ignore `chmod`, so the repository key would be readable by everyone. `make-repo-key.sh` refuses to write it there and `doctor` reports `file modes` as missing. The Linux filesystem is also much faster for the 4 GiB extractions.
- **`make`** is not installed on a fresh Ubuntu or WSL (`sudo apt install make`). `scripts/build.sh` works without it; `tests/test_phase3.sh` checks the Makefile and fails without it.
- **Rootful Docker on Linux** makes `build/` root-owned; see the README's host requirements. Docker Desktop and rootless Docker are fine.
- **Disk.** Keep about 20 GB free:
  - the Jetson ISO build needs about 7 GiB;
  - the Phase 2 test ISO another 5 GB;
  - the VM base ISO 3.6 GB.
- **CPU.** Under emulation, raise the cap if the laptop has the cores, e.g. `BSOS_BUILD_CPUS=6 BSOS_BUILD_JOBS=6 scripts/build.sh ...`.
- **`scripts/utm-vm.sh` and `scripts/write-usb.sh` are macOS-only** (UTM, `diskutil`).
  - On Linux, write the ISO with `dd` or balenaEtcher.
  - Run the VM edition with `qemu-system-aarch64`. It works but is slow under TCG emulation. `tests/boot-smoke.py` already boots the VM ISO to the kernel and initramfs this way.
- **Compiling for 3b to 3d under qemu-user will be slow.** That covers llama.cpp (about 25 min at 2 jobs natively on the M4), BrainFlow, the ch341 module, and `nvcc` for sm_87. Options, best first:
  1. A native arm64 builder: an arm64 cloud VM, an Apple Silicon Mac, or the Orin itself. The same `scripts/builder.sh` works there unchanged.
  2. Cross-compile from x86-64 with `aarch64-linux-gnu` toolchains. The CUDA SBSA cross packages exist, but this is more work and is not in the plan yet.
  3. Accept emulation for the small builds (keyring, venv assembly from prebuilt wheels, ch341) and do only llama.cpp natively.

**Not in git, and it stays that way:**
- `config/authorized_keys`, `config/brainsync-password.hash` and `config/repo-signing-key*.asc` (gitignored);
- `build/` (base ISOs, downloads, outputs).

Use one repository signing key for every ISO you build. If you build on both
machines, copy `config/repo-signing-key.asc` securely instead of making a
second key: a device only trusts the key its ISO came with.

## Test status (2026-10-24, on the Mac, 2-CPU cap)

- `tests/test_phase3.sh`: **all 10 checks passed**: configs, builder caps, `build.sh`, key, packages, repo, apt helper, md5 maps, install hook, shellcheck.
- `tests/test_phase2.sh`: **all 16 checks passed**. That includes the test ISO with the repository and the full 7-step hook in NVIDIA's target layer.
- `tests/test_vm.sh`: **all 19 checks passed** (re-run after the `Dpkg::Use-Pty=0` fix).
  - That includes the test ISO with the vm repository and the full 7-step hook in Ubuntu's target layers.
  - It also includes the QEMU boot smoke test.
  - The earlier run's only failure, apt's `/dev/pts` log line, is gone. The Phase 3 hook check now fails on any `E:`/`W:` line.
- shellcheck v0.10.0 (`-x -S warning`) is clean over all BrainSyncOS scripts.

## Open items

- **Confirm on the new machine:** run `tests/test_phase3.sh` once under x86-64 emulation.
- **Review of 3a not finished.** A three-lens review (correctness, key handling and apt trust, install-time robustness on the real installers) was running when this was pushed; its results are not in this commit.
- **Not exercised yet:** a real UTM install of a 3a ISO, i.e. the repository bind mount under real subiquity/curtin. The tests use a chroot shim for curtin.
- **Clock risk:** the apt helper accepts a clock in the past (tested at 2001), and a clock past the key's expiry fails. That is correct, but a Jetson whose RTC jumps far ahead would fail the install.
- **Also on this branch:** a separate commit with another session's uncommitted app changes, which moved spoken prompts to Cartesia TTS (`brainsync/voice.py`, `tests/test_voice.py` and edits in `brainsync/` and `tests/`). They were pushed as found; they are not part of BrainSyncOS and were not reviewed or tested here.
