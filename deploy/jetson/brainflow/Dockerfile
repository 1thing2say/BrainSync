# Toolchain image for building the Cerelog BrainFlow fork for the Jetson Orin Nano
# (JetPack 6 = Ubuntu 22.04 aarch64, glibc 2.35). The source and scripts are bind-mounted
# at /work at run time, see INSTALL.md.
#
#   docker build --platform linux/arm64 -t brainflow-cerelog-jetson .
#   docker run --rm --platform linux/arm64 -v "$PWD":/work brainflow-cerelog-jetson /work/build_brainflow.sh
FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

# build-essential/cmake/git: the native build (cmake 3.22 >= the fork's 3.13 minimum).
# patchelf: sets $ORIGIN rpaths so the libs find each other inside brainflow/lib.
# python3 (3.10, the JetPack default) is kept to check that a stock pip accepts the wheel;
# python3.12 from deadsnakes is what the brainsync venv uses (needs Python >= 3.11).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates file binutils patchelf \
      software-properties-common gpg-agent \
      python3 python3-venv \
 && add-apt-repository -y ppa:deadsnakes/ppa \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work
