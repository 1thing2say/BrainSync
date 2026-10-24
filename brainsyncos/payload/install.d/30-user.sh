#!/bin/bash
# 30-user.sh - create the BrainSyncOS appliance account (BRAINSYNC_USER).
#
# Runs inside the installed system (hook.sh, curtin in-target), replacing the
# account NVIDIA's OOBE would have asked for. Logins come from the build:
#   config/authorized_keys     SSH public keys (optional)
#   config/password.hash       crypt(3) hash for console login (optional)
# build-iso.sh requires at least one. Without a password the account is
# locked for password logins and gets passwordless sudo (otherwise sudo would
# be unusable); with one, sudo asks for it. Groups: NVIDIA's hardware groups
# (as for its own default users) plus dialout for the EEG board, where they
# exist. NVIDIA's fallback users nvidia/ubuntu (known passwords, only created
# with the nooemconfig boot flag) are removed if present.

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck disable=SC1091
. "$d/settings.env"
user="${BRAINSYNC_USER:?}"
keys="$d/config/authorized_keys"
hash_file="$d/config/password.hash"
[ -s "$keys" ] || [ -s "$hash_file" ] || { echo "30-user.sh: no SSH keys and no password in the build" >&2; exit 1; }

for vendor_user in nvidia ubuntu; do
    if id "$vendor_user" > /dev/null 2>&1; then
        echo "30-user.sh: removing NVIDIA default user $vendor_user (known password)"
        userdel -r "$vendor_user" 2> /dev/null || userdel "$vendor_user"
    fi
done

groups=""
for g in adm dialout sudo audio video render input plugdev i2c gpio debug trusty crypto weston-launch; do
    if getent group "$g" > /dev/null; then groups="$groups${groups:+,}$g"; fi
done

if id "$user" > /dev/null 2>&1; then
    usermod -a -G "$groups" "$user"
else
    useradd -m -s /bin/bash -c "BrainSync" -G "$groups" "$user"
fi
home="$(getent passwd "$user" | cut -d: -f6)"

if [ -s "$hash_file" ]; then
    usermod -p "$(head -1 "$hash_file")" "$user"
    rm -f /etc/sudoers.d/90-brainsyncos
    echo "30-user.sh: console password set; sudo asks for it"
else
    usermod -p '!' "$user"
    printf '# BrainSyncOS: %s has no password (SSH keys only), so sudo cannot ask for one.\n%s ALL=(ALL) NOPASSWD:ALL\n' \
        "$user" "$user" > /etc/sudoers.d/.90-brainsyncos.tmp
    chmod 440 /etc/sudoers.d/.90-brainsyncos.tmp
    visudo -cf /etc/sudoers.d/.90-brainsyncos.tmp
    mv -f /etc/sudoers.d/.90-brainsyncos.tmp /etc/sudoers.d/90-brainsyncos
    echo "30-user.sh: password locked; passwordless sudo"
fi

install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
if [ -s "$keys" ]; then
    install -m 600 -o "$user" -g "$user" "$keys" "$home/.ssh/authorized_keys"
    echo "30-user.sh: $(grep -c '^[^#]' "$keys") SSH key(s) installed"
else
    rm -f "$home/.ssh/authorized_keys"
fi

# GNOME's first-login wizard would otherwise greet the account on the desktop.
install -d -m 755 -o "$user" -g "$user" "$home/.config"
printf 'yes' > "$home/.config/gnome-initial-setup-done"
chown "$user:$user" "$home/.config/gnome-initial-setup-done"

echo "30-user.sh: $user ($(id "$user"))"
