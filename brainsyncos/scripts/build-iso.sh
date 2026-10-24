#!/usr/bin/env bash
# build-iso.sh - build the BrainSyncOS installer ISO (builder only).
#
# Usage: scripts/builder.sh run scripts/build-iso.sh [--build-id ID] [--name NAME]
#            [--authorized-keys FILE] [--password-hash FILE]
#
# Output: build/out/NAME.iso, NAME defaulting to brainsyncos-VERSION-BOARD
# (+ .iso.sha256, .manifest.txt with the sha256 of every file BrainSyncOS adds
# or replaces, .xorriso.log). Staged files stay in build/stage/ for inspection.
#
# Logins for the appliance account (BRAINSYNC_USER), at least one required:
#   --authorized-keys FILE  SSH public keys (default config/authorized_keys)
#   --password-hash FILE    console password hash (default
#                           config/brainsync-password.hash; make one with
#                           scripts/make-password-hash.sh)
# Both end up on the ISO, readable by anyone holding the stick: public keys
# and a salted hash, never a private key or a plain password.
#
# Every build starts from the pinned base ISO, never from build/iso-root:
#  1. verify the base ISO and its boot bytes (config/base-iso.env pins)
#  2. read NVIDIA's grub.cfg, md5sum.txt and the overlay layer
#     AUTOINSTALL_LAYER from it, checked against the vendor md5sum.txt
#  3. rebuild that layer with the BrainSync hook in each HOOKED_AUTOINSTALL
#     file: unpack into /scratch (tmpfs; the layer's trusted.overlay.* xattrs
#     need CAP_SYS_ADMIN and cannot live on the macOS bind mount), check that
#     re-squashing it unmodified reproduces NVIDIA's file byte for byte, patch
#     (scripts/patch-autoinstall.py), re-squash with the same parameters
#  4. stage payload/ as /brainsyncos (+ the release file) and render
#     boot/grub/grub.cfg (scripts/render-grub.py)
#  5. merge md5sum.txt (scripts/merge-md5sums.py)
#  6. write the base ISO with its boot setup replayed and build/stage mapped
#     on top (MBR, GPT, El Torito and the ESP are NVIDIA's, byte for byte)
# BUILD_ID defaults to the host's git revision (BSOS_GIT_REV, set by builder.sh).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config
load_product_config

build_id="${BSOS_GIT_REV:-unknown}"
name="$(product_iso_name)"
keys_file="$BRAINSYNCOS_ROOT/config/authorized_keys"
hash_file="$BRAINSYNCOS_ROOT/config/brainsync-password.hash"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --build-id) build_id="${2:?--build-id needs a value}"; shift 2 ;;
        --name) name="${2:?--name needs a value}"; shift 2 ;;
        --authorized-keys) keys_file="${2:?--authorized-keys needs a file}"; shift 2 ;;
        --password-hash) hash_file="${2:?--password-hash needs a file}"; shift 2 ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
printf '%s\n' "$build_id" | grep -Eq '^[A-Za-z0-9._+-]+$' || die "invalid build id: $build_id"
case "$name" in */*|.*|'') die "invalid --name: $name" ;; esac
require_container
require_cmd xorriso unsquashfs mksquashfs python3 grub-script-check md5sum sha256sum sfdisk truncate ssh-keygen
[ -d /scratch ] && [ -w /scratch ] || die "/scratch (tmpfs) is missing; run this through scripts/builder.sh run"

# --- logins for BRAINSYNC_USER ------------------------------------------------
have_keys=0
have_hash=0
if [ -s "$keys_file" ]; then
    ! grep -q 'PRIVATE KEY' "$keys_file" || die "$keys_file contains a PRIVATE key; use the .pub file"
    ssh-keygen -l -f "$keys_file" > /dev/null 2>&1 || die "$keys_file is not a valid authorized_keys file"
    have_keys=1
    log "SSH keys for $BRAINSYNC_USER: $(ssh-keygen -l -f "$keys_file" | wc -l | tr -d ' ') from ${keys_file#$BRAINSYNCOS_ROOT/}"
fi
if [ -s "$hash_file" ]; then
    [ "$(grep -c . "$hash_file")" = 1 ] && grep -Eqx '\$(6|5|y)\$[A-Za-z0-9./$]+' "$hash_file" \
        || die "$hash_file is not a single crypt(3) hash line (\$6\$..., \$y\$...); make one with scripts/make-password-hash.sh"
    have_hash=1
    log "console password for $BRAINSYNC_USER: hash from ${hash_file#$BRAINSYNCOS_ROOT/}"
fi
[ "$have_keys" = 1 ] || [ "$have_hash" = 1 ] \
    || die "no way to log in: add SSH public keys to config/authorized_keys and/or run scripts/make-password-hash.sh"

layer="casper/$AUTOINSTALL_LAYER"

iso="$(resolve_base_iso)"
"$BRAINSYNCOS_ROOT/scripts/verify-base-iso.sh" --iso "$iso"
check_base_boot_bytes "$iso"
mkdir -p "$OUT_DIR"
check_free_space "$OUT_DIR"
log "building $name (build $build_id) for $TARGET_BOARD"

# --- 2. vendor inputs ---------------------------------------------------------
work=/scratch/bsos-build
force_rm_tree "$work"
mkdir -p "$work/vendor"
xorriso -osirrox on -indev "$iso" \
    -extract /boot/grub/grub.cfg "$work/vendor/grub.cfg" \
    -extract /md5sum.txt "$work/vendor/md5sum.txt" \
    -extract "/$layer" "$work/vendor/layer.squashfs" > "$work/extract.log" 2>&1 \
    || { tail -5 "$work/extract.log" >&2; die "could not read vendor files from $iso"; }
# check_vendor_md5 ISO_PATH FILE: FILE (read from the ISO) matches the vendor md5sum.txt entry.
check_vendor_md5() {
    local want got
    want="$(awk -v p="./$1" '$2 == p {print $1}' "$work/vendor/md5sum.txt")"
    got="$(md5sum "$2" | awk '{print $1}')"
    [ -n "$want" ] && [ "$want" = "$got" ] || die "vendor /$1 does not match md5sum.txt ($got vs ${want:-none})"
}
check_vendor_md5 boot/grub/grub.cfg "$work/vendor/grub.cfg"
check_vendor_md5 "$layer" "$work/vendor/layer.squashfs"
log "vendor grub.cfg, md5sum.txt and $layer read from the base ISO"

# --- 3. overlay layer with the BrainSync hook ---------------------------------
sq="$work/vendor/layer.squashfs"
unsquashfs -s "$sq" > "$work/layer.info"
comp="$(awk '/^Compression / {print $2}' "$work/layer.info")"
bsize="$(awk '/^Block size / {print $3}' "$work/layer.info")"
created="$(sed -n 's/^Creation or last append time //p' "$work/layer.info")"
[ -n "$comp" ] && [ -n "$bsize" ] && [ -n "$created" ] || die "could not read the parameters of $layer"
mkfs_time="$(TZ=UTC date -d "$created" +%s)"

unsquashfs -q -no-progress -d "$work/root" "$sq" > "$work/unsquashfs.log" 2>&1 \
    || { tail -5 "$work/unsquashfs.log" >&2; die "unsquashfs failed (it needs CAP_SYS_ADMIN for trusted.* xattrs; use builder.sh run)"; }
resquash() {
    mksquashfs "$work/root" "$1" -comp "$comp" -b "$bsize" -noappend -no-progress -quiet -mkfs-time "$mkfs_time"
}
resquash "$work/check.squashfs"
cmp -s "$sq" "$work/check.squashfs" \
    || die "re-squashing $layer unmodified does not reproduce NVIDIA's file; mksquashfs parameters differ"
rm -f "$work/check.squashfs"
log "re-squash check: unmodified $layer reproduced byte for byte ($comp, block $bsize)"

old_ifs="$IFS"; IFS=,
for f in $HOOKED_AUTOINSTALL; do
    IFS="$old_ifs"
    [ -f "$work/root/$f" ] || die "$f not found in $layer"
    cp -p "$work/root/$f" "$work/orig.yaml"
    python3 "$BRAINSYNCOS_ROOT/scripts/patch-autoinstall.py" "$work/orig.yaml" "$work/patched.yaml"
    # Rewrite in place so owner, mode and xattrs stay NVIDIA's; keep the mtime.
    cat "$work/patched.yaml" > "$work/root/$f"
    touch -r "$work/orig.yaml" "$work/root/$f"
done
IFS="$old_ifs"

force_rm_tree "$STAGE_DIR"
mkdir -p "$STAGE_DIR/casper" "$STAGE_DIR/boot/grub"
resquash "$STAGE_DIR/$layer"
log "rebuilt $layer with the BrainSync hook in: $HOOKED_AUTOINSTALL"

# --- 4. payload and boot menu --------------------------------------------------
cp -R "$BRAINSYNCOS_ROOT/payload" "$STAGE_DIR/brainsyncos"
cat > "$STAGE_DIR/brainsyncos/release" <<EOF
NAME=BrainSyncOS
VERSION=$BRAINSYNCOS_VERSION
BUILD_ID=$build_id
BOARD=$TARGET_BOARD
L4T_RELEASE=$L4T_RELEASE
JETPACK_RELEASE=$JETPACK_RELEASE
UBUNTU_RELEASE=$UBUNTU_RELEASE
BASE_ISO_NAME=$BASE_ISO_NAME
BASE_ISO_SHA256=$BASE_ISO_SHA256
EOF
cat > "$STAGE_DIR/brainsyncos/settings.env" <<EOF
BRAINSYNC_USER=$BRAINSYNC_USER
HOSTNAME_PREFIX=$HOSTNAME_PREFIX
EOF
mkdir -p "$STAGE_DIR/brainsyncos/config"
[ "$have_keys" = 0 ] || grep -v '^[[:space:]]*$' "$keys_file" > "$STAGE_DIR/brainsyncos/config/authorized_keys"
[ "$have_hash" = 0 ] || cp "$hash_file" "$STAGE_DIR/brainsyncos/config/password.hash"

python3 "$BRAINSYNCOS_ROOT/scripts/render-grub.py" "$work/vendor/grub.cfg" "$STAGE_DIR/boot/grub/grub.cfg" \
    --version "$BRAINSYNCOS_VERSION" --build-id "$build_id" \
    --default "$INSTALL_TARGET_DEFAULT" --timeout "$GRUB_TIMEOUT" --base-iso "$BASE_ISO_NAME"
grub-script-check "$STAGE_DIR/boot/grub/grub.cfg" || die "rendered grub.cfg does not parse"
[ "$GRUB_TIMEOUT" = -1 ] || warn "GRUB_TIMEOUT=$GRUB_TIMEOUT: this ISO erases the default disk unattended"

# --- 5. md5sum.txt -------------------------------------------------------------
python3 "$BRAINSYNCOS_ROOT/scripts/merge-md5sums.py" "$work/vendor/md5sum.txt" "$STAGE_DIR" > "$work/md5sum.txt"
mv "$work/md5sum.txt" "$STAGE_DIR/md5sum.txt"
log "md5sum.txt: $(wc -l < "$STAGE_DIR/md5sum.txt" | tr -d ' ') entries (vendor $(wc -l < "$work/vendor/md5sum.txt" | tr -d ' '))"

# --- 6. the ISO ----------------------------------------------------------------
out="$OUT_DIR/$name.iso"
partial="$OUT_DIR/.$name.iso.partial"
xlog="$OUT_DIR/$name.xorriso.log"
rm_released "$partial" "$out" "$out.sha256"
trap 'rm_released "$partial" "$partial.sfdisk-V"' EXIT
log "writing $out"
# Staged files get the vendor's ownership and read-only modes.
write_replayed_iso "$iso" "$partial" "$xlog" \
    -map "$STAGE_DIR" / \
    -chown_r 0 /brainsyncos /boot/grub/grub.cfg /md5sum.txt "/$layer" -- \
    -chgrp_r 0 /brainsyncos /boot/grub/grub.cfg /md5sum.txt "/$layer" -- \
    -chmod_r a-w,a+r /brainsyncos /boot/grub/grub.cfg /md5sum.txt "/$layer" --
finalize_iso "$partial" "$out"
trap - EXIT
(cd "$STAGE_DIR" && find . -type f | LC_ALL=C sort | xargs sha256sum) > "$OUT_DIR/$name.manifest.txt"
force_rm_tree "$work"
log "manifest: $OUT_DIR/$name.manifest.txt ($(wc -l < "$OUT_DIR/$name.manifest.txt" | tr -d ' ') files)"
