#!/usr/bin/env bash
# test_nocompile.sh - Phase 4: check scripts/check-no-compile.sh and run it (host side).
#
# Usage: tests/test_nocompile.sh [--keep]
#
# PASS/FAIL per check (tests/testlib.sh), exit 1 if any failed:
#   refuses     a fake repository whose .deb has `Depends: gcc | clang,
#               python3-dev`, `Recommends: nvidia-l4t-kernel-headers` and a
#               postinst running `pip install` and `make`, next to a fake
#               install step running `apt-get install build-essential` and
#               `make`: the script must exit 1 and name every one of them
#   accepts     a clean fake .deb (Depends on cuda-toolkit-13-2-config-common,
#               a postinst that says "make sure" in prose) and a clean step
#               pass, so prose and the config-common exception give no false
#               positives
#   steps       the real payload/install.d/*.sh and payload/lib/*.sh
#   repo        build/repo/jetson, with the llama check (build/llama/cpu and
#               the test model, through the arm64 builder), when the
#               repository exists; otherwise a SKIP line and no check
#   lint        both scripts through shellcheck (the host's, else
#               koalaman/shellcheck:v0.10.0 through docker); a SKIP line when
#               neither is available (docker down)
# The fake .debs are written and read in python3; nothing here needs dpkg-deb
# or the builder except the repo check. Temporary files: build/tmp/nocompile/
# (deleted unless --keep); logs: build/test-logs/nocompile/.

BSOS_SCRIPT=test_nocompile.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"
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
CHECK="$S/check-no-compile.sh"
LOGS="$BUILD_DIR/test-logs/nocompile"
TD="$BUILD_DIR/tmp/nocompile"
SHELLCHECK_IMAGE=koalaman/shellcheck:v0.10.0
mkdir -p "$LOGS"
rm -f "$LOGS"/*.log
# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"

cleanup() {
    [ "$keep" = 1 ] || force_rm_tree "$TD"
}
trap cleanup EXIT

# fake_deb PATH CONTROL [SCRIPT_NAME SCRIPT_TEXT]...: a .deb (ar + control.tar.gz
# + empty data.tar.gz) with that control file and those maintainer scripts.
fake_deb() {
    python3 -I - "$@" <<'PY'
import io, sys, tarfile
path, control = sys.argv[1], sys.argv[2]
scripts = dict(zip(sys.argv[3::2], sys.argv[4::2]))

def tar_gz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, (data, mode) in files.items():
            ti = tarfile.TarInfo("./" + name)
            ti.size, ti.mode, ti.mtime = len(data), mode, 0
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()

def member(name, body):
    hdr = ("%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, 0, 0, 0, "100644", len(body))).encode()
    return hdr + body + (b"\n" if len(body) % 2 else b"")

files = {"control": (control.encode(), 0o644)}
for n, s in scripts.items():
    files[n] = (s.encode(), 0o755)
with open(path, "wb") as f:
    f.write(b"!<arch>\n" + member("debian-binary", b"2.0\n") + member("control.tar.gz", tar_gz(files)) + member("data.tar.gz", tar_gz({})))
PY
}

# expect_in FILE PATTERN...: FILE holds a line matching each PATTERN (ERE).
expect_in() {
    local f="$1" p
    shift
    for p in "$@"; do
        grep -Eq -- "$p" "$f" || die "missing in the output: $p"
        echo "found: $(grep -Em1 -- "$p" "$f")"
    done
}

make_fakes() {
    force_rm_tree "$TD"
    mkdir -p "$TD/bad-repo/pool/main" "$TD/good-repo/pool/main" "$TD/bad-steps" "$TD/good-steps" "$TD/lib"
    fake_deb "$TD/bad-repo/pool/main/evil_1.0_arm64.deb" "Package: evil
Version: 1.0
Architecture: arm64
Maintainer: nobody <nobody@localhost>
Depends: libc6 (>= 2.34), gcc (>= 12) | clang, python3-dev
Pre-Depends: dpkg (>= 1.17)
Recommends: nvidia-l4t-kernel-headers
Description: a package that builds things on the device
 It must be refused.
" postinst '#!/bin/sh
set -e
# make sure the module is built
pip install requests
cd /usr/src/evil && /usr/bin/make -C /usr/src/evil
'
    fake_deb "$TD/good-repo/pool/main/good_1.0_all.deb" "Package: good
Version: 1.0
Architecture: all
Maintainer: nobody <nobody@localhost>
Depends: libc6 (>= 2.34), cuda-toolkit-13-2-config-common, libstdc++6
Description: a package that only ships files
" postinst '#!/bin/sh
set -e
# make sure ldconfig runs; cmake is not involved
if [ "$1" = configure ]; then ldconfig; fi
echo "please make sure udev is running (pip is not used)"
' postrm '#!/bin/sh
set -e
echo "removed, make a note"
'
    cat > "$TD/bad-steps/01-bad.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
apt-get -y -q --no-install-recommends install build-essential
pkgs=(libfoo-dev)
apt-get install "${pkgs[@]}"
cd /usr/src/thing && make -j4
EOF
    cat > "$TD/good-steps/01-good.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
pkgs=(brainsync-udev)
pkgs+=(cuda-cudart-13-2)
apt-get -y -q --no-install-recommends install "${pkgs[@]}" espeak-ng
echo "make sure the model is there; cmake-style configs are not needed"
makefile=/tmp/x; echo "$makefile"
EOF
    cat > "$TD/lib/helper.sh" <<'EOF'
# helper: nothing forbidden
helper_apt() { apt-get -y "$@"; }
EOF
    chmod 755 "$TD/bad-steps/01-bad.sh" "$TD/good-steps/01-good.sh"
}

t_refuses() {
    make_fakes
    local out="$TMP_DIR/nocompile-bad.out" rc=0
    mkdir -p "$TMP_DIR"
    "$CHECK" "$TD/bad-repo" --steps "$TD/bad-steps" --lib "$TD/lib" --llama none > "$out" 2>&1 || rc=$?
    cat "$out"
    [ "$rc" = 1 ] || die "the bad repository and step gave exit code $rc, want 1"
    expect_in "$out" \
        '^FAIL  deb evil Depends: gcc \(' \
        '^FAIL  deb evil Depends: clang \(' \
        '^FAIL  deb evil Depends: python3-dev \(' \
        '^FAIL  deb evil Recommends: nvidia-l4t-kernel-headers \(' \
        '^FAIL  deb evil postinst line 4: pip install requests' \
        '^FAIL  deb evil postinst line 5: .*make -C' \
        '^FAIL  step bad-steps/01-bad.sh installs build-essential \(' \
        '^FAIL  step bad-steps/01-bad.sh installs libfoo-dev \(' \
        '^FAIL  step bad-steps/01-bad.sh line 6: .*make -j4' \
        '^deb   evil 1.0: Depends: libc6' \
        '^no-compile: 9 FAILED'
    ! grep -q '^FAIL  step lib/helper.sh' "$out" || die "the clean helper was refused"
}

t_accepts() {
    [ -d "$TD/good-repo" ] || make_fakes
    local out="$TMP_DIR/nocompile-good.out"
    "$CHECK" "$TD/good-repo" --steps "$TD/good-steps" --lib "$TD/lib" --llama none > "$out" 2>&1 || { cat "$out"; die "the clean repository and step were refused"; }
    cat "$out"
    expect_in "$out" \
        '^ok    deb good: no build tool in its relations or maintainer scripts \(scripts: postinst postrm\)' \
        '^ok    step good-steps/01-good.sh: no build tool run; installs brainsync-udev cuda-cudart-13-2 espeak-ng$' \
        '^ok    step lib/helper.sh: no build tool run$' \
        '^SKIP  llama: not requested' \
        '^no-compile: all 4 checks ok$'
    ! grep -q '^FAIL' "$out" || die "a clean input produced a FAIL line"
}

t_steps() {
    "$CHECK" --only steps --llama none 2>&1 | tee "$TMP_DIR/nocompile-steps.out"
    grep -q '^no-compile: all [0-9]* checks ok$' "$TMP_DIR/nocompile-steps.out" || die "the real install steps did not pass"
    [ "$(grep -c '^ok    step install.d/' "$TMP_DIR/nocompile-steps.out")" -ge 10 ] || die "fewer than 10 install steps were checked"
}

t_repo() {
    "$CHECK" "$REPO_DIR/jetson" --only repo --only llama
}

t_shellcheck() {
    local files="scripts/check-no-compile.sh tests/test_nocompile.sh"
    if command -v shellcheck > /dev/null 2>&1; then
        # shellcheck disable=SC2086
        (cd "$BRAINSYNCOS_ROOT" && shellcheck -x -S warning $files)
        echo "shellcheck ($(shellcheck --version | sed -n 's/^version: //p')): $files clean"
    elif docker image inspect "$SHELLCHECK_IMAGE" > /dev/null 2>&1; then
        # shellcheck disable=SC2086
        docker run --rm --network none -v "$BRAINSYNCOS_ROOT:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" -x -S warning $files
        echo "shellcheck ($SHELLCHECK_IMAGE): $files clean"
    else
        die "no shellcheck on the host and $SHELLCHECK_IMAGE is not usable (docker pull $SHELLCHECK_IMAGE)"
    fi
}

log "Phase 4 no-compile checks (logs: ${LOGS#"$BRAINSYNCOS_ROOT"/}/)"
check refuses              t_refuses
check accepts              t_accepts
check steps                t_steps
if [ -d "$REPO_DIR/jetson/dists/$REPO_SUITE" ]; then
    check repo             t_repo
else
    printf 'SKIP  %-22s (no %s; run scripts/build.sh repo first, then this again)\n' repo "${REPO_DIR#"$BRAINSYNCOS_ROOT"/}/jetson"
fi
if command -v shellcheck > /dev/null 2>&1 || docker image inspect "$SHELLCHECK_IMAGE" > /dev/null 2>&1; then
    check shellcheck       t_shellcheck
else
    printf 'SKIP  %-22s (no shellcheck on the host and docker cannot run %s)\n' shellcheck "$SHELLCHECK_IMAGE"
fi

finish "no-compile"
