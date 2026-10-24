#!/usr/bin/env bash
# testlib.sh - PASS/FAIL check runner shared by tests/test_phase*.sh. Source it
# after scripts/lib.sh and set LOGS (per-check log directory) first.
# Host side: must stay bash 3.2 compatible, like lib.sh.

npass=0
nfail=0
failed=""

# check NAME CMD...: run CMD with output in $LOGS/NAME.log, report PASS/FAIL.
check() {
    local name="$1" t0 rc
    shift
    t0="$(date +%s)"
    # Subshell outside any if/&&/|| so set -e stays in force inside the check.
    set +e
    ( set -e; "$@" ) > "$LOGS/$name.log" 2>&1
    rc=$?
    set -e
    if [ "$rc" = 0 ]; then
        npass=$((npass + 1))
        printf 'PASS  %-22s (%ss)\n' "$name" $(( $(date +%s) - t0 ))
    else
        nfail=$((nfail + 1))
        failed="$failed $name"
        printf 'FAIL  %-22s (rc=%s, log: %s)\n' "$name" "$rc" "${LOGS#$BRAINSYNCOS_ROOT/}/$name.log"
        tail -5 "$LOGS/$name.log" | sed 's/^/      | /'
    fi
    return 0
}

# finish LABEL: print the summary and exit 1 if any check failed.
finish() {
    echo
    if [ "$nfail" -gt 0 ]; then
        echo "$1: $npass passed, $nfail FAILED:$failed"
        exit 1
    fi
    echo "$1: all $npass checks PASSED"
}
