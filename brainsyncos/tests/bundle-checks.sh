#!/usr/bin/env bash
# bundle-checks.sh - the in-container halves of tests/test_bundle.sh (builder only).
#
# Usage: scripts/builder.sh run tests/bundle-checks.sh CHECK TESTDIR
#   (TESTDIR: build/tmp/bundle-test, holding out/brainsyncos-jetson-VERSION.tar from
#   the host side, and repo-path, a file naming the repository the tar was built from)
#   tar       the tar: GNU format, every member owned 0/0 with the mtime
#             BSOS_SOURCE_DATE_EPOCH, install.sh executable, the steps and lib
#             byte-identical to payload/, the key's fingerprint = REPO_KEY in
#             bundle.env and release, the unpacked repository validates
#             (validate_repo) and is the one the tar was built from
#   install   payload/bundle/install.sh from the unpacked tar in NVIDIA's real
#             target base layer (ubuntu-server-minimal.squashfs, on disk under
#             TESTDIR, with nvidia-l4t-kernel from the pool, a fake
#             /etc/nv_tegra_release for R39.2.1 and an Orin Nano device tree),
#             chrooted through the curtin shim, the bundle bind-mounted
#             read-only: creates the brainsync account through 30-user.sh
#             (password hash), runs steps 60 to 86 in order with their own
#             log lines and no apt error or warning, records
#             /etc/brainsyncos-release (INSTALL_METHOD=bundle), leaves no apt
#             state; a re-run (no --user) takes the same account and keeps
#             secrets.env and the llama key; --user with an existing account
#             adds dialout and the other hardware groups; refuses a wrong
#             nv_tegra_release (R36.4.3) before touching anything, an account
#             to create while NVIDIA's `nvidia` exists, an account to create
#             without logins, and a repository whose key differs
# Work happens on disk under TESTDIR (the unpacked bundle and the target
# together are several GB, more than /scratch holds).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"
load_config
load_product_config
load_packages_config
require_container
[ "$#" = 2 ] || die "usage: bundle-checks.sh CHECK TESTDIR (see --help)"
case "$1" in -h|--help) usage_from_header; exit 0 ;; esac

# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"
T="$BRAINSYNCOS_ROOT/${2#/work/}"
NAME="brainsyncos-jetson-$BRAINSYNCOS_VERSION"
TAR="$T/out/$NAME.tar"
[ -s "$TAR" ] || die "no bundle at $TAR (run tests/test_bundle.sh)"
[ -s "$T/repo-path" ] || die "no $T/repo-path (the repository the tar was built from; tests/test_bundle.sh writes it)"
REPO="$BRAINSYNCOS_ROOT/$(sed 's|^/work/||' "$T/repo-path")"
W="$T/work"
force_rm_tree "$W"
mkdir -p "$W"

in_t() { "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$1" -- "${@:2}"; }

# unpack DIR: the tar unpacked at DIR/NAME; prints the bundle directory.
unpack() {
    mkdir -p "$1"
    tar -xf "$TAR" -C "$1"
    [ -s "$1/$NAME/install.sh" ] || die "the tar did not unpack to $1/$NAME"
    printf '%s\n' "$1/$NAME"
}

c_tar() {
    local epoch="${BSOS_SOURCE_DATE_EPOCH:?builder.sh sets it}" when b f fpr
    file "$TAR" | grep -q 'POSIX tar archive (GNU)' || die "not a GNU-format tar: $(file "$TAR")"
    LC_ALL=C tar -tvf "$TAR" --numeric-owner --full-time > "$W/list"
    when="$(date -u -d "@$epoch" '+%Y-%m-%d %H:%M:%S')"
    ! awk '$2 != "0/0"' "$W/list" | grep -q . || die "members not owned 0/0: $(awk '$2 != "0/0"' "$W/list" | head -3)"
    ! grep -v " $when " "$W/list" | grep -q . || die "members whose mtime is not @$epoch: $(grep -v " $when " "$W/list" | head -3)"
    ! awk '{print $1}' "$W/list" | grep -Eq '^.{5}w|^.{8}w' || die "a member is group- or world-writable"
    grep -Eq "^-rwxr-xr-x 0/0 .* $NAME/install.sh$" "$W/list" || die "install.sh is not 0755: $(grep "$NAME/install.sh$" "$W/list")"
    echo "tar: GNU format, $(wc -l < "$W/list" | tr -d ' ') members, all 0/0, mtime $when (@$epoch), install.sh 0755"

    b="$(unpack "$W/x")"
    for f in lib/apt-repo.sh install.d/60-apt-repo.sh install.d/70-brainsync.sh install.d/75-usb-serial.sh \
            install.d/80-cuda.sh install.d/85-llama.sh install.d/86-model.sh; do
        cmp "$b/$f" "$BRAINSYNCOS_ROOT/payload/$f" || die "$f differs from payload/"
    done
    cmp "$b/account/30-user.sh" "$BRAINSYNCOS_ROOT/payload/install.d/30-user.sh" || die "account/30-user.sh differs from payload/"
    cmp "$b/install.sh" "$BRAINSYNCOS_ROOT/payload/bundle/install.sh" || die "install.sh differs from payload/bundle/"
    [ "$(ls "$b/install.d" | wc -l | tr -d ' ')" = 6 ] || die "install.d holds $(ls "$b/install.d" | tr '\n' ' ')"
    fpr="$(key_fingerprint "$b/repo-key.gpg")"
    grep -qx "REPO_KEY=$fpr" "$b/bundle.env" && grep -qx "REPO_KEY=$fpr" "$b/release" || die "REPO_KEY is not the key's fingerprint $fpr"
    load_env_file "$b/bundle.env" "VERSION EDITION L4T_RELEASE TEGRA_KERNEL KERNEL_RELEASE REPO_SUITE REPO_COMPONENT BRAINSYNC_USER INSTALLED_KIB PACKAGES"
    [ "$VERSION" = "$BRAINSYNCOS_VERSION" ] && [ "$L4T_RELEASE" = "$(sed -n 's/^L4T_RELEASE=//p' "$BSOS_CONFIG")" ] \
        && [ "$TEGRA_KERNEL" = "$(sed -n 's/^TEGRA_KERNEL=//p' "$BSOS_CONFIG")" ] && [ "$EDITION" = orin-nano ] \
        || die "bundle.env: VERSION=$VERSION L4T_RELEASE=$L4T_RELEASE TEGRA_KERNEL=$TEGRA_KERNEL EDITION=$EDITION"
    validate_repo "$b/repo" jetson
    [ "$(find "$b/repo/pool" -name '*.deb' | wc -l | tr -d ' ')" = "$PACKAGES" ] || die "pool holds $(find "$b/repo/pool" -name '*.deb' | wc -l) .debs, PACKAGES=$PACKAGES"
    cmp "$b/repo/dists/$REPO_SUITE/InRelease" "$REPO/dists/$REPO_SUITE/InRelease" || die "the unpacked repository is not $REPO"
    [ "$INSTALLED_KIB" = "$(awk '/^Installed-Size: / {s += $2} END {printf "%d", s}' "$b/repo/dists/$REPO_SUITE/$REPO_COMPONENT/binary-arm64/Packages")" ] \
        || die "INSTALLED_KIB=$INSTALLED_KIB does not match the index"
    echo "unpacked: steps and lib identical to payload/, key $fpr, repository validates ($PACKAGES packages, Installed-Size $((INSTALLED_KIB / 1024)) MiB)"
    force_rm_tree "$W/x"
}

# run_install TARGET BUNDLE_IN_TARGET LOG ARGS...: install.sh in TARGET through the
# curtin shim, with the test's device tree and log; the exit code.
run_install() {
    local t="$1" b="$2" log="$3"
    shift 3
    local e=(env BSOS_DT_DIR=/tmp/dt BSOS_INSTALL_LOG="$log")
    [ -z "${BSOS_NV_TEGRA_RELEASE:-}" ] || e+=(BSOS_NV_TEGRA_RELEASE="$BSOS_NV_TEGRA_RELEASE")
    in_t "$t" "${e[@]}" /bin/bash "$b/install.sh" "$@"
}

c_install() {
    local t="$T/target" b bt=/mnt/bundle log="$T/target/var/log/brainsyncos/install.log" rc out kver pkgs npkgs p
    load_env_file "$BRAINSYNCOS_ROOT/llama/llama.env" "CUDA_RUNTIME"
    ls "$REPO/pool/$REPO_COMPONENT"/cuda-cudart-13-2_*.deb > /dev/null 2>&1 \
        || die "the repository has no CUDA runtime, so 80-cuda.sh and 85-llama.sh cannot pass (scripts/build.sh mirror, then repo)"
    kver="$(sed -n 's/^KERNEL_RELEASE=//p' "$BRAINSYNCOS_ROOT/packages/brainsyncos-ch341-tegra/package.env")"
    b="$(unpack "$T/bundle")"

    # NVIDIA's target base layer, on disk (target_layer puts it on /scratch), as
    # a stock JetPack would have left it: the L4T kernel, nv_tegra_release.
    force_rm_tree "$t"
    xorriso -osirrox on -indev "$(resolve_base_iso)" -extract /casper/ubuntu-server-minimal.squashfs "$W/t.squashfs" \
        > /dev/null 2>&1 || die "cannot read the target base layer from the base ISO"
    unsquashfs -q -no-progress -no-xattrs -d "$t" "$W/t.squashfs" > /dev/null
    rm_released "$W/t.squashfs"
    l4t_kernel_stub "$(resolve_base_iso)" "$t" "$TEGRA_KERNEL"
    printf '# R39 (release), REVISION: 2.1, GCID: 40366578, BOARD: generic, EABI: aarch64, DATE: Fri Aug  8 01:39:33 UTC 2026\n' \
        > "$t/etc/nv_tegra_release"
    printf '# R36 (release), REVISION: 4.3, GCID: 38968081, BOARD: generic, EABI: aarch64, DATE: Wed Jan  8 01:49:37 UTC 2025\n' \
        > "$t/tmp/nv36"
    mkdir -p "$t/tmp/dt"
    printf 'NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super\0' > "$t/tmp/dt/model"
    printf '1421224ABC123456\0' > "$t/tmp/dt/serial-number"
    openssl passwd -6 'bundle-test' > "$t/tmp/pw.hash"
    mkdir -p "$t$bt"
    mount --bind "$b" "$t$bt"
    mount -o remount,bind,ro "$t$bt"
    # shellcheck disable=SC2064  # expand now: local paths
    trap "umount '$t$bt' 2>/dev/null || umount -l '$t$bt' || true" EXIT

    # Refusals first: nothing is installed by them.
    set +e; BSOS_NV_TEGRA_RELEASE=/tmp/nv36 run_install "$t" "$bt" /tmp/refuse.log --user brainsync --password-hash /tmp/pw.hash --yes > "$W/r1.out" 2>&1; rc=$?; set -e
    [ "$rc" != 0 ] || die "install.sh accepted Jetson Linux R36.4.3"
    grep -q 'this system runs Jetson Linux R36.4.3; the bundle is built for R39.2.1' "$W/r1.out" || { cat "$W/r1.out"; die "R36: wrong refusal"; }
    ! grep -q '== ' "$t/tmp/refuse.log" && [ ! -e "$t/opt/brainsyncos/installer" ] && [ ! -e "$t/etc/brainsyncos-release" ] \
        || die "the refused run touched the system"
    echo "refuses R36.4.3: $(grep -m1 'ERROR' "$W/r1.out")"
    set +e; run_install "$t" "$bt" /tmp/refuse.log --user brainsync --yes --skip-space-check > "$W/r2.out" 2>&1; rc=$?; set -e
    [ "$rc" != 0 ] && grep -q 'creating brainsync needs --authorized-keys' "$W/r2.out" || { cat "$W/r2.out"; die "an account without logins was not refused"; }
    echo "refuses a new account without logins: $(grep -m1 'ERROR' "$W/r2.out")"
    in_t "$t" useradd -M -s /bin/bash nvidia
    set +e; run_install "$t" "$bt" /tmp/refuse.log --user brainsync --password-hash /tmp/pw.hash --yes --skip-space-check > "$W/r3.out" 2>&1; rc=$?; set -e
    [ "$rc" != 0 ] && grep -q "removes NVIDIA's fallback account nvidia" "$W/r3.out" || { cat "$W/r3.out"; die "30-user.sh would have removed nvidia"; }
    in_t "$t" userdel nvidia
    ! in_t "$t" id brainsync > /dev/null 2>&1 || die "a refused run created the account"
    echo "refuses to create an account while nvidia exists: $(grep -m1 'ERROR' "$W/r3.out")"
    mkdir -p "$t/tmp/badrepo"
    cp -R "$b/repo/dists" "$t/tmp/badrepo/dists"
    cp "$b/repo/edition" "$t/tmp/badrepo/"
    head -c 200 /dev/urandom > "$t/tmp/badrepo/repo-key.gpg"
    set +e; run_install "$t" "$bt" /tmp/refuse.log --user brainsync --password-hash /tmp/pw.hash --yes --repo /tmp/badrepo > "$W/r4.out" 2>&1; rc=$?; set -e
    [ "$rc" != 0 ] && grep -q "is not the bundle's key" "$W/r4.out" || { cat "$W/r4.out"; die "a repository with another key was not refused"; }
    force_rm_tree "$t/tmp/badrepo"
    echo "refuses a repository whose key is not the bundle's"

    # The install: a new account from a password hash, then steps 60 to 86.
    run_install "$t" "$bt" "" --user brainsync --password-hash /tmp/pw.hash --yes > "$W/install.out" 2>&1 \
        || { tail -40 "$W/install.out"; die "install.sh failed"; }
    grep -q 'install.sh: Jetson Linux R39.2.1: matches the bundle' "$log" || die "no release check line"
    grep -q "install.sh: nvidia-l4t-kernel $TEGRA_KERNEL: the one the CH340 module is built for" "$log" || die "no kernel check line"
    grep -q 'install.sh: board: NVIDIA Jetson Orin Nano' "$log" || die "no board line"
    grep -q 'install.sh: account: brainsync does not exist; 30-user.sh creates it' "$log" && grep -q '30-user.sh: brainsync (uid=' "$log" \
        || die "30-user.sh did not create the account"
    grep -q '30-user.sh: console password set; sudo asks for it' "$log" || die "the password hash was not used"
    for p in 60-apt-repo.sh 70-brainsync.sh 75-usb-serial.sh 80-cuda.sh 85-llama.sh 86-model.sh; do
        grep -q "install.sh: == $p" "$log" || die "step $p was not run"
    done
    grep -q 'install.sh: BrainSyncOS layer installed (6 steps); the apps'"'"' account is brainsync' "$log" || die "log lacks the summary"
    ! grep -n '^E: \|^W: \|WARNING' "$log" || die "the install logged an apt error or warning"
    grep -q '60-apt-repo.sh: repository verified; brainsyncos-archive-keyring ' "$log" || die "60-apt-repo.sh did not finish"
    grep -q '70-brainsync.sh: brainsync .*, brainsync-runtime .* installed; [0-9]* commands in /usr/bin' "$log" || die "70-brainsync.sh did not finish"
    grep -q "70-brainsync.sh: /etc/brainsync/secrets.env created (template, no keys), root:brainsync 640" "$log" || die "secrets.env not made for the account"
    grep -q "75-usb-serial.sh: ch341 for $kver: /lib/modules/$kver/kernel/drivers/usb/serial/usbserial.ko /lib/modules/$kver/updates/ch341.ko" "$log" \
        || die "75-usb-serial.sh did not resolve ch341"
    grep -q '80-cuda.sh: installed cuda-cudart-13-2 .*, libcublas-13-2 ' "$log" || die "80-cuda.sh did not finish"
    grep -q "85-llama.sh: installed brainsyncos-llama $BRAINSYNCOS_VERSION, brainsyncos-llama-cuda $BRAINSYNCOS_VERSION; llama-server version: " "$log" \
        || die "85-llama.sh did not finish"
    grep -q '86-model.sh: .* installed: /opt/brainsyncos/models/.* sha256 verified' "$log" \
        || grep -q '86-model.sh: orin-nano edition: the repository has no model package' "$log" || die "86-model.sh did not finish"
    grep -q 'install.sh: services: systemd is not running here' "$log" || die "install.sh tried to start services in the chroot"
    grep -qx 'INSTALL_METHOD=bundle' "$t/etc/brainsyncos-release" && grep -qx 'BRAINSYNC_USER=brainsync' "$t/etc/brainsyncos-release" \
        && grep -qx "VERSION=$BRAINSYNCOS_VERSION" "$t/etc/brainsyncos-release" && grep -q '^INSTALLED=' "$t/etc/brainsyncos-release" \
        || { cat "$t/etc/brainsyncos-release"; die "/etc/brainsyncos-release is wrong"; }
    grep -qx 'BRAINSYNC_USER=brainsync' "$t/opt/brainsyncos/installer/settings.env" && grep -qx 'BSOS_EDITION=orin-nano' "$t/opt/brainsyncos/installer/device.env" \
        || die "settings.env or device.env in the target are wrong"
    [ "$(stat -c '%a' "$t/opt/brainsyncos/installer/config")" = 700 ] || die "the logins in the target are not root-only"
    in_t "$t" id -nG brainsync | tr ' ' '\n' | grep -qx dialout || die "brainsync is not in dialout"
    [ "$(stat -c '%a %U' "$t/etc/brainsync/secrets.env"):$(in_t "$t" stat -c %G /etc/brainsync/secrets.env)" = "640 root:brainsync" ] \
        || die "secrets.env is $(stat -c '%a %U:%G' "$t/etc/brainsync/secrets.env")"
    pkgs="brainsyncos-archive-keyring brainsync-runtime brainsync brainsync-udev brainsyncos-ch341-tegra brainsyncos-llama brainsyncos-llama-cuda $(printf '%s\n' "$CUDA_RUNTIME" | tr ',' '\n' | sed 's/=.*//' | tr '\n' ' ')"
    npkgs="$(printf '%s\n' "$pkgs" | wc -w | tr -d ' ')"
    # shellcheck disable=SC2086  # one package name per word
    [ "$(in_t "$t" dpkg-query -W -f '${Status}\n' $pkgs | grep -cx 'install ok installed')" = "$npkgs" ] \
        || die "not all packages are installed: $(in_t "$t" dpkg-query -W -f '${Package} ${Status}\n' $pkgs | grep -v 'install ok installed')"
    [ -L "$t/etc/systemd/system/multi-user.target.wants/brainsyncos-llama.service" ] || die "the llama service is not enabled"
    ! ls -d "$t"/tmp/bsos-apt.* > /dev/null 2>&1 || die "private apt state left"
    [ -z "$(find "$t/etc/apt/sources.list.d" -name 'brainsyncos*' 2>/dev/null)" ] || die "an apt source was left behind"
    sed -n '/== 60-apt-repo.sh/,$p' "$log" | grep '^install.sh: \|^[0-9][0-9]-[a-z-]*\.sh: ' | head -40
    echo "install: account made by 30-user.sh, 6 steps, $npkgs packages, release recorded, no apt state left"

    # A re-run: the same account without --user, nothing re-made.
    sha256sum "$t/etc/brainsync/secrets.env" "$t/etc/brainsyncos/llama/api-key" > "$W/keep.sum"
    run_install "$t" "$bt" "" --yes > "$W/rerun.out" 2>&1 || { tail -30 "$W/rerun.out"; die "re-run failed"; }
    grep -q "install.sh: account: brainsync (the last install's" "$W/rerun.out" || die "the re-run did not take the last account"
    grep -q 'install.sh: account: brainsync (existing); groups added: none' "$W/rerun.out" || die "the re-run changed the account's groups"
    grep -q 'install.sh: BrainSyncOS layer installed (6 steps)' "$W/rerun.out" || die "re-run: no summary"
    grep -q '70-brainsync.sh: /etc/brainsync/secrets.env exists (root:brainsync 640); left as it is' "$W/rerun.out" \
        && grep -q '85-llama.sh: keeping the API key' "$W/rerun.out" || die "re-run: secrets.env or the key were remade"
    sha256sum -c --quiet "$W/keep.sum" || die "the re-run changed secrets.env or the llama key"
    ! grep -n '^E: \|^W: \|WARNING' "$W/rerun.out" || die "the re-run logged an apt error or warning"
    echo "re-run: same account, secrets.env and the llama key unchanged"

    # An existing account (JetPack's first-boot user): hardware groups, never sudo.
    in_t "$t" useradd -m -s /bin/bash operator
    run_install "$t" "$bt" "" --user operator --yes > "$W/op.out" 2>&1 || { tail -30 "$W/op.out"; die "install.sh --user operator failed"; }
    grep -q 'install.sh: account: operator (existing); groups added: dialout' "$W/op.out" || { grep 'account:' "$W/op.out"; die "operator did not get the groups"; }
    out="$(in_t "$t" id -nG operator | tr ' ' '\n')"
    printf '%s\n' "$out" | grep -qx dialout && ! printf '%s\n' "$out" | grep -qx sudo || die "operator's groups: $(printf '%s' "$out" | tr '\n' ' ')"
    grep -qx 'BRAINSYNC_USER=operator' "$t/etc/brainsyncos-release" || die "the release file does not name operator"
    echo "existing account: $(grep -m1 'install.sh: account: operator' "$W/op.out" | sed 's/^install.sh: //')"

    umount "$t$bt"
    trap - EXIT
    rmdir "$t$bt"
    ! grep -q " $t/" /proc/mounts || die "mounts left under the target: $(grep " $t/" /proc/mounts)"
    force_rm_tree "$t"
    force_rm_tree "$T/bundle"
}

case "$1" in
    tar) c_tar ;;
    install) c_install ;;
    *) die "unknown check: $1 (see --help)" ;;
esac
