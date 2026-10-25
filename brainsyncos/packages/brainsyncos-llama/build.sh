#!/usr/bin/env bash
# build.sh - brainsyncos-llama and brainsyncos-llama-cuda: llama.cpp's
# llama-server as BrainSync's local LLM.
#
# Run by scripts/build-packages.sh (builder only; see its header for the
# BSOS_* variables). It compiles nothing: it packages build/llama/, which
# scripts/build.sh llama compiled in the cross-builder and checked in the
# arm64 builder (scripts/build-llama.sh), and refuses an output whose files
# or check do not match. Both packages put their files in LLAMA_PREFIX
# (/opt/brainsyncos/llama, llama/llama.env); RUNPATH finds the libraries.
#   brainsyncos-llama (both editions, the same bytes in each): bin/, lib/ and
#     the CPU backends in lib/ggml/, the unit brainsyncos-llama.service
#     (this folder), /etc/brainsyncos/llama.d/ for a model package's
#     model.conf, and the licence. Needs only the C/C++ runtime.
#   brainsyncos-llama-cuda (jetson edition): lib/ggml/libggml-cuda.so and the
#     unit drop-in 50-cuda.conf. Depends on exactly the CUDA runtime it was
#     built against (CUDA_RUNTIME: NVIDIA's cuda-cudart-13-2 and
#     libcublas-13-2, which the repository mirrors from NVIDIA's Jetson
#     repository), never on libcuda.so.1: L4T's nvidia-l4t-cuda-nvgpu has it,
#     and a Depends could make apt pick the dGPU driver instead.
# The maintainer scripts reload systemd and restart a running service only
# on a running system, never in the installer's chroot. The install step
# 85-llama.sh enables the service and makes the API key.

BSOS_SCRIPT=brainsyncos-llama
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
load_packages_config
load_env_file "$BRAINSYNCOS_ROOT/llama/llama.env" "LLAMA_VERSION LLAMA_PREFIX CUDA_RUNTIME CUDA_TARGET"
in="$BUILD_DIR/llama"
ver="${BSOS_PKG_VERSION:?}"

# The checked build.
[ -f "$in/llama.env" ] && [ -f "$in/check.env" ] \
    || die "no checked llama.cpp build in ${in#$BRAINSYNCOS_ROOT/} (scripts/build.sh llama)"
(cd "$in" && sha256sum -c --quiet --strict SHA256SUMS) || die "${in#$BRAINSYNCOS_ROOT/} does not match its SHA256SUMS"
sums="$(sha256_of "$in/SHA256SUMS")"
grep -qx "SUMS_SHA256=$sums" "$in/llama.env" && grep -qx "CHECKED_SUMS_SHA256=$sums" "$in/check.env" \
    || die "${in#$BRAINSYNCOS_ROOT/} was not checked as it is now (scripts/build.sh llama)"
grep -qx "LLAMA_VERSION=$LLAMA_VERSION" "$in/llama.env" || die "${in#$BRAINSYNCOS_ROOT/} is not llama.cpp $LLAMA_VERSION"
glibc="$(sed -n 's/^GLIBC_MAX=//p' "$in/llama.env")"
[ -n "$glibc" ] || die "llama.env has no GLIBC_MAX"

s="$(scratch_dir llama-stage)"
trap 'force_rm_tree "$s"' EXIT

# maint_scripts DIR UNIT_ACTION: postinst and prerm for DIR's package.
maint_scripts() {
    cat > "$1/DEBIAN/postinst" <<'SH'
#!/bin/sh
# Reload systemd and restart the local LLM if it runs; only on a running
# system (not in a chroot such as the installer's).
set -e
if [ "$1" = configure ] && [ -d /run/systemd/system ] \
    && ! { command -v systemd-detect-virt > /dev/null 2>&1 && systemd-detect-virt -q --chroot; }; then
    systemctl daemon-reload || true
    systemctl try-restart brainsyncos-llama.service || true
fi
SH
    if [ "$2" = disable ]; then
        cat > "$1/DEBIAN/prerm" <<'SH'
#!/bin/sh
# Stop and disable the local LLM before its files go.
set -e
if [ "$1" = remove ]; then
    if [ -d /run/systemd/system ] && ! { command -v systemd-detect-virt > /dev/null 2>&1 && systemd-detect-virt -q --chroot; }; then
        systemctl stop brainsyncos-llama.service || true
    fi
    systemctl --no-reload disable brainsyncos-llama.service > /dev/null 2>&1 || true
fi
SH
    else
        cat > "$1/DEBIAN/postrm" <<'SH'
#!/bin/sh
# The CPU backend takes over: restart the local LLM if it runs.
set -e
if [ "$1" = remove ] && [ -d /run/systemd/system ] \
    && ! { command -v systemd-detect-virt > /dev/null 2>&1 && systemd-detect-virt -q --chroot; }; then
    systemctl daemon-reload || true
    systemctl try-restart brainsyncos-llama.service || true
fi
SH
    fi
    find "$1/DEBIAN" -type f -name 'p*' -exec chmod 755 {} +
}

# 1. brainsyncos-llama.
p="$s/llama"
doc="$p/usr/share/doc/brainsyncos-llama"
mkdir -p "$p/DEBIAN" "$p$LLAMA_PREFIX" "$p/usr/lib/systemd/system" "$p/etc/brainsyncos/llama.d" "$doc"
cp -a "$in/cpu/." "$p$LLAMA_PREFIX/"
install -m 644 brainsyncos-llama.service "$p/usr/lib/systemd/system/brainsyncos-llama.service"
install -m 644 "$in/LICENSE" "$doc/copyright"
grep -v '^#' "$in/llama.env" > "$doc/build.env"
chmod 644 "$doc/build.env"
cat > "$doc/README" <<EOF
llama.cpp $LLAMA_VERSION (llama-server and its tools) in $LLAMA_PREFIX, built
by BrainSyncOS for arm64 with every CPU variant as a loadable backend
(lib/ggml/). build.env says how it was built.

brainsyncos-llama.service runs llama-server on 127.0.0.1:9931 for BrainSync's
BCI Voice (its "local" LLM backend). It starts only once a model package is
installed. Such a package provides:
  /opt/brainsyncos/models/NAME.gguf        the model
  /etc/brainsyncos/llama.d/model.conf      plain KEY=value lines:
    BSOS_LLAMA_MODEL=/opt/brainsyncos/models/NAME.gguf   (required)
    BSOS_LLAMA_ALIAS=NAME             the name the API reports (default local)
    BSOS_LLAMA_CTX_SIZE=N             context length (default 8192)
and starts or restarts the service on a running system.

The API key is made once per device at install time, in
/etc/brainsyncos/llama/api-key (root only). BrainSync reads the same key from
/etc/brainsync/secrets.env (BRAINSYNC_LLAMA_API_KEY). /health answers without
it. Try it:
  curl -H "Authorization: Bearer \$(sudo cat /etc/brainsyncos/llama/api-key)" http://127.0.0.1:9931/v1/models

The service is sandboxed: a dynamic user, no network but 127.0.0.1, a
read-only system, an empty environment (only its command line counts). Its
settings are in /usr/lib/systemd/system/brainsyncos-llama.service; change
them with a drop-in (systemctl edit brainsyncos-llama). llama.cpp is MIT
licensed (copyright).
EOF
chmod 644 "$doc/README"
maint_scripts "$p" disable
cat > "$p/DEBIAN/control" <<EOF
Package: brainsyncos-llama
Version: $ver
Architecture: arm64
Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>
Installed-Size: $(installed_size "$p")
Section: utils
Priority: optional
Depends: libc6 (>= $glibc), libgcc-s1, libstdc++6 (>= 14)
Suggests: brainsyncos-llama-cuda
Description: local LLM server for BrainSync (llama.cpp $LLAMA_VERSION)
 llama.cpp's llama-server and tools in $LLAMA_PREFIX, with CPU backends for
 every arm64 level, and brainsyncos-llama.service, which serves a model on
 127.0.0.1:9931 behind an API key once a model package is installed.
EOF
build_deb "$p" "${BSOS_PKG_OUT:?}" > /dev/null

# 2. brainsyncos-llama-cuda, for the Jetson edition only.
[ "${BSOS_EDITION:?}" = jetson ] || exit 0
c="$s/cuda"
doc="$c/usr/share/doc/brainsyncos-llama-cuda"
mkdir -p "$c/DEBIAN" "$c$LLAMA_PREFIX/lib/ggml" "$c/usr/lib/systemd/system/brainsyncos-llama.service.d" "$doc"
install -m 644 "$in/cuda/lib$CUDA_TARGET.so" "$c$LLAMA_PREFIX/lib/ggml/lib$CUDA_TARGET.so"
install -m 644 50-cuda.conf "$c/usr/lib/systemd/system/brainsyncos-llama.service.d/50-cuda.conf"
install -m 644 "$in/LICENSE" "$doc/copyright"
runtime=""
for d in $(printf '%s\n' "$CUDA_RUNTIME" | tr ',' ' '); do
    runtime="$runtime, ${d%%=*} (= ${d#*=})"
done
runtime="${runtime#, }"
cat > "$doc/README" <<EOF
The CUDA backend of llama.cpp $LLAMA_VERSION for the Jetson Orin's GPU
(sm_87, CUDA 13.2), in $LLAMA_PREFIX/lib/ggml/. llama-server loads it when it
starts and falls back to the CPU backends when it cannot (no GPU, no
libcuda). It needs NVIDIA's CUDA runtime ($runtime)
and libcuda.so.1 from L4T. 50-cuda.conf gives the service the GPU's device nodes.
EOF
chmod 644 "$doc/README"
maint_scripts "$c" restart
cat > "$c/DEBIAN/control" <<EOF
Package: brainsyncos-llama-cuda
Version: $ver
Architecture: arm64
Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>
Installed-Size: $(installed_size "$c")
Section: utils
Priority: optional
Depends: brainsyncos-llama (= $ver), $runtime
Description: CUDA backend of BrainSync's local LLM server (Jetson)
 libggml-cuda.so for llama.cpp $LLAMA_VERSION in $LLAMA_PREFIX, built for the
 Jetson Orin (sm_87) against CUDA 13.2, and the service settings the GPU
 needs.
EOF
build_deb "$c" "${BSOS_PKG_OUT:?}" > /dev/null
