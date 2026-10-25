#!/bin/bash
# 80-cuda.sh - NVIDIA's CUDA 13.2 runtime (cudart and cuBLAS) on the Jetson.
#
# Runs inside the installed system (hook.sh, curtin in-target), after
# 60-apt-repo.sh, with the ISO's package repository at $BSOS_REPO_DIR.
# Jetson editions only: it installs cuda-cudart-13-2 and libcublas-13-2,
# which the repository mirrors unchanged from NVIDIA's Jetson repository
# (mirror/nvidia-jetson, pinned by sha256), the same way 70 and 75 install
# (lib/apt-repo.sh: that repository alone, offline). Their closure is the
# three cuda-toolkit config-common packages: they set the /usr/local/cuda
# alternatives and add /usr/local/cuda{,-13}/targets/sbsa-linux/lib to
# /etc/ld.so.conf.d. libcuda.so.1 is not part of it: L4T's
# nvidia-l4t-cuda-nvgpu has it, picked at boot by nv-load-gpu-libs.service.
# The step then checks that ldconfig resolves libcudart.so.13,
# libcublas.so.13 and libcublasLt.so.13 under /usr/local/cuda-13.2, that
# their own libraries are all found, and that no dGPU driver package
# (libnvidia-compute, nvidia-driver-*, cuda-drivers, ...) is installed.
# Nothing here runs CUDA code: libcuda is not set up in the installer's chroot.
# The VM edition (vm-*) has no NVIDIA GPU and skips the step.

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck source=payload/lib/apt-repo.sh
. "$d/lib/apt-repo.sh"
# shellcheck disable=SC1091  # written by hook.sh
. "$d/device.env"
edition="${BSOS_EDITION:-}"
case "$edition" in
    '') echo "80-cuda.sh: no BSOS_EDITION in $d/device.env" >&2; exit 1 ;;
    vm-*) echo "80-cuda.sh: $edition edition: skipped (no NVIDIA GPU, no CUDA)"; exit 0 ;;
esac

fail() { echo "80-cuda.sh: $*" >&2; exit 1; }
pkgs=(cuda-cudart-13-2 libcublas-13-2)
before="$(dpkg-query -W -f '${Package} ${Status}\n' | awk '/ installed$/ {print $1}' | LC_ALL=C sort)"
bsos_repo_open
bsos_apt install "${pkgs[@]}"
after="$(dpkg-query -W -f '${Package} ${Status}\n' | awk '/ installed$/ {print $1}' | LC_ALL=C sort)"
new=""
for p in $after; do
    printf '%s\n' "$before" | grep -qxF "$p" || new="$new $p"
done

# The dGPU stack would replace L4T's libcuda: none of it may be installed.
bad="$(printf '%s\n' "$after" | grep -E '^(libnvidia-compute|libnvidia-gl|nvidia-driver-|nvidia-kernel-|nvidia-dkms-|cuda-drivers|cuda-compat|libcuda1)' || true)"
[ -z "$bad" ] || fail "dGPU driver packages are installed: $(printf '%s\n' "$bad" | tr '\n' ' ')"

ldconfig
for lib in libcudart.so.13 libcublas.so.13 libcublasLt.so.13; do
    path="$(ldconfig -p | awk -v l="$lib" '$1 == l && /AArch64/ {print $NF; exit}')"
    [ -n "$path" ] || fail "ldconfig does not resolve $lib"
    real="$(readlink -f "$path")"
    case "$real" in /usr/local/cuda-13.2/targets/sbsa-linux/lib/*) ;; *) fail "$lib resolves to $real, not CUDA 13.2's" ;; esac
    missing="$(ldd "$real" 2>&1 | grep 'not found' || true)"
    [ -z "$missing" ] || fail "$lib needs libraries this system lacks: $missing"
    echo "80-cuda.sh: $lib -> $real"
done
[ "$(readlink -f /usr/local/cuda)" = /usr/local/cuda-13.2 ] || fail "/usr/local/cuda does not point to /usr/local/cuda-13.2"
echo "80-cuda.sh: installed $(dpkg-query -W -f '${Package} ${Version}\n' "${pkgs[@]}" | awk '{printf "%s%s", s, $0; s = ", "}'); new packages:${new:- none}"
