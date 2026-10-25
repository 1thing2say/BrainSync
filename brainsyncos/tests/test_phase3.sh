#!/usr/bin/env bash
# test_phase3.sh - check the BrainSyncOS package build and its signed repository (host side).
#
# Usage: BASE_ISO=/path/to/base.iso tests/test_phase3.sh [--keep]
#
# Checks config/build.env and config/packages.env (host bash 3.2), that the
# builder container really runs with the configured CPU and memory caps,
# scripts/build.sh and the Makefile, and the Phase 3 chain with a throwaway
# signing key (never config/repo-signing-key.asc): make-repo-key.sh, then
# build-packages.sh and build-repo.sh for both editions, the signed result,
# and refusals of bad input; each edition's packages (the Python venv
# included) must be byte-identical. In the builder (tests/phase3-checks.sh):
# the install-side apt helper against good and tampered repositories, the
# contents and dependencies of brainsync-runtime and brainsync, the md5sum
# merge of mapped trees, and hook.sh mounting the repository for
# 60-apt-repo.sh and 70-brainsync.sh in NVIDIA's real target base layer
# (bare, then with the account and espeak-ng's libraries from the pool: the
# offline voice and secrets.env); the
# board packages (brainsyncos-ch341-tegra against NVIDIA's kernel,
# brainsync-udev) and 75-usb-serial.sh installing them there; NVIDIA's CUDA
# runtime mirrored into the jetson repository (mirror/nvidia-jetson: fetched
# into build/downloads/mirror unless cached, signature and sha256 pins checked
# offline) and 80-cuda.sh installing it there; the model
# package (config/model.env pins, checked against Hugging Face when online)
# and 86-model.sh installing it there; the ISO size limit (ISO_MAX_BYTES);
# llama.cpp (build/llama from scripts/build-llama.sh, compiled in the
# cross-builder and checked in the arm64 builder unless up to date), its two
# packages and 85-llama.sh installing them there, with the service's own
# command line serving a test model to the BrainSync venv. In a test-only
# Ubuntu container with Xvfb (tests/runtime-checks.sh): the two BrainSync
# packages installed, imported, PsychoPy drawing, every command, and
# brainsync-voice serving its page offline. Then the BrainFlow wheel in
# build/wheels (built by scripts/build-wheels.sh unless up to date): its
# manifest, contents, and a venv install that reports 250 Hz. Last, every
# BrainSyncOS shell script through shellcheck (koalaman/shellcheck:v0.10.0,
# used offline).
# Builds no ISO: tests/test_phase2.sh and test_vm.sh build test ISOs that
# carry a repository and run the full hook. Everything goes to
# build/tmp/phase3/ (deleted at the end unless --keep). Per-check logs:
# build/test-logs/phase3/. BSOS_TEST_ONLY=NAME,... runs only those checks
# (tests/testlib.sh), e.g. again on a tree kept with --keep.

BSOS_SCRIPT=test_phase3.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"
source "$BRAINSYNCOS_ROOT/scripts/model.sh"

load_config
load_product_config
load_packages_config
load_build_config
load_model_config

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
LOGS="$BUILD_DIR/test-logs/phase3"
TD="build/tmp/phase3"          # relative to brainsyncos/ (= /work in the builder)
KEY="$TD/key/repo-signing-key.asc"
SHELLCHECK_IMAGE=koalaman/shellcheck:v0.10.0
mkdir -p "$LOGS"
rm -f "$LOGS"/*.log
# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"

cleanup() {
    [ "$keep" = 1 ] || force_rm_tree "$BRAINSYNCOS_ROOT/$TD"
}
trap cleanup EXIT

in_builder() { "$B" run tests/phase3-checks.sh "$1" "$TD"; }

# expect_fail PATTERN CMD...: CMD fails and its output matches PATTERN.
expect_fail() {
    local pat="$1" out
    shift
    if out="$("$@" 2>&1)"; then die "accepted: $*"; fi
    printf '%s\n' "$out" | grep -Eq -e "$pat" || die "refused for the wrong reason ($*): $out"
    echo "refuses ${*##*/}: $(printf '%s\n' "$out" | grep -Em1 -e "$pat")"
}

t_configs() {
    /bin/bash --version | head -1
    /bin/bash -c 'source "$1/scripts/lib.sh"; load_build_config; load_packages_config
        echo "build: $BUILD_CPUS CPUs, $BUILD_JOBS jobs, $BUILD_MEMORY, /scratch $BUILD_SCRATCH_SIZE; repo: $REPO_SUITE/$REPO_COMPONENT, key expiry $REPO_KEY_EXPIRE"' _ "$BRAINSYNCOS_ROOT"
    [ "$(BSOS_BUILD_CPUS=1.5 /bin/bash -c 'source "$1/scripts/lib.sh"; load_build_config; echo $BUILD_CPUS' _ "$BRAINSYNCOS_ROOT")" = 1.5 ] \
        || die "BSOS_BUILD_CPUS does not override BUILD_CPUS"
    local bad="$TMP_DIR/bad.env" kv out
    mkdir -p "$TMP_DIR"
    for kv in BUILD_CPUS=0 BUILD_CPUS=two BUILD_JOBS=0 BUILD_MEMORY=6G BUILD_MEMORY=3g BUILD_SCRATCH_SIZE=3; do
        sed "s/^${kv%%=*}=.*/$kv/" "$BSOS_BUILD_CONFIG" > "$bad"
        if out="$(BSOS_BUILD_CONFIG="$bad" /bin/bash -c 'source "$1/scripts/lib.sh"; load_build_config' _ "$BRAINSYNCOS_ROOT" 2>&1)"; then
            rm -f "$bad"; die "accepted $kv"
        fi
        printf '%s\n' "$out" | grep -q "${kv%%=*} must" || die "rejected $kv for the wrong reason: $out"
        echo "rejects $kv: ${out##*ERROR: }"
    done
    for kv in REPO_SUITE=Bad_Suite REPO_COMPONENT=-x REPO_KEY_EXPIRE=forever PACKAGE_MAINTAINER_EMAIL=nobody; do
        sed "s/^${kv%%=*}=.*/$kv/" "$BSOS_PACKAGES_CONFIG" > "$bad"
        if out="$(BSOS_PACKAGES_CONFIG="$bad" /bin/bash -c 'source "$1/scripts/lib.sh"; load_packages_config' _ "$BRAINSYNCOS_ROOT" 2>&1)"; then
            rm -f "$bad"; die "accepted $kv"
        fi
        printf '%s\n' "$out" | grep -q "${kv%%=*} must" || die "rejected $kv for the wrong reason: $out"
        echo "rejects $kv: ${out##*ERROR: }"
    done
    rm -f "$bad"
}

# The container must run with the caps of config/build.env (and an override).
t_builder_caps() {
    local cpu mem want_cpu want_mem
    "$B" build > /dev/null
    want_cpu="$(awk -v c="$BUILD_CPUS" 'BEGIN {printf "%d 100000", c * 100000}')"
    want_mem=$(( $(size_mb "$BUILD_MEMORY") * 1024 * 1024 ))
    cpu="$("$B" run cat /sys/fs/cgroup/cpu.max)"
    mem="$("$B" run cat /sys/fs/cgroup/memory.max)"
    [ "$cpu" = "$want_cpu" ] || die "cpu.max is '$cpu', want '$want_cpu' (BUILD_CPUS=$BUILD_CPUS)"
    [ "$mem" = "$want_mem" ] || die "memory.max is $mem, want $want_mem (BUILD_MEMORY=$BUILD_MEMORY)"
    echo "cpu.max $cpu, memory.max $mem"
    "$B" run bash -c 'echo "BSOS_JOBS=$BSOS_JOBS MAKEFLAGS=$MAKEFLAGS CMAKE_BUILD_PARALLEL_LEVEL=$CMAKE_BUILD_PARALLEL_LEVEL"; df -k /scratch | tail -1' > "$TMP_DIR/env.out"
    cat "$TMP_DIR/env.out"
    grep -q "^BSOS_JOBS=$BUILD_JOBS MAKEFLAGS=-j$BUILD_JOBS CMAKE_BUILD_PARALLEL_LEVEL=$BUILD_JOBS$" "$TMP_DIR/env.out" || die "job variables wrong"
    [ "$(awk 'NR==2 {print $2}' "$TMP_DIR/env.out")" = $(( $(size_mb "$BUILD_SCRATCH_SIZE") * 1024 )) ] || die "/scratch is not $BUILD_SCRATCH_SIZE"
    [ "$(BSOS_BUILD_CPUS=1 "$B" run cat /sys/fs/cgroup/cpu.max)" = "100000 100000" ] || die "BSOS_BUILD_CPUS=1 not applied"
    "$B" run bash -c 'command -v apt-ftparchive gpg gpgv faketime dpkg-deb git cmake g++ make modinfo depmod udevadm patch' > /dev/null \
        || die "builder lacks Phase 3 tools"
    "$B" run python3 -c 'import venv, numpy, pkg_resources' || die "builder lacks python3-venv, -numpy or -setuptools"
    echo "override BSOS_BUILD_CPUS=1: cpu.max 100000 100000; apt-ftparchive, gpg, gpgv, faketime, git, cmake, g++, kmod, udevadm present"
}

t_build_sh() {
    local out rc
    "$S/build.sh" --help | grep -q '^Usage: scripts/build.sh COMMAND' || die "build.sh --help"
    expect_fail 'unknown command: bogus' "$S/build.sh" bogus
    expect_fail '--edition must be jetson or vm' "$S/build.sh" packages --edition pi
    expect_fail 'takes no arguments' "$S/build.sh" repo --fast
    expect_fail 'unknown test suite: phase9' "$S/build.sh" test phase9
    set +e; out="$("$S/build.sh" doctor 2>&1)"; rc=$?; set -e
    printf '%s\n' "$out"
    case "$rc" in
        0) printf '%s\n' "$out" | grep -q '^ready: ' || die "doctor exit 0 without 'ready'" ;;
        1) printf '%s\n' "$out" | grep -q 'missing; fix them' || die "doctor exit 1 without the summary" ;;
        *) die "doctor exit code $rc" ;;
    esac
    printf '%s\n' "$out" | grep -q '^  ok       resources: ' || die "doctor did not report resources"
    command -v make > /dev/null 2>&1 || die "make is not installed, so the Makefile cannot be checked (Debian/Ubuntu: sudo apt install make)"
    (cd "$BRAINSYNCOS_ROOT" && make -n EDITION=vm iso) | grep -qx 'scripts/build.sh iso --edition vm' || die "make iso EDITION=vm"
    (cd "$BRAINSYNCOS_ROOT" && make -n) | grep -qx 'scripts/build.sh all --edition jetson' || die "make (all)"
    (cd "$BRAINSYNCOS_ROOT" && make -n test TESTS="phase2 vm") | grep -qx 'scripts/build.sh test phase2 vm' || die "make test"
    (cd "$BRAINSYNCOS_ROOT" && make -n mirror) | grep -qx 'scripts/build.sh mirror --edition jetson' || die "make mirror"
    expect_fail 'takes no arguments' "$S/build.sh" mirror --fast
    echo "build.sh: help, refusals, doctor (rc=$rc); Makefile targets map to build.sh"
}

t_repo_key() {
    force_rm_tree "$BRAINSYNCOS_ROOT/$TD"
    "$B" run scripts/make-repo-key.sh --out "$TD/key"
    local f="$BRAINSYNCOS_ROOT/$KEY" pub="$BRAINSYNCOS_ROOT/$TD/key/repo-signing-key.pub.asc" fpr fpr2
    # GNU stat first: its -f means filesystem status and succeeds, so the BSD form must come second.
    [ "$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")" = 600 ] || die "secret key is not mode 600"
    grep -q 'BEGIN PGP PRIVATE KEY BLOCK' "$f" || die "secret key file holds no secret key"
    ! grep -q 'PRIVATE KEY' "$pub" || die "public key file holds a private key"
    "$B" run bash -c 'source scripts/lib.sh; key_records "$1"' _ "$KEY" > "$TMP_DIR/key.recs"
    grep '^sec:' "$TMP_DIR/key.recs"
    awk -F: '/^sec:/ {exit !($4 == 22 && $12 ~ /S/ && $12 !~ /[eE]/ && $7 != "")}' "$TMP_DIR/key.recs" \
        || die "not an expiring Ed25519 (algo 22) sign-only key"
    [ "$(grep -c '^ssb:' "$TMP_DIR/key.recs")" = 0 ] || die "key has subkeys"
    fpr="$("$B" run bash -c 'source scripts/lib.sh; key_fingerprint "$1"' _ "$KEY")"
    expect_fail 'already exists' "$B" run scripts/make-repo-key.sh --out "$TD/key"
    "$B" run scripts/make-repo-key.sh --out "$TD/key" --force 2> /dev/null
    fpr2="$("$B" run bash -c 'source scripts/lib.sh; key_fingerprint "$1"' _ "$KEY")"
    [ -n "$fpr" ] && [ "$fpr" != "$fpr2" ] || die "--force did not make a new key"
    echo "key $fpr2: mode 600, Ed25519 sign-only, expires; refuses to overwrite; --force replaces"
}

t_packages() {
    local e deb fpr
    fpr="$("$B" run bash -c 'source scripts/lib.sh; key_fingerprint "$1"' _ "$KEY")"
    for e in jetson vm; do
        "$B" run scripts/build-packages.sh --edition "$e" --key "$KEY" --out "$TD/debs/$e"
        deb="$(ls "$BRAINSYNCOS_ROOT/$TD/debs/$e/brainsyncos-archive-keyring_${BRAINSYNCOS_VERSION}_all.deb")"
        "$B" run bash -c 'set -e; source scripts/lib.sh
            d="$(scratch_dir deb)"; dpkg-deb -x "$1" "$d"; dpkg-deb -f "$1" Package Version Architecture Maintainer
            dpkg-deb -c "$1" | awk "{print \$2}" | sort -u
            key_fingerprint "$d/usr/share/keyrings/brainsyncos-archive-keyring.gpg"
            dpkg-deb -e "$1" "$d/DEBIAN"; (cd "$d" && md5sum -c --quiet DEBIAN/md5sums)' _ "${deb#$BRAINSYNCOS_ROOT/}" > "$TMP_DIR/deb.$e"
        cat "$TMP_DIR/deb.$e"
        grep -qx 'root/root' "$TMP_DIR/deb.$e" && [ "$(grep -c '/' "$TMP_DIR/deb.$e")" = 1 ] || die "$e: files not all root/root"
        grep -qx "$fpr" "$TMP_DIR/deb.$e" || die "$e: the keyring does not hold key $fpr"
        grep -qx "Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>" "$TMP_DIR/deb.$e" || die "$e: Maintainer"
    done
    # Each edition's packages come from a separate builder run: the same bytes
    # in both, the venv included, means the build is reproducible.
    local n=0
    for deb in "$BRAINSYNCOS_ROOT/$TD/debs/vm/"*.deb; do
        cmp "$deb" "$BRAINSYNCOS_ROOT/$TD/debs/jetson/$(basename "$deb")" \
            || die "$(basename "$deb") differs between editions (not reproducible)"
        echo "$(basename "$deb"): $(file_size "$deb") bytes, the same in both editions"
        n=$((n + 1))
    done
    for e in jetson vm; do
        (cd "$BRAINSYNCOS_ROOT/$TD/debs/$e" && LC_ALL=C ls ./*.deb) | sed 's|^\./||' | sed 's/_.*//' | tr '\n' ' ' > "$TMP_DIR/names.$e"
        echo "$e: $(cat "$TMP_DIR/names.$e")"
    done
    [ "$(cat "$TMP_DIR/names.jetson")" = "brainsync-runtime brainsync-udev brainsync brainsyncos-archive-keyring brainsyncos-ch341-tegra brainsyncos-llama-cuda brainsyncos-llama brainsyncos-model-$MODEL_ID espeak-ng " ] \
        && [ "$(cat "$TMP_DIR/names.vm")" = "brainsync-runtime brainsync-udev brainsync brainsyncos-archive-keyring brainsyncos-llama " ] \
        || die "wrong package sets per edition"
    expect_fail '--edition jetson.vm is required' "$B" run scripts/build-packages.sh --key "$KEY"
    expect_fail 'no repository signing key' "$B" run scripts/build-packages.sh --edition vm --key "$TD/none.asc" --out "$TD/x"
    expect_fail 'no package named nope' "$B" run scripts/build-packages.sh --edition vm --only nope --key "$KEY" --out "$TD/x"
    echo "$n packages in both editions, byte-identical (SOURCE_DATE_EPOCH); keyring root-owned, md5sums OK, holds $fpr"
}

# The mirrored third-party .debs: build/downloads/mirror is a cache like
# build/wheels, so a filled one is only checked (offline); a first run
# downloads NVIDIA's CUDA runtime (about 0.5 GB).
t_mirror() {
    "$B" run scripts/fetch-mirror.sh --edition jetson
    "$B" run scripts/fetch-mirror.sh --edition vm 2>&1 | tee "$TMP_DIR/mirror.out"
    grep -q 'no mirrored packages for vm' "$TMP_DIR/mirror.out" || die "the vm edition mirrors something"
    expect_fail 'no mirror named nope' "$B" run scripts/fetch-mirror.sh --only nope
    expect_fail 'unknown argument: --fast' "$B" run scripts/fetch-mirror.sh --fast
}

# The BrainFlow wheel: build/wheels is a cache like the builder image, so an
# up-to-date wheel is checked as it is (building it takes long under emulation).
t_wheel() {
    "$B" run scripts/build-wheels.sh
    "$B" run scripts/build-wheels.sh 2>&1 | tee "$TMP_DIR/wheels.out"
    grep -q 'brainflow-cerelog: up to date' "$TMP_DIR/wheels.out" || die "a second build-wheels.sh run rebuilt the wheel"
    expect_fail 'no wheel named nope' "$B" run scripts/build-wheels.sh --only nope
    expect_fail 'wheels takes only --force' "$S/build.sh" wheels --fast
    in_builder wheel
}

# llama.cpp: build/llama is a cache like build/wheels, so an up-to-date,
# checked build is used as it is (compile and check take long; the check
# alone runs under emulation on x86-64). brainsyncos-llama packages it.
t_llama() {
    "$S/build.sh" llama
    "$S/build.sh" llama 2>&1 | tee "$TMP_DIR/llama.out"
    grep -q 'up to date' "$TMP_DIR/llama.out" && grep -q 'checked already' "$TMP_DIR/llama.out" \
        || die "a second scripts/build.sh llama run rebuilt or re-checked"
    cat "$BUILD_DIR/llama/llama.env" "$BUILD_DIR/llama/check.env"
    expect_fail 'llama takes only --force' "$S/build.sh" llama --fast
    expect_fail 'compile runs in the amd64 cross-builder' "$B" run scripts/build-llama.sh compile
    expect_fail 'check runs in the arm64 builder' "$B" --cross run scripts/build-llama.sh check
    expect_fail '--cross takes build, run, shell or image' "$B" --cross clean
}

t_repo() {
    local e r
    for e in jetson vm; do
        "$B" run scripts/build-repo.sh --edition "$e" --debs "$TD/debs/$e" --key "$KEY" --out "$TD/repo/$e"
        r="$BRAINSYNCOS_ROOT/$TD/repo/$e"
        (cd "$r" && find . -type f | LC_ALL=C sort)
        [ "$(cat "$r/edition")" = "$e" ] || die "$e: edition file"
        grep -qx "Suite: $REPO_SUITE" "$r/dists/$REPO_SUITE/Release" && grep -qx "Origin: $REPO_ORIGIN" "$r/dists/$REPO_SUITE/Release" \
            || die "$e: Release fields"
        "$B" run bash -c 'source scripts/lib.sh; load_packages_config; validate_repo "$1" "$2"' _ "$TD/repo/$e" "$e"
    done
    "$B" run bash -c 'source scripts/lib.sh; load_packages_config; validate_repo "$1" vm' _ "$TD/repo/jetson" 2> "$TMP_DIR/v.err" \
        && die "a jetson repository passed as vm"
    grep -q "built for edition 'jetson', not vm" "$TMP_DIR/v.err" || die "edition mix-up: $(cat "$TMP_DIR/v.err")"
    echo "refuses a jetson repository for the vm ISO"
    expect_fail 'holds no secret key' "$B" run scripts/build-repo.sh --edition vm --debs "$TD/debs/vm" \
        --key "$TD/key/repo-signing-key.pub.asc" --out "$TD/x"
    expect_fail 'no .debs in' "$B" run scripts/build-repo.sh --edition vm --debs "$TD/none" --key "$KEY" --out "$TD/x"
    [ ! -e "$BRAINSYNCOS_ROOT/$TD/x" ] && [ ! -e "$BRAINSYNCOS_ROOT/$TD/x.new" ] || die "a refused build left output"
    # A rebuild replaces the repository as a whole.
    "$B" run scripts/build-repo.sh --edition vm --debs "$TD/debs/vm" --key "$KEY" --out "$TD/repo/vm" > /dev/null 2>&1
    [ ! -e "$BRAINSYNCOS_ROOT/$TD/repo/vm.new" ] || die "rebuild left repo/vm.new"
    echo "repositories for both editions signed and validated; bad input refused; rebuild replaces"
}

# config/model.env and the edition switch; the pins against Hugging Face's
# API when it can be reached (the build itself checks the sha256 of the file).
t_model_pin() {
    local bad="$TMP_DIR/bad.env" kv out api
    mkdir -p "$TMP_DIR"
    /bin/bash -c 'source "$1/scripts/lib.sh"; source "$1/scripts/model.sh"; load_model_config
        echo "model: $(model_package): $MODEL_HF_REPO@$MODEL_HF_REVISION/$MODEL_FILE, $MODEL_SIZE bytes, sha256 $MODEL_SHA256, args \"$(model_llama_args)\""' \
        _ "$BRAINSYNCOS_ROOT"
    for kv in MODEL_ID=Qwen3.5 MODEL_HF_URL=http://huggingface.co MODEL_HF_REPO=unsloth MODEL_HF_REVISION=main \
        MODEL_FILE=model.bin MODEL_SIZE=1.3G MODEL_SHA256=aaf42c8b MODEL_FILE_TYPE=Q4_K_M MODEL_QUANT=Q4-K-M MODEL_LLAMA_ARGS=c,4096; do
        sed "s|^${kv%%=*}=.*|$kv|" "$BSOS_MODEL_CONFIG" > "$bad"
        if out="$(BSOS_MODEL_CONFIG="$bad" /bin/bash -c 'source "$1/scripts/lib.sh"; source "$1/scripts/model.sh"; load_model_config' \
            _ "$BRAINSYNCOS_ROOT" 2>&1)"; then
            rm -f "$bad"; die "accepted $kv"
        fi
        printf '%s\n' "$out" | grep -q "${kv%%=*} must" || die "rejected $kv for the wrong reason: $out"
        echo "rejects $kv: ${out##*ERROR: }"
    done
    rm -f "$bad"
    grep -qx 'PKG_EDITIONS=jetson' "$PACKAGES_DIR/brainsyncos-model/package.env" || die "the model is not Jetson-only by default"
    "$B" run scripts/build-packages.sh --edition vm --only brainsyncos-model --key "$KEY" --out "$TD/model-vm" 2>&1 \
        | tee "$TMP_DIR/vm.out" | grep 'not built for vm'
    [ -z "$(ls "$BRAINSYNCOS_ROOT/$TD/model-vm")" ] || die "a model package was built for the vm edition"
    echo "edition switch: PKG_EDITIONS=jetson, so the vm edition gets no model"
    api="$MODEL_HF_URL/api/models/$MODEL_HF_REPO/tree/$MODEL_HF_REVISION"
    if ! command -v curl > /dev/null 2>&1 || ! curl -fsS -m 30 -o "$TMP_DIR/tree.json" "$api" 2> /dev/null \
        || ! curl -fsS -m 30 -o "$TMP_DIR/ltree.json" "$MODEL_HF_URL/api/models/$MODEL_LICENSE_HF_REPO/tree/$MODEL_LICENSE_HF_REVISION" 2> /dev/null; then
        echo "note: Hugging Face not reachable; the pins were not compared with its API"
        return 0
    fi
    python3 -I - "$TMP_DIR/tree.json" "$MODEL_FILE" "$MODEL_SIZE" "$MODEL_SHA256" "$TMP_DIR/ltree.json" "$MODEL_LICENSE_FILE" "$MODEL_LICENSE_SIZE" <<'PY'
import json, sys
tree, name, size, sha, ltree, lname, lsize = sys.argv[1:]
f = {e["path"]: e for e in json.load(open(tree))}.get(name)
if not f or f.get("size") != int(size) or (f.get("lfs") or {}).get("oid") != sha:
    sys.exit("Hugging Face lists %s as %s, the pin says %s bytes, sha256 %s" % (name, f, size, sha))
l = {e["path"]: e for e in json.load(open(ltree))}.get(lname)
if not l or l.get("size") != int(lsize):
    sys.exit("Hugging Face lists %s as %s, the pin says %s bytes" % (lname, l, lsize))
print("Hugging Face at the pinned revisions: %s %s bytes, sha256 %s; %s %s bytes" % (name, size, sha, lname, lsize))
PY
}

# The BrainSync packages installed in a stock Ubuntu arm64 container with
# Xvfb (docker/Dockerfile.runtime-test, test only) and run there
# (tests/runtime-checks.sh). Its image is built on first use (needs the network).
t_runtime() {
    local df="$BRAINSYNCOS_ROOT/docker/Dockerfile.runtime-test" img
    img="brainsyncos-runtime-test:$(sha256_of "$df" | cut -c1-12)"
    docker image inspect "$img" > /dev/null 2>&1 \
        || docker build --platform "$BUILDER_PLATFORM" -t "$img" -f "$df" "$BRAINSYNCOS_ROOT/docker"
    docker run --rm --init --platform "$BUILDER_PLATFORM" --network none --cpus "$BUILD_CPUS" --memory "$BUILD_MEMORY" \
        -v "$BRAINSYNCOS_ROOT:/work:ro" -w /work "$img" bash tests/runtime-checks.sh "$TD/debs/jetson"
}

t_shellcheck() {
    docker image inspect "$SHELLCHECK_IMAGE" > /dev/null 2>&1 \
        || die "$SHELLCHECK_IMAGE is not available locally (docker pull $SHELLCHECK_IMAGE)"
    local files
    files="$(cd "$BRAINSYNCOS_ROOT" && ls scripts/*.sh tests/*.sh tests/shims/curtin payload/hook.sh payload/install.d/*.sh \
        payload/lib/*.sh packages/*/build.sh wheels/*/build.sh)"
    # shellcheck disable=SC2086
    docker run --rm --network none --cpus "$BUILD_CPUS" -v "$BRAINSYNCOS_ROOT:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" \
        -x -S warning $files
    echo "shellcheck: $(printf '%s\n' "$files" | wc -l | tr -d ' ') scripts clean"
}

log "Phase 3 test chain (logs: ${LOGS#$BRAINSYNCOS_ROOT/}/)"
check configs              t_configs
check builder-caps         t_builder_caps
check build-sh             t_build_sh
check repo-key             t_repo_key
if [ "$nfail" -gt 0 ]; then log "aborting: prerequisites failed"; exit 1; fi
check model-pin            t_model_pin
check llama                t_llama
check packages             t_packages
check mirror               t_mirror
check repo                 t_repo
if [ -f "$BRAINSYNCOS_ROOT/$TD/repo/jetson/dists/$REPO_SUITE/InRelease" ]; then
    check apt-helper       in_builder apt
    check app-debs         in_builder app-debs
    check runtime          t_runtime
    check md5-map          in_builder md5map
    check install-hook     in_builder hook
    check offline-voice    in_builder voice
    check board-debs       in_builder debs
    check usb-serial       in_builder usbserial
    check cuda             in_builder cuda
    check model-deb        in_builder model-deb
    check model-step       in_builder model-step
    check iso-size         in_builder iso-size
    check llama-debs       in_builder llama-debs
    check llama-install    in_builder llama
fi
check wheel                t_wheel
check shellcheck           t_shellcheck

finish "Phase 3"
