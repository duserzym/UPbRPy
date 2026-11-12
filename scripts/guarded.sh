#!/usr/bin/env bash
# Run a command under a memory and time watchdog (macOS has no working ulimit -v).
#
#   scripts/guarded.sh [-m MEGABYTES] [-t SECONDS] -- command [args...]
#
# Every 2 s the resident memory of the command's whole process tree is summed
# (ps -o rss). The tree is killed when it exceeds the limit (default 3072 MB)
# or runs longer than the time limit (default 600 s). Exit status: the
# command's own, or 137 (memory) / 124 (time) when the watchdog stopped it.
set -u

limit_mb=${GUARD_MB:-3072}
limit_s=${GUARD_SECONDS:-600}
while getopts "m:t:" option; do
    case "$option" in
        m) limit_mb=$OPTARG ;;
        t) limit_s=$OPTARG ;;
        *) echo "usage: $0 [-m MB] [-t SECONDS] -- command..." >&2; exit 2 ;;
    esac
done
shift $((OPTIND - 1))
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { echo "usage: $0 [-m MB] [-t SECONDS] -- command..." >&2; exit 2; }

"$@" &
root=$!

tree() {  # the root and all its descendants
    local pids=$1 frontier=$1 children
    while [ -n "$frontier" ]; do
        children=$(for pid in $frontier; do pgrep -P "$pid"; done | tr '\n' ' ')
        pids="$pids $children"
        frontier=$children
    done
    echo "$pids"
}

stop() {
    local pids
    pids=$(tree "$root")
    kill -TERM $pids 2>/dev/null
    sleep 2
    kill -KILL $pids 2>/dev/null
}

trap 'stop; exit 130' INT TERM
start=$(date +%s)
peak=0
while kill -0 "$root" 2>/dev/null; do
    rss_kb=$(ps -o rss= -p "$(tree "$root" | tr -s ' ' ',' | sed 's/^,//;s/,$//')" 2>/dev/null \
             | awk '{sum += $1} END {print sum + 0}')
    used_mb=$((rss_kb / 1024))
    [ "$used_mb" -gt "$peak" ] && peak=$used_mb
    if [ "$used_mb" -gt "$limit_mb" ]; then
        echo "guarded.sh: memory ${used_mb} MB exceeds ${limit_mb} MB; stopping: $*" >&2
        stop
        exit 137
    fi
    if [ $(( $(date +%s) - start )) -gt "$limit_s" ]; then
        echo "guarded.sh: running longer than ${limit_s} s; stopping: $*" >&2
        stop
        exit 124
    fi
    sleep 2
done
wait "$root"
status=$?
echo "guarded.sh: peak ${peak} MB, $(( $(date +%s) - start )) s, exit ${status}" >&2
exit $status
