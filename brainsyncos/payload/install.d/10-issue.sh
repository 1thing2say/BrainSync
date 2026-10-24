#!/bin/bash
# 10-issue.sh - name the BrainSyncOS build on the console login screen (/etc/issue).
#
# Runs inside the installed system (hook.sh, curtin in-target), after
# 00-release.sh. Puts "BrainSyncOS VERSION (build ID)" above Ubuntu's line, so a
# serial or HDMI console shows at a glance which build is installed. The VM
# edition says so and adds the VM's IPv4 address (agetty's \4), which is what
# you need to SSH in from the Mac.

set -euo pipefail

# shellcheck disable=SC1091
. /etc/brainsyncos-release
case "${EDITION:-orin-nano}" in
    orin-nano) line="BrainSyncOS ${VERSION:?} (build ${BUILD_ID:?})" ;;
    # \4 is expanded by agetty, not by the shell.
    vm-arm64) line="BrainSyncOS ${VERSION:?} VM edition (build ${BUILD_ID:?}), IP \\4" ;;
    *) echo "10-issue.sh: unknown EDITION $EDITION" >&2; exit 1 ;;
esac

touch /etc/issue
grep -v '^BrainSyncOS ' /etc/issue > /etc/.issue.tmp || true
{ printf '%s\n' "$line"; cat /etc/.issue.tmp; } > /etc/issue
rm -f /etc/.issue.tmp
head -2 /etc/issue
