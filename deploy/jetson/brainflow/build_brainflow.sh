#!/usr/bin/env bash
# Build the Cerelog fork of BrainFlow for Linux (aimed at the Jetson Orin Nano, JetPack 6,
# Ubuntu 22.04 aarch64) and package its Python binding as a wheel.
#
# Runs the same way natively on the Jetson and inside the Docker image from ./Dockerfile.
#
#   ./build_brainflow.sh            # clone if needed, patch, build, wheel, verify
#
# Environment (all optional):
#   SRC        pristine clone of the fork, at $COMMIT          (default: <script dir>/src)
#   BUILD_SRC  scratch copy that gets patched and built        (default: <script dir>/build-src)
#   OUT        where the wheel goes                            (default: <script dir>/out)
#   PYTHON     interpreter used to build the wheel             (default: python3.12, else python3)
#   VERIFY_PYTHONS  interpreters to install+test the wheel in   (default: $PYTHON)
#   SKIP_VERIFY=1   build only
#   JOBS       parallel compile jobs                           (default: nproc)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="https://github.com/shakimiansky/Shared_brainflow-cerelog"
COMMIT="932b3944bfe6c0f4e29a46b4a34afdeba39edd85"
SRC="${SRC:-$HERE/src}"
BUILD_SRC="${BUILD_SRC:-$HERE/build-src}"
OUT="${OUT:-$HERE/out}"
JOBS="${JOBS:-$(nproc)}"
PATCHES=("$HERE/cerelog-serial-timeout.patch" "$HERE/setuptools-pin.patch")

if [[ -z "${PYTHON:-}" ]]; then
    if command -v python3.12 >/dev/null; then PYTHON=python3.12; else PYTHON=python3; fi
fi

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || die "this builds Linux .so files; on a Mac use the Dockerfile (INSTALL.md)"
ARCH="$(uname -m)"
case "$ARCH" in
    aarch64) SYNCHRONI_LIBS=(libSynchroniLib_arm64.so libsensor_arm64.so) ;;
    x86_64)  SYNCHRONI_LIBS=(libSynchroniLib_x64.so libsensor_x64.so) ;;
    *) die "unsupported architecture $ARCH" ;;
esac
# Same set as the macOS dylibs vendored in third_party/brainflow-cerelog/brainflow/lib
# (default CMake options: no BLE, no Bluetooth, no ONNX), plus the Synchroni SDK the
# wrapper links against, which macOS gets as a framework.
EXPECTED_LIBS=(libBoardController.so libDataHandler.so libMLModule.so libGanglionLib.so
               libMuseLib.so "${SYNCHRONI_LIBS[@]}")
WHEEL_PLAT="${WHEEL_PLAT:-linux_$ARCH}"

for p in "${PATCHES[@]}"; do [[ -f "$p" ]] || die "missing patch $p"; done
for tool in git patch cmake make g++ patchelf readelf file "$PYTHON"; do
    command -v "$tool" >/dev/null || die "$tool not found (see INSTALL.md for the apt packages)"
done

# 1. pristine source at the pinned commit
# -c safe.directory: in Docker the bind-mounted clone belongs to another uid.
gsrc() { git -c safe.directory="$SRC" -C "$SRC" "$@"; }
if [[ ! -d "$SRC/.git" ]]; then
    log "cloning $REPO_URL into $SRC"
    git clone "$REPO_URL" "$SRC"
    gsrc checkout --detach "$COMMIT"
fi
head="$(gsrc rev-parse HEAD)"
[[ "$head" == "$COMMIT" ]] || die "$SRC is at $head, expected $COMMIT"
[[ -z "$(gsrc status --porcelain)" ]] || die "$SRC has local changes; it must stay pristine"

# 2. fresh copy of exactly that commit, then the patches (the clone is never touched)
log "exporting $COMMIT to $BUILD_SRC and applying patches"
rm -rf "$BUILD_SRC"
mkdir -p "$BUILD_SRC"
gsrc archive "$COMMIT" | tar -x -C "$BUILD_SRC"
for p in "${PATCHES[@]}"; do
    # patch, not git apply: git apply inside someone else's work tree resolves paths
    # against that repository's root.
    patch -p1 --forward --batch --no-backup-if-mismatch -d "$BUILD_SRC" < "$p"
done
grep -q 'set_serial_port_settings (1000, false)' "$BUILD_SRC/src/board_controller/cerelog/cerelog.cpp"

# 3. native libraries. Options as the fork's Ubuntu CI minus BLE/Bluetooth/ONNX/tests,
# matching the vendored macOS build. Each target's POST_BUILD step copies its .so into
# python_package/brainflow/lib.
log "configuring and building ($JOBS jobs)"
cmake -S "$BUILD_SRC" -B "$BUILD_SRC/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBRAINFLOW_VERSION=0.0.1 \
    -DCMAKE_INSTALL_PREFIX="$BUILD_SRC/installed"
cmake --build "$BUILD_SRC/build" -j "$JOBS"

LIB="$BUILD_SRC/python_package/brainflow/lib"
for f in "${EXPECTED_LIBS[@]}"; do [[ -f "$LIB/$f" ]] || die "missing $LIB/$f after the build"; done
# CMake also copies vendor SDKs prebuilt for other CPUs (ANT Neuro libeego-SDK.so and g.tec
# libunicorn.so are x86-64, libunicorn_raspberry.so is 32-bit ARM). They could never load
# here and only serve those boards, so drop anything not built for this machine.
want="$(readelf -h "$LIB/libBoardController.so" | grep -m1 'Machine:')"
for f in "$LIB"/*.so; do
    if [[ "$(readelf -h "$f" | grep -m1 'Machine:')" != "$want" ]]; then
        echo "dropping $(basename "$f"): $(file -b "$f" | cut -d, -f1-2)"
        rm -f "$f"
    fi
done
# CMake gives libs linked against a non-system .so (the Synchroni wrapper -> prebuilt SDK)
# a RUNPATH into this build tree. Point those at their own directory instead, so the
# package works wherever pip installs it. Libs without a RUNPATH are left alone.
for f in "$LIB"/*.so; do
    rp="$(patchelf --print-rpath "$f")"
    if [[ -n "$rp" && "$rp" != '$ORIGIN' ]]; then
        echo "rpath $(basename "$f"): $rp -> \$ORIGIN"
        patchelf --set-rpath '$ORIGIN' "$f"
    fi
done
log "native libraries"
ls -l "$LIB"

# 4. wheel. setup.py has no extensions, so setuptools tags it py3-none-any; retag it for
# this platform because it ships .so files.
log "building the wheel with $PYTHON"
VENV="$BUILD_SRC/.wheel-venv"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip "setuptools<81" "wheel>=0.42"
rm -rf "$BUILD_SRC/dist"
"$VENV/bin/python" -m pip wheel --no-build-isolation --no-deps -w "$BUILD_SRC/dist" \
    "$BUILD_SRC/python_package"
"$VENV/bin/python" -m wheel tags --remove --python-tag py3 --abi-tag none \
    --platform-tag "$WHEEL_PLAT" "$BUILD_SRC"/dist/brainflow-*-py3-none-any.whl
mkdir -p "$OUT"
rm -f "$OUT"/brainflow-*.whl
cp "$BUILD_SRC"/dist/brainflow-*-py3-none-"$WHEEL_PLAT".whl "$OUT"/
WHEEL="$(ls "$OUT"/brainflow-*-py3-none-"$WHEEL_PLAT".whl)"
log "wheel: $WHEEL"
"$VENV/bin/python" -m zipfile -l "$WHEEL" | grep -E '\.so|WHEEL' || true

# 5. install it into clean venvs and exercise it
if [[ "${SKIP_VERIFY:-0}" != 1 ]]; then
    for py in ${VERIFY_PYTHONS:-$PYTHON}; do
        "$HERE/verify_wheel.sh" "$WHEEL" "$py"
    done
fi
