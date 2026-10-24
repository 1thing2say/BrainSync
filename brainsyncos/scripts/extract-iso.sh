#!/usr/bin/env bash
# extract-iso.sh - unpack the verified base ISO into build/iso-root/ (builder only).
#
# Usage: scripts/builder.sh run scripts/extract-iso.sh [--force]
#
# Idempotent: skipped when build/meta/extract.stamp records the pinned ISO
# sha256 and build/iso-root/ exists; --force re-extracts. Extraction goes to a
# temp dir that is renamed into place, so an interrupted run never leaves a
# half-tree behind. Symlinks (/ubuntu -> ., /dists/stable -> noble, ...) are
# kept as links, and everything is made owner-writable for later phases.
# Boot metadata needed by repack-iso.sh is saved to build/meta/:
#   el-torito.plain.txt el-torito.as_mkisofs.txt system-area.plain.txt
#   partitions.sfdisk.txt esp.env esp.sha256 tree.txt md5sum.orig.txt
# plus builder-packages.txt (xorriso version + builder package versions).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config

force=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --force) force=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_container
require_cmd xorriso sfdisk sha256sum dd

iso="$(resolve_base_iso)"
stamp="$META_DIR/extract.stamp"
stamp_want="iso_sha256=$BASE_ISO_SHA256 format=1"

if [ "$force" = 0 ] && [ -d "$ISO_ROOT" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$stamp_want" ]; then
    log "up to date: $ISO_ROOT matches $BASE_ISO_NAME (use --force to re-extract)"
    exit 0
fi

"$BRAINSYNCOS_ROOT/scripts/verify-base-iso.sh" --iso "$iso"
check_free_space "$BUILD_DIR"

tmp="$BUILD_DIR/.iso-root.tmp"
tmp_meta="$BUILD_DIR/.meta.tmp"
force_rm_tree "$tmp"
force_rm_tree "$tmp_meta"
mkdir -p "$tmp_meta"

log "extracting $iso -> $ISO_ROOT (about 4.2 GiB) ..."
xorriso -osirrox on:auto_chmod_on -indev "$iso" -extract / "$tmp" 2>"$tmp_meta/xorriso-extract.log" \
    || { tail -20 "$tmp_meta/xorriso-extract.log" >&2; die "xorriso extraction failed"; }
chmod -R u+w "$tmp"
[ -L "$tmp/ubuntu" ] || die "symlink /ubuntu was not preserved by extraction"

log "saving boot metadata"
xorriso -indev "$iso" -report_el_torito plain       2>/dev/null > "$tmp_meta/el-torito.plain.txt"
xorriso -indev "$iso" -report_el_torito as_mkisofs  2>/dev/null > "$tmp_meta/el-torito.as_mkisofs.txt"
xorriso -indev "$iso" -report_system_area plain     2>/dev/null > "$tmp_meta/system-area.plain.txt"
sfdisk -d "$iso" > "$tmp_meta/partitions.sfdisk.txt"

# The appended EFI System partition (L4TLauncher + QSPI capsules). Repack copies
# it verbatim from the base ISO; its hash proves that later.
esp_line="$(grep -i 'type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B' "$tmp_meta/partitions.sfdisk.txt" | head -1)"
[ -n "$esp_line" ] || die "no EFI System partition in the base ISO partition table"
esp_start="$(printf '%s\n' "$esp_line" | sed -n 's/.*start= *\([0-9]*\).*/\1/p')"
esp_size="$(printf '%s\n' "$esp_line" | sed -n 's/.*size= *\([0-9]*\).*/\1/p')"
printf 'ESP_START_SECTOR=%s\nESP_SIZE_SECTORS=%s\n' "$esp_start" "$esp_size" > "$tmp_meta/esp.env"
dd if="$iso" bs=4M iflag=skip_bytes,count_bytes skip=$((esp_start * 512)) count=$((esp_size * 512)) 2>/dev/null \
    | sha256sum | awk '{print $1}' > "$tmp_meta/esp.sha256"
[ "$esp_start $esp_size $(cat "$tmp_meta/esp.sha256")" = "$BASE_ESP_START_SECTOR $BASE_ESP_SIZE_SECTORS $BASE_ESP_SHA256" ] \
    || die "base ISO ESP ($esp_start+$esp_size, $(cat "$tmp_meta/esp.sha256")) does not match the config pin"

cp "$tmp/md5sum.txt" "$tmp_meta/md5sum.orig.txt"
tree_listing "$tmp" > "$tmp_meta/tree.txt"
# Toolchain record (apt in the builder is not snapshot-pinned).
{ xorriso -version 2>/dev/null | head -1; cat /usr/share/brainsyncos/builder-packages.txt 2>/dev/null; } \
    > "$tmp_meta/builder-packages.txt" || true

# Swap into place: old tree out, new tree in, stamp last.
mkdir -p "$META_DIR"
rm -f "$stamp"
force_rm_tree "$ISO_ROOT"
mv "$tmp" "$ISO_ROOT"
cp "$tmp_meta"/* "$META_DIR"/
rm -rf "$tmp_meta"
printf '%s\n' "$stamp_want" > "$stamp"

log "extracted $(wc -l < "$META_DIR/tree.txt" | tr -d ' ') nodes to $ISO_ROOT"
