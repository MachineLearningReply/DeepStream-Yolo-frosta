# deepstream_project/debug_pipeline/stress_test/run_stress.py
#
# Runs N debug pipelines in parallel (one per emulated camera) for each (cameras, fps) step and
# records system load. Children are spawned like the production orchestrator does
# (asyncio subprocess, stdout+stderr piped, PYTHONUNBUFFERED=1).
#
# Everything is written line by line with fsync so the logs survive a hang/freeze of the Jetson.
#
#   python3 run_stress.py --replay-dir /data/frames/{line} -c carrots --cameras 1,2,3,4 --fps 5,7 --step-minutes 15
#   python3 run_stress.py --replay-dir /data/frames/{line} -c carrots --cameras 4 --fps 7 --step-minutes 600   # soak

import argparse
import asyncio
import csv
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

import psutil

DEBUG_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
STATS_RE = re.compile(r"^REPLAY_STATS (.*)$")
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


def write_run_info(run_dir, args):
    with open(os.path.join(run_dir, "run_info.txt"), "w") as f:
        f.write(f"started: {datetime.now().isoformat()}\nargs: {vars(args)}\n\n")
        for cmd in (["nvpmodel", "-q"], ["jetson_clocks", "--show"], ["uptime"], ["free", "-m"],
                    ["cat", "/etc/nv_tegra_release"]):
            f.write(f"$ {' '.join(cmd)}\n{run_cmd(cmd)}\n\n")


async def tegrastats_task(path, step_label):
    """Runs tegrastats and writes each line with a timestamp and the current step label."""
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
            writer.write(f"{time.time():.0f} {step_label['value']} {line.decode(errors='replace').rstrip()}\n")
    finally:
        if proc.returncode is None:
            proc.terminate()
        writer.close()


async def memory_task(path, step_label, children, interval_s=5):
    """Samples system memory and per-pipeline RSS into a CSV."""
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
            await asyncio.sleep(interval_s)
    finally:
        writer.close()


async def pump_output(proc, log_path, last_stats):
    """Copies a child's stdout to its log file and keeps its latest REPLAY_STATS values."""
    with open(log_path, "a", buffering=1) as f:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            f.write(text)
            m = STATS_RE.match(text.strip())
            if m:
                last_stats.update(dict(kv.split("=", 1) for kv in m.group(1).split()))


async def run_step(args, run_dir, n_cameras, fps, step_label, children):
    step_name = f"{n_cameras}cam_{fps:g}fps"
    step_label["value"] = step_name
    step_dir = os.path.join(run_dir, step_name)
    os.makedirs(step_dir, exist_ok=True)
    step_s = int(args.step_minutes * 60)
    print(f"[{datetime.now():%H:%M:%S}] step {step_name}: {n_cameras} pipelines at {fps:g} fps for {step_s}s", flush=True)

    lines = args.lines.split(",")
    procs, pumps, stats = {}, [], {}
    t_start = time.time()
    for i in range(n_cameras):
        tag = f"st{i + 1}"
        line = lines[i % len(lines)]
        # Staggered start, but all pipelines stop together
        duration = step_s + (n_cameras - 1 - i) * args.stagger_s
        cmd = [sys.executable, DEBUG_MAIN, "-l", line, "-c", args.crop_type,
               "--replay-dir", args.replay_dir.format(line=line), "--fps", str(fps),
               "--duration", str(duration), "--max-images", str(args.max_images), "--tag", tag]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        procs[tag] = proc
        children[tag] = proc
        stats[tag] = {"line": line}
        pumps.append(asyncio.create_task(pump_output(proc, os.path.join(step_dir, f"{tag}.log"), stats[tag])))
        print(f"  started {tag} ({line}) pid={proc.pid}", flush=True)
        if i < n_cameras - 1:
            await asyncio.sleep(args.stagger_s)

    # Pipelines end by themselves (EOS after --duration); grace period for engine load + shutdown
    deadline = t_start + step_s + (n_cameras - 1) * args.stagger_s + 180
    for tag, proc in procs.items():
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(deadline - time.time(), 1))
        except asyncio.TimeoutError:
            print(f"  {tag} did not stop in time, sending SIGINT", flush=True)
            proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    await asyncio.gather(*pumps)
    for tag in procs:
        children.pop(tag, None)

    return step_name, t_start, time.time(), {tag: (stats[tag], procs[tag].returncode) for tag in procs}


def summarise_step(run_dir, step_name, t_start, t_end, results, fps):
    """Builds one summary row from the children's last stats plus tegrastats/memory samples of the step."""
    row = {"step": step_name, "target_fps": fps, "pipelines": len(results), "duration_s": int(t_end - t_start)}

    cam_fps, appsrc_dropped, overruns, late, failed = [], 0, 0, 0, []
    for tag, (st, rc) in results.items():
        if rc != 0:
            failed.append(f"{tag}(rc={rc})")
        if "avg_processed_cam_fps" in st:
            cam_fps.append(float(st["avg_processed_cam_fps"]))
            appsrc_dropped += int(st["appsrc_dropped"])
            overruns += int(st["queue_src_overrun"])
            late += int(st["late"])
        else:
            failed.append(f"{tag}(no stats)")
    row["min_avg_processed_cam_fps"] = min(cam_fps) if cam_fps else ""
    row["mean_avg_processed_cam_fps"] = round(sum(cam_fps) / len(cam_fps), 2) if cam_fps else ""
    row["appsrc_dropped_total"] = appsrc_dropped
    row["queue_src_overrun_total"] = overruns
    row["late_pushes_total"] = late
    row["failed_pipelines"] = " ".join(failed)

    min_avail, max_swap_used, max_rss = None, 0, 0
    with open(os.path.join(run_dir, "memory.csv")) as f:
        for r in csv.DictReader(f):
            if r["step"] != step_name or not r["MemAvailable_MB"]:
                continue
            avail = int(r["MemAvailable_MB"])
            min_avail = avail if min_avail is None else min(min_avail, avail)
            if r["SwapTotal_MB"]:
                max_swap_used = max(max_swap_used, int(r["SwapTotal_MB"]) - int(r["SwapFree_MB"]))
            max_rss = max(max_rss, int(r["pipelines_rss_MB"] or 0))
    row["min_mem_available_MB"] = min_avail if min_avail is not None else ""
    row["max_swap_used_MB"] = max_swap_used
    row["max_pipelines_rss_MB"] = max_rss

    max_gpu = max_emc = max_temp = max_power = 0
    tegra_path = os.path.join(run_dir, "tegrastats.log")
    if os.path.exists(tegra_path):
        with open(tegra_path) as f:
            for line in f:
                parts = line.split(" ", 2)
                if len(parts) < 3 or parts[1] != step_name:
                    continue
                text = parts[2]
                if m := re.search(r"GR3D_FREQ (\d+)%", text):
                    max_gpu = max(max_gpu, int(m.group(1)))
                if m := re.search(r"EMC_FREQ (\d+)%", text):
                    max_emc = max(max_emc, int(m.group(1)))
                temps = [float(t) for t in re.findall(r"\w+@(-?[\d.]+)C", text)]
                if temps:
                    max_temp = max(max_temp, max(temps))
                rails = [int(p) for p in re.findall(r"(?:VDD|VIN)_\w+ (\d+)mW", text)]
                if rails:
                    max_power = max(max_power, sum(rails))
    row["max_gpu_pct"] = max_gpu
    row["max_emc_pct"] = max_emc
    row["max_temp_C"] = max_temp
    row["max_power_rails_sum_mW"] = max_power
    return row


async def main(args):
    run_dir = os.path.join(args.out_dir, datetime.now().strftime("run_%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    write_run_info(run_dir, args)
    print(f"Run directory: {run_dir}", flush=True)

    step_label = {"value": "idle"}
    children = {}
    monitors = [
        asyncio.create_task(tegrastats_task(os.path.join(run_dir, "tegrastats.log"), step_label)),
        asyncio.create_task(memory_task(os.path.join(run_dir, "memory.csv"), step_label, children)),
    ]

    summary_path = os.path.join(run_dir, "summary.csv")
    summary_writer = None
    try:
        for n_cameras in [int(c) for c in args.cameras.split(",")]:
            for fps in [float(f) for f in args.fps.split(",")]:
                step_name, t_start, t_end, results = await run_step(args, run_dir, n_cameras, fps, step_label, children)
                step_label["value"] = "idle"
                row = summarise_step(run_dir, step_name, t_start, t_end, results, fps)
                print(f"  summary: {row}", flush=True)
                with open(summary_path, "a", newline="") as f:
                    if summary_writer is None:
                        summary_writer = list(row.keys())
                        csv.DictWriter(f, fieldnames=summary_writer).writeheader()
                    csv.DictWriter(f, fieldnames=summary_writer).writerow(row)
                    f.flush()
                    os.fsync(f.fileno())
                await asyncio.sleep(args.cooldown_s)
    finally:
        for proc in children.values():
            if proc.returncode is None:
                proc.send_signal(signal.SIGINT)
        for task in monitors:
            task.cancel()
        await asyncio.gather(*monitors, return_exceptions=True)
    print(f"Done. Summary: {summary_path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stress test: N camera-less pipelines × fps steps on the Jetson.")
    parser.add_argument("--replay-dir", required=True,
                        help="Frames directory; may contain {line}, e.g. /data/frames/{line}, to use per-line frames.")
    parser.add_argument("-c", "--crop-type", choices=["peas", "carrots", "beans"], required=True)
    parser.add_argument("--cameras", default="1,2,3,4", help="Comma-separated numbers of parallel pipelines.")
    parser.add_argument("--fps", default="7", help="Comma-separated camera frame rates.")
    parser.add_argument("--step-minutes", type=float, default=15, help="Duration of each step (all pipelines running).")
    parser.add_argument("--lines", default="fl1,fl2,fl3", help="Camera geometries assigned cyclically to pipelines.")
    parser.add_argument("--stagger-s", type=int, default=20, help="Delay between pipeline starts (TensorRT engine load).")
    parser.add_argument("--cooldown-s", type=int, default=30, help="Pause between steps.")
    parser.add_argument("--max-images", type=int, default=10, help="Frames preloaded per pipeline.")
    parser.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"))
    asyncio.run(main(parser.parse_args()))
