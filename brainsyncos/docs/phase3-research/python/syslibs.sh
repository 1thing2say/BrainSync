#!/bin/bash
# System shared libs (outside the venv) needed by compiled extensions in the venv, mapped to packages.
V=${1:-/opt/brainsync/venv}
find "$V" -name "*.so*" -type f -print0 | xargs -0 ldd 2>/dev/null > /tmp/ldd.txt
grep "not found" /tmp/ldd.txt | sort -u
grep "=>" /tmp/ldd.txt | awk '{print $3}' | grep -v "^$V" | sort -u > /tmp/libs.txt
echo "distinct system libs: $(wc -l < /tmp/libs.txt)"
while read -r l; do r=$(readlink -f "$l"); dpkg -S "$r" 2>/dev/null || dpkg -S "${r#/usr}" 2>/dev/null || echo "?: $r"; done < /tmp/libs.txt | cut -d: -f1 | sort -u | tr '\n' ' '; echo
cat /tmp/libs.txt | xargs -n1 basename | tr '\n' ' '; echo
