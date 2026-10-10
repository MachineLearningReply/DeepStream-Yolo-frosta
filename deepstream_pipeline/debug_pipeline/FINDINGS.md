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
| Oct 9 15:30, **patched `host1x-fence.ko`** (production code on host) | fl1 + fl3 real cameras, beans, 10 fps | — | **no freeze in ~11 h**; pipeline stopped at 02:47 by a network link drop (`eno1: Link is Down`), board kept running (up since Oct 9 14:56) |

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
- **PL1 network link drops** (`eno1: Link is Down`, `PCS block lock`): every drop stops the pipelines (camera stream error),
  e.g. Oct 8 17:03, Oct 10 02:47 and 03:45; **≥ 62** drops in the kernel buffer on Oct 10 (Jetson port: Aquantia AQR113C,
  negotiated 2.5 Gbps). Separate from the freeze. Candidates: cable/connector/switch port (check `ethtool -S eno1` CRC/FCS
  errors), speed negotiation, Energy Efficient Ethernet (`ethtool --show-eee eno1`), or the `nvethernet` driver.
- Production `main.py` exits with code **0** after a GStreamer error, so a supervisor can't tell error from normal stop.
- Containers run in **UTC** (logs 2 h behind CEST).
- The model runs in **FP32** (`network-mode=0`); FP16 is roughly 2× faster (the colleague's setup already uses FP16).
- `hot-surface-alert` toggles hundreds of times per hour under load (warning only, no throttling) and fills `kern.log`.
- VS Code Remote on the Jetsons: ~16 GB RAM and, on Jetson 1, 12 cores at 100 % (file search with `--follow`).

### Next steps
1. ~~Build and install the patched `host1x-fence.ko`~~ done 2026-10-09 (`debug_pipeline/host1x_fence/`, original backed up as
   `~/host1x-fence.ko.orig`, md5 `18823ee5…` = NVIDIA package; patched sha256 `5cce8259…1aab`). First run: no freeze in ~11 h.
   Next: a second long run to confirm, with automatic pipeline restarts so a network drop doesn't end the test.
2. Post on NVIDIA's forum (draft below) with the crash reports, especially if the patch does not help.
3. Enable the persistent journal on both Jetsons: `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`.
4. Fix the PL1 network link (cable / switch port; see side findings) — now the main cause of pipeline stops.
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

## 2. Redis grows forever + constant disk snapshots (Jetson 2: fixed on 2026-10-09)

Note: the colleague's Docker setup (frosta-edge) already solves both parts (Redis started with
`--save "" --appendonly no`, and `nvmsgbroker` gets a broker config with `streamsize=500000`).

### Findings (Jetson 2)
- Redis used **9.55 GB** RAM on Oct 1 and **9.92 GB** on Oct 9; `maxmemory` **0** (no limit), `maxmemory_policy noeviction`.
- Streams were never trimmed: `deepstream_yolo_results_fl1` **22,506,095** messages on Oct 9, fl2 2,736,595.
  `nvmsgbroker` is created in `pipeline_builder.py` without a Redis adapter config, so there is no stream size limit.
- `save "900 1 300 10 60 10000"` → under production load Redis snapshotted everything to the SSD about every 60 s
  (one snapshot took **47 s**), and reloaded all old data after every restart.
- The messages are **live data only**. The consumer (`redis_consumer.py`, BigQuery uploads, other repo) reads with
  `XREAD` starting at `$` (only messages arriving after it starts), continues after the last ID it read, uses no
  consumer group and never deletes anything. Old messages are never read again → **trimming them is safe**.
  It also reads `discoloration_results_<line>` (peas) the same way.
- Caveat: the consumer reads 1 message per call and pauses during BigQuery uploads, so it can lag behind. If a stream
  is trimmed below what the consumer has not read yet, those messages are skipped silently. Hence a generous limit:
  **500,000 per stream** (≈ 7 h at 20 messages/s, ≈ 250 MB).

### Why trimming, and `maxmemory` only as a safety net
`maxmemory` limits Redis' total RAM, but at the limit Redis either refuses new data (`noeviction`: the pipelines'
`nvmsgbroker` gets errors) or deletes **whole keys** (e.g. `allkeys-lru`: an entire stream, including unread messages).
It cannot keep "the newest N messages per stream". Stream trimming (`XTRIM … MAXLEN`) does exactly that, so the cron
job below is the real limit; `maxmemory 4gb` is only a backstop in case the cron job ever stops (Redis then refuses new
messages at 4 GB instead of slowly filling the Jetson's RAM).

### Done on Jetson 2 (2026-10-09)
1. Disk snapshots off: `redis-cli config set save "" && redis-cli config rewrite`
   (undo: `redis-cli config set save "900 1 300 10 60 10000" && redis-cli config rewrite`).
2. Trimmed once to ~500,000: fl1 −22,006,084 messages, fl2 −2,236,575 (the others were already below 500,000).
3. Kept bounded with a cron job every 5 min (keeps the newest ~500,000 messages per stream):
   `*/5 * * * * for k in $(redis-cli --scan --pattern "deepstream_yolo_results_*") $(redis-cli --scan --pattern "discoloration_results_*"); do redis-cli xtrim $k MAXLEN "~" 500000 >/dev/null; done`
4. Safety net: `redis-cli config set maxmemory 4gb && redis-cli config rewrite` (undo: `maxmemory 0`).

Steps 1, 3 and 4 live in the camera setup script (with the `arv-tool-0.8` camera settings), so they are reapplied on
every setup. The cron line is installed duplicate-safe:
```bash
TRIM_CRON='*/5 * * * * for k in $(redis-cli --scan --pattern "deepstream_yolo_results_*") $(redis-cli --scan --pattern "discoloration_results_*"); do redis-cli xtrim $k MAXLEN "~" 500000 >/dev/null; done'
( crontab -l 2>/dev/null | grep -v "xtrim" ; echo "$TRIM_CRON" ) | crontab -
```
The cron job belongs to the user that runs the script (`crontab -l` as that user to check).

Result: Redis RAM **9.92 GB → 396 MB**, `maxmemory` 4.00 G, exactly one `xtrim` cron entry.

### Still to do
5. Delete the stress-test streams (nobody reads them):
   `redis-cli del deepstream_yolo_results_debug_st1 deepstream_yolo_results_debug_st2 deepstream_yolo_results_debug_st3 deepstream_yolo_results_debug_st4`
6. Same steps on Jetson 1 (PL2).
7. Later, cleaner: give `nvmsgbroker` a Redis adapter config with `streamsize=500000` in `pipeline_builder.py`
   (as the colleague's setup does), so the pipeline keeps streams bounded itself (production code change, separate step).

### Verify
`redis-cli info memory | grep -E "used_memory_human|maxmemory_human"` → well under 1 GB used, 4 GB max, flat over days;
`redis-cli info persistence | grep rdb_bgsave_in_progress` stays 0; the BigQuery uploads continue as before.
