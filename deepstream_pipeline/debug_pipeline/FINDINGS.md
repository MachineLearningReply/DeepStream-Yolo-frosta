# Findings — Jetson crash investigation (started 2026-10-01)

Naming: **Jetson 1 = PL2**, **Jetson 2 = PL1** (this repo).
Jetson 2: AGX Orin 64 GB Dev Kit, **L4T 36.4.7 (JetPack 6.2.1)**, kernel 5.15.148-tegra, DeepStream 7.1, TensorRT 10.3, MAXN.
Graphs of the test runs: https://claude.ai/artifact/XjXZTcMtqiPK8nAJmVZdLe (private; share it from the page).

## 1. Crash investigation — status

### Symptom
The Jetson freezes completely (unreachable, all logs stop) and restarts by itself minutes to hours later
(uptime reset, tmux sessions gone). No warning signs beforehand.

### Every freeze shows the same kernel signature
1. CPU 0 stops responding: `rcu: INFO: rcu_preempt detected stalls on CPUs/tasks: 0-...0` (only ever CPU 0).
2. When the kernel log includes the task dump (Oct 8 host and Docker runs): `Task dump for CPU 0: task:cuda-EvtHandlr state:R`
   (NVIDIA CUDA driver thread, running). The earlier crash reports only contain the stall lines.
3. Often ~20 s before: `tegra-hda 3510000.hda: azx_get_response timeout`.
4. Then SSD timeouts (`nvme nvme0: I/O … timeout, completion polled`), `BPMP transfer failed`, thermal sensors `-62`,
   logging service dies. Running pipelines report a stream error and stop. The board restarts later (warm restart,
   so `/sys/fs/pstore/console-ramoops-0` keeps the kernel messages).

### Runs on Jetson 2
| Run | Load | Ran until freeze | Result |
|---|---|---|---|
| Aug 28, production | pipelines started/stopped 90× in 44 h | ~44 h | froze (CPU 0 stall) |
| Oct 1, stress test 1 | 4 × replay, 7 fps, black frames, no cameras | 2 h 26 min | froze |
| Oct 2, stress test 2 | 4 × replay, 10 fps, black frames, no cameras | 2 h 17 min | froze (CPU 0 stall; no task dump in that report) |
| Oct 6, TensorRT only (`trtexec` × 4, same model) | GPU 99 %, ~53 W, Tj 77 °C — as hard or harder | — | **no freeze in 7 h** |
| Oct 8 11:35, production code on host | fl1 + fl3 real cameras, beans, 10 fps | 1 h 04 min | froze, `cuda-EvtHandlr` on CPU 0 (thread of fl3) |
| Oct 8 17:24, **same code in Docker** (DeepStream 7.1 container) | same | **1 h 13 min** | **froze, identical signature** |

Details per run: GPU 96–99 %, total power ~49–53 W, Tj 69–80 °C, RAM ≥ 20 GB available, swap 0, camera traffic
steady (~111 MB/s with 2 cameras), no kernel warnings in the minutes before. Processed fps: ~5 per camera with 4
pipelines (GPU limit, FP32), ~9.2–9.7 with 2 pipelines.

### Ruled out
- **Memory** (no OOM, ≥ 20 GB free at every freeze, pipeline RSS flat).
- **Overheating / power / load alone**: `trtexec` ran 7 h at equal power and higher temperature without freezing.
- **Our Python code, image saving, Redis messages, pipeline stops, cameras, network**: the stress tests froze without any of them.
- **Docker**: the containerised run froze the same way (containers share the host kernel and GPU drivers).

### Cause (known NVIDIA bug)
A deadlock in NVIDIA's `host1x-fence` kernel driver: the interrupt handler `host1x_intr_handle_interrupt()` and
`host1x_pollfd_poll()` (called by programs waiting for GPU/VIC work, e.g. DeepStream's `cuda-EvtHandlr` thread) take
the same spinlock; when the interrupt arrives on CPU 0 while that thread holds the lock, CPU 0 spins forever.
DeepStream waits on these fences constantly; plain TensorRT barely does, which explains why `trtexec` never froze.
More frames per second → more chances → freezes sooner on average, but it is random.

- NVIDIA forum, Orin NX, same signature, root cause + patch (April 2026), confirmed by users:
  https://forums.developer.nvidia.com/t/rcu-preempt-caused-by-cuda-evthandlr/326076?page=6
- AGX Orin, JetPack 6.2.1 / L4T 36.4.4 + 36.4.7, DeepStream 7.1, similar host1x hang; for that user the patch alone
  did not help (NVIDIA: "should relate to other issues"):
  https://forums.developer.nvidia.com/t/jetson-agx-orin-jetpack-6-2-1-silent-gpu-hang-host1x-interrupt-servicing-stalls-under-sustained-compute-reproduces-on-two-distinct-orin-systems/368922
- Patch: in `drivers/gpu/host1x-fence/dev.c`, `spin_lock()`/`spin_unlock()` → `spin_lock_irqsave()`/`spin_unlock_irqrestore()`;
  rebuild `host1x-fence.ko` for 5.15.148-tegra (build it from NVIDIA's L4T 36.4.7 sources, don't use a module from the forum).

### Side findings
- **PL1 network link drops** (`eno1: Link is Down`, `PCS block lock`): stops the pipelines (camera stream error), e.g. Oct 8 17:03.
  Separate from the freeze; cable/switch port suspected.
- Production `main.py` exits with code **0** after a GStreamer error, so a supervisor can't tell error from normal stop.
- Containers run in **UTC** (logs 2 h behind CEST).
- The model runs in **FP32** (`network-mode=0`); FP16 is roughly 2× faster (the colleague's setup already uses FP16).
- `hot-surface-alert` toggles hundreds of times per hour under load (warning only, no throttling) and fills `kern.log`.
- VS Code Remote on the Jetsons: ~16 GB RAM and, on Jetson 1, 12 cores at 100 % (file search with `--follow`).

### Next steps
1. Build and install the patched `host1x-fence.ko` (keep the original), rerun the Oct 8 setup (2 real cameras, 10 fps, monitor),
   ≥ 8 h, twice.
2. Post on NVIDIA's forum (draft below) with the crash reports, especially if the patch does not help.
3. Enable the persistent journal on both Jetsons: `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`.
4. Fix the PL1 network link (cable / switch port).
5. Later: always-on monitor (systemd service) so every future freeze is recorded.

Collect evidence after any freeze with `bash deepstream_pipeline/monitoring/collect_crash.sh`.

### NVIDIA forum draft
> **Title:** AGX Orin (JetPack 6.2.1 / L4T 36.4.7): hard freeze with DeepStream 7.1 — rcu_preempt stall on CPU0, task cuda-EvtHandlr, then NVMe/BPMP timeouts
>
> **Setup:** Jetson AGX Orin 64 GB Developer Kit, JetPack 6.2.1 (L4T 36.4.7, kernel 5.15.148-tegra), DeepStream 7.1,
> TensorRT 10.3, CUDA 12.6, MAXN. 2–4 DeepStream pipelines (one process each): aravissrc (GigE) or appsrc → tcamconvert →
> nvvideoconvert (crop, compute-hw=GPU) → nvstreammux → nvinfer (YOLOv7 1600×1600 FP32) → nvmsgconv → nvmsgbroker (Redis).
>
> **Problem:** complete freeze after 1–2.5 h under sustained load (reproduced 5 times, incl. inside the DeepStream 7.1
> container). Temperatures normal, > 20 GB RAM free, no warnings beforehand. The board warm-restarts minutes to hours later.
> 4 × `trtexec` with the same engine at the same GPU load/power ran 7 h without freezing.
>
> **Kernel log at the freeze (every time):**
> ```
> tegra-hda 3510000.hda: azx_get_response timeout, switching to polling mode
> rcu: INFO: rcu_preempt detected stalls on CPUs/tasks:
> rcu:     0-...0: (1 GPs behind) ...
> Task dump for CPU 0:
> task:cuda-EvtHandlr  state:R  running task ...
> nvme nvme0: I/O ... timeout, completion polled   (repeated)
> tegra-mc 2c00000.memory-controller: BPMP transfer failed: -110
> thermal thermal_zoneX: failed to read out thermal zone (-62)
> ```
> **Question:** this matches the host1x-fence spinlock race in thread 326076. Is the `spin_lock_irqsave` fix the
> complete fix for L4T 36.4.7, or is there an official updated `host1x-fence.ko` / L4T release with it?
> Crash reports (console-ramoops) and monitoring data attached.

## 2. Open task (not done yet): Redis grows forever + constant disk snapshots

Parked until the crash is understood. Note: the colleague's Docker setup (frosta-edge) already solves both parts
(Redis started with `--save "" --appendonly no`, and `nvmsgbroker` gets a broker config with `streamsize=500000`).

### Findings (Jetson 2, 2026-10-01)
- Redis uses **9.55 GB** RAM, `maxmemory` **0** (no limit).
- Streams are never trimmed: `deepstream_yolo_results_fl1` **21,049,253** messages, fl2 2,736,595, fl3 47,007.
  `nvmsgbroker` is created in `pipeline_builder.py` without a Redis adapter config, so there is no stream size limit.
- `save "900 1 300 10 60 10000"` → under production load Redis snapshots everything to the SSD about every 60 s;
  the last snapshot took **47 s** (`rdb_last_bgsave_time_sec`). Near-constant multi-GB SSD writes + fork memory overhead.
- The messages are **live data only** (no need to survive a restart) and are read by **another service**.

### Fix (run on each Jetson; check Jetson 1 too)
1. Read-only: how does the other service read?
   ```bash
   redis-cli xinfo groups deepstream_yolo_results_fl1
   redis-cli client list | grep -v "cmd=client"
   ```
   If it only reads new messages, keeping the last ~100 k is safe. If it reads the whole history, ask its owner first.
2. Stop disk snapshots:
   ```bash
   redis-cli config set save "" && redis-cli config rewrite
   # undo: redis-cli config set save "900 1 300 10 60 10000" && redis-cli config rewrite
   ```
3. Trim once, then keep bounded with cron (every 5 min):
   ```bash
   for k in $(redis-cli --scan --pattern "deepstream_yolo_results_*"); do redis-cli xtrim $k MAXLEN ~ 100000; done
   # crontab -e:
   # */5 * * * * for k in $(redis-cli --scan --pattern "deepstream_yolo_results_*"); do redis-cli xtrim $k MAXLEN ~ 100000 >/dev/null; done
   ```
   Delete debug topics after testing: `redis-cli del deepstream_yolo_results_debug_st1` (etc.).
4. Later, cleaner: give `nvmsgbroker` a Redis adapter config with a stream-size limit in `pipeline_builder.py`
   (production code change, separate step).

### Verify
`redis-cli info memory | grep used_memory_human` well under 1 GB and flat over days;
`redis-cli info persistence | grep rdb_bgsave_in_progress` stays 0; the other service still shows live results.
