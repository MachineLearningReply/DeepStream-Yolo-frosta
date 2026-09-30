# deepstream_project/main.py

import sys
import os
import glob
import threading
import time
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib, GObject
import argparse
import logging
from logging.handlers import RotatingFileHandler


# Import custom modules
from config import set_dynamic_config
from pipeline_builder import build_pipeline
import callbacks
import utils

# aravissrc lives outside the default plugin dirs; make manual runs work like the
# service (start_system.sh exports the same path). Prepend, never clobber.
_GST_PLUGIN_DIRS = "/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0:/usr/lib/aarch64-linux-gnu/gstreamer-1.0"
os.environ["GST_PLUGIN_PATH"] = ":".join(
    p for p in [_GST_PLUGIN_DIRS, os.environ.get("GST_PLUGIN_PATH", "")] if p
)

# Global variable to signal the feeding thread to stop
stop_feeding_thread = False

deepstream_logger = logging.getLogger('deepstream_pipeline')
deepstream_logger.setLevel(logging.DEBUG)



def appsrc_need_data(appsrc, length):
    """
    This signal is not used in our push-based approach, but it's good practice
    to connect it to prevent warnings or potential stalls.
    """
    pass

def appsrc_enough_data(appsrc):
    """
    This signal is not used in our push-based approach.
    """
    pass

def image_feeding_thread(appsrc, image_list, framerate):
    """
    A thread that reads image files and pushes them into the appsrc element.
    """
    global stop_feeding_thread
    frame_duration = 1.0 / framerate
    frame_count = 0

    print(f"Starting image feeding thread. Found {len(image_list)} images.")

    while not stop_feeding_thread:
        for image_path in image_list:
            if stop_feeding_thread:
                break
            
            try:
                with open(image_path, 'rb') as f:
                    image_data = f.read()
            except Exception as e:
                print(f"Warning: Could not read image {image_path}: {e}")
                continue

            # Create a GStreamer buffer
            buf = Gst.Buffer.new_allocate(None, len(image_data), None)
            buf.fill(0, image_data)

            # Set timestamp
            buf.pts = buf.dts = frame_count * Gst.SECOND * frame_duration
            buf.duration = Gst.SECOND * frame_duration
            frame_count += 1
            
            # Push the buffer into the appsrc
            retval = appsrc.emit('push-buffer', buf)
            if retval != Gst.FlowReturn.OK:
                print(f"Error pushing buffer, retval: {retval}. Stopping thread.")
                stop_feeding_thread = True
                break
            
            # Sleep to maintain the desired framerate
            time.sleep(frame_duration)
    
    # When the loop is finished (or broken), push the End-of-Stream signal
    appsrc.emit('end-of-stream')
    print("Image feeding thread finished and sent EOS.")

def stop_pipeline(pipeline, loop, feeding_thread):
    global stop_feeding_thread
    
    print("Stopping the pipeline form stop_pipeline...")
    deepstream_logger.info("\nStopping the pipeline...")
    stop_feeding_thread = True
    if feeding_thread and feeding_thread.is_alive():
        deepstream_logger.info("Waiting for feeding thread to finish...")
        feeding_thread.join(timeout=2)

    pipeline.set_state(Gst.State.NULL)
    loop.quit()
    deepstream_logger.info("Pipeline stopped.")


def main(config, is_deployment):
    global stop_feeding_thread

    os.makedirs(config.save_image_path, exist_ok=True)
    deepstream_logger.info(f"Save image path {config.save_image_path}")
    os.makedirs(config.save_prediction_path, exist_ok=True)
    deepstream_logger.info(f"Save prediction path {config.save_prediction_path}")
    
    Gst.init(None)
    utils.check_required_files(config)
    utils.check_redis_connection(config)

    pipeline, elements = build_pipeline(config, deepstream_logger)

    feeding_thread = None
    if config.use_file_source:
        appsrc = elements.get("source")
        if not appsrc:
            deepstream_logger.error("ERROR: appsrc element not found in the pipeline.")
            sys.exit("ERROR: appsrc element not found in the pipeline.")
            
        appsrc.connect('need-data', appsrc_need_data)
        appsrc.connect('enough-data', appsrc_enough_data)
        
        image_list = sorted(glob.glob(os.path.join(config.image_directory, '*.jpg')))
        if not image_list:
            deepstream_logger.error(f"ERROR: No .jpg images found in directory: {config.image_directory}")
            sys.exit(f"ERROR: No .jpg images found in directory: {config.image_directory}")

        feeding_thread = threading.Thread(
            target=image_feeding_thread, 
            args=(appsrc, image_list, config.source_framerate)
        )
        feeding_thread.daemon = True
    
    # --- PROBES RE-ENABLED HERE ---
    deepstream_logger.info("Attaching probes to the pipeline...")
    pgie_src_pad = elements["pgie"].get_static_pad("src")
    if not pgie_src_pad:
        deepstream_logger.error("ERROR: Unable to get src pad of primary GIE", file=sys.stderr)
        sys.exit(1)
    
    # Add the main inference processing probe
    pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, callbacks.nvinfer_probe, {"config": config, "logger": deepstream_logger})
    # Add the FPS calculation probe
    pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, callbacks.fps_probe_callback, {"interval_frames": config.fps_interval_frames, "logger": deepstream_logger})
    deepstream_logger.info("Probes attached successfully.")
    # ----------------------------

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", callbacks.bus_call, loop)

    print("Starting the pipeline...")
    deepstream_logger.info("Starting the pipeline...")
    pipeline.set_state(Gst.State.PLAYING)
    
    if feeding_thread:
        print("Starting image feeding thread...")
        feeding_thread.start()
    # Add a timeout if it's a deployment. The timeout is added after the model is converted to .engine
    if is_deployment:
        GLib.timeout_add_seconds(5, lambda: stop_pipeline(pipeline, loop, None))
    
    try:
        print("Pipeline is running. Press Ctrl+C to stop.")
        deepstream_logger.info("Pipeline is running. Press Ctrl+C to stop.")
        loop.run()
    except KeyboardInterrupt:
        deepstream_logger.info("\nCtrl+C received, shutting down.")
    finally:
        stop_feeding_thread = True
        if feeding_thread and feeding_thread.is_alive():
            print("Waiting for feeding thread to finish...")
            feeding_thread.join(timeout=2)

        deepstream_logger.info("Stopping the pipeline...")
        pipeline.set_state(Gst.State.NULL)
        deepstream_logger.info("Pipeline stopped.")

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--line", choices=["fl1", "fl2", "fl3", "fl4"], required=True)
    parser.add_argument("-c", "--crop-type", choices=["peas", "carrots", "beans"])
    parser.add_argument("-r", "--manual-retraining", default=None, type=str, help="Name of the manual retraining. Introduce a value if you want to start the manual retraining.")
    parser.add_argument("-d", "--deployment", action="store_true", help="Activate if you want to deploy the a model. The pipeline stops 5 seconds after the model is deployed.")
    args = parser.parse_args()

    config = set_dynamic_config(args.crop_type, args.line, args.manual_retraining)

    deepstream_log_handler = RotatingFileHandler(f"{config.log_directory}/{config.log_filename}", maxBytes=1024*1024, backupCount=5)
    deepstream_log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    deepstream_logger.addHandler(deepstream_log_handler)
    deepstream_logger.debug(config)

    main(config, args.deployment)