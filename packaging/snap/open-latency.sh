#!/usr/bin/env bash
# How long opening a file takes, from the launch to the first frame of its
# window, the way GNOME Files does it (a new launcher process per open):
#
#   packaging/snap/open-latency.sh FILE [LAUNCHER...]
#     LAUNCHER defaults to "snap run view"; e.g. ~/.local/bin/view-app
#
# The clock starts here, before the launcher (VIEW_OPEN_T0, epoch seconds).
# When View is already running, the launch is handed to it (preview.instance)
# and the launcher prints the time once that window has drawn; otherwise the
# new View prints it at its first frame and keeps running (it is the running
# View for the next measurement).  The snap can only read files in $HOME.
set -euo pipefail
if [ $# -lt 1 ]; then
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi
file=$(realpath "$1")
shift
[ $# -gt 0 ] || set -- snap run view
log=$(mktemp "${TMPDIR:-/tmp}/view-open-latency.XXXXXX")
trap 'rm -f "$log"' EXIT

VIEW_OPEN_T0=$(date +%s.%N) "$@" "$file" 2>"$log" &
pid=$!
for _ in $(seq 600); do
    if grep -q "after VIEW_OPEN_T0" "$log"; then
        break
    fi
    if ! kill -0 "$pid" 2>/dev/null && ! grep -q "after VIEW_OPEN_T0" "$log"; then
        sleep 0.2                    # (a forwarded launch prints just before it exits)
        grep -q "after VIEW_OPEN_T0" "$log" || { echo "no measurement:"; cat "$log"; exit 1; }
        break
    fi
    sleep 0.05
done
grep -m1 "after VIEW_OPEN_T0" "$log" || { echo "timed out"; exit 1; }
