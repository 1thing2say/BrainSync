#!/bin/bash
# 70-brainsync.sh - install the BrainSync apps and their Python runtime.
#
# Runs inside the installed system (hook.sh, curtin in-target), on both
# editions, after 60-apt-repo.sh: installs brainsync-runtime (the venv at
# /opt/brainsyncos/venv) and brainsync (the apps, /etc/brainsync,
# /usr/share/brainsync, the commands in /usr/bin) from the ISO's signed
# package repository, the same way 60-apt-repo.sh does (lib/apt-repo.sh: that
# repository alone, offline). Their Depends (python3.12, libc6, ...) are
# already on both targets. The GL and X libraries the display apps load are
# only Recommends: NVIDIA's install puts them on the Jetson; the step lists
# any that are missing (the VM without a desktop) and goes on. Then it checks
# that the venv's Python imports the apps and that brainsync-voice runs.
# API keys are not installed: they go in /etc/brainsync/secrets.env later.

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck source=payload/lib/apt-repo.sh
. "$d/lib/apt-repo.sh"
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
echo "70-brainsync.sh: $(dpkg-query -W -f '${Package} ${Version}' brainsync), $(dpkg-query -W -f '${Package} ${Version}' brainsync-runtime) installed; $(dpkg -L brainsync | grep -c '^/usr/bin/.') commands in /usr/bin"
