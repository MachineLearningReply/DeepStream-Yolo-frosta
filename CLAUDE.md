# CLAUDE.md

Frosta defect-detection pipeline on top of a fork of [marcoslucianops/DeepStream-Yolo](https://github.com/marcoslucianops/DeepStream-Yolo).
The Frosta-specific code lives in `deepstream_pipeline/`; almost everything else is upstream.

## Target platform

- NVIDIA Jetson AGX Orin Developer Kit, JetPack 6.2.1 (L4T 36.4.7, kernel 5.15.148-tegra; checked on Jetson 2 via `/etc/nv_tegra_release`)
- DeepStream 7.1, CUDA 12.6, TensorRT 10.3, cuDNN 9.0, Python 3.10 with `pyds` bindings
- Cameras: Baumer GigE (`VCXG.2-127C`), read via `aravissrc` + `tcamconvert`
- Development happens on macOS: nothing here can be run or tested locally, only on the Jetson.

## How it runs

- Two production lines exist: **PL1** (fl1, fl2, fl3) and **PL2** (fl1–fl4), one camera per filling line.
  This repo is the **PL1** variant (`PRODUCTION_LINE = "pl1"` in `config.py`); PL2 runs a separate copy.
  Unifying both into one repo is planned for later — don't do it unless asked.
- One pipeline **process per filling line**, at most 3 running at the same time on the Orin.
- This repo is part of a larger system: references to other repos/paths (orchestrator, `/home/reply/frosta/...`,
  `/home/reply/frosta-retraining/...`) are intentional.
- Processes are started by a separate orchestrator project (not in this repo) via
  `asyncio.create_subprocess_exec(...)` with stdout+stderr piped and `PYTHONUNBUFFERED=1`:

  ```
  python3 deepstream_pipeline/main.py -l <fl1|fl2|fl3> -c <carrots|peas|beans> [-r <session_name>] [-d]
  ```
  - `-r` manual retraining: save every frame with detections to `manual/<fl>/<unix_ts>`, Redis topic `deepstream_yolo_results_manual`.
  - `-d` deployment: stop 5 s after start (used to build the TensorRT `.engine`).
- Because it is a child process: stdout/stderr go to the parent, exit codes from `sys.exit(...)` are what the parent sees,
  and shutdown cleanup (`pipeline.set_state(NULL)`) only runs on SIGINT (`KeyboardInterrupt`) / EOS / bus error — SIGTERM is not handled.

## Pipeline (`pipeline_builder.py`)

```
aravissrc → capsfilter(video/x-bayer) → tcamconvert → queue(leaky, 5 bufs) → capsfilter(video/x-raw) → tee
   ├─ queue1 → nvvideoconvert(src-crop = left half)  → capsfilter(NVMM RGBA 1600×H) → nvstreammux.sink_0
   └─ queue2 → nvvideoconvert(src-crop = right half) → capsfilter(NVMM RGBA 1600×H) → nvstreammux.sink_1
nvstreammux → nvinfer (YOLOv7) → nvmsgconv → nvmsgbroker (Redis)
```

- "Caps" = the format agreed between two elements (pixel format, width/height, system vs. GPU `NVMM` memory). The capsfilters force each step.
- With `use_file_source=True` the source is `appsrc → jpegdec → videoconvert` fed by a thread in `main.py` (offline testing with JPEGs).
- **Geometry**: each camera frame is split into left/right halves. Each half is scaled to width 1600 and height
  `round(src_h * 1600 / (src_w // 2))` (even number), keeping the aspect ratio. `nvstreammux` has `enable_padding=0`, so it does not letterbox.
  The padding to the square 1600×1600 network input happens in `nvinfer` (`maintain-aspect-ratio=1`, `symmetric-padding=1` → top/bottom letterbox).
- `source_id` 0 = left half, 1 = right half.

## Key files

| Path | Purpose |
|---|---|
| `deepstream_pipeline/main.py` | CLI entry, logging, probes, GLib main loop, `GST_PLUGIN_PATH` setup |
| `deepstream_pipeline/pipeline_builder.py` | Builds and links the GStreamer pipeline |
| `deepstream_pipeline/config.py` | `FILLING_LINE_CONFIGS`, `ERROR_CLASSES_DICT`, `set_dynamic_config()` |
| `deepstream_pipeline/structures/pipeline_config.py` | `PipelineConfig` defaults (paths, Redis, bayer format, fps interval) |
| `deepstream_pipeline/structures/crop_type_enum.py` | `CropType` enum |
| `deepstream_pipeline/callbacks.py` | `nvinfer_probe`, `fps_probe_callback`, bus handler, event-msg meta |
| `deepstream_pipeline/utils.py` | Startup checks (files, Redis), pad caps debug |
| `deepstream_pipeline/upload_images_predictions/` | Uploader of saved images/predictions to GCS + BigQuery |
| `deepstream_pipeline/count_predictions.py`, `draw_boxes.py` | Offline helpers for inspecting saved predictions |
| `deepstream_pipeline/debug_pipeline/` | Camera-less pipeline: replays frames from disk at a set fps; `stress_test/run_stress.py` runs N in parallel and logs system load. See its README |
| `deepstream_pipeline/monitoring/` | `monitor.py` records the Jetson (tegrastats, memory, per-pipeline fps, network, kernel events) while real production pipelines run; observes only. `jetson_monitoring.py` is shared with `run_stress.py` |
| `docker/` | Unchanged production pipeline in a DeepStream 7.1 container (repo mounted at the same path, host Redis); used to show the freeze also happens in Docker. See its README |
| `nvdsinfer_custom_impl_Yolo/` | Upstream custom nvinfer parser/engine lib (C++/CUDA) |
| `utils/export_*.py`, root `config_infer_primary_*.txt`, `docs/` | Upstream export scripts, sample configs, docs |

## Configuration

- **Filling line**: add/edit an entry in `FILLING_LINE_CONFIGS` (`camera_name`, `source_width`, `source_height`). Crop and muxer values are derived from it in `set_dynamic_config()`.
- **Error classes**: `ERROR_CLASSES_DICT[crop][label] = N` → an image is saved every N-th detection of that label (countdown in `error_class_tracker`). Labels must match the model's label file.
- **Model**: `nvinfer` config at `/home/reply/DeepStream-Yolo-frosta/models/<crop>/config/config_infer_primary_yoloV7.txt`. The `models/` dir, `*.onnx` and `*.engine` are not in git.

## Outputs and contracts

- Saved frames: `deepstream_pipeline/images/<production|manual>/<fl>/...` as `.bmp`, predictions as `.json` in `deepstream_pipeline/predictions/...`.
- **File naming** (`build_image_basename()`): `crop_PL_FL_DDMMYYYY_HHMMSS_NS_source_ERRORS...`.
  The `.bmp` and `.json` basenames must match and the crop must be the first `_` segment — the uploader relies on both. Don't change this without updating the uploader.
- Prediction JSON: list of `label|left|top|width|height|confidence|src_w|src_h` strings.
- Peas: every 30th frame is saved to `deepstream_pipeline/discoloration/{images,predictions}/<fl>` for discoloration metrics.
- No images are saved when disk usage of `/` is above 80%.
- Redis (`localhost:6379`): topic `deepstream_yolo_results_<fl>`, one minimal-payload event per detection (via `nvmsgconv`, `payload-type=1`).
- Logs: `deepstream_pipeline/logs/deepstream_pipeline_<fl>.log` (rotating, 1 MB × 5).

## Build / deploy notes

- Custom lib: `CUDA_VER=12.6 make -C nvdsinfer_custom_impl_Yolo` (on the Jetson).
- Paths are hard-coded for the device user: `/home/reply/DeepStream-Yolo-frosta/...`.
- Redis must be running before start (`utils.check_redis_connection` exits otherwise).

## Known quirks

- `main.py` accepts `-l fl4` (shared with PL2), but PL1's `FILLING_LINE_CONFIGS` has no `fl4` → camera/crop/muxer values would stay 0.
- `nvmsgconv_config.txt` still contains NVIDIA sample content. Leave it as-is; it is not understood well enough yet to change safely.
- `__pycache__/*.pyc` files are committed.
- `callbacks.save_image_manual` is a debug flag (saves the first 20 frames to `images/manual/<fl>/`).
- **Jetson freezes under sustained DeepStream load** (CPU 0 stuck in `cuda-EvtHandlr`): a known NVIDIA `host1x-fence`
  driver bug, not this code; also happens in Docker. Status, evidence and next steps: `deepstream_pipeline/debug_pipeline/FINDINGS.md`.
  After a freeze, collect evidence with `bash deepstream_pipeline/monitoring/collect_crash.sh`.

## Working conventions

- `debug_pipeline/pipeline_builder.py` mirrors `pipeline_builder.py` (only the source differs). Any change to the production pipeline must be applied there too.
- Debug/stress tooling lives in `debug_pipeline/` and imports production modules; don't modify production code for debugging.

- Keep changes compatible with DeepStream 7.1 / JetPack 6.2.1 and with 3 pipelines sharing one Orin (GPU memory, CPU in probes).
- Probes run on the streaming thread: keep per-frame Python work light.
- Verify changes on the Jetson; say so explicitly when something could not be tested.
