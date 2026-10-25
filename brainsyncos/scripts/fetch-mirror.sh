#!/usr/bin/env bash
# fetch-mirror.sh - fetch the third-party .debs an edition's repository mirrors (builder only).
#
# Usage: scripts/builder.sh run scripts/fetch-mirror.sh [--edition jetson|vm]
#            [--only NAME] [--lock]
#
# BrainSyncOS ships a few .debs it does not build: NVIDIA's CUDA runtime on
# the Jetson edition. Each source is a folder mirror/NAME/ with mirror.env
# (repository, signing key fingerprint, the packages wanted), the key, and
# debs.lock, which pins every .deb by size and sha256. This script, for each
# mirror of the edition (--edition; default every mirror):
#   - checks that MIRROR_KEY's primary key has the fingerprint MIRROR_KEY_FPR;
#   - verifies the cached InRelease with gpgv against that key alone, and
#     the cached Packages index against InRelease;
#   - checks that debs.lock is exactly the closure of MIRROR_ROOTS in that
#     signed index, with nothing MIRROR_FORBID rules out;
#   - downloads each pinned .deb that is missing from the cache and keeps it
#     only if its size and sha256 equal the pin (and so the signed index).
# InRelease and the index are downloaded only when a .deb is missing, so a
# filled cache works offline. --lock downloads them afresh and rewrites
# debs.lock (commit it). The cache: build/downloads/mirror/NAME/.
# scripts/build-repo.sh copies the .debs into the repository and checks
# them against the pins again (scripts/mirror.py check).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

edition=""
only=""
lock=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --edition) edition="$(parse_edition "${2:-}")"; shift 2 ;;
        --only) only="${2:?--only needs a mirror name}"; shift 2 ;;
        --lock) lock=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_container
require_cmd python3 gpg gpgv dpkg

names="$(mirror_names "${edition:-all}")"
if [ -n "$only" ]; then
    case " $names " in *" $only "*) names="$only" ;; *) die "no mirror named $only${edition:+ for $edition} in ${MIRROR_DIR#$BRAINSYNCOS_ROOT/}/" ;; esac
fi
if [ -z "$names" ]; then
    log "no mirrored packages${edition:+ for $edition}"
    exit 0
fi
for name in $names; do
    (
        load_mirror "$name"
        s="$(scratch_dir "mirror-$name")"
        trap 'force_rm_tree "$s"' EXIT
        mirror_keyring "$name" "$s/key.gpg"
        cmd=fetch
        [ "$lock" = 0 ] || cmd=lock
        log "$name: $cmd ($MIRROR_URL $MIRROR_SUITE, key $MIRROR_KEY_FPR)"
        python3 -I "$BRAINSYNCOS_ROOT/scripts/mirror.py" "$cmd" --mirror "$MIRROR_DIR/$name" \
            --cache "$MIRROR_CACHE/$name" --keyring "$s/key.gpg" || die "$name: $cmd failed"
    ) || exit 1
done
log "mirrored packages in ${MIRROR_CACHE#$BRAINSYNCOS_ROOT/}/: $names"
