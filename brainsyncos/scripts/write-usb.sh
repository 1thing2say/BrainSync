#!/usr/bin/env bash
# write-usb.sh - write a BrainSyncOS ISO to a USB stick (macOS host).
#
# Usage: scripts/write-usb.sh                    list external physical disks
#        scripts/write-usb.sh ISO /dev/diskN [--no-verify]
#
# ERASES the whole target disk. Refuses anything that is not an external,
# physical, whole disk at least as large as the ISO, or that holds the running
# system; checks ISO against ISO.sha256 when present; then asks you to type the
# disk id (diskN) to confirm (BSOS_CONFIRM_DISK=diskN answers non-interactively).
# Writes with sudo dd to the raw device, reads the image back to verify it
# (skip with --no-verify), and ejects. balenaEtcher (NVIDIA's documented tool)
# works just as well.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

[ "$(uname -s)" = Darwin ] || die "macOS only (on Linux: dd if=ISO of=/dev/sdX bs=4M conv=fsync)"
require_cmd diskutil plutil dd shasum

if [ "$#" = 0 ]; then
    log "external physical disks (pass one as /dev/diskN):"
    diskutil list external physical
    exit 0
fi
case "${1:-}" in -h|--help) usage_from_header; exit 0 ;; esac
[ "$#" -ge 2 ] || die "usage: write-usb.sh ISO /dev/diskN [--no-verify]"
iso="$1"
dev="$2"
verify=1
[ "${3:-}" = --no-verify ] && verify=0
[ "$#" -le 3 ] || die "too many arguments"
[ "$#" -lt 3 ] || [ "$verify" = 0 ] || die "unknown argument: $3"

[ -f "$iso" ] || die "no such ISO: $iso"
case "$dev" in
    /dev/disk[0-9]|/dev/disk[0-9][0-9]) ;;
    *) die "target must be a whole disk like /dev/disk4, got $dev" ;;
esac
disk="${dev#/dev/}"

info="$(diskutil info -plist "$disk" 2>/dev/null)" || die "diskutil knows no $disk"
key() { printf '%s' "$info" | plutil -extract "$1" raw -o - - 2>/dev/null || echo "?"; }
[ "$(key WholeDisk)" = true ] || die "$disk is not a whole disk"
[ "$(key Internal)" = false ] || die "$disk is an internal disk; refusing"
[ "$(key VirtualOrPhysical)" != Virtual ] || die "$disk is a virtual disk (APFS container or image); refusing"
# The running system: its APFS container (synthesized disk) and the physical
# disk(s) holding that container, which may be external.
root_info="$(diskutil info -plist /)"
root_disks="$(printf '%s' "$root_info" | plutil -extract ParentWholeDisk raw -o - - 2>/dev/null || true)"
i=0
while store="$(printf '%s' "$root_info" | plutil -extract "APFSPhysicalStores.$i.APFSPhysicalStore" raw -o - - 2>/dev/null)"; do
    root_disks="$root_disks ${store%s[0-9]*}"
    i=$((i + 1))
done
for r in $root_disks; do
    [ "$disk" != "$r" ] || die "$disk holds the running system; refusing"
done
disk_size="$(key TotalSize)"
iso_size="$(file_size "$iso")"
case "$disk_size" in ''|*[!0-9]*) die "cannot read the size of $disk" ;; esac
[ "$disk_size" -ge "$iso_size" ] || die "$disk ($disk_size bytes) is smaller than the ISO ($iso_size bytes)"

if [ -f "$iso.sha256" ]; then
    log "checking $iso against $(basename "$iso").sha256"
    (cd "$(dirname "$iso")" && shasum -a 256 -c "$(basename "$iso").sha256") >&2 || die "ISO checksum mismatch"
else
    warn "no $iso.sha256; writing unverified ISO"
fi

log "target: /dev/$disk, $(key MediaName), $((disk_size / 1000000000)) GB, bus $(key BusProtocol)"
diskutil list "$disk" >&2
log "EVERYTHING on /dev/$disk will be erased."
answer="${BSOS_CONFIRM_DISK:-}"
if [ -z "$answer" ]; then
    printf 'Type %s to write %s to it: ' "$disk" "$(basename "$iso")" >&2
    read -r answer || die "no confirmation; nothing written"
fi
[ "$answer" = "$disk" ] || die "confirmation '$answer' does not match $disk; nothing written"

diskutil unmountDisk "/dev/$disk" >&2
log "writing (sudo dd to /dev/r$disk) ..."
sudo dd if="$iso" of="/dev/r$disk" bs=4m status=progress
sync
if [ "$verify" = 1 ]; then
    log "verifying: reading $iso_size bytes back from /dev/r$disk ..."
    want="$(shasum -a 256 < "$iso" | awk '{print $1}')"
    got="$(sudo head -c "$iso_size" "/dev/r$disk" | shasum -a 256 | awk '{print $1}')"
    [ "$want" = "$got" ] || die "read-back sha256 $got != ISO $want"
    log "verified: sha256 $got"
fi
diskutil eject "/dev/$disk" >&2
log "done: /dev/$disk holds $(basename "$iso"); boot the Jetson from it"
