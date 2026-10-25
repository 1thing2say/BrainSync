#!/usr/bin/env bash
# builder.sh - host-side entrypoint to the BrainSyncOS arm64 builder container.
#
# Usage:
#   scripts/builder.sh build            build the builder image
#   scripts/builder.sh run CMD [ARG...] run CMD in the builder (cwd /work)
#   scripts/builder.sh shell            interactive shell in the builder
#   scripts/builder.sh image            print the image reference
#   scripts/builder.sh clean [--all]    delete build/out (--all: all of build/)
#   scripts/builder.sh tools CMD [ARG...]  run CMD in the native tools container
#                                       (the builder when tools mode is off)
#   scripts/builder.sh tools-mode       print "native PLATFORM" or "builder"
#   scripts/builder.sh ids              print the image ids: "builder ID",
#                                       "tools ID" (or "tools -"); builds them
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
# `clean` truncates large files before deleting them: Docker Desktop keeps
# deleted bind-mounted files open, so a plain rm would not free their space.
#
# Native tools mode (BSOS_NATIVE_TOOLS=auto, the default; 1 forces it on, 0
# off): `tools` runs in brainsyncos-tools (docker/Dockerfile.tools), built for
# the Docker engine's own architecture, with the same mounts, caps and
# environment as `run`. It takes the steps that run no aarch64 code: packing
# and byte-compiling the packages, the repository, the ISO builds. auto turns
# it on only where the builder is emulated (an x86-64 host); on an arm64 host
# `tools` is simply `run`.
# Build lock: `run` and `tools` of a heavy step (build-packages, build-wheels,
# build-repo, build-iso, build-vm-iso, extract-iso, repack-iso) first take an
# flock on BSOS_BUILD_LOCK_FILE (default /tmp/brainsyncos-build.lock, shared by
# every worktree), so parallel builds queue instead of fighting over the CPUs.
# It logs while it waits. BSOS_BUILD_LOCK=0 skips it; hosts without flock(1)
# (macOS) do not queue.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config
load_build_config

DOCKERFILE="$BRAINSYNCOS_ROOT/docker/Dockerfile.builder"
# tini as PID 1 passes Ctrl-C/SIGTERM to the whole process group. Without it the
# script itself is PID 1, ignores SIGINT, and xorriso/dd keep running.
INIT_FLAGS="--init -e TINI_KILL_PROCESS_GROUP=1"
# The image and platform `run` uses: the builder's, unless `tools` set them.
RUN_IMAGE=""
RUN_PLATFORM=""

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
    if [ -z "${RUN_IMAGE:-}" ]; then
        ensure_image
        RUN_IMAGE="$(image_ref)"
        RUN_PLATFORM="$BUILDER_PLATFORM"
    fi
    case "$1" in
        scripts/build-packages.sh|scripts/build-wheels.sh|scripts/build-repo.sh|scripts/build-iso.sh|\
        scripts/build-vm-iso.sh|scripts/extract-iso.sh|scripts/repack-iso.sh) take_build_lock ;;
    esac
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
        --platform "$RUN_PLATFORM" \
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
        "$RUN_IMAGE" "$@" || rc=$?
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
    docker run --rm $INIT_FLAGS --platform "${RUN_PLATFORM:-$BUILDER_PLATFORM}" -v "$BRAINSYNCOS_ROOT:/work" \
        "${RUN_IMAGE:-$(image_ref)}" find /work ! -uid "$uid" -exec chown -h "$uid:$gid" {} + \
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

# --- native tools mode -----------------------------------------------------------
TOOLS_DOCKERFILE="$BRAINSYNCOS_ROOT/docker/Dockerfile.tools"
TOOLS_IMAGE_NAME=brainsyncos-tools

TOOLS_PLATFORM=""

# detect_tools_platform: set TOOLS_PLATFORM to the Docker engine's own
# platform (linux/amd64 or linux/arm64), once.
detect_tools_platform() {
    [ -z "$TOOLS_PLATFORM" ] || return 0
    case "$(docker info -f '{{.Architecture}}' 2>/dev/null)" in
        x86_64|amd64) TOOLS_PLATFORM=linux/amd64 ;;
        aarch64|arm64) TOOLS_PLATFORM=linux/arm64 ;;
        *) die "cannot tell the Docker engine's architecture (docker info)" ;;
    esac
}

# tools_on: native tools mode is on (BSOS_NATIVE_TOOLS, see the header).
tools_on() {
    detect_tools_platform
    case "${BSOS_NATIVE_TOOLS:-auto}" in
        0) return 1 ;;
        1) return 0 ;;
        auto) [ "$TOOLS_PLATFORM" != "$BUILDER_PLATFORM" ] ;;
        *) die "BSOS_NATIVE_TOOLS must be auto, 1 or 0, got $BSOS_NATIVE_TOOLS" ;;
    esac
}

tools_ref() {
    printf '%s:%s-%s\n' "$TOOLS_IMAGE_NAME" "$(sha256_of "$TOOLS_DOCKERFILE" | cut -c1-12)" "${TOOLS_PLATFORM#*/}"
}

ensure_tools_image() {
    local ref
    ref="$(tools_ref)"
    docker image inspect "$ref" > /dev/null 2>&1 && return 0
    log "building $ref ($TOOLS_PLATFORM)"
    docker build --platform "$TOOLS_PLATFORM" -t "$ref" -f "$TOOLS_DOCKERFILE" "$BRAINSYNCOS_ROOT/docker"
}

cmd_tools() {
    require_cmd docker
    [ "$#" -gt 0 ] || die "usage: builder.sh tools CMD [ARG...]"
    if tools_on; then
        ensure_tools_image
        RUN_IMAGE="$(tools_ref)"
        RUN_PLATFORM="$TOOLS_PLATFORM"
    fi
    cmd_run "$@"
}

cmd_ids() {
    require_cmd docker
    ensure_image
    printf 'builder %s\n' "$(docker image inspect -f '{{.Id}}' "$(image_ref)")"
    if tools_on; then
        ensure_tools_image
        printf 'tools %s\n' "$(docker image inspect -f '{{.Id}}' "$(tools_ref)")"
    else
        echo 'tools -'
    fi
}

# take_build_lock: queue behind other heavy builds (see the header). The lock
# is held on fd 9 until this script exits.
take_build_lock() {
    [ "${BSOS_BUILD_LOCK:-1}" != 0 ] || return 0
    command -v flock > /dev/null 2>&1 || return 0
    local f="${BSOS_BUILD_LOCK_FILE:-/tmp/brainsyncos-build.lock}" t0
    exec 9>> "$f"
    flock -n 9 && return 0
    t0="$(date +%s)"
    log "waiting for another BrainSyncOS build to finish (lock $f; BSOS_BUILD_LOCK=0 skips the queue)"
    flock 9
    log "lock taken after $(( $(date +%s) - t0 )) s"
}

case "${1:-}" in
    build) shift; cmd_build "$@" ;;
    run)   shift; cmd_run "$@" ;;
    shell) shift; cmd_run bash -l ;;
    image) image_ref ;;
    clean) shift; cmd_clean "$@" ;;
    tools) shift; cmd_tools "$@" ;;
    tools-mode) if tools_on; then echo "native $TOOLS_PLATFORM"; else echo builder; fi ;;
    ids) cmd_ids ;;
    -h|--help|help|'') usage_from_header; [ -n "${1:-}" ] || exit 2 ;;
    *) die "unknown command: $1 (try --help)" ;;
esac
