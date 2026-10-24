#!/bin/bash
# 10-issue.sh - name the BrainSyncOS build on the console login screen (/etc/issue).
#
# Runs inside the installed system (hook.sh, curtin in-target), after
# 00-release.sh. Puts "BrainSyncOS VERSION (build ID)" above Ubuntu's line, so a
# serial or HDMI console shows at a glance which build is installed.

set -euo pipefail

# shellcheck disable=SC1091
. /etc/brainsyncos-release
line="BrainSyncOS ${VERSION:?} (build ${BUILD_ID:?})"

touch /etc/issue
grep -v '^BrainSyncOS ' /etc/issue > /etc/.issue.tmp || true
{ printf '%s\n' "$line"; cat /etc/.issue.tmp; } > /etc/issue
rm -f /etc/.issue.tmp
head -2 /etc/issue
