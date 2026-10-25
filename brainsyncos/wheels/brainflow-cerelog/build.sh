#!/usr/bin/env bash
# build.sh - brainflow-cerelog: the Cerelog X8 fork of BrainFlow as an aarch64 wheel.
#
# Run by scripts/build-wheels.sh (builder only; see its header for the BSOS_*
# variables). Steps, all pinned by wheel.env:
#   1. the fork at SOURCE_COMMIT, fetched without credentials into a cached
#      partial clone (build/downloads/brainflow-cerelog.git, sparse: none of
#      the 300 MB of vendor binaries and other language bindings), its tree
#      checked against SOURCE_TREE; a rebuild with the cache works offline
#   2. patches/ applied, each checked against its sha256 in PATCHES
#   3. CMake with CMAKE_FLAGS, then only CMAKE_TARGETS (libBoardController,
#      libDataHandler, libMLModule), in a fixed /scratch path so the build is
#      the same every time
#   4. a wheel (scripts/pack-wheel.py) with brainflow/*.py and the three
#      stripped libraries, nothing else: the fork's lib/ folder also collects
#      vendor blobs for other CPUs (x86-64 libunicorn and eego, 32-bit ARM),
#      which must never ship
#   5. the libraries checked (aarch64 ELF, only libc/libm/libstdc++/libgcc_s
#      needed), then the wheel installed with pip into a fresh venv
#      (--system-site-packages for numpy and pkg_resources) where
#      BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD) must print 250
# Writes the wheel and manifest.env (provenance for the manifest) to
# BSOS_WHEEL_OUT.

BSOS_SCRIPT=brainflow-cerelog
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
require_container
load_env_file wheel.env "WHEEL_NAME SOURCE_URL SOURCE_COMMIT SOURCE_TREE PATCHES BRAINFLOW_VERSION CMAKE_FLAGS CMAKE_TARGETS WHEEL_DIST WHEEL_VERSION WHEEL_TAG"
require_cmd git patch cmake make g++ strip objdump file python3
out="${BSOS_WHEEL_OUT:?}"
here="$(pwd -P)"

# Fixed paths: the libraries keep no trace of a per-run directory.
w=/scratch/brainflow-cerelog
force_rm_tree "$w"
mkdir -p "$w"
trap 'force_rm_tree "$w"' EXIT

# git as root on a checkout another uid owns (the bind-mounted build/).
export GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
    GIT_CONFIG_KEY_1=core.autocrlf GIT_CONFIG_VALUE_1=false GIT_TERMINAL_PROMPT=0

# Paths of the fork left out: prebuilt onnxruntime (265 MB), the Synchroni and
# NeuroSDK blobs (46 MB), the other language bindings, docs, CI. CMake needs
# the rest, including cpp_package/ and the 280 KB of onnxruntime headers.
SKIP="third_party/onnxruntime/runtimes third_party/synchroni third_party/neurosdk java_package csharp_package
julia_package matlab_package nodejs_package r_package rust_package docs emulator Docker .github"

# 1. Source.
cache="$DOWNLOADS_DIR/$WHEEL_NAME.git"
if [ ! -d "$cache/.git" ]; then
    force_rm_tree "$cache"
    mkdir -p "$cache"
    git -C "$cache" init -q
fi
git -C "$cache" config core.sparseCheckout true
git -C "$cache" config core.sparseCheckoutCone false
{ echo '/*'; for p in $SKIP; do echo "!/$p/"; done; } > "$cache/.git/info/sparse-checkout"
if ! git -C "$cache" cat-file -e "$SOURCE_COMMIT^{commit}" 2>/dev/null; then
    log "fetching $SOURCE_URL at $SOURCE_COMMIT (no credentials, blobs on demand)"
    git -C "$cache" -c credential.helper= fetch -q --depth 1 --filter=blob:none "$SOURCE_URL" "$SOURCE_COMMIT" \
        || die "cannot fetch $SOURCE_COMMIT from $SOURCE_URL"
else
    log "source: cached in ${cache#$BRAINSYNCOS_ROOT/}"
fi
tree="$(git -C "$cache" rev-parse "$SOURCE_COMMIT^{tree}")"
[ "$tree" = "$SOURCE_TREE" ] || die "commit $SOURCE_COMMIT has tree $tree, wheel.env pins $SOURCE_TREE"
# The checkout fetches the sparse paths' blobs in one batch (a no-op once cached).
git -C "$cache" -c advice.detachedHead=false checkout -q --force "$SOURCE_COMMIT" || die "checkout of $SOURCE_COMMIT failed"
excl=""
for p in $SKIP; do excl="$excl :(exclude)$p"; done
mkdir -p "$w/src"
# shellcheck disable=SC2086  # one pathspec per word
git -C "$cache" archive --format=tar "$SOURCE_COMMIT" -- . $excl | tar -xf - -C "$w/src"
src_epoch="$(git -C "$cache" log -1 --format=%ct "$SOURCE_COMMIT")"
log "source: $SOURCE_COMMIT (tree $tree), $(du -sh "$w/src" | cut -f1)"

# 2. Patches.
for entry in $(printf '%s\n' "$PATCHES" | tr ',' ' '); do
    name="${entry%%:*}"
    want="${entry#*:}"
    f="$here/patches/$name"
    [ -f "$f" ] || die "patch $name is listed in PATCHES but not in patches/"
    [ "$(sha256_of "$f")" = "$want" ] || die "patches/$name does not match its sha256 in PATCHES"
    (cd "$w/src" && patch -p1 --batch --forward --no-backup-if-mismatch < "$f") > "$w/patch.log" 2>&1 \
        || { cat "$w/patch.log" >&2; die "patch $name does not apply"; }
    log "applied $name"
done
n="$(find "$here/patches" -name '*.patch' | wc -l | tr -d ' ')"
[ "$n" = "$(printf '%s\n' "$PATCHES" | tr ',' '\n' | grep -c .)" ] || die "patches/ has $n patches, PATCHES lists a different number"

# 3. Build.
t0="$(date +%s)"
# shellcheck disable=SC2046  # one flag per word
cmake -S "$w/src" -B "$w/src/build" $(printf '%s\n' "$CMAKE_FLAGS" | tr ',' ' ') > "$w/cmake.log" 2>&1 \
    || { tail -30 "$w/cmake.log" >&2; die "cmake configure failed"; }
# shellcheck disable=SC2046
cmake --build "$w/src/build" --parallel "${BSOS_JOBS:-1}" --target $(printf '%s\n' "$CMAKE_TARGETS" | tr ',' ' ') \
    > "$w/make.log" 2>&1 || { tail -40 "$w/make.log" >&2; die "build failed"; }
log "built $CMAKE_TARGETS with ${BSOS_JOBS:-1} jobs in $(( $(date +%s) - t0 )) s ($(grep -ci 'warning:' "$w/make.log" || true) compiler warnings)"

# 4. Stage and pack.
st="$w/stage"
mkdir -p "$st/brainflow/lib"
for f in "$w/src/python_package/brainflow"/*.py; do
    install -m 644 "$f" "$st/brainflow/"
done
libs=""
for t in $(printf '%s\n' "$CMAKE_TARGETS" | tr ',' ' '); do
    lib="lib$t.so"
    [ -f "$w/src/compiled/$lib" ] || die "the build did not produce compiled/$lib"
    install -m 644 "$w/src/compiled/$lib" "$st/brainflow/lib/$lib"
    strip --strip-unneeded "$st/brainflow/lib/$lib"
    libs="$libs $lib"
done

# 5a. The libraries: aarch64, and nothing but the C/C++ runtime underneath.
for lib in $libs; do
    f="$st/brainflow/lib/$lib"
    file -b "$f" | grep -q '^ELF 64-bit LSB shared object, ARM aarch64' || die "$lib is not an aarch64 shared object: $(file -b "$f")"
    bad="$(objdump -p "$f" | awk '$1 == "NEEDED" {print $2}' \
        | grep -Evx 'libstdc\+\+\.so\.6|libm\.so\.6|libgcc_s\.so\.1|libc\.so\.6|ld-linux-aarch64\.so\.1' || true)"
    [ -z "$bad" ] || die "$lib needs more than the C/C++ runtime: $bad"
    ! grep -q "$w" "$f" || die "$lib contains the build path $w"
    log "$lib: $(file_size "$f") bytes, needs $(objdump -p "$f" | awk '$1 == "NEEDED" {print $2}' | tr '\n' ' ')"
done
extra="$(cd "$st" && find . -type f ! -path './brainflow/*.py' ! -path './brainflow/lib/lib*.so')"
[ -z "$extra" ] || die "unexpected files staged: $extra"

mkdir -p "$w/dist"
whl="$(python3 -I "$BRAINSYNCOS_ROOT/scripts/pack-wheel.py" --root "$st" --name "$WHEEL_DIST" --version "$WHEEL_VERSION" \
    --tag "$WHEEL_TAG" --out "$w/dist" --summary "BrainFlow, Cerelog X8 fork (commit ${SOURCE_COMMIT:0:7}), aarch64 build" \
    --license MIT --license-file "$w/src/python_package/LICENSE.txt" --requires "$here/requires.txt" --epoch "$src_epoch")"

# 5b. Install it the way the runtime venv will, and ask it for the Cerelog X8.
python3 -m venv --system-site-packages "$w/venv"
"$w/venv/bin/pip" install -q --no-index --no-deps --disable-pip-version-check "$whl" > "$w/pip.log" 2>&1 \
    || { cat "$w/pip.log" >&2; die "pip cannot install $(basename "$whl")"; }
check="$(cd / && "$w/venv/bin/python" -I -W ignore -c 'from brainflow.board_shim import BoardShim, BoardIds
from brainflow.data_filter import DataFilter
from brainflow.ml_model import MLModel
print(BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD), BoardShim.get_version(), DataFilter.get_version(), MLModel.get_version())')" \
    || die "the installed wheel does not import"
log "installed wheel: sampling rate, BoardShim, DataFilter and MLModel versions: $check"
[ "$check" = "250 $BRAINFLOW_VERSION $BRAINFLOW_VERSION $BRAINFLOW_VERSION" ] \
    || die "want '250 $BRAINFLOW_VERSION $BRAINFLOW_VERSION $BRAINFLOW_VERSION', got '$check'"

cp "$whl" "$out/"
cat > "$out/manifest.env" <<EOF
WHEEL_DIST=$WHEEL_DIST
WHEEL_VERSION=$WHEEL_VERSION
WHEEL_TAG=$WHEEL_TAG
SOURCE_URL=$SOURCE_URL
SOURCE_COMMIT=$SOURCE_COMMIT
SOURCE_TREE=$SOURCE_TREE
PATCHES=$PATCHES
CMAKE_FLAGS=$CMAKE_FLAGS
CMAKE_TARGETS=$CMAKE_TARGETS
TOOLCHAIN=gcc-$(gcc -dumpfullversion),cmake-$(cmake --version | awk 'NR==1 {print $3}'),python-$(python3 -c 'import platform; print(platform.python_version())')
VERIFIED_SAMPLING_RATE=${check%% *}
EOF
