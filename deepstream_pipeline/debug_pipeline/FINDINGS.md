# Findings — Jetson crash investigation (started 2026-10-01)

Naming: **Jetson 1 = PL2**, **Jetson 2 = PL1** (this repo).

## 1. Crash investigation — status

### Symptom
Under higher load (≈10 fps, several pipelines) the Jetson becomes unreachable, logs stop, and hours later it is
back with uptime reset and tmux sessions gone → a **real freeze followed by a reboot**. Stable at ~5 fps.

### Evidence
| Evidence | Source |
|---|---|
| Kernel log of an earlier crash (~44 h uptime): **CPU 0 stuck** (`rcu_preempt detected stalls`), then SSD timeouts (`nvme ... timeout, completion polled`), then power-management chip unreachable (`BPMP transfer failed`, all `thermal_zone` reads `-62`). Logging dies ~1 h later. | Jetson 2 `/sys/fs/pstore/console-ramoops-0` (dated Aug 28) |
| **No** out-of-memory, **no** kernel panic in that log. | same |
| 3 min before the stall: `nvgpu ... Channel unbind failed, tearing down TSG` (GPU clean-up when a pipeline stops). 90 pipeline starts and 11 such errors in 44 h. | same |
| The same GPU error appears **68× in a month on Jetson 1 without any crash** → not a cause on its own. | Jetson 1 `/var/log/kern.log*` |
| Jetson 1: no crash Aug 30 – Oct 1 (Sep 15 restart was a manual shutdown). | Jetson 1 `kern.log*` |
| Jetson 2 network link keeps dropping: 147× `nvethernet ... Failed to get PCS block lock`, plus an Aquantia PHY timeout. Cable switch↔Jetson suspected broken. | Jetson 2 ramoops |
| Stress test 4 cameras × 7 fps, black frames, 2 h: **no crash**, ~5.04 fps processed per camera (rest dropped), memory flat (≥ 27.6 GB available), GPU 99 %, all 12 CPU cores 100 %, ~53 W (MAXN), max 70 °C. | `stress_test/runs/…/summary.csv` |
| Real camera test (`arv-camera-test-0.8`, fl1 camera): 0 complete frames, >50 % packets missing, ~200 k resend requests, "Control lost". Unclear whether caused by CPU at 100 % (stress test running) or by the cable. | Jetson 2 |
| fl1 camera limits: 3872×1862 BayerRG8 = 7.2 MB/frame, `DeviceLinkThroughputLimit` 102.5 MB/s → **≈ 14 fps max**. | `arv-tool-0.8` |
| VS Code Remote server on the Jetson used ~16 GB + 3.4 GB (Pylance). | `ps aux` |

### Ruled out / unlikely
- Memory leak in the pipelines (RSS flat ~1.1 GB each), overheating, a crash inside our Python code.

### Current theory
A low-level freeze of the Jetson (kernel / firmware / power management), made likely by running at its absolute
limit for long periods (MAXN, CPU + GPU at 100 %). On Jetson 2, a bad network link (packet-loss / resend storm)
adds load. Pipeline stops (GPU clean-up errors) may be an occasional trigger.

### Next steps
1. Rerun `arv-camera-test-0.8 -n <cam>` with the stress test **stopped** → CPU load vs cable.
2. Replace the PL1 switch↔Jetson cable.
3. Enable the persistent journal on both Jetsons: `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`.
4. Long soak at the "crash" load: 1 real camera + replay pipelines at ~10 fps (needs a camera mode in the debug pipeline).
5. If it crashes: repeat with a lower power mode (`nvpmodel`) or lower fps.
6. Don't keep VS Code Remote connected to production Jetsons for long periods (memory).

Practical note: at 4 × 7 fps only ~5 fps per camera are processed anyway, so setting cameras to ~5 fps loses nothing
and takes the board off its limit.

## 2. Open task (not done yet): Redis grows forever + constant disk snapshots

Parked until the crash is understood.

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
