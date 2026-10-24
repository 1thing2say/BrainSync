#!/bin/bash
# 40-ssh.sh - SSH access for the appliance account only, by key only.
#
# Runs inside the installed system (hook.sh, curtin in-target). openssh-server
# comes from NVIDIA's package list (Jetson) or the autoinstall's ssh section
# (VM edition); this adds a drop-in that turns off
# password and root logins and allows only BRAINSYNC_USER, enables Ubuntu's
# socket-activated ssh, and checks the resulting config with sshd -t.

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck disable=SC1091
. "$d/settings.env"
[ -x /usr/sbin/sshd ] || { echo "40-ssh.sh: openssh-server is not installed" >&2; exit 1; }

install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-brainsyncos.conf <<EOF
# BrainSyncOS (40-ssh.sh): key-only SSH for ${BRAINSYNC_USER:?}.
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowUsers $BRAINSYNC_USER
EOF
chmod 644 /etc/ssh/sshd_config.d/10-brainsyncos.conf

if [ -f /lib/systemd/system/ssh.socket ] || [ -f /usr/lib/systemd/system/ssh.socket ]; then
    systemctl enable ssh.socket
else
    systemctl enable ssh.service
fi

if ls /etc/ssh/ssh_host_*_key > /dev/null 2>&1; then
    mkdir -p /run/sshd
    /usr/sbin/sshd -t
    echo "40-ssh.sh: sshd config OK"
else
    echo "40-ssh.sh: WARNING: no host keys yet; sshd -t skipped"
fi
