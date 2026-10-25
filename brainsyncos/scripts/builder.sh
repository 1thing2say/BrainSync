#!/usr/bin/env bash
# builder.sh - host-side entrypoint to the BrainSyncOS arm64 builder container.
#
# Usage:
#   scripts/builder.sh build            build the builder image
#   scripts/builder.sh run CMD [ARG...] run CMD in the builder (cwd /work)
#   scripts/builder.sh shell            interactive shell in the builder
#   scripts/builder.sh image            print the image reference
#   scripts/builder.sh clean [--all]    delete build/out (--all: all of build/)
#   scripts/builder.sh --cross build|run|shell|image
#                                       the same with the amd64 cross-builder
#   scripts/builder.sh --llama build|run|shell|image
#                                       the same with the llama builder for this
#                                       host: native arm64 on an arm64 host,
#                                       the cross-builder on x86-64
#
# Mounts: brainsyncos/ -> /work (rw), the base ISO's directory -> /in (ro,
# only when the ISO is found; commands that do not need it run without),
# the BrainSync source tree around brainsyncos/ -> /src (ro, as BSOS_APP_SRC,
# only when it is there: packages/brainsync builds the app from it),
# a tmpfs at /scratch (squashfs work, GnuPG homes; with CAP_SYS_ADMIN for xattrs).
# Resources (config/build.env, BSOS_BUILD_* overrides): the container gets
# BUILD_CPUS CPUs (--cpus), BUILD_MEMORY (--memory), a BUILD_SCRATCH_SIZE
# /scratch, and BUILD_JOBS as BSOS_JOBS, MAKEFLAGS=-jN and
# CMAKE_BUILD_PARALLEL_LEVEL for compilers. `clean` gets the same CPU cap.
# BSOS_GIT_REV carries the host's git revision (build id) into the container,
# BSOS_SOURCE_DATE_EPOCH its commit time (file times in the packages; not
# SOURCE_DATE_EPOCH itself, which mksquashfs would apply to the ISO's layers).
# The ISO is $BASE_ISO if set, else ~/Downloads/$BASE_ISO_NAME; symlinks are
# resolved first and the real file's directory is mounted.
# The image tag is derived from the Dockerfile hash, so editing the Dockerfile
# yields a new tag; `run` builds the image first if that tag is missing.
# Always uses --platform linux/arm64 (native on Apple Silicon; x86_64 hosts
# need qemu-user/binfmt registered with Docker).
# --cross selects a second, separate image instead: brainsyncos-cross-builder
# (docker/Dockerfile.cross-builder, --platform linux/amd64), which compiles
# aarch64 code at native speed on an x86-64 host (scripts/build-llama.sh
# compile). Same mounts, caps and variables; on Apple Silicon it runs under
# emulation instead. The arm64 builder is unchanged by it.
# --llama selects the image that compiles llama.cpp fastest on this host: on
# an arm64 host (Apple Silicon) brainsyncos-llama-builder
# (docker/Dockerfile.llama-builder, --platform linux/arm64, NVIDIA's aarch64
# nvcc from the Jetson repository, no emulation); on an x86-64 host the
# cross-builder above. Same mounts, caps and variables. Its image build gets
# mirror/nvidia-jetson as the named build context nvidia-jetson, so the
# Dockerfile can COPY the committed repository key from there.
# `clean` truncates large files before deleting them: Docker Desktop keeps
# deleted bind-mounted files open, so a plain rm would not free their space.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config
load_build_config

DOCKERFILE="$BRAINSYNCOS_ROOT/docker/Dockerfile.builder"
BUILD_CONTEXT_FLAGS=""
# use_cross: the amd64 cross-builder (aarch64 code compiled on x86-64).
use_cross() {
    DOCKERFILE="$BRAINSYNCOS_ROOT/docker/Dockerfile.cross-builder"
    BUILDER_IMAGE_NAME=brainsyncos-cross-builder
    BUILDER_PLATFORM=linux/amd64
}
# use_llama_native: the arm64 llama builder (native nvcc, no emulation).
use_llama_native() {
    DOCKERFILE="$BRAINSYNCOS_ROOT/docker/Dockerfile.llama-builder"
    BUILDER_IMAGE_NAME=brainsyncos-llama-builder
    BUILDER_PLATFORM=linux/arm64
    BUILD_CONTEXT_FLAGS="--build-context nvidia-jetson=$BRAINSYNCOS_ROOT/mirror/nvidia-jetson"
}
case "${1:-}" in
    --cross)
        shift
        use_cross
        case "${1:-}" in build|run|shell|image) ;; *) die "--cross takes build, run, shell or image" ;; esac
        ;;
    --llama)
        shift
        case "$(uname -m)" in
            arm64|aarch64) use_llama_native ;;
            x86_64|amd64) use_cross ;;
            *) die "--llama: no llama builder for a $(uname -m) host (arm64 or x86-64)" ;;
        esac
        case "${1:-}" in build|run|shell|image) ;; *) die "--llama takes build, run, shell or image" ;; esac
        ;;
esac
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
    # shellcheck disable=SC2086  # BUILD_CONTEXT_FLAGS: zero or more flags, split as given
    docker build --platform "$BUILDER_PLATFORM" $BUILD_CONTEXT_FLAGS \
        -t "$ref" -t "$BUILDER_IMAGE_NAME:latest" \
        -f "$DOCKERFILE" "$BRAINSYNCOS_ROOT/docker"
    log "built $ref"
}

# host_git_rev: short commit of the brainsyncos/ tree, "-dirty" if it has
# uncommitted changes (build/ is gitignored), "unknown" outside git. Computed
# on the host: the worktree's .git file points outside the container's mount.
host_git_rev() {
    local rev
    rev="$(git -C "$BRAINSYNCOS_ROOT" rev-parse --short=12 HEAD 2>/dev/null)" || { echo unknown; return 0; }
    if [ -n "$(git -C "$BRAINSYNCOS_ROOT" status --porcelain -- . 2>/dev/null)" ]; then
        rev="$rev-dirty"
    fi
    printf '%s\n' "$rev"
}

# host_source_date: commit time of HEAD (seconds), the time stamped into the
# packages; the current time outside git.
host_source_date() {
    git -C "$BRAINSYNCOS_ROOT" log -1 --format=%ct 2>/dev/null || date +%s
}

ensure_image() {
    docker image inspect "$(image_ref)" >/dev/null 2>&1 || cmd_build
}

cmd_run() {
    require_cmd docker
    [ "$#" -gt 0 ] || die "usage: builder.sh run CMD [ARG...]"
    local iso iso_flags="" io_flags="" src src_flags=""
    # Mount the real file's directory, so a symlinked $BASE_ISO cannot dangle.
    # Without the ISO the container still runs (e.g. make-password-hash.sh);
    # steps that need it fail inside with "base ISO not found at /in/...".
    if iso="$(resolve_base_iso 2>/dev/null)"; then
        iso="$(real_path "$iso")"
        case "$iso" in *' '*) die "the base ISO path must not contain spaces: $iso" ;; esac
        iso_flags="-v $(dirname "$iso"):/in:ro -e BASE_ISO=/in/$(basename "$iso")"
    fi
    # The app's source: brainsyncos/ sits inside the BrainSync repository.
    src="$(cd "$BRAINSYNCOS_ROOT/.." && pwd -P)"
    if [ -f "$src/pyproject.toml" ] && [ -d "$src/brainsync" ]; then
        case "$src" in *' '*) die "the BrainSync source path must not contain spaces: $src" ;; esac
        src_flags="-v $src:/src:ro -e BSOS_APP_SRC=/src"
    fi
    ensure_image
    # -t only for a real terminal; -i only when input is piped in, so a job
    # whose output is redirected does not hold on to the controlling TTY.
    if [ -t 0 ] && [ -t 1 ]; then
        io_flags="-i -t"
    elif [ ! -t 0 ]; then
        io_flags="-i"
    fi
    # /scratch is a tmpfs for unpacking squashfs layers: NVIDIA's overlay layers
    # carry trusted.overlay.* xattrs, which only CAP_SYS_ADMIN may write and the
    # macOS bind mount cannot store at all.
    local rc=0
    # shellcheck disable=SC2086
    docker run --rm $io_flags $INIT_FLAGS \
        --platform "$BUILDER_PLATFORM" \
        --cpus "$BUILD_CPUS" --memory "$BUILD_MEMORY" \
        --cap-add SYS_ADMIN \
        --tmpfs "/scratch:rw,exec,size=$BUILD_SCRATCH_SIZE" \
        -v "$BRAINSYNCOS_ROOT:/work" \
        $iso_flags $src_flags \
        -e BRAINSYNCOS_IN_CONTAINER=1 \
        -e BRAINSYNCOS_ROOT=/work \
        -e BSOS_GIT_REV="$(host_git_rev)" \
        -e BSOS_SOURCE_DATE_EPOCH="$(host_source_date)" \
        -e BSOS_JOBS="$BUILD_JOBS" \
        -e MAKEFLAGS="-j$BUILD_JOBS" \
        -e CMAKE_BUILD_PARALLEL_LEVEL="$BUILD_JOBS" \
        -w /work \
        "$(image_ref)" "$@" || rc=$?
    give_back_files
    return "$rc"
}

# give_back_files: on Linux with rootful Docker, or Docker Desktop on WSL, what
# the container writes as root stays root-owned on the host, and host-side
# steps can then neither clean nor rewrite it. Hand such files under
# brainsyncos/ back to the calling user. Docker Desktop on macOS and rootless
# Docker map ownership themselves, so nothing is found there.
give_back_files() {
    local uid gid
    uid="$(id -u)"
    gid="$(id -g)"
    [ "$uid" != 0 ] || return 0
    [ -n "$(find "$BRAINSYNCOS_ROOT" ! -user "$uid" -print 2>/dev/null | head -n 1)" ] || return 0
    # shellcheck disable=SC2086
    docker run --rm $INIT_FLAGS --platform "$BUILDER_PLATFORM" -v "$BRAINSYNCOS_ROOT:/work" \
        "$(image_ref)" find /work ! -uid "$uid" -exec chown -h "$uid:$gid" {} + \
        || warn "could not hand container-written files under brainsyncos/ back to uid $uid"
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
    docker run --rm $INIT_FLAGS --platform "$BUILDER_PLATFORM" --cpus "$BUILD_CPUS" --memory "$BUILD_MEMORY" \
        -v "$BRAINSYNCOS_ROOT:/work" -w /work \
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
