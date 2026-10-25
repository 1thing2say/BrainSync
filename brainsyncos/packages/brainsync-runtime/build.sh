#!/usr/bin/env bash
# build.sh - brainsync-runtime: the Python venv the BrainSync apps run in.
#
# Run by scripts/build-packages.sh (builder only; see its header for the
# BSOS_* variables). The package holds /opt/brainsyncos/venv, built at that
# path by scripts/venv.sh from the hash-locked wheels of
# python/brainsync.lock.txt (plus any local wheels, python/local-wheels.txt),
# without the app itself: packages/brainsync adds the app's wheel to the same
# venv, so an app change does not re-ship the runtime. The venv uses the
# system's python3.12 (the cp312 ABI); manylinux wheels bring their own
# libraries, so it needs only libc6, libgcc-s1, libstdc++6 and zlib1g. The
# display apps (PsychoPy and pyglet, X11 and GLX only) also load the GL and X
# libraries listed as Recommends: NVIDIA's install puts them on the Jetson; on
# the VM they come with a desktop. /usr/share/doc/brainsync-runtime/wheels.txt
# names every wheel with its sha256.

BSOS_SCRIPT=brainsync-runtime
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
source "$BRAINSYNCOS_ROOT/scripts/venv.sh"
load_packages_config

venv_build
# packages/brainsync installs the app into this venv only for as long as it
# stages it; nothing of it may be here now.
if ls -d "$VENV_SITE"/brainsync-*.dist-info "$VENV_SITE/brainsync" "$VENV_SITE/app" "$VENV_SITE/tools" > /dev/null 2>&1; then
    die "the venv holds BrainSync app files; they belong to the brainsync package"
fi

# Stage on the venv's own filesystem with hard links: no copy of ~450 MB.
s=/opt/brainsyncos/.stage-runtime
force_rm_tree "$s"
trap 'force_rm_tree "$s"' EXIT
doc="$s/usr/share/doc/brainsync-runtime"
mkdir -p "$s/DEBIAN" "$s/opt/brainsyncos" "$doc"
cp -al "$VENV_DIR" "$s/opt/brainsyncos/venv"
cp "$VENV_WHEELS" "$doc/wheels.txt"
cp "$PY_LOCK" "$doc/brainsync.lock.txt"
cat > "$doc/README" <<EOF
The Python runtime of the BrainSync apps: a venv at /opt/brainsyncos/venv
($("$SYSTEM_PY" -c 'import platform; print(platform.python_version())'), the system's python3.12), built by BrainSyncOS from
hash-locked wheels. wheels.txt names each installed wheel with its sha256;
brainsync.lock.txt is the lock it was installed from (sha256
$(sha256_of "$PY_LOCK")).
PsychoPy is installed without the dependencies the apps do not use, so
pip check lists those as missing; that is expected.
EOF
chmod 644 "$doc"/*
installed_kb="$(installed_size "$s")"
cat > "$s/DEBIAN/control" <<EOF
Package: brainsync-runtime
Version: ${BSOS_PKG_VERSION:?}
Architecture: arm64
Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>
Installed-Size: $installed_kb
Section: python
Priority: optional
Depends: python3.12 (>= 3.12.3), python3.12 (<< 3.13), libc6, libgcc-s1, libstdc++6, zlib1g
Recommends: libgl1, libglu1-mesa, libx11-6, libxext6, libxinerama1, libxxf86vm1, libxi6, libfontconfig1, libfreetype6
Description: Python runtime of the BrainSync apps
 A Python 3.12 venv at /opt/brainsyncos/venv with the locked third-party
 packages the BrainSync apps need: numpy, scipy, matplotlib, PsychoPy,
 FastAPI, uvicorn, anthropic and the rest ($(wc -l < "$VENV_WHEELS" | tr -d ' ') wheels).
 The apps themselves are in the brainsync package.
EOF
build_deb "$s" "${BSOS_PKG_OUT:?}" > /dev/null
