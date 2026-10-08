# deepstream_project/monitoring/jetson_monitoring.py
#
# Monitoring helpers shared by monitoring/monitor.py (real production runs) and
# debug_pipeline/stress_test/run_stress.py (synthetic stress tests).
# Every file is written line by line with fsync so it survives a hang/freeze of the Jetson.

import asyncio
import os
import subprocess
import time
from datetime import datetime

import psutil

MEMINFO_KEYS = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "CmaTotal", "CmaFree")


class SyncedWriter:
    """Append-only text file, flushed and fsynced on every write."""

    def __init__(self, path):
        self.f = open(path, "a", buffering=1)

    def write(self, text):
        self.f.write(text)
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self):
        self.f.close()


def read_meminfo():
    values = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, rest = line.split(":", 1)
            if key in MEMINFO_KEYS:
                values[key] = int(rest.split()[0]) // 1024   # kB -> MB
    return values


def run_cmd(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (out.stdout + out.stderr).strip()
    except Exception as e:
        return f"<failed: {e}>"


def write_run_info(run_dir, args, extra_cmds=()):
    with open(os.path.join(run_dir, "run_info.txt"), "w") as f:
        f.write(f"started: {datetime.now().isoformat()}\nargs: {vars(args)}\n\n")
        for cmd in (["nvpmodel", "-q"], ["jetson_clocks", "--show"], ["uptime"], ["free", "-m"],
                    ["cat", "/etc/nv_tegra_release"], *extra_cmds):
            f.write(f"$ {' '.join(cmd)}\n{run_cmd(cmd)}\n\n")


async def tegrastats_task(path, step_label, latest=None):
    """Runs tegrastats and writes each line with a timestamp and the current step label.

    If `latest` is a dict, latest["tegrastats"] always holds the newest line.
    """
    writer = SyncedWriter(path)
    try:
        proc = await asyncio.create_subprocess_exec("tegrastats", "--interval", "1000",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        writer.write("tegrastats not found\n")
        return
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors='replace').rstrip()
            writer.write(f"{time.time():.0f} {step_label['value']} {text}\n")
            if latest is not None:
                latest["tegrastats"] = text
    finally:
        if proc.returncode is None:
            proc.terminate()
        writer.close()


async def memory_task(path, step_label, children, interval_s=5, latest=None):
    """Samples system memory and per-pipeline RSS into a CSV.

    `children` maps a tag to any object with a `.pid`. If `latest` is a dict, latest["meminfo"] holds the newest sample.
    """
    new_file = not os.path.exists(path)
    writer = SyncedWriter(path)
    if new_file:
        writer.write("time,step," + ",".join(f"{k}_MB" for k in MEMINFO_KEYS) + ",pipelines_rss_MB,per_pipeline_rss_MB\n")
    try:
        while True:
            mem = read_meminfo()
            rss = []
            for tag, proc in list(children.items()):
                try:
                    rss.append(f"{tag}:{psutil.Process(proc.pid).memory_info().rss // 2**20}")
                except (psutil.NoSuchProcess, ProcessLookupError):
                    pass
            total = sum(int(r.split(":")[1]) for r in rss)
            writer.write(f"{time.time():.0f},{step_label['value']}," + ",".join(str(mem.get(k, "")) for k in MEMINFO_KEYS)
                         + f",{total},{' '.join(rss)}\n")
            if latest is not None:
                latest["meminfo"] = mem
            await asyncio.sleep(interval_s)
    finally:
        writer.close()
