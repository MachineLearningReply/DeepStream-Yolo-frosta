# docker

Runs the **unchanged production pipeline** (`deepstream_pipeline/main.py`) inside a DeepStream 7.1 container.
Purpose: show that the Jetson freeze (CPU 0 stuck in the CUDA driver thread `cuda-EvtHandlr`, see
`deepstream_pipeline/debug_pipeline/FINDINGS.md`) also happens in Docker. Containers share the host's kernel and
GPU drivers, so a driver bug is not avoided by Docker.

- `Dockerfile`: only the dependencies (aravis, tiscamera, pyds, numpy, opencv, redis, psutil) on top of
  `nvcr.io/nvidia/deepstream:7.1-triton-multiarch`.
- `compose.yaml`: two pipelines, `fl1` and `fl3`, crop `beans`, like the Oct 8 run.

The repo is mounted at the **same path** (`/home/reply/DeepStream-Yolo-frosta`), so the code, models, engines,
YOLO parser library, images, predictions and logs are the same files as on the host. Redis is the host's Redis
(`localhost:6379`). Production pipelines save real images; the uploader sends them to GCP as usual.

## One-time preparation (Jetson)

1. Allow your user to use Docker, then log out and back in (or reconnect SSH):
   ```bash
   sudo usermod -aG docker "$USER"
   ```
   Check: `groups` lists `docker`, and `docker info | grep -i runtimes` shows `nvidia`.
2. Check the message library the pipeline loads (read-only):
   ```bash
   ls -la /opt/nvidia/deepstream/deepstream/sources/libs/nvmsgconv/libnvds_msgconv.so /opt/nvidia/deepstream/deepstream/lib/libnvds_msgconv.so
   ```
   If the first file is a self-built one (different size/date from the second), tell the developer: the image links that
   path to NVIDIA's prebuilt library instead.

## Build

```bash
cd ~/DeepStream-Yolo-frosta && git pull
docker compose -f docker/compose.yaml build
```
The first build downloads the DeepStream base image (~10 GB) and compiles aravis; later builds use the cache.

## Run (same conditions as the Oct 8 run)

Each step in its own `tmux` window. Stop any pipeline running directly on the host first (`pgrep -af main.py` prints nothing).

1. **Monitor** (on the host, it finds the container pipelines by their command line):
   ```bash
   cd ~/DeepStream-Yolo-frosta/deepstream_pipeline/monitoring && python3 monitor.py
   ```
2. **Camera fps** (fl1 and fl3 cameras, as on Oct 8):
   ```bash
   arv-tool-0.8 -n Baumer-VCXG.2-127C.I-700012638609 control AcquisitionFrameRate=10
   arv-tool-0.8 -n Baumer-VCXG.2-127C.I-700012638608 control AcquisitionFrameRate=10
   ```
3. **Pipelines**:
   ```bash
   cd ~/DeepStream-Yolo-frosta && docker compose -f docker/compose.yaml up
   ```
   `Ctrl+C` stops both; `docker compose -f docker/compose.yaml down` removes the stopped containers.

### 2-minute check
- Both containers print "Pipeline is running".
- The monitor shows `fl1` and `fl3` with ~10 fps and the camera traffic (~110 MB/s on the camera port).
- `deepstream_pipeline/logs/deepstream_pipeline_fl1.log` gets new `FPS (last 30 frames)` lines.

If the first start takes several minutes, nvinfer is rebuilding the TensorRT engine (TensorRT version in the image differs
from the host); it writes the new engine next to the model and reuses it afterwards.

## After a freeze

```bash
bash ~/DeepStream-Yolo-frosta/deepstream_pipeline/monitoring/collect_crash.sh
```
Run it **before** `docker compose down` (it saves the container output). It collects everything into one `.tar.gz`
and prints the `scp` command for the Mac.
