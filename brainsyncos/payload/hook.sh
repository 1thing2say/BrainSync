#!/bin/bash
# hook.sh - BrainSyncOS install hook, run by the Jetson ISO installer.
#
# scripts/patch-autoinstall.py adds `/bin/bash /cdrom/brainsyncos/hook.sh` to
# NVIDIA's autoinstall as a subiquity late-command, after all of NVIDIA's own
# steps. It runs in the live installer: this payload is on the ISO at
# /cdrom/brainsyncos and the installed system is mounted at
# $TARGET_MOUNT_POINT (default /target).
#
# It copies the payload to /opt/brainsyncos/installer on the target, records
# the board (device tree model and serial number) in device.env there, then
# runs every install.d/NN-*.sh inside the target (curtin in-target), in order,
# with BSOS_INSTALL_DIR set. Any failure fails the installation: a
# half-installed BrainSyncOS is worse than an installer error on screen.
# Log: /var/log/brainsyncos/install.log, in the live system and on the target.
#
# Test overrides: BSOS_HOOK_SRC (payload dir), BSOS_HOOK_LOG (live log file),
# BSOS_DT_DIR (device tree, default /sys/firmware/devicetree/base).

set -euo pipefail

src="${BSOS_HOOK_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)}"
target="${TARGET_MOUNT_POINT:-/target}"
export TARGET_MOUNT_POINT="$target"
dest=/opt/brainsyncos/installer
live_log="${BSOS_HOOK_LOG:-/var/log/brainsyncos/install.log}"

mkdir -p "$(dirname "$live_log")"
exec > >(tee -a "$live_log") 2>&1
tee_pid=$!

# Copy the log to the target only after tee has written all of it. Wait at most
# 10 s: a daemon started by a step could keep the pipe open and hang the install.
save_log() {
    local rc=$?
    echo "hook.sh: exit $rc"
    exec >&- 2>&-
    for _ in $(seq 1 50); do
        kill -0 "$tee_pid" 2>/dev/null || break
        sleep 0.2
    done
    kill "$tee_pid" 2>/dev/null || true
    if [ -d "$target/var/log" ]; then
        mkdir -p "$target/var/log/brainsyncos"
        cp -f "$live_log" "$target/var/log/brainsyncos/install.log" || true
    fi
    exit "$rc"
}
trap save_log EXIT

echo "hook.sh: BrainSyncOS install hook, payload $src, target $target"
[ -f "$target/etc/os-release" ] || { echo "hook.sh: no installed system at $target" >&2; exit 1; }
[ -s "$src/release" ] || { echo "hook.sh: missing $src/release" >&2; exit 1; }
cat "$src/release"

dt="${BSOS_DT_DIR:-/sys/firmware/devicetree/base}"
model="$(tr -d '\0' < "$dt/model" 2>/dev/null || echo unknown)"
serial="$(tr -d '\0' < "$dt/serial-number" 2>/dev/null | tr -cd 'A-Za-z0-9' || true)"
case "$model" in
    "NVIDIA Jetson Orin Nano"*) echo "hook.sh: board: $model, serial ${serial:-unknown}" ;;
    *) echo "hook.sh: WARNING: BrainSyncOS is built for Jetson Orin Nano; this board is: $model" ;;
esac

rm -rf "${target:?}$dest"
mkdir -p "$target$dest"
cp -R "$src/." "$target$dest/"
printf 'BSOS_DT_MODEL=%q\nBSOS_SERIAL=%q\n' "$model" "$serial" > "$target$dest/device.env"
chown -R root:root "$target$dest"
chmod -R u+rwX,go+rX,go-w "$target$dest"
# Login credentials: readable by root only on the installed system.
[ ! -d "$target$dest/config" ] || chmod -R go-rwx "$target$dest/config"

n=0
for script in "$target$dest"/install.d/[0-9][0-9]-*.sh; do
    [ -e "$script" ] || continue
    step="$(basename "$script")"
    n=$((n + 1))
    echo "hook.sh: == $step"
    curtin in-target --target="$target" -- /usr/bin/env BSOS_INSTALL_DIR="$dest" /bin/bash "$dest/install.d/$step"
done
[ "$n" -gt 0 ] || { echo "hook.sh: no install.d steps in $src" >&2; exit 1; }
echo "hook.sh: BrainSyncOS layer installed ($n steps)"
