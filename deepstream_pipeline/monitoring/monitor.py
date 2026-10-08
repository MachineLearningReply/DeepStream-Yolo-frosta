# deepstream_project/monitoring/monitor.py
#
# Records the Jetson's state while the real production pipelines run (deepstream_pipeline/main.py).
# It only observes: production code and processes are not touched. Every line is fsynced, so the
# recordings survive a freeze of the board.
#
#   python3 deepstream_pipeline/monitoring/monitor.py            # until Ctrl+C
#   python3 deepstream_pipeline/monitoring/monitor.py --duration-h 12

import argparse
import asyncio
import os
import re
import time
from datetime import datetime
from types import SimpleNamespace

import psutil

from jetson_monitoring import SyncedWriter, run_cmd, write_run_info, tegrastats_task, memory_task

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE_DIR = os.path.dirname(HERE)
PIPELINE_MAIN = "deepstream_pipeline/main.py"

FPS_RE = re.compile(r"FPS \(last \d+ frames\): ([\d.]+)")
KERNEL_EVENTS = {
    "cpu_stall": re.compile(r"rcu_preempt detected stalls"),
    "nvme_timeout": re.compile(r"nvme nvme\d+: I/O .* timeout"),
    "bpmp_fail": re.compile(r"BPMP transfer failed|bpmp.*failed to transfer"),
    "gpu_unbind_failed": re.compile(r"unbind failed"),
    "net_link_lost": re.compile(r"PCS block lock"),
    "hot_surface_alert": re.compile(r"hot-surface-alert cooling state: 0 -> 1"),
    "out_of_memory": re.compile(r"Out of memory|oom-kill"),
}


def find_pipelines():
    """Returns {line: pid} for every running production pipeline (deepstream_pipeline/main.py -l <line>)."""
    found = {}
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = p.info["cmdline"] or []
        scripts = [c for c in cmd if c.endswith("main.py")]
        if not scripts:
            continue
        try:   # resolve 'python3 main.py' started from inside deepstream_pipeline/
            paths = [os.path.normpath(c if os.path.isabs(c) else os.path.join(p.cwd(), c)) for c in scripts]
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            paths = scripts
        if not any(path.endswith(PIPELINE_MAIN) for path in paths):
            continue
        line = next((cmd[i + 1] for i, c in enumerate(cmd[:-1]) if c in ("-l", "--line")), f"pid{p.info['pid']}")
        found[line] = p.info["pid"]
    return found


def latest_fps(logs_dir, line):
    """Latest FPS value from the production log of a line, plus how many seconds ago the log was written."""
    path = os.path.join(logs_dir, f"deepstream_pipeline_{line}.log")
    try:
        with open(path, "rb") as f:
            f.seek(max(os.path.getsize(path) - 16384, 0))
            matches = FPS_RE.findall(f.read().decode(errors="replace"))
        return (float(matches[-1]) if matches else None), time.time() - os.path.getmtime(path)
    except OSError:
        return None, None


def list_cameras():
    """Camera names reported by arv-tool-0.8, e.g. 'Baumer-VCXG.2-127C.I-700012638609 (192.168.1.3)'."""
    out = run_cmd(["arv-tool-0.8"])
    return [m.group(1) for m in re.finditer(r"^(\S+) \([\d.]+\)", out, re.MULTILINE)]


async def discovery_task(children, interval_s=5):
    """Keeps `children` ({line: obj with .pid}) in sync with the running pipelines; reports disappearances."""
    while True:
        found = find_pipelines()
        for line in list(children):
            if line not in found:
                print(f"[{datetime.now():%H:%M:%S}] !! pipeline {line} (pid {children[line].pid}) is no longer running", flush=True)
                del children[line]
        for line, pid in found.items():
            if line not in children or children[line].pid != pid:
                try:
                    proc = psutil.Process(pid)
                    proc.cpu_percent(None)   # start CPU measurement
                except psutil.NoSuchProcess:
                    continue
                print(f"[{datetime.now():%H:%M:%S}] pipeline {line} found (pid {pid})", flush=True)
                children[line] = SimpleNamespace(pid=pid, proc=proc)
        await asyncio.sleep(interval_s)


async def pipelines_task(path, children, logs_dir, latest, interval_s=30):
    writer = SyncedWriter(path)
    writer.write("time,line,pid,alive,cpu_pct,rss_MB,fps_batches,cam_fps,log_age_s\n")
    try:
        while True:
            await asyncio.sleep(interval_s)
            rows = {}
            for line, c in list(children.items()):
                try:
                    cpu, rss, alive = c.proc.cpu_percent(None), c.proc.memory_info().rss // 2**20, 1
                except psutil.NoSuchProcess:
                    cpu, rss, alive = "", "", 0
                fps, age = latest_fps(logs_dir, line)
                cam = round(fps / 2, 2) if fps is not None else ""
                writer.write(f"{time.time():.0f},{line},{c.pid},{alive},{cpu},{rss},{'' if fps is None else fps},"
                             f"{cam},{'' if age is None else round(age)}\n")
                rows[line] = {"cam_fps": cam, "cpu": cpu, "alive": alive, "age": age}
            latest["pipelines"] = rows
    finally:
        writer.close()


def read_net():
    stats = {}
    for iface in os.listdir("/sys/class/net"):
        if iface == "lo":
            continue
        base = f"/sys/class/net/{iface}/statistics/"
        try:
            stats[iface] = {k: int(open(base + k).read()) for k in ("rx_bytes", "rx_packets", "rx_dropped", "rx_errors")}
        except OSError:
            pass
    return stats


async def network_task(path, latest, interval_s=5):
    writer = SyncedWriter(path)
    writer.write("time,iface,rx_MBps,rx_pps,rx_dropped_new,rx_errors_new\n")
    prev, t_prev = read_net(), time.time()
    try:
        while True:
            await asyncio.sleep(interval_s)
            cur, now = read_net(), time.time()
            dt = max(now - t_prev, 1e-6)
            summary = {}
            for iface, s in cur.items():
                p = prev.get(iface)
                if not p:
                    continue
                mbps = (s["rx_bytes"] - p["rx_bytes"]) / dt / 1e6
                pps = (s["rx_packets"] - p["rx_packets"]) / dt
                drop, err = s["rx_dropped"] - p["rx_dropped"], s["rx_errors"] - p["rx_errors"]
                writer.write(f"{now:.0f},{iface},{mbps:.2f},{pps:.0f},{drop},{err}\n")
                if mbps > 0.5 or drop or err:
                    summary[iface] = (mbps, drop, err)
            latest["network"] = summary
            prev, t_prev = cur, now
    finally:
        writer.close()


async def kernel_task(path, kern_log, latest, interval_s=60):
    """Counts new kernel messages of the types seen in the crashes (reads only lines added since start)."""
    writer = SyncedWriter(path)
    writer.write("time," + ",".join(KERNEL_EVENTS) + "\n")
    totals = dict.fromkeys(KERNEL_EVENTS, 0)
    latest["kernel_totals"] = totals
    try:
        try:
            f = open(kern_log, "r", errors="replace")
        except PermissionError:
            msg = f"cannot read {kern_log}: run the monitor with sudo, or add the user to group 'adm' (sudo usermod -aG adm $USER, then log in again)"
            writer.write(f"# {msg}\n")
            print(f"[{datetime.now():%H:%M:%S}] note: {msg}", flush=True)
            latest["kernel_totals"] = None
            return
        f.seek(0, os.SEEK_END)
        while True:
            await asyncio.sleep(interval_s)
            if os.path.getsize(kern_log) < f.tell():   # log was rotated
                f.close()
                f = open(kern_log, "r", errors="replace")
            counts = dict.fromkeys(KERNEL_EVENTS, 0)
            for line in f:
                for name, rx in KERNEL_EVENTS.items():
                    if rx.search(line):
                        counts[name] += 1
            for name, n in counts.items():
                totals[name] += n
            writer.write(f"{time.time():.0f}," + ",".join(str(counts[k]) for k in KERNEL_EVENTS) + "\n")
            for name in ("cpu_stall", "nvme_timeout", "bpmp_fail", "out_of_memory"):
                if counts[name]:
                    print(f"[{datetime.now():%H:%M:%S}] !! kernel: {counts[name]} new '{name}' messages", flush=True)
    finally:
        writer.close()


def parse_tegrastats(line):
    if not line:
        return {}
    out = {}
    if m := re.search(r"GR3D_FREQ (\d+)%", line):
        out["gpu"] = int(m.group(1))
    if m := re.search(r"tj@([\d.]+)C", line):
        out["tj"] = float(m.group(1))
    cores = [int(c) for c in re.findall(r"(\d+)%@\d+", line.split("GR3D_FREQ")[0])]
    if cores:
        out["cpu"] = sum(cores) / len(cores)
    rails = [int(p) for p in re.findall(r"(?:VDD|VIN)_\w+ (\d+)mW", line)]
    if rails:
        out["power"] = sum(rails) / 1000
    return out


async def status_task(latest, t0, interval_s=30):
    while True:
        await asyncio.sleep(interval_s)
        t = parse_tegrastats(latest.get("tegrastats"))
        mem = latest.get("meminfo") or {}
        swap = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
        elapsed = time.time() - t0
        lines = [f"[{datetime.now():%H:%M:%S}] running {int(elapsed // 3600)} h {int(elapsed % 3600 // 60):02d} min"]
        pipes = latest.get("pipelines") or {}
        lines.append("  pipelines: " + ("  ".join(
            f"{k} {v['cam_fps'] if v['cam_fps'] != '' else '?'} fps cpu {v['cpu']}%" for k, v in sorted(pipes.items()))
            or "none found"))
        lines.append(f"  RAM avail {mem.get('MemAvailable', '?')} MB  swap {swap} MB  GPU {t.get('gpu', '?')}%  "
                     f"CPU {t.get('cpu', 0):.0f}%  Tj {t.get('tj', '?')}C  power {t.get('power', 0):.1f} W")
        net = latest.get("network") or {}
        if net:
            lines.append("  network: " + "  ".join(f"{i} {m:.1f} MB/s" + (f" dropped+{d}" if d else "") + (f" errors+{e}" if e else "")
                                                   for i, (m, d, e) in net.items()))
        kt = latest.get("kernel_totals")
        if kt:
            seen = {k: v for k, v in kt.items() if v}
            lines.append("  kernel events since start: " + (", ".join(f"{k} {v}" for k, v in seen.items()) or "none"))
        print("\n".join(lines), flush=True)


async def main(args):
    run_dir = os.path.join(args.out_dir, datetime.now().strftime("monitor_%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    cameras = list_cameras()
    write_run_info(run_dir, args, extra_cmds=[
        ["arv-tool-0.8", "-n", cam, "control", "AcquisitionFrameRate", "Width", "Height", "PixelFormat",
         "DeviceLinkThroughputLimit"] for cam in cameras])
    print(f"Monitoring into {run_dir}", flush=True)
    print(f"Cameras: {', '.join(cameras) or 'none found'}. Start the pipelines whenever you like; Ctrl+C stops the monitor.", flush=True)

    step_label = {"value": "monitor"}
    children, latest, t0 = {}, {}, time.time()
    tasks = [
        tegrastats_task(os.path.join(run_dir, "tegrastats.log"), step_label, latest=latest),
        memory_task(os.path.join(run_dir, "memory.csv"), step_label, children, latest=latest),
        discovery_task(children),
        pipelines_task(os.path.join(run_dir, "pipelines.csv"), children, args.logs_dir, latest),
        network_task(os.path.join(run_dir, "network.csv"), latest),
        kernel_task(os.path.join(run_dir, "kernel_events.csv"), args.kern_log, latest),
        status_task(latest, t0, args.status_interval_s),
    ]
    running = asyncio.gather(*tasks)
    try:
        if args.duration_h > 0:
            await asyncio.wait_for(running, timeout=args.duration_h * 3600)
        else:
            await running
    except asyncio.TimeoutError:
        print(f"Duration of {args.duration_h} h reached.", flush=True)
    finally:
        running.cancel()
        print(f"Monitor stopped. Recordings: {run_dir}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor the Jetson while production pipelines run (observes only).")
    parser.add_argument("--duration-h", type=float, default=0, help="Stop after this many hours (0 = until Ctrl+C).")
    parser.add_argument("--status-interval-s", type=int, default=30, help="Seconds between console status blocks.")
    parser.add_argument("--logs-dir", default=os.path.join(PIPELINE_DIR, "logs"), help="Production log directory (for FPS).")
    parser.add_argument("--kern-log", default="/var/log/kern.log", help="Kernel log to watch for crash-related messages.")
    parser.add_argument("--out-dir", default=os.path.join(HERE, "runs"))
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
