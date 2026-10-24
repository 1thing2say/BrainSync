#!/usr/bin/env bash
# compare-iso.sh - check a repacked ISO's boot structure and file tree (builder only).
#
# Usage: scripts/builder.sh run scripts/compare-iso.sh NEW.iso [--tree DIR] [--boot-only]
#                                    [--tree-only] [--extraction] [--edition jetson|vm]
#
# Compares NEW.iso against the pinned base ISO and build/meta/ (from
# extract-iso.sh), modulo the differences a rebuild is expected to introduce
# (LBAs that move with the tree size, GPT GUIDs, image length).
# --edition vm compares against the VM edition's Ubuntu ISO
# (config/vm-base-iso.env) instead, reading its El Torito reports from the
# base file itself (there is no extraction); it implies --boot-only, and its
# partition table is an MBR (check name mbr) rather than a GPT:
#   eltorito    El Torito catalog/boot entries equal except LBAs
#   boot-esp    El Torito UEFI image == appended ESP partition (LBA*4 == start)
#   mkisofs     as_mkisofs replay report equal except intervals/paths (VM:
#               also except the MBR geometry lines, see chs)
#   chs         (VM only) MBR CHS fields encode each partition's LBAs in the
#               base's geometry; the ESP is cylinder-aligned
#   descriptors volume descriptor sequence (PVD, boot record, Joliet SVD) equal
#   gpt/mbr     same partitions (type/name/attrs/order), p1 start, ESP and
#               Gap1 sizes, contiguous; sfdisk -V clean; file length == image
#   esp-bytes   ESP partition sha256 equals BASE_ESP_SHA256 (config pin)
#   names       plain ISO9660 and Joliet directory trees (raw directory
#               records, not Rock Ridge): every base name present unchanged in
#               NEW, so 7-Zip/Explorer/norock readers see the same names;
#               added names are counted, not failed
#   tree        xorriso -compare_r DIR (default build/iso-root) vs NEW.iso:
#               content, type, mode, mtime, links; only /boot/boot.cat (the El
#               Torito catalog, regenerated) and ctime may differ
# --boot-only skips the tree check (for a BrainSyncOS build, whose tree differs
# from the base on purpose; names then only requires every base name to stay);
# --tree-only runs just the tree check (e.g. base ISO vs a fresh extraction);
# --extraction also tolerates what extract-iso.sh changes on disk: permission
# bits (it adds u+w) and symlink mtimes (osirrox cannot restore them).
# Prints PASS/FAIL per check; exits non-zero if any check fails.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

new=""
edition=jetson
tree="$ISO_ROOT"
tree_only=0
boot_only=0
extraction=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --tree) tree="${2:?--tree needs a dir}"; shift 2 ;;
        --tree-only) tree_only=1; shift ;;
        --boot-only) boot_only=1; shift ;;
        --extraction) extraction=1; shift ;;
        --edition) edition="${2:?--edition needs jetson or vm}"; shift 2 ;;
        -h|--help) usage_from_header; exit 0 ;;
        -*) die "unknown argument: $1" ;;
        *) new="$1"; shift ;;
    esac
done
[ -n "$new" ] || die "usage: compare-iso.sh NEW.iso [--tree DIR]"
[ "$tree_only" = 1 ] && [ "$boot_only" = 1 ] && die "--tree-only and --boot-only exclude each other"
case "$edition" in
    jetson) load_config ;;
    vm) load_vm_config
        [ "$tree_only" = 0 ] || die "--edition vm has no extracted tree to compare (--tree-only)"
        boot_only=1 ;;
    *) die "--edition must be jetson or vm, got $edition" ;;
esac
require_container
require_cmd xorriso sfdisk python3 sha256sum
[ -f "$new" ] || die "no such ISO: $new"
if [ "$edition" = vm ]; then
    base="$(resolve_vm_base_iso)"
else
    [ -f "$META_DIR/el-torito.plain.txt" ] || die "missing $META_DIR (run extract-iso.sh first)"
    base="$(resolve_base_iso)"
fi

fails=0
pass() { printf 'PASS %-12s %s\n' "$1" "$2"; }
failc() { printf 'FAIL %-12s %s\n' "$1" "$2"; fails=$((fails + 1)); }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# Reference El Torito reports: from the extraction (Jetson) or the base file (VM).
if [ "$edition" = vm ]; then
    ref_et_plain="$work/base.et.plain"
    ref_et_mkisofs="$work/base.et.mkisofs"
    xorriso -indev "$base" -report_el_torito plain      2>/dev/null > "$ref_et_plain"
    xorriso -indev "$base" -report_el_torito as_mkisofs 2>/dev/null > "$ref_et_mkisofs"
else
    ref_et_plain="$META_DIR/el-torito.plain.txt"
    ref_et_mkisofs="$META_DIR/el-torito.as_mkisofs.txt"
fi

if [ "$tree_only" = 0 ]; then
xorriso -indev "$new" -report_el_torito plain      2>/dev/null > "$work/et.plain"
xorriso -indev "$new" -report_el_torito as_mkisofs 2>/dev/null > "$work/et.mkisofs"
sfdisk --json "$new" > "$work/new.json"
sfdisk --json "$base" > "$work/base.json"

# eltorito: blank the catalog LBA and the boot image LBA (last column)
norm_et() {
    sed -e 's/^\(El Torito catalog *: *\)[0-9]*/\1LBA/' \
        -e 's/^\(El Torito boot img *:.*[^0-9]\)[0-9][0-9]*$/\1LBA/' "$1"
}
if diff <(norm_et "$ref_et_plain") <(norm_et "$work/et.plain") > "$work/d1"; then
    pass eltorito "$(grep -c '^El Torito' "$work/et.plain") El Torito lines match modulo LBAs"
else
    failc eltorito "$(tr '\n' ' ' < "$work/d1")"
fi

# boot-esp: the El Torito UEFI image must be the appended EFI System partition
boot_lba="$(awk -F: '/^El Torito boot img/ {n=split($2, a, " "); print a[n]; exit}' "$work/et.plain")"
esp_start="$(python3 -I -c 'import json,sys; p=[x for x in json.load(open(sys.argv[1]))["partitiontable"]["partitions"] if x["type"].upper() in ("C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "EF")]; print(p[0]["start"] if p else "")' "$work/new.json")"
if [ -n "$boot_lba" ] && [ -n "$esp_start" ] && [ $((boot_lba * 4)) = "$esp_start" ]; then
    pass boot-esp "boot image LBA $boot_lba x4 = ESP start sector $esp_start"
else
    failc boot-esp "boot image LBA '$boot_lba' does not map to ESP start '$esp_start'"
fi

# mkisofs: replay report, with interval addresses and image paths blanked
norm_mk() {
    sed -e "s/local_fs:[0-9]*d-[0-9]*d/local_fs:RANGE/" \
        -e "s/appended_partition_2_start_[0-9]*s/appended_partition_2_start_Xs/" \
        -e "s/::'[^']*'/::'IMG'/" -e "s/:zero_mbrpt,zero_gpt:'[^']*'/:zero_mbrpt,zero_gpt:'IMG'/" \
        -e "s/:zero_mbrpt:'[^']*'/:zero_mbrpt:'IMG'/" "$1" |
    if [ "$edition" = vm ]; then
        # Re-derived by xorriso from the MBR's CHS fields on reading, which
        # fails once a partition ends beyond cylinder 1023 (CHS saturates);
        # the chs check below verifies the geometry and alignment directly.
        grep -Ev '^-partition_(cyl_align|hd_cyl|sec_hd) '
    else
        cat
    fi
}
if diff <(norm_mk "$ref_et_mkisofs") <(norm_mk "$work/et.mkisofs") > "$work/d2"; then
    pass mkisofs "$(norm_mk "$work/et.mkisofs" | wc -l | tr -d ' ') replay options match modulo addresses"
else
    failc mkisofs "$(tr '\n' ' ' < "$work/d2")"
fi

# chs (VM edition: MBR with cylinder alignment): every MBR entry's CHS start
# and end encode its LBAs in the base's geometry (saturated at cylinder 1023
# as xorriso writes it), and the appended ESP starts and ends on a cylinder
# boundary (-partition_cyl_align all). Checked on the base too, as a control.
if [ "$edition" = vm ]; then
    heads="$(awk '$1 == "-partition_hd_cyl" {print $2}' "$ref_et_mkisofs")"
    secs="$(awk '$1 == "-partition_sec_hd" {print $2}' "$ref_et_mkisofs")"
    chs_py='
import struct, sys
path, H, S = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
mbr = open(path, "rb").read(512)
def chs(lba):
    c = lba // (H * S)
    if c > 1023:
        return (1023, H - 1, S)
    return (c, (lba // S) % H, lba % S + 1)
def dec(b):
    return (((b[1] & 0xC0) << 2) | b[2], b[0], b[1] & 0x3F)
errs, parts = [], []
for i in range(4):
    e = mbr[446 + 16 * i:462 + 16 * i]
    start, size = struct.unpack_from("<II", e, 8)
    if e[4] == 0:
        continue
    parts.append((i + 1, e[4], start, size))
    for what, lba, raw in (("start", start, e[1:4]), ("end", start + size - 1, e[5:8])):
        if dec(raw) != chs(lba):
            errs.append("p%d %s CHS %s != %s for LBA %d" % (i + 1, what, dec(raw), chs(lba), lba))
for n, t, start, size in parts:
    if t == 0xEF and (start % (H * S) or (start + size) % (H * S)):
        errs.append("ESP p%d %d+%d not cylinder-aligned (%d sectors)" % (n, start, size, H * S))
if errs:
    print("; ".join(errs)); sys.exit(1)
print("%d entries" % len(parts))
'
    if [ -z "$heads" ] || [ -z "$secs" ]; then
        failc chs "base report has no MBR geometry"
    elif ! b="$(python3 -I -c "$chs_py" "$base" "$heads" "$secs")"; then
        failc chs "control failed, base MBR: $b"
    elif out="$(python3 -I -c "$chs_py" "$new" "$heads" "$secs")"; then
        pass chs "$out encode their LBAs in the base geometry ${heads}x${secs}; ESP cylinder-aligned (base: $b, same)"
    else
        failc chs "$out"
    fi
fi

# descriptors: type byte of each volume descriptor from LBA 16 to the terminator
vd_types() {
    local i=16 t out=""
    while [ "$i" -lt 32 ]; do
        t="$(dd if="$1" bs=2048 skip="$i" count=1 2>/dev/null | od -An -tu1 -N1 | tr -d ' ')"
        out="$out $t"
        [ "$t" = 255 ] && break
        i=$((i + 1))
    done
    printf '%s\n' "${out# }"
}
vd_base="$(vd_types "$base")"; vd_new="$(vd_types "$new")"
if [ "$vd_base" = "$vd_new" ]; then
    pass descriptors "volume descriptor types [$vd_new] (1=PVD 0=boot 2=Joliet 255=end)"
else
    failc descriptors "base [$vd_base] vs new [$vd_new]"
fi

# gpt (mbr for the VM edition's MBR-only base): structure equal, sizes that
# must not change unchanged, layout contiguous
pt=gpt
[ "$edition" = vm ] && pt=mbr
if out="$(python3 -I - "$work/base.json" "$work/new.json" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))["partitiontable"]
n = json.load(open(sys.argv[2]))["partitiontable"]
errs = []
if b["label"] != n["label"]:
    errs.append("label %s != %s" % (b["label"], n["label"]))
bp, np_ = b["partitions"], n["partitions"]
if len(bp) != len(np_):
    errs.append("partition count %d != %d" % (len(bp), len(np_)))
for i, (x, y) in enumerate(zip(bp, np_), 1):
    for k in ("type", "name", "attrs"):
        if x.get(k) != y.get(k):
            errs.append("p%d %s %r != %r" % (i, k, x.get(k), y.get(k)))
    if i != 1 and x["size"] != y["size"]:
        errs.append("p%d size %d != %d" % (i, x["size"], y["size"]))
if bp and np_ and bp[0]["start"] != np_[0]["start"]:
    errs.append("p1 start %d != %d" % (bp[0]["start"], np_[0]["start"]))
for i in range(len(np_) - 1):
    if np_[i]["start"] + np_[i]["size"] != np_[i + 1]["start"]:
        errs.append("p%d not contiguous with p%d" % (i + 1, i + 2))
if errs:
    print("; ".join(errs)); sys.exit(1)
print(", ".join("p%d %s+%d %s" % (i, p["start"], p["size"], p.get("name", "")) for i, p in enumerate(np_, 1)))
PY
)"; then
    img_sectors="$(xorriso -indev "$new" -report_system_area plain 2>/dev/null | awk '/ISO image size\/512/ {print $NF}')"
    file_sectors=$(( $(file_size "$new") / 512 ))
    if ! sfdisk -V "$new" > "$work/v" 2>&1 || ! grep -q 'No errors detected' "$work/v"; then
        failc "$pt" "sfdisk -V: $(tr '\n' ' ' < "$work/v")"
    elif [ "$img_sectors" != "$file_sectors" ]; then
        failc "$pt" "file is $file_sectors sectors, image is $img_sectors (not truncated?)"
    else
        pass "$pt" "$out; sfdisk -V clean; $file_sectors sectors"
    fi
else
    failc "$pt" "$out"
fi

# esp-bytes
esp_size="$(python3 -I -c 'import json,sys; p=[x for x in json.load(open(sys.argv[1]))["partitiontable"]["partitions"] if x["type"].upper() in ("C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "EF")]; print(p[0]["size"] if p else 0)' "$work/new.json")"
esp_sum="$(dd if="$new" bs=4M iflag=skip_bytes,count_bytes skip=$(( ${esp_start:-0} * 512 )) \
    count=$((esp_size * 512)) 2>/dev/null | sha256sum | awk '{print $1}')"
if [ "$esp_sum" = "$BASE_ESP_SHA256" ]; then
    pass esp-bytes "ESP sha256 $esp_sum identical to the pinned base ESP"
else
    failc esp-bytes "ESP sha256 $esp_sum != pinned BASE_ESP_SHA256 $BASE_ESP_SHA256"
fi

# names: the plain ISO9660 and Joliet trees, which -compare_r (Rock Ridge) cannot see
iso_names() {
    python3 -I - "$1" "$2" <<'PY'
import struct, sys
path, which = sys.argv[1], sys.argv[2]
f = open(path, "rb")
def blocks(lba, n=1):
    f.seek(lba * 2048)
    return f.read(2048 * n)
root, lba = None, 16
while True:
    d = blocks(lba)
    if d[1:6] != b"CD001" or d[0] == 255:
        break
    if which == "iso9660" and d[0] == 1:
        root = d[156:190]
    if which == "joliet" and d[0] == 2 and d[88:91] in (b"%/@", b"%/C", b"%/E"):
        root = d[156:190]
    lba += 1
if root is None:
    sys.exit("no %s volume descriptor" % which)
out, seen = set(), set()
def walk(rec, prefix):
    ext, size = struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0]
    if ext in seen:
        return
    seen.add(ext)
    data, i = blocks(ext, (size + 2047) // 2048)[:size], 0
    while i < len(data):
        ln = data[i]
        if ln == 0:  # records never cross a block boundary
            i = (i // 2048 + 1) * 2048
            continue
        r = data[i:i + ln]
        i += ln
        raw = r[33:33 + r[32]]
        if raw in (b"\x00", b"\x01"):
            continue
        name = raw.decode("utf-16-be") if which == "joliet" else raw.decode("latin-1")
        isdir = r[25] & 2
        out.add(("d " if isdir else "f ") + prefix + "/" + name)
        if isdir:
            walk(r, prefix + "/" + name)
walk(root, "")
print("\n".join(sorted(out)))
PY
}
names_bad=""
names_ok=""
for ns in iso9660 joliet; do
    if ! iso_names "$base" "$ns" > "$work/names.base" 2> "$work/names.err" \
        || ! iso_names "$new" "$ns" > "$work/names.new" 2>> "$work/names.err"; then
        names_bad="$names_bad $ns: $(tr '\n' ' ' < "$work/names.err");"
        continue
    fi
    missing="$(LC_ALL=C comm -23 "$work/names.base" "$work/names.new")"
    added="$(LC_ALL=C comm -13 "$work/names.base" "$work/names.new" | wc -l | tr -d ' ')"
    if [ -n "$missing" ]; then
        names_bad="$names_bad $ns: $(printf '%s\n' "$missing" | wc -l | tr -d ' ') base names missing/changed, first: $(printf '%s\n' "$missing" | head -3 | tr '\n' ' ');"
    else
        names_ok="$names_ok$ns $(wc -l < "$work/names.base" | tr -d ' ') names equal (+$added added); "
    fi
done
if [ -z "$names_bad" ]; then
    pass names "${names_ok%; }"
else
    failc names "$names_bad"
fi

fi # tree_only

if [ "$boot_only" = 0 ]; then
# tree
# Lines look like: <type> '<disk path>' (DISK) : <field> : <disk> <> <iso>
xorriso -indev "$new" -compare_r "$tree" / > "$work/cmp" 2>&1 || true
if grep -q -e 'Differences detected' -e 'match as far as expectable' "$work/cmp"; then
    unexpected="$(awk -v ext="$extraction" '
        /^[-dl?] \x27/ {
            if ($0 ~ /\/boot\/boot\.cat\x27/) next              # El Torito catalog node
            split($0, a, /\) : /); f = a[2]; sub(/ *:.*/, "", f)
            if (f == "st_ctime") next                            # never settable
            if (ext == 1 && f == "st_mode") next
            if (ext == 1 && f == "st_mtime" && substr($0, 1, 1) == "l") next
            print
        }' "$work/cmp")"
    ignored="$(grep -c "^[-dl?] '" "$work/cmp" || true)"
    if [ -z "$unexpected" ]; then
        pass tree "$tree == ISO tree (content, type, links$([ "$extraction" = 1 ] || printf ', mode, mtime'); $ignored expected attribute diffs ignored)"
    else
        failc tree "$(printf '%s\n' "$unexpected" | wc -l | tr -d ' ') differences, first: $(printf '%s\n' "$unexpected" | head -3 | tr '\n' ' ')"
    fi
else
    failc tree "xorriso -compare_r produced no verdict: $(tail -3 "$work/cmp" | tr '\n' ' ')"
fi
fi # boot_only

if [ "$fails" -gt 0 ]; then
    log "$fails check(s) failed for $new"
    exit 1
fi
log "all checks passed for $new"
