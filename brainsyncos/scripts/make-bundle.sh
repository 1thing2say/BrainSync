#!/usr/bin/env bash
# make-bundle.sh - pack the install bundle for an Orin already on stock JetPack (builder only).
#
# Usage: scripts/builder.sh run scripts/make-bundle.sh [--repo DIR] [--out DIR] [--build-id ID]
#
# Output: build/out/brainsyncos-jetson-VERSION.tar (+ .tar.sha256), holding one
# directory brainsyncos-jetson-VERSION/ with:
#   install.sh           payload/bundle/install.sh: run as root on the Orin
#   bundle.env           what install.sh checks and the steps need (plain
#                        KEY=value): VERSION, BUILD_ID, EDITION, L4T_RELEASE,
#                        JETPACK_RELEASE, TEGRA_KERNEL (the nvidia-l4t-kernel
#                        the CH340 module depends on), KERNEL_RELEASE (its
#                        uname -r), REPO_SUITE, REPO_COMPONENT, BRAINSYNC_USER,
#                        HOSTNAME_PREFIX, REPO_KEY, PACKAGES, PACKAGE_NAMES and
#                        INSTALLED_KIB (the packages' Installed-Size, summed)
#   release              the release file, as the ISO's payload carries it
#   repo-key.gpg         the repository's public key: the install's trust anchor
#   lib/apt-repo.sh      the apt helper the steps install through
#   install.d/           the BrainSync layer only, steps 60 to 89 (today 60
#                        repository key, 70 apps, 75 USB serial, 80 CUDA, 85
#                        llama, 86 model). 00 to 50 (release, issue, hostname,
#                        account, SSH, OOBE) shape a fresh install; a running
#                        Orin keeps its own.
#   account/30-user.sh   unchanged, for install.sh to create the appliance
#                        account on request, the way the ISO install does
#   repo/                the signed jetson repository (build/repo/jetson) as it is
#
# The repository (--repo, default build/repo/jetson) must be a jetson one whose
# InRelease verifies against its own repo-key.gpg (validate_repo, as the ISO
# build checks it); otherwise nothing is written. The tar is reproducible:
# GNU format, members sorted by name, owner 0:0, modes normalized to
# u+rwX,go+rX,go-w, every mtime BSOS_SOURCE_DATE_EPOCH (builder.sh: the commit
# time, as for the .debs). It is not compressed: the pool's .debs already are,
# and the model barely compresses. BUILD_ID defaults to the host's git
# revision (BSOS_GIT_REV, set by builder.sh).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config
load_product_config
load_packages_config

repo="$REPO_DIR/jetson"
out="$OUT_DIR"
build_id="${BSOS_GIT_REV:-unknown}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) repo="${2:?--repo needs a directory}"; shift 2 ;;
        --out) out="${2:?--out needs a directory}"; shift 2 ;;
        --build-id) build_id="${2:?--build-id needs a value}"; shift 2 ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_container
require_cmd tar gpgv gpg gpgconf sha256sum du
tar --version | head -1 | grep -q 'GNU tar' || die "GNU tar is needed (--sort, --mtime, --owner)"
printf '%s\n' "$build_id" | grep -Eq '^[A-Za-z0-9._+-]+$' || die "invalid build id: $build_id"

payload="$BRAINSYNCOS_ROOT/payload"
[ -s "$payload/bundle/install.sh" ] || die "no $payload/bundle/install.sh"
[ -s "$payload/install.d/30-user.sh" ] || die "no $payload/install.d/30-user.sh"
steps="$(cd "$payload/install.d" && LC_ALL=C ls [6-8][0-9]-*.sh 2>/dev/null | tr '\n' ' ')"
[ -n "$steps" ] || die "no install.d steps numbered 60 to 89 in $payload/install.d"
kernel_release="$(sed -n 's/^KERNEL_RELEASE=//p' "$PACKAGES_DIR/brainsyncos-ch341-tegra/package.env")"
[ -n "$kernel_release" ] || die "no KERNEL_RELEASE in packages/brainsyncos-ch341-tegra/package.env"

# The repository: a signed jetson one, or nothing is written (sets REPO_KEY_FPR).
validate_repo "$repo" jetson
repo="$(cd "$repo" && pwd -P)"
index="$repo/dists/$REPO_SUITE/$REPO_COMPONENT/binary-arm64/Packages"
[ -s "$index" ] || die "no package index at $index"
packages="$(grep -c '^Package: ' "$index")" || true
[ "$packages" -gt 0 ] || die "$index lists no packages"
names="$(sed -n 's/^Package: //p' "$index" | LC_ALL=C sort -u | tr '\n' ',' | sed 's/,$//')"
installed_kib="$(awk '/^Installed-Size: / {s += $2} END {printf "%d", s}' "$index")"
repo_kb="$(du -sk "$repo" | awk '{print $1}')"

name="brainsyncos-jetson-$BRAINSYNCOS_VERSION"
stage="$TMP_DIR/bundle/$name"
force_rm_tree "$TMP_DIR/bundle"
mkdir -p "$stage/install.d" "$stage/account"
cp "$payload/bundle/install.sh" "$stage/install.sh"
chmod 755 "$stage/install.sh"
cp -R "$payload/lib" "$stage/lib"
for s in $steps; do cp "$payload/install.d/$s" "$stage/install.d/"; done
cp "$payload/install.d/30-user.sh" "$stage/account/30-user.sh"
cp "$repo/repo-key.gpg" "$stage/repo-key.gpg"
cat > "$stage/release" <<REL
NAME=BrainSyncOS
VERSION=$BRAINSYNCOS_VERSION
BUILD_ID=$build_id
EDITION=$(jetson_edition)
BOARD=$TARGET_BOARD
L4T_RELEASE=$L4T_RELEASE
JETPACK_RELEASE=$JETPACK_RELEASE
UBUNTU_RELEASE=$UBUNTU_RELEASE
REPO_KEY=$REPO_KEY_FPR
BUNDLE=jetson
REL
cat > "$stage/bundle.env" <<ENV
VERSION=$BRAINSYNCOS_VERSION
BUILD_ID=$build_id
EDITION=$(jetson_edition)
BOARD=$TARGET_BOARD
L4T_RELEASE=$L4T_RELEASE
JETPACK_RELEASE=$JETPACK_RELEASE
UBUNTU_RELEASE=$UBUNTU_RELEASE
TEGRA_KERNEL=$TEGRA_KERNEL
KERNEL_RELEASE=$kernel_release
REPO_SUITE=$REPO_SUITE
REPO_COMPONENT=$REPO_COMPONENT
BRAINSYNC_USER=$BRAINSYNC_USER
HOSTNAME_PREFIX=$HOSTNAME_PREFIX
REPO_KEY=$REPO_KEY_FPR
PACKAGES=$packages
PACKAGE_NAMES=$names
INSTALLED_KIB=$installed_kib
ENV
# Both files must stay plain KEY=value (00-release.sh and install.sh check that).
load_env_file "$stage/release" "NAME VERSION EDITION REPO_KEY" > /dev/null
load_env_file "$stage/bundle.env" "VERSION L4T_RELEASE TEGRA_KERNEL KERNEL_RELEASE REPO_SUITE REPO_COMPONENT BRAINSYNC_USER INSTALLED_KIB" > /dev/null
# The repository is not copied: tar follows this link (--dereference).
ln -s "$repo" "$stage/repo"

epoch="${BSOS_SOURCE_DATE_EPOCH:-}"
if [ -z "$epoch" ]; then
    epoch="$(date +%s)"
    warn "BSOS_SOURCE_DATE_EPOCH is not set: file times are now, so this tar is not reproducible"
fi
mkdir -p "$out"
check_free_space "$out" $((repo_kb / 1024 / 1024 + 1))
tarball="$out/$name.tar"
partial="$out/.$name.tar.partial"
rm_released "$partial" "$tarball" "$tarball.sha256"
# The repo link goes first, so the cleanup never follows it into the repository.
# shellcheck disable=SC2064  # expand now: local paths
trap "rm_released '$partial'; rm -f '$stage/repo'; force_rm_tree '$TMP_DIR/bundle'" EXIT
log "writing $tarball ($steps; $packages packages, $(bytes_gb $((repo_kb * 1024))) GB repository)"
LC_ALL=C tar --format=gnu --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime="@$epoch" --mode='u+rwX,go+rX,go-w' --dereference \
    -C "$(dirname "$stage")" -cf "$partial" "$name" || die "tar failed"

# What was written: the members the Orin's install.sh needs, and nothing from
# the fresh-install steps.
members="$(LC_ALL=C tar -tf "$partial")"
for m in install.sh bundle.env release repo-key.gpg lib/apt-repo.sh account/30-user.sh \
        "repo/dists/$REPO_SUITE/InRelease" repo/repo-key.gpg repo/edition; do
    printf '%s\n' "$members" | grep -qx "$name/$m" || die "the tar lacks $name/$m"
done
for s in $steps; do
    printf '%s\n' "$members" | grep -qx "$name/install.d/$s" || die "the tar lacks install.d/$s"
done
! printf '%s\n' "$members" | grep -q "^$name/install.d/[0-5][0-9]-" || die "the tar holds a fresh-install step (00-59)"
[ "$(printf '%s\n' "$members" | grep -c "^$name/repo/pool/.*\.deb$")" = "$packages" ] \
    || die "the tar holds $(printf '%s\n' "$members" | grep -c "^$name/repo/pool/.*\.deb$") .debs, the index lists $packages"
mv "$partial" "$tarball"
(cd "$out" && sha256sum "$name.tar" > "$name.tar.sha256")
trap - EXIT
rm -f "$stage/repo"
force_rm_tree "$TMP_DIR/bundle"
log "done: $tarball ($(file_size "$tarball") bytes, $(printf '%s\n' "$members" | wc -l | tr -d ' ') members, sha256 $(cut -d' ' -f1 "$tarball.sha256"))"
log "packages: $names; Installed-Size $((installed_kib / 1024)) MiB; key $REPO_KEY_FPR; mtime @$epoch"
