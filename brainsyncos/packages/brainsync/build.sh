#!/usr/bin/env bash
# build.sh - brainsync: the BrainSync apps, in the brainsync-runtime venv.
#
# Run by scripts/build-packages.sh (builder only; see its header for the
# BSOS_* variables), with the BrainSync source tree at BSOS_APP_SRC
# (scripts/builder.sh mounts the repository around brainsyncos/ at /src). The
# app's dependencies must be pinned in python/brainsync.lock.txt
# (python/check-lock.py). The package holds:
#   - the app's wheel (packages brainsync, app, tools), installed into
#     /opt/brainsyncos/venv as pip installs it, byte-compiled;
#   - a /usr/bin link to each console script of pyproject.toml
#     (brainsync-voice, brainsync-demo, brainchess, ...);
#   - config/{default,intents,profile}.yaml as conffiles in /etc/brainsync;
#   - ui/ in /usr/share/brainsync/ui and clients/ (without its tests) in
#     /usr/share/brainsync/clients, where brainsync/paths.py looks when
#     installed.
# It never holds API keys: they go on the device in
# /etc/brainsync/secrets.env, and the build refuses any .env file. BrainSyncOS's
# 70-brainsync.sh creates that file; this package does not ship or own it.
# Depends on brainsync-runtime of exactly this version. Recommends espeak-ng,
# the offline voice of brainsync/voice.py (the Jetson's comes from our
# repository, packages/espeak-ng). The app is installed
# into the venv only while it is staged, then removed again, so the
# brainsync-runtime build (same container) packages the venv without it.

BSOS_SCRIPT=brainsync
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
source "$BRAINSYNCOS_ROOT/scripts/venv.sh"
load_packages_config
require_cmd rsync

src="${BSOS_APP_SRC:-}"
[ -n "$src" ] && [ -f "$src/pyproject.toml" ] && [ -d "$src/brainsync" ] \
    || die "no BrainSync source tree (BSOS_APP_SRC; scripts/builder.sh mounts it at /src)"

venv_build
PYTHONPATH="$(venv_pip_wheel)" python3 "$PY_DIR/check-lock.py" "$src/pyproject.toml" "$PY_LOCK" \
    || die "python/brainsync.lock.txt does not cover pyproject.toml's dependencies: re-lock"

w="$(scratch_dir brainsync-app)"
s="$w/stage"
trap 'force_rm_tree "$w"' EXIT
mkdir -p "$w/src" "$w/wheel" "$s/DEBIAN"

# The wheel, from a private copy of the source (setuptools writes build/ and
# egg-info next to it, and the mount is read-only).
rsync -a --exclude __pycache__ --exclude '*.py[cod]' --exclude '*.egg-info' \
    "$src/pyproject.toml" "$src/README.md" "$src/brainsync" "$src/app" "$src/tools" "$w/src/"
SOURCE_DATE_EPOCH="${BSOS_SOURCE_DATE_EPOCH:-0}" venv_pip "$VENV_PY" wheel --quiet --no-deps \
    --no-build-isolation --no-index -w "$w/wheel" "$w/src" || die "building the app's wheel failed"
whl="$(ls "$w/wheel"/brainsync-*.whl)"
ver="$(basename "$whl" | cut -d- -f2)"
log "app wheel: $(basename "$whl") ($(file_size "$whl") bytes)"

# Install it into the venv, take its files (RECORD, plus the .pyc files made
# here), then remove them again.
tree_listing "$VENV_DIR" > "$w/venv.before"
venv_pip "$VENV_PY" install --quiet --no-index --no-deps --only-binary :all: --no-compile \
    --find-links "$w/wheel" "brainsync==$ver" || die "installing the app's wheel failed"
dist="$VENV_SITE/brainsync-$ver.dist-info"
[ -f "$dist/RECORD" ] || die "no $dist/RECORD"
venv_compile "$VENV_SITE/brainsync" "$VENV_SITE/app" "$VENV_SITE/tools"
python3 - "$VENV_SITE" "$dist/RECORD" > "$w/files" <<'EOF'
import csv, os, sys
site, record = sys.argv[1:]
paths = {os.path.normpath(os.path.join(site, row[0])) for row in csv.reader(open(record)) if row}
for top in ("brainsync", "app", "tools"):
    for root, dirs, files in os.walk(os.path.join(site, top)):
        if os.path.basename(root) == "__pycache__":
            paths.update(os.path.join(root, f) for f in files)
venv = os.path.dirname(os.path.dirname(os.path.dirname(site)))
for p in sorted(paths):
    if not p.startswith(venv + "/"):
        sys.exit(f"RECORD names a file outside the venv: {p}")
    print(p)
EOF
(cd / && sed 's|^/||' "$w/files" | xargs cp -a --parents -t "$s") || die "staging the app's files failed"
xargs rm -f < "$w/files"
find "$VENV_SITE/brainsync" "$VENV_SITE/app" "$VENV_SITE/tools" "$dist" -depth -type d -empty -delete 2>/dev/null || true
tree_listing "$VENV_DIR" > "$w/venv.after"
diff "$w/venv.before" "$w/venv.after" > /dev/null || die "removing the app did not restore the venv"
log "app files: $(wc -l < "$w/files" | tr -d ' ') in $VENV_DIR"

# Commands on PATH.
mkdir -p "$s/usr/bin"
scripts="$(sed -n '/^\[console_scripts\]/,/^\[/{s/^\([A-Za-z0-9_.-]*\) *=.*/\1/p;}' "$s$dist/entry_points.txt")"
[ -n "$scripts" ] || die "the app's wheel has no console scripts"
for c in $scripts; do
    [ -x "$s$VENV_DIR/bin/$c" ] || die "no $VENV_DIR/bin/$c in the staged app"
    head -1 "$s$VENV_DIR/bin/$c" | grep -qx "#!$VENV_PY" || die "$c does not run $VENV_PY"
    ln -s "$VENV_DIR/bin/$c" "$s/usr/bin/$c"
done

# Config (conffiles), UI and clients.
mkdir -p "$s/etc/brainsync" "$s/usr/share/brainsync" "$s/usr/share/doc/brainsync"
for f in default.yaml intents.yaml profile.yaml; do
    install -m 644 "$src/config/$f" "$s/etc/brainsync/$f"
    printf '/etc/brainsync/%s\n' "$f"
done > "$s/DEBIAN/conffiles"
chmod 644 "$s/DEBIAN/conffiles"
rsync -a --chmod=D755,F644 --exclude '.*' "$src/ui/" "$s/usr/share/brainsync/ui/"
rsync -a --chmod=D755,F644 --exclude '.*' --exclude test/ --exclude '*.test.mjs' --exclude node_modules/ \
    "$src/clients/" "$s/usr/share/brainsync/clients/"
cat > "$s/usr/share/doc/brainsync/README" <<EOF
BrainSync $ver: the SSVEP apps, BrainSwipe, BrainChess, BCI Voice and the
BrainSync server, installed into /opt/brainsyncos/venv (brainsync-runtime).

Commands: $(printf '%s ' $scripts)
Config:   /etc/brainsync/{default,intents,profile}.yaml
UI:       /usr/share/brainsync/ui, /usr/share/brainsync/clients
State:    \$XDG_STATE_HOME/brainsync (default ~/.local/state/brainsync):
          the user's profile, the TTS clip cache, logs/
Data:     ~/brainsync_data (recordings), or \$BRAINSYNC_DATA_DIR

API keys (ANTHROPIC_API_KEY, CARTESIA_API_KEY, BRAINSYNC_LLAMA_API_KEY) are
never part of this package. They go in /etc/brainsync/secrets.env, one
KEY=value per line. BrainSyncOS's install creates it as a commented template,
root:<the appliance account's group>, mode 0640, and never overwrites it.
BCI Voice and the spoken prompts read it; brainsync-server needs
--env-file /etc/brainsync/secrets.env. Offline, the prompts are spoken by
espeak-ng when it is installed. Each place can be moved with a BRAINSYNC_*
variable (brainsync/paths.py).
EOF
chmod 644 "$s/usr/share/doc/brainsync/README"

# Never a secret.
found="$(find "$s" \( -name '*.env' -o -name '.env*' \) -print)"
[ -z "$found" ] || die "refusing to package environment files: $found"
! grep -rIlE 'sk-ant-[A-Za-z0-9_-]{20,}' "$s" || die "something that looks like an Anthropic API key is staged"

installed_kb="$(installed_size "$s")"
cat > "$s/DEBIAN/control" <<EOF
Package: brainsync
Version: ${BSOS_PKG_VERSION:?}
Architecture: arm64
Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>
Installed-Size: $installed_kb
Section: science
Priority: optional
Depends: brainsync-runtime (= $BSOS_PKG_VERSION)
Recommends: espeak-ng
Description: BrainSync SSVEP brain-computer interface apps
 The BrainSync apps $ver: the SSVEP demo, recorder, checks and stimulus,
 BrainSwipe, BrainChess, BCI Voice (brainsync-voice) and the BrainSync
 server, with their config in /etc/brainsync and their web pages in
 /usr/share/brainsync. API keys go in /etc/brainsync/secrets.env on the
 device; this package has none.
EOF
build_deb "$s" "${BSOS_PKG_OUT:?}" > /dev/null
