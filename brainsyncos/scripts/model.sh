# shellcheck shell=bash
# model.sh - the default model of the local LLM, pinned in config/model.env.
# Source it after lib.sh; do not execute it.
#
# The model is a GGUF file from a Hugging Face repository, pinned by
# repository, revision (a commit hash, never a branch) and sha256, with its
# licence text pinned the same way. packages/brainsyncos-model ships it.
# Downloads go to build/downloads/models/<sha256>/ once (an interrupted
# download resumes from its .part file); every call checks the size and the
# sha256 again, so a damaged cache stops the build instead of being packaged.
# Only https://huggingface.co/<repo>/resolve/<revision>/<file> is fetched.
#
#   load_model_config          config/model.env, checked (MODEL_* variables)
#   model_package              the package name, brainsyncos-model-MODEL_ID
#   model_llama_args           MODEL_LLAMA_ARGS with its commas as spaces
#   model_fetch REPO REV FILE SHA256 SIZE
#                              print the cached file's path (download if needed)
#   gguf_info FILE             the GGUF header as KEY=value lines: version,
#                              tensors, and general.architecture, general.name,
#                              general.file_type, ARCH.context_length

# Variables defined here are used by the scripts that source it.
# shellcheck disable=SC2034

BSOS_MODEL_CONFIG="${BSOS_MODEL_CONFIG:-$BRAINSYNCOS_ROOT/config/model.env}"
BSOS_MODEL_REQUIRED_KEYS="MODEL_ID MODEL_HF_URL MODEL_HF_REPO MODEL_HF_REVISION MODEL_FILE MODEL_SIZE
MODEL_SHA256 MODEL_FILE_TYPE MODEL_QUANT MODEL_LICENSE MODEL_LICENSE_HF_REPO MODEL_LICENSE_HF_REVISION
MODEL_LICENSE_FILE MODEL_LICENSE_SIZE MODEL_LICENSE_SHA256 MODEL_LLAMA_ARGS"
MODEL_CACHE_DIR="$DOWNLOADS_DIR/models"
# Where the package installs models, and the file the llama-server unit reads.
MODEL_INSTALL_DIR=/opt/brainsyncos/models
MODEL_CONF=/etc/brainsyncos/llama.d/model.conf

load_model_config() {
    load_env_file "$BSOS_MODEL_CONFIG" "$BSOS_MODEL_REQUIRED_KEYS"
    local key val
    printf '%s\n' "$MODEL_ID" | grep -Eq '^[a-z0-9][a-z0-9.+-]{1,40}$' \
        || die "MODEL_ID must be lowercase letters, digits, . + and -, got $MODEL_ID"
    [ "$MODEL_HF_URL" = https://huggingface.co ] || die "MODEL_HF_URL must be https://huggingface.co, got $MODEL_HF_URL"
    for key in MODEL_HF_REPO MODEL_LICENSE_HF_REPO; do
        eval "val=\$$key"
        printf '%s\n' "$val" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$' \
            || die "$key must be OWNER/NAME, got $val"
    done
    for key in MODEL_HF_REVISION MODEL_LICENSE_HF_REVISION; do
        eval "val=\$$key"
        printf '%s\n' "$val" | grep -Eq '^[0-9a-f]{40}$' || die "$key must be a 40-character commit hash, got $val"
    done
    for key in MODEL_SHA256 MODEL_LICENSE_SHA256; do
        eval "val=\$$key"
        printf '%s\n' "$val" | grep -Eq '^[0-9a-f]{64}$' || die "$key must be a sha256 (64 hex characters), got $val"
    done
    for key in MODEL_SIZE MODEL_LICENSE_SIZE MODEL_FILE_TYPE; do
        eval "val=\$$key"
        printf '%s\n' "$val" | grep -Eq '^(0|[1-9][0-9]*)$' || die "$key must be a whole number, got $val"
    done
    printf '%s\n' "$MODEL_FILE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._+-]*\.gguf$' \
        || die "MODEL_FILE must be a file name ending in .gguf, got $MODEL_FILE"
    printf '%s\n' "$MODEL_LICENSE_FILE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._+-]*$' \
        || die "MODEL_LICENSE_FILE must be a file name, got $MODEL_LICENSE_FILE"
    printf '%s\n' "$MODEL_QUANT" | grep -Eq '^[A-Za-z0-9_]+$' || die "MODEL_QUANT must be a quantization name like Q4_K_M, got $MODEL_QUANT"
    printf '%s\n' "$MODEL_LICENSE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9.+-]*$' \
        || die "MODEL_LICENSE must be an SPDX identifier, got $MODEL_LICENSE"
    # Comma-separated llama-server options: no shell or systemd specials.
    printf '%s\n' "$MODEL_LLAMA_ARGS" | grep -Eq '^-[A-Za-z0-9.:=+/-]*(,[A-Za-z0-9.:=+/-]+)*$' \
        || die "MODEL_LLAMA_ARGS must be llama-server options separated by commas (e.g. -c,4096), got $MODEL_LLAMA_ARGS"
}

model_package() { printf 'brainsyncos-model-%s\n' "$MODEL_ID"; }

model_llama_args() { printf '%s\n' "$MODEL_LLAMA_ARGS" | tr ',' ' '; }

# model_fetch REPO REV FILE SHA256 SIZE: download the file once into
# MODEL_CACHE_DIR/SHA256/FILE, check it, and print its path.
model_fetch() {
    local repo="$1" rev="$2" file="$3" sha="$4" size="$5" dir f url got
    dir="$MODEL_CACHE_DIR/$sha"
    f="$dir/$file"
    url="$MODEL_HF_URL/$repo/resolve/$rev/$file"
    if [ ! -f "$f" ]; then
        mkdir -p "$dir"
        check_free_space "$dir" $(( size / 1073741824 + 2 ))
        log "downloading $url ($size bytes)"
        python3 -I - "$url" "$f.part" "$size" <<'PY' || die "download failed (run again to resume): $url"
import os, sys, time, urllib.error, urllib.request
url, part, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
done = os.path.getsize(part) if os.path.exists(part) else 0
if done > size:
    os.remove(part)
    done = 0
tries = 0
while done < size:
    req = urllib.request.Request(url, headers={"User-Agent": "BrainSyncOS-build"})
    if done:
        req.add_header("Range", "bytes=%d-" % done)
    err = "the connection ended early"
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab" if done else "wb") as out:
            if done and r.status != 206:
                sys.exit("the server ignored the resume request (HTTP %d)" % r.status)
            last = time.time()
            while done < size:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if time.time() - last > 30:
                    print("  %d of %d MB" % (done >> 20, size >> 20), file=sys.stderr, flush=True)
                    last = time.time()
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            sys.exit("HTTP %d %s" % (e.code, e.reason))
        err = str(e)
    except OSError as e:
        err = str(e)
    done = os.path.getsize(part)
    if done < size:
        tries += 1
        if tries > 5:
            sys.exit("giving up after %d tries: %s" % (tries, err))
        print("  %s at %d bytes; resuming in 5 s" % (err, done), file=sys.stderr, flush=True)
        time.sleep(5)
if os.path.getsize(part) != size:
    sys.exit("got %d bytes, want %d" % (os.path.getsize(part), size))
PY
        got="$(sha256_of "$f.part")"
        if [ "$got" != "$sha" ]; then
            rm_released "$f.part"
            die "$url: sha256 $got, the pin is $sha; deleted the download"
        fi
        mv "$f.part" "$f"
    fi
    [ "$(file_size "$f")" = "$size" ] || die "${f#$BRAINSYNCOS_ROOT/} is $(file_size "$f") bytes, the pin is $size (delete it to download again)"
    got="$(sha256_of "$f")"
    [ "$got" = "$sha" ] || die "${f#$BRAINSYNCOS_ROOT/}: sha256 $got, the pin is $sha (delete it to download again)"
    log "$file: $size bytes, sha256 $sha (as pinned)"
    printf '%s\n' "$f"
}

# gguf_info FILE: the GGUF header (format version 2 or 3) as KEY=value lines.
gguf_info() {
    python3 -I - "$1" <<'PY'
import struct, sys
f = open(sys.argv[1], "rb")
def rd(fmt):
    n = struct.calcsize(fmt)
    b = f.read(n)
    if len(b) != n:
        sys.exit("truncated GGUF header")
    return struct.unpack("<" + fmt, b)[0]
def string():
    return f.read(rd("Q")).decode("utf-8", "replace")
SCALAR = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
def value(t):
    if t in SCALAR:
        return rd(SCALAR[t])
    if t == 8:
        return string()
    if t == 9:
        it, n = rd("I"), rd("Q")
        for _ in range(n):
            value(it)
        return "[%d]" % n
    sys.exit("unknown GGUF value type %d" % t)
if f.read(4) != b"GGUF":
    sys.exit("not a GGUF file")
version = rd("I")
if version not in (2, 3):
    sys.exit("GGUF version %d" % version)
tensors, nkv = rd("Q"), rd("Q")
kv = {}
for _ in range(nkv):
    k = string()
    kv[k] = value(rd("I"))
arch = kv.get("general.architecture", "")
print("GGUF_VERSION=%d" % version)
print("GGUF_TENSORS=%d" % tensors)
print("GGUF_ARCHITECTURE=%s" % arch)
print("GGUF_NAME=%s" % str(kv.get("general.name", "")).replace("\n", " "))
print("GGUF_FILE_TYPE=%s" % kv.get("general.file_type", ""))
print("GGUF_CONTEXT_LENGTH=%s" % kv.get(arch + ".context_length", ""))
PY
}
