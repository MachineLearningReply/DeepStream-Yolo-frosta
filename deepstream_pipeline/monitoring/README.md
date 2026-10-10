# monitoring

Records the Jetson's state while the **real production pipelines** run. The monitor only observes:
production code and processes are not touched, and it can be started or stopped at any time.

- `monitor.py`: the monitor for production runs.
- `jetson_monitoring.py`: shared recording code, also used by `debug_pipeline/stress_test/run_stress.py`.
- `runs/`: recordings (ignored by git).

Every line is written to disk immediately, so the recordings survive a freeze of the board.

## Monitored production run

Run each step in its own `tmux` window on the Jetson.

1. **Start the monitor first**
   ```bash
   cd ~/DeepStream-Yolo-frosta/deepstream_pipeline/monitoring
   python3 monitor.py                 # until Ctrl+C, or: python3 monitor.py --duration-h 12
   ```
2. **Set the camera frame rate** (as usual), e.g. 10 fps:
   ```bash
   arv-tool-0.8 -n <camera-name> control AcquisitionFrameRate=10
   ```
   `arv-tool-0.8` without arguments lists the camera names.
3. **Start the pipelines** as in production, e.g.:
   ```bash
   cd ~/DeepStream-Yolo-frosta/deepstream_pipeline
   python3 main.py -l fl1 -c beans
   python3 main.py -l fl2 -c beans
   ```
   The monitor finds them by itself (also when they start later or are restarted).

Production pipelines save real images into `images/production/...`; the uploader sends them to GCP as usual.

Every 30 s the monitor prints a status block:
```
[14:05:30] running 2 h 10 min
  pipelines: fl1 9.8 fps cpu 85.0%  fl2 9.7 fps cpu 82.5%
  RAM avail 41230 MB  swap 0 MB  GPU 97%  CPU 70%  Tj 71.2C  power 48.3 W
  network: eno1 111.4 MB/s
  link eno1 up 2500 Mb/s (changes since boot 2) | cameras reachable 2/2
  kernel events since start: hot_surface_alert 312
```
Important events are printed **and** saved immediately to `events.log`: pipeline found/disappeared, the camera network
link going down/up (with how long it was down), a camera becoming unreachable, and kernel CPU stalls, SSD timeouts,
BPMP failures, out-of-memory, link drops and failed link alignments (`PCS block lock`).

Options: `--iface eno1` (network port to watch, default `eno1`), `--no-ping` (don't ping the cameras).

## Recorded files (`runs/monitor_<date>_<time>/`)

| File | Content |
|---|---|
| `run_info.txt` | Power mode, clocks, versions, and each camera's frame rate, resolution and bandwidth limit at the start |
| `tegrastats.log` | `tegrastats` every second: CPU per core, GPU load, temperatures, power |
| `memory.csv` | Every 5 s: available RAM, swap, CMA, memory of each pipeline |
| `pipelines.csv` | Every 30 s per pipeline: alive, CPU %, memory, FPS from its log. `fps_batches` is the value logged by the pipeline (both frame halves counted); `cam_fps` = half of it = camera frames per second |
| `network.csv` | Every 5 s per network port: received MB/s and packets/s, new dropped and errored packets |
| `kernel_events.csv` | Every minute: number of new kernel messages per type: `cpu_stall`, `nvme_timeout`, `bpmp_fail`, `gpu_unbind_failed`, `link_down`, `pcs_lock_failed`, `hot_surface_alert`, `out_of_memory` |
| `events.log` | One timestamped line per event (see above): the timeline of an incident |
| `link.csv` | Every second: camera network link (`eno1`) connected, up/down, speed, total link changes |
| `ping.csv` | Every second: each camera reachable (1/0) and its response time in ms |
| `nic_stats.csv` | Every 30 s: network card counters (`ethtool -S eno1`) that changed, with difference and total |
| `hwmon.csv` | Every 5 s: every temperature sensor the kernel exposes (incl. the network chip's, if it has one) |

### Reading a network drop
- **Camera unreachable while the Jetson's link is still `up`** → the problem is behind the Jetson: switch or camera
  (e.g. the switch's PoE power to the cameras).
- **The Jetson's link goes `down` first** (cameras unreachable only afterwards) → the Jetson's port, its cable or the switch port.
- **Network chip temperature (`hwmon.csv`) rising before each drop** → overheating.
- **`nic_stats.csv` error counters rising before the drop** → damaged packets, i.e. cable/connector/port.

`kernel_events.csv` needs read access to `/var/log/kern.log`. If the monitor says it cannot read it, either start
it with `sudo` or add your user to the `adm` group once (`sudo usermod -aG adm $USER`, then log in again).

## After a freeze

One command collects everything (restart time, kernel crash report, kernel log, monitor recordings, pipeline logs,
Docker container output) into `~/crash_<hostname>_<date_time>/` and packs it as a `.tar.gz`:
```bash
bash ~/DeepStream-Yolo-frosta/deepstream_pipeline/monitoring/collect_crash.sh
```
It asks for the sudo password once and prints the `scp` command to copy the `.tar.gz` to the Mac.
Run it **before** `docker compose down`, otherwise the container output is gone.
