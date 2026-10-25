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
   - The BrainFlow fork's wheel from 3c (`build/wheels/`, checked against its `SHA256SUMS`) goes into the venv: `python/local-wheels.txt` lists `brainflow`. The build fails if a listed wheel is missing; `scripts/build.sh all` builds the wheels first (skipped while up to date).
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

### 3c: the board. **Done 2026-10-24** (branch `bsos/3c-board`)

The pins below are the ones the research found; the README section "The package repository (Phase 3)" describes the result.

1. **The BrainFlow fork as a wheel**, `scripts/build.sh wheels` (`scripts/build-wheels.sh`, recipe in `wheels/brainflow-cerelog/`).
   - Source: `https://github.com/shakimiansky/Shared_brainflow-cerelog`, commit `932b3944bfe6c0f4e29a46b4a34afdeba39edd85`, tree `f31dd196…` (checked). Fetched without credentials as a sparse partial clone, cached in `build/downloads/brainflow-cerelog.git` (40 MB checked out); a rebuild works offline.
   - The patch moved to `wheels/brainflow-cerelog/patches/0001-cerelog-connect_v1-raw-timeout.patch`, pinned by sha256 (`eace7b03…`, the same file as before). It is still the only copy outside the Mac.
   - CMake as in the research: `BUILD_SYNCHRONI_SDK=OFF BUILD_BLE=OFF BUILD_BLUETOOTH=OFF BUILD_ONNX=OFF BUILD_TESTS=OFF`, `BRAINFLOW_VERSION=5.0.0`, targets `BoardController DataHandler MLModule`. The fork has no release version; 5.0.0 is the research's label.
   - The wheel is `brainflow-5.0.0+cerelog932b394.1-py3-none-linux_aarch64.whl`: `brainflow/*.py` plus the three stripped libraries, `Requires-Dist: numpy, setuptools<81`. `scripts/pack-wheel.py` writes it reproducibly (sorted entries, the fork commit's time).
   - The build checks the libraries (aarch64, only the C/C++ runtime needed), then installs the wheel with pip into a fresh venv and requires `get_sampling_rate(CERELOG_X8_BOARD)` = 250 before anything lands in `build/wheels/`.
   - **For 3b (`brainsync-runtime`):** take the wheel from `build/wheels/`. `brainflow-cerelog.env` is the manifest, plain KEY=value for `load_env_file`: `WHEEL_FILE`, `WHEEL_SHA256`, `WHEEL_SIZE`, `WHEEL_DIST`, `WHEEL_VERSION`, `WHEEL_TAG`, `INPUTS_SHA256`, `BUILD_REV`, `SOURCE_*`, `PATCHES`, `CMAKE_*`, `TOOLCHAIN`, `VERIFIED_SAMPLING_RATE`. Install it with `pip install --no-index --find-links build/wheels --require-hashes --no-deps -r build/wheels/requirements.txt`. The venv's lock already has `numpy` and `setuptools==80.10.2`. Never let upstream `brainflow` into the venv: same import name, board ID 65 means another board there.
   - `wheels` is not part of `scripts/build.sh all` yet, since nothing on the ISO uses it. When `brainsync-runtime` exists, `all` (or that package's build) should run it first.
   - The builder image now has `git`, `cmake`, `g++`, `make`, `patch`, `kmod`, `udev`, `python3-venv`, `python3-numpy` and `python3-setuptools`.
2. **`brainsyncos-ch341-tegra`** (Jetson only; `packages/brainsyncos-ch341-tegra/`, with `ch341.c` and `Kbuild` moved there).
   - Built against `nvidia-l4t-kernel-headers` from the base ISO's pool. The build checks: the headers are for `6.8.12-1021-tegra`, `CONFIG_USB_SERIAL_CH341` is unset and `CONFIG_USB_SERIAL=m`, NVIDIA's `nvidia-l4t-kernel` has `usbserial.ko` and no ch341, and the vermagic equals NVIDIA's `cp210x.ko`. The `.ko` is stripped of debug info (18 KB).
   - Installs `/lib/modules/6.8.12-1021-tegra/updates/ch341.ko`; postinst and postrm run `depmod -a 6.8.12-1021-tegra`. `Depends: nvidia-l4t-kernel (= 6.8.12-tegra-39.2.1-20260806224157), kmod`. The source ships in `/usr/share/doc/brainsyncos-ch341-tegra/` (GPL-2.0).
   - NVIDIA's autoinstall installs `nvidia-l4t-kernel` in late-command Step 5 (`ai/altkernel-pkgs`), before our hook, so the exact Depends is met offline.
   - **Unsigned.** It loads only while module signature enforcement is off, as on stock JetPack.
3. **`brainsync-udev`** (both editions): `/usr/lib/udev/rules.d/70-brainsync-cerelog.rules`, tty devices of vendors `1a86` and `303a` get `GROUP=dialout`, `MODE=0660`, `SYMLINK+=cerelog` and `ENV{ID_MM_DEVICE_IGNORE}=1`. The postinst reloads udev and replays tty events only outside a chroot and with udevd running. `brainsync/board.py` still finds the board by `/dev/ttyUSB*` and `/dev/ttyACM*`; `/dev/cerelog` is for people, so the app is unchanged.
4. **Install step `75-usb-serial.sh`:** both packages on a Jetson edition, `brainsync-udev` only on `vm-*`. It then checks that `modprobe -S 6.8.12-1021-tegra --show-depends ch341` resolves to `updates/ch341.ko` after `usbserial.ko`.

**Verified** (x86-64 WSL2, arm64 builder under qemu): see "Test status of 3c" below.
- The wheel build: the source fetch, the patch, CMake and the three libraries, the venv check (250 Hz, versions 5.0.0). The compile took 47 minutes at 3 jobs under emulation (`BSOS_BUILD_CPUS=3 BSOS_BUILD_JOBS=3`), with no compiler warnings. The stripped libraries have exactly the sizes of the research build on the M4: 1,972,472, 790,320 and 265,624 bytes. The wheel is 1,196,414 bytes, sha256 `6ed6d54c…`.
- `ch341.ko` against the ISO's own kernel: same vermagic as `cp210x.ko`; all its modversion CRCs match the headers' `Module.symvers`, and match the CRCs NVIDIA's 1352 shipped modules import for the same symbols, so the headers fit the kernel that ships. `depmod` with NVIDIA's modules resolves it to `usbserial.ko`.
- The install in NVIDIA's real target layer with `nvidia-l4t-kernel` from the pool (without its maintainer scripts): packages installed, modules.dep and modules.alias updated, re-run works, purge removes it from modules.dep.

**Not verified** (no hardware here): the module loading on an Orin; a CH340 or ESP32-S3 board appearing as `/dev/cerelog`; BrainFlow streaming from it; ModemManager really leaving the port alone; the udev postinst on a running system. Note: noble's `depmod -E` misreads Ubuntu's variable-length `__versions` records and warns about every module, NVIDIA's included; the CRCs were compared with a small parser instead.

**Open decisions:**
- Do you have V1 (CH340) boards, V2 (ESP32-S3), or both? With V2 only, `brainsyncos-ch341-tegra` could go.
- Secure Boot on the Jetson would need a module signing key and MOK enrollment for `ch341.ko`.
- Whether to hold `nvidia-l4t-kernel` on the device: a kernel update from NVIDIA's online repository now stops at the exact Depends instead of silently dropping V1 support.
- Push the patch upstream to `shakimiansky/Shared_brainflow-cerelog`, or mirror the fork: a force-push there breaks fresh builds without the cache.
- When `wheels` joins `all` (see 1.).

### 3d: CUDA and llama.cpp

1. **CUDA debs** come from NVIDIA's `https://repo.download.nvidia.com/jetson/common r39.2 main`.
   - The signing key is `jetson-ota-public.asc`, fingerprint `3C6D1FF3100C8C3ABB0869C0E6543461A9996195`.
   - Pins: `cuda-cudart-13-2=13.2.86-1`, `libcublas-13-2=13.4.1.3-1`. For building only: `cuda-nvcc-13-2=13.2.86-1`, `cuda-cudart-dev-13-2`, `libcublas-dev-13-2`.
   - The user-space libraries are NVIDIA's SBSA build, re-hosted in that repo. `libcuda` comes from L4T (`nvidia-l4t-cuda-nvgpu`); never depend on `libcuda.so.1`, or apt may pull in the dGPU `libnvidia-compute`.

   **Part 1, the CUDA runtime on the ISO: done 2026-10-25** (branch `bsos/3d-cuda`).
   - **Mirrored, not vendored.** The ISO is for our own devices, so NVIDIA's debs go into the Jetson repository unchanged, with their EULA in `/usr/share/doc`. This settles contradiction A3 of the research. llama.cpp's CUDA package (part 2) should depend on `cuda-cudart-13-2 (= 13.2.86-1), libcublas-13-2 (= 13.4.1.3-1)`, never on `libcuda.so.1`.
   - **The mechanism:** `mirror/nvidia-jetson/`. `mirror.env` names the repository, the key fingerprint, the roots and the names nothing may be, provide or depend on (`MIRROR_FORBID`: `libnvidia-compute`, `nvidia-driver-*`, `cuda-drivers`, `cuda-compat`, `libcuda*`). `jetson-ota-public.asc` is the key, and `debs.lock` pins each .deb by size and sha256.
   - `scripts/fetch-mirror.sh` (`scripts/build.sh mirror`, also run by `all`) caches the debs in `build/downloads/mirror/nvidia-jetson/`. `scripts/mirror.py` does the work: gpgv against that key alone, the index against the signed InRelease, the closure from the signed index, and the size and sha256 of each deb. A filled cache works offline. `--lock` re-resolves the closure and rewrites `debs.lock`.
   - `scripts/build-repo.sh` copies the pinned debs into the Jetson repository on every build and checks them in the pool again. The VM repository gets none.
   - **The closure is 5 debs:** `cuda-cudart-13-2`, `libcublas-13-2` and the three `cuda-toolkit*-config-common` 13.2.86-1. Nothing outside NVIDIA's repo is needed. They are 483,944,278 bytes to download and 676 MB installed. libcublas holds `libcublas`, `libcublasLt` (607 MB) and `libnvblas`.
   - **No ld.so.conf package of ours is needed.** `cuda-toolkit-config-common` and `cuda-toolkit-13-config-common` ship `/etc/ld.so.conf.d/000_cuda.conf` and `987_cuda-13.conf`, which point at `/usr/local/cuda{,-13}/targets/sbsa-linux/lib`. The postinst of `cuda-toolkit-13-2-config-common` points those at `/usr/local/cuda-13.2` with `update-alternatives`, then runs `ldconfig`. cudart and cublas have no maintainer scripts.
   - **`80-cuda.sh`** (Jetson only; prints `vm-arm64 edition: skipped` on the VM) installs `cuda-cudart-13-2` and `libcublas-13-2` through `lib/apt-repo.sh`. It then checks four things: ldconfig resolves `libcudart.so.13`, `libcublas.so.13` and `libcublasLt.so.13` under `/usr/local/cuda-13.2`; `ldd` finds all their libraries; `/usr/local/cuda` points at 13.2; and no dGPU driver package is installed. It runs no CUDA code, because libcuda is only set up at boot by `nv-load-gpu-libs.service`.
   - **ISO size:** the repository grows by 0.48 GB, so the Jetson ISO is about 5.52 GB (5.04 + 0.48) plus the apps (about 0.08 GB), before llama.cpp and the model.

   **Verified** (x86-64 WSL2, arm64 builder under qemu, 1 CPU):
   - `fetch-mirror.sh --lock` against NVIDIA's live repository (InRelease of 23 Oct 2026): signature good, closure of 5, no external relations. The committed key is the one `nvidia-l4t-apt-source` in the base ISO's pool ships, and its sources list names that repository and suite.
   - The new phase 3 check `cuda`, run on its own against a test repository built by the real `build-repo.sh` (keyring plus mirror):
     - the cache verifies with no network (`unshare -n`);
     - the repository index lists every pinned deb at its pinned sha256, and the VM repository has none;
     - it refuses a tampered InRelease, another key, a stale lock, a `libnvidia-compute` root and a tampered deb, and `build-repo.sh` refuses a tampered cache;
     - in NVIDIA's real target layer, the hook runs `60-apt-repo.sh` and `80-cuda.sh`. Exactly the 5 debs get installed, their postinsts ran in the chroot, ldconfig resolves the three libraries, nothing provides `libcuda.so`, the target's apt state is unchanged, a re-run installs nothing new, and the VM edition skips.
   - shellcheck is clean.

   **Not done or not verified:**
   - CUDA code on an Orin: whether nvgpu's libcuda loads with this cudart (3e acceptance).
   - The full `tests/test_phase3.sh`, `test_phase2.sh` and `test_vm.sh`. The last two now expect 10 hook steps, and phase 2 checks the CUDA libraries.
   - An install in UTM or on a Jetson.
   - The licence (EULA Attachment A) assumes the ISO stays on our own devices.
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
5. **BCI Voice** (done 2026-10-24, branch `bsos/3d-llm-backends`): `llm.backends` in `config/default.yaml`, tried in order: local, then Claude, then the fixed lists.
   - `local`: `kind: llama`, `http://127.0.0.1:9931`, key from `BRAINSYNC_LLAMA_API_KEY`, 10 s, `chat_template_kwargs: {enable_thinking: false}`. Without that key the backend is off, so a laptop runs Claude alone as before.
   - Each backend has its own timeout. A refused connection or a bad key hands over at once; a page takes at most the sum (14 s).
   - The pydantic checks run for every backend, since llama-server ignores `strict`.
   - A Claude model that rejects a forced `tool_choice` is a config error.
   - Status, logs and preflight name the backend; preflight checks `/health` and `/v1/models` on the local server.
   - Verified with fakes only: a stand-in HTTP server that answers `/v1/messages` the way llama-server does, driven through the real SDK, and a closed port. No real llama-server or model was run.
   - Still to do: the install step (`85-llama.sh`) must also write the key in llama-server's `--api-key-file` to `/etc/brainsync/secrets.env` as `BRAINSYNC_LLAMA_API_KEY`, and the 10 s needs measuring on the Orin (3e).
6. **Install steps:** `80-cuda.sh` (Jetson only; done, see 1) and `85-llama.sh`.

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

## Test status of 3c (2026-10-25, x86-64 WSL2, arm64 builder under qemu, 2-CPU cap)

- `tests/test_phase3.sh`: **all 13 checks passed**, including the new `board-debs`, `usb-serial` and `wheel`.
- `tests/test_phase2.sh`: **all 16 checks passed**, with the 8-step hook (now including `75-usb-serial.sh` and `nvidia-l4t-kernel` from the pool) in NVIDIA's target layer. `boot-compare` needs `build/meta` from `scripts/extract-iso.sh` first; on a fresh checkout it fails until then.
- `tests/test_vm.sh`: **not completed on this host.** It stops before the VM ISO build: `verify-base` needs `xorriso` or `bsdtar` on the host (WSL has neither), and `utm-stubbed` needs macOS property lists. So the VM hook check with `75-usb-serial.sh` (expecting 8 steps and the udev rules only) has not run. The VM path of the step itself is covered by `usb-serial` in `test_phase3.sh`. Run `test_vm.sh` on the Mac.
- shellcheck v0.10.0 (`-x -S warning`) is clean over all scripts.

## Test status (2026-10-24, on the Mac, 2-CPU cap)

- `tests/test_phase3.sh`: **all 10 checks passed**: configs, builder caps, `build.sh`, key, packages, repo, apt helper, md5 maps, install hook, shellcheck.
- `tests/test_phase2.sh`: **all 16 checks passed**. That includes the test ISO with the repository and the full 7-step hook in NVIDIA's target layer.
- `tests/test_vm.sh`: **all 19 checks passed** (re-run after the `Dpkg::Use-Pty=0` fix).
  - That includes the test ISO with the vm repository and the full 7-step hook in Ubuntu's target layers.
  - It also includes the QEMU boot smoke test.
  - The earlier run's only failure, apt's `/dev/pts` log line, is gone. The Phase 3 hook check now fails on any `E:`/`W:` line.
- shellcheck v0.10.0 (`-x -S warning`) is clean over all BrainSyncOS scripts.

## Open items

- **Confirmed on x86-64 (2026-10-24):** `tests/test_phase3.sh` passes all 10 checks on a WSL2 laptop with Docker Desktop, under arm64 emulation, from a clone under `~`. It needed two fixes: `builder.sh` hands root-owned output back to the user, and the key-mode check reads GNU `stat` first. From `/mnt/c` the key check fails by design (Windows drives ignore `chmod`).
- **Review of 3a not finished.** A three-lens review (correctness, key handling and apt trust, install-time robustness on the real installers) was running when this was pushed; its results are not in this commit.
- **Not exercised yet:** a real UTM install of a 3a ISO, i.e. the repository bind mount under real subiquity/curtin. The tests use a chroot shim for curtin.
- **Clock risk:** the apt helper accepts a clock in the past (tested at 2001), and a clock past the key's expiry fails. That is correct, but a Jetson whose RTC jumps far ahead would fail the install.
- **Also on this branch:** a separate commit with another session's uncommitted app changes, which moved spoken prompts to Cartesia TTS (`brainsync/voice.py`, `tests/test_voice.py` and edits in `brainsync/` and `tests/`). They were pushed as found; they are not part of BrainSyncOS and were not reviewed or tested here.
