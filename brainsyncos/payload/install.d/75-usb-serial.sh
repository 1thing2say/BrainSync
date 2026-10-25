#!/bin/bash
# 75-usb-serial.sh - the Cerelog X8 EEG board's USB serial port.
#
# Runs inside the installed system (hook.sh, curtin in-target), after
# 60-apt-repo.sh, with the ISO's package repository at $BSOS_REPO_DIR.
# Both editions get brainsync-udev (dialout, /dev/cerelog, no ModemManager).
# The Jetson edition also gets brainsyncos-ch341-tegra, the CH340 driver
# NVIDIA's kernel lacks (V1 boards); it depends on the exact nvidia-l4t-kernel
# it was built for, which NVIDIA's late-commands installed before this hook.
# The VM edition skips it: Ubuntu's kernel has ch341 in linux-modules-extra.
# The step then checks that the rules are in place and, on the Jetson, that
# modprobe finds the new module for that kernel.

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck source=payload/lib/apt-repo.sh
. "$d/lib/apt-repo.sh"
# shellcheck disable=SC1091  # written by hook.sh
. "$d/device.env"
edition="${BSOS_EDITION:-}"
# Every Jetson edition (orin-nano, ...) runs NVIDIA's L4T kernel; vm-* runs Ubuntu's.
pkgs=(brainsync-udev)
jetson=0
case "$edition" in
    '') echo "75-usb-serial.sh: no BSOS_EDITION in $d/device.env" >&2; exit 1 ;;
    vm-*) echo "75-usb-serial.sh: $edition edition: no brainsyncos-ch341-tegra (Ubuntu's kernel has ch341)" ;;
    *) pkgs+=(brainsyncos-ch341-tegra); jetson=1 ;;
esac
bsos_repo_open
bsos_apt install "${pkgs[@]}"

rules=/usr/lib/udev/rules.d/70-brainsync-cerelog.rules
[ -s "$rules" ] || { echo "75-usb-serial.sh: brainsync-udev did not install $rules" >&2; exit 1; }
if [ "$jetson" = 1 ]; then
    kver="$(dpkg -L brainsyncos-ch341-tegra | sed -n 's|^/lib/modules/\([^/]*\)/updates/ch341\.ko$|\1|p')"
    [ -n "$kver" ] || { echo "75-usb-serial.sh: brainsyncos-ch341-tegra has no ch341.ko" >&2; exit 1; }
    found="$(modprobe -S "$kver" --show-depends ch341 2>&1)" \
        || { echo "75-usb-serial.sh: modprobe does not find ch341 for $kver: $found" >&2; exit 1; }
    printf '%s\n' "$found" | grep -q "/lib/modules/$kver/updates/ch341.ko" \
        || { echo "75-usb-serial.sh: modprobe resolves ch341 elsewhere: $found" >&2; exit 1; }
    echo "75-usb-serial.sh: ch341 for $kver: $(printf '%s\n' "$found" | awk '{printf "%s%s", s, $2; s = " "}')"
fi
echo "75-usb-serial.sh: installed $(dpkg-query -W -f '${Package} ${Version}\n' "${pkgs[@]}" | awk '{printf "%s%s", s, $0; s = ", "}')"
