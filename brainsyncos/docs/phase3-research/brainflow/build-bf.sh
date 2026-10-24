#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
t0=$(date +%s)
apt-get update -qq
apt-get install -y -qq --no-install-recommends cmake g++ make python3 python3-setuptools python3-numpy python3-pkg-resources ca-certificates >/dev/null
t1=$(date +%s); echo "deps install: $((t1-t0))s"
dpkg-query -W cmake g++ gcc python3 python3-setuptools python3-numpy python3-pkg-resources | sed 's/^/ver: /'
cp -a /src /work
mkdir -p /work/build && cd /work/build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/bf -DBRAINFLOW_VERSION=5.0.0 \
  -DBUILD_SYNCHRONI_SDK=OFF -DBUILD_BLE=OFF -DBUILD_BLUETOOTH=OFF -DBUILD_ONNX=OFF -DBUILD_OYMOTION_SDK=OFF \
  -DBUILD_TESTS=OFF -DUSE_LIBFTDI=OFF -DUSE_OPENMP=OFF -DBUILD_PERIPHERY=OFF .. > /out/cmake-config.log 2>&1
t2=$(date +%s); echo "configure: $((t2-t1))s"
make -j"$(nproc)" > /out/make.log 2>&1 || { tail -50 /out/make.log; exit 1; }
make install > /out/install.log 2>&1
t3=$(date +%s); echo "build (nproc=$(nproc)): $((t3-t2))s"
echo "== compiled/"; ls -la /work/compiled
echo "== python lib/"; ls -la /work/python_package/brainflow/lib
echo "== install tree"; find /opt/bf -type f | sort
for f in /work/compiled/*.so; do echo "== $f"; file "$f" 2>/dev/null || true; objdump -p "$f" | grep -E "NEEDED|SONAME"; done
cp -a /work/compiled /out/compiled
cp -a /work/python_package /out/python_package
