#!/usr/bin/env bash
# builder.sh - host-side entrypoint to the BrainSyncOS arm64 builder container.
#
# Usage:
#   scripts/builder.sh build            build the builder image
#   scripts/builder.sh run CMD [ARG...] run CMD in the builder (cwd /work)
#   scripts/builder.sh shell            interactive shell in the builder
#   scripts/builder.sh image            print the image reference
#   scripts/builder.sh clean [--all]    delete build/out (--all: all of build/)
#
# Mounts: brainsyncos/ -> /work (rw), the base ISO's directory -> /in (ro).
# The ISO is $BASE_ISO if set, else ~/Downloads/$BASE_ISO_NAME; symlinks are
# resolved first and the real file's directory is mounted.
# The image tag is derived from the Dockerfile hash, so editing the Dockerfile
# yields a new tag; `run` builds the image first if that tag is missing.
# Always uses --platform linux/arm64 (native on Apple Silicon; x86_64 hosts
# need qemu-user/binfmt registered with Docker).
# `clean` truncates large files before deleting them: Docker Desktop keeps
# deleted bind-mounted files open, so a plain rm would not free their space.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config

DOCKERFILE="$BRAINSYNCOS_ROOT/docker/Dockerfile.builder"
# tini as PID 1 passes Ctrl-C/SIGTERM to the whole process group. Without it the
# script itself is PID 1, ignores SIGINT, and xorriso/dd keep running.
INIT_FLAGS="--init -e TINI_KILL_PROCESS_GROUP=1"

image_ref() {
    printf '%s:%s\n' "$BUILDER_IMAGE_NAME" "$(sha256_of "$DOCKERFILE" | cut -c1-12)"
}

cmd_build() {
    require_cmd docker
    local ref
    ref="$(image_ref)"
    log "building $ref ($BUILDER_PLATFORM)"
    docker build --platform "$BUILDER_PLATFORM" \
        -t "$ref" -t "$BUILDER_IMAGE_NAME:latest" \
        -f "$DOCKERFILE" "$BRAINSYNCOS_ROOT/docker"
    log "built $ref"
}

ensure_image() {
    docker image inspect "$(image_ref)" >/dev/null 2>&1 || cmd_build
}

cmd_run() {
    require_cmd docker
    [ "$#" -gt 0 ] || die "usage: builder.sh run CMD [ARG...]"
    local iso iso_dir iso_base io_flags=""
    # Mount the real file's directory, so a symlinked $BASE_ISO cannot dangle.
    iso="$(resolve_base_iso)"
    iso="$(real_path "$iso")"
    iso_dir="$(dirname "$iso")"
    iso_base="$(basename "$iso")"
    ensure_image
    # -t only for a real terminal; -i only when input is piped in, so a job
    # whose output is redirected does not hold on to the controlling TTY.
    if [ -t 0 ] && [ -t 1 ]; then
        io_flags="-i -t"
    elif [ ! -t 0 ]; then
        io_flags="-i"
    fi
    # shellcheck disable=SC2086
    exec docker run --rm $io_flags $INIT_FLAGS \
        --platform "$BUILDER_PLATFORM" \
        -v "$BRAINSYNCOS_ROOT:/work" \
        -v "$iso_dir:/in:ro" \
        -e BRAINSYNCOS_IN_CONTAINER=1 \
        -e BRAINSYNCOS_ROOT=/work \
        -e BASE_ISO="/in/$iso_base" \
        -w /work \
        "$(image_ref)" "$@"
}

cmd_clean() {
    local target=build/out
    case "${1:-}" in
        --all) target=build ;;
        '') ;;
        *) die "usage: builder.sh clean [--all]" ;;
    esac
    require_cmd docker
    ensure_image
    log "removing $target/ (inside the builder)"
    # shellcheck disable=SC2086
    docker run --rm $INIT_FLAGS --platform "$BUILDER_PLATFORM" -v "$BRAINSYNCOS_ROOT:/work" -w /work \
        "$(image_ref)" bash -c 'source scripts/lib.sh; force_rm_tree "$1"' _ "$target"
}

case "${1:-}" in
    build) shift; cmd_build "$@" ;;
    run)   shift; cmd_run "$@" ;;
    shell) shift; cmd_run bash -l ;;
    image) image_ref ;;
    clean) shift; cmd_clean "$@" ;;
    -h|--help|help|'') usage_from_header; [ -n "${1:-}" ] || exit 2 ;;
    *) die "unknown command: $1 (try --help)" ;;
esac
