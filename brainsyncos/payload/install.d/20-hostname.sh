#!/bin/bash
# 20-hostname.sh - name the device HOSTNAME_PREFIX-<serial suffix>.
#
# Runs inside the installed system (hook.sh, curtin in-target). The suffix is
# the last 6 characters of the board serial number (device.env, read by
# hook.sh from the device tree), lowercased; without a serial the hostname is
# just HOSTNAME_PREFIX. Sets /etc/hostname and the 127.0.1.1 line in
# /etc/hosts, and tells cloud-init not to change it on first boot. avahi
# (installed by NVIDIA's package list) then announces NAME.local.

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck disable=SC1091
. "$d/settings.env"
# shellcheck disable=SC1091
. "$d/device.env"

suffix="$(printf '%s' "${BSOS_SERIAL:-}" | tr 'A-Z' 'a-z' | tail -c 6)"
name="${HOSTNAME_PREFIX:?}${suffix:+-$suffix}"

printf '%s\n' "$name" > /etc/hostname
touch /etc/hosts
sed -i '/^127\.0\.1\.1[[:space:]]/d' /etc/hosts
printf '127.0.1.1\t%s\n' "$name" >> /etc/hosts

if [ -d /etc/cloud/cloud.cfg.d ]; then
    printf '# BrainSyncOS sets the hostname at install time (20-hostname.sh).\npreserve_hostname: true\n' \
        > /etc/cloud/cloud.cfg.d/99-brainsyncos.cfg
fi
echo "hostname: $name"
