# debug_pipeline

Camera-less version of the production pipeline. Instead of a GigE camera (`aravissrc`), an `appsrc` pushes
raw bayer frames from disk at a fixed fps. Everything after that is identical to production:
`tcamconvert` (debayer) → split left/right → `nvstreammux` → `nvinfer` → Redis, with the production
probes from `../callbacks.py` (image saving, discoloration sampling, FPS).

- `main.py` — run one debug pipeline (like `../main.py`, plus replay options)
- `pipeline_builder.py` — **mirrors `../pipeline_builder.py`**; when the production pipeline changes, change this too
- `replay_source.py` — loads frames and pushes them at the target fps
- `stress_test/run_stress.py` — runs N pipelines in parallel and records system load

Outputs never mix with production:
- images/predictions → `deepstream_pipeline/{images,predictions}/debug/<tag>/`
- discoloration samples → `deepstream_pipeline/discoloration/{images,predictions}/debug/<tag>/`
- Redis topic → `deepstream_yolo_results_debug_<tag>`
- log → `deepstream_pipeline/logs/debug_pipeline_<tag>.log`

Clean up `*/debug/` directories after long runs; images are still saved (same disk load as production).

## Frames

Frames live in `debug_pipeline/frames/` (ignored by git, never committed), one folder per line:
`frames/fl1/`, `frames/fl2/`, `frames/fl3/`, plus e.g. `frames/black/`. The commands below use a shell variable
for that folder, so they work from any directory:

```bash
FRAMES=~/DeepStream-Yolo-frosta/deepstream_pipeline/debug_pipeline/frames
```

Folders must exist before writing into them: `mkdir -p` creates the folder **and** any missing parent folders
(plain `mkdir` fails with "No such file or directory" if a parent is missing).

Two formats are accepted:

1. **`.raw` bayer frames (preferred, exactly what the camera delivers).** Capture them on the Jetson with a camera connected:
   ```bash
   # fl1: 3872x1862, fl2: 3728x2000, fl3: 3400x1150 (see FILLING_LINE_CONFIGS in ../config.py)
   mkdir -p "$FRAMES/fl1"
   gst-launch-1.0 aravissrc camera-name=Baumer-VCXG.2-127C.I-700012638609 num-buffers=20 \
     ! video/x-bayer,format=rggb,width=3872,height=1862 ! multifilesink location="$FRAMES/fl1/fl1_%03d.raw"
   ```
   Each file must be exactly `width × height` bytes, otherwise it is skipped.
2. **Colour images** (`.bmp/.png/.jpg`). Resized to the line's sensor resolution and converted to RGGB bayer.
   Use full-resolution camera images if possible, so detections (and therefore image saving) are realistic.

Only `--max-images` frames (default 10, ~7 MB each) are loaded into RAM per pipeline; they are replayed in a loop.

## Single pipeline

Redis must be running.

```bash
FRAMES=~/DeepStream-Yolo-frosta/deepstream_pipeline/debug_pipeline/frames
cd ~/DeepStream-Yolo-frosta/deepstream_pipeline
python3 debug_pipeline/main.py -l fl1 -c carrots --replay-dir "$FRAMES/fl1" --fps 7 --duration 60 --tag test1
```

Every 30 s it prints a line like:
```
REPLAY_STATS tag=test1 ... pushed=420 late=0 appsrc_dropped=0 queue_src_overrun=0 processed_frames=838 ... avg_processed_cam_fps=6.98
```
- `avg_processed_cam_fps` ≈ `--fps` → the pipeline keeps up. (`processed_frames` counts halves: 2 per camera frame.)
- `appsrc_dropped` > 0 → debayering (`tcamconvert`, CPU) can't keep up.
- `queue_src_overrun` > 0 → GPU part (convert / mux / inference / probe) can't keep up.
- `late` > 0 → the feeder itself is blocked (only with GStreamer < 1.20).

**First check on the Jetson:** run this once. If `tcamconvert` refuses the appsrc bayer input, the pipeline fails during
caps negotiation (`not-negotiated` error) — report that before running the stress test.

## Stress test

```bash
FRAMES=~/DeepStream-Yolo-frosta/deepstream_pipeline/debug_pipeline/frames
cd ~/DeepStream-Yolo-frosta/deepstream_pipeline/debug_pipeline/stress_test

# Ramp: every combination of cameras × fps, 15 min each
python3 run_stress.py --replay-dir "$FRAMES/{line}" -c carrots --cameras 1,2,3,4,5 --fps 3,5,7,10 --step-minutes 15

# Soak: reproduce the crash (4 cameras at 7 fps for 10 h)
python3 run_stress.py --replay-dir "$FRAMES/{line}" -c carrots --cameras 4 --fps 7 --step-minutes 600
```

- `{line}` in `--replay-dir` is replaced by the line of each pipeline; geometries are assigned cyclically from `--lines` (default `fl1,fl2,fl3`).
- Pipelines start `--stagger-s` (20 s) apart so TensorRT engines load one by one; they all stop together.
- Run it inside `tmux` so it survives SSH disconnects.

Output in `stress_test/runs/run_<timestamp>/` (written with fsync, so it survives a freeze):

| File | Content |
|---|---|
| `run_info.txt` | `nvpmodel`, `jetson_clocks`, uptime, memory, L4T version at start |
| `tegrastats.log` | tegrastats every 1 s, prefixed with time and step |
| `memory.csv` | MemAvailable, swap, CMA, RSS of each pipeline every 5 s |
| `<step>/stN.log` | Output of each pipeline |
| `summary.csv` | Per step: achieved fps, drops, min free RAM, max swap, max GPU/EMC %, max temperature, max power |

After a freeze, the **last lines of `memory.csv` and `tegrastats.log`** show the state just before it.

## Pinpointing the load: tests A / B / C

Model inference costs the same with 0 or 300 detections. What changes is the work **after** inference
(Python loop in `callbacks.py`, Redis messages, copying frames off the GPU and saving `.bmp`). Fake detections
make black frames produce that work, so each part can be switched on separately:

```bash
FRAMES=~/DeepStream-Yolo-frosta/deepstream_pipeline/debug_pipeline/frames

# One black frame is enough (resized to each line's resolution). Prints True if it was written:
# cv2.imwrite does NOT create folders and fails silently, hence mkdir -p first.
mkdir -p "$FRAMES/black"
python3 -c "import numpy as np, cv2; print(cv2.imwrite('$FRAMES/black/black.png', np.zeros((2000, 3872, 3), np.uint8)))"

cd ~/DeepStream-Yolo-frosta/deepstream_pipeline/debug_pipeline/stress_test
# A: inference only (no detections, nothing after the model)
python3 run_stress.py --replay-dir "$FRAMES/black" -c carrots --cameras 4 --fps 7 --step-minutes 120
# B: + 20 detections per frame half → Python loop + Redis messages, no image saving
python3 run_stress.py --replay-dir "$FRAMES/black" -c carrots --cameras 4 --fps 7 --step-minutes 120 \
    --fake-detections 20 --save-every-frames 0
# C: + image saving (one image every 50 frame halves)
python3 run_stress.py --replay-dir "$FRAMES/black" -c carrots --cameras 4 --fps 7 --step-minutes 120 \
    --fake-detections 20 --save-every-frames 50
```

| Memory (`memory.csv` MemAvailable) keeps going down in… | Points to |
|---|---|
| A already | GPU / pipeline itself |
| B but not A | per-detection work: event metadata, `nvmsgconv`, Redis |
| C but not B | image saving (GPU frame copy, `.bmp` writes) |
| none | not reproduced: compare with real frames / GigE network |

Notes:
- The fake boxes use a real error-class label (default: the crop's first, e.g. `Bruch`), so `callbacks.py` treats them
  exactly like real detections. `REPLAY_STATS` shows `fake_detections=` to confirm they are added.
- `--save-every-frames` overrides the save thresholds of `config.py` **only in the debug run**.
- Disk: 4 cameras × 7 fps × 2 halves = 56 halves/s. `--save-every-frames 50` ≈ 1 image/s ≈ 8 MB/s ≈ 30 GB/h.
  Above 80 % disk usage `callbacks.py` stops saving, which changes test C — delete `images/debug/` between runs.
- First check: a 60 s single run with `--fake-detections 5`. If the log shows
  `Fake detections disabled: cannot set obj_label`, the pyds bindings don't allow setting labels and this needs another approach.

## Crash forensics (one-time setup on the Jetson)

The board becomes unreachable for hours and then comes back. To find out why:

1. **Keep kernel logs across reboots:**
   ```bash
   sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald
   ```
2. **After it comes back**, first check whether it rebooted or just froze:
   ```bash
   uptime; last -x | head
   ```
   Then look for the cause (`-b` = current boot if it only froze, `-b -1` = previous boot if it rebooted):
   ```bash
   journalctl -k -b -1 | grep -iE "oom|out of memory|nvgpu|nvmap|soft lockup|hung_task|watchdog|panic|thermal"
   ls /sys/fs/pstore
   ```
   - `oom-killer` / `Out of memory` → memory exhaustion (most likely, given the long freeze then recovery).
   - `nvgpu` / `nvmap` errors → GPU driver / GPU memory.
   - `soft lockup` / `hung_task` → kernel stuck.
3. **Best evidence:** during a soak, connect a laptop to the dev kit's micro-USB debug port and keep a serial
   console open (`screen /dev/ttyACM0 115200` on Linux/macOS: `/dev/tty.usbmodem*`). Kernel messages printed while
   the board is unreachable over the network show up there.

## Limits

- No GigE networking is exercised (4 cameras at 7 fps is ~1.6 Gbit/s of camera traffic). If the replay never crashes
  but production does, the network / camera driver side becomes the main suspect.
- The emulated cameras are perfectly regular; real cameras can deliver bursts.
