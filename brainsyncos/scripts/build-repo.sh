#!/usr/bin/env bash
# build-repo.sh - sign an edition's packages into the apt repository for its ISO (builder only).
#
# Usage: scripts/builder.sh run scripts/build-repo.sh --edition jetson|vm
#            [--debs DIR] [--key FILE] [--out DIR]
#
# Input: the .debs in build/debs/EDITION/ (--debs; scripts/build-packages.sh).
# Output: build/repo/EDITION/ (--out), replaced as a whole:
#   pool/COMPONENT/*.deb                          the packages
#   dists/SUITE/COMPONENT/binary-arm64/Packages   index (+ .gz), apt-ftparchive
#   dists/SUITE/Release, InRelease, Release.gpg   signed with --key (default
#                                                 config/repo-signing-key.asc)
#   repo-key.gpg                                  the public key, binary: the
#                                                 ISO builds ship it as the
#                                                 install's trust anchor
#   edition                                       EDITION (checked by the ISO builds)
# SUITE, COMPONENT, Origin and Label come from config/packages.env. The
# build-iso.sh / build-vm-iso.sh map the result onto the ISO at
# /brainsyncos-repo.
#
# The secret key is imported into a GnuPG home on /scratch (tmpfs) only. The
# result is checked before it replaces the old one: InRelease and Release.gpg
# verify with gpgv against repo-key.gpg, the index lists every .deb with its
# size and sha256, and no file outside pool/ contains private key material.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_packages_config

edition=""
debs=""
key="$REPO_SECRET_KEY"
out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --edition) edition="$(parse_edition "${2:-}")"; shift 2 ;;
        --debs) debs="${2:?--debs needs a directory}"; shift 2 ;;
        --key) key="${2:?--key needs a file}"; shift 2 ;;
        --out) out="${2:?--out needs a directory}"; shift 2 ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[ -n "$edition" ] || die "--edition jetson|vm is required"
debs="${debs:-$DEBS_DIR/$edition}"
out="${out:-$REPO_DIR/$edition}"
require_container
require_cmd apt-ftparchive gpg gpgv gpgconf dpkg-deb sha256sum python3
[ -s "$key" ] || die "no repository signing key at ${key#$BRAINSYNCOS_ROOT/} (create one: scripts/build.sh key)"
ls "$debs"/*.deb > /dev/null 2>&1 || die "no .debs in ${debs#$BRAINSYNCOS_ROOT/} (run: scripts/build.sh packages --edition $edition)"

# The key must be one secret key that can sign and has not expired.
recs="$(key_records "$key")" || die "not an OpenPGP key file: $key"
printf '%s\n' "$recs" | grep -q '^sec:' || die "$key holds no secret key (use repo-signing-key.asc, not the .pub.asc)"
fpr="$(key_fingerprint "$key")"
caps="$(printf '%s\n' "$recs" | awk -F: '/^sec:/ {print $12; exit}')"
case "$caps" in *S*) ;; *) die "key $fpr cannot sign (capabilities: $caps)" ;; esac
expires="$(printf '%s\n' "$recs" | awk -F: '/^sec:/ {print $7; exit}')"
if [ -n "$expires" ]; then
    [ "$expires" -gt "$(date +%s)" ] || die "key $fpr expired on $(date -u -d "@$expires" +%Y-%m-%d)"
    [ "$expires" -gt $(( $(date +%s) + 90 * 86400 )) ] \
        || warn "key $fpr expires on $(date -u -d "@$expires" +%Y-%m-%d); devices cannot verify updates signed with an expired key"
fi

# Build the new repository next to the old one, then swap.
new="$out.new"
force_rm_tree "$new"
dist="$new/dists/$REPO_SUITE"
pool="$new/pool/$REPO_COMPONENT"
mkdir -p "$pool" "$dist/$REPO_COMPONENT/binary-arm64"
for deb in "$debs"/*.deb; do
    cp "$deb" "$pool/"
done
(cd "$new" && apt-ftparchive packages "pool/$REPO_COMPONENT") > "$dist/$REPO_COMPONENT/binary-arm64/Packages" \
    || die "apt-ftparchive packages failed"
gzip -9n < "$dist/$REPO_COMPONENT/binary-arm64/Packages" > "$dist/$REPO_COMPONENT/binary-arm64/Packages.gz"
(cd "$dist" && apt-ftparchive \
    -o APT::FTPArchive::Release::Origin="$REPO_ORIGIN" \
    -o APT::FTPArchive::Release::Label="$REPO_LABEL" \
    -o APT::FTPArchive::Release::Suite="$REPO_SUITE" \
    -o APT::FTPArchive::Release::Codename="$REPO_SUITE" \
    -o APT::FTPArchive::Release::Architectures=arm64 \
    -o APT::FTPArchive::Release::Components="$REPO_COMPONENT" \
    -o APT::FTPArchive::Release::Description="BrainSyncOS packages ($edition edition)" \
    release .) > "$new/Release.tmp" || die "apt-ftparchive release failed"
mv "$new/Release.tmp" "$dist/Release"

home="$(scratch_dir build-repo)"
trap 'gpgconf_kill "$home"; rm -rf "$home"; force_rm_tree "$new"' EXIT
GNUPGHOME="$home" gpg --batch --quiet --import "$key" 2> /dev/null || die "gpg cannot import $key"
sign() {  # sign GPG_ARGS...
    GNUPGHOME="$home" gpg --batch --yes --quiet --pinentry-mode loopback --passphrase '' \
        --local-user "$fpr" --digest-algo SHA512 "$@"
}
sign --clearsign --output "$dist/InRelease" "$dist/Release" || die "could not sign InRelease"
sign --armor --detach-sign --output "$dist/Release.gpg" "$dist/Release" || die "could not sign Release.gpg"
GNUPGHOME="$home" gpg --batch --export "$fpr" > "$new/repo-key.gpg"
printf '%s\n' "$edition" > "$new/edition"

# --- checks ------------------------------------------------------------------------
gpgv --keyring "$new/repo-key.gpg" "$dist/InRelease" > /dev/null 2>&1 || die "InRelease does not verify against repo-key.gpg"
gpgv --keyring "$new/repo-key.gpg" "$dist/Release.gpg" "$dist/Release" > /dev/null 2>&1 || die "Release.gpg does not verify against repo-key.gpg"
! grep -rlq --exclude-dir=pool -e 'PRIVATE KEY' "$new" || die "the repository contains private key material"
python3 - "$new" "$dist/$REPO_COMPONENT/binary-arm64/Packages" <<'PYEOF' || die "the package index does not match the pool"
import hashlib, os, sys
root, index = sys.argv[1], sys.argv[2]
stanzas = [dict(l.split(": ", 1) for l in s.splitlines() if ": " in l and not l.startswith(" "))
           for s in open(index).read().split("\n\n") if s.strip()]
listed = {}
for st in stanzas:
    path = os.path.join(root, st["Filename"])
    data = open(path, "rb").read()
    if int(st["Size"]) != len(data) or st["SHA256"] != hashlib.sha256(data).hexdigest():
        sys.exit("%s: size or sha256 differs from the index" % st["Filename"])
    listed[st["Filename"]] = st["Package"]
pool = {os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(os.path.join(root, "pool")) for f in fs}
if pool != set(listed):
    sys.exit("pool and index differ: %s" % sorted(pool ^ set(listed)))
print("index: %d packages (%s)" % (len(listed), ", ".join(sorted(listed.values()))))
PYEOF

force_rm_tree "$out"
mv "$new" "$out"
log "repository for $edition: ${out#$BRAINSYNCOS_ROOT/} (suite $REPO_SUITE, signed by $fpr)"
