#!/bin/bash
# 70-brainsync.sh - install the BrainSync apps, their Python runtime, the
# offline voice and the API key file.
#
# Runs inside the installed system (hook.sh, curtin in-target), on both
# editions, after 30-user.sh and 60-apt-repo.sh: installs brainsync-runtime
# (the venv at /opt/brainsyncos/venv) and brainsync (the apps, /etc/brainsync,
# /usr/share/brainsync, the commands in /usr/bin) from the ISO's signed
# package repository, the same way 60-apt-repo.sh does (lib/apt-repo.sh: that
# repository alone, offline). Their Depends (python3.12, libc6, ...) are
# already on both targets. The GL and X libraries the display apps load are
# only Recommends: NVIDIA's install puts them on the Jetson; the step lists
# any that are missing (the VM without a desktop) and goes on. Then it checks
# that the venv's Python imports the apps and that brainsync-voice runs.
#
# The offline voice: brainsync/voice.py speaks with espeak-ng when Cartesia
# cannot (no key, no network). espeak-ng comes from the repository when it
# installs there with what the system already has. That is the Jetson: our
# repository carries the command (packages/espeak-ng), NVIDIA's install its
# libraries. The VM edition, whose install has the network, gets it from
# Ubuntu's archive with the system's own apt sources. If neither works, a
# note: the apps then print their prompts when offline.
#
# API keys are not installed. The step creates /etc/brainsync/secrets.env if
# it is absent: a commented template without values, owner root, group GROUP,
# mode 0640. GROUP is the primary group of the appliance account
# (BRAINSYNC_USER, made by 30-user.sh), which runs the apps. Without that
# account the file is root:root, with a note. An existing file (a re-run,
# keys already added) is never changed. This step owns the file, not the
# brainsync package: the account name is a build setting the package does not
# know; as a conffile the keys would be package data (an upgrade would ask
# about them, a purge would delete them); and the brainsync build refuses any
# .env file, so a key can never end up in a package. Later steps add their
# keys to it (85-llama.sh appends BRAINSYNC_LLAMA_API_KEY).

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck source=payload/lib/apt-repo.sh
. "$d/lib/apt-repo.sh"
# shellcheck disable=SC1091  # from the payload, and written by hook.sh
. "$d/settings.env"
# shellcheck disable=SC1091
. "$d/device.env"
bsos_repo_open
bsos_apt install brainsync-runtime brainsync

fail() { echo "70-brainsync.sh: $*" >&2; exit 1; }
py=/opt/brainsyncos/venv/bin/python
[ -x "$py" ] || fail "brainsync-runtime did not install $py"
"$py" -I -c 'import brainsync, app, numpy, scipy, fastapi, yaml' || fail "the venv cannot import the apps"
/usr/bin/brainsync-voice --help > /dev/null || fail "brainsync-voice does not run"

missing=""
for p in $(dpkg-query -W -f '${Recommends}' brainsync-runtime | tr ',' ' '); do
    dpkg-query -W -f '${Status}' "$p" 2> /dev/null | grep -q 'install ok installed' || missing="$missing $p"
done
[ -z "$missing" ] || echo "70-brainsync.sh: note: the display apps need X and GL libraries this system lacks:$missing"

# The offline voice. apt's messages go to a file: where a failure is
# expected, its E: lines would read like an install error.
out="$(mktemp /tmp/bsos-espeak.XXXXXX)"
vm=0
case "${BSOS_EDITION:-}" in vm-*) vm=1 ;; esac
if dpkg-query -W -f '${Status}' espeak-ng 2> /dev/null | grep -q 'install ok installed'; then
    voice="already installed"
elif bsos_apt -s install espeak-ng > "$out" 2>&1; then
    bsos_apt install espeak-ng
    voice="from the repository"
elif [ "$vm" = 1 ]; then
    if DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 timeout 300 \
            apt-get -q -o Dpkg::Use-Pty=0 --error-on=any update > "$out" 2>&1 \
        && DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 timeout 300 \
            apt-get -y -q -o Dpkg::Use-Pty=0 --no-install-recommends install espeak-ng >> "$out" 2>&1; then
        voice="from Ubuntu's archive"
    else
        voice="not installed: Ubuntu's archive failed ($(sed -n 's/^E: //p' "$out" | tail -1))"
    fi
else
    unmet="$(grep -o 'Depends: [^ ]*' "$out" | cut -d' ' -f2 | tr '\n' ' ' | sed 's/ $//' || true)"
    voice="not installed: ${unmet:+its libraries are missing ($unmet)}"
    [ -n "$unmet" ] || voice="$voice$(sed -n 's/^E: //p' "$out" | head -1)"
fi
rm -f "$out"
if command -v espeak-ng > /dev/null; then
    espeak-ng --version > /dev/null || fail "espeak-ng does not run"
    echo "70-brainsync.sh: offline voice: $(dpkg-query -W -f '${Package} ${Version}' espeak-ng), $voice"
else
    echo "70-brainsync.sh: note: offline voice espeak-ng $voice; offline, the apps print their prompts"
fi

# The API key file, if absent.
secrets=/etc/brainsync/secrets.env
user="${BRAINSYNC_USER:?}"
if [ -e "$secrets" ] || [ -L "$secrets" ]; then
    echo "70-brainsync.sh: $secrets exists ($(stat -c '%U:%G %a' "$secrets")); left as it is"
else
    if ! group="$(id -gn "$user" 2> /dev/null)"; then
        group=root
        echo "70-brainsync.sh: note: no account $user; $secrets is root:root (chgrp it to the apps' account)"
    fi
    tmp="$(mktemp /etc/brainsync/.secrets.env.XXXXXX)"
    cat > "$tmp" <<EOF
# $secrets: API keys for the BrainSync apps.
# Made by the BrainSyncOS install (70-brainsync.sh), which never overwrites it.
# Owner root, group $group, mode 0640: only root and that group can read it.
# One KEY=value per line, no spaces around =; quotes around a value are allowed.
# Lines that start with # are comments. A variable set in the environment wins.
# BCI Voice (brainsync-voice) and the spoken prompts read this file; the
# BrainSync server reads it with: brainsync-server --env-file $secrets
# A key that is not set turns its service off: BCI Voice then uses its fixed
# reply lists, and the prompts are spoken by espeak-ng instead of Cartesia.
#
# Claude, for BCI Voice's reply pages:
#ANTHROPIC_API_KEY=
# Cartesia, the voice of BCI Voice and of the spoken prompts:
#CARTESIA_API_KEY=
# The local llama-server, BCI Voice's first backend (85-llama.sh adds it):
#BRAINSYNC_LLAMA_API_KEY=
EOF
    chown "root:$group" "$tmp"
    chmod 640 "$tmp"
    mv "$tmp" "$secrets"
    echo "70-brainsync.sh: $secrets created (template, no keys), root:$group 640"
fi

echo "70-brainsync.sh: $(dpkg-query -W -f '${Package} ${Version}' brainsync), $(dpkg-query -W -f '${Package} ${Version}' brainsync-runtime) installed; $(dpkg -L brainsync | grep -c '^/usr/bin/.') commands in /usr/bin"
