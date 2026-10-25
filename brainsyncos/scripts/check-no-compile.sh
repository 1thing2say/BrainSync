#!/usr/bin/env bash
# check-no-compile.sh - Phase 4: check that nothing compiles on the device.
#
# Usage: scripts/check-no-compile.sh [REPO_DIR] [--steps DIR]... [--lib DIR]...
#            [--llama DIR|none] [--model FILE] [--cpu-model NAME]
#            [--allow PKG]... [--only repo|steps|llama]...
#
# Three checks, one line per finding (ok / FAIL / SKIP / NOTE / WARN), exit 0
# only when nothing failed:
#   repo   every .deb under REPO_DIR (pool/**/*.deb, or *.deb in a flat
#          directory such as build/debs/jetson): Depends, Pre-Depends and
#          Recommends hold no compiler or build tool (gcc, g++, cc, clang, cpp,
#          make, cmake, ninja-build, meson, dkms, build-essential, rustc, cargo,
#          golang, npm, python3-dev, python3-pip, nvidia-l4t-kernel-headers,
#          linux-headers-*, cuda-nvcc-*, cuda-toolkit-* except *-config-common,
#          and any *-dev, *-dev-N-M or *-headers package not named with
#          --allow), and no maintainer script (preinst, postinst, prerm,
#          postrm, config) runs gcc, cc, g++, clang, make, cmake, ninja, dkms,
#          cargo, rustc, `pip install`, `python -m pip`, `go build` or
#          `npm install`. Each package's Depends lists are printed. The .debs
#          are read with python3 (ar + tarfile; zstd through the zstd command),
#          so this part needs no dpkg-deb and runs on the Mac as it is.
#   steps  the install steps (default payload/install.d/*.sh) and the helpers
#          they source (--lib, default payload/lib/*.sh): the same forbidden
#          commands, and no `apt-get ... install`, `apt install`, `bsos_apt
#          install` or `apt_install` of a forbidden package. Packages named
#          through a variable are resolved from that file's `pkgs=(...)` or
#          `pkgs+=(...)` lines; anything else is reported as a NOTE.
#   llama  build/llama/cpu (--llama; `none` skips) as the Orin's Cortex-A78AE
#          runs it (ARMv8.2 with dotprod and fp16; no i8mm, SVE or SME):
#          every CPU_VARIANTS backend is there (llama/llama.env); each
#          lib/ggml/libggml-cpu-*.so is disassembled (objdump -d) and classed
#          by the instructions it uses (dotprod, fp16 arithmetic, i8mm, SVE,
#          SME), and the backend ggml would score highest for the A78AE's
#          hwcaps must be an armv8.2 one that uses only dotprod and fp16; then
#          llama-server serves the pinned test model (TEST_MODEL_* in
#          llama/llama.env, in build/downloads/llama-test/ once `build-llama.sh
#          check` ran) the way that script does, and /completion must return a
#          few tokens. The live run happens in the arm64 builder (started
#          through scripts/builder.sh when this runs on the host), in one of
#          three ways, and the output says which:
#            - `qemu-aarch64 -cpu cortex-a76` when that binary is in the
#              builder's PATH (it is not in docker/Dockerfile.builder today);
#            - QEMU_CPU=cortex-a76 when the builder itself runs under qemu
#              binfmt, i.e. on an x86-64 host;
#            - the host's own CPU on an arm64 host (Apple Silicon with Docker
#              Desktop): no qemu is involved, so cortex-a76 cannot be modelled.
#          Cortex-A76 is the qemu model closest to the A78AE (dotprod and
#          fp16, no i8mm/SVE/SME: the same ggml variant). Emulated, the
#          loaded backend must be armv8.2_*. Natively, the loaded backend is
#          compared with what the disassembly predicts for this CPU's hwcaps
#          (WARN on a mismatch), and the A78AE's backend rests on the
#          disassembly alone. SKIP when build/llama/cpu or the test model is
#          missing.
# Host side this must stay bash 3.2 compatible (scripts/lib.sh).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

repo=""
steps=()
libs=()
llama=""
llama_set=0
model=""
cpu_model=cortex-a76
allow=""
only=""
host_arch=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --steps) steps+=("${2:?--steps needs a directory}"); shift 2 ;;
        --lib) libs+=("${2:?--lib needs a directory}"); shift 2 ;;
        --llama) llama="${2:?--llama needs a directory or none}"; llama_set=1; shift 2 ;;
        --model) model="${2:?--model needs a file}"; shift 2 ;;
        --cpu-model) cpu_model="${2:?--cpu-model needs a qemu CPU name}"; shift 2 ;;
        --allow) allow="$allow ${2:?--allow needs a package name}"; shift 2 ;;
        --only) only="$only ${2:?--only needs repo, steps or llama}"; shift 2 ;;
        --host-arch) host_arch="${2:?--host-arch needs a machine name}"; shift 2 ;;
        -h|--help) usage_from_header; exit 0 ;;
        -*) die "unknown argument: $1" ;;
        *) [ -z "$repo" ] || die "only one REPO_DIR: $repo and $1"; repo="$1"; shift ;;
    esac
done
for o in $only; do
    case "$o" in repo|steps|llama) ;; *) die "--only takes repo, steps or llama, not $o" ;; esac
done
wanted() {
    [ -n "$only" ] || return 0
    case " $only " in *" $1 "*) return 0 ;; esac
    return 1
}
if [ "${#steps[@]}" = 0 ]; then steps=("$BRAINSYNCOS_ROOT/payload/install.d"); fi
if [ "${#libs[@]}" = 0 ]; then libs=("$BRAINSYNCOS_ROOT/payload/lib"); fi
if [ "$llama_set" = 0 ]; then
    if [ -d "$BUILD_DIR/llama/cpu" ]; then llama="$BUILD_DIR/llama/cpu"; else llama=none; fi
fi
require_cmd python3

nfail=0
nok=0
ok()   { nok=$((nok + 1)); printf 'ok    %s\n' "$*"; }
fail() { nfail=$((nfail + 1)); printf 'FAIL  %s\n' "$*"; }
skip() { printf 'SKIP  %s\n' "$*"; }
note() { printf 'NOTE  %s\n' "$*"; }
warn_line() { printf 'WARN  %s\n' "$*"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/bsos-nocompile.XXXXXX")"
SERVER=""
stop_server() {
    [ -z "$SERVER" ] || { kill "$SERVER" 2> /dev/null || true; wait "$SERVER" 2> /dev/null || true; SERVER=""; }
}
trap 'stop_server; rm -rf "$WORK"' EXIT

# --- what is forbidden ----------------------------------------------------------

# Package names that are compilers or build tools, as whole names.
FORBID_NAMES=" gcc g++ cc c++ cpp clang clang++ tcc gfortran gnat make gmake cmake ninja-build meson
 autoconf automake libtool pkg-config pkgconf dkms build-essential dpkg-dev debhelper fakeroot
 rustc cargo golang golang-go gccgo nodejs npm python3-dev python3-pip python3-all-dev nvidia-l4t-kernel-headers "
# Patterns (ERE, whole name) for versioned names and families.
FORBID_RE='^((gcc|g\+\+|cpp|clang|clang\+\+|gfortran|rustc|cargo|golang|golang-go|nodejs)-[0-9.]+|gcc-[0-9]+-.*cross.*|lib(gcc|stdc\+\+)-[0-9]+-dev|linux-headers-.*|.*-dkms|cuda-(nvcc|compiler|command-line-tools|libraries-dev|tools|nvprune|cuobjdump|nvdisasm|crt|cccl|nsight.*|gdb|nvrtc-dev|cupti-dev|sanitizer)-.*|cuda-toolkit-.*|.*-dev|.*-dev-[0-9]+(-[0-9]+)*|.*-headers)$'

# forbidden_pkg NAME: prints why NAME may not be on the device; exit 1 if it may.
forbidden_pkg() {
    local n="$1" a
    for a in $allow; do [ "$n" != "$a" ] || return 1; done
    case "$FORBID_NAMES" in *" $n "*) echo "compiler or build tool"; return 0 ;; esac
    case "$n" in cuda-toolkit-*-config-common|cuda-toolkit-config-common) return 1 ;; esac
    printf '%s\n' "$n" | grep -Eq "$FORBID_RE" || return 1
    case "$n" in
        *-dev|*-dev-[0-9]*) echo "development package (headers, static libraries)" ;;
        *-headers*) echo "kernel or library headers" ;;
        cuda-nvcc-*|cuda-compiler-*) echo "the CUDA compiler" ;;
        cuda-toolkit-*) echo "CUDA toolkit meta package (pulls nvcc)" ;;
        *) echo "compiler or build tool" ;;
    esac
    return 0
}

# dep_names FIELD_VALUE: the package names in a Depends-style field, one per
# line, without versions, architecture qualifiers, build profiles or alternatives.
dep_names() {
    printf '%s\n' "$1" | tr ',|' '\n\n' | sed -e 's/([^)]*)//g' -e 's/\[[^]]*\]//g' -e 's/<[^>]*>//g' \
        -e 's/:[a-z0-9-]*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^$' || true
}

# Commands no maintainer script or install step may run. A tool counts only in
# command position: at the start of a line or after ; & | ( ` { ! or a shell
# keyword, also behind VAR=value prefixes and a path, never inside prose such
# as echo "make sure". Comment-only lines are blanked before matching.
CMD_KW='(if|then|do|else|elif|while|until|exec|sudo|env|nice|chroot|time|xargs|eval)'
CMD_PRE="(^|[;&|(\`{!]|[[:space:]]$CMD_KW|^[[:space:]]*$CMD_KW|[A-Z_][A-Z0-9_]*=[^[:space:]]*)[[:space:]]*(\"?[^[:space:]]*/)?"
CMD_TOOLS='(gcc|cc|g\+\+|c\+\+|clang|clang\+\+|tcc|gfortran|make|gmake|cmake|ninja|meson|dkms|cargo|rustc|dpkg-buildpackage|debuild)"?([[:space:]]|$)'
CMD_SUBS='((pip|pip3|pip[0-9.]+)"?[[:space:]]+install|python[0-9.]*"?[[:space:]]+-m[[:space:]]+pip|go"?[[:space:]]+(build|install|get)([[:space:]]|$)|npm"?[[:space:]]+(install|ci|i)([[:space:]]|$))'
CMD_RE="$CMD_PRE($CMD_TOOLS|$CMD_SUBS)"

# code_lines FILE: FILE with comment-only lines blanked (line numbers kept).
code_lines() { sed -e 's/^[[:space:]]*#.*$//' "$1"; }

# scan_commands FILE LABEL: a FAIL for every line of FILE that runs a forbidden command.
scan_commands() {
    local f="$1" label="$2" hits line
    hits="$(code_lines "$f" | grep -nE "$CMD_RE" || true)"
    [ -n "$hits" ] || return 0
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        fail "$label line ${line%%:*}: $(printf '%s' "${line#*:}" | sed 's/^[[:space:]]*//')"
    done <<EOF
$hits
EOF
    return 1
}

# --- repo -----------------------------------------------------------------------------

# deb_extract DEB DIR: DIR/control and the maintainer scripts of DEB, then the
# fields Package, Version, Depends, Pre-Depends, Recommends as "Field<TAB>value"
# (continuation lines joined). Pure python: an ar reader plus tarfile.
deb_extract() {
    python3 -I - "$1" "$2" <<'PY'
import io, os, subprocess, sys, tarfile
deb, out = sys.argv[1], sys.argv[2]
want = ("control", "preinst", "postinst", "prerm", "postrm", "config", "triggers")
with open(deb, "rb") as f:
    if f.read(8) != b"!<arch>\n":
        sys.exit("%s: not an ar archive (.deb)" % deb)
    found = False
    while True:
        hdr = f.read(60)
        if len(hdr) < 60:
            break
        name = hdr[:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(hdr[48:58])
        data = f.read(size)
        if size % 2:
            f.read(1)
        if not name.startswith("control.tar"):
            continue
        found = True
        if name.endswith(".zst"):
            data = subprocess.run(["zstd", "-dc"], input=data, capture_output=True, check=True).stdout
            mode = "r:"
        else:
            mode = "r:*"
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as t:
            for m in t.getmembers():
                n = m.name[2:] if m.name.startswith("./") else m.name.lstrip("/")
                if m.isfile() and n in want:
                    with open(os.path.join(out, n), "wb") as o:
                        o.write(t.extractfile(m).read())
        break
if not found:
    sys.exit("%s: no control.tar member" % deb)
fields = {}
key = None
with open(os.path.join(out, "control"), encoding="utf-8", errors="replace") as c:
    for line in c.read().splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            fields[key] = val.strip()
for k in ("Package", "Version", "Depends", "Pre-Depends", "Recommends"):
    print("%s\t%s" % (k, fields.get(k, "")))
PY
}

check_repo() {
    local dir="$1" debs deb d pkg ver dep pre rec field val name why n=0 s hadfail scripts
    if [ -z "$dir" ]; then
        skip "repo: no REPO_DIR given (build/repo/jetson after scripts/build.sh repo)"
        return 0
    fi
    [ -d "$dir" ] || { fail "repo: $dir is not a directory"; return 0; }
    if [ -d "$dir/pool" ]; then
        debs="$(find "$dir/pool" -type f -name '*.deb' | LC_ALL=C sort)"
    else
        debs="$(find "$dir" -maxdepth 1 -type f -name '*.deb' | LC_ALL=C sort)"
    fi
    [ -n "$debs" ] || { fail "repo: no .deb under $dir"; return 0; }
    while IFS= read -r deb; do
        [ -n "$deb" ] || continue
        n=$((n + 1))
        d="$WORK/deb$n"
        mkdir -p "$d"
        if ! deb_extract "$deb" "$d" > "$d/fields"; then
            fail "deb $(basename "$deb"): cannot be read"
            continue
        fi
        pkg="$(sed -n 's/^Package\t//p' "$d/fields")"
        ver="$(sed -n 's/^Version\t//p' "$d/fields")"
        dep="$(sed -n 's/^Depends\t//p' "$d/fields")"
        pre="$(sed -n 's/^Pre-Depends\t//p' "$d/fields")"
        rec="$(sed -n 's/^Recommends\t//p' "$d/fields")"
        printf 'deb   %s %s: Depends: %s | Pre-Depends: %s | Recommends: %s\n' "$pkg" "$ver" "${dep:--}" "${pre:--}" "${rec:--}"
        hadfail=0
        for field in Depends Pre-Depends Recommends; do
            case "$field" in Depends) val="$dep" ;; Pre-Depends) val="$pre" ;; *) val="$rec" ;; esac
            [ -n "$val" ] || continue
            while IFS= read -r name; do
                [ -n "$name" ] || continue
                if why="$(forbidden_pkg "$name")"; then
                    fail "deb $pkg $field: $name ($why)"
                    hadfail=1
                fi
            done <<EOF
$(dep_names "$val")
EOF
        done
        scripts=""
        for s in preinst postinst prerm postrm config; do
            [ -f "$d/$s" ] || continue
            scripts="$scripts $s"
            scan_commands "$d/$s" "deb $pkg $s" || hadfail=1
        done
        if [ "$hadfail" = 0 ]; then
            if [ -n "$scripts" ]; then
                ok "deb $pkg: no build tool in its relations or maintainer scripts (scripts:$scripts)"
            else
                ok "deb $pkg: no build tool in its relations; no maintainer scripts"
            fi
        fi
    done <<EOF
$debs
EOF
    ok "repo: $n .deb files scanned under ${dir#"$BRAINSYNCOS_ROOT"/}"
}

# --- steps ---------------------------------------------------------------------------

# array_words FILE VAR: the literal words in every VAR=( ... ) and VAR+=( ... ) of FILE.
array_words() {
    grep -oE "(^|[[:space:];])$2\+?=\([^)]*\)" "$1" 2> /dev/null | sed -e 's/^[^(]*(//' -e 's/)$//' | tr -d '"'"'" | tr ' ' '\n' | grep -v '^$' || true
}

# scalar_value FILE VAR: the literal value of the first VAR=value in FILE.
scalar_value() {
    grep -oE "(^|[[:space:];])$2=[^[:space:];]*" "$1" 2> /dev/null | head -n 1 | sed -e 's/^[^=]*=//' | tr -d '"'"'" || true
}

# step_packages FILE: the package names FILE installs, one per line; a line
# starting with "?" names a token that could not be resolved from the file.
step_packages() {
    local f="$1" lines line tokens tok var words
    lines="$(code_lines "$f" | grep -E '(^|[[:space:];&|(`])(apt-get|apt|aptitude|bsos_apt|apt_install)([[:space:]]|$)' \
        | grep -E '(^|[[:space:]])(install|apt_install)([[:space:]]|$)' || true)"
    [ -n "$lines" ] || return 0
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        # the words after "install" (or after apt_install), minus options, up to the next operator
        tokens="$(printf '%s\n' "$line" | awk '{
            on = 0
            for (i = 1; i <= NF; i++) {
                if (on && ($i ~ /^[;|&>#]/ || $i == "||" || $i == "&&")) break
                if (on && $i !~ /^-/) print $i
                if ($i == "install" || $i == "apt_install") on = 1
            }
        }')"
        for tok in $tokens; do
            tok="$(printf '%s' "$tok" | tr -d '"'"'")"
            case "$tok" in
                '${'*'[@]}'|'${'*'[*]}')
                    var="${tok#'${'}"; var="${var%%\[*}"
                    words="$(array_words "$f" "$var")"
                    if [ -n "$words" ]; then printf '%s\n' "$words"; else echo "?$tok"; fi ;;
                '$'*)
                    var="${tok#'$'}"; var="${var#'{'}"; var="${var%'}'}"
                    words="$(scalar_value "$f" "$var")"
                    case "$words" in ''|*'$'*|*'('*) echo "?$tok" ;; *) printf '%s\n' "$words" ;; esac ;;
                *'$'*|*'('*) echo "?$tok" ;;
                *) printf '%s\n' "$tok" ;;
            esac
        done
    done <<EOF
$lines
EOF
}

check_steps() {
    local dir f label pkgs p why hadfail installed unresolved n=0
    for dir in "$@"; do
        [ -d "$dir" ] || { fail "steps: $dir is not a directory"; continue; }
        for f in "$dir"/*.sh; do
            [ -f "$f" ] || continue
            n=$((n + 1))
            label="step $(basename "$dir")/$(basename "$f")"
            hadfail=0
            scan_commands "$f" "$label" || hadfail=1
            installed=""
            unresolved=""
            pkgs="$(step_packages "$f")"
            for p in $pkgs; do
                case "$p" in
                    '?'*) unresolved="$unresolved ${p#?}" ;;
                    *)
                        case " $installed " in *" $p "*) continue ;; esac
                        installed="$installed $p"
                        if why="$(forbidden_pkg "$p")"; then
                            fail "$label installs $p ($why)"
                            hadfail=1
                        fi ;;
                esac
            done
            [ -z "$unresolved" ] || note "$label installs packages named at run time:$unresolved (not checked here)"
            [ "$hadfail" = 1 ] || ok "$label: no build tool run${installed:+; installs$installed}"
        done
    done
    [ "$n" -gt 0 ] || fail "steps: no *.sh found in $*"
}

# --- llama -------------------------------------------------------------------------

# insn_classes SO: counts of the instruction classes in SO's code, as
# "dotprod N fp16 N i8mm N sve N sme N unknown N", from objdump -d (GNU
# binutils: "addr:<TAB>hex <TAB>mnem<TAB>ops"; LLVM: "addr: hex <TAB>mnem<TAB>ops").
insn_classes() {
    "$OBJDUMP" -d "$1" 2> /dev/null | awk '
        BEGIN { d = h = m = s = z = u = 0 }
        /^[[:space:]]*[0-9a-f]+:/ {
            n = split($0, parts, "\t")
            if (n < 2) next
            if (parts[1] ~ /:[[:space:]]*$/) { first = 3 } else { first = 2 }
            if (n < first) next
            mnem = parts[first]
            sub(/^[[:space:]]+/, "", mnem); sub(/[[:space:]].*$/, "", mnem)
            ops = ""
            for (i = first + 1; i <= n; i++) ops = ops (i > first + 1 ? " " : "") parts[i]
            sub(/^[[:space:]]+/, "", ops)
            if (mnem == "sdot" || mnem == "udot") d++
            else if (mnem == "smmla" || mnem == "ummla" || mnem == "usmmla" || mnem == "sudot" || mnem == "usdot") m++
            else if (mnem == "smstart" || mnem == "smstop" || ops ~ /(^|[[:space:],{])za[0-9]*[.hbsdq]/ || ops ~ /(^|[[:space:],{])za([[:space:],}]|$)/) z++
            else if (ops ~ /(^|[[:space:],{])z[0-9]+\.[bhsdq]/ || ops ~ /(^|[[:space:],])p[0-9]+(\/[mz]|\.[bhsdq])/) s++
            else if (mnem ~ /^(fadd|fsub|fmul|fdiv|fmla|fmls|fmax|fmin|fmaxnm|fminnm|fabs|fneg|fsqrt|fcmeq|fcmgt|fcmge|fmulx|frecpe|frsqrte|fabd|faddp|fmaxp|fminp|fmaxnmp|fminnmp)$/ && ops ~ /^(v[0-9]+\.[48]h|h[0-9]+,)/) h++
            else if (mnem == ".inst" || mnem == "<unknown>" || mnem == "udf" || mnem ~ /^\.(word|byte|short|long|4byte)$/) u++
        }
        END { printf "dotprod %d fp16 %d i8mm %d sve %d sme %d unknown %d\n", d, h, m, s, z, u }'
}

# class_set CLASSES: the classes with a nonzero count, space separated (unknown left out).
class_set() {
    printf '%s\n' "$1" | awk '{ s = ""; for (i = 1; i < NF; i += 2) if ($(i + 1) > 0 && $i != "unknown") s = s " " $i; print substr(s, 2) }'
}

# class_score SET: ggml_backend_score's weights for arm (cpu-feats): dotprod 2,
# fp16 4, sve 8, i8mm 16, sme 64, plus 1; a variant with a feature the CPU
# lacks scores 0, which best_variant handles by leaving it out.
class_score() {
    local c s=1
    for c in $1; do
        case "$c" in dotprod) s=$((s + 2)) ;; fp16) s=$((s + 4)) ;; sve) s=$((s + 8)) ;; i8mm) s=$((s + 16)) ;; sme) s=$((s + 64)) ;; esac
    done
    echo "$s"
}

# best_variant FEATURES: the variant with the highest score among those whose
# classes are all in FEATURES (space separated), from $WORK/variants.
best_variant() {
    local feats="$1" v set c fits best="" bests=0 score
    while IFS='|' read -r v set; do
        [ -n "$v" ] || continue
        fits=1
        for c in $set; do case " $feats " in *" $c "*) ;; *) fits=0 ;; esac; done
        [ "$fits" = 1 ] || continue
        score="$(class_score "$set")"
        if [ "$score" -gt "$bests" ]; then best="$v"; bests="$score"; fi
    done < "$WORK/variants"
    printf '%s\n' "$best"
}

# hwcap_features WORDS: a /proc/cpuinfo Features line, or glibc's decoded
# AT_HWCAP (LD_SHOW_AUXV=1), as the classes above.
hwcap_features() {
    local w out=""
    for w in $1; do
        case "$w" in
            asimddp) out="$out dotprod" ;;
            asimdhp) out="$out fp16" ;;
            i8mm) out="$out i8mm" ;;
            sve) out="$out sve" ;;
            sme) out="$out sme" ;;
        esac
    done
    printf '%s\n' "${out# }"
}

# wait_http URL TRIES: until URL answers 200 (TRIES half seconds) while the server lives.
wait_http() {
    local _
    for _ in $(seq 1 "$2"); do
        [ "$(curl -s -o /dev/null -w '%{http_code}' "$1")" = 200 ] && return 0
        kill -0 "$SERVER" 2> /dev/null || return 1
        sleep 0.5
    done
    return 1
}

# llama_in_builder DIR MODEL: the whole llama check inside the arm64 builder.
llama_in_builder() {
    local rel="${1#"$BRAINSYNCOS_ROOT"/}" mrel="${2#"$BRAINSYNCOS_ROOT"/}" rc=0
    case "$rel$mrel" in
        /*) fail "llama: $1 and $2 must be under brainsyncos/ to be checked in the builder"; return 0 ;;
    esac
    note "llama: running the llama check in the arm64 builder (scripts/builder.sh run scripts/check-no-compile.sh --only llama)"
    "$BRAINSYNCOS_ROOT/scripts/builder.sh" run scripts/check-no-compile.sh --only llama --llama "$rel" --model "$mrel" \
        --cpu-model "$cpu_model" --host-arch "$(uname -m)" > "$WORK/builder.out" 2> "$WORK/builder.err" || rc=$?
    grep -v '^no-compile: ' "$WORK/builder.out" || true
    [ "$rc" = 0 ] || grep -v '^\[bsos:' "$WORK/builder.err" | tail -20 | sed 's/^/      | /'
    if [ "$rc" = 0 ]; then
        nok=$((nok + 1))
    else
        fail "llama: the check in the builder failed (see the lines above)"
    fi
}

check_llama() {
    local dir="$1" v missing="" n set out a78="dotprod fp16" best bestset log mode="" pre="" feats loaded want tok
    local port=19932 secs=240
    if [ "$dir" = none ]; then
        skip "llama: not requested (--llama none)"
        return 0
    fi
    if [ ! -d "$dir" ]; then
        skip "llama: no $dir (scripts/build.sh llama builds it); nothing checked"
        return 0
    fi
    load_env_file "$BRAINSYNCOS_ROOT/llama/llama.env" "CPU_VARIANTS TEST_MODEL_FILE TEST_MODEL_SHA256 TEST_MODEL_SIZE LLAMA_PREFIX"
    [ -n "$model" ] || model="$DOWNLOADS_DIR/llama-test/$TEST_MODEL_FILE"
    # On a host that is not Linux arm64, the builder does all of it when docker runs.
    if ! in_container && { [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != aarch64 ]; }; then
        if docker info > /dev/null 2>&1; then
            llama_in_builder "$dir" "$model"
            return 0
        fi
        note "llama: docker is not available, so the static checks run on this host and llama-server is not run"
    fi

    # 1. Every variant is there.
    n=0
    for v in $(printf '%s\n' "$CPU_VARIANTS" | tr ',' ' '); do
        n=$((n + 1))
        [ -f "$dir/lib/ggml/libggml-cpu-$v.so" ] || missing="$missing $v"
    done
    if [ -n "$missing" ]; then
        fail "llama: lib/ggml lacks CPU variant(s):$missing (rebuild: scripts/build.sh llama --force)"
        return 0
    fi
    ok "llama: all $n CPU variants in lib/ggml ($CPU_VARIANTS)"
    [ -x "$dir/bin/llama-server" ] || { fail "llama: no executable $dir/bin/llama-server"; return 0; }

    # 2. What each variant is compiled with, from its instructions.
    OBJDUMP="$(command -v objdump || command -v llvm-objdump || command -v aarch64-linux-gnu-objdump || true)"
    : > "$WORK/variants"
    if [ -z "$OBJDUMP" ]; then
        skip "llama: no objdump here, so the variants' instruction sets were not classed"
    else
        for v in $(printf '%s\n' "$CPU_VARIANTS" | tr ',' ' '); do
            [ -f "$dir/lib/ggml/libggml-cpu-$v.so" ] || continue
            out="$(insn_classes "$dir/lib/ggml/libggml-cpu-$v.so")"
            set="$(class_set "$out")"
            printf '%s|%s\n' "$v" "$set" >> "$WORK/variants"
            printf 'insn  libggml-cpu-%s.so: %s -> {%s}\n' "$v" "$out" "$set"
        done
        # The classing itself: the baseline variant uses none of them, and some variants use SVE and SME.
        if ! grep -q '^armv8\.0_1|$' "$WORK/variants"; then
            fail "llama: the baseline variant armv8.0_1 shows extension instructions ({$(grep '^armv8\.0_1|' "$WORK/variants" | cut -d'|' -f2)}), so the classing cannot be trusted"
        elif ! grep -q 'sve' "$WORK/variants" || ! grep -q 'sme' "$WORK/variants"; then
            warn_line "llama: no variant shows SVE or SME instructions ($OBJDUMP may not decode them); the classing of dotprod, fp16 and i8mm still holds"
        else
            ok "llama: instruction classing is consistent (armv8.0_1 plain; SVE and SME seen in the newer variants)"
        fi
        best="$(best_variant "$a78")"
        bestset="$(grep "^$best|" "$WORK/variants" | cut -d'|' -f2)"
        if [ -z "$best" ]; then
            fail "llama: no variant fits a Cortex-A78AE (hwcaps dotprod and fp16; no i8mm, SVE or SME)"
        elif ! printf '%s\n' "$best" | grep -q '^armv8\.2_'; then
            fail "llama: ggml would pick $best on a Cortex-A78AE (uses {$bestset}); expected an armv8.2 variant"
        elif [ "$bestset" != "dotprod fp16" ]; then
            fail "llama: the A78AE's variant $best uses {$bestset}, not exactly dotprod and fp16"
        else
            ok "llama: on the Orin's Cortex-A78AE (hwcaps dotprod, fp16) ggml scores libggml-cpu-$best.so highest, and it uses only dotprod and fp16 instructions"
        fi
    fi

    # 3. Live: llama-server with the test model.
    if [ ! -f "$model" ]; then
        skip "llama: test model ${model#"$BRAINSYNCOS_ROOT"/} missing (scripts/build.sh llama fetches it); llama-server not run"
        return 0
    fi
    [ "$(file_size "$model")" = "$TEST_MODEL_SIZE" ] && [ "$(sha256_of "$model")" = "$TEST_MODEL_SHA256" ] \
        || { fail "llama: $model does not match TEST_MODEL_SIZE/TEST_MODEL_SHA256 in llama/llama.env"; return 0; }
    if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != aarch64 ]; then
        skip "llama: llama-server runs only on Linux arm64 (the builder); live run not done on $(uname -s) $(uname -m)"
        return 0
    fi
    require_cmd curl
    # ggml finds its CPU backends next to the executable or in the directory
    # compiled in (LLAMA_PREFIX/lib/ggml). Under qemu-user the first is qemu's,
    # so run the tree where the package installs it, as build-llama.sh check does.
    if [ "$dir" != "$LLAMA_PREFIX" ]; then
        in_container || { skip "llama: the live run installs the tree at $LLAMA_PREFIX, so it runs only in the builder"; return 0; }
        rm -rf "${LLAMA_PREFIX:?}"
        mkdir -p "$(dirname "$LLAMA_PREFIX")"
        cp -a "$dir" "$LLAMA_PREFIX"
        dir="$LLAMA_PREFIX"
    fi
    if command -v qemu-aarch64 > /dev/null 2>&1; then
        mode="emulated $cpu_model (qemu-aarch64 -cpu $cpu_model)"
        pre="qemu-aarch64 -cpu $cpu_model"
        secs=1200
    elif [ "$host_arch" = x86_64 ] || [ "$host_arch" = amd64 ]; then
        mode="emulated $cpu_model (the builder runs under qemu binfmt on an x86-64 host; QEMU_CPU=$cpu_model)"
        export QEMU_CPU="$cpu_model"
        secs=1200
    else
        mode="this host's own CPU (arm64 host, no qemu-aarch64 in the builder): not a Cortex-A76 model"
    fi
    # qemu-user fakes /proc/cpuinfo for the modelled CPU, so read it through the same prefix.
    # shellcheck disable=SC2086  # $pre is a command prefix, split on purpose
    feats="$($pre /bin/grep -m1 '^Features' /proc/cpuinfo 2> /dev/null | sed 's/^Features[[:space:]]*:[[:space:]]*//')"
    printf 'live  llama-server runs on: %s; hwcaps: %s\n' "$mode" "${feats:-unknown}"
    log="$WORK/server.log"
    # shellcheck disable=SC2086  # $pre is a command prefix, split on purpose
    $pre "$dir/bin/llama-server" --host 127.0.0.1 --port "$port" -m "$model" \
        -c 512 -np 1 -t 2 --no-webui --offline -v > "$log" 2>&1 &   # -v: the "loaded CPU backend" line is logged only verbose
    SERVER=$!
    if ! wait_http "http://127.0.0.1:$port/health" "$((secs * 2))"; then
        tail -20 "$log" | sed 's/^/      | /'
        fail "llama: llama-server did not answer /health within $secs s ($mode)"
        stop_server
        return 0
    fi
    out="$(curl -sf --max-time "$secs" -H 'Content-Type: application/json' \
        -d '{"prompt": "The capital of France is", "n_predict": 8, "temperature": 0}' "http://127.0.0.1:$port/completion")" || out=""
    stop_server
    tok="$(printf '%s' "$out" | python3 -I -c 'import json, sys
d = json.load(sys.stdin); print(d["tokens_predicted"], repr(d["content"]))' 2> /dev/null || true)"
    case "$tok" in
        '') tail -20 "$log" | sed 's/^/      | /'; fail "llama: /completion gave no tokens ($mode): ${out:-no answer}" ;;
        0*) fail "llama: /completion predicted 0 tokens ($mode)" ;;
        *) ok "llama: /completion returned ${tok%% *} tokens (${tok#* }) on $mode" ;;
    esac
    loaded="$(grep -oE 'loaded CPU backend from [^ ]*/libggml-cpu-[a-z0-9._]*\.so' "$log" | head -n 1 | sed 's|.*/libggml-cpu-||; s|\.so$||')"
    if [ -z "$loaded" ]; then
        tail -20 "$log" | sed 's/^/      | /'
        fail "llama: the log does not say which CPU backend was loaded from lib/ggml"
        return 0
    fi
    case "$mode" in
        emulated*)
            if printf '%s\n' "$loaded" | grep -q '^armv8\.2_'; then
                ok "llama: under $cpu_model llama-server loaded libggml-cpu-$loaded.so, an armv8.2 variant, as the A78AE will"
            else
                fail "llama: under $cpu_model llama-server loaded libggml-cpu-$loaded.so, not an armv8.2 variant"
            fi ;;
        *)
            want="$(best_variant "$(hwcap_features "$feats")")"
            if [ -s "$WORK/variants" ] && [ -n "$want" ]; then
                if [ "$want" = "$loaded" ]; then
                    ok "llama: natively llama-server loaded libggml-cpu-$loaded.so, the variant the disassembly predicts for this CPU's hwcaps ($(hwcap_features "$feats")); the A78AE prediction above rests on the same classing"
                else
                    warn_line "llama: natively llama-server loaded libggml-cpu-$loaded.so, but the disassembly predicts $want for hwcaps ($(hwcap_features "$feats")); the armv8.2 requirement itself was not exercised (no Cortex-A76 emulation here)"
                fi
            else
                note "llama: natively llama-server loaded libggml-cpu-$loaded.so; the armv8.2 requirement was not exercised (no Cortex-A76 emulation here)"
            fi ;;
    esac
}

# --- run -------------------------------------------------------------------------------

if wanted repo; then check_repo "$repo"; fi
if wanted steps; then check_steps "${steps[@]}" "${libs[@]}"; fi
if wanted llama; then check_llama "$llama"; fi

if [ "$nfail" -gt 0 ]; then
    echo "no-compile: $nfail FAILED, $nok ok"
    exit 1
fi
echo "no-compile: all $nok checks ok"
