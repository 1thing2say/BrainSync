#!/usr/bin/env bash
# test_phase1.sh - run the whole Phase-1 chain for real and check every step (host side).
#
# Usage: BASE_ISO=/path/to/base.iso tests/test_phase1.sh [--reuse]
#
# Builds the builder image, checks that Ctrl-C stops a non-TTY builder job,
# verifies the base ISO (full re-hash), extracts it fresh (--reuse keeps an
# existing up-to-date build/iso-root), checks that a second extraction is a
# no-op, writes the inventory report, does a no-op repack and compares the
# result's El Torito/GPT/ESP/ISO9660+Joliet names/file tree with the base.
# The roundtrip ISO (~5 GB) is always deleted at the end. Per-check logs go to
# build/test-logs/phase1/. Prints PASS/FAIL per check; exits 1 on any failure.

BSOS_SCRIPT=test_phase1.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"

load_config

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
LOGS="$BUILD_DIR/test-logs/phase1"
RT_NAME=phase1-roundtrip
RT_ISO="$OUT_DIR/$RT_NAME.iso"
mkdir -p "$LOGS"
rm -f "$LOGS"/*.log
# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"

# rm_released truncates before unlinking: Docker Desktop keeps deleted
# bind-mounted files open, which would otherwise pin ~5 GB per run.
cleanup_roundtrip() {
    rm_released "$RT_ISO" "$RT_ISO.sha256" "$OUT_DIR/$RT_NAME.xorriso.log" "$OUT_DIR/.$RT_NAME.iso.partial" \
        "$OUT_DIR/$RT_NAME.tree-changes.txt"
}
trap cleanup_roundtrip EXIT

# --- checks -------------------------------------------------------------------

t_config_host() {
    # Explicitly the macOS system bash (3.2) when present, to prove compatibility.
    local sh=/bin/bash
    "$sh" --version | head -1
    "$sh" -c 'source "$1/scripts/lib.sh"; load_config
        for k in $BSOS_REQUIRED_KEYS; do eval "printf \"%s=%s\n\" $k \"\$$k\""; done' _ "$BRAINSYNCOS_ROOT"
}

t_disk_space() { check_free_space "$BUILD_DIR" "$MIN_FREE_GB"; }

t_builder_image() {
    "$B" build
    [ "$("$B" run uname -m | tr -d '\r')" = aarch64 ] || die "builder is not aarch64"
}

t_config_container() {
    "$B" run bash -c 'source scripts/lib.sh; load_config; echo "lib.sh in $(bash --version | head -1)"; echo "$BASE_ISO_SHA256"' \
        | tee /dev/stderr | grep -qx "$BASE_ISO_SHA256"
    # The same file must work as a docker --env-file (no quoting tricks).
    [ "$(docker run --rm --platform "$BUILDER_PLATFORM" --env-file "$BSOS_CONFIG" "$("$B" image)" \
        printenv BASE_ISO_SHA256)" = "$BASE_ISO_SHA256" ] || die "--env-file did not round-trip BASE_ISO_SHA256"
    echo "docker --env-file OK"
}

t_verify_iso() { "$B" run scripts/verify-base-iso.sh --no-cache; }

t_verify_host() { /bin/bash "$S/verify-base-iso.sh"; }

t_verify_rejects() {
    # A wrong pin must fail with the failing check named.
    local bad="$TMP_DIR/bad-base-iso.env" out
    mkdir -p "$TMP_DIR"
    sed 's/^BASE_ISO_SIZE=.*/BASE_ISO_SIZE=12345/' "$BRAINSYNCOS_ROOT/config/base-iso.env" > "$bad"
    if out="$(BSOS_CONFIG="$bad" /bin/bash "$S/verify-base-iso.sh" 2>&1)"; then
        rm -f "$bad"; die "verify accepted a wrong size pin"
    fi
    rm -f "$bad"
    printf '%s\n' "$out"
    printf '%s\n' "$out" | grep -q 'verify FAILED \[size\]'
}

t_extract() {
    if [ "$reuse" = 1 ]; then "$B" run scripts/extract-iso.sh; else "$B" run scripts/extract-iso.sh --force; fi
}

t_extract_layout() {
    [ -L "$ISO_ROOT/ubuntu" ] && [ "$(readlink "$ISO_ROOT/ubuntu")" = . ] || die "/ubuntu -> . not preserved"
    [ "$(readlink "$ISO_ROOT/dists/stable")" = noble ] || die "/dists/stable -> noble not preserved"
    local ro
    ro="$(find "$ISO_ROOT" ! -type l ! -perm -u+w | head -5)"
    [ -z "$ro" ] || die "not owner-writable: $ro"
    local f
    for f in el-torito.plain.txt el-torito.as_mkisofs.txt system-area.plain.txt partitions.sfdisk.txt esp.env esp.sha256 tree.txt md5sum.orig.txt builder-packages.txt extract.stamp; do
        [ -s "$META_DIR/$f" ] || die "missing build/meta/$f"
    done
    [ ! -e "$BUILD_DIR/.iso-root.tmp" ] && [ ! -e "$BUILD_DIR/.meta.tmp" ] || die "temp extraction dirs left behind"
    echo "nodes: $(wc -l < "$META_DIR/tree.txt" | tr -d ' '), files: $(grep -c '^f ' "$META_DIR/tree.txt")"
}

# Fresh extraction == base ISO tree (content/type/links), modulo u+w and symlink mtimes.
t_extract_fidelity() { "$B" run bash -c 'scripts/compare-iso.sh --tree-only --extraction "$BASE_ISO"'; }

t_extract_idempotent() {
    local before after out
    before="$(ls -lid "$ISO_ROOT" | awk '{print $1}') $(file_mtime "$META_DIR/extract.stamp") $(file_mtime "$ISO_ROOT")"
    out="$("$B" run scripts/extract-iso.sh 2>&1)"
    printf '%s\n' "$out"
    printf '%s\n' "$out" | grep -q 'up to date' || die "second extraction did work"
    after="$(ls -lid "$ISO_ROOT" | awk '{print $1}') $(file_mtime "$META_DIR/extract.stamp") $(file_mtime "$ISO_ROOT")"
    [ "$before" = "$after" ] || die "iso-root/stamp changed: [$before] -> [$after]"
    echo "unchanged: inode/stamp-mtime/root-mtime $after"
}

t_inspect() {
    local r="$REPORTS_DIR/base-iso-inventory.md"
    rm -f "$r"
    "$B" run scripts/inspect-iso.sh
    [ -s "$r" ] || die "no report at $r"
    grep -q "casper/$AUTOINSTALL_LAYER" "$r" || die "report does not name the autoinstall layer"
    grep -q '/ai/preseed.sh' "$r" || die "report lacks the autoinstall contents"
    grep -q '^\*\*Location:\*\* `/autoinstall.yaml`' "$r" || die "report lacks the autoinstall location line"
    grep -q 'nvidia-l4t-kernel.*'"$TEGRA_KERNEL" "$r" || die "report lacks the kernel version"
    grep -q '0 failing `md5sum -c`' "$r" || die "md5sum.txt does not verify"
    cmp "$r" "$BRAINSYNCOS_ROOT/docs/base-iso-inventory.md" || die "tracked docs/base-iso-inventory.md not updated"
    ! grep -n "$HOME" "$r" || die "report contains host paths"
    echo "report: $(wc -l < "$r" | tr -d ' ') lines (tracked copy: docs/base-iso-inventory.md)"
    grep -E '^\*\*Location|^\| `ubuntu-server-minimal' "$r"
}

t_repack() {
    "$B" run scripts/repack-iso.sh --name "$RT_NAME" --expect-noop
    [ -f "$RT_ISO" ] || die "no ISO at $RT_ISO"
    [ ! -s "$OUT_DIR/$RT_NAME.tree-changes.txt" ] || die "tree changes in a no-op repack"
    cmp "$ISO_ROOT/md5sum.txt" "$META_DIR/md5sum.orig.txt" || die "regenerated md5sum.txt differs from the original"
    echo "md5sum.txt regenerated byte-identical to the original"
}

t_roundtrip_compare() {
    local out
    out="$("$B" run scripts/compare-iso.sh "build/out/$RT_NAME.iso" 2>&1)" || { printf '%s\n' "$out"; return 1; }
    printf '%s\n' "$out"
    # The plain ISO9660 and Joliet names must equal the base's, not just Rock Ridge.
    printf '%s\n' "$out" | grep -q '^PASS names .*iso9660 [0-9]* names equal (+0 added); joliet [0-9]* names equal (+0 added)' \
        || die "names check missing or not an exact match"
}

# Ctrl-C must stop an in-container job even without a TTY (tini via --init).
t_interrupt() {
    local img pid i alive
    img="$("$B" image)"
    "$B" run sleep 97 < /dev/null > "$LOGS/interrupt-job.log" 2>&1 &
    pid=$!
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        docker ps --filter "ancestor=$img" --format '{{.Command}}' | grep -q 'sleep 97' && break
        sleep 1
    done
    docker ps --filter "ancestor=$img" --format '{{.Command}}' | grep -q 'sleep 97' || die "test container did not start"
    kill -INT "$pid"
    alive=1
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        if ! kill -0 "$pid" 2>/dev/null; then alive=0; break; fi
        sleep 1
    done
    if [ "$alive" = 1 ] || docker ps --filter "ancestor=$img" --format '{{.Command}}' | grep -q 'sleep 97'; then
        docker ps -q --filter "ancestor=$img" | xargs docker rm -f > /dev/null 2>&1 || true
        kill "$pid" 2>/dev/null || true
        die "SIGINT did not stop the builder job (cli alive=$alive)"
    fi
    wait "$pid" 2>/dev/null || echo "builder.sh run exited rc=$? after SIGINT"
    echo "SIGINT stopped the container within ${i}s"
}

t_cleanup() {
    cleanup_roundtrip
    [ ! -e "$RT_ISO" ] || die "roundtrip ISO still present"
    echo "removed $RT_ISO"
    # A deleted file may stay open in the Docker VM, but it must hold no blocks.
    local held
    if command -v lsof >/dev/null 2>&1; then
        held="$(lsof +L1 2>/dev/null | awk -v n="$RT_NAME.iso" 'index($0, n) && $7 > 0' || true)"
        [ -z "$held" ] || die "deleted roundtrip ISO still holds disk space: $held"
        echo "no deleted-but-allocated roundtrip ISO (lsof +L1)"
    fi
    df -h "$BUILD_DIR" | tail -1
}

# --- run ----------------------------------------------------------------------

log "Phase 1 test chain (logs: ${LOGS#$BRAINSYNCOS_ROOT/}/)"
check config-host          t_config_host
check disk-space           t_disk_space
if [ "$nfail" -gt 0 ]; then log "aborting: prerequisites failed"; exit 1; fi
check builder-image        t_builder_image
check config-container     t_config_container
check interrupt            t_interrupt
check verify-iso           t_verify_iso
check verify-iso-host      t_verify_host
check verify-rejects-bad   t_verify_rejects
if [ "$nfail" -gt 0 ]; then log "aborting: base ISO or builder not usable"; exit 1; fi
check extract              t_extract
check extract-layout       t_extract_layout
check extract-fidelity     t_extract_fidelity
check extract-idempotent   t_extract_idempotent
check inspect-report       t_inspect
check repack-noop          t_repack
if [ -f "$RT_ISO" ]; then
    check roundtrip-compare t_roundtrip_compare
fi
check cleanup-roundtrip    t_cleanup

finish "Phase 1"
