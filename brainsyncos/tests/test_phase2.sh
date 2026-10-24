#!/usr/bin/env bash
# test_phase2.sh - build a BrainSyncOS ISO for real and check it (host side).
#
# Usage: BASE_ISO=/path/to/base.iso tests/test_phase2.sh [--keep]
#
# Checks the product config (host bash 3.2), the boot-menu and autoinstall
# generators and the md5sum merge on NVIDIA's real files, and the login
# checks of build-iso.sh. Builds a test ISO (build/out/phase2-test.iso, with a
# throwaway SSH key and a throwaway package repository signing key generated
# for the run, so the real deliverable build/out/brainsyncos-*.iso and
# config/ are never touched), compares its boot structure
# with the base (El Torito, GPT, ESP bytes, ISO9660/Joliet names), checks its
# contents and rebuilt overlay layer, and runs the install hook against
# NVIDIA's real target base layer prepared the way NVIDIA's late-commands
# leave it (Phase 2.5 identity: hostname, account, SSH, no OOBE), including
# the console-password variant and the failure paths; the hook also mounts
# the ISO's package repository and 60-apt-repo.sh installs its keyring. The
# test ISO (~5 GB) and build/tmp/phase2-repo are deleted at the end unless --keep. Per-check logs: build/test-logs/phase2/.
# The in-container halves are in tests/phase2-checks.sh.

BSOS_SCRIPT=test_phase2.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"

load_config
load_product_config

keep=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --keep) keep=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

S="$BRAINSYNCOS_ROOT/scripts"
B="$S/builder.sh"
LOGS="$BUILD_DIR/test-logs/phase2"
NAME=phase2-test
ISO="$OUT_DIR/$NAME.iso"
LOGIN="build/tmp/test-login"   # relative to brainsyncos/ (= /work in the builder)
REPO_T="build/tmp/phase2-repo" # test key, packages and repository (test_repo)
mkdir -p "$LOGS"
rm -f "$LOGS"/*.log
# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"

cleanup_test_iso() {
    [ "$keep" = 1 ] && return 0
    rm_released "$ISO" "$ISO.sha256" "$OUT_DIR/$NAME.manifest.txt" "$OUT_DIR/$NAME.xorriso.log" \
        "$OUT_DIR/.$NAME.iso.partial"
    force_rm_tree "$BRAINSYNCOS_ROOT/$REPO_T"
}
trap cleanup_test_iso EXIT

in_builder() { "$B" run tests/phase2-checks.sh "$1" "$NAME"; }

# A throwaway repository signing key and the jetson package repository built
# with it, plus a vm one (the build must refuse it for this edition).
t_test_repo() {
    test_repo jetson "$REPO_T"
    "$B" run scripts/build-repo.sh --edition vm --debs "$REPO_T/debs" --key "$REPO_T/key/repo-signing-key.asc" \
        --out "$REPO_T/vm-repo" > /dev/null 2>&1
    [ -f "$BRAINSYNCOS_ROOT/$REPO_T/repo/dists/brainsyncos/InRelease" ] && [ -f "$BRAINSYNCOS_ROOT/$REPO_T/vm-repo/edition" ]
}

t_product_config() {
    /bin/bash --version | head -1
    /bin/bash -c 'source "$1/scripts/lib.sh"; load_config; load_product_config
        echo "$BRAINSYNCOS_VERSION $TARGET_BOARD $INSTALL_TARGET_DEFAULT $GRUB_TIMEOUT $BRAINSYNC_USER $HOSTNAME_PREFIX -> $(product_iso_name)"' _ "$BRAINSYNCOS_ROOT"
    local bad="$TMP_DIR/bad-brainsyncos.env" kv out
    mkdir -p "$TMP_DIR"
    for kv in GRUB_TIMEOUT=soon INSTALL_TARGET_DEFAULT=emmc BRAINSYNCOS_VERSION=1.0 \
              BRAINSYNC_USER=Root BRAINSYNC_USER=nvidia HOSTNAME_PREFIX=-x; do
        sed "s/^${kv%%=*}=.*/$kv/" "$BSOS_PRODUCT_CONFIG" > "$bad"
        if out="$(BSOS_PRODUCT_CONFIG="$bad" /bin/bash -c 'source "$1/scripts/lib.sh"; load_config; load_product_config' _ "$BRAINSYNCOS_ROOT" 2>&1)"; then
            rm -f "$bad"; die "accepted $kv"
        fi
        printf '%s\n' "$out" | grep -q "${kv%%=*} must" || die "rejected $kv for the wrong reason: $out"
        echo "rejects $kv: $out"
    done
    rm -f "$bad"
}

t_builder_image() {
    "$B" build
    "$B" run bash -c 'command -v grub-script-check ssh-keygen openssl && test -d /scratch && test -w /scratch'
    "$B" run bash -c 'echo "BSOS_GIT_REV=$BSOS_GIT_REV"' | grep -Eq '^BSOS_GIT_REV=([0-9a-f]{12}(-dirty)?|unknown)$'
}

# A throwaway key pair for the test build (never the user's config/ files).
t_test_login() {
    "$B" run bash -c 'set -e; rm -rf "$1"; mkdir -p "$1"
        ssh-keygen -q -t ed25519 -N "" -C brainsyncos-test -f "$1/id_ed25519"
        cp "$1/id_ed25519.pub" "$1/authorized_keys"
        openssl passwd -6 -salt bsostestsalt bsos-test-pw > "$1/password.hash"' _ "$LOGIN"
    [ -s "$BRAINSYNCOS_ROOT/$LOGIN/authorized_keys" ] && [ -s "$BRAINSYNCOS_ROOT/$LOGIN/password.hash" ]
    cat "$BRAINSYNCOS_ROOT/$LOGIN/authorized_keys"
}

# build-iso.sh must refuse bad or missing logins before doing any work.
t_build_logins() {
    local out
    expect_refusal() {  # expect_refusal MESSAGE ARGS...
        local msg="$1"; shift
        if out="$("$B" run scripts/build-iso.sh --name refused "$@" 2>&1)"; then die "build accepted: $*"; fi
        printf '%s\n' "$out" | grep -q "$msg" || die "refused for the wrong reason ($*): $out"
        echo "refuses $*: $(printf '%s\n' "$out" | tail -1)"
    }
    expect_refusal 'no way to log in' --authorized-keys "$LOGIN/none" --password-hash "$LOGIN/none"
    expect_refusal 'contains a PRIVATE key' --authorized-keys "$LOGIN/id_ed25519" --password-hash "$LOGIN/none"
    expect_refusal 'not a valid authorized_keys' --authorized-keys "$LOGIN/password.hash" --password-hash "$LOGIN/none"
    expect_refusal 'not a single crypt(3) hash' --authorized-keys "$LOGIN/none" --password-hash "$LOGIN/authorized_keys"
    expect_refusal 'no BrainSyncOS package repository' --authorized-keys "$LOGIN/authorized_keys" --repo "$REPO_T/none"
    expect_refusal "built for edition 'vm', not jetson" --authorized-keys "$LOGIN/authorized_keys" --repo "$REPO_T/vm-repo"
    [ ! -e "$OUT_DIR/refused.iso" ] || die "a refused build left an ISO"
}

t_build() {
    "$B" run scripts/build-iso.sh --name "$NAME" --authorized-keys "$LOGIN/authorized_keys" --password-hash "$LOGIN/none" \
        --repo "$REPO_T/repo"
    [ -s "$ISO" ] && [ -s "$ISO.sha256" ] && [ -s "$OUT_DIR/$NAME.manifest.txt" ] || die "build outputs missing"
    (cd "$OUT_DIR" && shasum -a 256 -c "$NAME.iso.sha256")
}

t_boot_compare() {
    local out added
    out="$("$B" run scripts/compare-iso.sh "build/out/$NAME.iso" --boot-only 2>&1)" || { printf '%s\n' "$out"; return 1; }
    printf '%s\n' "$out"
    # Every base name unchanged; exactly the staged payload and the package
    # repository (dirs + files) added.
    added=$(( $(find "$STAGE_DIR/brainsyncos" | wc -l) + $(find "$BRAINSYNCOS_ROOT/$REPO_T/repo" | wc -l) ))
    printf '%s\n' "$out" | grep -q "^PASS names .*iso9660 [0-9]* names equal (+$added added); joliet [0-9]* names equal (+$added added)" \
        || die "names check: expected all base names unchanged and $added added"
}

log "Phase 2 test chain (logs: ${LOGS#$BRAINSYNCOS_ROOT/}/)"
check product-config       t_product_config
check disk-space           check_free_space "$BUILD_DIR" "$(iso_need_gb "$BASE_ISO_SIZE")"
if [ "$nfail" -gt 0 ]; then log "aborting: prerequisites failed"; exit 1; fi
check builder-image        t_builder_image
check test-login           t_test_login
check test-repo            t_test_repo
check render-grub          in_builder grub
check patch-autoinstall    in_builder autoinstall
check merge-md5sums        in_builder md5
check build-logins         t_build_logins
if [ "$nfail" -gt 0 ]; then log "aborting: generators or login checks failed"; exit 1; fi
check build-iso            t_build
if [ -f "$ISO" ]; then
    check boot-compare     t_boot_compare
    check iso-contents     in_builder contents
    check overlay-layer    in_builder layer
    check install-hook     in_builder hook
    check hook-password    in_builder hook-password
    check hook-failures    in_builder hook-fail
fi

finish "Phase 2"
