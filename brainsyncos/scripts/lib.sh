#!/usr/bin/env bash
# Shared helpers for BrainSyncOS scripts. Source it; do not execute it.
# Must stay compatible with macOS /bin/bash 3.2 (host) and bash 5 (builder
# container): no associative arrays, no ${v,,}, no mapfile/readarray, and no
# "${arr[@]}" on possibly-empty arrays under set -u.

# Path/config variables defined here are used by the scripts that source it.
# shellcheck disable=SC2034

set -euo pipefail

BSOS_SCRIPT="${BSOS_SCRIPT:-$(basename "$0")}"

log()  { printf '[bsos:%s] %s\n' "$BSOS_SCRIPT" "$*" >&2; }
warn() { printf '[bsos:%s] WARN: %s\n' "$BSOS_SCRIPT" "$*" >&2; }
die()  { printf '[bsos:%s] ERROR: %s\n' "$BSOS_SCRIPT" "$*" >&2; exit 1; }

# Root of the brainsyncos/ tree (the directory holding config/, scripts/, ...).
if [ -z "${BRAINSYNCOS_ROOT:-}" ]; then
    BRAINSYNCOS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi
export BRAINSYNCOS_ROOT

BSOS_CONFIG="${BSOS_CONFIG:-$BRAINSYNCOS_ROOT/config/base-iso.env}"
BUILD_DIR="$BRAINSYNCOS_ROOT/build"
ISO_ROOT="$BUILD_DIR/iso-root"
META_DIR="$BUILD_DIR/meta"
REPORTS_DIR="$BUILD_DIR/reports"
OUT_DIR="$BUILD_DIR/out"
TMP_DIR="$BUILD_DIR/tmp"

BSOS_REQUIRED_KEYS="BASE_ISO_NAME BASE_ISO_SHA256 BASE_ISO_SIZE BASE_ISO_VOLID
BASE_ISO_DISK_INFO_BUILD BASE_ESP_START_SECTOR BASE_ESP_SIZE_SECTORS
BASE_ESP_SHA256 BASE_SYSTEM_AREA_SHA256 ISO_COMPLIANCE L4T_RELEASE UBUNTU_RELEASE TEGRA_KERNEL LIVE_KERNEL
CASPER_LAYERFS_PATH AUTOINSTALL_LAYER KERNEL_LAYER_MANIFEST BOOT_TARGETS
BUILDER_IMAGE_NAME BUILDER_PLATFORM MIN_FREE_GB"

# Load config/base-iso.env after checking every line is plain KEY=value, so the
# file stays usable as a docker --env-file and sourcing it cannot run code.
load_config() {
    local f="$BSOS_CONFIG" n=0 line key
    [ -r "$f" ] || die "config not found: $f"
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        case "$line" in ''|'#'*) continue ;; esac
        if ! printf '%s\n' "$line" | grep -Eq '^[A-Z][A-Z0-9_]*=[A-Za-z0-9._,:/+@=-]*$'; then
            die "$f:$n: not a plain KEY=value line: $line"
        fi
    done < "$f"
    set -a
    # shellcheck disable=SC1090
    . "$f"
    set +a
    for key in $BSOS_REQUIRED_KEYS; do
        eval "[ -n \"\${$key:-}\" ]" || die "config $f: $key is missing or empty"
    done
}

require_cmd() {
    local c
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"
    done
}

in_container() {
    [ "${BRAINSYNCOS_IN_CONTAINER:-0}" = 1 ] || [ -f /.dockerenv ]
}

require_container() {
    in_container || die "run this inside the builder: scripts/builder.sh run scripts/$BSOS_SCRIPT $*"
}

# Locate the base ISO: $BASE_ISO, else /in/<name> (container), else ~/Downloads/<name>.
resolve_base_iso() {
    local p
    if [ -n "${BASE_ISO:-}" ]; then
        p="$BASE_ISO"
    elif in_container; then
        p="/in/$BASE_ISO_NAME"
    else
        p="$HOME/Downloads/$BASE_ISO_NAME"
    fi
    [ -f "$p" ] || die "base ISO not found at $p (set BASE_ISO=/path/to/$BASE_ISO_NAME)"
    [ -r "$p" ] || die "base ISO not readable: $p"
    printf '%s\n' "$p"
}

# real_path FILE: absolute path with every symlink resolved (bash 3.2 safe;
# macOS has no readlink -f before 12.3).
real_path() {
    local p="$1" l n=0
    while [ -L "$p" ]; do
        n=$((n + 1))
        [ "$n" -le 40 ] || die "too many symlink levels: $1"
        l="$(readlink "$p")"
        case "$l" in
            /*) p="$l" ;;
            *) p="$(dirname "$p")/$l" ;;
        esac
    done
    printf '%s/%s\n' "$(cd "$(dirname "$p")" && pwd -P)" "$(basename "$p")"
}

# file_ident FILE: "inode ctime" (ctime cannot be set back by touch or cp -p).
file_ident() {
    stat -c '%i %Z' "$1" 2>/dev/null || stat -f '%i %c' "$1"
}

file_size() {
    stat -c %s "$1" 2>/dev/null || stat -f %z "$1"
}

file_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# check_free_space DIR [GB]: abort when the filesystem holding DIR has < GB free.
check_free_space() {
    local dir="$1" need="${2:-$MIN_FREE_GB}" avail_kb
    while [ ! -d "$dir" ]; do dir="$(dirname "$dir")"; done
    avail_kb="$(df -Pk "$dir" | awk 'NR==2 {print $4}')"
    [ -n "$avail_kb" ] || die "could not read free space for $dir"
    if [ "$avail_kb" -lt $((need * 1024 * 1024)) ]; then
        die "only $((avail_kb / 1024 / 1024)) GiB free under $dir; need at least $need GiB"
    fi
    log "free space under $dir: $((avail_kb / 1024 / 1024)) GiB (need $need)"
}

# iso_cat ISO PATH: print one file from an ISO (xorriso in the builder, bsdtar on the host).
iso_cat() {
    local iso="$1" path="$2" t
    if command -v xorriso >/dev/null 2>&1; then
        t="$(mktemp)"
        xorriso -osirrox on -indev "$iso" -extract "$path" "$t" >/dev/null 2>&1 \
            || { rm -f "$t"; return 1; }
        cat "$t"
        rm -f "$t"
    elif command -v bsdtar >/dev/null 2>&1; then
        bsdtar -xOf "$iso" "${path#/}"
    else
        die "need xorriso or bsdtar to read files from the ISO"
    fi
}

# Docker Desktop (macOS) keeps host file descriptors open for files that were
# accessed through the bind mount, even after they are deleted, so their space
# is not returned. Truncating first frees the blocks; rm then drops the name.
rm_released() {
    local f
    for f in "$@"; do
        if [ -f "$f" ] && [ ! -L "$f" ]; then
            chmod u+w "$f" 2>/dev/null || true
            : > "$f"
        fi
        rm -f "$f"
    done
}

# Remove a tree that may contain read-only files/dirs (ISO extractions are r-x),
# truncating large files first (see rm_released).
force_rm_tree() {
    [ -e "$1" ] || [ -L "$1" ] || return 0
    chmod -R u+w "$1" 2>/dev/null || true
    find "$1" -type f -size +1024k -exec sh -c 'for f; do : > "$f"; done' _ {} + 2>/dev/null || true
    rm -rf "$1"
}

# tree_listing DIR: "type size path[ -> target]" per node, sorted by path (GNU find).
# Directory sizes are filesystem-specific, so they are printed as 0.
tree_listing() {
    (cd "$1" && find . -mindepth 1 \
        \( -type l -printf 'l 0 %P -> %l\n' \) -o \
        \( -type d -printf 'd 0 %P\n' \) -o \
        -printf '%y %s %P\n') | LC_ALL=C sort -k3
}

# usage_from_header: print the leading comment block of the calling script.
usage_from_header() {
    sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "$0"
}
