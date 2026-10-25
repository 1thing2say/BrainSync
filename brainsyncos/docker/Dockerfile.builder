# BrainSyncOS builder: the arm64 toolbox every in-container build step runs in.
# Only the base image is pinned (manifest-list digest, ubuntu:24.04 as of
# 2026-10-02). apt packages come from the live noble/noble-updates pockets:
# snapshot.ubuntu.com does not serve ubuntu-ports (401), and exact version pins
# break once -updates moves on. The installed versions are recorded in
# /usr/share/brainsyncos/builder-packages.txt (copied to build/meta/ on
# extraction) so drift is visible; bump deliberately and re-run tests/test_phase1.sh.
# Phase 3c compiles in here: git, cmake and g++ for the BrainFlow fork
# (scripts/build-wheels.sh), make and kmod for the ch341 module against NVIDIA's
# kernel headers, udev for `udevadm verify`, and python3-venv, -numpy and
# -setuptools to install and import the wheel in a test venv.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xorriso squashfs-tools mtools fdisk gdisk dosfstools \
      cpio zstd lz4 xz-utils initramfs-tools-core \
      rsync file bsdextrautils gnupg apt-utils faketime \
      python3 python3-yaml \
      grub-common openssh-client openssl \
      ca-certificates curl git patch cmake g++ make kmod udev \
      python3-venv python3-numpy python3-setuptools \
      qemu-user \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/share/brainsyncos \
 && dpkg-query -W -f '${Package} ${Version}\n' > /usr/share/brainsyncos/builder-packages.txt

WORKDIR /work
