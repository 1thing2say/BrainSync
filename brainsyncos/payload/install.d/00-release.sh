#!/bin/bash
# 00-release.sh - record the installed BrainSyncOS build in /etc/brainsyncos-release.
#
# Runs inside the installed system (hook.sh, curtin in-target). The file is the
# build's release file (KEY=value, written by scripts/build-iso.sh) plus
# INSTALLED=<UTC time>. /etc/os-release is left alone: it stays Ubuntu's, which
# apt and NVIDIA's tools expect.

set -euo pipefail

src="${BSOS_INSTALL_DIR:?}/release"
[ -s "$src" ] || { echo "00-release.sh: missing $src" >&2; exit 1; }
if grep -Evq '^[A-Z][A-Z0-9_]*=[A-Za-z0-9._,:/+@-]*$' "$src"; then
    echo "00-release.sh: malformed $src" >&2
    exit 1
fi

tmp=/etc/.brainsyncos-release.tmp
{
    cat "$src"
    printf 'INSTALLED=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" /etc/brainsyncos-release
cat /etc/brainsyncos-release
