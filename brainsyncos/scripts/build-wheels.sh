#!/usr/bin/env bash
# build-wheels.sh - build the Python wheels BrainSyncOS compiles itself (builder only).
#
# Usage: scripts/builder.sh run scripts/build-wheels.sh [--only NAME] [--out DIR] [--force]
#
# Builds every wheels/NAME/ (wheel.env + build.sh) into build/wheels/ (--out).
# A wheel is for both editions (both are Ubuntu 24.04 arm64); it is not a
# .deb, because it goes into the BrainSync venv (/opt/brainsyncos/venv,
# packaged as brainsync-runtime), which cannot see system site-packages.
# A wheel whose manifest says it was built from the same inputs, and whose
# file still matches its sha256, is kept as it is unless --force. The inputs
# are every file in wheels/NAME/, scripts/pack-wheel.py and the builder's
# Dockerfile (INPUTS_SHA256); downloads are cached in build/downloads/.
#
# Output in DIR, per wheel:
#   the .whl
#   NAME.env  the manifest, plain KEY=value (lib.sh load_env_file reads it):
#             WHEEL_NAME WHEEL_FILE WHEEL_SHA256 WHEEL_SIZE WHEEL_DIST
#             WHEEL_VERSION WHEEL_TAG INPUTS_SHA256 BUILD_REV, plus what
#             build.sh reports (for brainflow-cerelog: SOURCE_URL,
#             SOURCE_COMMIT, SOURCE_TREE, PATCHES, CMAKE_FLAGS, CMAKE_TARGETS,
#             TOOLCHAIN, VERIFIED_SAMPLING_RATE)
# and for all wheels in DIR, rewritten after every build:
#   SHA256SUMS        for sha256sum -c
#   requirements.txt  DIST==VERSION --hash=sha256:..., for
#       pip install --no-index --find-links DIR --require-hashes --no-deps -r DIR/requirements.txt
#
# Each wheels/NAME/build.sh runs with the current directory set to its own
# folder and gets BSOS_WHEEL_OUT (an empty directory: it must leave exactly
# one .whl there, plus manifest.env with extra KEY=value lines) and BSOS_JOBS
# (config/build.env, set by builder.sh).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

WHEELS_DIR="$BRAINSYNCOS_ROOT/wheels"
only=""
out="$BUILD_DIR/wheels"
force=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --only) only="${2:?--only needs a wheel name}"; shift 2 ;;
        --out) out="${2:?--out needs a directory}"; shift 2 ;;
        --force) force=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_container
require_cmd python3 sha256sum
case "$out" in /*) ;; *) out="$BRAINSYNCOS_ROOT/$out" ;; esac

names=""
for env in "$WHEELS_DIR"/*/wheel.env; do
    [ -e "$env" ] || continue
    name="$(basename "$(dirname "$env")")"
    [ -z "$only" ] || [ "$name" = "$only" ] || continue
    names="$names $name"
done
[ -n "$names" ] || die "no wheel ${only:+named $only }in ${WHEELS_DIR#$BRAINSYNCOS_ROOT/}/"
mkdir -p "$out" "$DOWNLOADS_DIR"

# inputs_sha256 NAME: one hash over the recipe, the packer and the builder image.
inputs_sha256() {
    (
        cd "$BRAINSYNCOS_ROOT" || exit 1
        find "wheels/$1" -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
        sha256sum scripts/pack-wheel.py docker/Dockerfile.builder
    ) | sha256sum | awk '{print $1}'
}

# up_to_date NAME INPUTS: DIR/NAME.env was built from INPUTS and its wheel is intact.
up_to_date() {
    local m="$out/$1.env"
    [ -f "$m" ] || return 1
    (
        load_env_file "$m" "WHEEL_FILE WHEEL_SHA256 INPUTS_SHA256"
        [ "$INPUTS_SHA256" = "$2" ] && [ -f "$out/$WHEEL_FILE" ] && [ "$(sha256_of "$out/$WHEEL_FILE")" = "$WHEEL_SHA256" ]
    )
}

for name in $names; do
    dir="$WHEELS_DIR/$name"
    (
        load_env_file "$dir/wheel.env" "WHEEL_NAME WHEEL_DIST WHEEL_VERSION WHEEL_TAG"
        [ "$WHEEL_NAME" = "$name" ] || die "$dir/wheel.env: WHEEL_NAME=$WHEEL_NAME, but the folder is $name"
        [ -x "$dir/build.sh" ] || die "$dir/build.sh is missing or not executable"
        inputs="$(inputs_sha256 "$name")"
        if [ "$force" = 0 ] && up_to_date "$name" "$inputs"; then
            log "$name: up to date (${out#$BRAINSYNCOS_ROOT/}/$name.env, inputs $inputs)"
            exit 0
        fi
        stage="$(scratch_dir "wheel-$name")"
        mkdir -p "$stage/out"
        log "$name: building $WHEEL_DIST $WHEEL_VERSION"
        (cd "$dir" && BSOS_WHEEL_OUT="$stage/out" BSOS_JOBS="${BSOS_JOBS:-1}" ./build.sh) || die "$name: build.sh failed"
        n="$(find "$stage/out" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')"
        [ "$n" = 1 ] || die "$name: build.sh left $n wheels, want 1"
        whl="$(find "$stage/out" -maxdepth 1 -name '*.whl')"
        file="$(basename "$whl")"
        case "$file" in
            "$(printf '%s' "$WHEEL_DIST" | tr -- '-.' '__')-$WHEEL_VERSION-$WHEEL_TAG.whl") ;;
            *) die "$name: built $file, wheel.env says $WHEEL_DIST $WHEEL_VERSION $WHEEL_TAG" ;;
        esac
        [ -f "$stage/out/manifest.env" ] || die "$name: build.sh wrote no manifest.env"
        load_env_file "$stage/out/manifest.env" ""
        if [ -f "$out/$name.env" ]; then
            old="$(sed -n 's/^WHEEL_FILE=//p' "$out/$name.env")"
            [ -z "$old" ] || [ "$old" = "$file" ] || rm_released "$out/$old"
        fi
        cp "$whl" "$out/$file.tmp"
        mv "$out/$file.tmp" "$out/$file"
        {
            echo "# $name: written by scripts/build-wheels.sh; plain KEY=value."
            echo "WHEEL_NAME=$name"
            echo "WHEEL_FILE=$file"
            echo "WHEEL_SHA256=$(sha256_of "$out/$file")"
            echo "WHEEL_SIZE=$(file_size "$out/$file")"
            echo "INPUTS_SHA256=$inputs"
            echo "BUILD_REV=${BSOS_GIT_REV:-unknown}"
            grep -v '^#' "$stage/out/manifest.env"
        } > "$out/$name.env.tmp"
        mv "$out/$name.env.tmp" "$out/$name.env"
        force_rm_tree "$stage"
        log "$name: ${out#$BRAINSYNCOS_ROOT/}/$file ($(file_size "$out/$file") bytes, sha256 $(sha256_of "$out/$file"))"
    ) || exit 1
done

# The indexes over every wheel in DIR.
: > "$out/SHA256SUMS.tmp"
: > "$out/requirements.txt.tmp"
for m in "$out"/*.env; do
    [ -e "$m" ] || continue
    (
        load_env_file "$m" "WHEEL_FILE WHEEL_SHA256 WHEEL_DIST WHEEL_VERSION"
        [ "$(sha256_of "$out/$WHEEL_FILE")" = "$WHEEL_SHA256" ] || die "$WHEEL_FILE does not match $(basename "$m")"
        printf '%s  %s\n' "$WHEEL_SHA256" "$WHEEL_FILE" >> "$out/SHA256SUMS.tmp"
        printf '%s==%s --hash=sha256:%s\n' "$WHEEL_DIST" "$WHEEL_VERSION" "$WHEEL_SHA256" >> "$out/requirements.txt.tmp"
    ) || exit 1
done
mv "$out/SHA256SUMS.tmp" "$out/SHA256SUMS"
mv "$out/requirements.txt.tmp" "$out/requirements.txt"
log "wheels in ${out#$BRAINSYNCOS_ROOT/}: $(wc -l < "$out/SHA256SUMS" | tr -d ' ') (SHA256SUMS, requirements.txt)"
