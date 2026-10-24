#!/usr/bin/env bash
# repack-iso.sh - rebuild a bootable ISO from build/iso-root/ (builder only).
#
# Usage: scripts/builder.sh run scripts/repack-iso.sh [--name NAME] [--keep-md5]
#                                                     [--expect-noop]
#
# Output: build/out/NAME.iso (+ NAME.iso.sha256, NAME.xorriso.log,
# NAME.tree-changes.txt). Default NAME: brainsyncos-l4t-$L4T_RELEASE.
#
# Recipe (verified in Phase 1): load the pinned base ISO with -indev and
# replay its boot setup (-boot_image any replay), so the protective MBR, GPT,
# El Torito UEFI entry and the appended 512 MiB ESP (NVIDIA L4TLauncher + QSPI
# capsules) are reproduced and the ESP is copied byte-for-byte; never rebuild
# the ESP. Those boot bytes are read from the base file, so they are checked
# against the hashes pinned in config/base-iso.env first. -update_r then syncs
# the ISO tree to build/iso-root (adds, replaces and deletes). md5sum.txt is
# regenerated first unless --keep-md5.
# Fixups: -compliance $ISO_COMPLIANCE reproduces NVIDIA's ISO9660/Joliet names
# (replay does not), -padding included keeps the Gap1 partition, -joliet on
# keeps Joliet, and the output is truncated to the image size so the backup
# GPT sits at the very end of the file (xorriso pads stdio output to 32 blocks).
# Host junk (.DS_Store, ._*, .Spotlight-V100, .fseventsd, .Trashes) is pruned
# from build/iso-root first. Paths added/removed relative to the extraction
# (build/meta/tree.txt) are listed in NAME.tree-changes.txt; --expect-noop makes
# any such change fatal.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config

name="brainsyncos-l4t-$L4T_RELEASE"
regen_md5=1
expect_noop=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --name) name="${2:?--name needs a value}"; shift 2 ;;
        --keep-md5) regen_md5=0; shift ;;
        --expect-noop) expect_noop=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
case "$name" in */*|.*|'') die "invalid --name: $name" ;; esac
require_container
require_cmd xorriso sfdisk truncate md5sum sha256sum dd

iso="$(resolve_base_iso)"
[ -d "$ISO_ROOT" ] && [ -f "$META_DIR/extract.stamp" ] || die "no extracted tree; run scripts/extract-iso.sh first"
"$BRAINSYNCOS_ROOT/scripts/verify-base-iso.sh" --iso "$iso"
mkdir -p "$OUT_DIR"
check_free_space "$OUT_DIR"

out="$OUT_DIR/$name.iso"
partial="$OUT_DIR/.$name.iso.partial"
xlog="$OUT_DIR/$name.xorriso.log"
changes="$OUT_DIR/$name.tree-changes.txt"
rm_released "$partial" "$out" "$out.sha256"
# Any failure or interrupt from here on drops the ~5 GB partial image.
trap 'rm_released "$partial" "$OUT_DIR/.$name.sfdisk-V"' EXIT

# The replay copies the ESP and the system area from the base file itself;
# check those bytes against the pins rather than trusting the sha256 cache.
log "checking the base ISO boot bytes against the pinned hashes"
esp_sum="$(dd if="$iso" bs=4M iflag=skip_bytes,count_bytes skip=$((BASE_ESP_START_SECTOR * 512)) \
    count=$((BASE_ESP_SIZE_SECTORS * 512)) 2>/dev/null | sha256sum | awk '{print $1}')"
[ "$esp_sum" = "$BASE_ESP_SHA256" ] || die "base ISO ESP sha256 is $esp_sum, expected $BASE_ESP_SHA256"
sa_sum="$(dd if="$iso" bs=2048 count=16 2>/dev/null | sha256sum | awk '{print $1}')"
[ "$sa_sum" = "$BASE_SYSTEM_AREA_SHA256" ] || die "base ISO system area sha256 is $sa_sum, expected $BASE_SYSTEM_AREA_SHA256"

# Host junk (Finder/Spotlight) must never reach the ISO or md5sum.txt.
junk="$(cd "$ISO_ROOT" && find . \( -name .DS_Store -o -name '._*' -o -name .Spotlight-V100 \
    -o -name .fseventsd -o -name .Trashes -o -name .TemporaryItems \) -print)"
if [ -n "$junk" ]; then
    printf '%s\n' "$junk" | while IFS= read -r j; do warn "pruning host junk: $j"; done
    (cd "$ISO_ROOT" && printf '%s\n' "$junk" | while IFS= read -r j; do rm -rf "./$j"; done)
fi

# Paths added or removed since extraction ("type path", sizes ignored).
mkdir -p "$TMP_DIR"
tree_listing "$ISO_ROOT" | awk '{$2 = ""; print}' > "$TMP_DIR/tree.now"
awk '{$2 = ""; print}' "$META_DIR/tree.txt" > "$TMP_DIR/tree.orig"
diff "$TMP_DIR/tree.orig" "$TMP_DIR/tree.now" | sed -n 's/^> /added   /p; s/^< /removed /p' > "$changes" || true
rm -f "$TMP_DIR/tree.now" "$TMP_DIR/tree.orig"
nchanges="$(wc -l < "$changes" | tr -d ' ')"
log "tree changes vs extraction: $nchanges (list: $changes)"
if [ "$expect_noop" = 1 ] && [ "$nchanges" != 0 ]; then
    head -10 "$changes" >&2
    die "--expect-noop: build/iso-root differs from the extraction by $nchanges path(s)"
fi

if [ "$regen_md5" = 1 ]; then
    log "regenerating md5sum.txt"; mkdir -p "$TMP_DIR"
    (cd "$ISO_ROOT" && find . -type f ! -path ./md5sum.txt ! -path ./boot/boot.cat -print0 \
        | LC_ALL=C sort -z | xargs -0 md5sum) > "$TMP_DIR/md5sum.txt"
    if cmp -s "$TMP_DIR/md5sum.txt" "$ISO_ROOT/md5sum.txt"; then
        log "md5sum.txt unchanged ($(wc -l < "$ISO_ROOT/md5sum.txt" | tr -d ' ') entries)"
        rm -f "$TMP_DIR/md5sum.txt"
    else
        mv "$TMP_DIR/md5sum.txt" "$ISO_ROOT/md5sum.txt"
        log "md5sum.txt updated ($(wc -l < "$ISO_ROOT/md5sum.txt" | tr -d ' ') entries)"
    fi
fi

log "writing $out"
if ! xorriso -indev "$iso" -outdev "$partial" \
        -joliet on \
        -compliance "$ISO_COMPLIANCE" \
        -boot_image any replay \
        -padding included \
        -update_r "$ISO_ROOT" / \
        -commit > "$xlog" 2>&1; then
    tail -20 "$xlog" >&2
    die "xorriso failed (log: $xlog)"
fi
grep -q 'Replayed [0-9]* boot related commands' "$xlog" || die "xorriso did not replay the boot setup (log: $xlog)"

sectors="$(xorriso -indev "$partial" -report_system_area plain 2>/dev/null | awk '/ISO image size\/512/ {print $NF}')"
[ -n "$sectors" ] || die "could not read the image size of $partial"
truncate -s $((sectors * 512)) "$partial"
sfdisk -V "$partial" > "$OUT_DIR/.$name.sfdisk-V" 2>&1 || true
grep -q 'No errors detected' "$OUT_DIR/.$name.sfdisk-V" \
    || { cat "$OUT_DIR/.$name.sfdisk-V" >&2; die "partition table of $partial is not valid"; }
rm -f "$OUT_DIR/.$name.sfdisk-V"

mv "$partial" "$out"
trap - EXIT
(cd "$OUT_DIR" && sha256sum "$name.iso" > "$name.iso.sha256")
log "done: $out ($(file_size "$out") bytes, sha256 $(cut -d' ' -f1 "$out.sha256"))"
