#!/bin/bash
# 50-skip-oobe.sh - boot straight to the system instead of NVIDIA's OOBE.
#
# Runs inside the installed system (hook.sh, curtin in-target), after
# 30-user.sh created the account. NVIDIA's autoinstall makes nv-oobe.target
# the default target; on first boot nv-oobe.sh switches to graphical.target
# and GNOME Initial Setup creates a user and shows NVIDIA's software license.
# This does the switch at install time and removes the OOBE target links and
# the /etc/nv/nvautoconfig marker. nvpmodel, nvpower and nvfancontrol stay
# enabled: their packages also link them into multi-user.target.wants.
# Only when the OOBE is actually configured: a system whose default target is
# anything else (the VM edition, a Jetson installed with nooemconfig) keeps it.

set -euo pipefail

link=/etc/systemd/system/default.target
current="$(readlink "$link" 2>/dev/null || true)"
case "$current" in
    nv-oobe.target|*/nv-oobe.target)
        ln -sfn /lib/systemd/system/graphical.target "$link"
        echo "50-skip-oobe.sh: default target was $current (NVIDIA OOBE)"
        ;;
    *) echo "50-skip-oobe.sh: no NVIDIA OOBE default target; default target left as ${current:-the distribution default}" ;;
esac
rm -f /etc/systemd/system/nv-oobe.target
rm -rf /etc/systemd/system/nv-oobe.target.wants
rm -f /etc/nv/nvautoconfig
echo "50-skip-oobe.sh: default target $(readlink "$link" 2>/dev/null || echo '(distribution default)')"
