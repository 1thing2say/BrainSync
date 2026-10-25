#!/usr/bin/env bash
# pack-llama-release.sh - standalone llama.cpp archives for the release page.
#
# Usage: scripts/pack-llama-release.sh [--out DIR]
#
# Packs build/llama/ (scripts/build-llama.sh compile, then check) into
# archives that work outside BrainSyncOS, on any aarch64 Ubuntu 24.04 or
# JetPack 7.2 system (glibc >= GLIBC_MAX in build/llama/llama.env):
#   llama.cpp-VERSION-bBUILD-linux-aarch64-cpu.tar.xz
#       llama-server, llama-cli and the other tools, with every arm64 CPU
#       variant in lib/ggml/; llama.cpp picks the best one at start (on an
#       Orin Nano's Cortex-A78AE an armv8.2 one)
#   llama.cpp-VERSION-bBUILD-linux-aarch64-cuda-sm87.tar.xz
#       the same plus lib/ggml/libggml-cuda.so for the Orin's GPU (sm_87).
#       Only when build/llama has the CUDA part (PARTS=cpu,cuda). It needs
#       NVIDIA's CUDA 13.2 runtime (cuda-cudart-13-2, libcublas-13-2) and
#       libcuda from L4T R39.2; it bundles none of NVIDIA's files.
# Both unpack to llama/, meant for /opt/brainsyncos (the backends are found
# in /opt/brainsyncos/llama/lib/ggml, the path compiled in):
#   sudo mkdir -p /opt/brainsyncos && sudo tar -xJf FILE -C /opt/brainsyncos
# Each holds llama.cpp's LICENSE (MIT) and a README with the build's pins.
# Output in build/release/llama/ (--out), with SHA256SUMS. The archives are
# reproducible: sorted, owner 0, every mtime the llama.cpp release's own.
# Runs on the host (GNU tar and xz; on macOS use gtar) or in either builder.
# It compiles nothing.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

out="$BUILD_DIR/release/llama"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --out) out="${2:?--out needs a directory}"; shift 2 ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
TAR=tar
command -v gtar > /dev/null 2>&1 && TAR=gtar
"$TAR" --version 2> /dev/null | grep -q 'GNU tar' || die "needs GNU tar (macOS: brew install gnu-tar)"
require_cmd xz python3

L="$BUILD_DIR/llama"
[ -f "$L/llama.env" ] && [ -f "$L/check.env" ] || die "no checked build/llama (scripts/build.sh llama)"
load_env_file "$L/llama.env" "LLAMA_VERSION LLAMA_COMMIT LLAMA_SHA256 SUMS_SHA256 PARTS CMAKE_FLAGS CUDA_CMAKE_FLAGS CUDA_RUNTIME TOOLCHAIN GLIBC_MAX"
load_env_file "$BRAINSYNCOS_ROOT/llama/llama.env" "LLAMA_BUILD_NUMBER LLAMA_TARBALL CPU_VARIANTS"
(cd "$L" && sha256sum -c --quiet --strict SHA256SUMS > /dev/null 2>&1) || die "build/llama does not match its SHA256SUMS"
[ "$(sha256_of "$L/SHA256SUMS")" = "$SUMS_SHA256" ] || die "build/llama/llama.env does not describe its SHA256SUMS"
grep -qx "CHECKED_SUMS_SHA256=$SUMS_SHA256" "$L/check.env" || die "build/llama was not checked as it is now (scripts/build-llama.sh check)"

# Every mtime: the newest file time in llama.cpp's own release tarball.
src="$DOWNLOADS_DIR/$LLAMA_TARBALL"
[ -f "$src" ] || die "no $src (scripts/build-llama.sh compile fetches it)"
epoch="$(python3 -I -c 'import sys, tarfile; print(max(m.mtime for m in tarfile.open(sys.argv[1])))' "$src")"
stem="llama.cpp-$LLAMA_VERSION-b$LLAMA_BUILD_NUMBER-linux-aarch64"

W="$(mktemp -d)"
trap 'force_rm_tree "$W"' EXIT

# pack NAME WITH_CUDA: stage llama/ and write NAME.tar.xz.
pack() {
    local name="$1" cuda="$2" s="$W/$1"
    mkdir -p "$s/llama"
    cp -a "$L/cpu/." "$s/llama/"
    [ "$cuda" = 0 ] || install -m 644 "$L/cuda/libggml-cuda.so" "$s/llama/lib/ggml/libggml-cuda.so"
    install -m 644 "$L/LICENSE" "$s/llama/LICENSE"
    {
        echo "llama.cpp $LLAMA_VERSION (build $LLAMA_BUILD_NUMBER, commit $LLAMA_COMMIT)"
        echo "Built by BrainSyncOS (github.com/1thing2say/BrainSync, brainsyncos/) for aarch64."
        echo
        echo "Source:     llama.cpp release tarball, sha256 $LLAMA_SHA256"
        echo "Toolchain:  $TOOLCHAIN"
        echo "CMake:      $CMAKE_FLAGS" | tr ',' ' '
        echo "CPU:        $(printf '%s' "$CPU_VARIANTS" | tr ',' ' '); the best match is loaded at start"
        if [ "$cuda" = 1 ]; then
            echo "CUDA:       $CUDA_CMAKE_FLAGS" | tr ',' ' '
            echo "Needs:      $CUDA_RUNTIME (NVIDIA's packages), libcuda from L4T R39.2 (JetPack 7.2)" | tr ',' ' '
        fi
        echo "glibc:      $GLIBC_MAX or newer (Ubuntu 24.04, JetPack 7.2)"
        echo
        echo "Install:    sudo mkdir -p /opt/brainsyncos && sudo tar -xJf $name.tar.xz -C /opt/brainsyncos"
        echo "            The backends are found in /opt/brainsyncos/llama/lib/ggml (compiled in)."
        echo "Run:        /opt/brainsyncos/llama/bin/llama-server -m MODEL.gguf --host 127.0.0.1"
        echo "Licence:    LICENSE (MIT)."
    } > "$s/llama/README.txt"
    find "$s" -exec touch -h -d "@$epoch" {} +
    "$TAR" --sort=name --mtime="@$epoch" --owner=0 --group=0 --numeric-owner \
        --format=gnu -C "$s" -cf - llama | xz -9e -T1 > "$W/$name.tar.xz"
    log "$name.tar.xz: $(file_size "$W/$name.tar.xz") bytes"
}

pack "$stem-cpu" 0
case "$PARTS" in
    cpu,cuda) pack "$stem-cuda-sm87" 1 ;;
    cpu) log "no CUDA part in build/llama (PARTS=cpu): the cuda-sm87 archive is not made" ;;
    *) die "build/llama/llama.env has PARTS=$PARTS" ;;
esac

force_rm_tree "$out"
mkdir -p "$out"
cp "$W"/*.tar.xz "$out/"
(cd "$out" && sha256sum -- *.tar.xz > SHA256SUMS)
log "${out#$BRAINSYNCOS_ROOT/}: $(wc -l < "$out/SHA256SUMS" | tr -d ' ') archives, SHA256SUMS $(sha256_of "$out/SHA256SUMS")"
