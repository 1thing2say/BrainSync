#!/usr/bin/env bash
# build.sh - one entry point for building BrainSyncOS (host side).
#
# Usage: scripts/build.sh COMMAND [--edition jetson|vm] [ISO OPTIONS...]
#
#   doctor     check everything a build of the edition needs, and say what to do
#   key        create the package repository signing key (once; kept in config/)
#   wheels [--force]  build the Python wheels BrainSyncOS compiles (the
#              BrainFlow Cerelog fork; same for both editions) -> build/wheels/
#   llama [--force]   build llama.cpp for aarch64 (compiled in the amd64
#              cross-builder, checked in the arm64 builder; same for both
#              editions) -> build/llama/
#   packages   build the edition's packages        -> build/debs/EDITION/
#   mirror     fetch the third-party .debs the edition's repository mirrors
#              (NVIDIA's CUDA runtime, Jetson only; mirror/NAME/, pinned by
#              sha256)                             -> build/downloads/mirror/
#   repo       sign them into an apt repository    -> build/repo/EDITION/
#   iso        build the installer ISO from it     -> build/out/ (ISO OPTIONS are
#              passed to scripts/build-iso.sh or build-vm-iso.sh, e.g. --name)
#   all        doctor + wheels + llama + packages + mirror + repo + iso (vm: fetches
#              Ubuntu's ISO first; wheels and llama are skipped while up to date)
#   test [SUITE...]   run test suites: phase1 phase2 phase3 vm (default phase3)
#   clean [--all]     delete build/out (--all: all of build/), as builder.sh clean
#
# --edition defaults to jetson (the Orin Nano ISO); vm is the UTM edition.
# Everything heavy runs in the arm64 builder container (scripts/builder.sh),
# capped by config/build.env (2 CPUs by default). Same as `make` in
# brainsyncos/ (see Makefile).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config
load_product_config
load_packages_config

S="$BRAINSYNCOS_ROOT/scripts"
B="$S/builder.sh"

cmd="${1:-}"
[ -n "$cmd" ] || { usage_from_header; exit 2; }
shift
case "$cmd" in -h|--help|help) usage_from_header; exit 0 ;; esac

edition=jetson
rest=""
nrest=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --edition) edition="$(parse_edition "${2:-}")"; shift 2 ;;
        --edition=*) edition="$(parse_edition "${1#--edition=}")"; shift ;;
        *) rest="$rest $1"; nrest=$((nrest + 1)); shift ;;
    esac
done

# --- doctor ---------------------------------------------------------------------
nbad=0
ok()  { printf '  ok       %s\n' "$*"; }
bad() { printf '  MISSING  %s\n' "$*"; nbad=$((nbad + 1)); }
note() { printf '  note     %s\n' "$*"; }

c_doctor() {
    local iso keys hash avail_kb need probe mode m
    echo "BrainSyncOS $BRAINSYNCOS_VERSION, $edition edition: build prerequisites"
    if ! command -v docker > /dev/null 2>&1; then
        bad "docker: not installed (Docker Desktop on Apple Silicon is the reference host)"
    elif ! docker info > /dev/null 2>&1; then
        bad "docker: installed but the daemon is not running (start Docker Desktop: open -a Docker)"
    else
        ok "docker: $(docker version --format '{{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})' 2>/dev/null)"
        if docker image inspect "$("$B" image)" > /dev/null 2>&1; then
            ok "builder image: $("$B" image)"
        else
            note "builder image $("$B" image) is built on first use (~1 min)"
        fi
    fi
    if [ "$edition" = jetson ]; then
        if iso="$(resolve_base_iso 2>/dev/null)"; then
            if [ "$(file_size "$iso")" = "$BASE_ISO_SIZE" ]; then
                ok "base ISO: $iso (size matches the pin; the build checks its sha256)"
            else
                bad "base ISO: $iso is $(file_size "$iso") bytes, the pin is $BASE_ISO_SIZE (config/base-iso.env)"
            fi
        else
            bad "base ISO: $BASE_ISO_NAME not found (download JetPack $JETPACK_RELEASE's Jetson ISO; set BASE_ISO=/path/to/it if it is not in ~/Downloads)"
        fi
    else
        (
            load_vm_config
            if [ -f "$DOWNLOADS_DIR/$BASE_ISO_NAME" ]; then
                ok "VM base ISO: build/downloads/$BASE_ISO_NAME"
            else
                note "VM base ISO: not downloaded yet; 'all' fetches it (scripts/fetch-vm-base.sh, ~3 GB)"
            fi
        )
    fi
    probe="$(mktemp "$BRAINSYNCOS_ROOT/config/.mode-probe.XXXXXX")"
    chmod 600 "$probe"
    mode="$(ls -l "$probe" | cut -c2-10)"
    rm -f "$probe"
    if [ "$mode" = rw------- ]; then
        ok "file modes: kept (the repository key stays private)"
    else
        bad "file modes: this checkout's filesystem ignores chmod ($mode after chmod 600), so the repository key would be readable by everyone (WSL: clone under ~, not /mnt/c)"
    fi
    keys="$BRAINSYNCOS_ROOT/config/authorized_keys"
    hash="$BRAINSYNCOS_ROOT/config/brainsync-password.hash"
    if [ -s "$keys" ] || [ -s "$hash" ]; then
        ok "logins: $( [ -s "$keys" ] && printf 'SSH keys (config/authorized_keys) ')$( [ -s "$hash" ] && printf 'console password (config/brainsync-password.hash)')"
    else
        bad "logins: none (cp ~/.ssh/id_ed25519.pub config/authorized_keys, and/or scripts/make-password-hash.sh)"
    fi
    if [ -s "$REPO_SECRET_KEY" ]; then
        ok "repository key: config/repo-signing-key.asc"
    else
        bad "repository key: none (scripts/build.sh key, once; then back it up)"
    fi
    if [ -d "$REPO_DIR/$edition/dists" ] && repo_edition_ok "$REPO_DIR/$edition" "$edition"; then
        ok "package repository: build/repo/$edition ($(find "$REPO_DIR/$edition/pool" -name '*.deb' | wc -l | tr -d ' ') packages)"
    else
        note "package repository: not built yet (scripts/build.sh packages and repo, or all)"
    fi
    for m in $(mirror_names "$edition"); do
        if [ -s "$MIRROR_CACHE/$m/InRelease" ]; then
            ok "mirror $m: build/downloads/mirror/$m ($(find "$MIRROR_CACHE/$m" -name '*.deb' | wc -l | tr -d ' ') .debs; the repo build checks them)"
        else
            note "mirror $m: not fetched yet (scripts/build.sh mirror; 'all' fetches it)"
        fi
    done
    if [ -s "$BUILD_DIR/llama/check.env" ]; then
        ok "llama.cpp: build/llama (checked; for brainsyncos-llama)"
    else
        note "llama.cpp: not built yet (scripts/build.sh llama; the cross-builder image is built on first use)"
    fi
    if [ -s "$BUILD_DIR/wheels/SHA256SUMS" ]; then
        ok "wheels: build/wheels ($(wc -l < "$BUILD_DIR/wheels/SHA256SUMS" | tr -d ' '); for the BrainSync venv)"
    else
        note "wheels: not built yet (scripts/build.sh wheels; the BrainFlow fork, for the BrainSync venv)"
    fi
    # The ISO build's need (iso_need_gb), plus the Ubuntu download for a vm build without it.
    if [ "$edition" = jetson ]; then
        need="$(iso_need_gb "$BASE_ISO_SIZE" "$REPO_DIR/$edition")"
    else
        need="$(load_vm_config; n="$(iso_need_gb "$BASE_ISO_SIZE" "$REPO_DIR/$edition")"
            [ -f "$DOWNLOADS_DIR/$BASE_ISO_NAME" ] || n=$((n + BASE_ISO_SIZE / 1073741824 + 1)); echo "$n")"
    fi
    avail_kb="$(df -Pk "$BRAINSYNCOS_ROOT" | awk 'NR==2 {print $4}')"
    if [ "$avail_kb" -ge $((need * 1024 * 1024)) ]; then
        ok "disk: $((avail_kb / 1024 / 1024)) GiB free (this build needs about $need)"
    else
        bad "disk: only $((avail_kb / 1024 / 1024)) GiB free, this build needs about $need (scripts/build.sh clean)"
    fi
    command -v make > /dev/null 2>&1 || note "make: not installed; use scripts/build.sh directly (tests/test_phase3.sh needs make)"
    (load_build_config; ok "resources: $BUILD_CPUS CPUs, $BUILD_JOBS jobs, $BUILD_MEMORY memory, $BUILD_SCRATCH_SIZE /scratch (config/build.env)")
    echo
    if [ "$nbad" -gt 0 ]; then
        echo "$nbad missing; fix them, then: scripts/build.sh all --edition $edition"
        return 1
    fi
    echo "ready: scripts/build.sh all --edition $edition"
}

# --- build steps ------------------------------------------------------------------
c_key() {
    if [ -s "$REPO_SECRET_KEY" ]; then
        log "repository key exists: config/repo-signing-key.asc (scripts/builder.sh run scripts/make-repo-key.sh --force replaces it)"
        return 0
    fi
    "$B" run scripts/make-repo-key.sh
}

c_packages() { "$B" run scripts/build-packages.sh --edition "$edition"; }
c_mirror()   { "$B" run scripts/fetch-mirror.sh --edition "$edition"; }
c_wheels() {
    case "$rest" in ''|' --force') ;; *) die "wheels takes only --force:$rest" ;; esac
    # shellcheck disable=SC2086  # [--force], split as given
    "$B" run scripts/build-wheels.sh $rest
}
c_repo()     { "$B" run scripts/build-repo.sh --edition "$edition"; }
c_llama() {
    case "$rest" in ''|' --force') ;; *) die "llama takes only --force:$rest" ;; esac
    # shellcheck disable=SC2086  # [--force], split as given
    "$B" --cross run scripts/build-llama.sh compile $rest
    # shellcheck disable=SC2086
    "$B" run scripts/build-llama.sh check $rest
}

c_iso() {
    # shellcheck disable=SC2086  # ISO OPTIONS, split as given
    if [ "$edition" = jetson ]; then
        "$B" run scripts/build-iso.sh $rest
    else
        "$B" run scripts/build-vm-iso.sh $rest
    fi
}

c_all() {
    c_doctor || die "prerequisites missing (see above)"
    [ "$edition" = jetson ] || "$S/fetch-vm-base.sh"
    "$B" run scripts/build-wheels.sh   # skipped while up to date; brainsync-runtime needs them
    "$B" --cross run scripts/build-llama.sh compile   # both skipped while up to date; brainsyncos-llama needs them
    "$B" run scripts/build-llama.sh check
    c_packages
    c_mirror   # offline once build/downloads/mirror holds the pinned .debs
    c_repo
    c_iso
}

c_test() {
    local suite suites="${rest:- phase3}" t
    for suite in $suites; do
        case "$suite" in
            phase1|phase2|phase3|vm) t="$BRAINSYNCOS_ROOT/tests/test_$suite.sh" ;;
            *) die "unknown test suite: $suite (phase1 phase2 phase3 vm)" ;;
        esac
        log "== tests/test_$suite.sh"
        "$t"
    done
}

no_args() { [ "$nrest" = 0 ] || die "$cmd takes no arguments besides --edition:$rest"; }

# shellcheck disable=SC2086  # [--all], split as given
c_clean() { "$B" clean $rest; }

case "$cmd" in
    doctor) no_args; c_doctor ;;
    key) no_args; c_key ;;
    wheels) c_wheels ;;
    llama) c_llama ;;
    packages) no_args; c_packages ;;
    mirror) no_args; c_mirror ;;
    repo) no_args; c_repo ;;
    iso) c_iso ;;
    all) c_all ;;
    test) c_test ;;
    clean) c_clean ;;
    *) die "unknown command: $cmd (try --help)" ;;
esac
