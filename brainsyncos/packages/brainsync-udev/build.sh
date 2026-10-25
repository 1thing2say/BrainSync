#!/usr/bin/env bash
# build.sh - brainsync-udev: udev rules for the Cerelog X8 EEG board.
#
# Run by scripts/build-packages.sh (builder only; see its header for the
# BSOS_* variables). The package installs 70-brainsync-cerelog.rules (this
# folder) to /usr/lib/udev/rules.d/: tty devices of USB vendor 1a86 (V1
# boards, CH340) and 303a (V2 boards, ESP32-S3) get group dialout, mode 0660,
# the symlink /dev/cerelog, and ID_MM_DEVICE_IGNORE=1 for ModemManager.
# The rules are checked with `udevadm verify` first. The postinst reloads
# udev and replays tty add events, but only on a running system: not in a
# chroot (the installer's curtin in-target) and not without udevd.

BSOS_SCRIPT=brainsync-udev
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
load_packages_config
require_cmd udevadm
rules=70-brainsync-cerelog.rules

udevadm verify --resolve-names=never "$rules" > /dev/null || die "udevadm verify rejects $rules"
s="$(scratch_dir udev-stage)"
trap 'force_rm_tree "$s"' EXIT
mkdir -p "$s/DEBIAN" "$s/usr/lib/udev/rules.d" "$s/usr/share/doc/brainsync-udev"
install -m 644 "$rules" "$s/usr/lib/udev/rules.d/$rules"
cat > "$s/usr/share/doc/brainsync-udev/README" <<DOC
/usr/lib/udev/rules.d/$rules: the Cerelog X8 EEG board's serial port
(V1: CH340, vendor 1a86, /dev/ttyUSB*; V2: ESP32-S3, vendor 303a,
/dev/ttyACM*) for group dialout, as /dev/cerelog too, and ignored by
ModemManager. BrainSync's port lookup (brainsync/board.py) and the BrainFlow
fork find the ttyUSB/ttyACM name itself; /dev/cerelog is for people.
On a Jetson, V1 boards also need brainsyncos-ch341-tegra (the driver).
DOC
chmod 644 "$s/usr/share/doc/brainsync-udev/README"
cat > "$s/DEBIAN/postinst" <<'SH'
#!/bin/sh
# brainsync-udev: apply the rules to a board that is already plugged in.
set -e
if [ "$1" = configure ] && [ -d /run/udev ] && command -v udevadm > /dev/null 2>&1 \
    && ! { command -v systemd-detect-virt > /dev/null 2>&1 && systemd-detect-virt -q --chroot; }; then
    udevadm control --reload-rules || true
    udevadm trigger --action=add --subsystem-match=tty || true
fi
SH
chmod 755 "$s/DEBIAN/postinst"
cat > "$s/DEBIAN/control" <<CTL
Package: brainsync-udev
Version: ${BSOS_PKG_VERSION:?}
Architecture: all
Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>
Section: misc
Priority: optional
Multi-Arch: foreign
Description: udev rules for the Cerelog X8 EEG board
 Gives the board's USB serial port (CH340 vendor 1a86, ESP32-S3 vendor
 303a) to group dialout as /dev/cerelog, and keeps ModemManager off it.
CTL
build_deb "$s" "${BSOS_PKG_OUT:?}" > /dev/null
