#!/bin/bash
# Throwaway-container script: build ch341.ko out of tree against NVIDIA's L4T R39.2.1 kernel headers.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
t0=$(date +%s)
apt-get update -qq
apt-get install -y -qq --no-install-recommends gcc-13 make libc6-dev libelf1t64 kmod file binutils >/dev/null
ln -sf /usr/bin/gcc-13 /usr/bin/gcc
KDIR=/hdr/usr/src/linux-headers-6.8.12-1021-tegra-ubuntu24.04_aarch64/3rdparty/canonical/linux-noble
cp -a /ch341src /work
t1=$(date +%s)
make -C "$KDIR" M=/work modules 2>&1 | tail -8
t2=$(date +%s); echo "deps: $((t1-t0))s  build: $((t2-t1))s"
ls -la /work/ch341.ko; file /work/ch341.ko
modinfo /work/ch341.ko | grep -E "vermagic|alias|depends|name|sig"
cp /work/ch341.ko /out/ch341.ko
