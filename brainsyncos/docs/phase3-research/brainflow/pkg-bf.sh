#!/bin/bash
# Throwaway-container script: build BrainFlow natives and stage a python3 deb in /out.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
t0=$(date +%s)
apt-get update -qq
apt-get install -y -qq --no-install-recommends cmake g++ make python3 binutils file >/dev/null
t1=$(date +%s); echo "deps install: $((t1-t0))s"
cp -a /src /work; mkdir -p /work/build && cd /work/build
cmake -DCMAKE_BUILD_TYPE=Release -DBRAINFLOW_VERSION=5.0.0 -DBUILD_SYNCHRONI_SDK=OFF -DBUILD_BLE=OFF \
  -DBUILD_BLUETOOTH=OFF -DBUILD_ONNX=OFF -DBUILD_TESTS=OFF .. >/dev/null
t2=$(date +%s); echo "configure: $((t2-t1))s"
make -j"$(nproc)" BoardController DataHandler MLModule >/dev/null 2>&1
t3=$(date +%s); echo "build BoardController+DataHandler+MLModule (nproc=$(nproc)): $((t3-t2))s"
P=/pkg/python3-brainflow-cerelog; D=$P/usr/lib/python3/dist-packages
mkdir -p $D/brainflow/lib $P/DEBIAN
# copy python sources only (no lib/ blobs, no pycache)
(cd /work/python_package && find brainflow -name '*.py' -print0 | xargs -0 -I{} install -D -m0644 {} $D/{})
for l in libBoardController.so libDataHandler.so libMLModule.so; do
  install -m0644 /work/compiled/$l $D/brainflow/lib/; strip --strip-unneeded $D/brainflow/lib/$l
done
cat > $P/DEBIAN/control <<C
Package: python3-brainflow-cerelog
Version: 5.0.0+cerelog932b394-1
Architecture: arm64
Maintainer: BrainSyncOS <noreply@example.invalid>
Depends: python3, python3-numpy, python3-pkg-resources, libc6, libstdc++6, libgcc-s1
Section: python
Priority: optional
Description: BrainFlow (Cerelog X8 fork) Python binding with native libs
C
dpkg-deb --root-owner-group -Zxz --build $P /out/ >/dev/null
ls -la /out/*.deb; ls -la $D/brainflow/lib; du -sh $D/brainflow
