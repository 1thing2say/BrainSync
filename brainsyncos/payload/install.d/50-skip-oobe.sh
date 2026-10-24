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

set -euo pipefail

ln -sfn /lib/systemd/system/graphical.target /etc/systemd/system/default.target
rm -f /etc/systemd/system/nv-oobe.target
rm -rf /etc/systemd/system/nv-oobe.target.wants
rm -f /etc/nv/nvautoconfig
echo "50-skip-oobe.sh: default target $(readlink /etc/systemd/system/default.target)"
