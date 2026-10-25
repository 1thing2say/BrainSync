# BrainSyncOS: thin wrapper around scripts/build.sh (which does the work and
# also runs without make). EDITION is jetson (default) or vm.
#
#   make                  doctor, wheels, llama, packages, mirror, repo and the installer ISO
#   make doctor           check what the build needs
#   make key              create the package repository signing key (once)
#   make wheels           the BrainFlow Cerelog wheel (build/wheels/)
#   make llama            llama.cpp for aarch64 (build/llama/)
#   make packages mirror repo iso   (mirror: NVIDIA's CUDA debs, Jetson)
#   make test             tests/test_phase3.sh (TESTS="phase2 vm" for others)
#   make clean            delete build/out
#   make EDITION=vm       the same for the UTM edition

EDITION ?= jetson
TESTS ?= phase3
BUILD := scripts/build.sh

.PHONY: all doctor key wheels llama packages mirror repo iso test clean

all:
	$(BUILD) all --edition $(EDITION)

doctor key packages mirror repo iso:
	$(BUILD) $@ --edition $(EDITION)

wheels:
	$(BUILD) wheels

llama:
	$(BUILD) llama

test:
	$(BUILD) test $(TESTS)

clean:
	$(BUILD) clean
