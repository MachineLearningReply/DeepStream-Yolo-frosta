#!/usr/bin/env bash
# Collects all evidence about a Jetson freeze/restart into one folder and one .tar.gz, ready to copy to the Mac.
# Only reads system files; writes only into ~/crash_<hostname>_<date_time>/ and its .tar.gz.
#
#   bash ~/DeepStream-Yolo-frosta/deepstream_pipeline/monitoring/collect_crash.sh
#
# Run it as the normal user (it asks for the sudo password once). Run it BEFORE `docker compose down`,
# otherwise the container output is gone.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="crash_$(hostname)_$(date +%Y%m%d_%H%M%S)"
OUT="$HOME/$NAME"
mkdir -p "$OUT"/{pstore,monitor_runs,pipeline_logs,docker}

step() { echo; echo "=== $*"; }
note() { echo "  note: $*"; echo "$*" >> "$OUT/notes.txt"; }

step "sudo (needed for the crash report and the kernel log)"
sudo -v || { echo "sudo is required"; exit 1; }

step "Summary"
{
    for cmd in "date" "uptime -s" "uptime" "last -x reboot shutdown" "nvpmodel -q" "cat /etc/nv_tegra_release" \
               "free -m" "df -h /" "sudo ls -la /sys/fs/pstore/"; do
        echo "\$ $cmd"
        if [ "$cmd" = "last -x reboot shutdown" ]; then $cmd 2>&1 | head -20; else $cmd 2>&1; fi
        echo
    done
} > "$OUT/summary.txt"
grep -A1 "uptime -s" "$OUT/summary.txt" | tail -1 | sed 's/^/  running since: /'

step "Kernel crash report (pstore)"
if sudo find /sys/fs/pstore -maxdepth 1 -type f | grep -q .; then
    sudo find /sys/fs/pstore -maxdepth 1 -type f -exec cp {} "$OUT/pstore/" \;
    ls -la "$OUT/pstore" | tail -n +2 | sed 's/^/  /'
else
    note "no crash report in /sys/fs/pstore"
fi

step "Kernel log (last 50 MB)"
if sudo test -f /var/log/kern.log; then
    sudo tail -c 50M /var/log/kern.log > "$OUT/kern_tail.log"
    # After a log rotation the current file can be short; then the end of the previous one matters
    if [ "$(stat -c %s "$OUT/kern_tail.log")" -lt 5000000 ] && sudo test -f /var/log/kern.log.1; then
        sudo tail -c 50M /var/log/kern.log.1 > "$OUT/kern1_tail.log"
    fi
else
    note "/var/log/kern.log not found"
fi

step "Journal of the previous boot"
sudo journalctl -k -b -1 --no-pager > "$OUT/journal_prev_boot.txt" 2>&1
head -1 "$OUT/journal_prev_boot.txt" | sed 's/^/  /'

step "Monitor recordings"
if [ -d "$REPO/deepstream_pipeline/monitoring/runs" ] && [ -n "$(ls -A "$REPO/deepstream_pipeline/monitoring/runs")" ]; then
    cp -r "$REPO/deepstream_pipeline/monitoring/runs/." "$OUT/monitor_runs/"
    ls "$OUT/monitor_runs" | sed 's/^/  /'
else
    note "no monitor recordings in $REPO/deepstream_pipeline/monitoring/runs"
fi

step "Pipeline logs"
if ls "$REPO"/deepstream_pipeline/logs/deepstream_pipeline_*.log* > /dev/null 2>&1; then
    cp "$REPO"/deepstream_pipeline/logs/deepstream_pipeline_*.log* "$OUT/pipeline_logs/"
    ls "$OUT/pipeline_logs" | sed 's/^/  /'
else
    note "no pipeline logs in $REPO/deepstream_pipeline/logs"
fi

step "Docker containers"
if command -v docker > /dev/null && docker info > /dev/null 2>&1; then
    docker ps -a > "$OUT/docker/ps.txt" 2>&1
    if [ -f "$REPO/docker/compose.yaml" ]; then
        docker compose -f "$REPO/docker/compose.yaml" logs --timestamps --no-color > "$OUT/docker/compose_logs.txt" 2>&1
    fi
    sed 's/^/  /' "$OUT/docker/ps.txt"
else
    note "docker not usable for this user (not installed, or user not in group 'docker')"
fi

step "Key kernel lines (quick first look)"
PATTERN="rcu_preempt|Task dump|cuda-EvtHandlr|nvme.*timeout|BPMP|bpmp|unbind failed|Link is Down|Link is Up|PCS block lock|Out of memory|oom-kill|panic|watchdog|Booting Linux"
for f in "$OUT"/pstore/* "$OUT"/kern_tail.log "$OUT"/kern1_tail.log; do
    [ -f "$f" ] || continue
    echo "##### $(basename "$f")"
    grep -n -E "$PATTERN" "$f" | head -300
    echo
done > "$OUT/kernel_events_grep.txt"
echo "  $(grep -c -E 'rcu_preempt' "$OUT/kernel_events_grep.txt") CPU-stall lines, $(grep -c 'Link is Down' "$OUT/kernel_events_grep.txt") link-down lines"

step "Packing"
sudo chown -R "$(id -u):$(id -g)" "$OUT"
chmod -R a+r "$OUT"
tar -czf "$HOME/$NAME.tar.gz" -C "$HOME" "$NAME"
ls -la "$HOME/$NAME.tar.gz" | sed 's/^/  /'

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done. On the Mac, copy it with:"
echo "  scp $(id -un)@$IP:~/$NAME.tar.gz ~/Downloads/"
