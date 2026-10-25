#!/usr/bin/env bash
# build.sh - espeak-ng: Ubuntu's espeak-ng command, the offline voice.
#
# Run by scripts/build-packages.sh (builder only; see its header for the
# BSOS_* variables). brainsync/voice.py speaks with Cartesia, and with
# espeak-ng when Cartesia cannot (no key, no network). NVIDIA's install puts
# espeak-ng's library and data on the Jetson from the ISO's pool
# (libespeak-ng1, espeak-ng-data, libpcaudio0, libsonic0), but not the
# command itself, which is in Ubuntu's universe. This build does not rebuild
# it: it fetches Ubuntu's own .deb (DEB_URL in package.env, cached in
# build/downloads/), checks it against DEB_SHA256, its name, version and
# architecture, and puts it in the repository unchanged. The pin is the only
# trust anchor here, as for pip in python/pip.env. GPL-3+; the source is
# Ubuntu's espeak-ng 1.51+dfsg-12build1 source package.

BSOS_SCRIPT=espeak-ng
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
load_packages_config
require_cmd dpkg-deb python3
load_env_file package.env "DEB_VERSION DEB_URL DEB_SHA256"

f="$DOWNLOADS_DIR/${DEB_URL##*/}"
if [ ! -f "$f" ] || [ "$(sha256_of "$f")" != "$DEB_SHA256" ]; then
    mkdir -p "$DOWNLOADS_DIR"
    log "fetching ${DEB_URL##*/}"
    python3 -c 'import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=60) as r, open(sys.argv[2], "wb") as f:
    f.write(r.read())' "$DEB_URL" "$f.part" || die "cannot download $DEB_URL"
    [ "$(sha256_of "$f.part")" = "$DEB_SHA256" ] || { rm -f "$f.part"; die "sha256 mismatch: $DEB_URL"; }
    mv "$f.part" "$f"
fi
[ "$(dpkg-deb -f "$f" Package)" = espeak-ng ] && [ "$(dpkg-deb -f "$f" Version)" = "$DEB_VERSION" ] \
    && [ "$(dpkg-deb -f "$f" Architecture)" = arm64 ] || die "${f##*/} is not espeak-ng $DEB_VERSION for arm64"
cp "$f" "${BSOS_PKG_OUT:?}/espeak-ng_${DEB_VERSION}_arm64.deb"
log "espeak-ng $DEB_VERSION: Ubuntu's .deb, sha256 $DEB_SHA256"
