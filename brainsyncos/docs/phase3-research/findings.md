# Phase 3 research findings (2026-10-24)

Raw results of the research that the Phase 3 plan is based on: six research agents and a completeness critic, run on the M4 Mac before any Phase 3 code was written. They are kept so the work can continue on another machine; see [../phase3-notes.md](../phase3-notes.md) for the plan and status.

`<scratch>` paths pointed at a temporary directory on that Mac. The useful artifacts from it are in this folder (`brainflow/`, `python/`, `ch341/`, `cuda/`, `llama/`). Everything else there was deleted. Versions and sizes were current on 2026-10-24.

## CUDA on the Jetson edition

## CUDA on the BrainSyncOS Jetson edition (JetPack 7.2.1 / L4T R39.2.1, Orin Nano)

Scratch: <scratch>/phase3/cuda (indexes in `idx/`, debs in `dl/`), helper scripts in <scratch>/phase3/cuda-tools. The repo and the ISO were only read. Every container I ran was `--rm --cpus=2 --memory=4g`, one at a time, and none are still running. The container that is still running (`dec4633195a2`, `build-test.sh`, `phase3/llama` mounts) belongs to a sibling agent. Total download was about 0.4 GB.

### 1. Where the repos are (verified)
- I extracted `nvidia-l4t-apt-source_39.2.1-20260806224157_arm64.deb` from the ISO pool with `7z x` and read it with `dpkg-deb -R`. Its `/etc/apt/sources.list.d/nvidia-l4t-apt-source.list` contains:
  ```
  deb https://repo.download.nvidia.com/jetson/common r39.2 main
  deb https://repo.download.nvidia.com/jetson/som r39.2 main
  deb https://repo.download.nvidia.com/jetson/ffmpeg r39.2 main
  ```
  The key is `/etc/apt/trusted.gpg.d/jetson-ota-public.asc`, fingerprint `3C6D1FF3100C8C3ABB0869C0E6543461A9996195`. It is the same key served at `https://repo.download.nvidia.com/jetson/jetson-ota-public.asc`. The deb also pins `Pin: origin "*.nvidia.com"` at priority 600 (`preferences.d/nvidia-repo-pin`). Its postinst only substitutes `<SOC>` (t234 or t264); the R39.2 list has no `<SOC>` in it.
- `nvidia-l4t-bsp` on the ISO depends on `nvidia-l4t-apt-source`, `-cuda`, `-cuda-nvgpu`, `-cuda-openrm` and `-cuda-utils`. A stock install therefore already has the network repo configured and libcuda installed. There is **no developer.download.nvidia.com entry**.
- The ISO pool has **no** `cuda-*`, `libcublas*`, `libcudnn*`, `libnvinfer*` or `nvidia-jetpack` packages (grep of `dists/noble/main/binary-arm64/Packages`, 2190 entries).
- NVIDIA's installer can also turn on the network repo itself: `jetsoniso_setup.bash` reads the kernel arguments `codename=r39.2`, `repo-url=` and `repo-key=`, and the Orin autoinstall then adds `common` and `som` with `signed-by` (`build/tmp/nvlayer/ai/jetsoniso_orin-ai.yaml` lines 523-535). This is off by default.

### 2. Package names, versions and sizes (from the `jetson/common r39.2` binary-arm64 Packages index, Release dated 23 Oct 2026, 413 entries)
| package | version | download | installed |
|---|---|---|---|
| nvidia-jetpack / -runtime / -dev (metapackages) | 7.2.1-b49 (also 7.2-b187, 7.2-b184 in the same suite) | - | - |
| nvidia-cuda (meta) | depends cuda-cudart-dev-13-2 (=13.2.86-1), cuda-driver-dev-13-2, nvidia-l4t-cudadebuggingsupport | | |
| nvidia-cuda-dev | depends cuda-toolkit-13-2 (=13.2.2-1) | | |
| cuda-toolkit-13-2 | 13.2.2-1 (meta) | | |
| cuda-cudart-13-2 | 13.2.86-1 | 0.18 MB | 0.84 MB |
| libcublas-13-2 | 13.4.1.3-1 | 483.7 MB | 659.5 MB |
| libcublas-dev-13-2 | 13.4.1.3-1 | 537.8 MB | 984.7 MB |
| cuda-nvcc-13-2 | 13.2.86-1 | 26.7 MB | 120 MB |
| libnvvm-13-2 | 13.2.86-1 | 40.2 MB | 130.6 MB |
| cuda-nvrtc-13-2 | 13.2.86-1 | 29.7 MB | 106.8 MB |
| libcudnn9-cuda-13 (via nvidia-cudnn, cudnn 9.20.0) | 9.20.0.46-1 | 403 MB | 577 MB |
| libnvinfer10 (TensorRT, via tensorrt / nvidia-tensorrt) | 10.16.2.10-1+cuda13.2 | 2313 MB | 2998 MB |
| cuda-compat-orin-13-2 | 13.2.44429113-1 | 82 MB | 445 MB |

Dependency closures, NVIDIA packages only (computed with `cuda-tools/closure.py` from the index; approximate):
- **llama.cpp runtime (cudart + cublas/cublasLt + 3 config-common):** 5 packages, **0.48 GB download / 0.66 GB installed**.
- llama.cpp build set (nvcc + cudart-dev + cublas-dev): 14 packages, 1.11 GB / 2.01 GB, plus build-essential.
- cuda-runtime-13-2: 1.25 GB / 1.94 GB.
- cuda-toolkit-13-2: 63 packages, **3.28 GB / 5.62 GB**.
- cuDNN runtime: 0.40 / 0.58 GB.
- TensorRT runtime: 2.35 / 3.23 GB.
- nvidia-jetpack-runtime: about 3.2 / 5.0 GB.
- **Full nvidia-jetpack: about 9.5 GB download / 15.7 GB installed.**

### (a) CUDA comes from the SBSA build, mirrored into the Jetson repo (verified)
- The same versions exist in `developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa`: cudart 13.2.86-1, cublas 13.4.1.3-1, nvcc 13.2.86-1, toolkit 13.2.2-1, and cuda-compat-orin too. Each Jetson deb is 4 bytes larger and has a different SHA256.
- I downloaded `cuda-cudart-13-2_13.2.86-1` from both repos. `dpkg-deb -R` plus `diff -r` show **identical trees**: files under `/usr/local/cuda-13.2/targets/sbsa-linux/lib`, identical control files. So the CUDA user-space is the SBSA build, re-hosted (and re-wrapped) in `jetson/common r39.2`.
- cuDNN and TensorRT are Jetson-pinned and older than SBSA's: common has cudnn 9.20.0.46 while SBSA has up to 9.27; common has TensorRT 10.16.2.10 while SBSA has 10.16.1.11.
- Recommendation: use the Jetson repo, because it is the set `nvidia-jetpack 7.2.1` pins.

### (b) libcuda comes from L4T, not from CUDA (verified)
- `libcuda.so.1.1` ships in `nvidia-l4t-cuda-nvgpu` (Orin) and `nvidia-l4t-cuda-openrm` (Thor), under `/opt/nvidia/l4t-gpu-libs/{nvgpu,openrm}/`, about 91 MB each.
- `nvidia-l4t-cuda` itself only holds libnvcucompat, libnvcudla and libnvcuextend.
- At boot, `nv-load-gpu-libs.service` (from nvidia-l4t-init, `/etc/systemd/nv-load-gpu-libs.sh`) picks the variant. It writes `/etc/ld.so.conf.d/000-nvidia-gpu-libs.conf`, symlinks `/usr/lib/aarch64-linux-gnu/libcuda.so{,.1}`, and writes the nvidia-container `l4t.csv`.
- Inferred: Orin selects nvgpu. I did not read the choose-variant helper.
- Caution: `jetson/common` also carries the dGPU `libnvidia-compute 595.71.05`, which `Provides: libcuda.so.1`. Never let apt pick it on the Jetson.

### (c) Minimal runtime for a CUDA llama.cpp
- Needed: `cuda-cudart-13-2` + `libcublas-13-2` (which holds libcublas and libcublasLt; inferred from standard CUDA packaging, I did not list the 484 MB deb), plus libcuda from L4T, which is already installed.
- Size: 0.48 GB download / 0.66 GB installed, against 3.28 / 5.62 GB for the full toolkit.
- The JetPack `nvidia-cuda` meta does **not** pull cuBLAS; only `cuda-toolkit` / `cuda-libraries` do.
- Libraries land in `/usr/local/cuda-13.2/targets/sbsa-linux/lib`. The config-common debs add `/etc/ld.so.conf.d/000_cuda.conf` and `987_cuda-13.conf` and set up the `/usr/local/cuda` alternatives (postinst read).

### (d) The CUDA toolkit works in a GPU-less arm64 Docker builder (verified)
- Setup: `ubuntu:24.04` arm64 with `deb [signed-by=jetson.gpg] https://repo.download.nvidia.com/jetson/common r39.2 main`, then `apt-get install --no-install-recommends cuda-nvcc-13-2 cuda-cudart-dev-13-2 cuda-cuobjdump-13-2`. apt checked InRelease against the OTA key.
- This installed 13 CUDA packages plus build-essential. `/usr/local/cuda-13.2` is 365 MB.
- `nvcc --version` reports 13.2, V13.2.86.
- `--list-gpu-arch` reports `compute_75 80 86 87 88 89 90 100 110 103 120 121`, so **sm_87 is supported**.
- `nvcc -arch=sm_87` built a test program, and `cuobjdump --list-elf` shows `sm_87.cubin` (plus PTX with `-gencode ...code=[sm_87,compute_87]`).
- The SBSA repo would work too, since the payload is identical.
- Not verified: building llama.cpp itself, and linking against cuBLAS (I skipped the 1 GB cublas download).
- Inferred: the `cuda-driver-dev-13-2` stubs (`lib/stubs/libcuda.so`) cover linking against libcuda without a GPU.

### (e) Redistribution terms (read from `cuda-cudart-13-2` `/usr/share/doc/.../copyright`; not legal advice)
- §1.1.1(3) and Attachment A allow distributing **libcudart.so, libcudart_static.a, libcublas.so, libcublasLt.so and libcublas_static.a / libcublasLt_static.a** "as incorporated in object code format into a software application".
- §1.1.2 sets conditions:
  - (1) your application must have material additional functionality;
  - (2) "The distributable portions of the SDK shall only be accessed by your application";
  - (4) developer tools are for internal use only, so **nvcc, nvvm and the toolkit must not go on the ISO**;
  - (5) your terms must be consistent with the EULA, and the files must be unmodified (§2.3).
- The L4T packages are under the "NVIDIA Driver License", which allows redistribution of unmodified binaries for OSI-licensed kernels with the license included. That is how NVIDIA's own ISO ships them.
- cuDNN and TensorRT have their own SLAs, which I did not read. llama.cpp does not need them.
- Condition (2) favours a **private copy** inside our llama.cpp package (RPATH), or static linking, over dropping NVIDIA's debs into the system-wide `/usr/local/cuda`.

### (f) Options
1. **Bundle offline on the ISO.**
   - Cost: +0.48 GB for runtime only; the ISO is 5.04 GB today.
   - Pros: deterministic and works with no network.
   - Licence: OK for cudart/cublas if they are private to the app.
   - The full toolkit (+3.3 GB, and not redistributable) is ruled out.
2. **apt from NVIDIA's repo during install (curtin in-target).**
   - Pros: no licence questions.
   - Cons: needs network in the installer. `r39.2` floats (three JetPack builds and two cudart and cublas versions sit in the same suite), so it is not reproducible. An offline `apt-get update` fails with an error, which would abort the installer, because hook.sh treats any failure as fatal.
3. **First boot.** Same network and reproducibility problems, and the device is half-ready until the download finishes. Its only advantage is a faster install.

**Recommendation:** use option 1 for the runtime and keep the network for opt-in extras.
- Build llama.cpp for sm_87 in a separate CUDA builder image.
- Ship it as our own `brainsync-llama-cpp-cuda` deb in `/brainsyncos/repo`. Vendor unmodified `libcudart.so.13`, `libcublas.so.13` and `libcublasLt.so.13` into e.g. `/opt/brainsyncos/llama.cpp/lib` with RPATH, and include the EULA. Alternatively link them statically with `GGML_STATIC`; that option is unverified.
- libcuda comes from the L4T packages that are already installed.
- Leave the full toolkit, cuDNN and TensorRT to a documented opt-in `apt install nvidia-jetpack` (or `cuda-toolkit-13-2`) over the NVIDIA repo the stock install already configures.

**Recommendations**

- Add a separate CUDA builder image, e.g. docker/Dockerfile.cuda-builder, so the existing builder stays small. Base it on ubuntu:24.04 arm64 with the jetson/common r39.2 source (signed-by the OTA key, fingerprint 3C6D1FF3100C8C3ABB0869C0E6543461A9996195; the ISO's nvidia-l4t-apt-source holds the same key). Pin cuda-nvcc-13-2=13.2.86-1, cuda-cudart-dev-13-2=13.2.86-1 and libcublas-dev-13-2=13.4.1.3-1. Expect about 1.1 GB download and 2 GB installed.
- Build llama.cpp with -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 and 2 jobs, and package it as brainsync-llama-cpp-cuda (arm64, Jetson only). Vendor unmodified libcudart.so.13, libcublas.so.13 and libcublasLt.so.13 under /opt/brainsyncos/llama.cpp/lib with RPATH $ORIGIN/../lib, and install the CUDA EULA under /usr/share/doc. Do not depend on system /usr/local/cuda. Evaluate GGML_STATIC (cudart_static plus cublas_static) as an alternative.
- Do not ship nvcc, nvvm, cuda-toolkit, cuDNN or TensorRT debs on the ISO. nvcc is not redistributable, the toolkit is 3.3 GB and TensorRT is 2.3 GB, and the total would push the ISO past an 8 GB USB stick.
- Do not ship libcuda. Have the package depend on nvidia-l4t-cuda (>= 39.2), which stock installs already have through nvidia-l4t-bsp, and never on libcuda.so.1. That keeps apt from choosing the dGPU libnvidia-compute 595.x from jetson/common.
- Write a Jetson-only install.d step, e.g. 60-llm-runtime.sh, that is a clean no-op on the VM edition (check EDITION from the release file). It should install only from the local /brainsyncos/repo, with apt options that use only that source list (e.g. -o Dir::Etc::sourcelist=<file> -o Dir::Etc::sourceparts=-). Otherwise the NVIDIA network lists that nvidia-l4t-apt-source installs will make an offline apt-get update fail, and that aborts the install.
- Document an opt-in post-install path for developers: `sudo apt install nvidia-jetpack` (or cuda-toolkit-13-2 or nvidia-cudnn) over the NVIDIA repo that is already configured. Pin to the 7.2.1-b49 metapackage versions, because suite r39.2 also holds 7.2-b184 and 7.2-b187.
- For the VM edition, build llama.cpp CPU-only (or Vulkan) as a separate package flavour, e.g. brainsync-llama-cpp-cpu with Provides: brainsync-llama-cpp, so the install.d step can choose the flavour per edition.
- Record the exact CUDA versions (cudart 13.2.86-1, cublas 13.4.1.3-1, toolkit 13.2.2-1) in config/*.env and check them against the jetson/common Packages index at build time, so drift is caught.

**Risks**

- Licence interpretation. Attachment A allows redistributing cudart/cublas/cublasLt only inside an application with additional functionality, and only for access by that application. Putting NVIDIA's own debs into a system-wide /usr/local/cuda on our ISO is the weaker reading. This is not legal advice; nvidia-compute-license-questions@nvidia.com is the contact the EULA names.
- Suite r39.2 floats: it already holds 2 cudart, 2 cublas and 3 JetPack builds. Network installs are not reproducible, and a later `apt upgrade` with NVIDIA's Pin-Priority 600 can move the CUDA libraries under a llama.cpp that was built for one version. Vendoring privately avoids this.
- ISO size grows by about 0.5 GB for cuBLAS. cuBLAS is also 660 MB installed on an 8 GB-RAM device with an NVMe disk; disk space is fine, but very large packages slow the apt step during install.
- The libcuda variant is chosen at boot by nv-load-gpu-libs.service. In the curtin chroot at install time, /usr/lib/aarch64-linux-gnu/libcuda.so.1 may not exist yet, so any install.d step that runs CUDA code (smoke tests) would fail. Only test on the first real boot.
- jetson/common also offers the dGPU libnvidia-compute, which Provides libcuda.so.1. A careless dependency on libcuda.so.1 could pull it in and break the Orin GPU stack.
- Unverified: a full llama.cpp CUDA build at 2 jobs, cuBLAS linking in the builder, the libcublas deb's file list (cublasLt assumed to be inside it), runtime on real Orin hardware, and whether the Orin Nano picks nvgpu.
- Building in the CUDA builder image needs about 2 GB of Docker disk, and the Mac has about 31 GB free.

**Open questions**

- Should the base install include local LLM inference, with +0.5 GB on the ISO and llama.cpp working offline? Or should CUDA and llama.cpp be an opt-in download after install? I recommend bundling the minimal runtime.
- Is BrainSyncOS distributed only to your own devices (internal use), or to third parties? That changes how much EULA risk is acceptable for bundled NVIDIA binaries.
- Do you need cuDNN or TensorRT at all, e.g. for TRIBE or PyTorch on the Jetson, or only llama.cpp? If only llama.cpp, the 2.3 GB TensorRT and 0.4 GB cuDNN can stay network-only.
- Is a dynamically linked llama.cpp with vendored .so files acceptable, or do you prefer a single static binary with cuBLAS linked in (larger, simpler licensing story)?
- Should the developer toolkit (nvcc) be available on the device for on-device builds? That means a network `apt install cuda-toolkit-13-2`, 3.3 GB download and 5.6 GB installed.
- Will the Jetson have network during installation? This decides whether the install-time apt option (b) is even viable as a fallback.

## llama.cpp

## llama.cpp for BrainSyncOS: Phase 3 research

Nothing in the repo or the base ISO was modified. All my containers ran with `--cpus=2 --memory=4g`, one at a time, builds used `-j 2`, and every container was removed afterwards. The `p3-apps-inventory` and `practical_cerf` containers seen during the run belong to other agents. I downloaded about 2.3 GB in total (CUDA debs 1.31 GB, test model 563 MB, v0.7.0 clone and tarball, apt).

Scratch files are under `<scratch>/phase3/llama/`:
- scripts: `scripts-v070/` (build-test.sh, build-cuda.sh, test*.sh)
- CPU build and test logs: `out-v070/`
- CUDA compile check: `out-v070-cuda/`

That folder already held output from an earlier run of this task (`out-cpu/`, built from nightly b11856, possibly with more than 2 jobs). I re-did everything on the pinned tag and only reused one lesson from it, noted below.

**Legend:** **[V]** = I ran it and checked. **[S]** = I read it in the v0.7.0 source or docs. **[I]** = inferred, not checked.

### 1. Which release to pin
- **[V]** Upstream now makes semver releases. **v0.7.0** = commit `620d2225cc436ab0b001e3200e590e3b286ef9ab` = build tag **b11669**, tagged 2026-10-15.
  - Found with `gh api repos/ggml-org/llama.cpp/releases/latest` (its `nightly-tag.txt` asset says `b11669`) and `git ls-remote` (both tags point at the same commit).
  - The newest nightly is b11856 (2026-10-24). Nightlies come out several times a day.
- **[V]** The source tarball `https://github.com/ggml-org/llama.cpp/archive/refs/tags/v0.7.0.tar.gz` is 38,183,379 B. Its sha256 was the same on two downloads: `55d67acf3ad2ad747a9bbeb75f9f0e173dbcc0d1753a471038a90c819421c5e1`.
- License is MIT (GitHub API).
- **[S]** `docs/release.md`: people building from a release tag must pass `-DLLAMA_BUILD_IS_DEV=OFF`. Without it, `--version` prints `0.7.0-dev`, which my CPU build did **[V]**.
- **[S]** v0.7.0 changed the default server port from 8080 to **9931** (#30159). Pass `--port` explicitly.

### 2. CMake options at v0.7.0 [S, then V by building]
- **`LLAMA_CURL` is gone.** It is now a deprecation warning (`llama_option_depr(WARNING LLAMA_CURL)`). Downloads use the built-in httplib client, with `LLAMA_OPENSSL=ON` by default for HTTPS.
  - **[V]** Configure found OpenSSL 3.0.13. The binaries need `libssl.so.3` and `libcrypto.so.3`.
- **Dynamic backends (`GGML_BACKEND_DL`) need `BUILD_SHARED_LIBS=ON`.**
  - `GGML_CPU_ALL_VARIANTS` needs `GGML_BACKEND_DL`, and cannot be combined with `GGML_CPU_ARM_ARCH`.
  - `GGML_BACKEND_DIR` compiles in the path backends are searched first. Then come the executable's directory and **the current working directory**.
  - `GGML_BACKEND_PATH` (environment variable) loads one extra backend.
- **`GGML_NATIVE`** defaults to OFF when cross-building or with ALL_VARIANTS. I set it OFF explicitly.
- **`CMAKE_CUDA_ARCHITECTURES`:** the default list does **not** include 87. With CUDA 13 it is 75-virtual, 80-virtual, 86-real, 89-real, 90-virtual, 120a/121a. You must pass `87-real`.
- **CUDA options:**
  - `GGML_CUDA_FA=ON` and `GGML_CUDA_GRAPHS` are on by default.
  - `GGML_CUDA_NCCL=ON` by default (optional; it just warns when NCCL is missing).
  - `GGML_CUDA_NO_VMM`: when set, `libcuda.so` is not linked.
  - `GGML_STATIC` would link `cublas_static` (not recommended).
- **UI and build-time network:** `LLAMA_USE_PREBUILT_UI` defaults to **ON**, which fetches web-UI assets from an HF bucket at build time. Use `LLAMA_USE_PREBUILT_UI=OFF` and `LLAMA_BUILD_UI=OFF` for an offline, reproducible build.
  - **[V]** With both off, `GET /` returns 404 (no UI).
- **[S]** `LLAMA_SUBPROCESS` defaults to ON. It is needed for router mode, the server's built-in tools and MCP-over-stdio. Turning it OFF removes those features (not built here).

### 3. CPU-only test build, arm64 ubuntu:24.04 [V]
**Flags:** `-G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/brainsyncos/llama -DCMAKE_INSTALL_RPATH='$ORIGIN/../lib' -DBUILD_SHARED_LIBS=ON -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DGGML_BACKEND_DIR=/opt/brainsyncos/llama/lib/ggml -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_BUILD_UI=OFF -DLLAMA_OPENSSL=ON`, compiled with gcc-14.

**Compiler:** the default gcc 13.3 **fails** on the armv9.2 variants (`cc1: error: invalid feature modifier 'sme'`, seen in the earlier run's `out-cpu/run2.log`). gcc-14 (14.2.0 from noble) builds them. Using it pulls in nothing new at runtime, because noble already ships `libstdc++6 14.2.0-4ubuntu2~24.04.1`.

**Time:** apt 23 s, configure 3 s, **build 265 s at `-j 2`** (408 ninja steps), install under 1 s.

**Size:** the install tree is 29 MB, **25 MB stripped**, a 9.9 MB `.tgz`.
- `bin/` 2.5 MB, 15 thin launchers: `llama`, `llama-server`, `llama-cli`, `llama-bench`, `llama-quantize`, `llama-mtmd-cli`, `llama-tts`, and others.
- `lib/`: `libllama.so.0.7.0` 4.7M, `libllama-common` 5.6M, `libllama-server-impl.so` 3.8M, `libmtmd` 1.9M, `libggml-base.so.0.27.0`, `libggml.so.0.27.0`.
- `lib/ggml/`: **8 CPU backends**, about 1 MB each, 7.5 MB stripped: `libggml-cpu-armv8.0_1`, `armv8.2_1`, `armv8.2_2`, `armv8.2_3`, `armv8.6_1`, `armv8.6_2`, `armv9.2_1`, `armv9.2_2.so`.
- Also installed: `include/`, `lib/cmake`, `lib/pkgconfig`.

**Runtime linking:**
- `RUNPATH $ORIGIN/../lib` on binaries and libraries.
- Libraries needed: only `libc`, `libm`, `libstdc++`, `libgcc_s`, `libgomp1`, `libssl3t64`.
- `ldd` in a bare ubuntu:24.04 with just `libgomp1` and `libssl3t64` installed reports nothing missing.

**Backend selection:**
- On the M4 Docker VM the loader logged `loaded CPU backend from /opt/brainsyncos/llama/lib/ggml/libggml-cpu-armv8.2_2.so` (NEON, FP16_VA, DOTPROD, REPACK).
- **[I]** Orin's Cortex-A78AE (armv8.2 + dotprod + fp16, no SVE) should pick the same variant.

**VM fallback check:**
- I put a `libggml-cuda.so` into `lib/ggml/` whose `NEEDED` is a missing `libcudart.so.13`. It was skipped **silently**, `llama-bench` ran on CPU, and the exit code was 0.
- So one package tree works on both editions. The CUDA backend is a separate file that is simply ignored when it cannot load.
- `llama-cli --list-devices` prints `(none)` on CPU-only systems, because it lists only accelerators.

### 4. CUDA 13.2 / sm_87 compile check (no GPU, so compile only) [V]
**Toolchain:**
- Used NVIDIA's `https://repo.download.nvidia.com/jetson/common r39.2` repo, signing key fingerprint `3C6D1FF3100C8C3ABB0869C0E6543461A9996195`.
- Installed `cuda-nvcc-13-2 13.2.86-1`, `cuda-cudart-dev-13-2`, `cuda-cccl-13-2`, `libcublas-dev-13-2 13.4.1.3-1`. apt took 313 s, about 1.3 GB downloaded.
- The toolkit lives at `targets/sbsa-linux`: on JetPack 7, Jetson uses the SBSA CUDA layout.

**Flags:** the CPU flags plus `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87-real -DCMAKE_CUDA_HOST_COMPILER=g++-14 -DGGML_CUDA_NO_VMM=ON -DGGML_CUDA_NCCL=OFF -DLLAMA_BUILD_IS_DEV=OFF`.

**Result:**
- **The `ggml-cuda` target built: 145 `.cu` objects in 1077 s at `-j 2`.** Peak memory stayed under about 0.8 GB.
- `libggml-cuda.so` is **45.8 MB, 39.7 MB stripped**.
- `NEEDED`: `libggml-base.so.0`, `libcudart.so.13`, `libcublas.so.13`, plus the C/C++ runtime. **No `libcuda.so`**, because of NO_VMM.

**Runtime cost on Jetson:**
- `libcublas-13-2` is a **484 MB deb, 659 MB installed** (`libcublasLt.so.13` alone is 579 MB, `libcublas.so.13` is 65 MB). `cuda-cudart-13-2` is 0.8 MB.
- The ISO pool has `nvidia-l4t-cuda` (driver side) but **no libcublas or cudart**. I checked this in another agent's ISO index under `phase3/iso-index`. So these NVIDIA debs must be mirrored into our repo.

**Not tested:** loading or running on a real Orin GPU (no hardware).

### 5. Known Jetson / Orin issues [S, V from GitHub issues]
- **#29142, open, updated 2026-10-20:** the CUDA VMM pool reserves 32 GiB of address space (`CUDA_POOL_VMM_MAX_SIZE = 1ull << 35`, `cuMemAddressReserve`). On Orin iGPUs this makes every image (mmproj) request fail with OOM. The confirmed workaround is **`-DGGML_CUDA_NO_VMM=ON`**.
- **#15034, closed:** broken Gemma 3n output on Orin Nano. v0.7.0 still has `info.devices[id].integrated = false; // Temporarily disabled due to issues with corrupted output`.
  - So ggml-cuda treats Orin as a discrete GPU and copies weights into CUDA buffers.
  - **[I]** During load this can briefly hold the model twice in the shared 8 GB (mmap page cache plus the CUDA copy). Test `--load-mode none` on the device.
  - The device type is still reported as IGPU, and llama.cpp uses an IGPU when it is the only GPU [S].
- **Free-memory reading:** on UMA systems ggml reads free memory from `/proc/meminfo`, so `--fit` (on by default, 1024 MiB margin) adapts to whatever BrainSync is using at load time.
- **`GGML_CUDA_ENABLE_UNIFIED_MEMORY=1`:** the docs describe it as a way to make integrated GPUs work. It is optional; A/B test it.
- **#29499, open:** llama-server sometimes hangs after printing `listening` on an Orin NX (L4T 36.4.7). It is non-deterministic and appeared after the server rewrite.
- **Prompt cache default:** `--cache-ram` defaults to **8192 MiB** of host prompt cache. On an 8 GB board that must be capped (I tested `--cache-ram 256 --ctx-checkpoints 4`).

### 6. llama-server API [V on CPU with SmolLM2-135M and Qwen3.5-0.8B]
- **Health and auth:** `/health` returns 200 with **no auth**. `/v1/models` and `/slots` return 401 without a key.
- **`--api-key-file`** works (one key per line, `#` comments). Prefer it over `--api-key`, which is visible in `ps`.
- **JSON schema:** `/v1/chat/completions` with `response_format: {type: json_schema}` returned valid enum-constrained JSON (`{"color":"blue","n":14}`).
- **Grammar:** `/completion` with a GBNF `grammar` returned only `'yes'` or `'no'`.
- **Tool calling with Qwen3.5-0.8B** (`--jinja` is on by default):
  - With `tool_choice=required` it returned `finish_reason: tool_calls` and `get_weather {"city":"Paris"}`.
  - Reasoning came back separately in `reasoning_content`.
  - With `tool_choice=auto` the 0.8B model declined to call the tool, which is a model-quality issue.
  - SmolLM2 (generic template) hit `length` and returned no tool call.
- **Native parsers [S]:** v0.7.0 ships a dedicated `gemma4` parser and has Qwen3.5-4B template tests (`tests/test-chat.cpp`).
- **CORS:** with `--cors-origins localhost`, a localhost origin gets `Access-Control-Allow-Origin` and `http://evil.example` gets none. The default is `*`.
- **Image fetching [S]:** `image_url` accepts remote URLs, so the server can make outbound requests. Local `file://` needs `--media-path`, which is off by default. Without mmproj, a text-only model answers "image input is not supported".
- **Other endpoints [S]:** `/v1/completions`, `/v1/responses`, `/v1/embeddings`, Anthropic `/v1/messages`, `/metrics` (off by default; 501 [V]).
- **Risky features [S]:** built-in agent tools (`--tools`, can read files and run commands) and MCP are off by default. Never enable them.
- **Memory and speed on the M4 VM, 2 threads:**
  - Qwen3.5-0.8B Q4_0 RSS was **1.41 GB** with `-c 4096`.
  - Speed: pp128 150 t/s, tg32 50 t/s. SmolLM2-135M: tg32 153 t/s.
- **No sd_notify** in the source, so the unit must use `Type=exec`.

### 7. Model candidates for an 8 GB Orin Nano with BrainSync (~1 GB)
Sizes and sha256 come from the HF tree API [V]. I downloaded the Qwen3.5-0.8B file at a pinned revision, and its sha256 matched the API's `lfs.oid` [V].

| Model (repo @ rev) | File | Size | sha256 | License | Published speed on Orin Nano 8 GB |
|---|---|---|---|---|---|
| **Qwen3.5-2B** (`unsloth/Qwen3.5-2B-GGUF` @ f6d5376be1ed) | Q4_K_M | 1.281 GB | `aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223` | Apache-2.0 | 30.0 tok/s in NVIDIA TensorRT-Edge-LLM (INT4 AWQ, not llama.cpp). **[I]** llama.cpp around 20–30 |
| **Qwen3.5-4B** (`unsloth/Qwen3.5-4B-GGUF` @ e87f176479d0) | Q4_K_M | 2.741 GB | `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4` | Apache-2.0 | None published. **[I]** about 12–18 tok/s, bandwidth-bound (68 GB/s, or 102 GB/s in Super mode) |
| **gemma-4-E2B-it** (`ggml-org/gemma-4-E2B-it-GGUF` @ 6938c58e6985) | Q4_0 | 2.841 GB | `8e30dff3ac4c8434c49a7036fa15564bdbb6044e42bf04550bf1a096ad7e6a52` | Apache-2.0 | None published |

- **Smaller fallback:** `ggml-org/Qwen3-1.7B-GGUF` Q4_K_M, 1.282 GB, `d2387ca2…bc7b5`, Apache-2.0. TRT-Edge-LLM: 37.4 tok/s.
- **Too big:** gemma-4-E4B Q4_0 is 4.59 GB, and Google's QAT E4B file is 5.16 GB.
- **Closest published llama.cpp number:** Cosmos-Reason2-2B Q4_K_M at 38 tok/s on an Orin Nano 8 GB (JetPack 6.2.2, MAXN_SUPER).

Sources:
- [TensorRT-Edge-LLM benchmarks](https://nvidia.github.io/TensorRT-Edge-LLM/0.9.1/user_guide/performance/performance-benchmarks.html)
- [jetson-orin-nano-benchmarks](https://github.com/hokwangchoi/jetson-orin-nano-benchmarks)
- [llama.cpp #29142](https://github.com/ggml-org/llama.cpp/issues/29142)
- [#29499](https://github.com/ggml-org/llama.cpp/issues/29499)
- [#15034](https://github.com/ggml-org/llama.cpp/issues/15034)

**Recommendations**

- Pin llama.cpp **v0.7.0**: commit `620d2225cc436ab0b001e3200e590e3b286ef9ab` (= b11669), tarball sha256 `55d67acf3ad2ad747a9bbeb75f9f0e173dbcc0d1753a471038a90c819421c5e1`. Keep it in a config file such as `config/llama.env` (`LLAMA_TAG`, `LLAMA_COMMIT`, `LLAMA_SHA256`). Check both the sha256 and the commit in a new builder step, `scripts/build-llama.sh`, run with `scripts/builder.sh run`. Bump it on purpose, not by following nightlies.
- Build flags, both editions: `-G Ninja -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_IS_DEV=OFF -DCMAKE_INSTALL_PREFIX=/opt/brainsyncos/llama -DCMAKE_INSTALL_RPATH='$ORIGIN/../lib' -DBUILD_SHARED_LIBS=ON -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DGGML_BACKEND_DIR=/opt/brainsyncos/llama/lib/ggml -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_BUILD_UI=OFF`, with CC/CXX set to gcc-14/g++-14. gcc-13 fails on the SME variants.
- Jetson-only CUDA backend flags: `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87-real -DCMAKE_CUDA_HOST_COMPILER=g++-14 -DGGML_CUDA_NO_VMM=ON -DGGML_CUDA_NCCL=OFF`. NO_VMM works around #29142 and removes the `libcuda.so` link. Build it in one configure with the CPU parts and split the outputs into two packages. Budget about 25 min at 2 jobs plus about 1.3 GB of CUDA debs. Cache the toolchain layer in the builder.
- Package as 2 debs, with a third optional and a model deb (below):
- (a) `brainsyncos-llama` (arm64, both editions): `/opt/brainsyncos/llama/{bin,lib,lib/ggml/libggml-cpu-*.so}`, about 25 MB stripped and 10 MB compressed. Depends on `libgomp1`, `libssl3t64`, `libstdc++6 (>= 14)`.
- (b) `brainsyncos-llama-cuda` (orin-nano only): `/opt/brainsyncos/llama/lib/ggml/libggml-cuda.so` (about 40 MB). Depends on `brainsyncos-llama (= ${binary:Version})`, `libcublas-13-2 (= 13.4.1.3-1)`, `cuda-cudart-13-2 (= 13.2.86-1)`, `nvidia-l4t-cuda`.
- (c) optionally, a `-dev` package with include/cmake/pkgconfig. Otherwise drop those from the runtime deb.
Use `/opt`, not `/usr/lib`: the sonames (`libllama.so.0`, `libggml-base.so.0`) would clash with Debian/Ubuntu llama.cpp/ggml packages. With RUNPATH set, no ld.so.conf entry is needed.
- Mirror NVIDIA's `libcublas-13-2` (484 MB deb) and `cuda-cudart-13-2` and their config-common deps into our signed `/brainsyncos/repo`, Jetson ISO only. Do not ship `libcublas-dev`. Check that `cuda-toolkit-config-common` adds the `targets/sbsa-linux/lib` linker path. Otherwise ship `/etc/ld.so.conf.d/brainsyncos-cuda.conf`.
- New install.d steps (numbers are suggestions):
- `60-llama.sh`: `apt-get install brainsyncos-llama` from the local repo. Add `brainsyncos-llama-cuda` only when `EDITION=orin-nano`. Install a system user `llm` and the systemd unit, write a random API key to `/etc/brainsyncos/llama/api-keys` (0600 root, passed with LoadCredential), and do not enable autostart until a model is present.
- `61-llm-model.sh`: install the model deb and point `/var/lib/brainsyncos/models/default.gguf` at it.
- Unit sketch for `brainsyncos-llama.service`:
- `Type=exec`, `User=llm`, `WorkingDirectory=/opt/brainsyncos/llama` (root-owned; ggml also searches the cwd for backend .so files).
- `ExecStart=/opt/brainsyncos/llama/bin/llama-server --host 127.0.0.1 --port 9931 -m /var/lib/brainsyncos/models/default.gguf -c 8192 -np 1 --cache-ram 512 --ctx-checkpoints 4 --no-ui --no-slots --offline --cors-origins localhost --api-key-file ${CREDENTIALS_DIRECTORY}/api-keys` (Jetson adds `-ngl all`).
- `LoadCredential=api-keys:/etc/brainsyncos/llama/api-keys`, `Environment=LLAMA_CACHE=/var/cache/brainsyncos-llama`.
- Network: `IPAddressDeny=any` + `IPAddressAllow=localhost` (blocks outbound image_url fetches), `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`.
- Sandboxing: `NoNewPrivileges=yes ProtectSystem=strict ProtectHome=yes PrivateTmp=yes ProtectKernelTunables=yes ProtectKernelModules=yes ProtectKernelLogs=yes ProtectControlGroups=yes ProtectClock=yes ProtectHostname=yes RestrictNamespaces=yes RestrictRealtime=yes RestrictSUIDSGID=yes LockPersonality=yes SystemCallArchitectures=native SystemCallFilter=@system-service CapabilityBoundingSet= UMask=0077 ReadOnlyPaths=/var/lib/brainsyncos/models`.
- Priority: `Nice=10 CPUWeight=50 IOWeight=50 MemoryHigh=/MemoryMax=` sized after measuring, `Restart=on-failure`, `TimeoutStartSec=300`.
- Per-edition drop-ins: VM gets `PrivateDevices=yes` and can try `MemoryDenyWriteExecute=yes`. Jetson gets `PrivateDevices=no` + `SupplementaryGroups=video render`, or a `DeviceAllow=` list for `/dev/nvmap` and the nvgpu nodes, worked out on hardware.
- Do not enable `--tools`, `--mcp-servers-*`, `--media-path`, `--props`, `--metrics` or router mode (`--models-dir`). Consider `-DLLAMA_SUBPROCESS=OFF`, and `-DLLAMA_OPENSSL=OFF` if models never download on the device. Both are unverified builds; test them first. Remember that `/health` stays unauthenticated.
- Default model: Qwen3.5-2B Q4_K_M (1.28 GB, Apache-2.0), with Qwen3.5-4B Q4_K_M (2.74 GB) as the quality option and gemma-4-E2B Q4_0 (2.84 GB) as the alternative. Ship the default in the signed repo as `brainsyncos-model-qwen3.5-2b-q4km`, built with `dpkg-deb -Znone`, `-Zzstd -z1` or similar, since GGUF Q4 data barely compresses. Record the HF repo, the revision commit, the filename and the sha256 in a pinned manifest (for example `config/models.lock`). The build verifies the sha256 before packaging, and the postinst or the service's ExecStartPre checks it again. Larger models: an optional `brainsyncos-model fetch <name>` that downloads only from `https://huggingface.co/<repo>/resolve/<commit>/<file>` and refuses a sha256 mismatch.
- Add tests in the repo's style:
- Build check: the `lib/ggml/libggml-cpu-*.so` count is 8, `libggml-cuda.so` exists only in the -cuda deb, its NEEDED list has no `libcuda.so`, and `--version` reports `0.7.0` with no `-dev` suffix.
- Container smoke test, as I ran it: `/health` returns 200, `/v1/models` returns 401 without a key, then json_schema, a grammar, `tool_choice=required` and CORS rejection.
- On the device: `llama-bench` on Orin, plus a test that SSVEP stimulus timing stays within limits while the LLM is generating.

**Risks**

- Nothing ran on a real Orin GPU. CUDA runtime behaviour (loading, VMM-off allocation, the iGPU being treated as discrete, real tok/s, memory peaks during load) is unverified. The Orin NX hang in #29499 is still open and non-deterministic.
- Shared DRAM contention: LLM decoding saturates memory bandwidth and the GPU. That can disturb BrainSync's SSVEP flicker timing and EEG processing on the same 8 GB / 68–102 GB/s bus. Use cgroup priority, and possibly pause the LLM during stimulus sessions. This needs measuring.
- Memory: about 5 GB is realistically left for the LLM after the OS and BrainSync. The upstream defaults (`--cache-ram 8192`, 32 context checkpoints, `-np` auto) could drive the board into OOM. On CPU, Qwen3.5-0.8B already used 1.41 GB RSS (repacked weights on top of mmap). It is unknown whether nvmap/CUDA allocations count against systemd `MemoryMax`.
- Jetson ISO size: libcublas adds about 484 MB, plus 1.3–2.8 GB for the model deb. Mirroring NVIDIA CUDA debs into our repo needs a CUDA EULA redistribution check (cuBLAS is normally on the redistributable list, but re-hosting the debs should be confirmed).
- Build reproducibility: the default `LLAMA_USE_PREBUILT_UI=ON` fetches from the network at build time, so it must stay OFF. Building the CUDA backend needs network access to NVIDIA's r39.2 repo, or a cached copy, and takes about 18 min of nvcc at 2 jobs.
- Upstream moves fast (about 190 nightlies since v0.7.0 in 9 days, and the default port changed in this release). Every version bump needs the smoke test re-run. Server flag names and defaults change often.
- Tool calling depends on the model and its template. A 0.8B model refused tools with `tool_choice=auto`, and quality at 2B or 4B is untested. Tok/s figures for Qwen3.5-2B, Qwen3.5-4B and gemma-4-E2B on llama.cpp/Orin are estimates; the only published numbers are from TensorRT-Edge-LLM or for other models.

**Open questions**

- Will the Orin Nano run JetPack 7.2.1 in MAXN SUPER mode (102 GB/s) or the original 68 GB/s profile? That roughly sets LLM tok/s.
- Is a 1.3–2.8 GB model on the Jetson ISO acceptable, or should the model be fetched on first boot (pinned by revision and sha256) so the ISO stays small? Should the VM edition ship a model at all?
- Which LLM use case matters: chat with tool calling, structured JSON for BrainSync, or vision (mmproj adds about 0.56–1 GB and needs NO_VMM because of #29142)? This decides Qwen3.5 versus gemma-4 and whether mmproj ships.
- Should llama-server run all the time, start on demand (socket activation or `--sleep-idle-seconds`), or be paused while BrainSync stimulus sessions run?
- Is it acceptable to mirror NVIDIA's libcublas-13-2 and cuda-cudart-13-2 debs into the BrainSyncOS signed repo, or should Jetson installs fetch them from repo.download.nvidia.com at install time?
- Should the build be pared down: drop optional binaries (llama-tts, mtmd-cli, perplexity, imatrix), build with `LLAMA_SUBPROCESS=OFF` and `LLAMA_OPENSSL=OFF`? Each would need its own verified build.

## Python dependencies (arm64, Python 3.12)

## Python dependencies of the BrainSync apps on Ubuntu 24.04 arm64 (cp312)

**Answer in brief.** Every runtime dependency installs from binary wheels (`--only-binary :all:`), as long as PsychoPy's dependency list is cut down. I made a 57-package hash-pinned lock that has no sdists at all. It builds a venv at its final path in which:
- the app's own `PsychoPyRenderer` (two_targets, and swipe with a card), BrainChess's `BoardRenderer` and every `brainsync`/`app`/`tools` module work under Xvfb;
- **1370 of the app's 1393 tests pass.** 2 fail and 21 are skipped (no real-data recordings, no Node). The 2 failures are float differences of 1 ulp in a fixture, not a dependency problem.
- The Cerelog BrainFlow fork, with aarch64 libraries built by the BrainFlow research agent, also works inside this venv with numpy 2.5.4.

The venv takes 439 MB on disk without .pyc files and 348 MB with the `tests/` folders removed. Compressed with zstd -19 it is about 66 MB, or 83 MB with .pyc files.

Environment: one container, `docker run --platform linux/arm64 --cpus=2 --memory=4g ubuntu:24.04` (24.04.5, Python 3.12.3-1ubuntu0.17), with uv 0.13.5 and pip 26.2.2. It has been removed. Scratch files are in `<scratch>/phase3/pydeps`.

Note: an earlier run of this same task had left files in the shared `phase3/` folder (a lock, a wheel check, a smoke script). I re-ran every step myself in `pydeps/` and did not rely on those files.

### 1. Wheel availability per dependency (verified: `pip download --only-binary=:all: --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 --platform manylinux_2_39_aarch64 --python-version 3.12 --abi cp312`, and `uv pip compile --python-platform aarch64-manylinux_2_39 --only-binary :all:`)

| Dependency | Version resolved | Wheel type |
|---|---|---|
| numpy | 2.5.4 | cp312 manylinux_2_27/2_28 aarch64 (15.7 MB) |
| scipy | 1.18.1 | cp312 manylinux_2_27/2_28 aarch64 (34 MB) |
| matplotlib | 3.11.2 | cp312 manylinux_2_26/2_28 aarch64 (plus contourpy, kiwisolver, fonttools, all aarch64) |
| fastapi | 0.162.0 | pure Python |
| starlette | 1.10.0 | pure Python |
| uvicorn[standard] | 0.54.0 | uvloop 0.23.0, httptools 0.9.0 and watchfiles 1.3.0 (abi3) are aarch64 wheels |
| websockets | 17.3 | cp312 aarch64 |
| pydantic | 2.14.1 | pydantic-core 2.50.1 is a cp312 aarch64 wheel |
| pyyaml | 6.0.3 | cp312 aarch64 |
| anthropic | 2.3.0 | pure Python (jiter 0.18.0 is aarch64) |
| httpx | 0.28.1 | pure Python |
| pillow | 13.0.0 | cp312 manylinux_2_27/2_28 aarch64 |
| chess | 1.10.0 | pure Python |
| psychopy | 2026.2.4 | `py3-none-any`, 42 MB, sha256 `a81fdca5…3277`, Requires-Python `<3.13,>=3.10` |
| pyglet | 1.5.27 | pure Python (psychopy pins exactly this on non-Windows) |

- **None of these needs a source build.** musllinux does not apply, since Ubuntu uses glibc 2.39. The newest manylinux tag in the lock is 2_28.
- **BrainFlow from PyPI** (5.23.0) is one `py3-none-any` wheel of 31 MB whose `.so` files are **x86-64** (I checked `libBoardController.so` with `file`). It also has no Cerelog board, so it cannot be used. The fork's Python code works in the venv:
  - I copied the BrainFlow agent's aarch64 deb payload into the venv's site-packages.
  - Results: `get_sampling_rate(CERELOG_X8_BOARD)=250`, EEG channels 1 to 8, a SYNTHETIC_BOARD session returning a (32, 250) float64 array, and `DataFilter.perform_bandpass` all worked.
  - `tests/test_board.py`: 28 passed.
  - setuptools 80.10.2 (from the `<81` pin) still has `pkg_resources`; importing it only prints a UserWarning.

### 2. PsychoPy's full dependency tree (verified)
- PsychoPy 2026.2.4 **no longer depends on wxPython**; it uses PyQt6. wxPython 4.3.2 has no aarch64 Linux wheel anyway. pyqt6-qt6 6.11.2 does have one (`manylinux_2_39_aarch64`, 85.5 MB).
- `uv pip compile psychopy==2026.2.4 --only-binary :all:` **fails on `psychtoolbox`**. Its newest version, 3.0.22.2, has Linux wheels only for x86_64; for aarch64 there is only the sdist.
- **`esprima` 4.0.1 is sdist-only** (pure Python).
- Resolving with sdists allowed gives 97 packages. That includes pyqt6 + Qt6, opencv-python (50.6 MB), pyarrow (50.9 MB), ffpyplayer (28 MB), imageio-ffmpeg (25.6 MB), tables, gevent and moviepy.
- I did not try building psychtoolbox from source. The app does not need it.

### 3. Minimal set for `from psychopy import visual, event` plus a pyglet window (verified)
Method: `uv pip install --no-deps psychopy-2026.2.4`, then repeated import attempts under `xvfb-run`, installing each missing module in turn (`pydeps/iter.sh`).
- Needed for `from psychopy import visual, event`: packaging, six, scipy, json-tricks, pyyaml, i18next, python-bidi, pandas, arabic-reshaper, freetype-py, requests. pyglet 1.5.27, numpy and pillow were installed at the start.
- **The window also needs pyserial.** `psychopy.hardware.manager` imports `serial.tools.list_ports`. Without it the pyglet backend fails to load, and you only see the vague `NameError: Specified name does not reference a valid object`.
- **`json-tricks` and `six` are imported but not declared** in psychopy's Requires-Dist. They must be listed explicitly.
- matplotlib is **not** needed by psychopy.visual (the app needs it for other things). pandas **is** needed, and it is large: 48 MB.
- With that set, `pydeps/smoke.py` works under Xvfb at 1280x720 and 1920x1080. It uses Window(fullscr, checkTiming=False), Rect, TextStim, ImageStim, ElementArrayStim, ShapeStim, `flip()`, `event.getKeys` and `clearEvents`.
- Under Xvfb the flip interval is about 2 to 3 ms, because Xvfb has no vsync. **Real vsync timing on the Jetson or the VM was not tested.**
- `import psychopy.visual` connects to X when it is imported (`Cannot connect to "None"` with no DISPLAY). The app's lazy import is therefore essential, and headless services must never import it.
- An audit hook (`pydeps/netaudit.py`) recorded **no network activity** during a renderer run. The only write was to `$HOME/.psychopy3/monitors/testMonitor.json`, so the kiosk user needs a writable HOME.

### 4. The lock (verified)
- Files: `pydeps/brainsync.in` (runtime deps, plus the display and chess extras, `json-tricks`, `six` and `setuptools<81`) and `pydeps/psychopy-excludes.txt` (31 psychopy dependencies the app does not need).
- Command: `uv pip compile brainsync.in --excludes psychopy-excludes.txt --python-version 3.12 --python-platform aarch64-manylinux_2_39 --only-binary :all: --generate-hashes -o brainsync.lock.txt`
- Result: **57 packages, all wheels**, 1841 lines, sha256 `b4a91690…1066`.
- The lock lists hashes for every distribution of each version (66 for numpy), not only the aarch64 one.
- Installing it both ways works:
  - `uv pip sync --require-hashes --only-binary :all:`;
  - `pip install --no-deps --require-hashes --only-binary :all: -r` (rc=0, 57 packages).
- `uv pip check` reports psychopy's excluded dependencies as missing. That is expected, so the build must not run `pip check`.

### 5. Venv relocation (verified)
- I made the venv with `python3 -m venv --without-pip /opt/brainsync/venv` and copied it to another path.
  - `bin/python` still worked: it is a symlink to `/usr/bin/python3`, and `pyvenv.cfg` has `home = /usr/bin`, so `sys.prefix` followed the new path.
  - The **console scripts failed** (`/opt/moved/bin/uvicorn: cannot execute: required file not found`). Their shebangs are `#!/opt/brainsync/venv/bin/python`, and `activate` also hard-codes the build path.
  - So the venv **must be built at its final install path**.
- `uv venv --relocatable --python /usr/bin/python3.12` writes `/bin/sh` trampolines that work out their own location. `uvicorn --version` worked after a `mv`. It also links `bin/python` to `/usr/bin/python3.12`, which pins the minor version better than `python3` does.
- Whichever way it is made, the venv only works with the system interpreter (cp312 ABI).
  - The Jetson ISO pool has `python3.12 3.12.3-1ubuntu0.15` and `libpython3.12-stdlib`. **It has no `python3-venv` / `python3.12-venv`**, which does not matter if the venv is built in the builder.
  - The builder has 0.17. Both are 3.12.3 and ABI-compatible.

### 6. Size (verified)
| State | Size |
|---|---|
| uv-installed, no .pyc | 439 MB |
| pip-installed (pip writes .pyc) | 581 MB |
| Without `psychopy/{tests,demos}` (20 MB + 26 MB) and the `tests/` folders of numpy, scipy, pandas, matplotlib and fontTools | 348 MB |
| That stripped venv, tar + zstd -19 | 66 MB |
| Stripped venv with .pyc, tar + zstd -19 | 83 MB (439 MB on disk) |

- The largest folders are scipy 89+31 MB, psychopy 64 MB, pandas 48 MB, numpy 27+28 MB, matplotlib 27 MB, fontTools 24 MB, uvloop 17 MB and anthropic 15 MB.
- **Removing `numpy/testing` breaks scipy** (`array_api_compat` imports `numpy.testing`; I verified the failure). Only folders named `tests` are safe to remove. After that strip I re-checked the renderer, all 43 module imports and the test suite.

### 7. System libraries the venv needs (verified with ldd and the ISO pool)
- The compiled extensions need only **libc6, libgcc-s1, libstdc++6 and zlib1g**; manylinux wheels bundle everything else. freetype-py bundles its own libfreetype.
- pyglet 1.5 loads libraries at runtime with ctypes `load_library`: GL, GLU, X11, Xext, Xinerama, Xxf86vm, Xi, fontconfig and freetype. It is **X11/GLX only**, with no Wayland backend.
- In the Jetson ISO pool (extracted `dists/noble/main/binary-arm64/Packages`, 59,029 lines) all of these are present:

| Package | Version |
|---|---|
| libgl1 | 1.7.0-1build1 |
| libglu1-mesa | 9.0.2-1.1build1 |
| libgl1-mesa-dri | 25.2.8-0ubuntu0.24.04.2 |
| libx11-6 | 2:1.8.7-1build1 |
| libxext6 | 2:1.3.4-1build2 |
| libxinerama1 | 2:1.1.4-3build1 |
| libxxf86vm1 | 1:1.1.4-1build4 |
| libxi6 | 2:1.8.1-1build1 |
| libfontconfig1 | 2.15.0-1.1ubuntu2 |
| libfreetype6 | 2.13.2+dfsg-1ubuntu0.1 |
| xserver-xorg-core | 2:21.1.12-1ubuntu1.6 |
| xinit | 1.4.1-0ubuntu5 |
| nvidia-l4t-3d-core, nvidia-l4t-x11, nvidia-l4t-wayland | 39.2.1 |

- Not in the pool: xvfb, weston and cage.
- I did not check the VM ISO pool myself.

### 8. System python3-* debs instead (verified with `apt-cache policy` against the noble archive)
- python3-pydantic is **1.10.14** (we need 2.6 or newer).
- python3-fastapi is 0.101.0 (we need 0.110 or newer).
- python3-uvicorn is 0.27.1 (we need 0.29 or newer).
- python3-websockets is 10.4 (we need 12 or newer).
- python3-httpx 0.26.0 is just below the 0.27 floor.
- python3-anthropic and python3-chess **do not exist**.
- The Debian `psychopy` package is 2023.2.4 (we need 2026.2 or newer).
- python3-pyglet is 2.0.10, but psychopy needs 1.5.27.
- The Jetson ISO pool is a subset of this archive. **Using system debs is not possible without rebuilding about 10 packages ourselves.**

### 9. Test suite in the venv (verified)
- Run: `pytest -q -p no:cacheprovider`, one process, on a `git archive HEAD` copy of the repo.
- Result: **1370 passed, 21 skipped, 2 failed, in 151 s.**
- The two failures are `tests/test_extension.py::test_fixture_on_disk_matches_brainsync_flicker` and `::test_make_fixture_check_flag`.
- Cause: 17 floats in `flicker_fixture.json` differ by at most 1.11e-16 (1 ulp) between aarch64 Linux and the Mac (`pydeps/fixture_diff.py`). The test compares floats exactly.
- `tests/test_schemas.py` also needs `INTERFACE-v2-draft.md` at the repo root, so packaging the tests needs the whole tree.

**Recommendations**

- Ship one self-contained venv, not system python3-* debs. Noble's versions are too old (pydantic 1.10, fastapi 0.101, uvicorn 0.27, websockets 10.4) or missing (anthropic, chess, psychopy 2026).
- Package layout: `brainsync-runtime` holds the venv with the 57 locked wheels plus the BrainFlow fork. `brainsync` holds the app wheel installed into the same venv, with `Depends: brainsync-runtime (= <exact version>)`. App changes then do not re-ship the 66 to 83 MB runtime. Keep dpkg file ownership separate: runtime owns site-packages/<third-party>, the app owns site-packages/{brainsync,app,tools} and its dist-info.
- Build the venv in the builder at its exact install path, e.g. `uv venv --python /usr/bin/python3.12 /opt/brainsync/venv`, or `--relocatable` for extra safety. Then copy that tree into the deb's staging dir with `cp -a`. Give the runtime deb `Depends: python3.12 (>= 3.12.3), python3.12 (<< 3.13), libc6, libstdc++6, libgcc-s1, zlib1g`. The display part also needs `libgl1, libglu1-mesa, libx11-6, libxext6, libxinerama1, libxxf86vm1, libxi6, libfontconfig1, libfreetype6`; libgl1-mesa-dri is needed on the VM only.
- Commit `brainsyncos/python/brainsync.in`, `psychopy-excludes.txt` and the generated `brainsync.lock.txt` (`uv pip compile ... --python-platform aarch64-manylinux_2_39 --only-binary :all: --generate-hashes`). Pin uv itself by hash in the builder: uv 0.13.5 has an aarch64 manylinux wheel. Install with `uv pip sync --require-hashes --only-binary :all:` (pip `install --no-deps --require-hashes -r` also works). Never run `pip check`, which would flag the 31 psychopy dependencies we excluded on purpose. Keep a downloaded wheelhouse cache in build/ so rebuilds are offline and repeatable.
- List `json-tricks`, `six` and `pyserial` explicitly in the lock input. psychopy imports them, it does not declare the first two, and without pyserial the pyglet backend fails with a vague NameError. Better still, add them to pyproject's `display` extra.
- Put the BrainFlow fork into the venv, not into /usr/lib/python3/dist-packages. The venv has include-system-site-packages=false, so a system `python3-brainflow-cerelog` deb would be invisible to the app. Install the fork's .py files plus the aarch64-built libBoardController/DataHandler/MLModule into the venv as a locally built wheel whose sha256 the build records. Keep `setuptools<81` (resolves to 80.10.2) until the fork stops using pkg_resources.
- Shrink the runtime: delete `psychopy/{tests,demos}` and the folders named `tests` under numpy, scipy, pandas, matplotlib and fontTools (439 MB down to 348 MB). Never delete `numpy/testing`: scipy imports it. Precompile .pyc at build time with `python -m compileall -q -j 2 --invalidation-mode unchecked-hash` and `SOURCE_DATE_EPOCH` set, so the deb is reproducible and nothing gets written to /opt at runtime. Compress with zstd (about 83 MB).
- Install the same aarch64 venv deb on both editions, since both are arm64 noble with glibc 2.39. Only the GL provider differs: NVIDIA's nvidia-l4t-3d-core on the Jetson, Mesa on the VM. The install.d step needs no edition check for Python; add one only for CUDA and llama.cpp.
- Kiosk session: pyglet 1.5 is X11/GLX only, so the display apps need an X server (xserver-xorg-core + xinit, plus nvidia-l4t-x11 on the Jetson; all in the ISO pool) or XWayland. The kiosk user needs a writable HOME for `~/.psychopy3`. Services that do not open a window must never import psychopy.visual, because it connects to X on import.
- Builder test: add a step that runs `xvfb-run` with `pydeps/smoke.py`, `real_renderer.py`, `chess_renderer.py` and `import_all.py` against the built venv. xvfb, Mesa and xauth go into the test container only; they are not in the ISO pool and are not needed on the target. Also run the pytest suite single-process. Change `tests/test_extension.py` to compare the fixture with a tolerance (about 1e-12) instead of exact equality, or regenerate the fixture on the target architecture.

**Risks**

- PsychoPy's dependency list is cut down by hand. A future psychopy version may import another excluded or undeclared module when `visual` loads, and the failure can be a vague NameError rather than an ImportError (as happened with pyserial). Pin psychopy exactly (==2026.2.4) and gate every update on the Xvfb smoke test.
- psychopy requires Python <3.13 and the venv is tied to noble's python3.12 ABI. Moving the base to 26.04 (Python 3.13+) breaks both until psychopy supports it. Ubuntu python3.12 security updates (0.15 in the ISO, 0.17 in the builder) are ABI-compatible, and the imports were tested on 0.17 only.
- Display timing was verified only under Xvfb, which has no vsync (flip about 2 to 3 ms). Frame-locked SSVEP flicker on the Jetson (NVIDIA GLX) and especially in the UTM VM (Mesa, likely virtio-gpu or llvmpipe) has not been measured. A VM may never give a stable 60 Hz vsync.
- The runtime deb is large: about 350 to 440 MB installed and about 66 to 83 MB compressed. It sits on the ISO next to CUDA and llama.cpp packages, so watch the ISO size budget and how long the install step takes.
- Two copies of numpy: the venv has numpy 2.5.4, and the system may carry python3-numpy 1.26.4 for other tools. That is harmless while the venv is isolated. It breaks if anyone sets include-system-site-packages=true or installs BrainFlow as a system deb and expects the app to see it.
- The lock pulls the newest versions at compile time: numpy 2.5, scipy 1.18, pandas 3.1, pillow 13, starlette 1.10, fastapi 0.162. The app's tests pass with them (1370 passed), but they are newer than what the Mac dev venv may be running, so the two environments can drift unless the Mac uses the same lock.
- Shipping a venv means bundled native libraries (OpenBLAS in numpy/scipy, libjpeg/libpng in pillow) are not patched by Ubuntu security updates. They change only when we rebuild the lock.

**Open questions**

- Install path and owner: /opt/brainsync/venv (my test path) or /usr/lib/brainsync/venv? Does the kiosk/service user get a writable HOME for ~/.psychopy3 and the app's data?
- Do you want the dev extra (pytest, pytest-cov, pytest-asyncio) on the appliance for on-device self-tests, or only in the builder? It adds little size, but the tests also need repo files outside the packages (INTERFACE-v2-draft.md and others).
- Should the Mac dev environment use the same uv lock (with a second compile for macOS arm64), so versions on the dev machine and the appliance cannot drift?
- Which display stack runs the PsychoPy apps on the appliance: plain Xorg + xinit kiosk, or a Wayland compositor with XWayland? pyglet 1.5 cannot use Wayland directly.
- Is Xvfb-level verification enough for the VM edition, or must the VM also reach real vsync timing for SSVEP demos? If so, the UTM display/GPU setup needs its own investigation.
- Should the 2 exact-float fixture tests in tests/test_extension.py be changed to use a tolerance? They fail on aarch64 Linux by 1 ulp.

## The Cerelog BrainFlow fork on arm64

## BrainFlow Cerelog fork on linux aarch64: Phase 3 research

**Bottom line:** the fork builds cleanly on arm64 Ubuntu 24.04. A plain `.deb` I made from it installs offline using only packages that are in the Jetson ISO's pool, and the check prints `250`. The real problem is the kernel: **NVIDIA's L4T R39.2.1 kernel does not include the CH340 driver (`ch341`)**, so a V1 Cerelog board will not appear as a serial port on the Jetson until we ship that driver ourselves. I built a working `ch341.ko` for that kernel to show it can be done.

Scratch directory (call it `$P`): `<scratch>/phase3`

### 1. The GitHub remote is public (verified)
- `git ls-remote` with no credentials (`GIT_TERMINAL_PROMPT=0`, `credential.helper=` empty) returned `932b3944… HEAD` and `refs/heads/master`, plus `refs/pull/1/head = ca85cf3d`. The remote master is the same commit as the local checkout.
- A no-credentials shallow fetch of exactly that commit also worked: `git fetch --depth 1 --filter=blob:none <url> 932b3944…` gave `FETCH_HEAD = 932b3944…` and a 1.4 MB `.git`.
- The GitHub tarball URL `archive/932b3944….tar.gz` answers with a 302 and then a 200 `application/x-gzip`.
- The `api.github.com` repo query returned 403. That looks like the API refusing an anonymous request, not a private repo, since the git endpoints work.
- Pinned tree: `932b3944^{tree} = f31dd196ff4fdb5e1706b976ff66ee2637c9a891`. The tree is 351 MB in total:
  - `third_party/onnxruntime/runtimes` (prebuilt binaries): 264.8 MB, not needed.
  - `synchroni` + `neurosdk` blobs: 46 MB, not needed.
  - The ONNX headers that `src/ml/onnx/onnx_classifier.h` includes even with `BUILD_ONNX=OFF` (`third_party/onnxruntime/build/native/include`): only 280 KB.
- The fork is based on upstream BrainFlow as of about June 2025 (Parfenov's last commit is 2025-06-01). It is MIT licensed.

### 2. The uncommitted patch (captured)
- Saved to `$P/cerelog.patch`: 20 lines, only `src/board_controller/cerelog/cerelog.cpp`, sha256 `eace7b03a40df30df1fe95a53b5dbaffaac72caa1de419077a024958df4ebc1e`. I left out the `.DS_Store` noise.
- It adds `serial->set_serial_port_settings(1000, false)` after the port is re-opened in `connect_v1`.
- It applies cleanly with `patch -p1` to a `git archive` export of 932b3944.

### 3. How the native libraries build on Linux
- `tools/build.py` is a thin wrapper around `cmake` and `make` (or ninja).
- **`build.py` has a bug:** `parser.set_defaults(synchroni='store_false')` sets the default to a non-empty string, which counts as true. So `build.py` always passes `-DBUILD_SYNCHRONI_SDK=ON`, and the CMake default is ON as well. Call CMake directly instead.
- CMake options: `USE_LIBFTDI`, `USE_OPENMP`, `BUILD_OYMOTION_SDK`, `BUILD_BLUETOOTH`, `BUILD_BLE`, `BUILD_ONNX`, `BUILD_TESTS` and `BUILD_PERIPHERY` all default to OFF; `BUILD_SYNCHRONI_SDK` defaults to ON.
- `MLModule` is always built; there is no switch to turn it off. Without ONNX it is small and only links libstdc++.
- The Python package loads each library lazily through `ctypes` singletons, so `libMLModule.so` is only opened when `ml_model` is actually used.
- Build outputs go to `compiled/`, and a POST_BUILD step copies them into `python_package/brainflow/lib/`.
- **That lib folder also receives vendor blobs built for the wrong CPU:** `libunicorn.so` and `libeego-SDK.so` are x86-64, and `libunicorn_raspberry.so` is 32-bit ARM. Packaging must leave them out.
- The full build also produces `libGanglionLib.so` and `libMuseLib.so` (aarch64; BLE-dongle boards we don't need), plus static `libBrainflow.a`, `libDSPFilters.a`, `libWaveLib.a` and `libkissfft-double.a`.

### 4. Real aarch64 build (verified)
- Environment: `docker run --rm --platform linux/arm64 ubuntu:24.04` on Docker 29.6.2. The server is natively arm64, so there was no emulation. gcc 13.3.0, cmake from noble.
- Command used:
  `cmake -DCMAKE_BUILD_TYPE=Release -DBRAINFLOW_VERSION=5.0.0 -DBUILD_SYNCHRONI_SDK=OFF -DBUILD_BLE=OFF -DBUILD_BLUETOOTH=OFF -DBUILD_ONNX=OFF -DBUILD_TESTS=OFF ..`
  then `make -j10 BoardController DataHandler MLModule`
- Timings:

| Step | Time |
|---|---|
| Installing build deps (`cmake g++ make python3 binutils file`) | 100 s |
| Configure | 19 s |
| Build of the three libraries, 10 cores on the M4 | 146 s |

- Zero compiler warnings in the full-build log.
- Output sizes:

| Library | Unstripped | After `strip --strip-unneeded` |
|---|---|---|
| `libBoardController.so` | 2,345,752 | 1,972,472 |
| `libDataHandler.so` | 951,928 | 790,320 |
| `libMLModule.so` | 370,264 | 265,624 |

- All three are aarch64 ELF files whose only runtime dependencies are `libstdc++.so.6`, `libm.so.6`, `libgcc_s.so.1`, `libc.so.6` and `ld-linux-aarch64.so.1`. No libftdi, BlueZ or ONNX.
- **Packaging test:**
  - I made `python3-brainflow-cerelog_5.0.0+cerelog932b394-1_arm64.deb` (751 KB, xz) at `$P/out/`. It contains the `.py` files plus the three stripped libraries under `/usr/lib/python3/dist-packages/brainflow`, 3.1 MB unpacked.
  - It declares `Depends: python3, python3-numpy, python3-pkg-resources, libc6, libstdc++6, libgcc-s1`.
  - In a fresh `ubuntu:24.04` it installed with `apt-get install ./deb`, pulling in python3 3.12.3-0ubuntu2.1, python3-numpy 1:1.26.4+ds-6ubuntu1 and python3-pkg-resources 68.1.2-2ubuntu1.2.
  - `python3 -I -c 'from brainflow.board_shim import BoardShim, BoardIds; print(BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD))'` printed **250**. It also listed EEG channels `[1..8]`, and the BoardShim, DataHandler and MLModule versions all reported 5.0.0.
  - The only side effect is a harmless `pkg_resources` DeprecationWarning.
  - `python3-numpy 1:1.26.4+ds-6ubuntu1` and `python3-pkg-resources 68.1.2-2ubuntu1.2` are both in the **Jetson ISO pool**, so the deb's dependencies install offline. No pip, no venv, and the `setuptools<81` pin doesn't matter, because noble stays on setuptools 68.
- The vendored copy at `/Users/school/Documents/ssvep/third_party/brainflow-cerelog` has `.py` files identical to the fork's `python_package/brainflow` (checked with `cmp` on all 6 files). Its libraries are macOS arm64 dylibs only, and its only local change is the setuptools pin. It is not needed on Linux.
- Upstream BrainFlow 5.23.0 on PyPI (one `py3-none-any` wheel, 31 MB) has **no Cerelog**, and upstream board ID 65 is `IRONBCI_32_BOARD`. Upstream and the fork must never both be installed.

### 5. The Cerelog X8's USB serial chip (from the fork's source)
`cerelog.h` and `cerelog.cpp` describe two hardware revisions:
- **V1: CH340 UART bridge, VID 0x1A86.** Linux driver `ch341`, shows up as `/dev/ttyUSB*`.
- **V2: ESP32-S3 native USB CDC, VID 0x303A** ("USB JTAG/serial debug unit"). Linux driver `cdc_acm`, shows up as `/dev/ttyACM*`.

On Linux the fork scans `/dev/ttyUSB0-2` and `/dev/ttyACM0-2`. `brainsync/board.py` also globs `/dev/ttyUSB*` and `/dev/ttyACM*`, and on macOS adds `SLAB_USBtoUART` (CP210x) and `wchusbserial`.

### 6. Which kernels have those drivers (verified)
- **Jetson** (`nvidia-l4t-kernel_6.8.12-tegra-39.2.1-20260806224157`, module directory `6.8.12-1021-tegra`):
  - The kernel `.config` from `nvidia-l4t-kernel-headers` says `CONFIG_USB_ACM=m`, `CONFIG_USB_SERIAL=m`, `CONFIG_USB_SERIAL_CP210X=m` and `CONFIG_USB_SERIAL_FTDI_SIO=m`, but **`# CONFIG_USB_SERIAL_CH341 is not set`**.
  - There is no `ch341.ko` in the deb and nothing in `modules.builtin`.
  - So **V1 boards don't work on the stock Jetson kernel; V2 (`cdc-acm.ko`) does.**
- **VM edition:** `linux-modules-extra-6.8.0-139-generic` (on the 24.04.5 ISO) contains `ch341.ko.zst`, `cdc-acm.ko.zst`, `cp210x` and `ftdi_sio`. `linux-image-generic 6.8.0-139.139` depends on linux-modules-extra, so the default install (`VM_INSTALL_KERNEL=ga`) already has both drivers.
- **Proof that a ch341 module can be built for the Jetson:**
  - Source: upstream `ch341.c` from kernel.org stable v6.8.12, sha256 `b286d49d…`.
  - Built out of tree in an arm64 noble container with gcc-13 against the headers deb taken from the ISO pool. `KDIR=/usr/src/linux-headers-6.8.12-1021-tegra-ubuntu24.04_aarch64/3rdparty/canonical/linux-noble`, which has aarch64 `modpost` and `fixdep`, `Module.symvers` and `.config`.
  - Took 11 s and produced `$P/out/ch341.ko` (76,864 bytes, unstripped). Its `vermagic=6.8.12-1021-tegra SMP preempt mod_unload modversions aarch64` matches NVIDIA's own `cp210x.ko`, and it covers aliases 1a86:7523, 1a86:5523, 1a86:7522 and others.
  - The only warning was a compiler mismatch: 13.3.0 versus the kernel's 13.2.0-23ubuntu4.
  - NVIDIA's modules are signed (`~Module signature appended~`). Ours is not, but `CONFIG_MODULE_SIG_FORCE` is unset and `CONFIG_SECURITY_LOCKDOWN_LSM` is unset, so it should load and only mark the kernel as tainted. **I have not loaded it on real hardware.**
- The Jetson pool has `gcc-13`, `make` and `kmod`, but **no `dkms`**.

### 7. Other system packages that touch these ports (checked versus inferred)
- `brltty 6.6-4ubuntu5` is in the Jetson pool, and `ubuntu-desktop-minimal` recommends it. Its CH340 rule only fires when the parent hub is 1a40:0101, so it is unlikely to grab the board (checked).
- `modemmanager 1.23.4` is in the pool and in the live installer environment. It could probe `/dev/ttyACM*` and toggle DTR, which resets an ESP32-S3. That risk is my inference, not tested.

### Verified vs inferred
- **Verified:** remote reachability, the patch applying, the CMake flags, build time and sizes, linked dependencies, the deb install and the `250` check, kernel configs on both editions, the ch341 build and its vermagic, pool contents.
- **Inferred:** the ch341 module actually loading and the board streaming on a Jetson; the ModemManager interference; build time on the Orin itself (6x A78AE, my estimate is about 3-5x the M4).

**Recommendations**

- Package 1, `python3-brainflow-cerelog` (Architecture: arm64; one deb for both editions, since both are noble arm64). Build it in `scripts/builder.sh` by calling CMake directly, not `tools/build.py` (its synchroni default is broken): `-DCMAKE_BUILD_TYPE=Release -DBRAINFLOW_VERSION=<x> -DBUILD_SYNCHRONI_SDK=OFF -DBUILD_BLE=OFF -DBUILD_BLUETOOTH=OFF -DBUILD_ONNX=OFF -DBUILD_TESTS=OFF`, then `make BoardController DataHandler MLModule`. Install only those three stripped .so files plus the brainflow/*.py files into /usr/lib/python3/dist-packages/brainflow/lib. Never ship the prebuilt libunicorn.so, libeego-SDK.so or libunicorn_raspberry.so from python_package/brainflow/lib.
- Give that deb `Depends: python3, python3-numpy, python3-pkg-resources` (all in the Jetson ISO pool, so the install works offline) and `Provides: python3-brainflow` plus `Conflicts/Replaces: python3-brainflow`. Upstream BrainFlow uses board ID 65 for IRONBCI_32, so the two must never coexist. Do not install with pip or a venv (python3-pip and python3-venv are not in the pool); BrainSync should use the system python3, or a venv created with --system-site-packages.
- Keep the source build-ready without vendoring 351 MB. Add something like brainsyncos/packages/brainflow-cerelog/ with a lock file holding the URL, commit 932b3944bfe6c0f4e29a46b4a34afdeba39edd85 and tree f31dd196ff4fdb5e1706b976ff66ee2637c9a891, plus `patches/0001-cerelog-connect_v1-raw-timeout.patch` (copy of the patch saved at <scratch>/phase3/cerelog.patch). Fetch with an anonymous `git fetch --depth 1 <sha>` and a sparse checkout that skips third_party/onnxruntime/runtimes, synchroni and neurosdk but keeps onnxruntime/build/native/include (280 KB, needed by the compile). Check the tree hash after checkout instead of trusting GitHub tarball checksums. Cache the fetched tree under build/downloads so rebuilds work offline.
- Optionally add a second patch that switches board_shim.py, data_filter.py and ml_model.py from pkg_resources to importlib.resources, as upstream 5.23 did. It is not needed on noble (setuptools 68), but it removes the DeprecationWarning and the setuptools<81 trap on the Mac.
- Package 2, Jetson only: `brainsyncos-ch341-tegra`, a prebuilt ch341.ko built in the builder against nvidia-l4t-kernel-headers taken from the ISO pool (the build takes about 11 s). Install it to /lib/modules/6.8.12-1021-tegra/updates/ch341.ko, run `depmod -a 6.8.12-1021-tegra` in postinst, declare `Depends: nvidia-l4t-kernel (= 6.8.12-tegra-39.2.1-20260806224157)` so a kernel upgrade can't silently strand it, and ship ch341.c (GPL-2.0) in the source tree. Use prebuilt rather than DKMS because dkms is not in the pool. The VM edition needs nothing, since linux-modules-extra already has ch341 and cdc-acm.
- Package 3, `brainsync-udev` (both editions): an /etc/udev/rules.d file matching ATTRS{idVendor}=="1a86" (V1) and "303a" (V2), setting GROUP="dialout" and MODE="0660", adding SYMLINK+="cerelog", and setting ENV{ID_MM_DEVICE_IGNORE}="1" so ModemManager leaves the ports alone. Since neither the fork's port scan nor board.py looks for /dev/cerelog, either pass serial_port=/dev/cerelog from BrainSync or add /dev/cerelog* to board.py's SERIAL_PATTERNS.
- New install.d steps: something like `60-brainsync-repo.sh` (add /brainsyncos/repo as a signed-by source in-target), `70-brainflow.sh` (apt-get install python3-brainflow-cerelog brainsync-udev, plus brainsyncos-ch341-tegra only when EDITION is Jetson), and a check that runs `python3 -I -c '...get_sampling_rate(BoardIds.CERELOG_X8_BOARD)'` and expects 250. Add a builder test that repeats the clean-container install I did here (script at <scratch>/phase3/scripts/test-bf.sh).
- Build in the arm64 builder container on the Mac, not on the Jetson. The full deb build takes about 4.5 min on the M4 including apt, and would be several times slower on the Orin. Version the package as `5.0.0+cerelog<shortsha>-<rev>` and pass the same string to -DBRAINFLOW_VERSION so BoardShim.get_version() shows which build is installed.

**Risks**

- The CH340 driver (`ch341`) is missing from NVIDIA's L4T R39.2.1 kernel (`# CONFIG_USB_SERIAL_CH341 is not set`, no .ko file, not built in). A V1 board will not appear as /dev/ttyUSB* on the Jetson without our out-of-tree module. That module is built and its vermagic matches, but it has not been loaded or streamed on real hardware.
- Our ch341.ko is unsigned while NVIDIA's modules are signed. It loads today only because MODULE_SIG_FORCE and LOCKDOWN_LSM are both off. If UEFI Secure Boot is turned on for the Jetson later, or a future L4T enforces signatures, the module will be refused unless we sign it with an enrolled key.
- The prebuilt module only works with kernel 6.8.12-1021-tegra. Any nvidia-l4t-kernel update from NVIDIA's online apt repo breaks V1 support until we rebuild; the exact-version Depends turns this into a visible apt conflict instead of a silent failure.
- ModemManager (in the pool) may probe /dev/ttyACM* and toggle DTR/RTS, which resets the ESP32-S3 on a V2 board. This is inferred, not tested; the ID_MM_DEVICE_IGNORE udev rule is meant to prevent it.
- Board ID 65 means CERELOG_X8 in the fork but IRONBCI_32 upstream. If anyone pip-installs upstream `brainflow` (for example a dependency pulling it into a venv), it will shadow the fork and silently talk to the wrong board type.
- The fork's tree contains vendor blobs for the wrong CPU (x86-64 libunicorn and eego, 32-bit ARM unicorn_raspberry), and the POST_BUILD step copies them into python_package/brainflow/lib. A careless `cp -a` of that folder would ship them, and dpkg-shlibdeps/lintian may then fail.
- We depend on a personal GitHub fork (shakimiansky) that is not upstream. A force-push or deletion would break fresh builds unless the sparse source tree is cached or mirrored. The uncommitted patch exists only on one machine plus my scratch copy until it lands in the BrainSyncOS repo.
- The fork imports pkg_resources. That is fine on noble (setuptools 68), but it will break on any base with setuptools 81 or later unless the importlib.resources patch is applied.

**Open questions**

- Which Cerelog X8 revision(s) do you actually have: V1 (CH340, /dev/ttyUSB*), V2 (ESP32-S3, /dev/ttyACM*), or both? If it is only V2, the ch341 module could be dropped from Phase 3 or kept as a fallback.
- Should the brainflow-cerelog source live in the BrainSyncOS folder as a pinned fetch (lock file + patches, about 40 MB sparse) or be fully vendored (351 MB, mostly onnxruntime binaries we don't use)? Or should the patch be pushed upstream to shakimiansky/Shared_brainflow-cerelog (there is an open refs/pull/1 = ca85cf3d) so we pin a commit with no patch at all?
- Do you need libGanglionLib.so and libMuseLib.so (OpenBCI Ganglion and Muse over BLE dongles) or any BrainFlow ML/ONNX features, or is Cerelog plus DataFilter enough? The plan above ships only BoardController, DataHandler and MLModule.
- Will Secure Boot ever be enabled on the Jetson? If so, we need a module-signing key and MOK enrollment for ch341.ko.
- Should the Jetson keep NVIDIA's online apt repo enabled (kernel updates would break the prebuilt ch341.ko), or should BrainSyncOS pin or hold nvidia-l4t-kernel?
- Is BrainSync going to run from system python3 (recommended, since python3-venv and pip are not in the pool) or from a venv? A venv would need --system-site-packages to see the deb-installed brainflow.

## The BrainSync apps

## Phase 3 research: inventory of the BrainSync apps in the repo

**Scope:** I read README.md, pyproject.toml, PLAN.md, INTERFACE.md and the code in `brainsync/`, `app/`, `ui/`, `web/`, `extension/`, `tools/` and `config/`.

**Where I tested:** a throwaway container `p3-apps-inventory` (ubuntu:24.04 arm64, `--cpus=2 --memory=4g`, the repo mounted read-only). It has been removed. The repo is untouched: `git status` is clean. Scratch files are in `<scratch>/phase3/apps/` (2.1 MB).

Labels: **[V]** = verified by running something, **[R]** = read in the code, **[I]** = inferred.

### 1. The apps (no `[project.scripts]` entry points; everything is `python -m`)

| App | Command | What it needs at runtime | Network and secrets |
|---|---|---|---|
| SSVEP core: demo, record, check, stimulus, online, score_cued, select_freqs, nakanishi | `python -m brainsync.demo` / `.record` / `.check` / `.stimulus` | Full-screen PsychoPy window. Default pyglet backend: `visual.Window(fullscr=True, units="height", checkTiming=False)`, display.py:330. It refuses to run unless the refresh is within 1 Hz of 60 Hz. Needs the Cerelog X8 over USB serial through the **Cerelog BrainFlow fork, whose native libraries are macOS arm64 only** (README:48). Serial patterns already include `/dev/ttyUSB*` and `/dev/ttyACM*` (board.py:25-30). Spoken prompts use the macOS `say` command only (launch.py:96-108), so they are silent on Linux. | None |
| BrainSwipe | `python -m brainsync.swipe` | The same stimulus plus PNG cards drawn with Pillow (Pillow comes in through matplotlib) | None |
| BrainSwipe browser mock | `python -m brainsync.swipe_web [--open]` | Standard-library HTTP server with Server-Sent Events on **127.0.0.1:8780**. Static files from `brainsync/web/`, which is package data. No flicker. Any modern browser (ES modules, `speechSynthesis`). | None. It works offline [R][V]. |
| BrainChess | `python -m brainsync.brainchess` | Board stimulus `brainsync.brainchess.stimulus` (PsychoPy, commands on stdin). Needs `chess` (the `chess` extra). Stockfish is optional; it is looked for in `$BRAINCHESS_STOCKFISH`, then on `PATH`, then in Homebrew paths (prior.py:33-34). | None |
| BrainChess web mockup | open `web/brainchess/index.html` as a file | Standalone page; no Python serves it | **Internet:** Google Fonts (index.html:10-12) and cdnjs chess.js 0.10.3 (index.html:649). Not in the wheel. |
| Reels (BrainScroll) | `python -m brainsync.reels` | WebSocket bridge on **ws://127.0.0.1:8765** (`--ws-port`; the Origin is checked). Chrome MV3 extension in `extension/`: Chrome 116 or newer, permissions `debugger` and `storage`, keys sent through `chrome.debugger`. Needs Chrome or Chromium in full screen at 60 Hz. | **instagram.com** (cloud, login) |
| BCI Voice | `python -m app.server [--fake-decoder\|--keyboard]` | FastAPI and uvicorn on **127.0.0.1:8000** (`config/default.yaml` `server`). Serves `ui/` and the websockets `/ws/ui` and `/ws/decoder`. **No module in this repo speaks `/ws/decoder` (protocol 2).** The real decoder bridge is the teammate's external `ssvep` repo, so this app only runs here with the fake decoder or keyboard mode [R]. | `ANTHROPIC_API_KEY` (app/menus.py) and `CARTESIA_API_KEY` (app/tts.py, `https://api.cartesia.ai/tts/bytes`), read from the environment or `REPO_ROOT/.env`. Without them it falls back to the fixed lists in `intents.yaml` and the browser's `speechSynthesis`. Speech input (Web Speech STT, Google cloud) is planned for V4 and not built. |
| Test tools | `python -m tools.fake_decoder`, `python -m tools.contract_check` | `contract_check` is a stand-in server on :8000 | None |

**Data paths:**
- **Recordings:** `$BRAINSYNC_DATA_DIR`. Otherwise `<repo>/data` when `pyproject.toml` sits next to the package, and `~/brainsync_data` for an install. From the install it resolved to `/root/brainsync_data` [V]. `record` writes to `data_dir/spectrum_checks`, and `--fake` writes to a temp folder.
- **App logs:** `REPO_ROOT/logs` (app/log.py:27).
- **Other outputs:** the stimulus log goes to a tempfile; `--log FILE` (demo and reels) and `--pgn` (chess) write where you point them.

**Stale docs:**
- The README says the suite collects 997 tests; it collects 1393.
- The README has no BrainChess section.
- The BrainSyncOS README still mentions an `ssvep` package, LSL and TRCA. None of them exist in this repo; there is no pylsl and no TRCA.

### 2. Non-editable install: what breaks [V]

I built a wheel, installed it with `pip install "/src[dev,reels,chess]"` into `/opt/brainsync` (426 MB), and ran each app from `cwd=/tmp`.
- **The wheel (275 KB) contains** the `brainsync`, `app` and `tools` packages, plus `stimulus/configs/*.toml`, `swipe_deck/profiles.toml` and `brainsync/web/*`.
- **It does not contain** `config/*.yaml`, `ui/`, `extension/`, `web/brainchess/` or `.env`.

**Works from the install:** `demo`, `reels --no-browser`, `swipe` and `brainchess` (all `--fake --fast`), `stimulus --plan`, `record --fake`, `check --fake`, `swipe_web` (all `/static/*` returned 200), and `tools.fake_decoder --help`.

**Breaks from the install:**

| file:line | Path | Effect, verified |
|---|---|---|
| app/config.py:26-30 | `REPO_ROOT/config/{default,intents,profile}.yaml` | `python -m app.server` fails: `ConfigError ... site-packages/config/default.yaml: No such file`. `--config /etc/brainsync/default.yaml` does not help, because `intents.yaml` is still looked up in `CONFIG_DIR`. `main()` has no flags for intents, profile or log dir. |
| app/config.py:377-379 | `load_intents()` without limits calls `load_config()` on the default path | Hidden second dependency on `CONFIG_DIR` |
| app/config.py:31 | `REPO_ROOT/.env` | The secrets file would have to live inside site-packages |
| app/server.py:50, 227 | `UI_DIR = REPO_ROOT/"ui"` mounted with `StaticFiles` | Even with every config path passed in: `RuntimeError Directory '.../site-packages/ui' does not exist` |
| app/server.py:263 | `--fake-decoder` subprocess with `cwd=REPO_ROOT` | Works by accident, because the module is importable |
| app/log.py:27 | `DEFAULT_LOG_DIR = <site-packages>/logs` | Run as a non-root user: `PermissionError: .../site-packages/logs` |
| tools/contract_check.py (imports app.config) | Same config path | `--help` fails with `ConfigError` |
| brainsync/paths.py:9-15 | `REPO_ROOT` detection | Works: falls back to `~/brainsync_data`. Needs `BRAINSYNC_DATA_DIR` set for a system service. |
| brainsync/launch.py:69, tools/*.py:37/41 | `PYTHONPATH` and `sys.path` set to `REPO_ROOT` | Harmless |
| brainsync/brainchess/prior.py:34 | Homebrew Stockfish paths | Ubuntu installs `/usr/games/stockfish`, which is not on systemd's `PATH`, so it would silently fall back to the heuristic [I] |
| extension/, web/brainchess/ | Not packaged | Reels cannot be installed from the wheel |

The test suite run against the installed wheel (tests copied out of the tree) gave **70 failed, 1027 passed, 14 skipped, 6 collection errors** (test_config 53, extension 7, integration_v0 5, ...). Top-level package names `app` and `tools` are very generic.

### 3. How app/menus.py calls the LLM [R][V]

- **SDK and client:** `anthropic.AsyncAnthropic(api_key=..., max_retries=0, timeout=llm.timeout_s)` (menus.py:71). It is created only when a key is set, and the SDK is imported lazily.
- **Request** (menus.py:152-160):
  - `model = llm.model` (`claude-haiku-5-5`)
  - `max_tokens = 1024`
  - `system`: a static persona and rules prompt (3420 characters, about 850 tokens) [V]
  - `tools`: one tool, `propose_replies`, with `strict: True`, `additionalProperties: false`, fields `option_1..3`, each `{label, utterance, intent_id enum}`
  - forced `tool_choice: {"type":"tool","name":"propose_replies"}`
  - `output_config: {"effort":"low"}`
  - one user message
- **Retries:** two attempts inside one 4 s deadline (`llm.timeout_s`). Each answer is validated with pydantic (lengths, intents, duplicates, repeats), and a label that is too long is shortened. The error message is fed back on the retry. Status 400/401/403/404 or a TypeError stops at once, and the app shows the fixed list.
- **Model facts** (from the claude-api skill reference): Haiku 5.5 accepts effort `low`..`max` (default `medium`). The forced `tool_choice` that now returns 400 applies to Opus 5.5, Sonnet 5.5 and Fable 5.1, not to Haiku 5.5. **Changing `llm.model` to Opus 5.5 or Sonnet 5.5 would make every call return 400, and the app would silently use the fixed lists.**
- **SDK path, checked against a mock server:** with `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`, `MenuMaker.make_page` sent `POST /v1/messages` (User-Agent `AsyncAnthropic/Python 2.3.0`, anthropic-version `2026-10-21`). The body had exactly `max_tokens, messages, model, output_config, system, tool_choice, tools`, with `strict: true`. It parsed a llama-style `tool_use` reply into 3 valid options.
- **llama.cpp master, from the source** (tools/server/server-chat.cpp `server_chat_convert_anthropic_to_oai` and the server README):
  - `/v1/messages` exists ("Anthropic-compatible"; tool use needs `--jinja`, which is now on by default).
  - Tools become OpenAI `function` entries with `parameters = input_schema`. **`strict` is dropped.**
  - `tool_choice` of type `tool` or `any` becomes `"required"`; the tool name is lost, which is harmless with one tool.
  - **`output_config` is ignored**, since it is not copied.
  - `chat_template_kwargs` and `extra_params` pass through. Use them for `enable_thinking: false` on Qwen-style models.
- **So the current code should run unchanged against llama-server** through `ANTHROPIC_BASE_URL` with any non-empty `ANTHROPIC_API_KEY` [I]. I did not test against a live llama-server; no model was downloaded and no build was run.
- **Latency [I]:** on an Orin Nano, a 1.5-4B Q4 model generating about 150 output tokens plus about 900 prompt tokens will very likely exceed the 4 s budget, so `timeout_s` must go up.

### 4. Browsers [V]

- **Ubuntu 24.04 arm64 apt (noble):**
  - `chromium-browser 2:1snap1-0ubuntu2`: "Transitional package - chromium-browser -> chromium snap", Pre-Depends `snapd`.
  - `firefox 1:1snap1-0ubuntu5`: also a snap transitional package.
  - There is no `chromium` deb.
- **The Jetson ISO pool** (`build/meta/tree.txt`) has a **real Firefox deb**, `firefox_153.0.3+build1-0ubuntu0.24.04.1~mt1_arm64.deb` (Mozilla Team PPA build), plus `snapd_2.76`. It has no Chromium. It also has `speech-dispatcher 0.12.0~rc2`, `speech-dispatcher-espeak-ng`, `espeak-ng-data`, `python3.12 3.12.3-1ubuntu0.15`, `python3-pil`, `python3-yaml`, `python3-matplotlib` and `xserver-xorg-core`.
- **Google Chrome:** `dl.google.com/linux/chrome/deb` now publishes arm64 builds, including **google-chrome-stable 156.0.8078.47-1 arm64 (136.6 MB)**.
- **Which apps need which browser:**
  - **Chrome or Chromium only:** Reels (MV3, `chrome.debugger`).
  - **Any browser:** BCI Voice `ui/` and the `swipe_web` mock. Neither page loads anything from outside. On Linux, `speechSynthesis` needs speech-dispatcher with espeak-ng.
  - **No browser:** the PsychoPy apps (demo, swipe, brainchess, record, stimulus), which need X11 and GL.

### 5. Dependencies and tests [V]

- **Offline wheelhouse:** `pip download --only-binary=:all:` for cp312 manylinux aarch64 of `.[reels,chess]` gives 44 wheels, 84 MB (scipy, numpy, matplotlib, pillow, fonttools, uvloop, anthropic 2.3.0, pydantic_core, ...). Base venv without extras: 412 MB.
- **PsychoPy 2026.2.4 on aarch64 (py3.12):**
  - Wheels only: no solution, because **psychtoolbox 3.0.22.2 is sdist-only**; esprima 4.0.1 is also sdist-only.
  - Allowing sdists, it resolves to about 90 packages, including PyQt6 6.11 (manylinux_2_39), opencv 5.0, pyarrow, pandas, tables and moviepy.
  - The repo only uses `psychopy.visual` and `psychopy.event` (display.py:323, brainchess/stimulus.py:351).
- **README test command, in the container** (py3.12.3, Ubuntu's nodejs 18.19.1): **1393 collected, 1375 passed, 5 failed, 13 skipped in 119.7 s.**
  - 3 failures are node tests: "SyntaxError: Unexpected token 'export'", because Node 18 is too old; the README assumes Node 24.
  - 2 failures are flicker fixture checks (`test_fixture_on_disk_matches_brainsync_flicker`, `test_make_fixture_check_flag`): 17 values differ by at most 1.1e-16, last-bit float differences between Linux and macOS.
  - Skips: brainflow, stockfish, real data, and checks that cannot run as root.

**Recommendations**

- Core apps for the OS image: the SSVEP core (demo, record, check, stimulus, online and the analysis tools), BrainSwipe with its swipe_web mock, BrainChess, and the BCI Voice server (fake-decoder and keyboard modes until a protocol-2 decoder bridge exists in this repo). Make Reels an optional add-on: it needs Chrome, instagram.com and a login. Leave out web/brainchess, or vendor chess.js and the fonts so it works offline.
- Proposed debs in /brainsyncos/repo: (a) `brainsync`: a venv at /opt/brainsync built in an arm64 noble container at that exact path (venvs cannot be moved, and the pool has no python3-venv), installed from a pinned wheelhouse (about 84 MB, about 410 MB installed), depending on the pool's python3.12. (b) `brainsync-data`: /usr/share/brainsync/{ui,extension}, /etc/brainsync/{default,intents,profile}.yaml as conffiles, and systemd units. (c) `brainsync-display`: PsychoPy installed with only the deps it uses (pyglet, freetype-py, ...) or a built psychtoolbox wheel; build it separately because it is heavy. (d) `brainflow-cerelog`: the fork rebuilt for linux-aarch64. Real EEG on both editions is blocked without it. (e) `brainsync-chess-engine`, or depend on noble's `stockfish 16-1build1` (universe; not in the Jetson pool, so it must go into our repo). (f) Separate llama.cpp and CUDA debs, with only (f) restricted to Jetson.
- Before packaging, make the code work from an install: replace REPO_ROOT in app/config.py:26-31, app/server.py:50 and app/log.py:27 with env-overridable paths (BRAINSYNC_CONFIG_DIR, BRAINSYNC_UI_DIR, BRAINSYNC_LOG_DIR, BRAINSYNC_ENV_FILE), or move ui/ and the default YAMLs into package data read with importlib.resources. Add --intents, --profile and --log-dir to app.server main(). Log to /var/log/brainsync or $XDG_STATE_HOME. Load secrets through systemd EnvironmentFile=/etc/brainsync/secrets.env (0640 root:brainsync) instead of REPO_ROOT/.env.
- Add [project.scripts] console entry points (brainsync-demo, brainsync-swipe, brainchess, brainsync-voice, ...). Rename the generic top-level packages `app` and `tools`, for example to brainsync_voice and brainsync_tools.
- Linux fixes: make launch.speak fall back to `spd-say` or `espeak-ng` when `say` is missing; on the Jetson both come from pool packages. Set BRAINCHESS_STOCKFISH=/usr/games/stockfish in the units, or add /usr/games to the fallback list. Set BRAINSYNC_DATA_DIR in the units.
- Local LLM: step 1 needs no code change (ANTHROPIC_BASE_URL=http://127.0.0.1:8080, a dummy ANTHROPIC_API_KEY, llama-server started with --jinja, a larger llm.timeout_s). Step 2 adds an `llm.backends` list to LLMConfig (each with base_url, model, timeout_s, api_key_env), tried in order: local llama-server first, Claude if online, then the fixed lists, with one deadline budget per backend. Send `chat_template_kwargs: {enable_thinking: false}` through extra_body for Qwen-style templates. Keep the existing pydantic validation and retry, because llama-server drops `strict`. Add a startup check that rejects models where forced tool_choice returns 400 (Opus 5.5, Sonnet 5.5, Fable 5.1), or switch to tool_choice auto plus strict for those models.
- Browser: for the partner page and swipe_web, use the pool's Firefox deb (mozillateam ~mt1 build) with speech-dispatcher and espeak-ng. For Reels, either ship google-chrome-stable arm64 (136.6 MB) in our repo with the extension force-installed through /etc/opt/chrome/policies/managed, or a Chromium deb from xtradeb or Debian. Avoid the snap for an offline installer.
- Test hygiene for the builder: use Node 22 or newer for the node tests (Node 18 fails on ES modules), and compare the flicker fixture with a 1e-12 tolerance instead of exact equality so it passes on linux-aarch64. Run pytest against the installed wheel as a packaging gate; today it gives 70 failures and 6 collection errors.

**Risks**

- The Cerelog BrainFlow fork ships only macOS arm64 libraries (README:48). Without a linux-aarch64 build, no app can read real EEG on either edition.
- No protocol-2 decoder bridge exists in this repo; INTERFACE-v2 is still a draft and the bridge lives in the teammate's external ssvep repo. On the OS, BCI Voice would only run with the fake decoder or keyboard.
- PsychoPy on aarch64 needs psychtoolbox built from source and pulls in about 90 packages (PyQt6, opencv, pyarrow, ...). This is a heavy build and a big install. The 60 Hz frame timing on Jetson and in the UTM VM is unvalidated.
- Redistributing Google Chrome inside an ISO may break Google's terms (unverified). The snap-only Chromium and Firefox in noble's archive need snapd and network seeding, which does not suit an offline installer. Branded Chrome 137+ reportedly ignores --load-extension (from memory, not checked), so the extension would need enterprise policy and a CRX.
- With strict dropped and output_config ignored, a small local model can return invalid options. Retries cost seconds, and the 4 s budget will likely make every local call time out unless it is raised. The Orin Nano's 8 GB shared memory must hold the LLM, PsychoPy and a browser at once.
- Switching llm.model to claude-opus-5-5 or claude-sonnet-5-5 makes the forced tool_choice return 400, and the app silently falls back to the fixed lists.
- Online services stay a dependency for parts of the experience: Anthropic, Cartesia, Instagram, the Google Fonts and cdnjs links in the chess mockup, and Chrome's cloud speech-to-text planned for V4. Offline, BCI Voice degrades to the fixed lists and the browser voice.

**Open questions**

- Is BCI Voice core for the image even though its real decoder bridge is outside this repo? Should a protocol-2 bridge built on brainsync.online become part of Phase 3?
- Is Reels (Instagram, which needs internet and a login) wanted on the appliance? If so, may we redistribute Google Chrome arm64, or should we source a Chromium deb (xtradeb or Debian)?
- For the LLM: local llama-server as primary with Claude as fallback, or Claude as primary with local as an offline fallback? Which model size and latency budget is acceptable for a reply page (today 4 s)?
- Should the VM edition (Ubuntu Server, no desktop) get a display stack (X11 plus a browser) so the PsychoPy and browser apps can run, or is it headless-only (fake board, swipe_web served to the Mac's browser)?
- Should secrets (ANTHROPIC_API_KEY, CARTESIA_API_KEY) ever be baked in at build time, or only provisioned on the device after install?
- Where is the Cerelog BrainFlow fork's source for a Linux build? The README only points at a read-only macOS copy under /Users/school.

## BrainSyncOS layout, ISO size and the kernel check

## Scope and what I ran
This was research only. Nothing in the repo or the base ISO changed: `git status --porcelain` is empty, I made no commits, and I touched no UTM VMs. Every container used `--cpus=2 --memory=4g --platform linux/arm64 --rm`, one at a time; all are gone. I downloaded about 200 MB in total (apt inside throwaway containers, curtin's `util.py`, NVIDIA's `Release` files). Scratch files are in `<scratch>/phase3/os-layout`. Other agents share the `phase3/` folder, so I kept to my own subfolder, and I deleted my unpacked debs afterwards.

## 1. Build entry points and the order for a fresh clone (verified by reading the code)
There is **no Makefile, no single build command and no general prerequisites check.** Each script checks only its own needs: `require_cmd` (lib.sh:283-288), `check_free_space` (lib.sh:351-360), `require_container` (lib.sh:294-296) and `validate_logins` (lib.sh:217-234). `builder.sh run` builds the builder image itself when its tag is missing (builder.sh:59-61).

Jetson edition, current order:
1. `export BASE_ISO=...` (or keep the ISO in ~/Downloads; lib.sh:299-311)
2. `config/authorized_keys` and/or `scripts/make-password-hash.sh` (both gitignored in the repo-root `.gitignore`)
3. `scripts/builder.sh build` (optional)
4. `scripts/builder.sh run scripts/build-iso.sh`. It runs verify-base-iso and the boot-byte check itself (build-iso.sh:48-50).
5. `tests/test_phase2.sh`
6. `scripts/write-usb.sh ISO /dev/diskN`

extract-iso, inspect-iso, repack-iso and compare-iso are only for Phase 1 (inventory and roundtrip); the product build does not need them.

VM edition: `scripts/fetch-vm-base.sh` → `builder.sh run scripts/build-vm-iso.sh` → `tests/test_vm.sh` → `scripts/utm-vm.sh create|install`.

Gaps for Phase 3:
- `builder.sh run` passes no `--cpus`/`--memory` (builder.sh:87-97), so there is no way to apply the user's CPU cap.
- The `/scratch` tmpfs is fixed at 3 GiB (builder.sh:90). A tmpfs counts against container memory, so with `--memory=4g` there is little room left for compiles.
- The builder image has no `apt-utils` (apt-ftparchive), cmake or compilers (Dockerfile.builder:13-19).
- The build id covers only `brainsyncos/`: `git status -- .` (builder.sh:50-57). Edits to `../brainsync`, `../app` or `../tools` would not mark a build `-dirty`.
- The secrets gitignore lives in the repo root `.gitignore`, not in `brainsyncos/`. README:101 says to commit `brainsyncos/` plus `.gitignore`.

## 2. Self-containment blockers (verified)
- The app sources are outside `brainsyncos/` (`../brainsync`, `../app`, `../tools`, `../pyproject.toml`). The builder mounts only `brainsyncos/` at `/work` (builder.sh:91).
- The Cerelog fork of BrainFlow is **outside the repo**, at `/Users/school/Documents/ssvep/third_party/brainflow-cerelog`.
  - It holds macOS arm64 `.dylib`s only and no C++ source.
  - Its `VENDORED.md` names the upstream as `github.com/shakimiansky/Shared_brainflow-cerelog@932b3944` **plus an uncommitted change** to `src/board_controller/cerelog/cerelog.cpp`.
  - `brainsync/board.py:60-71` imports it.
  - A Linux arm64 build cannot be reproduced from anything in the repo today.
- The app's dependencies do not fit the ISO pool:
  - `pyproject.toml` needs fastapi, uvicorn[standard], pydantic>=2.6, anthropic, httpx and websockets. The pool has none of them; it has numpy 1:1.26.4+ds-6ubuntu1, scipy 1.11.4-6build1, matplotlib 3.6.3, pil 10.2.0, yaml 6.0.1, serial 3.5-2 and python3.12 3.12.3-1ubuntu0.15. There is no pip, venv or setuptools. (Checked from the ISO's `dists/noble/main/binary-arm64/Packages`: 2190 packages, 2.83 GB pool.)
  - Verified: `python3 -m venv --without-pip /opt/brainsync/venv` works on noble without `python3.12-venv`, so a venv built in the builder at its final path can be shipped inside a deb.
  - The builder's python3.12 is 3.12.3-1ubuntu0.17, the target's is 0.15. The ABI is the same, but the versions drift.
- **The VM ISO pool has only 276 packages and no numpy, scipy or matplotlib** (verified from `Packages.gz` on the Ubuntu ISO). The VM repo must therefore bundle those Ubuntu debs, or let the VM install download them; the VM install already needs network, see README:224-226.

## 3. How the hook runs, and how install.d steps can reach a large repo (verified from code and curtin source)
- `hook.sh` runs in the live installer from `/cdrom/brainsyncos`. It copies **the whole `$src/.`** to `/target/opt/brainsyncos/installer` (hook.sh:106-112), then runs each `install.d/[0-9][0-9]-*.sh` with `curtin in-target -- /usr/bin/env BSOS_INSTALL_DIR=... /bin/bash` (hook.sh:117-123).
  - **If the repo sat at `/brainsyncos/repo`, hook.sh:108 would copy 1-4 GB onto the target.**
  - phase2-checks.sh:219 (`iso_get "$OUT" /brainsyncos ...` into the 3 GiB tmpfs) would fill `/scratch`.
  - The boot-compare check would need its added-names count changed. That count is `find $STAGE_DIR/brainsyncos` (test_phase2.sh:115-117).
- curtin's `ChrootableTarget` bind-mounts only `/dev /proc /run /sys` (+ efivars); checked in canonical/curtin master `util.py:777-779`. A bind mount the hook makes under `/target/tmp` or `/target/media` therefore stays visible to every `curtin in-target` call. `/target/run` would be hidden, because curtin binds the host's `/run` over it.
- The Jetson autoinstall already does `mount --bind /cdrom /target/tmp/cdrom` at Step 2 and never unmounts it. But at Step 10 it rewrites `/target/etc/apt/sources.list` to ports.ubuntu.com (network), before our hook runs (jetsoniso_orin-ai.yaml:514-520, 656-664). The VM autoinstall has no such bind. **So hook.sh must bind-mount the repo itself, the same way on both editions**: `mount --bind /cdrom/<repo> $target/tmp/brainsyncos-repo; mount -o remount,bind,ro ...`, unmounted in `save_log`.
- Offline apt behaviour (verified in a `--network none` noble container): `apt-get update` with only ports.ubuntu.com sources **exits 0** with `W: Failed to fetch`. A tampered index exits 100. So install.d steps should use isolated sources (`-o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts=<dir>`), not the target's sources. That keeps installs offline and reproducible.

## 4. A signed apt repo recipe that works (verified end to end in ubuntu:24.04 arm64)
- Steps: `dpkg-deb --root-owner-group -Zxz` → `apt-ftparchive packages pool` → `apt-ftparchive -o ...Origin/Label/Suite/Codename/Architectures/Components release` → `gpg --clearsign` (InRelease) and `-abs` (Release.gpg) with an **ed25519** key made by `gpg --quick-gen-key ... ed25519 sign never` → `gpg --export` to `/etc/apt/keyrings/brainsyncos.gpg` → a deb822 `.sources` file with `Signed-By:` and `URIs: file:///tmp/brainsyncos-repo` → isolated `apt-get update && install`.
- Results:
  - The package installed.
  - A Packages file changed after signing was **rejected** (rc=100).
  - A different key was **rejected** with `NO_PUBKEY` / "is not signed".
- Script: `<scratch>/phase3/os-layout/scripts/repo-test.sh`.

## 5. ISO size (computed from the config pins; nothing was built)
- **Jetson ISO**: 5,035,601,920 B. The ESP is appended at sector 8,785,920 (4.50 GB), so the ISO9660 area is about 4.2 GiB. Sizes of the NVIDIA r39.2 additions, from https://repo.download.nvidia.com/jetson/{common,som}/dists/r39.2/ (Release fetched; Packages md5 95d5e1c8… matches Release line 13; dependency closure taken outside the ISO pool):

| Addition | Debs | Download | Installed | Jetson ISO becomes |
|---|---|---|---|---|
| CUDA runtime (`cuda-cudart-13-2` + `libcublas-13-2`) | 5 | 0.48 GB | 0.66 GB | about 5.6 GB, plus apps and llama.cpp: about 5.8-6 GB |
| Full `cuda-toolkit-13-2` | 63 | 3.28 GB | 5.62 GB | about 8.3-8.5 GB |
| `tensorrt` (`libnvinfer10` alone is 2.31 GB) | 22 | 4.90 GB | 8.27 GB | about 10 GB |

  - The full toolkit includes libcublas-dev 538 MB and nsight-systems 439 MB, and it pulls `build-essential` and `libglib2.0-0` from the Ubuntu side.
  - The toolkit or TensorRT **no longer fits an "8 GB" stick** (7.45-8.0 GB usable).
  - The index lists two versions each of cuda-toolkit-13-2 (13.2.1-1 and 13.2.2-1) and libcublas-13-2 (13.4.0.1, 13.4.1.3), so the plan needs exact version pins.
- **VM ISO**: 3,825,512,448 B plus the repo, with no CUDA.
- **xorriso limits**: `ISO_COMPLIANCE` already uses iso_9660_level=3 (multi-extent, so single files over 4 GiB are fine), and the largest deb is 2.3 GB. The volume limit is 8 TiB. Size is not the problem; the USB stick and host disk are. The output goes to `build/out` on the Mac, which has 28 GiB free; `MIN_FREE_GB=20`. Copying the repo into `build/stage` and writing the ISO doubles the space needed.

## 6. Kernel check (verified)
- Source: `nvidia-l4t-kernel_6.8.12-tegra-39.2.1-20260806224157_arm64.deb` (36.5 MB), taken from the ISO pool read-only. It has **no `/boot/config-*`**. The module directory is `lib/modules/6.8.12-1021-tegra` (uname -r = 6.8.12-1021-tegra), and the config is embedded (`CONFIG_IKCONFIG=y`; IKCFG_ST at byte 21,958,008 of `/boot/Image`).
- The extracted config's sha256 is ccad9053…7612, the same as another agent's copy.

| Option | Setting | Module file |
|---|---|---|
| CONFIG_USB_ACM | m | `kernel/drivers/usb/class/cdc-acm.ko` exists |
| CONFIG_USB_SERIAL_CP210X | m | `cp210x.ko` exists |
| CONFIG_USB_SERIAL_FTDI_SIO | m | `ftdi_sio.ko` exists |
| CONFIG_USB_SERIAL_CH341 | **not set** | no `ch341.ko`, in the kernel or in nvidia-l4t-kernel-oot-modules (107 .ko files) |

  - CONFIG_USB_SERIAL is m (`usbserial.ko`) and CONFIG_USB_SERIAL_PL2303 is not set.
  - Module signing: `CONFIG_MODULE_SIG=y`, `MODULE_SIG_FORCE` not set, `SECURITY_LOCKDOWN_LSM` not set, `MODVERSIONS=y`, no compression. An unsigned out-of-tree `ch341.ko` will load (it taints the kernel), but it must be built with the vermagic `6.8.12-1021-tegra` and the Module.symvers from `nvidia-l4t-kernel-headers`.
  - `board.py:23-30` looks for `/dev/cu.wchusbserial*`, a CH34x pattern. If the Cerelog X8 uses a CH340/CH341 chip, the stock kernel has no driver for it.

## 7. Conventions a Phase 3 change should copy (file:line)
- Script header doubling as usage: `usage_from_header` (lib.sh:466-468); `source lib.sh` + `load_*` + `require_container` + `require_cmd` (build-iso.sh:35-42).
- Logging: `log/warn/die` with the `[bsos:<script>]` prefix (lib.sh:14-16). Install steps prefix their messages with their own file name (`30-user.sh: ...`).
- Config: plain KEY=value, linted (lib.sh:60-77); a `*_REQUIRED_KEYS` list (lib.sh:41-55); per-key checks in the loader (lib.sh:82-101).
- Install steps: `set -euo pipefail`, `${BSOS_INSTALL_DIR:?}`, edition switch on `/etc/brainsyncos-release` EDITION (10-issue.sh:13-19), skip cleanly where a step does not apply (50-skip-oobe.sh:18-24; README:631), atomic tmp+mv writes (00-release.sh:23-29, 30-user.sh:50-54).
- Test overrides through env vars: `BSOS_HOOK_SRC` etc. (hook.sh:20-23).
- Large files on Docker Desktop: delete with `rm_released`/`force_rm_tree` (lib.sh:381-399).
- ISO writing: `write_stage_iso` (lib.sh:264-281), with `-map` + `chown/chmod` on the listed paths; md5 merge (lib.sh:253-258).
- Tests:
  - the `check`/`finish` runner (testlib.sh:11-40)
  - a host test plus an in-container checks script (test_phase2.sh:52)
  - abort gates (test_phase2.sh:123, 130)
  - refusal tests that match the message (test_phase2.sh:91-96)
  - throwaway credentials under `build/tmp/test-login` (test_phase2.sh:79-86)
  - a separate test output name, deleted unless `--keep` (test_phase2.sh:37-50)
- Secrets: refuse private keys where only a public key belongs (lib.sh:221); gitignored config files (`.gitignore` brainsyncos section).
- Image tag from the Dockerfile hash (builder.sh:32-34).

**Recommendations**

- Repo location on the ISO: put the repo at its own top-level path, `/brainsyncos-repo/` (ISO9660 plain name BRAINSYNCOS_REPO on the VM edition), not `/brainsyncos/repo`. Under `/brainsyncos`, hook.sh:108 would copy it onto the target, phase2-checks.sh:219 would fill the 3 GiB /scratch tmpfs, and the test_phase2.sh:115 name count would break. If you keep `/brainsyncos/repo`, all three must change (an exclude in the hook copy and in the test extracts).
- Map the repo straight from `build/repo/<edition>/` with a second xorriso `-map build/repo/<edition> /brainsyncos-repo`, instead of copying it into build/stage. Extend `write_stage_iso` (lib.sh:264) to take extra map pairs, and `merge-md5sums.py` to hash them, so `md5sum.txt` stays complete.
- Change hook.sh to make the repo visible the same way on both editions: if `/cdrom/brainsyncos-repo` exists, run `mkdir -p $target/tmp/brainsyncos-repo && mount --bind ... && mount -o remount,bind,ro ...`, unmount it in `save_log`, and add a `BSOS_REPO_SRC` test override. Bind under /tmp, not /run: curtin binds the host's /run over it (curtin util.py:777).
- Add a shared helper `payload/lib/apt-repo.sh` that install steps source. It writes a temporary deb822 source with `Signed-By: /etc/apt/keyrings/brainsyncos.gpg` and `URIs: file:///tmp/brainsyncos-repo`. On Jetson it adds the still-mounted `file:///tmp/cdrom/ noble main restricted [check-date=no]` NVIDIA pool. It runs `apt-get -o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts=<tmp>` update and install, with `--no-install-recommends`, fully offline, and removes the temporary source at the end, keeping only the keyring.
- New install steps:
- `60-apt-repo.sh`: installs the keyring and checks the InRelease signature with `apt-get update` failing on any `W:`/`E:`.
- `70-brainsync.sh`: apps, brainflow and the venv; both editions.
- `75-usb-serial.sh`: the ch341 module deb; Jetson only, skips cleanly on the VM.
- `80-cuda.sh`: CUDA runtime; Jetson only, prints 'skipped on vm-arm64'.
- `85-llama.sh`: the cuda llama.cpp variant on Jetson, cpu on the VM.

Keep the gaps in the numbering so Phase 4 can add the kiosk (90-).
- Layout to add inside brainsyncos/:
- `packages/<name>/`, one folder per package (`brainsync`, `brainflow-cerelog`, `llama-cpp`, `ch341-dkms` or `ch341-modules`, `brainsyncos-archive-keyring`), each with `build.sh` (a header-usage script like the others), `debian/control.in`, `patches/` and `README.md`.
- `config/packages.env`: pins as plain KEY=value, e.g. `LLAMA_CPP_REF=b<tag>`, `LLAMA_CPP_SHA256=`, `BRAINFLOW_CERELOG_COMMIT=932b3944`, `CUDA_VERSION=13.2.2-1`, `LIBCUBLAS_VERSION=13.4.1.3-1`, `CUDA_ARCHS=87`, `BSOS_JOBS=2`, `REPO_SUITE=brainsyncos`, `REPO_COMPONENTS=main,jetson`.
- `config/packages.lock`: name, version and sha256 for every third-party deb (NVIDIA and Ubuntu), checked on download.
- `scripts/build-packages.sh [--only NAME] [--edition E]` → `build/debs/<edition>/`.
- `scripts/fetch-debs.sh` for the NVIDIA and Ubuntu debs, cached in `build/downloads/debs/` by sha256.
- `scripts/build-repo.sh --edition jetson|vm` → `build/repo/<edition>/`, with dependency closure against that edition's base package set and the run failing on any unresolved dependency.
- `scripts/make-repo-key.sh` → `config/repo-signing-key.asc` + `.pub.asc`.
- `tests/test_phase3.sh` + `tests/phase3-checks.sh`.
- Signing key: make it per user with `scripts/make-repo-key.sh` (ed25519, sign-only). Gitignore `config/repo-signing-key.asc` and the public key, the same way as `config/authorized_keys`. In the build, import it into a GNUPGHOME on /scratch (tmpfs), never into build/. Fail if any staged or mapped file contains `PRIVATE KEY`. The test generates a throwaway key in `build/tmp/test-repo-key` (copying test_phase2.sh:79-86) and checks that apt rejects a tampered Packages file and a wrong key; both rejections were verified in this research.
- Make brainsyncos/ self-contained:
- Add `brainsyncos/.gitignore` with build/, *.iso and all secrets.
- Pull the app source in through the host side: `builder.sh` runs `git archive` on the repo-root revision into `build/src/brainsync-<rev>.tar`, and adds `BRAINSYNC_SRC_REV` (with -dirty) to the release file, next to BUILD_ID.
- Vendor brainflow-cerelog as a commit pin plus `packages/brainflow-cerelog/patches/0001-cerelog-raw-serial.patch` recovered from the uncommitted change, and build its native libraries for Linux arm64 with CMake in the builder.
- Make the build one command, with a prerequisites check: add `scripts/build.sh` (bash 3.2 safe, optionally behind a thin Makefile) with subcommands `doctor`, `builder`, `packages`, `repo`, `iso`, `vm-iso`, `test` and `all --edition jetson|vm`.

`doctor` checks:
- Docker is running and supports linux/arm64
- free space, against a computed need (repo size + ISO size + margin) instead of the fixed MIN_FREE_GB=20
- base ISO present and pinned
- VM base fetched
- logins present
- repo key present
- package pins complete
- Resource caps: add `BSOS_DOCKER_CPUS`, `BSOS_DOCKER_MEMORY` and `BSOS_JOBS` (default 2) to `builder.sh run`, passed as `--cpus/--memory` and as `-e MAKEFLAGS=-j$BSOS_JOBS CMAKE_BUILD_PARALLEL_LEVEL=...`. Make the /scratch size configurable, because a tmpfs counts against `--memory`. Put compilers and CUDA in a separate `docker/Dockerfile.packages` image, tagged by its own hash, so the ISO builder image stays small.
- CUDA scope: ship only the runtime libraries (cuda-cudart-13-2 and libcublas-13-2, about 0.48 GB), with llama.cpp compiled in the package builder against a toolkit downloaded only there (sm_87). Make the full toolkit an opt-in `BSOS_WITH_CUDA_TOOLKIT=1`, because it adds 3.3 GB. Leave TensorRT out (4.9 GB). Add a size check (`MAX_ISO_BYTES`, default 7.8e9) so a build that would not fit an 8 GB stick fails or warns; otherwise document a 16 GB stick.
- VM edition repo: build the `main` component only (no `jetson` component), and include the Ubuntu debs the VM pool lacks (numpy, scipy, matplotlib…), pinned to the same versions as NVIDIA's pool so both editions run the same libraries. Or make 'download during install' an explicit VM-only choice.
- Tests to add, following the existing structure:
- repo signature checks (good key, tampered file, wrong key)
- `apt-get install --simulate` closure against each edition's real target layers with the network off
- the hook against the Jetson target layer with a bind-mounted repo, extending phase2-checks.sh `hook`
- install.d steps skipping cleanly on the VM
- `modinfo ch341.ko` vermagic matching `6.8.12-1021-tegra`
- the ISO size limit

Build each test with its own throwaway repo and ISO name and delete it unless `--keep`.

**Risks**

- NVIDIA licence: putting CUDA and TensorRT debs on an ISO that leaves your hands may break NVIDIA's licence. Only the runtime libraries listed in the CUDA EULA's Attachment A are clearly redistributable; nvcc and nsight are not. Private use is fine.
- brainflow-cerelog: the Linux arm64 build depends on an uncommitted change that exists only on one Mac (VENDORED.md). If it is lost, the Cerelog board will not stream on the Jetson.
- No CH341 driver: CONFIG_USB_SERIAL_CH341 is not set and no ch341.ko ships anywhere. An out-of-tree module must match vermagic 6.8.12-1021-tegra and MODVERSIONS. It loads unsigned today (no MODULE_SIG_FORCE, no lockdown), but this breaks if NVIDIA enables Secure Boot or lockdown, or bumps the kernel in an L4T update.
- ISO size: adding the toolkit or TensorRT pushes the Jetson ISO past an 8 GB stick (about 8.3-10 GB). Builds also need roughly 2x the repo size plus the ISO size free on a Mac that has 28 GiB free; Docker Desktop keeps deleted bind-mounted files open (README:77).
- Version drift: the NVIDIA r39.2 index already holds two versions of several CUDA packages, and ports.ubuntu.com has no snapshots (README:69). Without a sha256 lock and a local deb cache, rebuilds will not be reproducible, and older pinned versions can disappear.
- Python ABI drift: the builder's python3.12 is 3.12.3-1ubuntu0.17, the target pool's is 0.15. Same ABI, but a venv built in the builder should be smoke-tested against the target layer's interpreter (the roadmap's chroot of the target layers).
- Installing several GB from USB inside NVIDIA's already 15-minute install makes it longer. Any failure in a step fails the whole install, by design (hook.sh); the CUDA postinst has not been tested under curtin's policy-rc.d (allow_daemons=False).
- Memory: compiling in the builder with --memory=4g while the 3 GiB /scratch tmpfs is in use can run the container out of memory.

**Open questions**

- Is the ISO meant for anyone other than you? If it is, can it carry CUDA (and maybe TensorRT) debs, or should it ship only the redistributable runtime libraries?
- Does the Jetson need the full CUDA toolkit (nvcc, for compiling on the device), or only the runtime libraries that llama.cpp needs (about 0.5 GB vs 3.3 GB)?
- Which USB-serial chip does the Cerelog X8 use (CH340/CH341, FTDI, CP210x, or CDC-ACM)? That decides whether the ch341 module package is needed at all.
- Can the uncommitted cerelog.cpp change be committed to the fork (or exported as a patch) so it can be pinned inside brainsyncos/packages/?
- For the VM edition, is downloading during the install acceptable for Ubuntu dependencies (numpy, scipy, matplotlib), or must both editions install fully offline from the ISO?
- Which llama.cpp release and which models? Are models shipped on the ISO (adding GBs) or downloaded after install?
- What USB stick size should the build target: 8 GB (fixes a size limit of about 7.8e9 bytes) or 16 GB?
- Is a `scripts/build.sh` front end enough, or do you want a Makefile in brainsyncos/? make is not guaranteed on a fresh macOS without the Xcode command-line tools.

## Completeness critique

## Completeness review of the Phase 3 research (cuda, llama, pydeps, apps, layout, brainflow)

Nothing in the repo or the ISO was modified. I ran one throwaway container (`--rm --cpus=2 --memory=4g --network none`, now gone) and read code and the scratch indexes. Tags: **[me-V]** = I re-checked it now; **[agent-V]** = an agent verified it and I found no reason to doubt it; **[I]** = inferred.

Note on the relayed user request: it says only "and then restart". There is nothing in this critique step that can be restarted, so I did the review as assigned. The orchestrator should confirm what the user meant to restart.

### A. Contradictions between topics, and how to resolve each

1. **How Python is packaged (the biggest conflict).**
   - brainflow: ship `python3-brainflow-cerelog` into `/usr/lib/python3/dist-packages`, using system python3 and `python3-numpy` 1.26.
   - pydeps: one venv with `include-system-site-packages=false`, numpy 2.5.4, and BrainFlow installed *inside* the venv. A system BrainFlow deb would be invisible to that venv.
   - apps: a venv at `/opt/brainsync`, plus a separate `brainsync-display` deb that might build psychtoolbox.
   - Only the venv can work. Noble's fastapi 0.101, pydantic 1.10 and uvicorn 0.27 are too old, and anthropic, chess and psychopy 2026 have no debs at all (pydeps, agent-V).
   - So the brainflow deb layout should become a locally built wheel installed into the venv. That also drops the need for `python3-numpy` and `python3-pkg-resources` on the target. The VM base target has neither numpy nor pkg-resources offline [me-V: `python3-numpy` is not in `vm-target.txt`].
   - apps' "psychtoolbox needed, about 90 packages" is superseded. pydeps showed a 57-wheel, all-binary lock without psychtoolbox, and the renderers run under Xvfb.

2. **Where the repo goes on the ISO.**
   - The task text, the README roadmap ("under `/brainsyncos/repo`") and brainflow's `60-brainsync-repo.sh` all use `/brainsyncos/repo`.
   - layout points out that `hook.sh` copies the whole `$src/.` onto the target [me-V: `cp -R "$src/." "$target$dest/"` at hook.sh:108]. A repo there would therefore copy 1-4 GB onto the target and fill the 3 GiB `/scratch` during the phase2 checks.
   - Resolve with layout's `/brainsyncos-repo` mapped separately, or add an exclude. The README roadmap text has to be updated either way.

3. **How the CUDA runtime is shipped.**
   - cuda: vendor `libcudart.so.13`, `libcublas.so.13` and `libcublasLt.so.13` *privately* under the llama.cpp tree with RPATH. This follows EULA §1.1.2(2), "accessed only by your application".
   - llama and layout: mirror NVIDIA's `libcublas-13-2` and `cuda-cudart-13-2` debs into our repo, and have `brainsyncos-llama-cuda` depend on them `(=)`.
   - These are two different package designs, and which one is right depends on the licence and distribution decision (D2 below). Both cost about 0.48 GB.

4. **Does an offline `apt-get update` abort the install?**
   - cuda says yes ("fails with an error, which would abort the installer").
   - layout says it exits 0 with `W:`.
   - **[me-V]** In a noble container with `--network none` and NVIDIA's `jetson/common r39.2` https source added, `apt-get update` gave **rc=0** (only `W: Failed to fetch`) and survived `set -e`. With `-o APT::Update::Error-Mode=any` it gave **rc=100**.
   - So cuda's claim is wrong. The real risk is the opposite one: a missing or unsigned local repo would also only produce a warning. Use `Error-Mode=any` together with isolated sources in the repo step.

5. **The port for llama-server.**
   - apps recommends `ANTHROPIC_BASE_URL=http://127.0.0.1:8080`.
   - **[me-V]** v0.7.0 `common/common.h:630` has `port = 9931`, which matches llama.
   - apps also analysed the "llama.cpp master" source. I checked v0.7.0 `tools/server/server-chat.cpp` itself. Lines 559 and 567-575 confirm `input_schema` becomes `parameters` and `tool_choice` becomes `required`, so apps' conclusions hold for the pinned tag.

6. **Names, paths and step numbers do not agree.** The plan has to pick one set:
   - Paths: `/opt/brainsync/venv`, `/opt/brainsyncos/llama` and `/opt/brainsyncos/llama.cpp`.
   - Package names: `brainsync-llama-cpp-cuda` and `brainsyncos-llama-cuda`; `python3-brainflow-cerelog`, `brainflow-cerelog` and a wheel.
   - Step numbers: cuda uses `60-llm-runtime`; llama uses `60-llama` and `61-llm-model`; layout uses `60-apt-repo`, `70`, `75`, `80` and `85`; brainflow uses `60-brainsync-repo` and `70-brainflow`.
   - I recommend layout's numbering and a single prefix, `/opt/brainsyncos/{venv,llama}`.

7. **CUDA claims that one agent inferred and another verified.**
   - cuda inferred that cublasLt is inside `libcublas-13-2`. llama verified it: `libcublasLt.so.13` is 579 MB.
   - cuda inferred that the libcuda stubs are needed for linking. llama verified that `GGML_CUDA_NO_VMM=ON` removes the `libcuda.so` NEEDED entry altogether.
   - The plan should cite llama's verified versions of both.

8. **The roadmap is stale.** README lines 632-638 list "the `ssvep` package … liblsl", and neither exists in this repo (apps). It also says to build "in an arm64 chroot of the target layers". **No agent did that**: every build and test ran in a plain `ubuntu:24.04` container. The Jetson target runs python3.12 `3.12.3-1ubuntu0.15`, while the builder has `0.17`.

9. **Udev and ModemManager scope.** brainflow puts a `brainsync-udev` rule into Phase 3, but README:643 places "udev rules for the Cerelog board" in Phase 4. This is a scope choice, but it is needed in Phase 3 if the V2 board is supposed to stream.

### B. Claims that are shakier than they are presented
- **brainflow, "installs offline using only pool packages".** That deb was installed with `apt-get install ./deb` in a container *with network*. Only the presence of matching versions in the pool was checked, not an offline install against the pool or the target. [me-V: `python3-numpy 1:1.26.4+ds-6ubuntu1` and `python3-pkg-resources 68.1.2-2ubuntu1.2` are in the Jetson pool.]
- **pydeps, "the same aarch64 venv deb works on both editions".** The display libraries `libgl1`, `libglu1-mesa`, `libxinerama1` and `libxxf86vm1` are **not in the VM base target**, and neither is `libgomp1`, which llama needs [me-V: 0 hits in `vm-index/vm-target.txt`].
  - On the VM they only arrive through `ubuntu-desktop-minimal`, which is downloaded during the install (README:224-231).
  - With `VM_DESKTOP=none`, or with apt sources limited to our local repo, the VM install would fail to resolve dependencies.
  - On the Jetson all of these are in the pool [me-V: libgomp1 14.2.0-4ubuntu2~24.04.1, libssl3t64 3.0.13-0ubuntu3.12, libstdc++6 14.2.0-4ubuntu2~24.04.1, libgl1 1.7.0-1build1]. Being in the pool does not mean they are installed in the target. Layout's idea of adding `file:///tmp/cdrom` as a second isolated source is what makes this work; it has not been tested.
- **layout, "VM repo must bundle numpy/scipy/matplotlib".** This is moot if the venv is chosen, since the venv carries its own wheels. What the VM actually lacks offline is the GL, X and libgomp1 libraries.
- **cuda, "nv-load-gpu-libs picks nvgpu on Orin"** [I], and **llama, "Orin picks the armv8.2_2 CPU variant"** [I]. Neither has been run on hardware. No CUDA code has run anywhere.
- **brainflow build timings were measured with `make -j10`**, which breaks the 2-job cap. The sizes are still valid; the timings for a 2-job build are unknown (probably about 4-5x longer). llama's earlier `out-cpu/` run may also have used more than 2 jobs, but its v0.7.0 rerun used `-j 2`.
- **Stockfish.** apps proposes depending on noble's `stockfish 16-1build1`. [me-V] It is **not in the Jetson pool**. It is in noble universe (Depends libc6, libgcc-s1, libstdc++6), so it would have to be mirrored into our repo, or BrainChess falls back to its heuristic.
- **Test counts differ but do not conflict.** pydeps got 1370 passed, 2 failed, 21 skipped. apps got 1375 passed, 5 failed, 13 skipped with Node 18. Both agree on the same 2 float-fixture failures (1 ulp).

### C. Areas a Phase 3 plan needs that are missing or thin
1. **Dependency closure against what the target already has.**
   - No agent resolved our debs against the *installed target*: NVIDIA's squashfs layers plus what the autoinstall installs on the Jetson, or server-minimal plus `ubuntu-desktop-minimal` on the VM.
   - The plan needs an offline test: `apt-get install --simulate` (then a real install) in a `--network none` chroot of each edition's target, using exactly the isolated sources the install step will use.
   - The phase2 `hook` check already runs against the Jetson target layer, so extend it.
   - The VM test layer has no desktop, so the VM test must either add the desktop layer or allow network.
2. **The signing key lifecycle is barely covered.** Only layout addresses it: a per-user, gitignored ed25519 key with no expiry. Missing pieces:
   - Who owns the key for team or CI builds.
   - Whether the *public* key is committed. It should be: gitignoring it as well, as layout suggests, gains nothing.
   - Expiry and rotation. What happens to devices installed under an old key, since `/etc/apt/keyrings/brainsyncos.gpg` stays on the target.
   - Where the private key is backed up. Losing it means devices can never get updates signed by the same key.
3. **Upgrade path.** Phase 6 owns OTA, but Phase 3 already fixes things that make upgrades possible later:
   - Version scheme: brainsync `0.1.0+g<rev>`, `5.0.0+cerelog<sha>-N`, llama `0.7.0-N`.
   - Conffiles under `/etc/brainsync`, `/etc/brainsyncos/llama/api-keys`.
   - Whether the local repo source is removed after install (layout) or kept.
   - Exact-version `Depends` on `nvidia-l4t-kernel` (ch341) and on cublas. `apt upgrade` holds these back, but `full-upgrade` against NVIDIA's online repo (Pin 600) can *remove* our ch341 package.
   - A data-only update for a new model without reinstalling.
4. **Test strategy for the two editions.**
   - Builder-level checks are well covered: llama `--version`, backend counts, NEEDED lists, a container smoke test of llama-server, Xvfb renderer smoke tests, repo signature good/tampered/wrong-key, and ch341 vermagic.
   - Missing:
     - (a) offline install tests in each edition's target chroot (C1);
     - (b) a pytest run against the *installed wheel* as a gate (apps found 70 failures and 6 collection errors, caused by `REPO_ROOT`);
     - (c) a written hardware acceptance checklist for the Orin, covering: CUDA backend loads; `llama-bench` tok/s; peak memory while loading under `MemoryMax`; ch341/cdc-acm enumeration plus streaming with the board (check returns 250); vsync and flicker timing while the LLM generates; #29499 hang check.
     - (d) for the VM: llama CPU flavour, swipe_web, fake board. Display timing explicitly out of scope (README:171).
   - Real UTM installs are possible through `test_vm.sh` and `utm-vm.sh`, but this research was not allowed to touch them, so that path is untested for Phase 3.
5. **ISO size budget.** No agent added it up. My estimate [I, from agent numbers]:
   - Jetson: 5.04 GB base + 0.48 cuBLAS/cudart + about 0.05 llama (CPU and CUDA debs) + about 0.08 venv + about 0.01 brainflow, ch341 and udev = **about 5.7 GB without a model**.
   - With Qwen3.5-2B (1.28 GB): **about 6.9 GB**, which fits an 8 GB stick (layout's limit is 7.8e9).
   - With Qwen3.5-4B (2.74 GB) or gemma-4-E2B (2.84 GB): **about 8.4 GB**, which does not fit.
   - Adding Chrome adds 0.14 GB.
   - VM: 3.83 GB plus about 0.1 GB, plus the model if it ships one.
6. **Disk budget on the Mac (missing, and it will block builds).**
   - [me-V] The volume has 29 GiB free, and `build/out` already holds 8.3 GB.
   - `check_free_space` refuses to run below `MIN_FREE_GB=20` (lib.sh:351). That leaves about 9 GB for Phase 3.
   - Phase 3 adds roughly: a CUDA builder image of about 2-2.5 GB; caches of 1.3 GB toolchain debs, 0.5 GB runtime debs, 1.3-2.8 GB model, about 0.3 GB wheels and about 40 MB fork; `build/repo` of about 2 GB; and a 7 GB ISO plus a 7 GB test ISO.
   - **This will not fit under the current guard.** The plan needs cleanup rules, or a lower guard together with a computed requirement (layout's `doctor` idea).
7. **Build-time network and caching.** A fresh clone needs to reach:
   - NVIDIA `repo.download.nvidia.com` (toolchain and runtime debs; the suite floats);
   - PyPI (57 wheels, hash-locked);
   - GitHub (llama tarball, Cerelog fork);
   - Hugging Face (model at a pinned revision);
   - ports.ubuntu.com (stockfish and the builder's apt).
   - Each needs a sha256-keyed cache under `build/downloads/` so rebuilds work offline. Only layout proposes `config/packages.lock`; the per-topic pins (`config/llama.env`, `models.lock`, `brainsync.lock.txt`, the fork's lock) should be merged into one scheme.
8. **Licensing beyond NVIDIA.** No one produced a licence manifest. Items to cover:
   - **GPL**: PsychoPy is GPL-3.0 and ch341.c is GPL-2.0, so source must be offered.
   - **Permissive**: llama.cpp and the Cerelog fork are MIT; the models are Apache-2.0.
   - **Not checked or not settled**: the wheels in the lock have mixed licences; the Firefox mt1 deb comes from the pool; Google Chrome redistribution was not checked; NVIDIA's EULA question is still open.
   - Suggestion: `/usr/share/doc/<pkg>/copyright` for each package, plus a generated `LICENSES.txt` on the ISO.
9. **CPU and memory caps in the builder.** layout found that `builder.sh run` passes no `--cpus` or `--memory` [me-V: builder.sh:87-97], and that `/scratch` is a 3 GiB tmpfs, which counts against `--memory=4g`. A CUDA compile at 2 jobs peaked at about 0.8 GB (llama), so it fits if `/scratch` is made smaller for package builds.
10. **Apps work from an install only after code changes outside `brainsyncos/`.** These are the `REPO_ROOT` paths in app/config.py:26-31, app/server.py:50 and app/log.py:27, plus the macOS-only `say`, the Homebrew paths for Stockfish, and the generic `app`/`tools` package names. Phase 3 is framed as BrainSyncOS work, but it needs these changes in the app repo, and the BrainSyncOS build id does not track them (builder.sh:50-57 runs `git status -- .`).
11. **Runtime integration that Phase 3 must at least stub, even though the kiosk is Phase 4:**
   - the service users: `llm`, and whether the app runs as `brainsync` (30-user.sh already adds `dialout`, `video`, `render`);
   - a writable HOME for `~/.psychopy3`;
   - `BRAINSYNC_DATA_DIR`;
   - provisioning `ANTHROPIC_API_KEY` and `CARTESIA_API_KEY` on the device;
   - `llm.timeout_s` (4 s today) versus local LLM latency;
   - LLM memory and bandwidth versus SSVEP timing on the shared 8 GB.

### D. Decisions the user must make before implementation
1. **D1, how Python is packaged:** one venv (recommended: `/opt/brainsyncos/venv`, built at that exact path, with the Cerelog BrainFlow wheel inside), or system python3 debs (not workable without rebuilding about 10 packages).
2. **D2, who receives the ISO, and therefore how NVIDIA bits ship:**
   - internal devices only: mirroring NVIDIA's `libcublas-13-2` and `cuda-cudart-13-2` debs is simplest;
   - third parties: vendor a private copy inside the llama package, or fetch from NVIDIA during install or on first boot.
   - Also: are TensorRT, cuDNN or the full toolkit wanted? Recommendation: no, opt-in over the network only.
3. **D3, the LLM and its model:** ship Qwen3.5-2B Q4_K_M on the ISO (about 6.9 GB ISO, 8 GB stick), move to a 16 GB stick for a 4B model, or fetch a pinned model on first boot. Also whether the VM ships a model, and whether the local LLM is primary or a fallback for Claude in BCI Voice.
4. **D4, what "offline" means per edition:** the Jetson fully offline from the ISO (recommended). For the VM, either let its install.d steps use the network (its install already requires network, README:224) or bundle the closure (libgomp1, GL and X libraries, stockfish). Also whether `VM_DESKTOP=none` is still supported for the BrainSync apps.
5. **D5, scope and boundaries:**
   - Which apps go in core: SSVEP core, BrainSwipe and swipe_web, BrainChess, BCI Voice in fake or keyboard mode only.
   - Whether Reels and Chrome stay out.
   - Whether Phase 3 may change app code outside `brainsyncos/` (the `REPO_ROOT` fixes, entry points, `say` fallback).
   - How "self-contained" consumes the app source: a `git archive` of a pinned parent-repo revision, recorded as `BRAINSYNC_SRC_REV`, is recommended.
6. **D6, hardware and repo key facts only the user has:**
   - Which Cerelog revision is in use: V1 with a CH340 chip (needs our `ch341.ko`), or V2 with an ESP32-S3 (works with the stock cdc-acm driver).
   - Whether Secure Boot will ever be enabled.
   - Who holds the repo signing key, whether the public key is committed, and whether the key has an expiry.

**Recommendations**

- Settle the Python model first: one venv at /opt/brainsyncos/venv, built in the builder at that exact path from pydeps' 57-wheel hash lock. Build the Cerelog BrainFlow fork as a local wheel (3 stripped .so files plus the .py files) and install it into the venv. Drop the system python3-brainflow-cerelog deb design, which the venv cannot see.
- Put the repo at /brainsyncos-repo, mapped separately with xorriso, not at /brainsyncos/repo (hook.sh:108 copies $src wholesale). Update the README roadmap and layout's test counts to match. Bind-mount it read-only at $target/tmp/brainsyncos-repo, as layout proposes.
- In the shared apt helper, use isolated sources (Dir::Etc::SourceList=/dev/null, Dir::Etc::SourceParts=<tmp>) plus -o APT::Update::Error-Mode=any. Without Error-Mode=any, a missing or unsigned repo only warns (rc=0, re-checked offline). Fix the cuda topic's wrong claim that an offline update aborts the install.
- Unify names and numbers before writing code: a single prefix /opt/brainsyncos/{venv,llama}; package names brainsync-runtime, brainsync, brainsyncos-llama, brainsyncos-llama-cuda, brainsyncos-ch341-tegra, brainsyncos-archive-keyring, and a model deb; install.d steps 60-apt-repo, 70-brainsync, 75-usb-serial (Jetson), 80-cuda (Jetson), 85-llama. Every Jetson-only step must skip cleanly on vm-arm64.
- Use port 9931 (the llama.cpp v0.7.0 default) or set --port explicitly everywhere. Correct apps' ANTHROPIC_BASE_URL example, which uses 8080. Raise llm.timeout_s for local inference.
- Add a dependency-closure test per edition: in a --network none chroot of that edition's real target layers (the Jetson autoinstall target, and the VM server-minimal plus desktop), run apt-get install --simulate and then a real install, with exactly the sources install.d will use. This is the only check that proves the offline install; no agent did it.
- For the VM, decide between allowing network sources in the install.d steps and bundling libgomp1, libgl1, libglu1-mesa, libxinerama1, libxxf86vm1 and stockfish. None of these is in the VM base target. Keep the Jetson strictly offline and add file:///tmp/cdrom (NVIDIA's pool) as the second isolated source there.
- Merge the separate pin files (llama.env, models.lock, brainsync.lock.txt, the fork's lock, CUDA versions) into one config/packages.env plus one packages.lock with sha256 values. Add a sha256-keyed download cache in build/downloads, so a rebuild needs no network.
- Add a disk budget and cleanup to the plan: about 29 GiB is free, build/out already holds 8.3 GB, and MIN_FREE_GB=20 leaves about 9 GB, which is less than Phase 3 needs (CUDA builder image, caches, repo, a 7 GB ISO and a test ISO). Compute the need instead of the fixed guard, and delete the test ISO by default.
- Add --cpus, --memory and BSOS_JOBS=2 to builder.sh run, make the /scratch tmpfs size configurable, and put compilers and CUDA in a separate Dockerfile.packages image. Re-time the BrainFlow build at 2 jobs; the reported timings were at -j10.
- Document the key lifecycle: an ed25519 signing subkey with an expiry, the public key committed and the private key gitignored and backed up, and a rotation procedure (ship old and new public keys in brainsyncos-archive-keyring). Record which key signed each build in /etc/brainsyncos-release.
- Write a Jetson hardware acceptance checklist now, to run on the first device: CUDA backend loads (and nvgpu is the libcuda variant), llama-bench tok/s, peak memory while loading, ch341 or cdc-acm device appears and BrainFlow streams, SSVEP vsync timing while the LLM generates, and the llama-server hang from #29499.
- Gate packaging on pytest run against the installed wheel (today: 70 failures and 6 collection errors from REPO_ROOT). Plan the app-side changes (configurable paths, [project.scripts], spd-say fallback, /usr/games/stockfish) as explicit Phase 3 work in the parent repo, pinned into brainsyncos through BRAINSYNC_SRC_REV.
- Generate a licence manifest on the ISO (copyright files per package plus LICENSES.txt). Ship the source of ch341.c (GPL-2) and the source offer for PsychoPy (GPL-3). Keep nvcc, nvvm, cuDNN and TensorRT off the ISO.

**Risks**

- The sub-results disagree on how Python ships (system deb or venv) and how the CUDA runtime ships (mirrored NVIDIA debs or a private vendored copy). Implementing them as written would produce a BrainFlow install the app cannot import, and a licence position that differs by package.
- No offline install was tested against either edition's real target. Every result was tested in plain ubuntu:24.04, with the network on for the BrainFlow deb, while the roadmap requires a chroot of the target layers. The VM target lacks libgomp1 and the GL and X libraries, and the Jetson target's python3.12 is 0.15 while the builder's is 0.17.
- An offline apt-get update exits 0 even when sources fail (re-checked). Without Error-Mode=any or an explicit check, a broken or unsigned local repo can surface later as a confusing failed install, or a silent partial install, rather than a clear signature error.
- Mac disk space: about 29 GiB free against a 20 GB guard, with 8.3 GB already in build/out. Phase 3's images, caches, repo and two ISOs will trip check_free_space or fill the disk. Docker Desktop keeps deleted bind-mounted files open (README:77).
- ISO size grows from about 5.7 GB to about 6.9 GB with a 2B model. A 4B model or gemma-4-E2B pushes it to about 8.4 GB, past an 8 GB stick.
- Nothing CUDA-related or kernel-module-related has run on Orin hardware: libggml-cuda loading, the nvgpu libcuda variant, memory peaks with the iGPU treated as a discrete GPU, ch341.ko loading, BrainFlow streaming, and vsync timing while the LLM loads the memory bus.
- Upgrade fragility: an exact-version Depends on nvidia-l4t-kernel (ch341) and on cublas is held back by apt upgrade, but full-upgrade against NVIDIA's Pin-600 online repo can remove our packages. Losing or rotating the repo key without a plan leaves installed devices unable to verify future updates.
- Phase 3 needs app code changes outside brainsyncos/ (REPO_ROOT paths, entry points). The current build id ignores the parent tree, so ISOs could carry untracked app changes unless BRAINSYNC_SRC_REV is recorded.
- The relayed user request ('and then restart') is ambiguous and was not acted on in this critique step; the orchestrator should confirm what the user wants restarted.

**Decisions it asked for**

- D1: Is a single venv (/opt/brainsyncos/venv with BrainFlow inside) acceptable as the BrainSync runtime, instead of system python3 debs?
- D2: Will the ISO go only to your own devices, or to third parties? That decides between mirroring NVIDIA's cudart/cublas debs, vendoring a private copy in the llama package, or fetching them from NVIDIA at install or first boot. Do you need TensorRT, cuDNN or nvcc on the device at all?
- D3: Ship a model on the ISO (Qwen3.5-2B, ISO about 6.9 GB, 8 GB stick), use a 16 GB stick for a 4B model, or fetch a pinned model on first boot? Should the VM ship a model? For BCI Voice, is the local LLM primary or an offline fallback for Claude?
- D4: Must the VM edition install BrainSync fully offline from its ISO, or may its install.d steps use the network (its install already requires network)? Does VM_DESKTOP=none still need to support the BrainSync apps?
- D5: Which apps are core (SSVEP core, BrainSwipe and swipe_web, BrainChess, BCI Voice in fake or keyboard mode)? Is Reels with Chrome excluded? May Phase 3 change app code outside brainsyncos/ (REPO_ROOT fixes, entry points, the say fallback, renaming app/ and tools/)?
- D6: Which Cerelog X8 revision do you have: V1 with a CH340 chip (needs our ch341.ko) or V2 with an ESP32-S3 (stock cdc-acm works)? Will Secure Boot ever be enabled on the Jetson?
- Key ownership: one team signing key (public part committed, private part backed up where?) or per-developer keys? Should the key expire and be rotated?
- Your request said 'and then restart'. What should be restarted: the planning workflow, a research topic, or something else?
