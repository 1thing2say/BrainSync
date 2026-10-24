#!/bin/bash
# 60-apt-repo.sh - check the ISO's package repository and install its keyring.
#
# Runs inside the installed system (hook.sh, curtin in-target), with the
# ISO's signed package repository mounted read-only at $BSOS_REPO_DIR. The
# first step of the BrainSync payload, on both editions: apt must accept the
# repository's signature against the build's key ($BSOS_INSTALL_DIR/repo-key.gpg,
# lib/apt-repo.sh), and brainsyncos-archive-keyring is installed from it. The
# installed keyring (/usr/share/keyrings/brainsyncos-archive-keyring.gpg)
# must verify the repository by itself: it is what the device keeps for later
# updates. No apt source is left behind; the repository exists only on the
# install medium, and later steps install from it the same way.

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck source=payload/lib/apt-repo.sh
. "$d/lib/apt-repo.sh"
bsos_repo_open
bsos_apt install brainsyncos-archive-keyring

k=/usr/share/keyrings/brainsyncos-archive-keyring.gpg
[ -s "$k" ] || { echo "60-apt-repo.sh: brainsyncos-archive-keyring did not install $k" >&2; exit 1; }
if ! gpgv --keyring "$k" "${BSOS_REPO_DIR:-/tmp/brainsyncos-repo}/dists/$REPO_SUITE/InRelease" > /dev/null 2>&1; then
    echo "60-apt-repo.sh: $k does not verify the repository it came from" >&2
    exit 1
fi
echo "60-apt-repo.sh: repository verified; $(dpkg-query -W -f '${Package} ${Version}' brainsyncos-archive-keyring) installed"
