#!/usr/bin/env bash
# test_phase2.sh - build the BrainSyncOS ISO for real and check it (host side).
#
# Usage: BASE_ISO=/path/to/base.iso tests/test_phase2.sh [--reuse]
#
# Checks the product config (host bash 3.2), the boot-menu and autoinstall
# generators and the md5sum merge on NVIDIA's real files, builds the ISO
# (--reuse keeps an existing build/out ISO), compares its boot structure with
# the base (El Torito, GPT, ESP bytes, ISO9660/Joliet names), checks its
# contents and rebuilt overlay layer, and runs the install hook against
# NVIDIA's real target base layer, including the failure paths. The built ISO
# is kept: it is the deliverable. Per-check logs go to build/test-logs/phase2/.
# The in-container halves are in tests/phase2-checks.sh.

BSOS_SCRIPT=test_phase2.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"

load_config
load_product_config

reuse=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --reuse) reuse=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

S="$BRAINSYNCOS_ROOT/scripts"
B="$S/builder.sh"
LOGS="$BUILD_DIR/test-logs/phase2"
NAME="$(product_iso_name)"
ISO="$OUT_DIR/$NAME.iso"
mkdir -p "$LOGS"
rm -f "$LOGS"/*.log
# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"

in_builder() { "$B" run tests/phase2-checks.sh "$1"; }

t_product_config() {
    /bin/bash --version | head -1
    /bin/bash -c 'source "$1/scripts/lib.sh"; load_config; load_product_config
        echo "$BRAINSYNCOS_VERSION $TARGET_BOARD $INSTALL_TARGET_DEFAULT $GRUB_TIMEOUT -> $(product_iso_name)"' _ "$BRAINSYNCOS_ROOT"
    local bad="$TMP_DIR/bad-brainsyncos.env" kv out
    mkdir -p "$TMP_DIR"
    for kv in GRUB_TIMEOUT=soon INSTALL_TARGET_DEFAULT=emmc BRAINSYNCOS_VERSION=1.0; do
        sed "s/^${kv%%=*}=.*/$kv/" "$BSOS_PRODUCT_CONFIG" > "$bad"
        if out="$(BSOS_PRODUCT_CONFIG="$bad" /bin/bash -c 'source "$1/scripts/lib.sh"; load_config; load_product_config' _ "$BRAINSYNCOS_ROOT" 2>&1)"; then
            rm -f "$bad"; die "accepted $kv"
        fi
        printf '%s\n' "$out" | grep -q "${kv%%=*} must be" || die "rejected $kv for the wrong reason: $out"
        echo "rejects $kv: $out"
    done
    rm -f "$bad"
}

t_builder_image() {
    "$B" build
    "$B" run bash -c 'command -v grub-script-check && test -d /scratch && test -w /scratch'
    "$B" run bash -c 'echo "BSOS_GIT_REV=$BSOS_GIT_REV"' | grep -Eq '^BSOS_GIT_REV=([0-9a-f]{12}(-dirty)?|unknown)$'
}

t_build() {
    if [ "$reuse" = 1 ] && [ -f "$ISO" ]; then
        echo "reusing $ISO"
    else
        "$B" run scripts/build-iso.sh
    fi
    [ -s "$ISO" ] && [ -s "$ISO.sha256" ] && [ -s "$OUT_DIR/$NAME.manifest.txt" ] || die "build outputs missing"
    (cd "$OUT_DIR" && shasum -a 256 -c "$NAME.iso.sha256")
}

t_boot_compare() {
    local out
    out="$("$B" run scripts/compare-iso.sh "build/out/$NAME.iso" --boot-only 2>&1)" || { printf '%s\n' "$out"; return 1; }
    printf '%s\n' "$out"
    # Every base name unchanged; exactly the payload (2 dirs + 4 files) added.
    printf '%s\n' "$out" | grep -q '^PASS names .*iso9660 [0-9]* names equal (+6 added); joliet [0-9]* names equal (+6 added)' \
        || die "names check: expected all base names unchanged and 6 added"
}

log "Phase 2 test chain (logs: ${LOGS#$BRAINSYNCOS_ROOT/}/)"
check product-config       t_product_config
check disk-space           check_free_space "$BUILD_DIR" "$MIN_FREE_GB"
if [ "$nfail" -gt 0 ]; then log "aborting: prerequisites failed"; exit 1; fi
check builder-image        t_builder_image
check render-grub          in_builder grub
check patch-autoinstall    in_builder autoinstall
check merge-md5sums        in_builder md5
if [ "$nfail" -gt 0 ]; then log "aborting: generators failed"; exit 1; fi
check build-iso            t_build
if [ -f "$ISO" ]; then
    check boot-compare     t_boot_compare
    check iso-contents     in_builder contents
    check overlay-layer    in_builder layer
    check install-hook     in_builder hook
    check hook-failures    in_builder hook-fail
fi

finish "Phase 2"
