#!/usr/bin/env bash
# build-packages.sh - build the BrainSyncOS packages of one edition (builder only).
#
# Usage: scripts/builder.sh run scripts/build-packages.sh --edition jetson|vm
#            [--only NAME] [--skip NAME[,NAME...]] [--key FILE] [--out DIR]
#
# Builds every packages/NAME/ whose package.env lists the edition
# (PKG_EDITIONS=all, or a comma-separated list of jetson and vm) by running
# its build.sh, into build/debs/EDITION/ (--out). That directory is emptied
# first; with --only NAME just NAME is built and only its old .debs go.
# --skip leaves the named packages out (the test repositories skip the
# 1.3 GB model, brainsyncos-model).
# --key is the repository signing key (default config/repo-signing-key.asc,
# from scripts/make-repo-key.sh); the keyring package takes its public part.
#
# Each packages/NAME/build.sh runs with the current directory set to its own
# folder and gets:
#   BSOS_PKG_OUT      directory its .debs go to (build_deb in scripts/lib.sh)
#   BSOS_PKG_VERSION  BRAINSYNCOS_VERSION (config/brainsyncos.env)
#   BSOS_EDITION      jetson or vm
#   BSOS_REPO_KEY     the --key file
#   BSOS_JOBS         parallel jobs (config/build.env, set by builder.sh)
# and must leave at least one .deb, all for arm64 or all, whose Package names
# start with PKG_NAME (package.env). BSOS_PKG_OUT is on disk, under the
# output directory, not on /scratch: a package can be larger than the tmpfs.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config
load_product_config
load_packages_config

edition=""
only=""
skip=""
key="$REPO_SECRET_KEY"
out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --edition) edition="$(parse_edition "${2:-}")"; shift 2 ;;
        --only) only="${2:?--only needs a package name}"; shift 2 ;;
        --skip) skip="${2:?--skip needs package names}"; shift 2 ;;
        --key) key="${2:?--key needs a file}"; shift 2 ;;
        --out) out="${2:?--out needs a directory}"; shift 2 ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[ -n "$edition" ] || die "--edition jetson|vm is required"
out="${out:-$DEBS_DIR/$edition}"
case "$out" in /*) ;; *) out="$PWD/$out" ;; esac   # build.sh runs in its package folder
require_container
require_cmd dpkg-deb gpg md5sum
[ -s "$key" ] || die "no repository signing key at ${key#$BRAINSYNCOS_ROOT/} (create one: scripts/build.sh key)"
key="$(real_path "$key")"   # build.sh runs in its package folder

# The package folders this run builds, in name order.
names=""
for env in "$PACKAGES_DIR"/*/package.env; do
    [ -e "$env" ] || continue
    name="$(basename "$(dirname "$env")")"
    [ -z "$only" ] || [ "$name" = "$only" ] || continue
    case ",$skip," in *,"$name",*) log "$name: skipped (--skip)"; continue ;; esac
    names="$names $name"
done
[ -n "$names" ] || die "no package ${only:+named $only }in ${PACKAGES_DIR#$BRAINSYNCOS_ROOT/}/"

mkdir -p "$out"
if [ -n "$only" ]; then
    rm_released "$out/${only}"_*.deb
else
    rm_released "$out"/*.deb
fi

for name in $names; do
    dir="$PACKAGES_DIR/$name"
    # Each package.env is loaded in a subshell, so no PKG_* value leaks into the next.
    (
        load_env_file "$dir/package.env" "PKG_NAME PKG_EDITIONS"
        [ "$PKG_NAME" = "$name" ] || die "$dir/package.env: PKG_NAME=$PKG_NAME, but the folder is $name"
        for e in $(printf '%s\n' "$PKG_EDITIONS" | tr ',' ' '); do
            case "$e" in all|jetson|vm) ;; *) die "$dir/package.env: PKG_EDITIONS has '$e' (all, jetson or vm)" ;; esac
        done
        case ",$PKG_EDITIONS," in
            *,all,*|*,"$edition",*) ;;
            *) log "$name: not built for $edition (PKG_EDITIONS=$PKG_EDITIONS)"; exit 0 ;;
        esac
        [ -x "$dir/build.sh" ] || die "$dir/build.sh is missing or not executable"
        stage="$out/.build-$name"
        force_rm_tree "$stage"
        mkdir -p "$stage/out"
        log "$name: building for $edition"
        (cd "$dir" && BSOS_PKG_OUT="$stage/out" BSOS_PKG_VERSION="$BRAINSYNCOS_VERSION" \
            BSOS_EDITION="$edition" BSOS_REPO_KEY="$key" BSOS_JOBS="${BSOS_JOBS:-1}" \
            ./build.sh) || die "$name: build.sh failed"
        found=0
        for deb in "$stage/out"/*.deb; do
            [ -e "$deb" ] || continue
            pkg="$(dpkg-deb -f "$deb" Package)"
            arch="$(dpkg-deb -f "$deb" Architecture)"
            case "$pkg" in "$PKG_NAME"|"$PKG_NAME"-*) ;; *) die "$name: built $pkg, which does not start with $PKG_NAME" ;; esac
            case "$arch" in arm64|all) ;; *) die "$name: $pkg is for $arch, not arm64 or all" ;; esac
            log "$name: $(basename "$deb") ($(file_size "$deb") bytes)"
            rm_released "$out/${pkg}"_*.deb   # an older build of this package (--only)
            mv "$deb" "$out/"
            found=$((found + 1))
        done
        force_rm_tree "$stage"
        [ "$found" -gt 0 ] || die "$name: build.sh produced no .deb"
    ) || exit 1
done
log "packages for $edition in ${out#$BRAINSYNCOS_ROOT/}: $(find "$out" -maxdepth 1 -name '*.deb' | wc -l | tr -d ' ') .debs"
