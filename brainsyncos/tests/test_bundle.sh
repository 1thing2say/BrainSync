#!/usr/bin/env bash
# test_bundle.sh - check the install bundle for an Orin already on stock JetPack (host side).
#
# Usage: BASE_ISO=/path/to/base.iso tests/test_bundle.sh [--keep]
#
# scripts/make-bundle.sh and payload/bundle/install.sh, in the style of
# tests/test_phase3.sh. The repository: build/repo/jetson when it exists (the
# real one, with the mirrored CUDA runtime and the model; it says so), else a
# throwaway one from testlib's test_repo (a key, every jetson package but the
# model, the mirror; slow: it builds the packages). Checks:
#   syntax           bash -n on the four scripts; both --help texts
#   bundle-build     make-bundle.sh writes the tar and its .sha256; built again,
#                    the same bytes (reproducible)
#   bundle-contents  from the host: the member list (install.sh, bundle.env,
#                    release, the key, lib, steps 60 to 86 and no other,
#                    account/30-user.sh, the repository), the .sha256, the key
#                    byte-identical to the repository's, release and bundle.env fields
#   bundle-tar       in the builder (tests/bundle-checks.sh tar): GNU format,
#                    0/0, one mtime, the unpacked repository validates
#   bundle-refusals  make-bundle.sh refuses a missing repository, a tampered
#                    InRelease and a vm repository, and leaves no output
#   bundle-install   in the builder (tests/bundle-checks.sh install): install.sh
#                    in NVIDIA's real target base layer (needs the base ISO and
#                    a repository with the CUDA runtime)
#   shellcheck       the four scripts (koalaman/shellcheck:v0.10.0, offline)
# Everything goes to build/tmp/bundle-test/ (deleted at the end unless --keep;
# not build/tmp/bundle/, which make-bundle.sh stages in and deletes).
# Per-check logs: build/test-logs/bundle/. BSOS_TEST_ONLY=NAME,... runs only
# those checks (tests/testlib.sh).

BSOS_SCRIPT=test_bundle.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"
load_config
load_product_config
load_packages_config

keep=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --keep) keep=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

S="$BRAINSYNCOS_ROOT/scripts"
B="$S/builder.sh"
LOGS="$BUILD_DIR/test-logs/bundle"
TD="build/tmp/bundle-test"     # relative to brainsyncos/ (= /work in the builder); not make-bundle.sh's build/tmp/bundle
NAME="brainsyncos-jetson-$BRAINSYNCOS_VERSION"
TAR="$BRAINSYNCOS_ROOT/$TD/out/$NAME.tar"
SHELLCHECK_IMAGE=koalaman/shellcheck:v0.10.0
FILES="scripts/make-bundle.sh payload/bundle/install.sh tests/test_bundle.sh tests/bundle-checks.sh"
mkdir -p "$LOGS"
rm -f "${LOGS:?}"/*.log
# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"

cleanup() {
    [ "$keep" = 1 ] || force_rm_tree "$BRAINSYNCOS_ROOT/$TD"
}
trap cleanup EXIT

in_builder() { "$B" run tests/bundle-checks.sh "$1" "$TD"; }

# expect_fail PATTERN CMD...: CMD fails and its output matches PATTERN.
expect_fail() {
    local pat="$1" out
    shift
    if out="$("$@" 2>&1)"; then die "accepted: $*"; fi
    printf '%s\n' "$out" | grep -Eq -e "$pat" || die "refused for the wrong reason ($*): $out"
    echo "refuses ${*##*/}: $(printf '%s\n' "$out" | grep -Em1 -e "$pat")"
}

# The repository the bundle is built from (relative to brainsyncos/).
force_rm_tree "$BRAINSYNCOS_ROOT/$TD"
mkdir -p "$BRAINSYNCOS_ROOT/$TD"
if [ -f "$REPO_DIR/jetson/dists/$REPO_SUITE/InRelease" ]; then
    REPO="build/repo/jetson"
    log "repository: build/repo/jetson (the real one, $(find "$REPO_DIR/jetson/pool" -name '*.deb' | wc -l | tr -d ' ') packages)"
else
    REPO="$TD/tr/repo"
    log "repository: no build/repo/jetson; building a throwaway one in $TD/tr (slow: every jetson package but the model)"
fi
printf '%s\n' "$REPO" > "$BRAINSYNCOS_ROOT/$TD/repo-path"

t_syntax() {
    local f
    for f in $FILES; do
        /bin/bash -n "$BRAINSYNCOS_ROOT/$f" || die "bash -n: $f"
    done
    /bin/bash "$BRAINSYNCOS_ROOT/payload/bundle/install.sh" --help | grep -q '^Usage, as root' || die "install.sh --help"
    "$S/make-bundle.sh" --help | grep -q '^Usage: scripts/builder.sh run scripts/make-bundle.sh' || die "make-bundle.sh --help"
    expect_fail 'run this inside the builder' "$S/make-bundle.sh"
    echo "bash -n clean: $FILES; both --help texts; make-bundle.sh refuses to run outside the builder"
}

t_build() {
    local sum1 sum2
    [ -f "$BRAINSYNCOS_ROOT/$REPO/dists/$REPO_SUITE/InRelease" ] || test_repo jetson "$TD/tr"
    "$B" run scripts/make-bundle.sh --repo "$REPO" --out "$TD/out"
    [ -s "$TAR" ] && [ -s "$TAR.sha256" ] || die "no $TAR or its .sha256"
    sum1="$(sha256_of "$TAR")"
    [ "$sum1" = "$(cut -d' ' -f1 "$TAR.sha256")" ] || die ".sha256 does not match the tar"
    [ ! -e "$BRAINSYNCOS_ROOT/$TD/out/.$NAME.tar.partial" ] || die "the partial file was left"
    "$B" run scripts/make-bundle.sh --repo "$REPO" --out "$TD/out2" > /dev/null 2>&1
    sum2="$(sha256_of "$BRAINSYNCOS_ROOT/$TD/out2/$NAME.tar")"
    [ "$sum1" = "$sum2" ] || die "two builds differ: $sum1 vs $sum2 (not reproducible)"
    force_rm_tree "$BRAINSYNCOS_ROOT/$TD/out2"
    echo "$NAME.tar: $(file_size "$TAR") bytes, sha256 $sum1, the same bytes on a second build"
}

t_contents() {
    local m x="$TMP_DIR/bundle-x" fpr
    mkdir -p "$TMP_DIR"
    tar -tf "$TAR" > "$TMP_DIR/bundle.list"
    for m in install.sh bundle.env release repo-key.gpg lib/apt-repo.sh account/30-user.sh \
            install.d/60-apt-repo.sh install.d/70-brainsync.sh install.d/75-usb-serial.sh install.d/80-cuda.sh \
            install.d/85-llama.sh install.d/86-model.sh "repo/dists/$REPO_SUITE/InRelease" "repo/dists/$REPO_SUITE/Release.gpg" \
            "repo/dists/$REPO_SUITE/$REPO_COMPONENT/binary-arm64/Packages" repo/repo-key.gpg repo/edition; do
        grep -qx "$NAME/$m" "$TMP_DIR/bundle.list" || die "the tar lacks $NAME/$m"
    done
    [ "$(grep -c "^$NAME/install.d/" "$TMP_DIR/bundle.list")" = 7 ] \
        || die "install.d holds other members: $(grep "^$NAME/install.d/" "$TMP_DIR/bundle.list" | tr '\n' ' ')"
    ! grep -q '/hook\.sh$' "$TMP_DIR/bundle.list" || die "hook.sh is in the tar"
    ! grep -Eq "^$NAME/install.d/[0-5][0-9]-" "$TMP_DIR/bundle.list" || die "a fresh-install step is in the tar"
    ! grep -Evq "^$NAME(/|$)" "$TMP_DIR/bundle.list" || die "members outside $NAME/: $(grep -Ev "^$NAME(/|$)" "$TMP_DIR/bundle.list" | head -3)"
    [ "$(grep -c "^$NAME/repo/pool/.*\.deb$" "$TMP_DIR/bundle.list")" = "$(find "$BRAINSYNCOS_ROOT/$REPO/pool" -name '*.deb' | wc -l | tr -d ' ')" ] \
        || die "the tar's pool differs from the repository's"
    force_rm_tree "$x"
    mkdir -p "$x"
    tar -xf "$TAR" -C "$x" "$NAME/install.sh" "$NAME/bundle.env" "$NAME/release" "$NAME/repo-key.gpg"
    cmp "$x/$NAME/repo-key.gpg" "$BRAINSYNCOS_ROOT/$REPO/repo-key.gpg" || die "the tar's key is not the repository's"
    [ -x "$x/$NAME/install.sh" ] || die "install.sh is not executable in the tar"
    cmp "$x/$NAME/install.sh" "$BRAINSYNCOS_ROOT/payload/bundle/install.sh" || die "install.sh differs from payload/bundle/"
    /bin/bash -c 'source "$1/scripts/lib.sh"; load_env_file "$2" "NAME VERSION BUILD_ID EDITION L4T_RELEASE REPO_KEY BUNDLE"
        [ "$NAME" = BrainSyncOS ] && [ "$EDITION" = orin-nano ] && [ "$BUNDLE" = jetson ] || exit 1' _ "$BRAINSYNCOS_ROOT" "$x/$NAME/release" \
        || die "release: wrong fields: $(cat "$x/$NAME/release")"
    /bin/bash -c 'source "$1/scripts/lib.sh"; load_env_file "$2" "VERSION EDITION L4T_RELEASE JETPACK_RELEASE TEGRA_KERNEL KERNEL_RELEASE REPO_SUITE REPO_COMPONENT BRAINSYNC_USER HOSTNAME_PREFIX REPO_KEY PACKAGES PACKAGE_NAMES INSTALLED_KIB"
        [ "$VERSION" = "$3" ] && [ "$L4T_RELEASE" = "$4" ] && [ "$TEGRA_KERNEL" = "$5" ] && [ "$INSTALLED_KIB" -gt 0 ] && [ "$PACKAGES" -gt 0 ] || exit 1' \
        _ "$BRAINSYNCOS_ROOT" "$x/$NAME/bundle.env" "$BRAINSYNCOS_VERSION" "$L4T_RELEASE" "$TEGRA_KERNEL" \
        || die "bundle.env: wrong fields: $(cat "$x/$NAME/bundle.env")"
    fpr="$(sed -n 's/^REPO_KEY=//p' "$x/$NAME/bundle.env")"
    [ "$fpr" = "$(sed -n 's/^REPO_KEY=//p' "$x/$NAME/release")" ] && printf '%s\n' "$fpr" | grep -Eq '^[0-9A-F]{40}$' || die "REPO_KEY: $fpr"
    cat "$x/$NAME/bundle.env"
    force_rm_tree "$x"
    echo "members: $(wc -l < "$TMP_DIR/bundle.list" | tr -d ' ') under $NAME/; steps 60-86 only, no hook.sh; the key is the repository's; release and bundle.env OK"
}

t_refusals() {
    local bad="$TD/badrepo"
    expect_fail 'no BrainSyncOS package repository at' "$B" run scripts/make-bundle.sh --repo "$TD/none" --out "$TD/x"
    # dists and the key only (no pool): validate_repo checks the signature first.
    force_rm_tree "$BRAINSYNCOS_ROOT/$bad"
    mkdir -p "$BRAINSYNCOS_ROOT/$bad"
    cp -R "$BRAINSYNCOS_ROOT/$REPO/dists" "$BRAINSYNCOS_ROOT/$bad/dists"
    cp "$BRAINSYNCOS_ROOT/$REPO/repo-key.gpg" "$BRAINSYNCOS_ROOT/$REPO/edition" "$BRAINSYNCOS_ROOT/$bad/"
    printf '\n' >> "$BRAINSYNCOS_ROOT/$bad/dists/$REPO_SUITE/InRelease"
    expect_fail 'InRelease does not verify against' "$B" run scripts/make-bundle.sh --repo "$bad" --out "$TD/x"
    cp "$BRAINSYNCOS_ROOT/$REPO/dists/$REPO_SUITE/InRelease" "$BRAINSYNCOS_ROOT/$bad/dists/$REPO_SUITE/InRelease"
    printf 'vm\n' > "$BRAINSYNCOS_ROOT/$bad/edition"
    expect_fail "built for edition 'vm', not jetson" "$B" run scripts/make-bundle.sh --repo "$bad" --out "$TD/x"
    expect_fail 'unknown argument: --fast' "$B" run scripts/make-bundle.sh --fast
    [ ! -e "$BRAINSYNCOS_ROOT/$TD/x" ] || die "a refused build left output"
    force_rm_tree "$BRAINSYNCOS_ROOT/$bad"
    echo "refused builds leave no output"
}

t_shellcheck() {
    docker image inspect "$SHELLCHECK_IMAGE" > /dev/null 2>&1 \
        || die "$SHELLCHECK_IMAGE is not available locally (docker pull $SHELLCHECK_IMAGE)"
    # shellcheck disable=SC2086
    docker run --rm --network none -v "$BRAINSYNCOS_ROOT:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" -x -S warning $FILES
    echo "shellcheck: $FILES clean"
}

log "install bundle test chain (logs: ${LOGS#$BRAINSYNCOS_ROOT/}/)"
check syntax               t_syntax
check bundle-build         t_build
if [ -s "$TAR" ]; then
    check bundle-contents  t_contents
    check bundle-tar       in_builder tar
    check bundle-refusals  t_refusals
    check bundle-install   in_builder install
else
    log "no bundle built: skipping the contents, tar, refusal and install checks"
fi
check shellcheck           t_shellcheck

finish "Install bundle"
