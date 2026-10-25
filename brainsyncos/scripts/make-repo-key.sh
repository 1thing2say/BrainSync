#!/usr/bin/env bash
# make-repo-key.sh - create the signing key of the BrainSyncOS package repository (builder only).
#
# Usage: scripts/builder.sh run scripts/make-repo-key.sh [--out DIR] [--force]
#
# Writes DIR/repo-signing-key.asc (the secret key, mode 600, no passphrase)
# and DIR/repo-signing-key.pub.asc (its public key), DIR defaulting to
# config/. Both are gitignored: the key is yours, like config/authorized_keys.
# It is an Ed25519 signing-only key, "BrainSyncOS package repository
# <PACKAGE_MAINTAINER_EMAIL>", expiring after REPO_KEY_EXPIRE
# (config/packages.env).
#
# scripts/build-repo.sh signs every repository with it, and an installed
# device trusts only that key for BrainSyncOS packages, so back the secret key
# up: devices installed from its ISOs can only take updates signed by it.
# Refuses to replace an existing key unless --force. The key is generated in
# a GnuPG home on /scratch (tmpfs), which is deleted afterwards.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_packages_config

out="$BRAINSYNCOS_ROOT/config"
force=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --out) out="${2:?--out needs a directory}"; shift 2 ;;
        --force) force=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_container
require_cmd gpg gpgconf

secret="$out/repo-signing-key.asc"
public="$out/repo-signing-key.pub.asc"
if [ -e "$secret" ] || [ -e "$public" ]; then
    [ "$force" = 1 ] || die "a repository key already exists in ${out#$BRAINSYNCOS_ROOT/} (ISOs built with it trust only it; --force replaces it)"
    warn "replacing the repository key in ${out#$BRAINSYNCOS_ROOT/}"
fi
mkdir -p "$out"

home="$(scratch_dir make-repo-key)"
trap 'gpgconf_kill "$home"; rm -rf "$home"' EXIT
uid="BrainSyncOS package repository <$PACKAGE_MAINTAINER_EMAIL>"
GNUPGHOME="$home" gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
    --quick-generate-key "$uid" ed25519 sign "$REPO_KEY_EXPIRE" 2> "$home/gen.log" \
    || { cat "$home/gen.log" >&2; die "gpg could not create the key"; }
fpr="$(GNUPGHOME="$home" gpg --batch --quiet --with-colons --list-secret-keys 2> /dev/null | awk -F: '/^fpr:/ {print $10; exit}')"
[ -n "$fpr" ] || die "the new key has no fingerprint"

# Write both files next to their final names, then move them into place.
(umask 077; GNUPGHOME="$home" gpg --batch --quiet --armor --pinentry-mode loopback --passphrase '' \
    --export-secret-keys "$fpr" > "$secret.tmp" 2> /dev/null)
GNUPGHOME="$home" gpg --batch --quiet --armor --export "$fpr" > "$public.tmp" 2> /dev/null
[ -s "$secret.tmp" ] && [ -s "$public.tmp" ] || die "could not export the new key"
chmod 600 "$secret.tmp"
chmod 644 "$public.tmp"
# Some filesystems ignore chmod (WSL's /mnt/c, other Windows drives): the secret
# key would stay readable by everyone, so it is never left there.
mode="$(stat -c %a "$secret.tmp")"
[ "$mode" = 600 ] || {
    rm -f "$secret.tmp" "$public.tmp"
    die "${out#$BRAINSYNCOS_ROOT/} ignores chmod (mode $mode after chmod 600), so the secret key would be readable by everyone; build from a checkout on a Linux filesystem (WSL: under ~, not /mnt/c)"
}
mv -f "$secret.tmp" "$secret"
mv -f "$public.tmp" "$public"

[ "$(key_fingerprint "$secret")" = "$fpr" ] && [ "$(key_fingerprint "$public")" = "$fpr" ] \
    || die "the written key files do not hold key $fpr"
log "repository signing key $fpr ($uid, expires: $REPO_KEY_EXPIRE)"
log "secret key: ${secret#$BRAINSYNCOS_ROOT/} (back it up; never commit it)"
log "public key: ${public#$BRAINSYNCOS_ROOT/}"
