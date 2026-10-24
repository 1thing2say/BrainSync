#!/usr/bin/env bash
# build.sh - brainsyncos-archive-keyring: the public key of the BrainSyncOS package repository.
#
# Run by scripts/build-packages.sh (builder only; see its header for the
# BSOS_* variables). The package holds
# /usr/share/keyrings/brainsyncos-archive-keyring.gpg, the public part of
# BSOS_REPO_KEY in binary form as apt's Signed-By takes it, and a README
# naming its fingerprint.
# payload/install.d/60-apt-repo.sh installs it from the ISO's repository and
# checks that it verifies that repository, so the device keeps the key its
# packages were signed with for later updates.

BSOS_SCRIPT=brainsyncos-archive-keyring
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
load_packages_config

s="$(scratch_dir keyring-stage)"
trap 'force_rm_tree "$s"' EXIT
mkdir -p "$s/DEBIAN" "$s/usr/share/keyrings" "$s/usr/share/doc/brainsyncos-archive-keyring"
fpr="$(public_keyring "${BSOS_REPO_KEY:?}" "$s/usr/share/keyrings/brainsyncos-archive-keyring.gpg")"
chmod 644 "$s/usr/share/keyrings/brainsyncos-archive-keyring.gpg"
cat > "$s/usr/share/doc/brainsyncos-archive-keyring/README" <<EOF
The OpenPGP key that signs the BrainSyncOS package repository:
$fpr
Made by scripts/make-repo-key.sh; see brainsyncos/README.md.
EOF
chmod 644 "$s/usr/share/doc/brainsyncos-archive-keyring/README"
cat > "$s/DEBIAN/control" <<EOF
Package: brainsyncos-archive-keyring
Version: ${BSOS_PKG_VERSION:?}
Architecture: all
Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>
Section: misc
Priority: optional
Multi-Arch: foreign
Description: BrainSyncOS package repository key
 The public OpenPGP key ($fpr) that the BrainSyncOS package
 repository is signed with, for apt (Signed-By).
EOF
build_deb "$s" "${BSOS_PKG_OUT:?}" > /dev/null
