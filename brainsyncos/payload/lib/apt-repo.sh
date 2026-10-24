# shellcheck shell=bash
# apt-repo.sh - install packages from the BrainSyncOS package repository. Source it
# from an install.d step (bash, inside the installed system); do not execute it.
#
# hook.sh mounts the ISO's signed repository read-only at $BSOS_REPO_DIR
# (/tmp/brainsyncos-repo) inside the target. bsos_repo_open points apt at that
# repository alone, with its package lists in a private directory: the
# target's own sources and lists are neither used nor changed, and nothing
# goes to the network. apt must accept the repository's signature against the
# build's key ($BSOS_INSTALL_DIR/repo-key.gpg): apt-get update runs with
# --error-on=any, so a missing, unsigned or tampered repository fails the
# step instead of printing a warning. dpkg runs without a pty (the installer's
# chroot has no /dev/pts). Check-Date is off for this source only:
# a Jetson without an RTC battery can start the installer with a wrong clock,
# and the signature check does not depend on it.
#
#   bsos_repo_open        set up the private apt configuration and update it
#   bsos_apt ARGS...      apt-get ARGS with it (noninteractive, -y, no recommends,
#                         needrestart suspended)
#   bsos_repo_close       remove the private state (also runs on exit)
# Sets REPO_SUITE and REPO_COMPONENT (settings.env) and BSOS_APT_DIR.

bsos_repo_open() {
    local d="${BSOS_INSTALL_DIR:?}" repo="${BSOS_REPO_DIR:-/tmp/brainsyncos-repo}"
    # shellcheck disable=SC1091
    . "$d/settings.env"
    if [ ! -d "$repo/dists/${REPO_SUITE:?}" ]; then
        echo "apt-repo.sh: no BrainSyncOS package repository at $repo" >&2
        return 1
    fi
    if [ ! -s "$d/repo-key.gpg" ]; then
        echo "apt-repo.sh: the payload has no repository key ($d/repo-key.gpg)" >&2
        return 1
    fi
    BSOS_APT_DIR="$(mktemp -d /tmp/bsos-apt.XXXXXX)"
    trap bsos_repo_close EXIT
    mkdir -p "$BSOS_APT_DIR/sources" "$BSOS_APT_DIR/lists/partial" "$BSOS_APT_DIR/archives/partial"
    cat > "$BSOS_APT_DIR/sources/brainsyncos.sources" <<EOF
Types: deb
URIs: file:$repo
Suites: $REPO_SUITE
Components: ${REPO_COMPONENT:?}
Signed-By: $d/repo-key.gpg
Check-Date: no
EOF
    BSOS_APT_OPTS=(
        -o Dir::Etc::SourceList=/dev/null
        -o "Dir::Etc::SourceParts=$BSOS_APT_DIR/sources"
        -o "Dir::State::Lists=$BSOS_APT_DIR/lists"
        -o "Dir::Cache::Archives=$BSOS_APT_DIR/archives"
        -o Dir::Cache::pkgcache=
        -o Dir::Cache::srcpkgcache=
        -o APT::Sandbox::User=root
        -o Dpkg::Use-Pty=0
    )
    bsos_apt update --error-on=any
}

# needrestart (Ubuntu's apt hook) must not try to restart services from the installer's chroot.
bsos_apt() {
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 \
        apt-get "${BSOS_APT_OPTS[@]}" -y -q --no-install-recommends "$@"
}

bsos_repo_close() {
    if [ -n "${BSOS_APT_DIR:-}" ]; then
        rm -rf "$BSOS_APT_DIR"
        BSOS_APT_DIR=""
    fi
}
