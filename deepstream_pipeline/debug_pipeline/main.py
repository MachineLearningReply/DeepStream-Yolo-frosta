# deepstream_project/debug_pipeline/main.py
#
# Camera-less version of ../main.py: replays frames from disk at a fixed fps through the
# production pipeline (tcamconvert → crop/split → nvstreammux → nvinfer → Redis) and the
# production probes (callbacks.py). Outputs go to debug/<tag> directories, never production ones.
#
#   python3 debug_pipeline/main.py -l fl1 -c carrots --replay-dir /data/frames/fl1 --fps 7 --duration 600 --tag st1

import os
import sys
import signal
import time
import argparse
import logging
from logging.handlers import RotatingFileHandler

# Production modules (config, callbacks, utils, structures) live in the parent directory.
# Appended (not prepended) so this directory's pipeline_builder.py wins over the production one.
PIPELINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PIPELINE_DIR)

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import pyds

from config import set_dynamic_config
from pipeline_builder import build_pipeline   # debug_pipeline/pipeline_builder.py
from replay_source import load_frames, ReplayFeeder
import callbacks
import utils

_GST_PLUGIN_DIRS = "/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0:/usr/lib/aarch64-linux-gnu/gstreamer-1.0"
os.environ["GST_PLUGIN_PATH"] = ":".join(
    p for p in [_GST_PLUGIN_DIRS, os.environ.get("GST_PLUGIN_PATH", "")] if p
)

deepstream_logger = logging.getLogger('deepstream_pipeline')
deepstream_logger.setLevel(logging.DEBUG)


def apply_debug_tag(config, tag):
    """Redirect every output of this run to debug/<tag> so it never mixes with production data."""
    config.run_tag = tag
    config.save_image_path = os.path.join(PIPELINE_DIR, "images", "debug", tag)
    config.save_prediction_path = os.path.join(PIPELINE_DIR, "predictions", "debug", tag)
    config.save_discoloration_image_path = os.path.join(PIPELINE_DIR, "discoloration", "images", "debug", tag)
    config.save_discoloration_prediction_path = os.path.join(PIPELINE_DIR, "discoloration", "predictions", "debug", tag)
    config.redis_topic = f"deepstream_yolo_results_debug_{tag}"
    config.log_filename = f"debug_pipeline_{tag}.log"
    config.create_directories()


def frames_counter_probe(pad, info, stats):
    """Counts camera-half frames leaving nvinfer (a batch may hold 1 or 2 frames)."""
    gst_buffer = info.get_buffer()
    if gst_buffer:
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        if batch_meta:
            stats["processed_frames"] += batch_meta.num_frames_in_batch
    return Gst.PadProbeReturn.OK


def make_reporter(stats, tag):
    """Returns a function that logs a single machine-parsable REPLAY_STATS line (read by stress_test/run_stress.py)."""
    t0 = time.monotonic()
    last = {"t": t0, "pushed": 0, "processed_frames": 0}

    def report():
        now = time.monotonic()
        dt = max(now - last["t"], 1e-6)
        elapsed = max(now - t0, 1e-6)
        push_fps = (stats["pushed"] - last["pushed"]) / dt
        # 2 frames (left + right half) per camera frame
        cam_fps = (stats["processed_frames"] - last["processed_frames"]) / 2 / dt
        avg_cam_fps = stats["processed_frames"] / 2 / elapsed
        # Frames pushed but never debayered were dropped by appsrc's leaky queue (tcamconvert too slow)
        appsrc_dropped = stats["pushed"] - stats["entered_tcamconvert"]
        line = (f"REPLAY_STATS tag={tag} t={time.time():.0f} elapsed_s={elapsed:.0f} pushed={stats['pushed']} "
                f"late={stats['late']} appsrc_dropped={appsrc_dropped} queue_src_overrun={stats['queue_src_overrun']} "
                f"processed_frames={stats['processed_frames']} push_fps={push_fps:.2f} "
                f"processed_cam_fps={cam_fps:.2f} avg_processed_cam_fps={avg_cam_fps:.2f}")
        print(line, flush=True)
        deepstream_logger.info(line)
        last.update(t=now, pushed=stats["pushed"], processed_frames=stats["processed_frames"])

    return report


def main(config):
    Gst.init(None)
    utils.check_required_files(config)
    utils.check_redis_connection(config)

    stats = {"pushed": 0, "late": 0, "entered_tcamconvert": 0, "queue_src_overrun": 0, "processed_frames": 0}

    frames = load_frames(config.replay_dir, config.source_width, config.source_height,
                         config.bayer_format, config.replay_max_images, deepstream_logger)

    pipeline, elements = build_pipeline(config, deepstream_logger, stats)

    pgie_src_pad = elements["pgie"].get_static_pad("src")
    pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, callbacks.nvinfer_probe, {"config": config, "logger": deepstream_logger})
    pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, callbacks.fps_probe_callback, {"interval_frames": config.fps_interval_frames, "logger": deepstream_logger})
    pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, frames_counter_probe, stats)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", callbacks.bus_call, loop)

    # Clean shutdown on SIGINT and SIGTERM (stress runner / orchestrator style stop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])

    feeder = ReplayFeeder(elements["source"], frames, config.replay_fps, config.replay_duration_s,
                          stats, deepstream_logger, make_reporter(stats, config.run_tag))

    deepstream_logger.info(f"Starting debug pipeline: line={config.filling_line} crop={config.crop_type} "
                           f"fps={config.replay_fps} duration={config.replay_duration_s or 'until stopped'}s tag={config.run_tag}")
    pipeline.set_state(Gst.State.PLAYING)
    feeder.start()

    try:
        print("Debug pipeline is running. Press Ctrl+C to stop.", flush=True)
        loop.run()
    finally:
        feeder.stop()
        feeder.join(timeout=5)
        deepstream_logger.info("Stopping the debug pipeline...")
        pipeline.set_state(Gst.State.NULL)
        deepstream_logger.info("Debug pipeline stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Camera-less DeepStream pipeline replaying frames from disk.")
    parser.add_argument("-l", "--line", choices=["fl1", "fl2", "fl3", "fl4"], required=True,
                        help="Filling line whose camera geometry (resolution, crop) is emulated.")
    parser.add_argument("-c", "--crop-type", choices=["peas", "carrots", "beans"], required=True)
    parser.add_argument("--replay-dir", required=True,
                        help="Directory with .raw bayer frames (preferred) or full-resolution images.")
    parser.add_argument("--fps", type=float, default=7.0, help="Emulated camera frame rate.")
    parser.add_argument("--duration", type=int, default=0, help="Seconds to run, 0 = until stopped.")
    parser.add_argument("--max-images", type=int, default=10, help="Max frames preloaded into RAM.")
    parser.add_argument("--tag", default=None, help="Run name for output dirs/topic/log (default: <line>).")
    args = parser.parse_args()

    config = set_dynamic_config(args.crop_type, args.line)
    if not config.source_width:
        sys.exit(f"ERROR: no camera geometry for line '{args.line}' in FILLING_LINE_CONFIGS.")
    config.replay_dir = args.replay_dir
    config.replay_fps = args.fps
    config.replay_duration_s = args.duration
    config.replay_max_images = args.max_images
    apply_debug_tag(config, args.tag or args.line)

    log_handler = RotatingFileHandler(f"{config.log_directory}/{config.log_filename}", maxBytes=1024*1024, backupCount=5)
    log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    deepstream_logger.addHandler(log_handler)
    deepstream_logger.debug(config)

    main(config)
