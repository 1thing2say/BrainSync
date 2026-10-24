#!/usr/bin/env bash
# build-vm-iso.sh - build the BrainSyncOS VM edition installer ISO (builder only).
#
# Usage: scripts/builder.sh run scripts/build-vm-iso.sh [--build-id ID] [--name NAME]
#            [--authorized-keys FILE] [--password-hash FILE] [--repo DIR]
#
# Output: build/out/NAME.iso, NAME defaulting to brainsyncos-VERSION-vm-arm64
# (+ .iso.sha256, .manifest.txt with the sha256 of every file BrainSyncOS adds
# or replaces, .xorriso.log). Staged files stay in build/stage-vm/.
# Logins (--authorized-keys / --password-hash) and the package repository
# (--repo, default build/repo/vm): exactly as for build-iso.sh.
#
# A remaster of stock Ubuntu Server arm64 (config/vm-base-iso.env, fetched by
# scripts/fetch-vm-base.sh into build/downloads/) for UTM on Apple Silicon.
# Booting its first menu entry installs Ubuntu Server unattended and runs the
# same payload/hook.sh + install.d steps as the Jetson edition (brainsync
# account, key-only SSH, hostname, release file). Nothing of the vendor ISO is
# rebuilt; only these paths are added or replaced:
#   /brainsyncos/            payload, release (EDITION=vm-arm64), settings.env,
#                            repo-key.gpg, config/ logins, autoinstall.yaml (vm/autoinstall.yaml
#                            plus the VM_DESKTOP package, scripts/render-vm-autoinstall.py;
#                            checked by scripts/check-autoinstall.py)
#   /brainsyncos-repo/       the signed package repository (--repo)
#   /boot/grub/grub.cfg      BrainSyncOS menu (scripts/render-vm-grub.py)
#   /md5sum.txt              vendor entries + the staged files
# The squashfs layers, casper/SHA256SUMS, the ESP (shim + signed GRUB) and the
# MBR are Ubuntu's, byte for byte (-boot_image any replay).
# BUILD_ID defaults to the host's git revision (BSOS_GIT_REV, set by builder.sh).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_vm_config
load_vm_product_config
load_packages_config

parse_build_args "$(vm_iso_name)" vm "$@"
require_container
require_cmd xorriso python3 grub-script-check md5sum sha256sum sfdisk truncate ssh-keygen gpg gpgv
[ -d /scratch ] && [ -w /scratch ] || die "/scratch (tmpfs) is missing; run this through scripts/builder.sh run"
validate_logins
validate_repo "$REPO_SRC" vm

iso="$(resolve_vm_base_iso)"
"$BRAINSYNCOS_ROOT/scripts/verify-base-iso.sh" --edition vm --iso "$iso"
check_base_boot_bytes "$iso"
mkdir -p "$OUT_DIR"
check_free_space "$OUT_DIR" "$(iso_need_gb "$BASE_ISO_SIZE" "$REPO_SRC")"
log "building $BUILD_NAME (build $BUILD_ID), $EDITION edition on Ubuntu $UBUNTU_RELEASE"

# --- vendor inputs ---------------------------------------------------------------
work=/scratch/bsos-vm-build
force_rm_tree "$work"
mkdir -p "$work/vendor"
xorriso -osirrox on -indev "$iso" \
    -extract /boot/grub/grub.cfg "$work/vendor/grub.cfg" \
    -extract /md5sum.txt "$work/vendor/md5sum.txt" > "$work/extract.log" 2>&1 \
    || { tail -5 "$work/extract.log" >&2; die "could not read vendor files from $iso"; }
want="$(awk '$2 == "./boot/grub/grub.cfg" {print $1}' "$work/vendor/md5sum.txt")"
got="$(md5sum "$work/vendor/grub.cfg" | awk '{print $1}')"
[ -n "$want" ] && [ "$want" = "$got" ] || die "vendor /boot/grub/grub.cfg does not match md5sum.txt ($got vs ${want:-none})"
log "vendor grub.cfg and md5sum.txt read from the base ISO"

# --- payload, autoinstall and boot menu --------------------------------------------
force_rm_tree "$VM_STAGE_DIR"
mkdir -p "$VM_STAGE_DIR/boot/grub"
stage_payload "$VM_STAGE_DIR" <<EOF
NAME=BrainSyncOS
VERSION=$BRAINSYNCOS_VERSION
BUILD_ID=$BUILD_ID
EDITION=$EDITION
UBUNTU_RELEASE=$UBUNTU_RELEASE
BASE_ISO_NAME=$BASE_ISO_NAME
BASE_ISO_SHA256=$BASE_ISO_SHA256
REPO_KEY=$REPO_KEY_FPR
EOF
python3 "$BRAINSYNCOS_ROOT/scripts/render-vm-autoinstall.py" "$BRAINSYNCOS_ROOT/vm/autoinstall.yaml" \
    "$VM_STAGE_DIR/brainsyncos/autoinstall.yaml" --desktop "$VM_DESKTOP" \
    || die "could not render the autoinstall for VM_DESKTOP=$VM_DESKTOP"
python3 "$BRAINSYNCOS_ROOT/scripts/check-autoinstall.py" "$VM_STAGE_DIR/brainsyncos/autoinstall.yaml" \
    || die "vm/autoinstall.yaml is not a valid unattended BrainSyncOS autoinstall"

python3 "$BRAINSYNCOS_ROOT/scripts/render-vm-grub.py" "$work/vendor/grub.cfg" "$VM_STAGE_DIR/boot/grub/grub.cfg" \
    --version "$BRAINSYNCOS_VERSION" --build-id "$BUILD_ID" --timeout "$VM_GRUB_TIMEOUT" \
    --kernel "$VM_INSTALL_KERNEL" --consoles "$VM_CONSOLES" \
    --autoinstall /cdrom/brainsyncos/autoinstall.yaml --base-iso "$BASE_ISO_NAME"
grub-script-check "$VM_STAGE_DIR/boot/grub/grub.cfg" || die "rendered grub.cfg does not parse"
[ "$VM_GRUB_TIMEOUT" = -1 ] || log "VM_GRUB_TIMEOUT=$VM_GRUB_TIMEOUT: the install entry (erases the VM's disk) starts after ${VM_GRUB_TIMEOUT}s"

# --- md5sum.txt and the ISO ------------------------------------------------------------
merge_stage_md5sums "$work/vendor/md5sum.txt" "$VM_STAGE_DIR" "$REPO_SRC" "$ISO_REPO_PATH"
write_stage_iso --map "$REPO_SRC" "$ISO_REPO_PATH" \
    "$iso" "$VM_STAGE_DIR" "$BUILD_NAME" /brainsyncos /boot/grub/grub.cfg /md5sum.txt
force_rm_tree "$work"
