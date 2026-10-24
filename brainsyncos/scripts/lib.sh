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
BSOS_PRODUCT_CONFIG="${BSOS_PRODUCT_CONFIG:-$BRAINSYNCOS_ROOT/config/brainsyncos.env}"
BUILD_DIR="$BRAINSYNCOS_ROOT/build"
ISO_ROOT="$BUILD_DIR/iso-root"
META_DIR="$BUILD_DIR/meta"
REPORTS_DIR="$BUILD_DIR/reports"
OUT_DIR="$BUILD_DIR/out"
TMP_DIR="$BUILD_DIR/tmp"
STAGE_DIR="$BUILD_DIR/stage"

BSOS_REQUIRED_KEYS="BASE_ISO_NAME BASE_ISO_SHA256 BASE_ISO_SIZE BASE_ISO_VOLID
BASE_ISO_DISK_INFO_BUILD BASE_ESP_START_SECTOR BASE_ESP_SIZE_SECTORS
BASE_ESP_SHA256 BASE_SYSTEM_AREA_SHA256 ISO_COMPLIANCE L4T_RELEASE UBUNTU_RELEASE TEGRA_KERNEL LIVE_KERNEL
CASPER_LAYERFS_PATH AUTOINSTALL_LAYER KERNEL_LAYER_MANIFEST BOOT_TARGETS
BUILDER_IMAGE_NAME BUILDER_PLATFORM MIN_FREE_GB"

BSOS_PRODUCT_REQUIRED_KEYS="BRAINSYNCOS_VERSION TARGET_BOARD HOOKED_AUTOINSTALL
INSTALL_TARGET_DEFAULT GRUB_TIMEOUT"

# load_env_file FILE KEYS: source FILE after checking every line is plain
# KEY=value (so it stays usable as a docker --env-file and sourcing it cannot
# run code), then require each of KEYS to be non-empty.
load_env_file() {
    local f="$1" keys="$2" n=0 line key
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
    for key in $keys; do
        eval "[ -n \"\${$key:-}\" ]" || die "config $f: $key is missing or empty"
    done
}

# config/base-iso.env: the pinned vendor ISO.
load_config() { load_env_file "$BSOS_CONFIG" "$BSOS_REQUIRED_KEYS"; }

# config/brainsyncos.env: what we build on top of it (after load_config).
load_product_config() {
    load_env_file "$BSOS_PRODUCT_CONFIG" "$BSOS_PRODUCT_REQUIRED_KEYS"
    printf '%s\n' "$BRAINSYNCOS_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
        || die "BRAINSYNCOS_VERSION must be MAJOR.MINOR.PATCH, got $BRAINSYNCOS_VERSION"
    case "$INSTALL_TARGET_DEFAULT" in
        nvme|microsd|usb) ;;
        *) die "INSTALL_TARGET_DEFAULT must be nvme, microsd or usb, got $INSTALL_TARGET_DEFAULT" ;;
    esac
    printf '%s\n' "$GRUB_TIMEOUT" | grep -Eq '^(-1|[0-9]+)$' \
        || die "GRUB_TIMEOUT must be -1 or a number of seconds, got $GRUB_TIMEOUT"
}

# product_iso_name: basename (no .iso) of the build-iso.sh output, e.g.
# brainsyncos-0.1.0-orin-nano for TARGET_BOARD=jetson-orin-nano-devkit.
product_iso_name() {
    local board="${TARGET_BOARD#jetson-}"
    printf 'brainsyncos-%s-%s\n' "$BRAINSYNCOS_VERSION" "${board%-devkit}"
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

# check_base_boot_bytes ISO: the replay copies the ESP and the system area
# (MBR + GPT) from the base file itself, so check those bytes against the pins
# in config/base-iso.env rather than trusting the sha256 cache.
check_base_boot_bytes() {
    local iso="$1" esp_sum sa_sum
    log "checking the base ISO boot bytes against the pinned hashes"
    esp_sum="$(dd if="$iso" bs=4M iflag=skip_bytes,count_bytes skip=$((BASE_ESP_START_SECTOR * 512)) \
        count=$((BASE_ESP_SIZE_SECTORS * 512)) 2>/dev/null | sha256sum | awk '{print $1}')"
    [ "$esp_sum" = "$BASE_ESP_SHA256" ] || die "base ISO ESP sha256 is $esp_sum, expected $BASE_ESP_SHA256"
    sa_sum="$(dd if="$iso" bs=2048 count=16 2>/dev/null | sha256sum | awk '{print $1}')"
    [ "$sa_sum" = "$BASE_SYSTEM_AREA_SHA256" ] || die "base ISO system area sha256 is $sa_sum, expected $BASE_SYSTEM_AREA_SHA256"
}

# write_replayed_iso BASE PARTIAL XLOG XORRISO_TREE_ARGS...: write a new image
# to PARTIAL that replays BASE's boot setup (protective MBR, GPT, El Torito
# UEFI entry, appended ESP copied byte-for-byte), with the ISO tree changed by
# the given xorriso commands (e.g. -update_r DIR / or -map DIR /).
# -compliance reproduces NVIDIA's ISO9660/Joliet names (replay does not),
# -padding included keeps the Gap1 partition, -joliet on keeps Joliet.
write_replayed_iso() {
    local base="$1" partial="$2" xlog="$3"
    shift 3
    if ! xorriso -indev "$base" -outdev "$partial" \
            -joliet on \
            -compliance "$ISO_COMPLIANCE" \
            -boot_image any replay \
            -padding included \
            "$@" \
            -commit > "$xlog" 2>&1; then
        tail -20 "$xlog" >&2
        die "xorriso failed (log: $xlog)"
    fi
    grep -q 'Replayed [0-9]* boot related commands' "$xlog" || die "xorriso did not replay the boot setup (log: $xlog)"
}

# finalize_iso PARTIAL OUT: truncate PARTIAL to the image size (xorriso pads
# stdio output to 32 blocks; the backup GPT must sit at the very end of the
# file), check the partition table, move it to OUT and write OUT.sha256.
finalize_iso() {
    local partial="$1" out="$2" sectors vlog
    vlog="$partial.sfdisk-V"
    sectors="$(xorriso -indev "$partial" -report_system_area plain 2>/dev/null | awk '/ISO image size\/512/ {print $NF}')"
    [ -n "$sectors" ] || die "could not read the image size of $partial"
    truncate -s $((sectors * 512)) "$partial"
    sfdisk -V "$partial" > "$vlog" 2>&1 || true
    if ! grep -q 'No errors detected' "$vlog"; then
        cat "$vlog" >&2; rm_released "$vlog"
        die "partition table of $partial is not valid"
    fi
    rm_released "$vlog"
    mv "$partial" "$out"
    (cd "$(dirname "$out")" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
    log "done: $out ($(file_size "$out") bytes, sha256 $(cut -d' ' -f1 "$out.sha256"))"
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
