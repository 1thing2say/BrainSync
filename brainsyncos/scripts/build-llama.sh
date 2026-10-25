#!/usr/bin/env bash
# build-llama.sh - build llama.cpp for aarch64, then check it (builder only).
#
# Usage: scripts/builder.sh --llama run scripts/build-llama.sh compile [--force]
#        scripts/builder.sh run scripts/build-llama.sh check [--force]
# scripts/build.sh llama runs both. Pins and flags: llama/llama.env.
#
# compile runs in the llama builder for the host (scripts/builder.sh --llama):
# on an arm64 host (Apple Silicon) the native arm64 llama builder
# (docker/Dockerfile.llama-builder: g++-14 and NVIDIA's aarch64 nvcc from the
# Jetson repository, no emulation); on an x86-64 host the amd64 cross-builder
# (docker/Dockerfile.cross-builder: aarch64-linux-gnu-g++-14, NVIDIA's x86
# nvcc with the sbsa target files and llama/aarch64-linux-gnu.cmake), which
# compiles at native speed where the arm64 builder would run gcc and nvcc
# under qemu, 6 to 9 times slower (measured). Both give the same build steps,
# flags and checks; the manifest's TOOLCHAIN names the path. Steps:
#   1. the release tarball (build/downloads/LLAMA_TARBALL, fetched once), its
#      size and sha256 checked, and the commit in its header
#   2. the CPU build (CMAKE_FLAGS: every arm64 CPU variant as a loadable
#      backend), installed for LLAMA_PREFIX; headers, CMake and pkg-config
#      files dropped, the rest stripped
#   3. the CUDA build: the same configure plus CUDA_CMAKE_FLAGS, of which only
#      CUDA_TARGET (libggml-cuda.so) is built and kept. Its libggml-base must
#      be byte-identical to the CPU build's, so the backend fits the base
#      library both editions ship.
#   4. every ELF file checked: aarch64, RUNPATH inside LLAMA_PREFIX, nothing
#      needed but the C/C++ runtime, its own libraries and, for the CUDA
#      backend only, libcudart.so.13 and libcublas(Lt).so.13 (never
#      libcuda.so.1: libcuda comes from L4T), no build path in any file
# Sources and builds sit at fixed /scratch paths, mapped to
# /usr/src/llama.cpp in debug strings, so two runs give the same bytes.
#
# check runs in the arm64 builder: it installs cpu/ at LLAMA_PREFIX inside
# the container, then
#   - ldd resolves every binary and backend there; NEEDED as in step 4
#   - llama-server --version reports LLAMA_VERSION's build and commit
#   - llama-server with the test model (TEST_MODEL_*, a tiny chat model,
#     fetched once to build/downloads/llama-test/, never shipped): /health
#     without a key, 401 without the key elsewhere, a few tokens from
#     /completion and an answer from /v1/messages (the Anthropic-style API
#     BCI Voice calls), on a CPU backend from lib/ggml
#   - the CUDA backend against NVIDIA's real Jetson runtime (TEST_CUDART_*,
#     TEST_CUBLAS_*, fetched once): ldd resolves it, and llama-server loads it
#     and, with no GPU and no libcuda here, falls back to the CPU
#
# Output in build/llama/, kept while up to date (compile: the inputs, every
# file in llama/, this script and both llama builder Dockerfiles, unchanged
# and SHA256SUMS intact; check: the same SHA256SUMS); --force rebuilds:
#   cpu/        the tree installed at LLAMA_PREFIX (bin/, lib/, lib/ggml/)
#   cuda/       libggml-cuda.so (for LLAMA_PREFIX/lib/ggml/)
#   LICENSE     llama.cpp's licence (MIT)
#   SHA256SUMS  for sha256sum -c, over the three above
#   llama.env   compile's manifest, plain KEY=value: LLAMA_*, INPUTS_SHA256,
#               SUMS_SHA256 (of SHA256SUMS), CMAKE_FLAGS, CUDA_CMAKE_FLAGS,
#               TOOLCHAIN (native-aarch64 or cross-x86_64, then the gcc, nvcc
#               and cmake versions), NEEDED_CPU, NEEDED_CUDA, GLIBC_MAX, GLIBCXX_MAX,
#               CPU_SECONDS, CUDA_SECONDS, BUILD_REV
#   check.env   check's results: CHECKED_SUMS_SHA256, VERIFIED_VERSION,
#               VERIFIED_CPU_BACKEND, VERIFIED_TOKENS, VERIFIED_CUDA, CHECK_REV
# packages/brainsyncos-llama packages only an output whose check.env matches.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

mode="${1:-}"
case "$mode" in
    compile|check) shift ;;
    -h|--help) usage_from_header; exit 0 ;;
    *) die "usage: build-llama.sh compile|check [--force] (see --help)" ;;
esac
force=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --force) force=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_container
RECIPE="$BRAINSYNCOS_ROOT/llama"
load_env_file "$RECIPE/llama.env" "LLAMA_VERSION LLAMA_COMMIT LLAMA_URL LLAMA_TARBALL LLAMA_SHA256 LLAMA_SIZE
LLAMA_PREFIX LLAMA_BUILD_NUMBER CMAKE_FLAGS CPU_VARIANTS CUDA_CMAKE_FLAGS CUDA_TARGET CUDA_NVCC_VERSION CUDA_RUNTIME
TEST_MODEL_URL TEST_MODEL_FILE TEST_MODEL_SHA256 TEST_MODEL_SIZE TEST_CUDART_URL TEST_CUDART_SHA256
TEST_CUBLAS_URL TEST_CUBLAS_SHA256"
OUT="$BUILD_DIR/llama"
W=/scratch/llama
# The C/C++ runtime every file may need, and what the CUDA backend adds.
RUNTIME_RE='libc\.so\.6|libm\.so\.6|libstdc\+\+\.so\.6|libgcc_s\.so\.1|ld-linux-aarch64\.so\.1'
CUDA_RE='libcudart\.so\.13|libcublas\.so\.13|libcublasLt\.so\.13'

# fetch URL FILE SHA256 [SIZE]: FILE (in build/downloads) from URL once, then checked.
fetch() {
    local url="$1" f="$2" sum="$3" size="${4:-}"
    if [ ! -f "$f" ]; then
        mkdir -p "$(dirname "$f")"
        log "fetching $url"
        curl -fsSL --retry 3 -o "$f.part" "$url" || { rm -f "$f.part"; die "cannot download $url"; }
        mv "$f.part" "$f"
    fi
    [ -z "$size" ] || [ "$(file_size "$f")" = "$size" ] || die "${f#$BRAINSYNCOS_ROOT/} is $(file_size "$f") bytes, llama.env pins $size"
    [ "$(sha256_of "$f")" = "$sum" ] || die "${f#$BRAINSYNCOS_ROOT/} does not match its sha256 in llama/llama.env"
}

# elf_files DIR: every ELF file under DIR (not symlinks), sorted.
elf_files() {
    find "$1" -type f -print0 | LC_ALL=C sort -z | while IFS= read -r -d '' f; do
        [ "$(head -c 4 "$f" | od -An -tx1 | tr -d ' \n')" = 7f454c46 ] && printf '%s\n' "$f"
    done
    return 0   # not the last file's ELF test
}

# needed FILE: the NEEDED entries of FILE ($OBJDUMP: objdump, or the cross objdump).
needed() { "$OBJDUMP" -p "$1" | awk '$1 == "NEEDED" {print $2}'; }

# check_elf FILE EXTRA_RE: FILE is an aarch64 ELF whose NEEDED entries are the
# C/C++ runtime, a soname of this build (SONAMES) or EXTRA_RE, never libcuda,
# and whose RUNPATH stays inside LLAMA_PREFIX.
check_elf() {
    local f="$1" extra="$2" name bad rp
    name="${f#"$TREE"/}"
    "$OBJDUMP" -f "$f" | grep -q 'architecture: aarch64' || die "$name is not aarch64"
    bad="$(needed "$f" | grep -Evx "$RUNTIME_RE${extra:+|$extra}" | grep -Fvx -f "$W/sonames" || true)"
    [ -z "$bad" ] || die "$name needs $bad"
    ! needed "$f" | grep -q '^libcuda\.so' || die "$name needs libcuda; it must come from L4T at run time only"
    rp="$("$OBJDUMP" -p "$f" | awk '$1 == "RUNPATH" || $1 == "RPATH" {print $2}')"
    case "$rp" in ''|'$ORIGIN/../lib:$ORIGIN/..') ;; *) die "$name has RUNPATH $rp" ;; esac
}

# --- compile ------------------------------------------------------------------

inputs_sha256() {
    (
        cd "$BRAINSYNCOS_ROOT" || exit 1
        find llama -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
        sha256sum scripts/build-llama.sh docker/Dockerfile.cross-builder docker/Dockerfile.llama-builder
    ) | sha256sum | awk '{print $1}'
}

# sums_ok: build/llama is complete and matches its SHA256SUMS.
sums_ok() {
    [ -f "$OUT/llama.env" ] && [ -f "$OUT/SHA256SUMS" ] && (cd "$OUT" && sha256sum -c --quiet --strict SHA256SUMS > /dev/null 2>&1)
}

# cmake_flags LIST: a comma-separated llama.env list, one flag per line.
cmake_flags() { printf '%s\n' "$1" | tr ',' '\n'; }

c_compile() {
    local inputs tar src bcpu bcuda inst t0 cpu_s cuda_s f n want base_a base_b map tc path nvcc
    # The toolchain: the cross one (aarch64-linux-gnu- prefix, toolchain file)
    # on x86-64, the native one on aarch64. Either way the output is aarch64.
    nvcc=/usr/local/cuda-13.2/bin/nvcc
    case "$(uname -m)" in
        x86_64) path=cross-x86_64; tc=aarch64-linux-gnu- ;;
        aarch64) path=native-aarch64; tc="" ;;
        *) die "compile runs in the amd64 cross-builder or the arm64 llama builder (scripts/builder.sh --llama run ...), not on $(uname -m)" ;;
    esac
    [ -x "$nvcc" ] && "$nvcc" --version | grep -q "V$CUDA_NVCC_VERSION\$" \
        || die "compile runs in the amd64 cross-builder (x86-64 hosts) or the arm64 llama builder (scripts/builder.sh --llama run ...): this container has no nvcc $CUDA_NVCC_VERSION"
    require_cmd cmake ninja "${tc}gcc-14" "${tc}g++-14" "${tc}strip" "${tc}objdump" python3 curl
    OBJDUMP="${tc}objdump"
    STRIP="${tc}strip"
    inputs="$(inputs_sha256)"
    if [ "$force" = 0 ] && sums_ok && grep -qx "INPUTS_SHA256=$inputs" "$OUT/llama.env"; then
        log "up to date (${OUT#$BRAINSYNCOS_ROOT/}/llama.env, inputs $inputs)"
        return 0
    fi

    # 1. Source.
    tar="$DOWNLOADS_DIR/$LLAMA_TARBALL"
    fetch "$LLAMA_URL" "$tar" "$LLAMA_SHA256" "$LLAMA_SIZE"
    [ "$(python3 -I -c 'import sys, tarfile; print(tarfile.open(sys.argv[1]).pax_headers.get("comment", ""))' "$tar")" = "$LLAMA_COMMIT" ] \
        || die "$LLAMA_TARBALL is not commit $LLAMA_COMMIT"
    force_rm_tree "$W"
    trap 'force_rm_tree "$W"' EXIT
    src="$W/src"
    mkdir -p "$src"
    tar -xzf "$tar" -C "$src" --strip-components=1 --no-same-owner
    log "source: llama.cpp $LLAMA_VERSION, commit $LLAMA_COMMIT (sha256 $LLAMA_SHA256)"

    # Flags of both builds. Build paths become /usr/src/llama.cpp in debug strings.
    map="-ffile-prefix-map=$W=/usr/src/llama.cpp"
    common=(-G Ninja -S "$src"
        "-DCMAKE_INSTALL_PREFIX=$LLAMA_PREFIX" '-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib;$ORIGIN/..' -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON
        "-DGGML_BACKEND_DIR=$LLAMA_PREFIX/lib/ggml" "-DLLAMA_BUILD_COMMIT=${LLAMA_COMMIT:0:7}"
        "-DLLAMA_BUILD_NUMBER=$LLAMA_BUILD_NUMBER" "-DCMAKE_C_FLAGS=$map" "-DCMAKE_CXX_FLAGS=$map")
    if [ -n "$tc" ]; then
        common+=(--toolchain "$RECIPE/aarch64-linux-gnu.cmake")
    else
        # Native: name the compilers (noble's default gcc is 13, which fails on
        # the SME variants) and the CUDA host compiler, as the toolchain file does.
        common+=(-DCMAKE_C_COMPILER=gcc-14 -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_CUDA_HOST_COMPILER=g++-14)
    fi
    while IFS= read -r f; do common+=("$f"); done < <(cmake_flags "$CMAKE_FLAGS")

    # 2. CPU build.
    bcpu="$W/build-cpu"
    inst="$W/inst-cpu"
    t0="$(date +%s)"
    cmake -B "$bcpu" "${common[@]}" > "$W/cpu-cmake.log" 2>&1 || { tail -30 "$W/cpu-cmake.log" >&2; die "CPU configure failed"; }
    cmake --build "$bcpu" --parallel "${BSOS_JOBS:-1}" > "$W/cpu-build.log" 2>&1 || { tail -40 "$W/cpu-build.log" >&2; die "CPU build failed"; }
    DESTDIR="$inst" cmake --install "$bcpu" > "$W/cpu-install.log" 2>&1 || { tail -20 "$W/cpu-install.log" >&2; die "CPU install failed"; }
    cpu_s=$(( $(date +%s) - t0 ))
    log "CPU build: $(grep -c '^\[' "$W/cpu-build.log" || true) steps with ${BSOS_JOBS:-1} jobs in $cpu_s s ($(grep -ci 'warning:' "$W/cpu-build.log" || true) compiler warnings)"

    TREE="$W/out"
    mkdir -p "$TREE/cpu" "$TREE/cuda"
    cp -a "$inst$LLAMA_PREFIX/." "$TREE/cpu/"
    rm -rf "$TREE/cpu/include" "$TREE/cpu/lib/cmake" "$TREE/cpu/lib/pkgconfig"
    # Unversioned lib*.so links are for linking against the libraries; nothing loads them.
    find "$TREE/cpu/lib" -maxdepth 1 -type l -name 'lib*.so' -delete
    n=0
    for v in $(printf '%s\n' "$CPU_VARIANTS" | tr ',' ' '); do
        [ -f "$TREE/cpu/lib/ggml/libggml-cpu-$v.so" ] || die "the CPU build has no lib/ggml/libggml-cpu-$v.so"
        n=$((n + 1))
    done
    want="$(find "$TREE/cpu/lib/ggml" -name 'libggml-cpu-*.so' | wc -l | tr -d ' ')"
    [ "$want" = "$n" ] || die "lib/ggml has $want CPU backends, CPU_VARIANTS lists $n"
    [ -z "$(find "$TREE/cpu/lib/ggml" -type f ! -name 'libggml-cpu-*.so')" ] || die "lib/ggml holds more than the CPU backends"
    [ -x "$TREE/cpu/bin/llama-server" ] || die "the CPU build has no bin/llama-server"
    elf_files "$TREE/cpu" | while IFS= read -r f; do "$STRIP" --strip-unneeded "$f"; done

    # 3. CUDA build: only the backend, against the same base library.
    bcuda="$W/build-cuda"
    common+=("-DCMAKE_CUDA_COMPILER=$nvcc" "-DCMAKE_CUDA_FLAGS=-Xcompiler=$map")
    while IFS= read -r f; do common+=("$f"); done < <(cmake_flags "$CUDA_CMAKE_FLAGS")
    t0="$(date +%s)"
    cmake -B "$bcuda" "${common[@]}" > "$W/cuda-cmake.log" 2>&1 || { tail -30 "$W/cuda-cmake.log" >&2; die "CUDA configure failed"; }
    cmake --build "$bcuda" --parallel "${BSOS_JOBS:-1}" --target "$CUDA_TARGET" > "$W/cuda-build.log" 2>&1 \
        || { tail -40 "$W/cuda-build.log" >&2; die "CUDA build failed"; }
    cuda_s=$(( $(date +%s) - t0 ))
    log "CUDA build: $(grep -c '\.cu\.o$' "$W/cuda-build.log" || true) CUDA objects with ${BSOS_JOBS:-1} jobs in $cuda_s s ($(grep -ci 'warning' "$W/cuda-build.log" || true) warning lines)"
    f="$(find "$bcuda" -name "lib$CUDA_TARGET.so" -type f | head -n 1)"
    [ -n "$f" ] || die "the CUDA build produced no lib$CUDA_TARGET.so"
    install -m 644 "$f" "$TREE/cuda/lib$CUDA_TARGET.so"
    "$STRIP" --strip-unneeded "$TREE/cuda/lib$CUDA_TARGET.so"
    base_a="$(find "$TREE/cpu/lib" -maxdepth 1 -type f -name 'libggml-base.so.*')"
    base_b="$(find "$bcuda" -type f -name "$(basename "$base_a")" | head -n 1)"
    [ -n "$base_b" ] || die "the CUDA build has no $(basename "$base_a")"
    cp "$base_b" "$W/base-b"
    "$STRIP" --strip-unneeded "$W/base-b"
    cmp -s "$base_a" "$W/base-b" || die "the CUDA build's $(basename "$base_a") differs from the CPU build's"
    log "libggml-base: the same bytes in the CPU and the CUDA build"

    # 4. Check every ELF file.
    find "$TREE/cpu" -type f -print0 | while IFS= read -r -d '' f; do "$OBJDUMP" -p "$f" 2> /dev/null | awk '$1 == "SONAME" {print $2}'; done \
        | LC_ALL=C sort -u > "$W/sonames"
    elf_files "$TREE/cpu" | while IFS= read -r f; do check_elf "$f" ""; done
    check_elf "$TREE/cuda/lib$CUDA_TARGET.so" "$CUDA_RE"
    ! grep -rlq -e "$W" -e /usr/local/cuda "$TREE" || die "a file holds a build path: $(grep -rl -e "$W" -e /usr/local/cuda "$TREE" | head -n 3)"
    install -m 644 "$src/LICENSE" "$TREE/LICENSE"

    # Record and swap in.
    (cd "$TREE" && find . -type f -printf '%P\0' | LC_ALL=C sort -z | xargs -0 sha256sum) > "$W/SHA256SUMS"
    mv "$W/SHA256SUMS" "$TREE/SHA256SUMS"
    {
        echo "# llama.cpp for BrainSyncOS: written by scripts/build-llama.sh compile; plain KEY=value."
        echo "LLAMA_VERSION=$LLAMA_VERSION"
        echo "LLAMA_COMMIT=$LLAMA_COMMIT"
        echo "LLAMA_SHA256=$LLAMA_SHA256"
        echo "LLAMA_PREFIX=$LLAMA_PREFIX"
        echo "INPUTS_SHA256=$inputs"
        echo "SUMS_SHA256=$(sha256_of "$TREE/SHA256SUMS")"
        echo "CMAKE_FLAGS=$CMAKE_FLAGS"
        echo "CUDA_CMAKE_FLAGS=$CUDA_CMAKE_FLAGS"
        echo "CUDA_RUNTIME=$CUDA_RUNTIME"
        echo "TOOLCHAIN=$path,gcc-$("${tc}gcc-14" -dumpfullversion),nvcc-$CUDA_NVCC_VERSION,cmake-$(cmake --version | awk 'NR==1 {print $3}')"
        echo "NEEDED_CPU=$(elf_files "$TREE/cpu" | while IFS= read -r f; do needed "$f"; done | grep -Fvx -f "$W/sonames" | LC_ALL=C sort -u | paste -sd, -)"
        echo "NEEDED_CUDA=$(needed "$TREE/cuda/lib$CUDA_TARGET.so" | grep -Fvx -f "$W/sonames" | LC_ALL=C sort -u | paste -sd, -)"
        echo "GLIBC_MAX=$(find "$TREE" -type f -print0 | xargs -0 "$OBJDUMP" -T 2> /dev/null | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -uV | tail -n 1)"
        echo "GLIBCXX_MAX=$(find "$TREE" -type f -print0 | xargs -0 "$OBJDUMP" -T 2> /dev/null | grep -o 'GLIBCXX_[0-9.]*' | sed 's/GLIBCXX_//' | sort -uV | tail -n 1)"
        echo "CPU_SECONDS=$cpu_s"
        echo "CUDA_SECONDS=$cuda_s"
        echo "BUILD_REV=${BSOS_GIT_REV:-unknown}"
    } > "$TREE/llama.env"
    force_rm_tree "$OUT.new"
    mkdir -p "$(dirname "$OUT")"
    cp -a "$TREE" "$OUT.new"
    force_rm_tree "$OUT"
    mv "$OUT.new" "$OUT"
    log "${OUT#$BRAINSYNCOS_ROOT/}: $(du -sh "$OUT/cpu" | cut -f1) CPU tree, $(file_size "$OUT/cuda/lib$CUDA_TARGET.so") bytes libggml-cuda.so; SHA256SUMS $(sha256_of "$OUT/SHA256SUMS")"
}

# --- check --------------------------------------------------------------------

# wait_http URL: until URL answers 200 (120 s; the model loads slowly under qemu).
wait_http() {
    local _
    for _ in $(seq 1 240); do
        [ "$(curl -s -o /dev/null -w '%{http_code}' "$1")" = 200 ] && return 0
        kill -0 "$SERVER" 2> /dev/null || return 1
        sleep 0.5
    done
    return 1
}

# serve LOG ARG...: llama-server with the test model on 127.0.0.1:$PORT in the background ($SERVER).
serve() {
    local log="$1"
    shift
    "$LLAMA_PREFIX/bin/llama-server" --host 127.0.0.1 --port "$PORT" -m "$MODEL" --api-key-file "$W/api-key" \
        -c 512 -np 1 -t 2 --no-webui --offline -v "$@" > "$log" 2>&1 &   # -v: the "loaded CPU backend" line is logged only verbose
    SERVER=$!
    wait_http "http://127.0.0.1:$PORT/health" || { tail -30 "$log" >&2; die "llama-server did not come up ($log)"; }
}

stop_server() {
    [ -z "${SERVER:-}" ] || { kill "$SERVER" 2> /dev/null || true; wait "$SERVER" 2> /dev/null || true; SERVER=""; }
}

c_check() {
    local sums tok code out ver backend cuda deb x lib miss
    [ "$(uname -m)" = aarch64 ] || die "check runs in the arm64 builder (scripts/builder.sh run ...)"
    require_cmd curl python3 ldd objdump dpkg-deb
    OBJDUMP=objdump
    [ -f "$OUT/llama.env" ] || die "no ${OUT#$BRAINSYNCOS_ROOT/}/llama.env (scripts/build-llama.sh compile)"
    sums_ok || die "${OUT#$BRAINSYNCOS_ROOT/} does not match its SHA256SUMS (rebuild: scripts/build.sh llama --force)"
    sums="$(sha256_of "$OUT/SHA256SUMS")"
    grep -qx "SUMS_SHA256=$sums" "$OUT/llama.env" || die "llama.env does not describe this SHA256SUMS"
    if [ "$force" = 0 ] && [ -f "$OUT/check.env" ] && grep -qx "CHECKED_SUMS_SHA256=$sums" "$OUT/check.env"; then
        log "checked already (${OUT#$BRAINSYNCOS_ROOT/}/check.env)"
        return 0
    fi
    force_rm_tree "$W"
    mkdir -p "$W"
    trap 'stop_server; force_rm_tree "$W"' EXIT
    [ ! -e "$LLAMA_PREFIX" ] || die "$LLAMA_PREFIX exists in this container"
    mkdir -p "$LLAMA_PREFIX"
    cp -a "$OUT/cpu/." "$LLAMA_PREFIX/"
    TREE="$LLAMA_PREFIX"
    find "$TREE" -type f -print0 | while IFS= read -r -d '' f; do objdump -p "$f" 2> /dev/null | awk '$1 == "SONAME" {print $2}'; done \
        | LC_ALL=C sort -u > "$W/sonames"

    # ldd, as installed.
    elf_files "$TREE" > "$W/elves"
    while IFS= read -r f; do
        check_elf "$f" ""
        out="$(ldd "$f" 2>&1)" || die "ldd ${f#"$TREE"/}: $out"
        ! printf '%s\n' "$out" | grep -q 'not found' || die "ldd ${f#"$TREE"/}: $(printf '%s\n' "$out" | grep 'not found')"
    done < "$W/elves"
    log "ldd: all $(wc -l < "$W/elves" | tr -d ' ') ELF files resolve; outside the tree they need $(sed -n 's/^NEEDED_CPU=//p' "$OUT/llama.env")"

    ver="$("$TREE/bin/llama-server" --version 2>&1 | grep -m1 '^version:')" || die "llama-server --version prints no version"
    case "$ver" in "version: $LLAMA_VERSION (build $LLAMA_BUILD_NUMBER, commit ${LLAMA_COMMIT:0:7})") ;; *) die "llama-server --version: '$ver'" ;; esac
    ! "$TREE/bin/llama-server" --version 2>&1 | grep -q -- '-dev' || die "--version says it is a dev build"
    log "llama-server --version: $ver"

    # A few tokens on the CPU, through the API BCI Voice uses.
    MODEL="$DOWNLOADS_DIR/llama-test/$TEST_MODEL_FILE"
    fetch "$TEST_MODEL_URL" "$MODEL" "$TEST_MODEL_SHA256" "$TEST_MODEL_SIZE"
    PORT=19931
    head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$W/api-key"
    serve "$W/server.log"
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/v1/models")"
    [ "$code" = 401 ] || die "/v1/models without the key: HTTP $code, want 401"
    code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer wrong" "http://127.0.0.1:$PORT/v1/models")"
    [ "$code" = 401 ] || die "/v1/models with a wrong key: HTTP $code, want 401"
    curl -sf -H "Authorization: Bearer $(cat "$W/api-key")" "http://127.0.0.1:$PORT/v1/models" > /dev/null || die "/v1/models with the key failed"
    out="$(curl -sf -H "Authorization: Bearer $(cat "$W/api-key")" -H 'Content-Type: application/json' \
        -d '{"prompt": "The capital of France is", "n_predict": 8, "temperature": 0}' "http://127.0.0.1:$PORT/completion")" \
        || die "/completion failed"
    tok="$(printf '%s' "$out" | python3 -I -c 'import json, sys; d = json.load(sys.stdin); print(d["tokens_predicted"]); print(repr(d["content"]), file=sys.stderr)' 2> "$W/content")" \
        || die "/completion answered: $out"
    [ "$tok" = 8 ] && [ "$(cat "$W/content")" != "''" ] || die "/completion gave $tok tokens: $(cat "$W/content")"
    out="$(curl -sf -H "x-api-key: $(cat "$W/api-key")" -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' \
        -d '{"model": "local", "max_tokens": 8, "messages": [{"role": "user", "content": "Say hello."}]}' \
        "http://127.0.0.1:$PORT/v1/messages")" || die "/v1/messages failed"
    printf '%s' "$out" | python3 -I -c 'import json, sys; d = json.load(sys.stdin); assert d["type"] == "message" and d["content"][0]["text"], d' \
        || die "/v1/messages answered: $out"
    stop_server
    backend="$(grep -o "loaded CPU backend from $LLAMA_PREFIX/lib/ggml/libggml-cpu-[a-z0-9._]*\.so" "$W/server.log" | head -n 1 || true)"
    [ -n "$backend" ] || { tail -30 "$W/server.log" >&2; die "llama-server did not load a CPU backend from lib/ggml"; }
    backend="${backend##*/}"
    log "generation: 8 tokens from /completion ($(cat "$W/content")), /v1/messages answers, 401 without the key; $backend"

    # The CUDA backend against NVIDIA's Jetson runtime.
    cuda="$W/cuda"
    mkdir -p "$cuda"
    for deb in "$TEST_CUDART_URL|$TEST_CUDART_SHA256" "$TEST_CUBLAS_URL|$TEST_CUBLAS_SHA256"; do
        x="$DOWNLOADS_DIR/llama-test/$(basename "${deb%%|*}")"
        fetch "${deb%%|*}" "$x" "${deb#*|}"
        dpkg-deb -x "$x" "$cuda"
    done
    lib="$(dirname "$(find "$cuda" -name 'libcudart.so.13' | head -n 1)")"
    [ -f "$lib/libcublas.so.13" ] || die "the Jetson runtime debs hold no libcublas.so.13 next to libcudart.so.13"
    f="$OUT/cuda/libggml-cuda.so"
    check_elf "$f" "$CUDA_RE"
    # ldd where it ships (lib/ggml/): its RUNPATH, $ORIGIN/.., finds libggml-base only there.
    cp "$f" "$TREE/lib/ggml/"
    f="$TREE/lib/ggml/libggml-cuda.so"
    miss="$(ldd "$f" 2>&1 | awk '/not found/ {print $1}' | LC_ALL=C sort | paste -sd' ' -)"
    [ "$miss" = "libcublas.so.13 libcudart.so.13" ] || [ "$miss" = "libcublas.so.13 libcublasLt.so.13 libcudart.so.13" ] \
        || die "without the CUDA runtime, libggml-cuda.so should miss only cudart and cublas, misses: $miss"
    out="$(LD_LIBRARY_PATH="$lib" ldd "$f" 2>&1)"
    ! printf '%s\n' "$out" | grep -q 'not found' || die "ldd libggml-cuda.so with NVIDIA's runtime: $(printf '%s\n' "$out" | grep 'not found')"
    LD_LIBRARY_PATH="$lib" "$TREE/bin/llama-server" --list-devices > "$W/devices.log" 2>&1 \
        || { cat "$W/devices.log" >&2; die "llama-server --list-devices fails with the CUDA backend in lib/ggml"; }
    grep -q 'ggml_cuda_init: failed to initialize CUDA' "$W/devices.log" \
        || { cat "$W/devices.log" >&2; die "the CUDA backend was not loaded and initialised"; }
    LD_LIBRARY_PATH="$lib" serve "$W/server-cuda.log"
    out="$(curl -sf -H "Authorization: Bearer $(cat "$W/api-key")" -H 'Content-Type: application/json' \
        -d '{"prompt": "One, two,", "n_predict": 4, "temperature": 0}' "http://127.0.0.1:$PORT/completion")" \
        || die "/completion failed with the CUDA backend present"
    stop_server
    cuda="$(grep -m1 -o 'ggml_cuda_init: failed to initialize CUDA: [^"]*' "$W/devices.log")"
    log "CUDA backend: ldd resolves it against cuda-cudart-13-2 and libcublas-13-2; loaded with no GPU ($cuda), generation falls back to the CPU"

    {
        echo "# written by scripts/build-llama.sh check; plain KEY=value."
        echo "CHECKED_SUMS_SHA256=$sums"
        echo "VERIFIED_VERSION=$LLAMA_VERSION-b$LLAMA_BUILD_NUMBER-${LLAMA_COMMIT:0:7}"
        echo "VERIFIED_CPU_BACKEND=$backend"
        echo "VERIFIED_TOKENS=$tok"
        echo "VERIFIED_CUDA=loads,no-gpu-fallback"
        echo "CHECK_REV=${BSOS_GIT_REV:-unknown}"
    } > "$OUT/check.env.tmp"
    mv "$OUT/check.env.tmp" "$OUT/check.env"
    log "checked: ${OUT#$BRAINSYNCOS_ROOT/}/check.env"
}

case "$mode" in
    compile) c_compile ;;
    check) c_check ;;
esac
