# shellcheck shell=bash
# venv.sh - the BrainSync Python runtime, a venv at /opt/brainsyncos/venv
# (builder only). Source it after lib.sh; do not execute it.
#
# A venv works only at the path it was made at (its console scripts name
# their interpreter), so it is built in the builder at that exact path and
# packaged from there: packages/brainsync-runtime ships it, packages/brainsync
# adds the app's wheel to it. Both call venv_build; it builds once per
# container, so one build-packages.sh run makes it once.
#
# Inputs (committed, python/):
#   brainsync.lock.txt   the hash-locked wheels (cp312, aarch64 manylinux):
#                        uv pip compile of brainsync.in, PsychoPy's unused
#                        dependencies left out (psychopy-excludes.txt)
#   pip.env              the pinned pip that downloads and installs them
#   local-wheels.txt     locally built wheels to add (the Cerelog BrainFlow fork)
# Caches (gitignored build/):
#   build/pip/           the pip wheel
#   build/wheelhouse/    the downloaded wheels; once complete, builds are offline
#   build/wheels/        the local wheels named in local-wheels.txt, as
#                        scripts/build-wheels.sh leaves them (with SHA256SUMS)
#
# Every wheel is installed with --no-deps (the lock is the whole set; never
# run pip check, which would list the PsychoPy dependencies left out on
# purpose) and --no-compile; then folders named tests are removed (never
# numpy/testing: scipy imports it) with psychopy/demos, and everything is
# byte-compiled with unchecked-hash .pyc files, so nothing is written under
# /opt at runtime and two builds give the same bytes.
#
#   venv_pip PYTHON ARGS...   the pinned pip, run by PYTHON
#   venv_fetch                complete the wheelhouse (no network if it is)
#   venv_build                build VENV_DIR, unless built with the same inputs
#   venv_compile DIR...       byte-compile DIR (as the venv does)
# VENV_WHEELS (after venv_build): "name==version sha256 file" per wheel.

VENV_DIR=/opt/brainsyncos/venv
VENV_PY="$VENV_DIR/bin/python"
VENV_SITE="$VENV_DIR/lib/python3.12/site-packages"
# Outside the venv, so it is never packaged: the inputs it was built from.
VENV_MARK=/opt/brainsyncos/.venv-inputs
VENV_WHEELS=/opt/brainsyncos/.venv-wheels.txt
PY_DIR="$BRAINSYNCOS_ROOT/python"
PY_LOCK="$PY_DIR/brainsync.lock.txt"
PY_LOCAL_LIST="$PY_DIR/local-wheels.txt"
PIP_DIR="$BUILD_DIR/pip"
WHEELHOUSE="$BUILD_DIR/wheelhouse"
LOCAL_WHEELS="$BUILD_DIR/wheels"
SYSTEM_PY=/usr/bin/python3.12

# venv_pip PYTHON ARGS...: run the pip pinned in python/pip.env from its wheel.
venv_pip() {
    local py="$1" whl
    shift
    whl="$(venv_pip_wheel)" || die "no pip wheel"
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PIP_NO_CACHE_DIR=1 PIP_PROGRESS_BAR=off \
        PYTHONDONTWRITEBYTECODE=1 "$py" "$whl/pip" "$@"
}

# venv_pip_wheel: the path of the pip wheel, fetched and checked on first use.
venv_pip_wheel() {
    (
        load_env_file "$PY_DIR/pip.env" "PIP_VERSION PIP_WHEEL_URL PIP_WHEEL_SHA256"
        local f="$PIP_DIR/${PIP_WHEEL_URL##*/}"
        if [ ! -f "$f" ] || [ "$(sha256_of "$f")" != "$PIP_WHEEL_SHA256" ]; then
            mkdir -p "$PIP_DIR"
            log "fetching pip $PIP_VERSION"
            # The builder has no CA store; the pinned sha256 is what is trusted
            # here. pip itself then verifies TLS with its own certificates.
            python3 -c 'import ssl, sys, urllib.request
ctx = ssl._create_unverified_context()
with urllib.request.urlopen(sys.argv[1], context=ctx, timeout=60) as r, open(sys.argv[2], "wb") as f:
    f.write(r.read())' "$PIP_WHEEL_URL" "$f.part" || die "cannot download $PIP_WHEEL_URL"
            [ "$(sha256_of "$f.part")" = "$PIP_WHEEL_SHA256" ] || { rm -f "$f.part"; die "pip wheel sha256 mismatch ($PIP_WHEEL_URL)"; }
            mv "$f.part" "$f"
        fi
        printf '%s\n' "$f"
    )
}

# venv_fetch: every wheel of the lock in build/wheelhouse, hash-checked.
# Offline when the wheelhouse is already complete.
venv_fetch() {
    mkdir -p "$WHEELHOUSE"
    if venv_pip python3 download --quiet --no-index --find-links "$WHEELHOUSE" -d "$WHEELHOUSE" \
            --no-deps --only-binary :all: --require-hashes -r "$PY_LOCK" > /dev/null 2>&1; then
        log "wheelhouse complete: build/wheelhouse ($(venv_lock_count) locked wheels, offline)"
        return 0
    fi
    log "downloading the locked wheels into build/wheelhouse"
    venv_pip python3 download --quiet -d "$WHEELHOUSE" \
        --no-deps --only-binary :all: --require-hashes -r "$PY_LOCK" \
        || die "could not download the locked wheels (network?)"
}

venv_lock_count() { grep -cE '^[A-Za-z0-9]' "$PY_LOCK"; }

# venv_local_wheels: the wheels python/local-wheels.txt names, one path per
# line; dies on a missing, doubled or unlisted wheel, or one whose sha256 is
# not the one build/wheels/SHA256SUMS records.
venv_local_wheels() {
    local names n f found norm
    names="$(sed 's/#.*//' "$PY_LOCAL_LIST" | tr -d ' \t' | grep -v '^$' || true)"
    for f in "$LOCAL_WHEELS"/*.whl; do
        [ -e "$f" ] || continue
        norm="$(basename "$f" | cut -d- -f1 | tr 'A-Z_.' 'a-z--')"
        printf '%s\n' "$names" | tr 'A-Z_.' 'a-z--' | grep -qx "$norm" \
            || die "build/wheels/$(basename "$f") is not listed in python/local-wheels.txt"
    done
    for n in $names; do
        norm="$(printf '%s' "$n" | tr 'A-Z.-' 'a-z__')"
        found=""
        for f in "$LOCAL_WHEELS/$norm"-*.whl; do
            [ -e "$f" ] || continue
            [ -z "$found" ] || die "more than one wheel of $n in build/wheels"
            found="$f"
        done
        [ -n "$found" ] || die "python/local-wheels.txt lists $n, but build/wheels has no wheel of it (scripts/build.sh wheels)"
        grep -qx "$(sha256_of "$found")  $(basename "$found")" "$LOCAL_WHEELS/SHA256SUMS" 2> /dev/null \
            || die "build/wheels/$(basename "$found") does not match build/wheels/SHA256SUMS"
        printf '%s\n' "$found"
    done
}

# venv_inputs: one hash over everything the venv is built from.
venv_inputs() {
    local f
    {
        "$SYSTEM_PY" -c 'import sys; print(sys.version)'
        cat "$PY_LOCK" "$PY_DIR/pip.env" "$BRAINSYNCOS_ROOT/scripts/venv.sh"
        for f in $(venv_local_wheels); do printf '%s %s\n' "$(basename "$f")" "$(sha256_of "$f")"; done
        printf 'epoch %s\n' "${BSOS_SOURCE_DATE_EPOCH:-}"
    } | sha256sum | awk '{print $1}'
}

# venv_compile DIR...: unchecked-hash .pyc files, the same bytes on every build.
venv_compile() {
    local out
    out="$(PYTHONDONTWRITEBYTECODE=1 SOURCE_DATE_EPOCH="${BSOS_SOURCE_DATE_EPOCH:-0}" "$VENV_PY" -m compileall -q -f -j "${BSOS_JOBS:-1}" \
        --invalidation-mode unchecked-hash "$@" 2>&1)" || { printf '%s\n' "$out" | tail -20 >&2; die "byte-compiling $* failed"; }
}

# venv_record REPORT...: "name==version sha256 file" for each wheel in pip's
# install reports (--report), sorted.
venv_record() {
    python3 - "$@" <<'EOF'
import hashlib, json, sys, urllib.parse, urllib.request
rows = []
for report in sys.argv[1:]:
    for item in json.load(open(report))["install"]:
        path = urllib.request.url2pathname(urllib.parse.urlparse(item["download_info"]["url"]).path)
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        meta = item["metadata"]
        rows.append(f'{meta["name"].lower()}=={meta["version"]} {digest} {path.rsplit("/", 1)[-1]}')
print("\n".join(sorted(rows)))
EOF
}

# venv_build: VENV_DIR from the wheelhouse and the local wheels.
venv_build() {
    local inputs locals n
    require_cmd "$SYSTEM_PY"
    inputs="$(venv_inputs)"
    if [ -f "$VENV_MARK" ] && [ "$(cat "$VENV_MARK")" = "$inputs" ] && [ -x "$VENV_PY" ]; then
        log "venv $VENV_DIR already built from these inputs"
        return 0
    fi
    venv_fetch
    locals="$(venv_local_wheels)"
    rm -rf "$VENV_DIR" "$VENV_MARK" "$VENV_WHEELS" /opt/brainsyncos/.venv-report-*.json
    mkdir -p "$(dirname "$VENV_DIR")"
    log "creating $VENV_DIR ($("$SYSTEM_PY" -c 'import platform; print(platform.python_version())'))"
    "$SYSTEM_PY" -m venv --without-pip "$VENV_DIR" || die "python3.12 -m venv failed"
    log "installing $(venv_lock_count) locked wheels"
    venv_pip "$VENV_PY" install --quiet --no-index --find-links "$WHEELHOUSE" --no-deps --only-binary :all: \
        --require-hashes --no-compile --report /opt/brainsyncos/.venv-report-lock.json -r "$PY_LOCK" \
        || die "installing the locked wheels failed"
    if [ -n "$locals" ]; then
        log "installing local wheels: $(printf '%s\n' "$locals" | xargs -n1 basename | tr '\n' ' ')"
        # shellcheck disable=SC2086  # build/ paths have no spaces
        venv_pip "$VENV_PY" install --quiet --no-index --no-deps --only-binary :all: --no-compile \
            --report /opt/brainsyncos/.venv-report-local.json $locals || die "installing the local wheels failed"
    fi
    venv_record /opt/brainsyncos/.venv-report-*.json > "$VENV_WHEELS"
    n="$(wc -l < "$VENV_WHEELS" | tr -d ' ')"
    [ "$n" -ge "$(venv_lock_count)" ] || die "pip installed $n wheels, the lock has $(venv_lock_count)"
    # Trim: test suites and PsychoPy's demos are not needed at runtime.
    find "$VENV_SITE" -type d \( -name tests -o -name __pycache__ \) -prune -exec rm -rf {} +
    rm -rf "$VENV_SITE/psychopy/demos"
    [ -f "$VENV_SITE/numpy/testing/__init__.py" ] || die "numpy/testing is gone (scipy imports it)"
    log "byte-compiling $(du -sm "$VENV_DIR" | awk '{print $1}') MB"
    venv_compile "$VENV_SITE"
    printf '%s\n' "$inputs" > "$VENV_MARK"
    log "venv ready: $n wheels, $(du -sm "$VENV_DIR" | awk '{print $1}') MB"
}
