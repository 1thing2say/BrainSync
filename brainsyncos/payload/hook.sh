#!/bin/bash
# hook.sh - BrainSyncOS install hook, run by the installer of either edition.
#
# Jetson edition: scripts/patch-autoinstall.py adds
# `/bin/bash /cdrom/brainsyncos/hook.sh` to NVIDIA's autoinstall as a
# subiquity late-command, after all of NVIDIA's own steps. VM edition: the
# same line is a late-command of vm/autoinstall.yaml (stock Ubuntu installer).
# It runs in the live installer: this payload is on the ISO at
# /cdrom/brainsyncos and the installed system is mounted at
# $TARGET_MOUNT_POINT (default /target). The edition comes from the
# release file (EDITION=orin-nano or vm-arm64).
#
# It copies the payload to /opt/brainsyncos/installer on the target, records
# the machine (model, serial number, edition) in device.env there, mounts
# the ISO's signed package repository (/brainsyncos-repo, next to the
# payload) read-only at /tmp/brainsyncos-repo inside the target, then runs
# every install.d/NN-*.sh inside the target (curtin in-target), in order,
# with BSOS_INSTALL_DIR and BSOS_REPO_DIR set. The repository is unmounted on
# exit, whatever happened, so the installer can unmount the target. Any
# failure fails the installation: a half-installed BrainSyncOS is worse than
# an installer error on screen.
# Log: /var/log/brainsyncos/install.log, in the live system and on the target.
#
# Test overrides: BSOS_HOOK_SRC (payload dir), BSOS_REPO_SRC (repository
# dir, default <payload>/../brainsyncos-repo), BSOS_HOOK_LOG (live log file),
# BSOS_DT_DIR (device tree, default /sys/firmware/devicetree/base),
# BSOS_DMI_DIR (default /sys/class/dmi/id), BSOS_MACHINE_ID_FILE (default
# /etc/machine-id).

set -euo pipefail

src="${BSOS_HOOK_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)}"
target="${TARGET_MOUNT_POINT:-/target}"
export TARGET_MOUNT_POINT="$target"
dest=/opt/brainsyncos/installer
repo_src="${BSOS_REPO_SRC:-$(dirname "$src")/brainsyncos-repo}"
repo_mnt=/tmp/brainsyncos-repo
live_log="${BSOS_HOOK_LOG:-/var/log/brainsyncos/install.log}"

mkdir -p "$(dirname "$live_log")"
exec > >(tee -a "$live_log") 2>&1
tee_pid=$!

# Copy the log to the target only after tee has written all of it. Wait at most
# 10 s: a daemon started by a step could keep the pipe open and hang the install.
save_log() {
    local rc=$?
    if mountpoint -q "$target$repo_mnt" 2>/dev/null; then
        umount "$target$repo_mnt" 2>/dev/null || umount -l "$target$repo_mnt" || true
    fi
    rmdir "$target$repo_mnt" 2>/dev/null || true
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

edition="$(sed -n 's/^EDITION=//p' "$src/release" | head -1)"
edition="${edition:-orin-nano}"

# Machine identity. A Jetson has a device tree with model and serial-number
# (used as before, whatever else exists). A VM (UEFI + ACPI) has none, or one
# without a model: then SMBIOS/DMI gives the model and the first usable of
# product_serial, product_uuid (UTM passes the VM's UUID) and, last, the live
# system's machine-id (random per install).
dt="${BSOS_DT_DIR:-/sys/firmware/devicetree/base}"
dmi="${BSOS_DMI_DIR:-/sys/class/dmi/id}"
dt_model="$(tr -d '\0' 2>/dev/null < "$dt/model" || true)"
if [ -n "$dt_model" ]; then
    model="$dt_model"
    serial="$(tr -d '\0' 2>/dev/null < "$dt/serial-number" | tr -cd 'A-Za-z0-9' || true)"
    id_source=devicetree
else
    dt_model=unknown
    # dmi_value FILE: the DMI string, NULs dropped and blanks trimmed ("" if unreadable).
    dmi_value() { tr -d '\0' 2>/dev/null < "$dmi/$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' || true; }
    model="$(printf '%s %s' "$(dmi_value sys_vendor)" "$(dmi_value product_name)" | sed 's/^ *//; s/ *$//')"
    model="${model:-unknown}"
    serial=""
    id_source=none
    for f in product_serial product_uuid; do
        s="$(dmi_value "$f" | tr -cd 'A-Za-z0-9')"
        # Placeholders firmware writes when there is no real value.
        case "$(printf '%s' "$s" | tr 'a-z' 'A-Z')" in
            ''|NONE|NOTSPECIFIED|NOTAPPLICABLE|TOBEFILLEDBYOEM|DEFAULTSTRING|SYSTEMSERIALNUMBER|0123456789) continue ;;
        esac
        printf '%s' "$s" | grep -Eqv '^(0+|[Ff]+)$' || continue
        serial="$s"
        id_source="dmi $f"
        break
    done
    if [ -z "$serial" ]; then
        serial="$(tr -cd 'A-Za-z0-9' 2>/dev/null < "${BSOS_MACHINE_ID_FILE:-/etc/machine-id}" || true)"
        [ -z "$serial" ] || id_source=machine-id
    fi
fi
case "$edition:$model" in
    orin-nano:"NVIDIA Jetson Orin Nano"*) echo "hook.sh: board: $model, serial ${serial:-unknown}" ;;
    orin-nano:*) echo "hook.sh: WARNING: BrainSyncOS is built for Jetson Orin Nano; this board is: $model" ;;
    *) echo "hook.sh: $edition edition on: $model, serial ${serial:-unknown} (from $id_source)" ;;
esac

rm -rf "${target:?}$dest"
mkdir -p "$target$dest"
cp -R "$src/." "$target$dest/"
printf 'BSOS_DT_MODEL=%q\nBSOS_SERIAL=%q\nBSOS_EDITION=%q\nBSOS_MODEL=%q\nBSOS_ID_SOURCE=%q\n' \
    "$dt_model" "$serial" "$edition" "$model" "$id_source" > "$target$dest/device.env"
chown -R root:root "$target$dest"
chmod -R u+rwX,go+rX,go-w "$target$dest"
# Login credentials: readable by root only on the installed system.
[ ! -d "$target$dest/config" ] || chmod -R go-rwx "$target$dest/config"

# The package repository, read-only for the steps (payload lib/apt-repo.sh).
[ -d "$repo_src/dists" ] || { echo "hook.sh: no package repository at $repo_src" >&2; exit 1; }
if mountpoint -q "$target$repo_mnt" 2>/dev/null; then
    umount "$target$repo_mnt"
fi
mkdir -p "$target$repo_mnt"
mount --bind "$repo_src" "$target$repo_mnt"
mount -o remount,bind,ro "$target$repo_mnt"
echo "hook.sh: package repository $repo_src mounted at $repo_mnt (read-only)"

n=0
for script in "$target$dest"/install.d/[0-9][0-9]-*.sh; do
    [ -e "$script" ] || continue
    step="$(basename "$script")"
    n=$((n + 1))
    echo "hook.sh: == $step"
    curtin in-target --target="$target" -- /usr/bin/env BSOS_INSTALL_DIR="$dest" BSOS_REPO_DIR="$repo_mnt" \
        /bin/bash "$dest/install.d/$step"
done
[ "$n" -gt 0 ] || { echo "hook.sh: no install.d steps in $src" >&2; exit 1; }
echo "hook.sh: BrainSyncOS layer installed ($n steps)"
