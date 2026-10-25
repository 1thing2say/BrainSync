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
#   app-debs  brainsync-runtime and brainsync (the jetson test packages):
#             control fields and dependencies (brainsync needs exactly this
#             brainsync-runtime), who ships what (the runtime: the venv without
#             the app; brainsync: the app in the venv, the /usr/bin links into
#             it, the /etc/brainsync conffiles, /usr/share/brainsync), no file
#             in both, nothing named *.env, no tests folders but numpy/testing,
#             a .pyc beside every module, root-owned, md5sums OK
#   md5map    merge-md5sums.py --map: a mapped tree's files are listed at their
#             ISO paths with correct sums; bad --map arguments are refused
#   hook      payload/hook.sh (00-release.sh, 60-apt-repo.sh and
#             70-brainsync.sh only) against NVIDIA's real target base layer
#             (ubuntu-server-minimal.squashfs), curtin via the chroot shim: the
#             repository is mounted read-only for the steps and unmounted
#             afterwards, brainsyncos-archive-keyring is installed (no apt error
#             or warning in the log) and verifies the repository, brainsync and
#             brainsync-runtime are installed and brainsync-voice runs in the
#             target, the target's apt sources and lists are unchanged; a re-run
#             works; a failing later step and a missing repository fail the
#             hook and leave no mount
#   debs      the board packages: brainsyncos-ch341-tegra (jetson only; files,
#             exact nvidia-l4t-kernel Depends, depmod scripts, unsigned
#             ch341.ko whose vermagic equals NVIDIA's cp210x.ko and whose
#             symbols resolve against the kernel's Module.symvers) and
#             brainsync-udev (both editions, identical; rules pass
#             `udevadm verify` and match both board vendors)
#   usbserial payload/hook.sh with 00, 60 and 75-usb-serial.sh against NVIDIA's
#             target base layer plus nvidia-l4t-kernel from the pool (no
#             maintainer scripts): both packages installed, modprobe resolves
#             ch341 to updates/ch341.ko with usbserial, a re-run works, removal
#             takes it out of modules.dep again; then the step on the vm
#             edition installs brainsync-udev only
#   llama-debs  brainsyncos-llama (both editions, identical) and
#             brainsyncos-llama-cuda (jetson only): files (the tree of
#             build/llama, the 8 CPU backends, no headers; the CUDA backend
#             only in -cuda), Depends (the C/C++ runtime; -cuda exactly
#             brainsyncos-llama and llama.env's CUDA runtime, never libcuda),
#             the unit's flags (127.0.0.1:9931, key from LoadCredential,
#             IPAddressDeny=any, sandbox, no tools or router) and the drop-in,
#             maintainer scripts that skip a chroot, root-owned, md5sums
#   llama     payload/hook.sh with 00, 60 and 85-llama.sh against NVIDIA's
#             target base layer, from the jetson test repository plus empty
#             stand-ins for NVIDIA's cuda-cudart-13-2 and libcublas-13-2 when
#             it lacks them: both packages installed, the service enabled, a
#             64-hex key root-only, the same key in secrets.env (root:brainsync
#             0640, template kept), systemd-analyze verify and security on the
#             unit; a re-run keeps key and file; other secrets.env lines kept
#             and a stale key line replaced; then a test model with its
#             model.conf: the unit's own ExecStart, expanded as systemd would,
#             serves it, and the Anthropic SDK in the BrainSync venv reaches it
#             with the key from secrets.env; last the vm edition gets
#             brainsyncos-llama only
#   wheel     build/wheels (scripts/build-wheels.sh, TESTDIR unused): the
#             manifest matches wheel.env and the file, the wheel holds only
#             brainflow/*.py, three aarch64 libraries and its dist-info, pip
#             installs it with --require-hashes from requirements.txt into a
#             fresh venv where the Cerelog X8 reports 250 Hz, and a tampered
#             copy is refused
# Work happens in /scratch (tmpfs).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"
load_config
load_product_config
load_packages_config
require_container
[ "$#" = 2 ] || die "usage: phase3-checks.sh CHECK TESTDIR (see --help)"
case "$1" in -h|--help) usage_from_header; exit 0 ;; esac

# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"
T="$BRAINSYNCOS_ROOT/${2#/work/}"
REPO="$T/repo/jetson"
[ "$1" = wheel ] || [ -f "$REPO/dists/$REPO_SUITE/InRelease" ] || die "no test repository at $REPO (run tests/test_phase3.sh)"
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

# deb_paths DEB: the paths in DEB without "./", symlinks as "path -> target".
deb_paths() {
    dpkg-deb -c "$1" | awk '{p = $6; for (i = 7; i <= NF; i++) p = p " " $i; sub(/^\.\//, "", p); if (p != "") print p}' \
        | LC_ALL=C sort
}

# no_pyc_missing DIR: every .py under DIR has its cpython-312 .pyc, and every
# .pyc is unchecked-hash (flags 1): the same bytes on every build, never
# rewritten at runtime when dpkg sets the file times.
no_pyc_missing() {
    python3 - "$1" <<'PY'
import os, struct, sys
missing = total = other = 0
for root, dirs, files in os.walk(sys.argv[1]):
    for name in files:
        path = os.path.join(root, name)
        if name.endswith(".py"):
            total += 1
            if not os.path.exists(os.path.join(root, "__pycache__", name[:-3] + ".cpython-312.pyc")):
                missing += 1
                if missing <= 5:
                    print("no .pyc:", path)
        elif name.endswith(".pyc"):
            with open(path, "rb") as f:
                flags = struct.unpack("<I", f.read(8)[4:8])[0]
            if flags != 1:
                other += 1
                if other <= 5:
                    print(f"not unchecked-hash (flags {flags}):", path)
print(f"{total} modules, {missing} without .pyc, {other} .pyc not unchecked-hash")
sys.exit(1 if missing or other else 0)
PY
}

c_app_debs() {
    local d="$T/debs/jetson" rt app f x="$W/x" bad c n site=opt/brainsyncos/venv/lib/python3.12/site-packages
    rt="$(ls "$d/brainsync-runtime_${BRAINSYNCOS_VERSION}_arm64.deb")"
    app="$(ls "$d/brainsync_${BRAINSYNCOS_VERSION}_arm64.deb")"
    mkdir -p "$x"
    for f in "$rt" "$app"; do
        echo "$(basename "$f"): $(file_size "$f") bytes, Installed-Size $(dpkg-deb -f "$f" Installed-Size) KiB"
        dpkg-deb -f "$f" Depends Recommends | sed 's/^/  /'
        [ "$(dpkg-deb -c "$f" | awk '{print $2}' | sort -u)" = root/root ] || die "$(basename "$f"): files not all root/root"
        dpkg-deb -R "$f" "$x/$(basename "$f")"
        (cd "$x/$(basename "$f")" && md5sum -c --quiet DEBIAN/md5sums) || die "$(basename "$f"): md5sums"
        no_pyc_missing "$x/$(basename "$f")/$site" || die "$(basename "$f"): modules without an unchecked-hash .pyc"
    done
    [ "$(dpkg-deb -f "$app" Depends)" = "brainsync-runtime (= $BRAINSYNCOS_VERSION)" ] \
        || die "brainsync must depend on exactly brainsync-runtime $BRAINSYNCOS_VERSION, has: $(dpkg-deb -f "$app" Depends)"
    [ "$(dpkg-deb -f "$rt" Depends)" = 'python3.12 (>= 3.12.3), python3.12 (<< 3.13), libc6, libgcc-s1, libstdc++6, zlib1g' ] \
        || die "brainsync-runtime Depends: $(dpkg-deb -f "$rt" Depends)"
    deb_paths "$rt" > "$W/rt.paths"
    deb_paths "$app" > "$W/app.paths"

    bad="$(grep -Ev '^(opt/|opt/brainsyncos/|opt/brainsyncos/venv/.*|usr/|usr/share/|usr/share/doc/|usr/share/doc/brainsync-runtime/.*)$' "$W/rt.paths" | head -3 || true)"
    [ -z "$bad" ] || die "brainsync-runtime ships files outside the venv: $bad"
    bad="$(grep -E "^$site/(brainsync|app|tools)/|^$site/brainsync-[0-9].*\.dist-info/" "$W/rt.paths" | head -3 || true)"
    [ -z "$bad" ] || die "brainsync-runtime ships app files: $bad"
    bad="$(grep -E '(^|/)tests/$' "$W/rt.paths" | head -3 || true)"
    [ -z "$bad" ] || die "brainsync-runtime ships tests folders: $bad"
    grep -qx "$site/numpy/testing/__init__.py" "$W/rt.paths" || die "numpy/testing is missing (scipy imports it)"
    ! grep -q "^$site/psychopy/demos/" "$W/rt.paths" || die "psychopy/demos is shipped"
    [ "$(grep -c . "$x/$(basename "$rt")/usr/share/doc/brainsync-runtime/wheels.txt")" -ge \
        "$(grep -cE '^[A-Za-z0-9]' "$BRAINSYNCOS_ROOT/python/brainsync.lock.txt")" ] || die "wheels.txt lists fewer wheels than the lock"

    bad="$(grep -Ev "^(etc/|etc/brainsync/|etc/brainsync/(default|intents|profile)\.yaml|usr/|usr/bin/|usr/bin/[a-z0-9-]+ -> /opt/brainsyncos/venv/bin/[a-z0-9-]+|usr/share/|usr/share/brainsync/|usr/share/brainsync/(ui|clients)/.*|usr/share/doc/|usr/share/doc/brainsync/.*|opt/|opt/brainsyncos/|opt/brainsyncos/venv/|opt/brainsyncos/venv/bin/|opt/brainsyncos/venv/bin/[a-z0-9-]+|opt/brainsyncos/venv/lib/|opt/brainsyncos/venv/lib/python3.12/|$site/|$site/(brainsync|app|tools|brainsync-[0-9.]+\.dist-info)/.*)$" "$W/app.paths" | head -3 || true)"
    [ -z "$bad" ] || die "brainsync ships unexpected paths: $bad"
    grep -v '/$' "$W/rt.paths" > "$W/rt.files"
    grep -v '/$' "$W/app.paths" > "$W/app.files"
    comm -12 "$W/rt.files" "$W/app.files" > "$W/both"
    [ ! -s "$W/both" ] || die "files in both packages: $(head -3 "$W/both")"
    ! grep -Eq '(^|/)(\.env[^/]*|[^/]*\.env)$' "$W/rt.files" "$W/app.files" || die "an .env file is packaged"
    [ "$(cat "$x/$(basename "$app")/DEBIAN/conffiles")" = "$(printf '/etc/brainsync/%s\n' default.yaml intents.yaml profile.yaml)" ] \
        || die "conffiles: $(cat "$x/$(basename "$app")/DEBIAN/conffiles")"
    for f in usr/share/brainsync/ui/index.html usr/share/brainsync/clients/web/index.html "$site/brainsync/paths.py"; do
        grep -qx "$f" "$W/app.paths" || die "brainsync lacks $f"
    done
    ! grep -Eq '^usr/share/brainsync/clients/(.*/)?(test/|[^/]*\.test\.mjs$)' "$W/app.paths" || die "the clients' tests are shipped"
    n=0
    for c in $(sed -n 's|^usr/bin/\([a-z0-9-]*\) -> .*|\1|p' "$W/app.paths"); do
        grep -qx "usr/bin/$c -> /opt/brainsyncos/venv/bin/$c" "$W/app.paths" && grep -qx "opt/brainsyncos/venv/bin/$c" "$W/app.paths" \
            || die "/usr/bin/$c is not a link to the app's script in the venv"
        n=$((n + 1))
    done
    for c in brainsync-voice brainsync-demo brainsync-server brainchess; do
        grep -qx "usr/bin/$c -> /opt/brainsyncos/venv/bin/$c" "$W/app.paths" || die "no /usr/bin/$c"
    done
    echo "brainsync-runtime: $(wc -l < "$W/rt.files" | tr -d ' ') files, all in the venv or its docs; brainsync: $(wc -l < "$W/app.files" | tr -d ' ') files, $n commands; none in both, no .env, conffiles OK"
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

# hook_payload DIR [STEP...]: a payload with only 00-release.sh,
# 60-apt-repo.sh and the STEPs (the identity steps need NVIDIA's late-command
# state; the phase 2 and VM hook checks run the full set).
hook_payload() {
    local d="$1" step
    shift
    mkdir -p "$d/install.d"
    cp "$BRAINSYNCOS_ROOT/payload/hook.sh" "$d/"
    cp -R "$BRAINSYNCOS_ROOT/payload/lib" "$d/"
    for step in 00-release.sh 60-apt-repo.sh "$@"; do
        cp "$BRAINSYNCOS_ROOT/payload/install.d/$step" "$d/install.d/"
    done
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
    hook_payload "$p" 70-brainsync.sh
    apt_state "$t" > "$W/apt-before"

    run_hook "$p" "$t" "$REPO" || { tail -20 "$W/live.log"; die "hook failed"; }
    grep -q 'package repository .* mounted at /tmp/brainsyncos-repo (read-only)' "$log" || die "hook did not mount the repository"
    grep -q '60-apt-repo.sh: repository verified; brainsyncos-archive-keyring ' "$log" || die "60-apt-repo.sh did not finish"
    grep -q '70-brainsync.sh: brainsync .*, brainsync-runtime .* installed; [0-9]* commands in /usr/bin' "$log" \
        || die "70-brainsync.sh did not finish"
    grep -q 'BrainSyncOS layer installed (3 steps)' "$log" || die "log lacks the summary"
    ! grep -n '^E: \|^W: \|WARNING' "$log" || die "the install logged an apt error or warning"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- dpkg -s brainsyncos-archive-keyring | grep -qx 'Status: install ok installed' \
        || die "brainsyncos-archive-keyring is not installed in the target"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- dpkg-query -W -f '${Status}\n' brainsync brainsync-runtime \
        | grep -cx 'install ok installed' | grep -qx 2 || die "brainsync and brainsync-runtime are not installed in the target"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- /usr/bin/brainsync-voice --help | grep -q 'usage:' \
        || die "brainsync-voice does not run in the target"
    gpgv --keyring "$t/usr/share/keyrings/brainsyncos-archive-keyring.gpg" "$REPO/dists/$REPO_SUITE/InRelease" > /dev/null 2>&1 \
        || die "the installed keyring does not verify the repository"
    apt_state "$t" > "$W/apt-after"
    diff "$W/apt-before" "$W/apt-after" || die "the target's apt sources or lists changed"
    ! ls -d "$t"/tmp/bsos-apt.* > /dev/null 2>&1 || die "private apt state left in the target"
    no_mount_left "$t"
    grep -q '60-apt-repo.sh' "$log" && sed -n '/== 60-apt-repo.sh/,$p' "$log" | grep -v '^Unpacking\|^Selecting\|^Preparing\|^Setting up' | head -24
    echo "hook: repository mounted read-only, keyring and BrainSync installed, target apt state unchanged, unmounted"

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

# target_layer DIR: NVIDIA's target base layer, unpacked at DIR, with a fake Orin Nano device tree at $W/dt.
target_layer() {
    xorriso -osirrox on -indev "$(resolve_base_iso)" -extract /casper/ubuntu-server-minimal.squashfs "$W/t.squashfs" \
        > /dev/null 2>&1 || die "cannot read the target base layer from the base ISO"
    unsquashfs -q -no-progress -d "$1" "$W/t.squashfs" > /dev/null
    rm_released "$W/t.squashfs"
    mkdir -p "$W/dt"
    printf 'NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super\0' > "$W/dt/model"
    printf '1421224ABC123456\0' > "$W/dt/serial-number"
}

# deb_files DEB: the regular files in DEB, one path per line, sorted.
deb_files() {
    dpkg-deb -c "$1" | awk '$1 ~ /^-/ {print $6}' | LC_ALL=C sort
}

c_debs() {
    local j="$T/debs/jetson" v="$T/debs/vm" ch u kver x k want got ko hdr symvers id f r
    kver="$(sed -n 's/^KERNEL_RELEASE=//p' "$BRAINSYNCOS_ROOT/packages/brainsyncos-ch341-tegra/package.env")"
    ch="$(ls "$j"/brainsyncos-ch341-tegra_"$BRAINSYNCOS_VERSION"_arm64.deb)"
    ! ls "$v"/brainsyncos-ch341-tegra_* > /dev/null 2>&1 || die "the vm edition got brainsyncos-ch341-tegra"
    dpkg-deb -f "$ch" Package Version Architecture Depends Section
    [ "$(dpkg-deb -f "$ch" Depends)" = "nvidia-l4t-kernel (= $TEGRA_KERNEL), kmod" ] || die "ch341 Depends: $(dpkg-deb -f "$ch" Depends)"
    printf '%s\n' "./lib/modules/$kver/updates/ch341.ko" ./usr/share/doc/brainsyncos-ch341-tegra/Kbuild \
        ./usr/share/doc/brainsyncos-ch341-tegra/README ./usr/share/doc/brainsyncos-ch341-tegra/ch341.c \
        ./usr/share/doc/brainsyncos-ch341-tegra/copyright > "$W/want"
    deb_files "$ch" | diff "$W/want" - || die "brainsyncos-ch341-tegra has other files"
    ! dpkg-deb -c "$ch" | awk '{print $2}' | grep -vqx root/root || die "brainsyncos-ch341-tegra: files not all root/root"
    x="$W/ch"
    dpkg-deb -R "$ch" "$x"
    (cd "$x" && md5sum -c --quiet DEBIAN/md5sums) || die "ch341 md5sums"
    cmp "$x/usr/share/doc/brainsyncos-ch341-tegra/ch341.c" "$BRAINSYNCOS_ROOT/packages/brainsyncos-ch341-tegra/ch341.c" || die "shipped source differs"
    for f in postinst postrm; do
        [ "$(stat -c %a "$x/DEBIAN/$f")" = 755 ] || die "$f is not mode 755"
        sh -n "$x/DEBIAN/$f" || die "$f: shell syntax"
        grep -q "depmod -a $kver" "$x/DEBIAN/$f" || die "$f does not run depmod -a $kver"
    done
    echo "ch341 package: files, root-owned, md5sums, Depends nvidia-l4t-kernel (= $TEGRA_KERNEL), depmod in postinst and postrm"

    # The module against NVIDIA's own kernel package and headers from the pool.
    k="$W/kernel"
    xorriso -osirrox on -indev "$(resolve_base_iso)" -extract "/pool/main/n/nvidia-l4t-kernel/nvidia-l4t-kernel_${TEGRA_KERNEL}_arm64.deb" \
        "$W/k.deb" > /dev/null 2>&1 || die "cannot read nvidia-l4t-kernel from the base ISO"
    dpkg-deb -x "$W/k.deb" "$k"
    rm_released "$W/k.deb"
    ko="$x/lib/modules/$kver/updates/ch341.ko"
    want="$(modinfo -F vermagic "$k/lib/modules/$kver/kernel/drivers/usb/serial/cp210x.ko")"
    got="$(modinfo -F vermagic "$ko")"
    echo "vermagic: ch341.ko '$got', NVIDIA's cp210x.ko '$want'"
    [ "$got" = "$want" ] || die "vermagic differs from the L4T kernel's"
    case "$got" in "$kver "*) ;; *) die "vermagic is not for $kver" ;; esac
    [ "$(modinfo -F depends "$ko")" = usbserial ] && [ -f "$k/lib/modules/$kver/kernel/drivers/usb/serial/usbserial.ko" ] \
        || die "ch341.ko needs '$(modinfo -F depends "$ko")'; the kernel must have usbserial.ko"
    [ -z "$(modinfo -F signer "$ko")" ] || die "ch341.ko is signed; the notes say it is not"
    ! grep -q '/scratch' "$ko" || die "ch341.ko holds a build path"
    for id in 1A86p7523 1A86p5523 1A86p7522; do
        modinfo -F alias "$ko" | grep -q "^usb:v${id}d" || die "ch341.ko lacks alias $id"
    done
    # Every symbol it imports is exported by that kernel or usbserial with the
    # same CRC (modversions), and NVIDIA's own modules import those symbols
    # with the same CRCs, so the headers match the kernel that ships.
    hdr="$W/hdr"
    xorriso -osirrox on -indev "$(resolve_base_iso)" -extract \
        "/pool/main/n/nvidia-l4t-kernel-headers/nvidia-l4t-kernel-headers_${TEGRA_KERNEL}_arm64.deb" "$W/h.deb" > /dev/null 2>&1 \
        || die "cannot read nvidia-l4t-kernel-headers from the base ISO"
    dpkg-deb -x "$W/h.deb" "$hdr"
    rm_released "$W/h.deb"
    symvers="$(find "$hdr/usr/src" -name Module.symvers -path '*linux-noble*' | head -n 1)"
    [ -n "$symvers" ] || die "no Module.symvers in the headers"
    python3 -I - "$ko" "$k/lib/modules/$kver/kernel" "$symvers" <<'PY'
import os, struct, sys
ko, kdir, symvers = sys.argv[1:]

def versions(path):
    """The __versions section of an ELF64 little-endian module: {symbol: CRC}.
    Ubuntu's records are variable-length: u32 record length, u32 CRC, NUL-padded name."""
    d = open(path, "rb").read()
    shoff, = struct.unpack_from("<Q", d, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 0x3A)
    sh = [struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shentsize) for i in range(shnum)]
    stroff = sh[shstrndx][4]
    out = {}
    for name, _, _, _, off, size, *_ in sh:
        if d[stroff + name:].split(b"\0")[0] != b"__versions":
            continue
        i = off
        while i + 8 <= off + size:
            n, crc = struct.unpack_from("<II", d, i)
            if n == 0:
                break  # padding
            if n < 9:
                sys.exit("bad __versions record in %s" % path)
            out[d[i + 8:i + n].split(b"\0")[0].decode()] = crc
            i += n
    return out

exp = {}
for line in open(symvers):
    f = line.rstrip("\n").split("\t")
    exp[f[1]] = int(f[0], 16)
mine = versions(ko)
if not mine:
    sys.exit("ch341.ko has no modversions")
theirs, nmods = {}, 0
for top, _, names in os.walk(kdir):
    for n in names:
        if n.endswith(".ko"):
            nmods += 1
            for sym, crc in versions(os.path.join(top, n)).items():
                theirs.setdefault(sym, set()).add(crc)
bad = sorted(n for n, c in mine.items() if exp.get(n) != c)
if bad:
    sys.exit("ch341.ko imports with no matching export in Module.symvers: %s" % bad)
shared = [n for n in mine if n in theirs]
clash = sorted(n for n in shared if theirs[n] != {mine[n]})
if clash:
    sys.exit("NVIDIA's modules import these with other CRCs: %s" % clash)
if len(shared) < len(mine) // 2:
    sys.exit("too few symbols shared with NVIDIA's modules to compare (%d of %d)" % (len(shared), len(mine)))
print("modversions: all %d imports of ch341.ko match Module.symvers; %d of them are imported by NVIDIA's %d modules with the same CRCs"
      % (len(mine), len(shared), nmods))
PY
    mkdir -p "$W/sym/lib/modules/$kver/updates"
    cp -R "$k/lib/modules/$kver/." "$W/sym/lib/modules/$kver/"
    cp "$ko" "$W/sym/lib/modules/$kver/updates/"
    depmod -b "$W/sym" "$kver"
    grep -qx "updates/ch341.ko: kernel/drivers/usb/serial/usbserial.ko" "$W/sym/lib/modules/$kver/modules.dep" \
        || die "modules.dep does not resolve ch341 to usbserial"
    echo "ch341.ko: unsigned, aliases for 1a86:7523/5523/7522; depmod with NVIDIA's modules: updates/ch341.ko needs usbserial.ko"

    u="$(ls "$j"/brainsync-udev_"$BRAINSYNCOS_VERSION"_all.deb)"
    cmp "$u" "$(ls "$v"/brainsync-udev_"$BRAINSYNCOS_VERSION"_all.deb)" || die "brainsync-udev differs between editions"
    [ -z "$(dpkg-deb -f "$u" Depends)" ] || die "brainsync-udev has Depends"
    printf '%s\n' ./usr/lib/udev/rules.d/70-brainsync-cerelog.rules ./usr/share/doc/brainsync-udev/README > "$W/want"
    deb_files "$u" | diff "$W/want" - || die "brainsync-udev has other files"
    x="$W/udev"
    dpkg-deb -R "$u" "$x"
    sh -n "$x/DEBIAN/postinst" || die "udev postinst: shell syntax"
    grep -q 'systemd-detect-virt -q --chroot' "$x/DEBIAN/postinst" || die "udev postinst does not skip chroots"
    r="$x/usr/lib/udev/rules.d/70-brainsync-cerelog.rules"
    udevadm verify --resolve-names=never "$r"
    for vid in 1a86 303a; do
        grep -qx "ATTRS{idVendor}==\"$vid\", GROUP=\"dialout\", MODE=\"0660\", SYMLINK+=\"cerelog\", ENV{ID_MM_DEVICE_IGNORE}=\"1\"" "$r" \
            || die "no rule for vendor $vid with dialout, 0660, cerelog and ID_MM_DEVICE_IGNORE"
    done
    grep -qx 'SUBSYSTEM!="tty", GOTO="brainsync_cerelog_end"' "$r" || die "the rules are not limited to tty devices"
    echo "brainsync-udev: same .deb for both editions, rules pass udevadm verify, vendors 1a86 and 303a: dialout 0660 /dev/cerelog, ModemManager ignores"
}

c_usbserial() {
    local t="$W/target" p="$W/hp" log="$W/target/var/log/brainsyncos/install.log" kver dep
    kver="$(sed -n 's/^KERNEL_RELEASE=//p' "$BRAINSYNCOS_ROOT/packages/brainsyncos-ch341-tegra/package.env")"
    target_layer "$t"
    l4t_kernel_stub "$(resolve_base_iso)" "$t" "$TEGRA_KERNEL"
    hook_payload "$p" 75-usb-serial.sh
    run_hook "$p" "$t" "$REPO" || { tail -30 "$W/live.log"; die "hook failed"; }
    grep -q 'BrainSyncOS layer installed (3 steps)' "$log" || die "log lacks the summary"
    ! grep -n '^E: \|^W: \|WARNING' "$log" || die "the install logged an apt error or warning"
    sed -n '/== 75-usb-serial.sh/,$p' "$log" | grep '75-usb-serial.sh'
    grep -q "75-usb-serial.sh: ch341 for $kver: /lib/modules/$kver/kernel/drivers/usb/serial/usbserial.ko /lib/modules/$kver/updates/ch341.ko" "$log" \
        || die "75-usb-serial.sh did not resolve ch341 with usbserial"
    dep="$t/lib/modules/$kver/modules.dep"
    grep -qx "updates/ch341.ko: kernel/drivers/usb/serial/usbserial.ko" "$dep" || die "postinst did not depmod ch341 in"
    grep -q '^alias usb:v1A86p7523d\*.* ch341$' "$t/lib/modules/$kver/modules.alias" || die "modules.alias lacks the CH340 alias"
    [ -s "$t/usr/lib/udev/rules.d/70-brainsync-cerelog.rules" ] || die "the udev rules are not installed"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- dpkg-query -W -f '${Package} ${Status}\n' \
        brainsync-udev brainsyncos-ch341-tegra | grep -c ' install ok installed$' | grep -qx 2 || die "packages not installed"
    no_mount_left "$t"
    echo "jetson: both packages installed, modules.dep and modules.alias have ch341, rules in /usr/lib/udev/rules.d"
    run_hook "$p" "$t" "$REPO" > /dev/null 2>&1 || { tail -20 "$W/live.log"; die "re-run failed"; }
    echo "re-run: OK"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- env DEBIAN_FRONTEND=noninteractive \
        dpkg --purge brainsyncos-ch341-tegra brainsync-udev > /dev/null
    ! grep -q ch341 "$dep" || die "postrm left ch341 in modules.dep"
    [ ! -e "$t/lib/modules/$kver/updates/ch341.ko" ] && [ ! -e "$t/usr/lib/udev/rules.d/70-brainsync-cerelog.rules" ] \
        || die "purge left files"
    echo "purge: ch341 out of modules.dep (postrm depmod), files gone"

    # The vm edition, straight through the step: the udev rules only.
    sed -i 's/^BSOS_EDITION=.*/BSOS_EDITION=vm-arm64/' "$t/opt/brainsyncos/installer/device.env"
    cp -a "$T/repo/vm" "$t/tmp/vm-repo"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- env BSOS_INSTALL_DIR=/opt/brainsyncos/installer \
        BSOS_REPO_DIR=/tmp/vm-repo /bin/bash /opt/brainsyncos/installer/install.d/75-usb-serial.sh > "$W/vm.out" 2>&1 \
        || { cat "$W/vm.out"; die "75-usb-serial.sh failed on the vm edition"; }
    force_rm_tree "$t/tmp/vm-repo"
    grep '75-usb-serial.sh' "$W/vm.out"
    grep -q 'vm-arm64 edition: no brainsyncos-ch341-tegra' "$W/vm.out" || die "vm: no skip message"
    [ -s "$t/usr/lib/udev/rules.d/70-brainsync-cerelog.rules" ] && [ ! -e "$t/lib/modules/$kver/updates/ch341.ko" ] \
        || die "vm: wrong packages"
    echo "vm edition: brainsync-udev only"
}

# unit_exec UNIT: the unit's ExecStart= command, its continuation lines joined.
unit_exec() {
    awk '/^ExecStart=/ {on = 1; sub(/^ExecStart=/, "")} on {l = $0; c = sub(/\\$/, "", l); printf "%s ", l; if (!c) exit}' "$1" \
        | tr -s ' ' | sed 's/ $//'
}

c_llama_debs() {
    local j="$T/debs/jetson" v="$T/debs/vm" base cuda x="$W/ld" in="$BUILD_DIR/llama" f want cmd u m d
    load_env_file "$BRAINSYNCOS_ROOT/llama/llama.env" "LLAMA_PREFIX CPU_VARIANTS CUDA_RUNTIME"
    base="$(ls "$j/brainsyncos-llama_${BRAINSYNCOS_VERSION}_arm64.deb")"
    cuda="$(ls "$j/brainsyncos-llama-cuda_${BRAINSYNCOS_VERSION}_arm64.deb")"
    cmp "$base" "$v/$(basename "$base")" || die "brainsyncos-llama differs between editions"
    ! ls "$v"/brainsyncos-llama-cuda_* > /dev/null 2>&1 || die "the vm edition got brainsyncos-llama-cuda"
    for f in "$base" "$cuda"; do
        dpkg-deb -f "$f" Package Version Installed-Size Depends | sed 's/^/  /'
        [ "$(dpkg-deb -c "$f" | awk '{print $2}' | sort -u)" = root/root ] || die "$(basename "$f"): files not all root/root"
        dpkg-deb -R "$f" "$x/$(dpkg-deb -f "$f" Package)"
        (cd "$x/$(dpkg-deb -f "$f" Package)" && md5sum -c --quiet DEBIAN/md5sums) || die "$(basename "$f"): md5sums"
        for m in "$x/$(dpkg-deb -f "$f" Package)"/DEBIAN/p*; do
            sh -n "$m" || die "$m: shell syntax"
            grep -q 'systemd-detect-virt -q --chroot' "$m" || die "$(basename "$m") of $(basename "$f") does not skip a chroot"
        done
    done
    printf '%s\n' "$(dpkg-deb -f "$base" Depends)" | grep -Eqx 'libc6 \(>= 2\.[0-9]+\), libgcc-s1, libstdc\+\+6 \(>= 14\)' \
        || die "brainsyncos-llama Depends: $(dpkg-deb -f "$base" Depends)"
    want="brainsyncos-llama (= $BRAINSYNCOS_VERSION)"
    for d in $(printf '%s\n' "$CUDA_RUNTIME" | tr ',' ' '); do want="$want, ${d%%=*} (= ${d#*=})"; done
    [ "$(dpkg-deb -f "$cuda" Depends)" = "$want" ] || die "brainsyncos-llama-cuda Depends: $(dpkg-deb -f "$cuda" Depends), want $want"

    # Files: the checked tree, nothing else of llama.cpp.
    (cd "$in/cpu" && find . -type f | sed "s|^\.|.$LLAMA_PREFIX|" | LC_ALL=C sort) > "$W/want"
    deb_files "$base" | grep "^\.$LLAMA_PREFIX/" | diff "$W/want" - || die "brainsyncos-llama's tree differs from build/llama/cpu"
    printf '%s\n' ./usr/lib/systemd/system/brainsyncos-llama.service ./usr/share/doc/brainsyncos-llama/README \
        ./usr/share/doc/brainsyncos-llama/build.env ./usr/share/doc/brainsyncos-llama/copyright > "$W/want-other"
    deb_files "$base" | grep -v "^\.$LLAMA_PREFIX/" | diff "$W/want-other" - || die "brainsyncos-llama has other files"
    dpkg-deb -c "$base" | grep -q " \./etc/brainsyncos/llama\.d/$" || die "no /etc/brainsyncos/llama.d/ for model.conf"
    for d in $(printf '%s\n' "$CPU_VARIANTS" | tr ',' ' '); do
        grep -qx "\.$LLAMA_PREFIX/lib/ggml/libggml-cpu-$d\.so" "$W/want" || die "no CPU backend $d"
    done
    ! grep -Eq '/include/|/cmake/|/pkgconfig/|libggml-cuda' "$W/want" || die "brainsyncos-llama ships headers, CMake files or the CUDA backend"
    printf '%s\n' ".$LLAMA_PREFIX/lib/ggml/libggml-cuda.so" ./usr/lib/systemd/system/brainsyncos-llama.service.d/50-cuda.conf \
        ./usr/share/doc/brainsyncos-llama-cuda/README ./usr/share/doc/brainsyncos-llama-cuda/copyright > "$W/want-cuda"
    deb_files "$cuda" | diff "$W/want-cuda" - || die "brainsyncos-llama-cuda has other files"
    cmp "$x/brainsyncos-llama-cuda$LLAMA_PREFIX/lib/ggml/libggml-cuda.so" "$in/cuda/libggml-cuda.so" || die "libggml-cuda.so is not the checked one"
    ! objdump -p "$in/cuda/libggml-cuda.so" | awk '$1 == "NEEDED" {print $2}' | grep -q '^libcuda\.' || die "libggml-cuda.so needs libcuda"
    echo "files: the checked tree ($(wc -l < "$W/want" | tr -d ' ') files, $(printf '%s\n' "$CPU_VARIANTS" | tr ',' '\n' | wc -l | tr -d ' ') CPU backends), the CUDA backend only in -cuda; Depends as built"

    # The unit and the drop-in.
    u="$x/brainsyncos-llama/usr/lib/systemd/system/brainsyncos-llama.service"
    cmd="$(unit_exec "$u")"
    echo "ExecStart: $cmd"
    case "$cmd" in "/usr/bin/env -i $LLAMA_PREFIX/bin/llama-server --host 127.0.0.1 --port 9931 --api-key-file %d/api-key "*) ;;
        *) die "ExecStart does not start llama-server on 127.0.0.1:9931 with the key file in an empty environment" ;; esac
    for f in --offline --no-webui --no-slots '--parallel 1' '--cache-ram 512'; do
        printf ' %s ' "$cmd" | grep -q -- " $f " || die "ExecStart lacks $f"
    done
    ! printf '%s\n' "$cmd" | grep -Eq -- '--tools|--mcp|--models-|--media-path|--props|--metrics|--webui-mcp|0\.0\.0\.0|--api-key ' \
        || die "ExecStart turns on something it must not"
    for f in ConditionPathExists=/etc/brainsyncos/llama.d/model.conf 'ConditionPathExistsGlob=/opt/brainsyncos/models/*.gguf' \
        Type=exec EnvironmentFile=/etc/brainsyncos/llama.d/model.conf LoadCredential=api-key:/etc/brainsyncos/llama/api-key \
        IPAddressDeny=any IPAddressAllow=localhost 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' DynamicUser=yes \
        ProtectSystem=strict ProtectHome=yes PrivateTmp=yes PrivateDevices=yes NoNewPrivileges=yes MemoryDenyWriteExecute=yes \
        CapabilityBoundingSet= Nice=10 CPUWeight=50 WantedBy=multi-user.target; do
        grep -qxF "$f" "$u" || die "the unit lacks $f"
    done
    f="$x/brainsyncos-llama-cuda/usr/lib/systemd/system/brainsyncos-llama.service.d/50-cuda.conf"
    for want in '[Service]' PrivateDevices=no 'SupplementaryGroups=video render' MemoryDenyWriteExecute=no; do
        grep -qxF "$want" "$f" || die "50-cuda.conf lacks $want"
    done
    [ "$(grep -cv '^#\|^$' "$f")" = 4 ] || die "50-cuda.conf changes more than the devices and W^X"
    echo "unit: 127.0.0.1:9931, key by LoadCredential, empty environment, IPAddressDeny=any, sandboxed, starts only with a model; -cuda drop-in: GPU devices, no MemoryDenyWriteExecute"
}

# cuda_stand_ins DIR: empty arm64 packages named and versioned like NVIDIA's
# CUDA runtime (llama.env CUDA_RUNTIME), for a repository that does not
# mirror the real ones. They test the install's dependency resolution only.
cuda_stand_ins() {
    local d n v s
    for d in $(printf '%s\n' "$CUDA_RUNTIME" | tr ',' ' '); do
        n="${d%%=*}"
        v="${d#*=}"
        s="$W/stand-in-$n"
        mkdir -p "$s/DEBIAN" "$s/usr/share/doc/$n"
        echo "Test stand-in for NVIDIA's $n $v (tests/phase3-checks.sh). Not a CUDA runtime." > "$s/usr/share/doc/$n/README"
        printf 'Package: %s\nVersion: %s\nArchitecture: arm64\nMaintainer: BrainSyncOS tests <test@localhost>\nDescription: test stand-in for %s\n' \
            "$n" "$v" "$n" > "$s/DEBIAN/control"
        build_deb "$s" "$1" > /dev/null
    done
}

# in_t TARGET CMD...: CMD inside TARGET (the curtin shim).
in_t() {
    local t="$1"
    shift
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- "$@"
}

# expand_exec UNIT CONF: the unit's ExecStart= as systemd would run it, with
# its Environment= defaults, then CONF's values, and %d (the credentials).
expand_exec() {
    python3 -I - "$1" "$2" <<'PY'
import re, shlex, sys
unit, conf = sys.argv[1:]
text = open(unit).read().replace("\\\n", " ")
env, cmd = {}, None
for line in text.splitlines():
    if line.startswith("Environment="):
        env.update(kv.split("=", 1) for kv in shlex.split(line[len("Environment="):]))
    if line.startswith("ExecStart="):
        cmd = line[len("ExecStart="):]
for line in open(conf):
    line = line.strip()
    if line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k] = v
cmd = cmd.replace("%d", "/run/credentials/brainsyncos-llama.service")
cmd = re.sub(r"\$\{(\w+)\}", lambda m: shlex.quote(env[m.group(1)]), cmd)
print(" ".join(cmd.split()))
PY
}

c_llama() {
    local t="$W/target" p="$W/hp" log="$W/target/var/log/brainsyncos/install.log" repo="$REPO" key sec k1 s1 cmd ok pid
    load_env_file "$BRAINSYNCOS_ROOT/llama/llama.env" "LLAMA_PREFIX CUDA_RUNTIME TEST_MODEL_FILE TEST_MODEL_SHA256"
    target_layer "$t"
    if ! ls "$REPO/pool/$REPO_COMPONENT"/cuda-cudart-13-2_*.deb > /dev/null 2>&1; then
        mkdir -p "$W/debs"
        cp "$T/debs/jetson"/*.deb "$W/debs/"
        cuda_stand_ins "$W/debs"
        "$BRAINSYNCOS_ROOT/scripts/build-repo.sh" --edition jetson --debs "$W/debs" --key "$T/key/repo-signing-key.asc" \
            --out "$W/repo" > "$W/repo.log" 2>&1 || { cat "$W/repo.log"; die "cannot sign the test repository"; }
        cmp "$W/repo/repo-key.gpg" "$REPO/repo-key.gpg" || die "the test repository has another key"
        repo="$W/repo"
        echo "repository: the jetson test packages plus stand-ins for $(printf '%s\n' "$CUDA_RUNTIME" | tr ',' ' ') (not mirrored on this branch)"
    fi
    in_t "$t" groupadd "$BRAINSYNC_USER"   # 30-user.sh's group
    hook_payload "$p" 70-brainsync.sh 85-llama.sh
    run_hook "$p" "$t" "$repo" || { tail -30 "$W/live.log"; die "hook failed"; }
    grep -q 'BrainSyncOS layer installed (4 steps)' "$log" || die "log lacks the summary"
    ! grep -n '^E: \|^W: \|WARNING' "$log" || die "the install logged an apt error or warning"
    sed -n '/== 85-llama.sh/,$p' "$log" | grep '85-llama.sh'
    grep -q "85-llama.sh: installed brainsyncos-llama $BRAINSYNCOS_VERSION, brainsyncos-llama-cuda $BRAINSYNCOS_VERSION; llama-server version: " "$log" \
        || die "85-llama.sh did not install both packages"
    grep -q '85-llama.sh: made a new API key' "$log" && grep -q '85-llama.sh: created /etc/brainsync/secrets.env' "$log" \
        || die "85-llama.sh did not make the key and secrets.env"
    key="$t/etc/brainsyncos/llama/api-key"
    sec="$t/etc/brainsync/secrets.env"
    [ "$(stat -c '%a %U:%G' "$key")" = "600 root:root" ] && [ "$(stat -c '%a %U:%G' "$(dirname "$key")")" = "700 root:root" ] \
        || die "the key is $(stat -c '%a %U:%G' "$key") in a $(stat -c '%a %U:%G' "$(dirname "$key")") directory"
    grep -Eqx '[0-9a-f]{64}' "$key" && [ "$(wc -l < "$key")" = 1 ] || die "the key is not one line of 64 hex digits"
    [ "$(stat -c '%a %U' "$sec"):$(in_t "$t" stat -c %G /etc/brainsync/secrets.env)" = "640 root:$BRAINSYNC_USER" ] \
        || die "secrets.env is $(stat -c '%a %U:%G' "$sec")"
    [ "$(grep -c '^BRAINSYNC_LLAMA_API_KEY=' "$sec")" = 1 ] && grep -qx "BRAINSYNC_LLAMA_API_KEY=$(cat "$key")" "$sec" \
        || die "secrets.env does not hold the key once"
    grep -qx '# ANTHROPIC_API_KEY=' "$sec" || die "secrets.env lacks its template"
    [ -L "$t/etc/systemd/system/multi-user.target.wants/brainsyncos-llama.service" ] || die "the service is not enabled"
    # shellcheck disable=SC2046  # one package name per word
    in_t "$t" dpkg-query -W -f '${Package} ${Status}\n' brainsyncos-llama brainsyncos-llama-cuda \
        $(printf '%s\n' "$CUDA_RUNTIME" | tr ',' '\n' | sed 's/=.*//') | grep -c ' install ok installed$' | grep -qx 4 \
        || die "packages not installed"
    [ -f "$t$LLAMA_PREFIX/lib/ggml/libggml-cuda.so" ] || die "no CUDA backend in the target"
    no_mount_left "$t"
    echo "jetson: both packages and the CUDA runtime installed, service enabled, key 600 root, secrets.env 640 root:$BRAINSYNC_USER with the key"

    # systemd's own view of the unit, in the target.
    in_t "$t" systemd-analyze verify brainsyncos-llama.service > "$W/verify.out" 2>&1 \
        || { cat "$W/verify.out"; die "systemd-analyze verify rejects the unit"; }
    ! grep -i 'brainsyncos-llama' "$W/verify.out" || die "systemd-analyze verify warns about the unit"
    in_t "$t" systemd-analyze security --offline=true brainsyncos-llama.service > "$W/security.out" 2>&1 \
        || { cat "$W/security.out"; die "systemd-analyze security failed"; }
    grep 'Overall exposure level' "$W/security.out" || { tail -5 "$W/security.out"; die "no exposure level"; }
    grep -Eq 'Overall exposure level for brainsyncos-llama.service: [0-1]\.[0-9] (OK|SAFE)' "$W/security.out" \
        || { grep '✗' "$W/security.out" | head -20; die "exposure level above 1.9"; }
    echo "systemd-analyze: verify clean"

    # A re-run keeps the key and secrets.env as they are.
    k1="$(sha256sum < "$key")"
    s1="$(sha256sum < "$sec")"
    run_hook "$p" "$t" "$repo" > /dev/null 2>&1 || { tail -20 "$W/live.log"; die "re-run failed"; }
    [ "$(sha256sum < "$key")" = "$k1" ] && [ "$(sha256sum < "$sec")" = "$s1" ] || die "a re-run changed the key or secrets.env"
    grep -q '85-llama.sh: keeping the API key' "$log" && grep -q 'secrets.env already has the key' "$log" || die "re-run: no keep messages"
    echo "re-run: key and secrets.env unchanged"
    # Other lines stay; a stale key line is replaced where it is, once; no newline at the end is fine.
    printf 'CARTESIA_API_KEY=keep-me\nBRAINSYNC_LLAMA_API_KEY=stale\n# note\nBRAINSYNC_LLAMA_API_KEY=stale2\nANTHROPIC_API_KEY=keep-too' > "$sec"
    cp -a "$repo" "$t/tmp/llama-repo"
    in_t "$t" env BSOS_INSTALL_DIR=/opt/brainsyncos/installer BSOS_REPO_DIR=/tmp/llama-repo \
        /bin/bash /opt/brainsyncos/installer/install.d/85-llama.sh > "$W/step.out" 2>&1 \
        || { cat "$W/step.out"; die "85-llama.sh failed on an edited secrets.env"; }
    force_rm_tree "$t/tmp/llama-repo"
    printf 'CARTESIA_API_KEY=keep-me\nBRAINSYNC_LLAMA_API_KEY=%s\n# note\nANTHROPIC_API_KEY=keep-too\n' "$(cat "$key")" | diff - "$sec" \
        || die "secrets.env was not merged as expected"
    [ "$(stat -c '%a %U' "$sec"):$(in_t "$t" stat -c %G /etc/brainsync/secrets.env)" = "640 root:$BRAINSYNC_USER" ] \
        || die "the merge changed secrets.env's mode or owner"
    [ "$(sha256sum < "$key")" = "$k1" ] || die "the key changed"
    echo "edited secrets.env: other lines kept, the stale key line replaced once, mode and owner kept"

    # A model package's files: the unit's own command line serves the model.
    mkdir -p "$t/opt/brainsyncos/models" "$t/etc/brainsyncos/llama.d" "$t/run/credentials/brainsyncos-llama.service"
    cp "$DOWNLOADS_DIR/llama-test/$TEST_MODEL_FILE" "$t/opt/brainsyncos/models/test-model.gguf"
    [ "$(sha256_of "$t/opt/brainsyncos/models/test-model.gguf")" = "$TEST_MODEL_SHA256" ] || die "test model: wrong sha256"
    printf 'BSOS_LLAMA_MODEL=/opt/brainsyncos/models/test-model.gguf\nBSOS_LLAMA_ALIAS=test-model\nBSOS_LLAMA_CTX_SIZE=2048\n' \
        > "$t/etc/brainsyncos/llama.d/model.conf"
    cp "$key" "$t/run/credentials/brainsyncos-llama.service/api-key"
    cmd="$(expand_exec "$t/usr/lib/systemd/system/brainsyncos-llama.service" "$t/etc/brainsyncos/llama.d/model.conf")"
    echo "as systemd would run it: $cmd"
    # shellcheck disable=SC2086  # the command line, split as systemd splits it
    in_t "$t" $cmd > "$W/service.log" 2>&1 &
    pid=$!
    ok=0
    for _ in $(seq 1 240); do
        [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9931/health)" = 200 ] && { ok=1; break; }
        kill -0 "$pid" 2> /dev/null || break
        sleep 0.5
    done
    [ "$ok" = 1 ] || { kill "$pid" 2> /dev/null || true; tail -30 "$W/service.log"; die "the service command did not come up"; }
    [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9931/v1/models)" = 401 ] \
        || { kill "$pid"; die "/v1/models answers without the key"; }
    in_t "$t" /opt/brainsyncos/venv/bin/python -I -c '
import os, anthropic
from app.config import load_env
load_env("/etc/brainsync/secrets.env")
c = anthropic.Anthropic(api_key=os.environ["BRAINSYNC_LLAMA_API_KEY"], base_url="http://127.0.0.1:9931", max_retries=0, timeout=120)
m = c.messages.create(model="test-model", max_tokens=8, messages=[{"role": "user", "content": "Say hello."}],
                      extra_body={"chat_template_kwargs": {"enable_thinking": False}})
assert m.content and m.content[0].text, m
print("BCI Voice path: the SDK with the key from secrets.env got", m.usage.output_tokens, "tokens from", m.model)
try:
    anthropic.Anthropic(api_key="wrong", base_url="http://127.0.0.1:9931", max_retries=0).messages.create(
        model="x", max_tokens=1, messages=[{"role": "user", "content": "x"}])
    raise SystemExit("a wrong key was accepted")
except anthropic.AuthenticationError:
    print("a wrong key: 401")
' || { kill "$pid" 2> /dev/null || true; tail -20 "$W/service.log"; die "the BrainSync venv cannot use the local server"; }
    kill "$pid" 2> /dev/null || true
    wait "$pid" 2> /dev/null || true
    grep -o 'loaded CPU backend from [^ ]*' "$W/service.log" | head -n 1 | grep . || die "no CPU backend in the service log"
    force_rm_tree "$t/opt/brainsyncos/models"
    force_rm_tree "$t/run/credentials"
    rm -f "$t/etc/brainsyncos/llama.d/model.conf"

    # The vm edition: brainsyncos-llama only.
    sed -i 's/^BSOS_EDITION=.*/BSOS_EDITION=vm-arm64/' "$t/opt/brainsyncos/installer/device.env"
    in_t "$t" env DEBIAN_FRONTEND=noninteractive dpkg --purge brainsyncos-llama-cuda > /dev/null
    cp -a "$T/repo/vm" "$t/tmp/vm-repo"
    in_t "$t" env BSOS_INSTALL_DIR=/opt/brainsyncos/installer BSOS_REPO_DIR=/tmp/vm-repo \
        /bin/bash /opt/brainsyncos/installer/install.d/85-llama.sh > "$W/vm.out" 2>&1 \
        || { cat "$W/vm.out"; die "85-llama.sh failed on the vm edition"; }
    force_rm_tree "$t/tmp/vm-repo"
    grep '85-llama.sh' "$W/vm.out"
    grep -q 'vm-arm64 edition: CPU backends only' "$W/vm.out" && grep -q "installed brainsyncos-llama $BRAINSYNCOS_VERSION; " "$W/vm.out" \
        || die "vm: wrong packages"
    grep -q 'model: none yet' "$W/vm.out" || die "vm: the step did not say there is no model"
    [ ! -e "$t$LLAMA_PREFIX/lib/ggml/libggml-cuda.so" ] || die "vm: CUDA backend present"
    echo "vm edition: brainsyncos-llama only, no model, service enabled"
}

c_wheel() {
    local d="$BUILD_DIR/wheels" m="$BUILD_DIR/wheels/brainflow-cerelog.env" whl v="$W/venv" out
    [ -f "$m" ] || die "no $m (scripts/build-wheels.sh)"
    cat "$m"
    (cd "$d" && sha256sum -c SHA256SUMS)
    (
        load_env_file "$BRAINSYNCOS_ROOT/wheels/brainflow-cerelog/wheel.env" "SOURCE_COMMIT"
        for key in SOURCE_URL SOURCE_COMMIT SOURCE_TREE PATCHES CMAKE_FLAGS CMAKE_TARGETS WHEEL_DIST WHEEL_VERSION WHEEL_TAG; do
            eval "want=\$$key"
            grep -qxF "$key=$want" "$m" || die "manifest $key differs from wheel.env"
        done
    )
    whl="$d/$(sed -n 's/^WHEEL_FILE=//p' "$m")"
    [ "$(sha256_of "$whl")" = "$(sed -n 's/^WHEEL_SHA256=//p' "$m")" ] || die "the wheel does not match its manifest"
    grep -qx "brainflow==$(sed -n 's/^WHEEL_VERSION=//p' "$m") --hash=sha256:$(sha256_of "$whl")" "$d/requirements.txt" \
        || die "requirements.txt does not pin the wheel"
    python3 -I - "$whl" "$W/x" > "$W/listing" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for i in z.infolist():
    print(i.filename, oct(i.external_attr >> 16 & 0o777), i.date_time[0])
z.extractall(sys.argv[2])
PY
    cat "$W/listing"
    awk '{print $1}' "$W/listing" | grep -Evx 'brainflow/[a-z_]+\.py|brainflow/lib/lib(BoardController|DataHandler|MLModule)\.so|brainflow-[^/]*\.dist-info/(METADATA|WHEEL|RECORD|LICENSE\.txt)' \
        && die "unexpected files in the wheel"
    [ "$(grep -c '\.so ' "$W/listing")" = 3 ] || die "the wheel must hold exactly three libraries"
    for f in "$W/x"/brainflow/lib/*.so; do
        file -b "$f" | grep -q '^ELF 64-bit LSB shared object, ARM aarch64' || die "$(basename "$f"): $(file -b "$f")"
        ! objdump -p "$f" | awk '$1 == "NEEDED" {print $2}' \
            | grep -Evx 'libstdc\+\+\.so\.6|libm\.so\.6|libgcc_s\.so\.1|libc\.so\.6|ld-linux-aarch64\.so\.1' \
            || die "$(basename "$f") needs more than the C/C++ runtime"
    done
    grep -qx 'Requires-Dist: setuptools<81' "$W/x"/*.dist-info/METADATA && grep -qx 'Root-Is-Purelib: false' "$W/x"/*.dist-info/WHEEL \
        || die "METADATA or WHEEL fields"
    echo "wheel: $(basename "$whl"), only brainflow/*.py, 3 aarch64 libraries (C/C++ runtime only) and dist-info"

    python3 -m venv --system-site-packages "$v"
    "$v/bin/pip" install -q --no-index --find-links "$d" --require-hashes --no-deps --disable-pip-version-check -r "$d/requirements.txt"
    out="$(cd / && "$v/bin/python" -I -W ignore -c 'import brainflow, numpy, pkg_resources
from brainflow.board_shim import BoardShim, BoardIds
print(BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD), BoardShim.get_eeg_channels(BoardIds.CERELOG_X8_BOARD), brainflow.__file__)')"
    echo "venv: $out"
    case "$out" in "250 [1, 2, 3, 4, 5, 6, 7, 8] $v/"*) ;; *) die "want 250 Hz and EEG channels 1-8 from the venv" ;; esac
    "$v/bin/pip" check --disable-pip-version-check
    cp "$whl" "$W/t.whl"
    printf 'x' >> "$W/t.whl"
    mkdir -p "$W/tampered"
    mv "$W/t.whl" "$W/tampered/$(basename "$whl")"
    if "$v/bin/pip" install -q --no-index --find-links "$W/tampered" --require-hashes --no-deps --force-reinstall \
        --disable-pip-version-check -r "$d/requirements.txt" > "$W/pip.out" 2>&1; then
        die "pip installed a tampered wheel"
    fi
    grep -q 'THESE PACKAGES DO NOT MATCH THE HASHES' "$W/pip.out" || { cat "$W/pip.out"; die "tampered wheel refused for another reason"; }
    echo "pip --require-hashes: installs the wheel, refuses a tampered copy"
}

case "$1" in
    apt) c_apt ;;
    app-debs) c_app_debs ;;
    md5map) c_md5map ;;
    hook) c_hook ;;
    debs) c_debs ;;
    usbserial) c_usbserial ;;
    llama-debs) c_llama_debs ;;
    llama) c_llama ;;
    wheel) c_wheel ;;
    *) die "unknown check: $1 (see --help)" ;;
esac
force_rm_tree "$W"
