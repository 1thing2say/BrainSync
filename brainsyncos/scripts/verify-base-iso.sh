#!/usr/bin/env bash
# verify-base-iso.sh - check the user-supplied base ISO against config/base-iso.env.
#
# Usage: scripts/verify-base-iso.sh [--iso PATH] [--no-cache]
#
# Checks size, ISO9660 volume id, sha256, /.disk/info (Ubuntu release + build)
# and that the kernel layer manifest lists nvidia-l4t-kernel $TEGRA_KERNEL.
# Runs in the builder or on the host (shasum/bsdtar fallbacks). A passing sha256
# is cached in build/meta/base-iso.verified keyed on name, size, mtime, inode
# and ctime; pass --no-cache to always re-hash. Exits non-zero with the failing check named.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config

iso=""
use_cache=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --iso) iso="${2:?--iso needs a path}"; shift 2 ;;
        --no-cache) use_cache=0; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[ -n "$iso" ] || iso="$(resolve_base_iso)"
[ -f "$iso" ] || die "ISO not found: $iso"

fail() { die "verify FAILED [$1]: $2"; }

# 1. size (cheap, first)
size="$(file_size "$iso")"
[ "$size" = "$BASE_ISO_SIZE" ] || fail size "expected $BASE_ISO_SIZE bytes, got $size ($iso)"
log "ok size: $size bytes"

# 2. ISO9660 primary volume descriptor at LBA 16: "CD001" at +1, volume id at +40..+71
pvd="$(dd if="$iso" bs=2048 skip=16 count=1 2>/dev/null | dd bs=1 skip=1 count=71 2>/dev/null | tr -d '\000')"
magic="$(printf '%s' "$pvd" | cut -c1-5)"
[ "$magic" = "CD001" ] || fail volid "no ISO9660 primary volume descriptor (got '$magic')"
volid="$(dd if="$iso" bs=2048 skip=16 count=1 2>/dev/null | dd bs=1 skip=40 count=32 2>/dev/null | tr -d '\000' | sed 's/ *$//')"
[ "$volid" = "$BASE_ISO_VOLID" ] || fail volid "expected '$BASE_ISO_VOLID', got '$volid'"
log "ok volume id: $volid"

# 3. sha256 (cached). The key includes inode and ctime, which touch/cp -p
# cannot forge, so a different file with the same name/size/mtime re-hashes.
# One line per environment: host and container see different inode numbers.
cache="$META_DIR/base-iso.verified"
key="$(basename "$iso") $size $(file_mtime "$iso") $(file_ident "$iso") $BASE_ISO_SHA256"
if [ "$use_cache" = 1 ] && [ -f "$cache" ] && grep -qxF "$key" "$cache"; then
    log "ok sha256: $BASE_ISO_SHA256 (cached; --no-cache to re-hash)"
else
    log "hashing $iso (about 5 GB) ..."
    sum="$(sha256_of "$iso")"
    [ "$sum" = "$BASE_ISO_SHA256" ] || fail sha256 "expected $BASE_ISO_SHA256, got $sum"
    log "ok sha256: $sum"
    mkdir -p "$META_DIR"
    { [ -f "$cache" ] && grep -vxF "$key" "$cache" | tail -3; printf '%s\n' "$key"; } > "$cache.tmp" || true
    mv "$cache.tmp" "$cache"
fi

# 4. /.disk/info
info="$(iso_cat "$iso" /.disk/info)" || fail disk-info "cannot read /.disk/info"
case "$info" in
    *"Ubuntu-Server $UBUNTU_RELEASE LTS"*"($BASE_ISO_DISK_INFO_BUILD)"*) ;;
    *) fail disk-info "expected 'Ubuntu-Server $UBUNTU_RELEASE LTS ... ($BASE_ISO_DISK_INFO_BUILD)', got '$info'" ;;
esac
log "ok .disk/info: $info"

# 5. kernel layer manifest (lines are "[+]package<TAB>version")
manifest="$(iso_cat "$iso" "/$KERNEL_LAYER_MANIFEST")" || fail kernel "cannot read /$KERNEL_LAYER_MANIFEST"
kver="$(printf '%s\n' "$manifest" | awk '{sub(/^\+/, "", $1)} $1 == "nvidia-l4t-kernel" {print $2; exit}')"
[ -n "$kver" ] || fail kernel "nvidia-l4t-kernel not listed in /$KERNEL_LAYER_MANIFEST"
[ "$kver" = "$TEGRA_KERNEL" ] || fail kernel "nvidia-l4t-kernel is $kver, expected $TEGRA_KERNEL"
log "ok kernel layer: nvidia-l4t-kernel $kver"

log "PASS: $iso is the pinned base (L4T $L4T_RELEASE, Ubuntu $UBUNTU_RELEASE)"
