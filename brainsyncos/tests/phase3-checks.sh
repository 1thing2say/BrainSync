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
#             a .pyc beside every module, root-owned, md5sums OK; brainsync
#             Recommends espeak-ng, which is Ubuntu's unchanged .deb
#   md5map    merge-md5sums.py --map: a mapped tree's files are listed at their
#             ISO paths with correct sums; bad --map arguments are refused
#   hook      payload/hook.sh (00-release.sh, 60-apt-repo.sh and
#             70-brainsync.sh only) against NVIDIA's real target base layer
#             (ubuntu-server-minimal.squashfs), curtin via the chroot shim: the
#             repository is mounted read-only for the steps and unmounted
#             afterwards, brainsyncos-archive-keyring is installed (no apt error
#             or warning in the log) and verifies the repository, brainsync and
#             brainsync-runtime are installed and brainsync-voice runs in the
#             target, the target's apt sources and lists are unchanged; without
#             espeak-ng's libraries and without the account the step notes both
#             and makes secrets.env root:root 0640, comments only; a re-run
#             works and leaves secrets.env alone; a failing later step and a
#             missing repository fail the hook and leave no mount
#   voice     payload/hook.sh with 00, 60 and 70-brainsync.sh against NVIDIA's
#             target base layer plus the account (as 30-user.sh makes it) and
#             espeak-ng's libraries from the pool with their dependencies (as
#             NVIDIA's late-command installs them): espeak-ng installed from
#             our repository; brainsync/voice.py without a Cartesia key writes
#             a real WAV with it, as the account; secrets.env root:ACCOUNT
#             0640; with keys added, BCI Voice reads all three as the account
#             (both LLM backends on, the Cartesia key found) and another
#             account starts without them; a re-run changes nothing
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
#   cuda      the mirrored CUDA runtime (mirror/nvidia-jetson): the committed
#             key has the pinned fingerprint and is the one NVIDIA's own
#             install trusts (nvidia-l4t-apt-source in the base ISO's pool,
#             which also lists that repository and suite); the filled cache
#             verifies with no network (InRelease signed by that key, the
#             index, debs.lock = the closure of the roots, nothing forbidden);
#             the jetson test repository holds exactly the pinned .debs, its
#             index with the pinned sha256, the vm one none; refuses a
#             tampered InRelease, another key, a stale lock, a forbidden root
#             and a tampered .deb, and build-repo.sh refuses a tampered cache;
#             payload/hook.sh with 00, 60 and 80-cuda.sh in NVIDIA's target
#             base layer: exactly the pinned packages installed, their postinsts
#             ran (/usr/local/cuda alternative, ld.so.conf.d), ldconfig resolves
#             libcudart, libcublas and libcublasLt, nothing provides
#             libcuda.so.1, the target's apt state is unchanged, a re-run
#             works; the step skips on the vm edition
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
    [ "$(dpkg-deb -f "$app" Recommends)" = espeak-ng ] || die "brainsync must recommend espeak-ng, has: $(dpkg-deb -f "$app" Recommends)"
    (
        load_env_file "$BRAINSYNCOS_ROOT/packages/espeak-ng/package.env" "DEB_VERSION DEB_SHA256"
        f="$d/espeak-ng_${DEB_VERSION}_arm64.deb"
        [ "$(sha256_of "$f")" = "$DEB_SHA256" ] || die "espeak-ng is not Ubuntu's .deb (sha256)"
        dpkg-deb -c "$f" > "$W/espeak.list"
        grep -q ' \./usr/bin/espeak-ng$' "$W/espeak.list" || die "espeak-ng has no /usr/bin/espeak-ng"
        echo "espeak-ng $DEB_VERSION: Ubuntu's .deb, sha256 OK; Depends: $(dpkg-deb -f "$f" Depends)"
    )
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
    grep -q '70-brainsync.sh: note: offline voice espeak-ng not installed: its libraries are missing (libespeak-ng1' "$log" \
        || die "70-brainsync.sh did not note espeak-ng's missing libraries"
    grep -q "70-brainsync.sh: note: no account $BRAINSYNC_USER; /etc/brainsync/secrets.env is root:root" "$log" \
        || die "70-brainsync.sh did not note the missing account"
    secrets_ok "$t" root
    sha256sum "$t/etc/brainsync/secrets.env" > "$W/secrets.sum"
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
    sha256sum -c --quiet "$W/secrets.sum" || die "the re-run changed secrets.env"
    grep -q '70-brainsync.sh: /etc/brainsync/secrets.env exists (root:root 640); left as it is' "$log" || die "re-run: secrets.env not left alone"
    echo "re-run: OK, secrets.env unchanged"

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

# secrets_ok TARGET GROUP: /etc/brainsync/secrets.env is root:GROUP 0640, a
# template of comments that names the three keys and sets none.
secrets_ok() {
    local f="$1/etc/brainsync/secrets.env" k
    [ -f "$f" ] && [ ! -L "$f" ] || die "no secrets.env in the target"
    [ "$(stat -c '%u %a' "$f")" = "0 640" ] || die "secrets.env is $(stat -c '%u %a' "$f"), want root 640"
    [ "$("$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$1" -- stat -c %G /etc/brainsync/secrets.env)" = "$2" ] \
        || die "secrets.env group is not $2"
    ! grep -qv '^#' "$f" || die "secrets.env has lines that are not comments: $(grep -cv '^#' "$f")"
    for k in ANTHROPIC_API_KEY CARTESIA_API_KEY BRAINSYNC_LLAMA_API_KEY; do
        grep -qx "#$k=" "$f" || die "secrets.env does not name $k"
    done
    echo "secrets.env: root:$2 640, $(wc -l < "$f" | tr -d ' ') comment lines, no keys"
}

# pool_install TARGET PKG...: install PKGs and their dependencies into TARGET
# from the base ISO's pool, --no-install-recommends, as NVIDIA's late-command
# does: only the debs apt needs are taken from the ISO.
pool_install() {
    local t="$1" r="$1/tmp/pool" f
    shift
    mkdir -p "$r/dists/noble/main/binary-arm64"
    for f in dists/noble/Release dists/noble/main/binary-arm64/Packages dists/noble/main/binary-arm64/Packages.gz; do
        xorriso -osirrox on -indev "$(resolve_base_iso)" -extract "/$f" "$r/$f" > /dev/null 2>&1 \
            || die "cannot read /$f from the base ISO"
    done
    printf 'Types: deb\nURIs: file:/tmp/pool\nSuites: noble\nComponents: main\nTrusted: yes\n' > "$W/pool.sources"
    mkdir -p "$t/tmp/pool-apt/lists/partial" "$t/tmp/pool-apt/archives/partial"
    cp "$W/pool.sources" "$t/tmp/pool-apt/pool.sources"
    pool_apt() {
        "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- env DEBIAN_FRONTEND=noninteractive \
            apt-get -o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts=/tmp/pool-apt \
            -o Dir::State::Lists=/tmp/pool-apt/lists -o Dir::Cache::Archives=/tmp/pool-apt/archives \
            -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= -o APT::Sandbox::User=root -o Dpkg::Use-Pty=0 \
            -y -q --no-install-recommends "$@"
    }
    pool_apt update > "$W/pool.log" 2>&1 || { cat "$W/pool.log"; die "apt update from the pool index failed"; }
    pool_apt -s install "$@" > "$W/pool.sim" 2>&1 || { cat "$W/pool.sim"; die "the pool cannot satisfy: $*"; }
    for f in $(awk '/^Inst /{print $2}' "$W/pool.sim"); do
        f="$(awk -v p="$f" '$1 == "Package:" {q = ($2 == p)} q && $1 == "Filename:" {print $2; exit}' \
            "$r/dists/noble/main/binary-arm64/Packages")"
        [ -n "$f" ] || die "no Filename in the pool index"
        mkdir -p "$r/$(dirname "$f")"
        xorriso -osirrox on -indev "$(resolve_base_iso)" -extract "/$f" "$r/$f" > /dev/null 2>&1 || die "cannot read /$f from the base ISO"
    done
    pool_apt install "$@" > "$W/pool.log" 2>&1 || { tail -20 "$W/pool.log"; die "installing from the pool failed"; }
    force_rm_tree "$r"
    force_rm_tree "$t/tmp/pool-apt"
    echo "target: $* from the pool, with $(grep -c '^Inst ' "$W/pool.sim") packages in all"
}

# as_user TARGET USER CMD...: CMD as USER inside TARGET, with a login-like environment.
as_user() {
    local t="$1" u="$2" g
    shift 2
    g="$("$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- id -g "$u")"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- \
        setpriv --reuid="$u" --regid="$g" --init-groups env -i HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 "$@"
}

c_voice() {
    local t="$W/target" p="$W/hp" log="$W/target/var/log/brainsyncos/install.log" u="$BRAINSYNC_USER" out
    target_layer "$t"
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- useradd -m -s /bin/bash "$u"
    pool_install "$t" libespeak-ng1 espeak-ng-data libpcaudio0 libsonic0
    hook_payload "$p" 70-brainsync.sh
    run_hook "$p" "$t" "$REPO" || { tail -30 "$W/live.log"; die "hook failed"; }
    ! grep -n '^E: \|^W: \|WARNING' "$log" || die "the install logged an apt error or warning"
    sed -n '/== 70-brainsync.sh/,$p' "$log" | grep '70-brainsync.sh:'
    grep -q '70-brainsync.sh: offline voice: espeak-ng .*, from the repository' "$log" || die "espeak-ng was not installed from the repository"
    grep -q "70-brainsync.sh: /etc/brainsync/secrets.env created (template, no keys), root:$u 640" "$log" || die "secrets.env not created"
    secrets_ok "$t" "$u"

    # The app's offline voice, as the account: no Cartesia key, so espeak-ng writes the clip.
    out="$(as_user "$t" "$u" BRAINSYNC_TTS_CACHE=/tmp/tts /opt/brainsyncos/venv/bin/python -I -c '
from brainsync import voice
s = voice.CartesiaSpeaker(None)
p = s.clip("Look at the piece you want to move.")
d = p.read_bytes()
print(p.name, len(d), d[:4] == b"RIFF" and d[8:12] == b"WAVE")
s.close()' 2> "$W/voice.err")" || { cat "$W/voice.err"; die "the app's voice failed in the target"; }
    echo "voice.py, no key: $out ($(cat "$W/voice.err"))"
    case "$out" in "espeak-"*" True") ;; *) die "no espeak-ng WAV from voice.py: $out" ;; esac
    [ "$(echo "$out" | cut -d' ' -f2)" -gt 10000 ] || die "the espeak-ng WAV is too short: $out"

    # Keys added (as the owner, or 85-llama.sh, would): BCI Voice reads all three as the account.
    printf 'ANTHROPIC_API_KEY=sk-ant-test-not-a-key\nCARTESIA_API_KEY=sk-car-test-not-a-key\nBRAINSYNC_LLAMA_API_KEY=llama-test-not-a-key\n' \
        >> "$t/etc/brainsync/secrets.env"
    out="$(as_user "$t" "$u" /opt/brainsyncos/venv/bin/python -I -c '
import json, os
from app import config
from app.menus import MenuMaker
from brainsync import voice
names = config.load_env()
cfg = config.load_config()
m = MenuMaker(cfg.menus, cfg.llm, config.load_intents(), config.load_profile(), keys=os.environ)
print(json.dumps({"names": sorted(names), "cartesia": voice.find_api_key() == "sk-car-test-not-a-key",
                  "off": [b.name for b in m.backends if b.why_disabled]}))' 2>&1)" || { echo "$out"; die "BCI Voice cannot read the keys"; }
    echo "as $u: $out"
    [ "$out" = '{"names": ["ANTHROPIC_API_KEY", "BRAINSYNC_LLAMA_API_KEY", "CARTESIA_API_KEY"], "cartesia": true, "off": []}' ] \
        || die "BCI Voice did not get all three keys"
    out="$(as_user "$t" nobody /opt/brainsyncos/venv/bin/python -I -c 'from app import config; print(config.load_env())' 2>&1)" \
        || { echo "$out"; die "another account cannot start BCI Voice's config"; }
    echo "as nobody: $out"
    printf '%s\n' "$out" | grep -q 'cannot read /etc/brainsync/secrets.env (PermissionError)' && printf '%s\n' "$out" | grep -qx '\[\]' \
        || die "another account: no note, or keys read"
    ! printf '%s\n' "$out" | grep -q 'not-a-key' || die "a key was printed"

    sha256sum "$t/etc/brainsync/secrets.env" > "$W/secrets.sum"
    run_hook "$p" "$t" "$REPO" > /dev/null 2>&1 || { tail -20 "$W/live.log"; die "re-run failed"; }
    sha256sum -c --quiet "$W/secrets.sum" || die "the re-run changed secrets.env"
    grep -q "70-brainsync.sh: /etc/brainsync/secrets.env exists (root:$u 640); left as it is" "$log" || die "re-run: secrets.env not left alone"
    grep -q '70-brainsync.sh: offline voice: espeak-ng .*, already installed' "$log" || die "re-run: espeak-ng"
    no_mount_left "$t"
    echo "re-run: secrets.env with its keys unchanged, espeak-ng already installed"
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

# mirror_py CMD MIRROR CACHE KEYRING [ARGS...]: scripts/mirror.py, output in $W/mirror.out.
mirror_py() {
    local cmd="$1" m="$2" c="$3" k="$4"
    shift 4
    python3 -I "$BRAINSYNCOS_ROOT/scripts/mirror.py" "$cmd" --mirror "$m" --cache "$c" --keyring "$k" "$@" > "$W/mirror.out" 2>&1
}

# expect_mirror_fail LABEL PATTERN CMD MIRROR CACHE KEYRING [ARGS...]
expect_mirror_fail() {
    local label="$1" pat="$2"
    shift 2
    if mirror_py "$@"; then cat "$W/mirror.out"; die "$label: accepted"; fi
    grep -Eq "$pat" "$W/mirror.out" || { cat "$W/mirror.out"; die "$label: refused for another reason"; }
    echo "refuses $label: $(grep -Em1 "$pat" "$W/mirror.out")"
}

# cache_copy SRC DEST: DEST = the cache SRC, small files copied and the large
# .debs as symlinks (never hard links: cleaning up truncates large files).
cache_copy() {
    local f
    mkdir -p "$2"
    for f in "$1"/*; do
        if [ "$(file_size "$f")" -gt 1048576 ]; then ln -s "$f" "$2/"; else cp "$f" "$2/"; fi
    done
}

in_t() { "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$1" -- "${@:2}"; }

c_cuda() {
    local m="$BRAINSYNCOS_ROOT/mirror/nvidia-jetson" cache="$MIRROR_CACHE/nvidia-jetson" k="$W/nvidia.gpg"
    local fpr x="$W/aptsrc" f n names new pool="$REPO/pool/$REPO_COMPONENT" t="$W/target" p="$W/hp"
    local log="$W/target/var/log/brainsyncos/install.log" key="$T/key/repo-signing-key.asc"
    [ "$(mirror_names jetson)" = nvidia-jetson ] && [ -z "$(mirror_names vm)" ] \
        || die "mirrors: jetson '$(mirror_names jetson)', vm '$(mirror_names vm)'"
    fpr="$(sed -n 's/^MIRROR_KEY_FPR=//p' "$m/mirror.env")"
    (load_mirror nvidia-jetson; mirror_keyring nvidia-jetson "$k")
    # The key and the source NVIDIA's own install configures (nvidia-l4t-apt-source).
    xorriso -osirrox on -indev "$(resolve_base_iso)" -extract /pool/main/n/nvidia-l4t-apt-source "$W/aptsrc-debs" \
        > /dev/null 2>&1 || die "cannot read nvidia-l4t-apt-source from the base ISO"
    dpkg-deb -x "$(ls "$W"/aptsrc-debs/nvidia-l4t-apt-source_*_arm64.deb)" "$x"
    [ "$(key_fingerprint "$x/etc/apt/trusted.gpg.d/jetson-ota-public.asc")" = "$fpr" ] || die "NVIDIA's ISO trusts another key"
    grep -qx "deb $(sed -n 's/^MIRROR_URL=//p' "$m/mirror.env") $(sed -n 's/^MIRROR_SUITE=//p' "$m/mirror.env") main" \
        "$x/etc/apt/sources.list.d/nvidia-l4t-apt-source.list" || die "the stock install configures another repository"
    if cmp -s "$x/etc/apt/trusted.gpg.d/jetson-ota-public.asc" "$m/jetson-ota-public.asc"; then f="the same file"; else f="the same key"; fi
    echo "key $fpr: $f as nvidia-l4t-apt-source's, whose sources list has the mirrored repository and suite"

    # The filled cache, with no network at all.
    unshare -n python3 -I "$BRAINSYNCOS_ROOT/scripts/mirror.py" fetch --mirror "$m" --cache "$cache" --keyring "$k" \
        > "$W/mirror.out" 2>&1 || { cat "$W/mirror.out"; die "the cached mirror does not verify offline"; }
    cat "$W/mirror.out"
    ! grep -q '^downloading' "$W/mirror.out" && [ "$(grep -c '^cached: ' "$W/mirror.out")" = 5 ] || die "want 5 cached .debs"
    grep -q 'relations left to the target system: none' "$W/mirror.out" || die "the closure needs packages from elsewhere"

    # The test repositories: exactly the pinned .debs on the jetson one, none on the vm one.
    names="$(sed -n 's/^\([a-z0-9][^ ]*\) .*/\1/p' "$m/debs.lock" | LC_ALL=C sort | tr '\n' ' ')"
    [ "$names" = "cuda-cudart-13-2 cuda-toolkit-13-2-config-common cuda-toolkit-13-config-common cuda-toolkit-config-common libcublas-13-2 " ] \
        || die "debs.lock pins: $names"
    n=0
    for f in $(python3 -I "$BRAINSYNCOS_ROOT/scripts/mirror.py" files --mirror "$m" --cache "$cache" --keyring "$k"); do
        cmp "$pool/$f" "$cache/$f" || die "$f in the repository differs from the cache"
        n=$((n + 1))
    done
    python3 -I - "$m/debs.lock" "$REPO/dists/$REPO_SUITE/$REPO_COMPONENT/binary-arm64/Packages" "$REPO_COMPONENT" <<'PY'
import os, sys
lock, index, comp = sys.argv[1:]
st = {}
for s in open(index).read().split("\n\n"):
    d = dict(l.split(": ", 1) for l in s.splitlines() if ": " in l and not l.startswith(" "))
    if d:
        st[d["Package"]] = d
for line in open(lock):
    if line.strip() and not line.startswith("#"):
        pkg, ver, arch, size, sha, fn = line.split()
        d = st.get(pkg)
        want = {"Version": ver, "Architecture": arch, "Size": size, "SHA256": sha,
                "Filename": "pool/%s/%s" % (comp, os.path.basename(fn))}
        if d is None or any(d.get(k) != v for k, v in want.items()):
            sys.exit("the repository index has %s other than pinned: %s" % (pkg, d))
print("repository index: every pinned .deb at its pinned version, size and sha256")
PY
    ! ls "$T/repo/vm/pool/$REPO_COMPONENT"/cuda-* "$T/repo/vm/pool/$REPO_COMPONENT"/libcublas* > /dev/null 2>&1 \
        || die "the vm repository has CUDA .debs"
    echo "jetson repository: the $n pinned .debs, the same bytes as the cache, $(du -sm "$pool" | cut -f1) MB pool; vm repository: none"

    # Refusals, on copies.
    cache_copy "$cache" "$W/c"
    sed -i 's/^Date: .*/Date: Sat, 24 Oct 2026 00:00:00 UTC/' "$W/c/InRelease"
    expect_mirror_fail "a tampered InRelease" 'does not verify' check "$m" "$W/c" "$k"
    expect_mirror_fail "another key" 'does not verify' check "$m" "$cache" "$REPO/repo-key.gpg"
    cp -R "$m" "$W/m-stale"
    sed -i 's/^\(cuda-cudart-13-2 [^ ]* [^ ]* [^ ]* \)[0-9a-f]\{64\}/\10000000000000000000000000000000000000000000000000000000000000000/' \
        "$W/m-stale/debs.lock"
    expect_mirror_fail "a stale debs.lock" 'debs.lock differs from the closure' check "$W/m-stale" "$cache" "$k"
    cp -R "$m" "$W/m-forbid"
    sed -i 's/^MIRROR_ROOTS=.*/MIRROR_ROOTS=libnvidia-compute=595.71.05-1ubuntu1/' "$W/m-forbid/mirror.env"
    expect_mirror_fail "a dGPU root" 'MIRROR_FORBID rules out' check "$W/m-forbid" "$cache" "$k"
    force_rm_tree "$W/c"
    cache_copy "$cache" "$W/c/nvidia-jetson"
    f="$(ls "$W"/c/nvidia-jetson/cuda-cudart-13-2_*.deb)"
    printf 'x' >> "$f"
    expect_mirror_fail "a tampered .deb" 'not the pinned size and sha256' check "$m" "$W/c/nvidia-jetson" "$k"
    if BSOS_MIRROR_CACHE="$W/c" "$BRAINSYNCOS_ROOT/scripts/build-repo.sh" --edition jetson --debs "$T/debs/jetson" \
            --key "$key" --out "$W/r" > "$W/repo.out" 2>&1; then
        die "build-repo.sh accepted a tampered mirror cache"
    fi
    grep -q 'the mirrored .debs do not match their pins' "$W/repo.out" || { cat "$W/repo.out"; die "build-repo.sh failed for another reason"; }
    [ ! -e "$W/r" ] && [ ! -e "$W/r.new" ] || die "the refused build-repo.sh left output"
    echo "refuses build-repo.sh with a tampered cache: $(grep -m1 'do not match' "$W/repo.out")"
    force_rm_tree "$W/c"

    # 80-cuda.sh through the hook in NVIDIA's target base layer.
    target_layer "$t"
    in_t "$t" dpkg-query -W -f '${Package}\n' | LC_ALL=C sort > "$W/pkgs-before"
    apt_state "$t" > "$W/apt-before"
    hook_payload "$p" 80-cuda.sh
    run_hook "$p" "$t" "$REPO" || { tail -30 "$W/live.log"; die "hook failed"; }
    grep -q 'BrainSyncOS layer installed (3 steps)' "$log" || die "log lacks the summary"
    ! grep -n '^E: \|^W: \|WARNING' "$log" || die "the install logged an apt error or warning"
    sed -n '/== 80-cuda.sh/,$p' "$log" | grep -E '80-cuda.sh|alternatives'
    grep -q '^80-cuda.sh: installed cuda-cudart-13-2 13.2.86-1, libcublas-13-2 13.4.1.3-1; new packages: ' "$log" \
        || die "80-cuda.sh did not finish"
    in_t "$t" dpkg-query -W -f '${Package}\n' | LC_ALL=C sort > "$W/pkgs-after"
    new="$(LC_ALL=C comm -13 "$W/pkgs-before" "$W/pkgs-after" | tr '\n' ' ')"
    [ "$new" = "brainsyncos-archive-keyring $names" ] || die "installed other packages than the keyring and the pinned ones: $new"
    grep -qx "80-cuda.sh: installed .*; new packages: ${names% }" "$log" || die "80-cuda.sh installed other packages than the pinned ones"
    [ "$(readlink "$t/usr/local/cuda")" = /etc/alternatives/cuda ] && [ "$(readlink "$t/etc/alternatives/cuda")" = /usr/local/cuda-13.2 ] \
        || die "the /usr/local/cuda alternative is not set"
    grep -qx /usr/local/cuda/targets/sbsa-linux/lib "$t/etc/ld.so.conf.d/000_cuda.conf" \
        && grep -qx /usr/local/cuda-13/targets/sbsa-linux/lib "$t/etc/ld.so.conf.d/987_cuda-13.conf" || die "ld.so.conf.d lacks the CUDA paths"
    in_t "$t" ldconfig -p > "$W/ldconfig.out"
    grep -E 'libcudart|libcublas' "$W/ldconfig.out"
    ! grep -q 'libcuda\.so' "$W/ldconfig.out" || die "something in the target provides libcuda.so"
    in_t "$t" ldd /usr/local/cuda-13.2/targets/sbsa-linux/lib/libcublas.so.13 | sed 's/^/  /'
    apt_state "$t" > "$W/apt-after"
    diff "$W/apt-before" "$W/apt-after" || die "the target's apt sources or lists changed"
    no_mount_left "$t"
    echo "jetson: 80-cuda.sh installed exactly the pinned packages (${names% }); their postinsts set the alternatives and ld.so.conf.d; no libcuda"
    run_hook "$p" "$t" "$REPO" > /dev/null 2>&1 || { tail -20 "$W/live.log"; die "re-run failed"; }
    grep -q '^80-cuda.sh: installed .*; new packages: none$' "$log" || die "re-run installed something"
    echo "re-run: OK, nothing new"

    # The vm edition: the step skips before it touches apt.
    sed -i 's/^BSOS_EDITION=.*/BSOS_EDITION=vm-arm64/' "$t/opt/brainsyncos/installer/device.env"
    in_t "$t" env BSOS_INSTALL_DIR=/opt/brainsyncos/installer BSOS_REPO_DIR=/tmp/no-such-repo \
        /bin/bash /opt/brainsyncos/installer/install.d/80-cuda.sh > "$W/vm.out" 2>&1 \
        || { cat "$W/vm.out"; die "80-cuda.sh failed on the vm edition"; }
    cat "$W/vm.out"
    grep -qx '80-cuda.sh: vm-arm64 edition: skipped (no NVIDIA GPU, no CUDA)' "$W/vm.out" || die "vm: no skip message"
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
    voice) c_voice ;;
    debs) c_debs ;;
    usbserial) c_usbserial ;;
    cuda) c_cuda ;;
    wheel) c_wheel ;;
    *) die "unknown check: $1 (see --help)" ;;
esac
force_rm_tree "$W"
