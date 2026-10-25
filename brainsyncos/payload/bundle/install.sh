#!/bin/bash
# install.sh - install the BrainSyncOS layer on a Jetson Orin that already runs
# stock JetPack 7.2.1 (Jetson Linux R39.2.1). No reflash: the system keeps its
# Ubuntu, kernel, accounts and settings. Packed by scripts/make-bundle.sh.
#
# Usage, as root, from the unpacked bundle directory:
#   sudo ./install.sh [--user NAME] [--authorized-keys FILE] [--password-hash FILE]
#                     [--yes] [--repo DIR] [--skip-space-check] [--no-start]
#
# What it does, in order:
#  1. Checks the system: aarch64; /etc/nv_tegra_release names the release the
#     bundle was built for (L4T_RELEASE in bundle.env, R39.2.1: the CH340
#     module and NVIDIA's CUDA 13.2 runtime in the bundle are built for exactly
#     that kernel and release, so any other release is refused); the installed
#     nvidia-l4t-kernel is the version the module depends on; enough free
#     space for the packages (their Installed-Size, plus a fifth and 1 GiB).
#  2. Settles the account that runs the apps (BRAINSYNC_USER for the steps:
#     70-brainsync.sh gives it /etc/brainsync/secrets.env, 85-llama.sh needs
#     its group). --user NAME: an existing account (JetPack's first-boot user)
#     gets the hardware groups 30-user.sh gives, where they exist (dialout for
#     the EEG board, audio, video, render, input, plugdev, i2c, gpio, ...;
#     never sudo or adm); a NAME that does not exist is created by the
#     bundle's unchanged copy of install.d/30-user.sh from --authorized-keys
#     and/or --password-hash (a crypt(3) line: openssl passwd -6), as the ISO
#     build would have, so it gets the same groups, sudo rule and SSH setup.
#     Without --user: a re-run takes the last run's account; otherwise it
#     asks (or, with --yes, takes the one login account here, else stops).
#     30-user.sh also removes NVIDIA's fallback accounts nvidia and ubuntu, so
#     when one exists here, creating an account is refused: use --user with
#     that account instead.
#  3. Copies lib/, install.d/, account/ and the key to /opt/brainsyncos/installer
#     (where the ISO install keeps them) with settings.env and device.env
#     written the way hook.sh writes them, then runs every install.d step
#     there in order, directly on this system (no curtin, no chroot), with
#     BSOS_INSTALL_DIR and BSOS_REPO_DIR = the bundle's repo/ directory: apt
#     reads it in place through lib/apt-repo.sh (that repository alone,
#     offline, signature checked against the bundle's key). The steps are the
#     ISO's, unchanged. A failing step stops the install; every step is
#     idempotent, so a re-run carries on from there.
#  4. Records the install in /etc/brainsyncos-release (as 00-release.sh does,
#     plus INSTALL_METHOD=bundle and BRAINSYNC_USER), reloads udev and systemd
#     and starts brainsyncos-llama.service when a model is installed
#     (--no-start: leave it to the next boot).
# Log: /var/log/brainsyncos/install.log (appended), like the ISO install.
# --yes asks nothing (tests, unattended): what would be asked must be given as
# an option. BSOS_MODEL_VERIFY=0 skips 86-model.sh's sha256 of the model.
# Test overrides: BSOS_NV_TEGRA_RELEASE (the release file), BSOS_DT_DIR (the
# device tree), BSOS_INSTALL_LOG (the log file).

set -euo pipefail

me=install.sh
say()  { echo "$me: $*"; }
fail() { echo "$me: ERROR: $*" >&2; exit 1; }

bundle="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
dest=/opt/brainsyncos/installer
log="${BSOS_INSTALL_LOG:-/var/log/brainsyncos/install.log}"
user=""
keys_file=""
hash_file=""
yes=0
repo=""
space_check=1
start=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --user) user="${2:?--user needs a name}"; shift 2 ;;
        --authorized-keys) keys_file="${2:?--authorized-keys needs a file}"; shift 2 ;;
        --password-hash) hash_file="${2:?--password-hash needs a file}"; shift 2 ;;
        --yes|-y) yes=1; shift ;;
        --repo) repo="${2:?--repo needs a directory}"; shift 2 ;;
        --skip-space-check) space_check=0; shift ;;
        --no-start) start=0; shift ;;
        -h|--help) sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "$0"; exit 0 ;;
        *) fail "unknown argument: $1 (try --help)" ;;
    esac
done
[ "$(id -u)" = 0 ] || fail "run it as root: sudo $0"
[ -t 0 ] || yes=1

# Everything to the log as well, like hook.sh. tee gets at most 10 s to drain.
mkdir -p "$(dirname "$log")"
exec > >(tee -a "$log") 2>&1
tee_pid=$!
tmp_hash=""
finish() {
    local rc=$?
    [ -z "$tmp_hash" ] || rm -f "$tmp_hash"
    say "exit $rc"
    exec >&- 2>&-
    for _ in $(seq 1 50); do
        kill -0 "$tee_pid" 2>/dev/null || break
        sleep 0.2
    done
    kill "$tee_pid" 2>/dev/null || true
    exit "$rc"
}
trap finish EXIT

# --- the bundle -------------------------------------------------------------------
for f in bundle.env release repo-key.gpg lib/apt-repo.sh account/30-user.sh; do
    [ -s "$bundle/$f" ] || fail "$bundle/$f is missing: $bundle is not an unpacked BrainSyncOS bundle"
done
ls "$bundle"/install.d/[0-9][0-9]-*.sh > /dev/null 2>&1 || fail "no install.d steps in $bundle"
case "$bundle" in "$dest"|"$dest"/*) fail "unpack the bundle somewhere other than $dest (install.sh replaces that directory)" ;; esac

# load_env FILE KEY...: source FILE after checking every line is plain KEY=value
# (so sourcing it cannot run code), then require each KEY.
load_env() {
    local f="$1" line key
    shift
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        printf '%s\n' "$line" | grep -Eq '^[A-Z][A-Z0-9_]*=[A-Za-z0-9._,:/+@=-]*$' || fail "$f: not a plain KEY=value line: $line"
    done < "$f"
    # shellcheck disable=SC1090  # checked just above
    . "$f"
    for key in "$@"; do
        [ -n "${!key:-}" ] || fail "$f: $key is missing or empty"
    done
}
load_env "$bundle/bundle.env" VERSION EDITION L4T_RELEASE JETPACK_RELEASE TEGRA_KERNEL KERNEL_RELEASE \
    REPO_SUITE REPO_COMPONENT BRAINSYNC_USER HOSTNAME_PREFIX REPO_KEY INSTALLED_KIB
say "BrainSyncOS $VERSION (build ${BUILD_ID:-unknown}, $EDITION) from $bundle"

repo="${repo:-$bundle/repo}"
[ -d "$repo/dists/$REPO_SUITE" ] || fail "no package repository at $repo (no dists/$REPO_SUITE)"
repo="$(cd "$repo" && pwd -P)"
case "$repo" in *[[:space:]:]*) fail "the repository path must have no spaces or colons (apt reads it as file:$repo)" ;; esac
case "$repo" in "$dest"|"$dest"/*) fail "the repository must not be under $dest (install.sh replaces that directory)" ;; esac
[ "$(cat "$repo/edition" 2>/dev/null)" = jetson ] || fail "$repo was built for edition '$(cat "$repo/edition" 2>/dev/null)', not jetson"
cmp -s "$repo/repo-key.gpg" "$bundle/repo-key.gpg" || fail "$repo/repo-key.gpg is not the bundle's key"
command -v gpgv > /dev/null || fail "gpgv is missing (apt needs it too)"
gpgv --keyring "$bundle/repo-key.gpg" "$repo/dists/$REPO_SUITE/InRelease" > /dev/null 2>&1 \
    || fail "$repo/dists/$REPO_SUITE/InRelease does not verify against the bundle's key $REPO_KEY"
say "repository: $repo (${PACKAGES:-?} packages, verified)"

# --- 1. the system ----------------------------------------------------------------
arch="$(uname -m)"
[ "$arch" = aarch64 ] || fail "this machine is $arch; the bundle is for aarch64 (Jetson Orin)"
nvrel="${BSOS_NV_TEGRA_RELEASE:-/etc/nv_tegra_release}"
[ -r "$nvrel" ] || fail "no $nvrel: this is not a Jetson Linux (L4T) system. The bundle needs stock JetPack $JETPACK_RELEASE (Jetson Linux R$L4T_RELEASE) installed first"
l4t="$(sed -n 's/^# R\([0-9][0-9]*\) (release), REVISION: \([0-9.][0-9.]*\),.*/\1.\2/p' "$nvrel" | head -1)"
[ -n "$l4t" ] || fail "cannot read the release from $nvrel: $(head -1 "$nvrel")"
[ "$l4t" = "$L4T_RELEASE" ] || fail "this system runs Jetson Linux R$l4t; the bundle is built for R$L4T_RELEASE (JetPack $JETPACK_RELEASE). Its CH340 module and CUDA runtime fit that kernel and release only: flash JetPack $JETPACK_RELEASE first, or build a bundle for R$l4t"
say "Jetson Linux R$l4t: matches the bundle"
kstate="$(dpkg-query -W -f '${db:Status-Status} ${Version}' nvidia-l4t-kernel 2>/dev/null || true)"
case "$kstate" in
    "installed $TEGRA_KERNEL") say "nvidia-l4t-kernel $TEGRA_KERNEL: the one the CH340 module is built for" ;;
    "installed "*) fail "nvidia-l4t-kernel is ${kstate#installed }; the bundle's CH340 module (brainsyncos-ch341-tegra) depends on exactly $TEGRA_KERNEL. Rebuild the bundle against this kernel" ;;
    *) say "note: nvidia-l4t-kernel is not installed; the CH340 module needs $TEGRA_KERNEL and 75-usb-serial.sh will say so" ;;
esac
[ "$(uname -r)" = "$KERNEL_RELEASE" ] || say "note: the running kernel is $(uname -r); the CH340 module is for $KERNEL_RELEASE and loads once that kernel runs"

dt="${BSOS_DT_DIR:-/sys/firmware/devicetree/base}"
dt_model="$(tr -d '\0' 2>/dev/null < "$dt/model" || true)"
serial="$(tr -d '\0' 2>/dev/null < "$dt/serial-number" | tr -cd 'A-Za-z0-9' || true)"
case "$dt_model" in
    "NVIDIA Jetson Orin Nano"*) say "board: $dt_model, serial ${serial:-unknown}" ;;
    '') say "WARNING: no device tree model at $dt; BrainSyncOS is built for Jetson Orin Nano"; dt_model=unknown ;;
    *) say "WARNING: BrainSyncOS is built for Jetson Orin Nano; this board is: $dt_model" ;;
esac

if [ "$space_check" = 1 ]; then
    need_kb=$(( INSTALLED_KIB + INSTALLED_KIB / 5 + 1048576 ))
    seen=""
    for d in / /opt /usr /var /etc; do
        fs="$(df -Pk "$d" | awk 'NR == 2 {print $1}')"
        case " $seen " in *" $fs "*) continue ;; esac
        seen="$seen $fs"
        avail="$(df -Pk "$d" | awk 'NR == 2 {print $4}')"
        [ "$avail" -ge "$need_kb" ] \
            || fail "only $((avail / 1024)) MiB free on $d ($fs); the packages need about $((need_kb / 1024)) MiB (Installed-Size $((INSTALLED_KIB / 1024)) MiB plus a margin). Free some space, or --skip-space-check"
    done
    say "free space: enough (about $((need_kb / 1024)) MiB needed)"
else
    say "free space: not checked (--skip-space-check)"
fi

# --- 2. the account -----------------------------------------------------------------
prev_user=""
[ ! -s "$dest/settings.env" ] || prev_user="$(sed -n 's/^BRAINSYNC_USER=//p' "$dest/settings.env" | head -1)"
candidates="$(awk -F: '$3 >= 1000 && $3 < 60000 && $7 !~ /(nologin|false)$/ {print $1}' /etc/passwd | tr '\n' ' ' | sed 's/ $//')"
if [ -z "$user" ]; then
    if [ -n "$prev_user" ]; then
        user="$prev_user"
        say "account: $user (the last install's, from $dest/settings.env; --user NAME changes it)"
    elif [ "$yes" = 1 ]; then
        case "$candidates" in
            '') fail "no login account here; pass --user NAME to create one (with --authorized-keys and/or --password-hash)" ;;
            *' '*) fail "which account runs the apps? Pass --user NAME (here: $candidates; a new name such as $BRAINSYNC_USER creates it)" ;;
            *) user="$candidates"; say "account: $user (the one login account here)" ;;
        esac
    else
        echo
        echo "Which account runs the BrainSync apps? An existing one (here: ${candidates:-none}),"
        echo "or a new name such as $BRAINSYNC_USER, which is then created (it needs a password"
        echo "or --authorized-keys)."
        read -r -p "Account [${candidates%% *}]: " user
        user="${user:-${candidates%% *}}"
    fi
fi
printf '%s\n' "$user" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$' || fail "not a usable account name: '$user'"
if id "$user" > /dev/null 2>&1; then
    create=0
    [ -z "$keys_file$hash_file" ] \
        || fail "--authorized-keys and --password-hash apply only to an account install.sh creates; $user exists (change its logins with passwd or ~/.ssh/authorized_keys)"
    [ "$(id -u "$user")" -ge 1000 ] || fail "$user is a system account (uid $(id -u "$user")); pick a login account (uid 1000 or more)"
    getent group "$user" > /dev/null \
        || fail "account $user has no group named $user; 70-brainsync.sh and 85-llama.sh give the apps' files to that group (make it, or pick another account)"
else
    create=1
    for v in nvidia ubuntu; do
        ! id "$v" > /dev/null 2>&1 \
            || fail "creating $user runs 30-user.sh, which removes NVIDIA's fallback account $v (known password), and this system has one. Use --user $v to make it the apps' account, or remove it yourself first"
    done
    if [ -z "$keys_file" ] && [ -z "$hash_file" ]; then
        [ "$yes" = 0 ] || fail "creating $user needs --authorized-keys FILE (SSH public keys) and/or --password-hash FILE (one crypt(3) line: openssl passwd -6)"
        read -r -s -p "Password for the new account $user (empty: SSH keys only; needs --authorized-keys): " pw
        echo
        if [ -n "$pw" ]; then
            tmp_hash="$(mktemp)"
            printf '%s\n' "$pw" | openssl passwd -6 -stdin > "$tmp_hash" || fail "openssl passwd failed"
            hash_file="$tmp_hash"
        fi
        unset pw
        [ -n "$hash_file" ] || fail "no password and no --authorized-keys: $user could not log in"
    fi
    [ -z "$keys_file" ] || grep -q '^[^#[:space:]]' "$keys_file" || fail "$keys_file holds no SSH public keys"
    [ -z "$keys_file" ] || ! grep -q 'PRIVATE KEY' "$keys_file" || fail "$keys_file holds a PRIVATE key; use the .pub file"
    [ -z "$hash_file" ] || { [ "$(grep -c . "$hash_file")" = 1 ] && grep -Eqx '\$(6|5|y)\$[A-Za-z0-9./$]+' "$hash_file"; } \
        || fail "$hash_file is not a single crypt(3) hash line (\$6\$..., \$y\$...; make one with: openssl passwd -6)"
    say "account: $user does not exist; 30-user.sh creates it"
fi

# --- 3. the payload, then the steps --------------------------------------------------
rm -rf "${dest:?}"
mkdir -p "$dest/install.d" "$dest/account" "$dest/config"
cp -R "$bundle/lib" "$dest/lib"
cp "$bundle"/install.d/[0-9][0-9]-*.sh "$dest/install.d/"
cp "$bundle/account/30-user.sh" "$dest/account/"
cp "$bundle/release" "$bundle/repo-key.gpg" "$bundle/bundle.env" "$dest/"
printf 'BRAINSYNC_USER=%s\nHOSTNAME_PREFIX=%s\nREPO_SUITE=%s\nREPO_COMPONENT=%s\n' \
    "$user" "$HOSTNAME_PREFIX" "$REPO_SUITE" "$REPO_COMPONENT" > "$dest/settings.env"
printf 'BSOS_DT_MODEL=%q\nBSOS_SERIAL=%q\nBSOS_EDITION=%q\nBSOS_MODEL=%q\nBSOS_ID_SOURCE=%q\n' \
    "$dt_model" "$serial" "$EDITION" "$dt_model" devicetree > "$dest/device.env"
[ -z "$keys_file" ] || grep -v '^[[:space:]]*$' "$keys_file" > "$dest/config/authorized_keys"
[ -z "$hash_file" ] || cp "$hash_file" "$dest/config/password.hash"
chown -R root:root "$dest"
chmod -R u+rwX,go+rX,go-w "$dest"
chmod -R go-rwx "$dest/config"
say "payload in $dest (settings.env: BRAINSYNC_USER=$user; device.env: BSOS_EDITION=$EDITION)"

# The steps' environment: what curtin in-target gives them in the ISO install,
# not the caller's shell (sudo keeps DISPLAY, LC_* and more).
step_env=(env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8
    TERM="${TERM:-dumb}" DEBIAN_FRONTEND=noninteractive BSOS_INSTALL_DIR="$dest" BSOS_REPO_DIR="$repo")
[ -z "${BSOS_MODEL_VERIFY:-}" ] || step_env+=(BSOS_MODEL_VERIFY="$BSOS_MODEL_VERIFY")
# A live system, unlike the ISO install: apt-daily, unattended-upgrades or
# packagekit may hold the dpkg lock for minutes after boot, so wait for it;
# and never let a dependency resolution remove an installed package.
printf 'DPkg::Lock::Timeout "600";\nAPT::Get::Remove "false";\n' > "$dest/apt.conf"
step_env+=(APT_CONFIG="$dest/apt.conf")

if [ "$create" = 1 ]; then
    say "== 30-user.sh (account/, creating $user)"
    "${step_env[@]}" /bin/bash "$dest/account/30-user.sh" || fail "30-user.sh failed (exit $?)"
else
    added=""
    for g in dialout audio video render input plugdev i2c gpio debug trusty crypto weston-launch; do
        getent group "$g" > /dev/null || continue
        id -nG "$user" | tr ' ' '\n' | grep -qx "$g" && continue
        usermod -a -G "$g" "$user"
        added="$added $g"
    done
    say "account: $user (existing); groups added:${added:- none} (sudo and adm are never added to an existing account)"
fi

n=0
for script in "$dest"/install.d/[0-9][0-9]-*.sh; do
    step="$(basename "$script")"
    n=$((n + 1))
    say "== $step"
    "${step_env[@]}" /bin/bash "$script" || {
        rc=$?
        say "ERROR: $step failed (exit $rc); fix the cause and run install.sh again (the steps before it stay done)" >&2
        exit "$rc"
    }
done
[ "$n" -gt 0 ] || fail "no install.d steps in $dest"

# --- 4. the record, udev and the service -----------------------------------------------
tmp=/etc/.brainsyncos-release.tmp
{
    grep -Ev '^(INSTALLED|INSTALL_METHOD|BRAINSYNC_USER)=' "$dest/release"
    printf 'INSTALLED=%s\nINSTALL_METHOD=bundle\nBRAINSYNC_USER=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$user"
} > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" /etc/brainsyncos-release
say "recorded in /etc/brainsyncos-release"

unit=brainsyncos-llama.service
state="$(systemctl is-system-running 2>/dev/null || true)"
if [ -d /run/systemd/system ] && [ -n "$state" ] && [ "$state" != offline ]; then
    systemctl daemon-reload
    udevadm control --reload 2>/dev/null || true
    udevadm trigger --subsystem-match=tty 2>/dev/null || true   # a plugged-in board gets /dev/cerelog now
    if [ "$start" = 0 ]; then
        say "$unit: enabled; --no-start, so it starts at the next boot"
    elif [ -s /etc/brainsyncos/llama.d/model.conf ]; then
        if systemctl restart "$unit"; then
            say "$unit: $(systemctl is-active "$unit" || true) (the model loads in the background; journalctl -u $unit)"
        else
            say "WARNING: $unit did not start: journalctl -u $unit"
        fi
    else
        say "$unit: enabled; no model installed, so it stays off"
    fi
else
    say "services: systemd is not running here; $unit starts at the next boot"
fi
say "BrainSyncOS layer installed ($n steps); the apps' account is $user; log: $log"
