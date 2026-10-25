#!/usr/bin/env bash
# testlib.sh - PASS/FAIL check runner shared by tests/test_phase*.sh. Source it
# after scripts/lib.sh and set LOGS (per-check log directory) first.
# Host side: must stay bash 3.2 compatible, like lib.sh.

npass=0
nfail=0
failed=""

# check NAME CMD...: run CMD with output in $LOGS/NAME.log, report PASS/FAIL.
check() {
    local name="$1" t0 rc
    shift
    t0="$(date +%s)"
    # Subshell outside any if/&&/|| so set -e stays in force inside the check.
    set +e
    ( set -e; "$@" ) > "$LOGS/$name.log" 2>&1
    rc=$?
    set -e
    if [ "$rc" = 0 ]; then
        npass=$((npass + 1))
        printf 'PASS  %-22s (%ss)\n' "$name" $(( $(date +%s) - t0 ))
    else
        nfail=$((nfail + 1))
        failed="$failed $name"
        printf 'FAIL  %-22s (rc=%s, log: %s)\n' "$name" "$rc" "${LOGS#$BRAINSYNCOS_ROOT/}/$name.log"
        tail -5 "$LOGS/$name.log" | sed 's/^/      | /'
    fi
    return 0
}

# test_repo EDITION DIR: a throwaway signing key, the edition's packages and
# their signed repository under DIR (relative to brainsyncos/, as the builder
# sees it): DIR/key, DIR/debs, DIR/repo. Never touches config/repo-signing-key.asc.
# The model (brainsyncos-model, 1.3 GB) is left out: the hook checks unpack
# their target on the /scratch tmpfs, and 86-model.sh's install is checked
# in tests/test_phase3.sh.
test_repo() {
    local e="$1" d="$2" b="$BRAINSYNCOS_ROOT/scripts/builder.sh"
    force_rm_tree "${BRAINSYNCOS_ROOT:?}/$d"
    "$b" run scripts/make-repo-key.sh --out "$d/key"
    "$b" run scripts/build-packages.sh --edition "$e" --key "$d/key/repo-signing-key.asc" --out "$d/debs" --skip brainsyncos-model
    "$b" run scripts/build-repo.sh --edition "$e" --debs "$d/debs" --key "$d/key/repo-signing-key.asc" --out "$d/repo"
}

# finish LABEL: print the summary and exit 1 if any check failed.
finish() {
    echo
    if [ "$nfail" -gt 0 ]; then
        echo "$1: $npass passed, $nfail FAILED:$failed"
        exit 1
    fi
    echo "$1: all $npass checks PASSED"
}

# l4t_kernel_stub ISO TARGET VERSION (builder only): dpkg-install
# nvidia-l4t-kernel VERSION from ISO's pool into TARGET, without its
# maintainer scripts, triggers and /boot. NVIDIA's late-commands install the
# real one before the BrainSyncOS hook runs; brainsyncos-ch341-tegra depends
# on exactly that version and is checked against its /lib/modules tree.
l4t_kernel_stub() {
    local iso="$1" t="$2" ver="$3" w f
    w="$(scratch_dir l4t-kernel)"
    xorriso -osirrox on -indev "$iso" -extract "/pool/main/n/nvidia-l4t-kernel/nvidia-l4t-kernel_${ver}_arm64.deb" "$w/k.deb" \
        > /dev/null 2>&1 || die "cannot read nvidia-l4t-kernel $ver from $iso"
    dpkg-deb -R "$w/k.deb" "$w/k"
    for f in preinst postinst prerm postrm triggers; do rm -f "$w/k/DEBIAN/$f"; done
    force_rm_tree "$w/k/boot"
    sed -i '/  boot\//d' "$w/k/DEBIAN/md5sums"
    dpkg-deb --root-owner-group -Znone -b "$w/k" "$t/tmp/nvidia-l4t-kernel-stub.deb" > /dev/null
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- dpkg -i /tmp/nvidia-l4t-kernel-stub.deb > /dev/null \
        || die "dpkg -i of the nvidia-l4t-kernel stub failed in $t"
    rm_released "$t/tmp/nvidia-l4t-kernel-stub.deb"
    force_rm_tree "$w"
    echo "target: nvidia-l4t-kernel $ver from the pool (no maintainer scripts, no /boot)"
}
