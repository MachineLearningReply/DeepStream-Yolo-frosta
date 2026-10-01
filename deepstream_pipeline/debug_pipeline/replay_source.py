# deepstream_project/debug_pipeline/replay_source.py
#
# Loads frames from disk once and pushes them into the replay appsrc at a fixed rate,
# emulating a GigE camera delivering raw bayer frames.

import glob
import os
import threading
import time

import cv2
import numpy as np
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

RAW_EXTENSIONS = (".raw",)
IMAGE_EXTENSIONS = (".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff")

# (row, col) offset of each colour inside the 2x2 bayer tile
BAYER_OFFSETS = {
    "rggb": {"r": (0, 0), "g1": (0, 1), "g2": (1, 0), "b": (1, 1)},
    "bggr": {"b": (0, 0), "g1": (0, 1), "g2": (1, 0), "r": (1, 1)},
    "grbg": {"g1": (0, 0), "r": (0, 1), "b": (1, 0), "g2": (1, 1)},
    "gbrg": {"g1": (0, 0), "b": (0, 1), "r": (1, 0), "g2": (1, 1)},
}
BGR_CHANNEL = {"b": 0, "g1": 1, "g2": 1, "r": 2}


def bgr_to_bayer(img_bgr, width, height, bayer_format):
    """Resize a colour image to the sensor resolution and mosaic it into a single-channel bayer frame."""
    if img_bgr.shape[1] != width or img_bgr.shape[0] != height:
        img_bgr = cv2.resize(img_bgr, (width, height), interpolation=cv2.INTER_AREA)
    bayer = np.empty((height, width), dtype=np.uint8)
    for colour, (dy, dx) in BAYER_OFFSETS[bayer_format].items():
        bayer[dy::2, dx::2] = img_bgr[dy::2, dx::2, BGR_CHANNEL[colour]]
    return bayer


def load_frames(directory, width, height, bayer_format, max_images, logger):
    """Returns a list of raw bayer frames (bytes, width*height each).

    Prefers .raw files (captured straight from the camera, exact production data);
    otherwise converts colour images to bayer.
    """
    files = sorted(glob.glob(os.path.join(directory, "*")))
    raw_files = [f for f in files if f.lower().endswith(RAW_EXTENSIONS)]
    image_files = [f for f in files if f.lower().endswith(IMAGE_EXTENSIONS)]
    frame_bytes = width * height

    frames = []
    if raw_files:
        for path in raw_files[:max_images]:
            size = os.path.getsize(path)
            if size != frame_bytes:
                logger.warning(f"Skipping {path}: {size} bytes, expected {frame_bytes} ({width}x{height} bayer8)")
                continue
            with open(path, "rb") as f:
                frames.append(f.read())
        source_kind = "raw bayer"
    else:
        for path in image_files[:max_images]:
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is None:
                logger.warning(f"Skipping {path}: could not be read")
                continue
            frames.append(bgr_to_bayer(img, width, height, bayer_format).tobytes())
        source_kind = "colour images converted to bayer"

    if not frames:
        raise RuntimeError(f"No usable frames in {directory} (expected .raw of {frame_bytes} bytes or images)")

    logger.info(f"Replay: loaded {len(frames)} frames ({source_kind}), "
                f"{width}x{height} {bayer_format}, {len(frames) * frame_bytes / 1e6:.0f} MB in RAM")
    return frames


class ReplayFeeder(threading.Thread):
    """Pushes frames into appsrc at `fps` using absolute scheduling (no drift)."""

    def __init__(self, appsrc, frames, fps, duration_s, stats, logger, report_fn, report_interval_s=30):
        super().__init__(daemon=True, name="replay-feeder")
        self.appsrc = appsrc
        self.frames = frames
        self.fps = fps
        self.duration_s = duration_s
        self.stats = stats
        self.logger = logger
        self.report_fn = report_fn
        self.report_interval_s = report_interval_s
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        period = 1.0 / self.fps
        t0 = time.monotonic()
        next_report = t0 + self.report_interval_s
        i = 0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if self.duration_s and now - t0 >= self.duration_s:
                self.logger.info(f"Replay: duration of {self.duration_s}s reached.")
                break

            delay = t0 + i * period - now
            if delay > 0:
                if self.stop_event.wait(delay):
                    break
            elif delay < -period:
                # More than one frame behind schedule: the push itself is blocking (backpressure)
                self.stats["late"] += 1

            buf = Gst.Buffer.new_wrapped(self.frames[i % len(self.frames)])
            ret = self.appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                self.logger.error(f"Replay: push-buffer returned {ret}, stopping feeder.")
                break
            self.stats["pushed"] += 1
            i += 1

            if time.monotonic() >= next_report:
                self.report_fn()
                next_report += self.report_interval_s

        self.report_fn()
        self.appsrc.emit("end-of-stream")
        self.logger.info("Replay: feeder finished and sent EOS.")
