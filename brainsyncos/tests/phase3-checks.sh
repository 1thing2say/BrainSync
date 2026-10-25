#!/usr/bin/env bash
# phase3-checks.sh - the in-container halves of tests/test_phase3.sh (builder only).
#
# Usage: scripts/builder.sh run tests/phase3-checks.sh CHECK TESTDIR
#   (TESTDIR: build/tmp/phase3, holding key/, debs/EDITION/, repo/EDITION/
#   from the host-side checks; read only, copies are tampered with)
#   apt       payload/lib/apt-repo.sh against the jetson test repository:
#             installs from a good one (also with the clock at 2001, as on a
#             Jetson without an RTC battery) and leaves no private state;
#             refuses a tampered index, a tampered .deb, an unsigned
#             repository, the wrong key and a missing repository
#   md5map    merge-md5sums.py --map: a mapped tree's files are listed at their
#             ISO paths with correct sums; bad --map arguments are refused
#   hook      payload/hook.sh (00-release.sh + 60-apt-repo.sh only) against
#             NVIDIA's real target base layer (ubuntu-server-minimal.squashfs),
#             curtin via the chroot shim: the repository is mounted read-only
#             for the steps and unmounted afterwards, brainsyncos-archive-keyring
#             is installed (no apt error or warning in the log) and verifies the
#             repository, the target's apt
#             sources and lists are unchanged; a re-run works; a failing later
#             step and a missing repository fail the hook and leave no mount
# Work happens in /scratch (tmpfs).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"
load_config
load_product_config
load_packages_config
require_container
[ "$#" = 2 ] || die "usage: phase3-checks.sh CHECK TESTDIR (see --help)"
case "$1" in -h|--help) usage_from_header; exit 0 ;; esac

T="$BRAINSYNCOS_ROOT/${2#/work/}"
REPO="$T/repo/jetson"
[ -f "$REPO/dists/$REPO_SUITE/InRelease" ] || die "no test repository at $REPO (run tests/test_phase3.sh)"
W=/scratch/p3
force_rm_tree "$W"
mkdir -p "$W"

# payload_for_apt DIR: the minimum lib/apt-repo.sh needs (settings.env, repo-key.gpg).
payload_for_apt() {
    mkdir -p "$1/lib"
    cp "$BRAINSYNCOS_ROOT/payload/lib/apt-repo.sh" "$1/lib/"
    cp "$REPO/repo-key.gpg" "$1/"
    printf 'REPO_SUITE=%s\nREPO_COMPONENT=%s\n' "$REPO_SUITE" "$REPO_COMPONENT" > "$1/settings.env"
}

# apt_run PAYLOAD REPO CMDS: bsos_repo_open, then CMDS, in a fresh bash; output to $W/apt.out.
apt_run() {
    BSOS_INSTALL_DIR="$1" BSOS_REPO_DIR="$2" bash -c \
        'set -euo pipefail; . "$BSOS_INSTALL_DIR/lib/apt-repo.sh"; bsos_repo_open; '"$3" > "$W/apt.out" 2>&1
}

# expect_apt_fail LABEL PATTERN PAYLOAD REPO [CMDS]: apt_run fails and its output matches PATTERN.
expect_apt_fail() {
    if apt_run "$3" "$4" "${5:-true}"; then cat "$W/apt.out"; die "$1: accepted"; fi
    grep -Eq "$2" "$W/apt.out" || { cat "$W/apt.out"; die "$1: failed for another reason"; }
    echo "refuses $1: $(grep -Em1 "$2" "$W/apt.out")"
}

fresh_repo() { force_rm_tree "$W/repo"; cp -a "$REPO" "$W/repo"; }

c_apt() {
    local p="$W/payload" idx="$W/repo/dists/$REPO_SUITE/$REPO_COMPONENT/binary-arm64" deb home
    payload_for_apt "$p"
    fresh_repo
    apt_run "$p" "$W/repo" 'bsos_apt install brainsyncos-archive-keyring; dpkg -s brainsyncos-archive-keyring | grep "^Status:"' \
        || { cat "$W/apt.out"; die "install from the good repository failed"; }
    grep -qx 'Status: install ok installed' "$W/apt.out" || die "brainsyncos-archive-keyring not installed"
    ! ls -d /tmp/bsos-apt.* > /dev/null 2>&1 || die "private apt state left in /tmp"
    echo "installs brainsyncos-archive-keyring from the good repository; no private state left"
    apt_run "$p" "$W/repo" true || die "update of the good repository failed"
    faketime '2001-01-01 00:00:00' bash -c "$(declare -f apt_run); W=$W; apt_run $p $W/repo true" \
        || { cat "$W/apt.out"; die "the repository is refused with the clock at 2001"; }
    echo "accepts the repository with the clock at 2001-01-01 (key and Release from the future)"

    gunzip -c "$idx/Packages.gz" | sed 's/^Version: .*/Version: 99/' | gzip -9n > "$idx/Packages.gz.t"
    mv "$idx/Packages.gz.t" "$idx/Packages.gz"
    expect_apt_fail "a tampered index" 'Hash Sum mismatch' "$p" "$W/repo"
    fresh_repo
    deb="$(ls "$W/repo/pool/$REPO_COMPONENT"/brainsyncos-archive-keyring_*.deb)"
    printf 'x' >> "$deb"
    expect_apt_fail "a tampered .deb" 'Hash Sum mismatch' "$p" "$W/repo" 'bsos_apt install brainsyncos-archive-keyring --reinstall'
    fresh_repo
    rm -f "$W/repo/dists/$REPO_SUITE/InRelease" "$W/repo/dists/$REPO_SUITE/Release.gpg"
    expect_apt_fail "an unsigned repository" 'is not signed' "$p" "$W/repo"
    fresh_repo
    home="$(scratch_dir other-key)"
    GNUPGHOME="$home" gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
        --quick-generate-key 'other key <other@localhost>' ed25519 sign 1y 2> /dev/null
    GNUPGHOME="$home" gpg --batch --export > "$p/repo-key.gpg"
    gpgconf_kill "$home"
    expect_apt_fail "the wrong key" 'NO_PUBKEY|not signed' "$p" "$W/repo"
    cp "$REPO/repo-key.gpg" "$p/repo-key.gpg"
    expect_apt_fail "a missing repository" 'no BrainSyncOS package repository' "$p" "$W/no-such-repo"
}

c_md5map() {
    local m="$W/m"
    mkdir -p "$m/stage/brainsyncos" "$m/tree/pool/main" "$m/tree/dists"
    printf 'a\n' > "$m/stage/brainsyncos/release"
    printf 'deb\n' > "$m/tree/pool/main/x_1_all.deb"
    printf 'rel\n' > "$m/tree/dists/InRelease"
    printf '%s  ./casper/x.squashfs\n' "$(printf 'v' | md5sum | cut -d' ' -f1)" > "$m/vendor.txt"
    python3 "$BRAINSYNCOS_ROOT/scripts/merge-md5sums.py" "$m/vendor.txt" "$m/stage" --map "$m/tree" /brainsyncos-repo > "$m/out.txt"
    cat "$m/out.txt"
    grep -qx "$(md5sum < "$m/tree/pool/main/x_1_all.deb" | cut -d' ' -f1)  ./brainsyncos-repo/pool/main/x_1_all.deb" "$m/out.txt" \
        || die "mapped .deb missing or wrong"
    grep -qx "$(md5sum < "$m/tree/dists/InRelease" | cut -d' ' -f1)  ./brainsyncos-repo/dists/InRelease" "$m/out.txt" \
        || die "mapped InRelease missing or wrong"
    grep -q '  ./brainsyncos/release$' "$m/out.txt" && grep -q '  ./casper/x.squashfs$' "$m/out.txt" \
        || die "stage or vendor entries lost"
    [ "$(wc -l < "$m/out.txt" | tr -d ' ')" = 4 ] || die "expected 4 entries"
    local bad
    for bad in "--map $m/tree" "--map $m/none /x" "--map $m/tree relative" "--map $m/tree /" "--mop $m/tree /x"; do
        # shellcheck disable=SC2086
        if python3 "$BRAINSYNCOS_ROOT/scripts/merge-md5sums.py" "$m/vendor.txt" "$m/stage" $bad > /dev/null 2>&1; then
            die "accepted: $bad"
        fi
    done
    echo "merge-md5sums.py --map: mapped files at their ISO paths; bad --map arguments refused"
}

# hook_payload DIR [EXTRA_STEP]: a payload with only 00-release.sh and
# 60-apt-repo.sh (the identity steps need NVIDIA's late-command state; the
# phase 2 and VM hook checks run the full set).
hook_payload() {
    local d="$1"
    mkdir -p "$d/install.d"
    cp "$BRAINSYNCOS_ROOT/payload/hook.sh" "$d/"
    cp -R "$BRAINSYNCOS_ROOT/payload/lib" "$d/"
    cp "$BRAINSYNCOS_ROOT/payload/install.d/00-release.sh" "$BRAINSYNCOS_ROOT/payload/install.d/60-apt-repo.sh" "$d/install.d/"
    cp "$REPO/repo-key.gpg" "$d/"
    printf 'NAME=BrainSyncOS\nVERSION=%s\nBUILD_ID=phase3\nEDITION=orin-nano\nREPO_KEY=%s\n' \
        "$BRAINSYNCOS_VERSION" "$(key_fingerprint "$REPO/repo-key.gpg")" > "$d/release"
    printf 'BRAINSYNC_USER=%s\nHOSTNAME_PREFIX=%s\nREPO_SUITE=%s\nREPO_COMPONENT=%s\n' \
        "$BRAINSYNC_USER" "$HOSTNAME_PREFIX" "$REPO_SUITE" "$REPO_COMPONENT" > "$d/settings.env"
}

run_hook() {  # run_hook PAYLOAD TARGET REPO -> hook's exit code
    PATH="$BRAINSYNCOS_ROOT/tests/shims:$PATH" TARGET_MOUNT_POINT="$2" BSOS_REPO_SRC="$3" \
        BSOS_HOOK_LOG="$W/live.log" BSOS_DT_DIR="$W/dt" bash "$1/hook.sh"
}

apt_state() {  # apt_state TARGET: the target's apt sources and lists, one line per file
    (cd "$1" && find etc/apt/sources.list etc/apt/sources.list.d var/lib/apt/lists -type f -print0 2>/dev/null \
        | xargs -0r sha256sum | LC_ALL=C sort -k2)
}

no_mount_left() {  # no_mount_left TARGET
    ! mountpoint -q "$1/tmp/brainsyncos-repo" || die "the repository is still mounted in the target"
    [ ! -e "$1/tmp/brainsyncos-repo" ] || die "the mount point was left in the target"
    ! grep -q " $1/" /proc/mounts || die "mounts left under the target: $(grep " $1/" /proc/mounts)"
}

c_hook() {
    local t="$W/target" p="$W/hp" rc log="$W/target/var/log/brainsyncos/install.log"
    resolve_base_iso > /dev/null
    xorriso -osirrox on -indev "$(resolve_base_iso)" -extract /casper/ubuntu-server-minimal.squashfs "$W/t.squashfs" \
        > /dev/null 2>&1 || die "cannot read the target base layer from the base ISO"
    unsquashfs -q -no-progress -d "$t" "$W/t.squashfs" > /dev/null
    rm -f "$W/t.squashfs"
    command -v gpgv > /dev/null && [ -x "$t/usr/bin/apt-get" ] && [ -x "$t/usr/bin/gpgv" ] \
        || die "the target layer lacks apt-get or gpgv"
    mkdir -p "$W/dt"
    printf 'NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super\0' > "$W/dt/model"
    printf '1421224ABC123456\0' > "$W/dt/serial-number"
    hook_payload "$p"
    apt_state "$t" > "$W/apt-before"

    run_hook "$p" "$t" "$REPO" || { tail -20 "$W/live.log"; die "hook failed"; }
    grep -q 'package repository .* mounted at /tmp/brainsyncos-repo (read-only)' "$log" || die "hook did not mount the repository"
    grep -q '60-apt-repo.sh: repository verified; brainsyncos-archive-keyring ' "$log" || die "60-apt-repo.sh did not finish"
    grep -q 'BrainSyncOS layer installed (2 steps)' "$log" || die "log lacks the summary"
    ! grep -n '^E: \|^W: \|WARNING' "$log" || die "the install logged an apt error or warning"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- dpkg -s brainsyncos-archive-keyring | grep -qx 'Status: install ok installed' \
        || die "brainsyncos-archive-keyring is not installed in the target"
    gpgv --keyring "$t/usr/share/keyrings/brainsyncos-archive-keyring.gpg" "$REPO/dists/$REPO_SUITE/InRelease" > /dev/null 2>&1 \
        || die "the installed keyring does not verify the repository"
    apt_state "$t" > "$W/apt-after"
    diff "$W/apt-before" "$W/apt-after" || die "the target's apt sources or lists changed"
    ! ls -d "$t"/tmp/bsos-apt.* > /dev/null 2>&1 || die "private apt state left in the target"
    no_mount_left "$t"
    grep -q '60-apt-repo.sh' "$log" && sed -n '/== 60-apt-repo.sh/,$p' "$log" | head -12
    echo "hook: repository mounted read-only, keyring installed and verifying, target apt state unchanged, unmounted"

    run_hook "$p" "$t" "$REPO" > /dev/null 2>&1 || { tail -20 "$W/live.log"; die "re-run failed"; }
    no_mount_left "$t"
    echo "re-run: OK"

    cp -R "$p" "$W/hp-fail"
    printf '#!/bin/bash\necho failing on purpose; exit 3\n' > "$W/hp-fail/install.d/70-fail.sh"
    set +e; run_hook "$W/hp-fail" "$t" "$REPO" > "$W/fail.out" 2>&1; rc=$?; set -e
    [ "$rc" != 0 ] || die "hook succeeded although 70-fail.sh failed"
    no_mount_left "$t"
    echo "failing later step: hook rc=$rc, repository unmounted"

    set +e; run_hook "$p" "$t" "$W/no-such-repo" > "$W/norepo.out" 2>&1; rc=$?; set -e
    [ "$rc" != 0 ] || die "hook succeeded without a package repository"
    grep -q 'no package repository at' "$W/norepo.out" || { cat "$W/norepo.out"; die "missing repository: wrong error"; }
    no_mount_left "$t"
    echo "missing repository: hook rc=$rc ($(grep -m1 'no package repository' "$W/norepo.out"))"
}

case "$1" in
    apt) c_apt ;;
    md5map) c_md5map ;;
    hook) c_hook ;;
    *) die "unknown check: $1 (see --help)" ;;
esac
force_rm_tree "$W"
