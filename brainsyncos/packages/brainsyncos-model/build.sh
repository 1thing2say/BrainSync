#!/usr/bin/env bash
# build.sh - brainsyncos-model: the default model of the local LLM.
#
# Run by scripts/build-packages.sh (builder only; see its header for the
# BSOS_* variables), for the editions in package.env (the Jetson only by
# default). It packages the GGUF file pinned in config/model.env as
# brainsyncos-model-MODEL_ID (scripts/model.sh downloads it once into
# build/downloads/models/ and checks its sha256 on every build), after
# checking the GGUF header: format version, and the file type pinned in
# MODEL_FILE_TYPE (MODEL_QUANT, e.g. Q4_K_M). The package installs:
#   /opt/brainsyncos/models/FILE           the model
#   /opt/brainsyncos/models/FILE.sha256    its sha256, for sha256sum -c
#   /etc/brainsyncos/llama.d/model.conf    what brainsyncos-llama.service reads:
#       BSOS_LLAMA_MODEL=/opt/brainsyncos/models/FILE
#       BSOS_LLAMA_ALIAS=MODEL_ID          (the name the server answers with)
#       BSOS_LLAMA_CTX_SIZE=N              (MODEL_CTX_SIZE, --ctx-size)
#   /usr/share/doc/PACKAGE/                LICENSE (pinned, from the model's
#                                          upstream), copyright and README
# model.conf is not a conffile: removing the package removes it, so the
# service's ConditionPathExists stops it from starting without a model.
# Each model package Provides, Conflicts with and Replaces brainsyncos-model,
# so one model is installed at a time and apt can swap them.
# The package is compressed with zstd at level 3 (BSOS_DEB_COMPRESS and
# BSOS_DEB_LEVEL), not xz: Q4_K_M weights barely compress. Measured on this
# model in the builder under qemu at 1 CPU: zstd -3 takes 19 s and saves
# 21 MB (1.6 %); xz -6 runs at about 0.5 MB/s (some 45 minutes) and gains
# nothing on a slice of the weights; dpkg-deb's own zstd default level took
# 90 s for 32 MiB (over an hour for the model).
# Everything is staged on disk under build/tmp, never on /scratch (a tmpfs
# that counts against the container's memory): the model is hard-linked
# from the download cache, not copied, and dpkg-deb writes its temporary
# data member there too.

BSOS_SCRIPT=brainsyncos-model
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
source "$BRAINSYNCOS_ROOT/scripts/model.sh"
load_packages_config
load_model_config
require_cmd dpkg-deb sha256sum python3
pkg="$(model_package)"

model="$(model_fetch "$MODEL_HF_REPO" "$MODEL_HF_REVISION" "$MODEL_FILE" "$MODEL_SHA256" "$MODEL_SIZE")"
licence="$(model_fetch "$MODEL_LICENSE_HF_REPO" "$MODEL_LICENSE_HF_REVISION" "$MODEL_LICENSE_FILE" \
    "$MODEL_LICENSE_SHA256" "$MODEL_LICENSE_SIZE")"
info="$(gguf_info "$model")" || die "$MODEL_FILE: cannot read its GGUF header"
printf '%s\n' "$info" | sed 's/^/  /' >&2
gguf() { printf '%s\n' "$info" | sed -n "s/^$1=//p"; }
[ "$(gguf GGUF_FILE_TYPE)" = "$MODEL_FILE_TYPE" ] \
    || die "$MODEL_FILE has general.file_type $(gguf GGUF_FILE_TYPE), config/model.env pins $MODEL_FILE_TYPE"
[ -n "$(gguf GGUF_ARCHITECTURE)" ] || die "$MODEL_FILE names no general.architecture"

# On disk, beside the download cache (same filesystem: the hard link works).
s="$TMP_DIR/stage-$pkg"
staged="$s/opt/brainsyncos/models/$MODEL_FILE"
# Unlink the model before removing the tree: force_rm_tree truncates large
# files first, which would empty the download cache through the link.
unstage() { rm -f "$staged"; force_rm_tree "$s"; }
unstage
trap unstage EXIT
doc="$s/usr/share/doc/$pkg"
mkdir -p "$s/DEBIAN" "$(dirname "$staged")" "$s$(dirname "$MODEL_CONF")" "$doc"
ln "$model" "$staged" 2> /dev/null || cp "$model" "$staged"
chmod 644 "$staged"
printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_FILE" > "$staged.sha256"
cat > "$s$MODEL_CONF" <<EOF
# The local LLM's model, from the package $pkg
# (BrainSyncOS config/model.env). brainsyncos-llama.service reads this file
# as an EnvironmentFile and starts only while it exists. It is replaced on
# every upgrade and removed with the package: put local llama-server options
# in local.conf next to it.
BSOS_LLAMA_MODEL=$MODEL_INSTALL_DIR/$MODEL_FILE
BSOS_LLAMA_ALIAS=$MODEL_ID
BSOS_LLAMA_CTX_SIZE=$MODEL_CTX_SIZE
EOF
install -m 644 "$licence" "$doc/LICENSE"
cat > "$doc/copyright" <<EOF
$MODEL_FILE: $(gguf GGUF_NAME) as $MODEL_QUANT GGUF, from $MODEL_HF_URL/$MODEL_HF_REPO
at revision $MODEL_HF_REVISION.
License: $MODEL_LICENSE. The licence text and its copyright notice are in LICENSE,
from $MODEL_HF_URL/$MODEL_LICENSE_HF_REPO at revision $MODEL_LICENSE_HF_REVISION.
EOF
cat > "$doc/README" <<EOF
The default model of BrainSyncOS's local LLM (llama-server, brainsyncos-llama).

file       $MODEL_INSTALL_DIR/$MODEL_FILE
source     $MODEL_HF_URL/$MODEL_HF_REPO/resolve/$MODEL_HF_REVISION/$MODEL_FILE
size       $MODEL_SIZE bytes
sha256     $MODEL_SHA256 (also in $MODEL_FILE.sha256 beside it)
GGUF       version $(gguf GGUF_VERSION), $(gguf GGUF_TENSORS) tensors, architecture $(gguf GGUF_ARCHITECTURE),
           file type $(gguf GGUF_FILE_TYPE) ($MODEL_QUANT), trained context $(gguf GGUF_CONTEXT_LENGTH) tokens
licence    $MODEL_LICENSE (LICENSE here)

$MODEL_CONF tells the llama-server unit which file to load
(BSOS_LLAMA_MODEL), under which name (BSOS_LLAMA_ALIAS=$MODEL_ID) and with
which context length (BSOS_LLAMA_CTX_SIZE=$MODEL_CTX_SIZE).
Check the file with:
  cd $MODEL_INSTALL_DIR && sha256sum -c $MODEL_FILE.sha256
EOF
chmod 644 "$staged.sha256" "$s$MODEL_CONF" "$doc"/*
installed_kb="$(installed_size "$s")"
cat > "$s/DEBIAN/control" <<EOF
Package: $pkg
Version: ${BSOS_PKG_VERSION:?}
Architecture: all
Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>
Installed-Size: $installed_kb
Provides: brainsyncos-model
Conflicts: brainsyncos-model
Replaces: brainsyncos-model
Section: misc
Priority: optional
Multi-Arch: foreign
Description: default model of the BrainSyncOS local LLM ($MODEL_FILE)
 $(gguf GGUF_NAME) quantized to $MODEL_QUANT, as GGUF for llama.cpp
 ($MODEL_SIZE bytes, $MODEL_LICENSE), from Hugging Face $MODEL_HF_REPO at
 revision $MODEL_HF_REVISION. Installs $MODEL_INSTALL_DIR/$MODEL_FILE and
 $MODEL_CONF for brainsyncos-llama.service.
EOF
# dpkg-deb writes the data member to a temporary file in TMPDIR first.
TMPDIR="$TMP_DIR" BSOS_DEB_COMPRESS=zstd BSOS_DEB_LEVEL=3 build_deb "$s" "${BSOS_PKG_OUT:?}" > /dev/null
