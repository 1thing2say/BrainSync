# aarch64-linux-gnu.cmake - CMake toolchain for llama.cpp in the cross-builder
# (docker/Dockerfile.cross-builder; scripts/build-llama.sh compile).
# Ubuntu noble's gcc-14 cross compilers build for arm64 Ubuntu 24.04 (glibc
# 2.39), with /usr/aarch64-linux-gnu as the sysroot. CUDA's target files are
# NVIDIA's sbsa-linux tree, which CMake's CUDAToolkit would otherwise look
# for under targets/aarch64-linux (the old Jetson layout).
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

set(CMAKE_C_COMPILER aarch64-linux-gnu-gcc-14)
set(CMAKE_CXX_COMPILER aarch64-linux-gnu-g++-14)
set(CMAKE_CUDA_HOST_COMPILER aarch64-linux-gnu-g++-14)
set(CUDAToolkit_TARGET_NAME sbsa-linux)

set(CMAKE_FIND_ROOT_PATH /usr/aarch64-linux-gnu /usr/local/cuda-13.2/targets/sbsa-linux)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
