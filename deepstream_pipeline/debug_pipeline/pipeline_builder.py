# deepstream_project/debug_pipeline/pipeline_builder.py
#
# MIRRORS ../pipeline_builder.py — keep both in sync.
# The only difference: the camera (aravissrc) is replaced by an appsrc that pushes raw bayer
# frames, so everything from tcamconvert onwards is identical to production.

import sys
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

# GstAppLeakyType (GStreamer >= 1.20): drop the oldest queued buffer, like a camera that can't deliver
APPSRC_LEAKY_DOWNSTREAM = 2


def build_pipeline(config, logger, stats):
    logger.info("Building the DEBUG (replay) GStreamer pipeline...")

    # --- 1. Create common GStreamer elements ---
    elements = {}
    common_factories = {
        "raw_caps": "capsfilter",
        "tee": "tee",
        "queue1": "queue",
        "queue2": "queue",
        "vidconv_left": "nvvideoconvert",
        "vidconv_right": "nvvideoconvert",
        "nvmm_caps_left": "capsfilter",
        "nvmm_caps_right": "capsfilter",
        "muxer": "nvstreammux",
        "pgie": "nvinfer",
        "msgconv": "nvmsgconv",
        "broker": "nvmsgbroker",
    }
    for name, factory in common_factories.items():
        elements[name] = Gst.ElementFactory.make(factory, name)
        if not elements[name]:
            logger.error(f"ERROR: Failed to create element '{name}'")
            sys.exit(f"ERROR: Failed to create element '{name}'")

    pipeline = Gst.Pipeline.new("deepstream-yolo-debug-pipeline")

    # --- 2. Replay source branch: appsrc(bayer) replaces aravissrc ---
    source_factories = {
        "source": "appsrc",
        "bayer_caps": "capsfilter",
        "tcamconv": "tcamconvert",
        "queue_src": "queue",
    }
    for name, factory in source_factories.items():
        elements[name] = Gst.ElementFactory.make(factory, name)
        if not elements[name]:
            sys.exit(f"ERROR: Failed to create replay source element '{name}'")

    bayer_caps_str = f"video/x-bayer,format={config.bayer_format},width={config.source_width},height={config.source_height}"

    appsrc = elements["source"]
    appsrc.set_property("format", Gst.Format.TIME)
    appsrc.set_property("is-live", True)
    appsrc.set_property("do-timestamp", True)
    appsrc_caps_str = f"{bayer_caps_str},framerate={int(round(config.replay_fps * 1000))}/1000"
    appsrc.set_property("caps", Gst.Caps.from_string(appsrc_caps_str))
    # Bound appsrc's internal queue so a slow pipeline drops frames instead of growing RAM forever
    frame_bytes = config.source_width * config.source_height
    appsrc.set_property("max-bytes", 2 * frame_bytes)
    if appsrc.find_property("leaky-type") is not None:
        appsrc.set_property("leaky-type", APPSRC_LEAKY_DOWNSTREAM)
        appsrc.set_property("block", False)
    else:
        logger.warning("appsrc has no 'leaky-type' (GStreamer < 1.20): falling back to block=True; late pushes show backpressure.")
        appsrc.set_property("block", True)

    # Same leaky queue as production, plus a drop counter
    elements["queue_src"].set_property("max-size-buffers", 5)
    elements["queue_src"].set_property("leaky", 2)  # 2 = drop oldest (downstream)

    def _on_overrun(_queue):
        stats["queue_src_overrun"] += 1
    elements["queue_src"].connect("overrun", _on_overrun)

    def _count_debayer_input(_pad, _info):
        stats["entered_tcamconvert"] += 1
        return Gst.PadProbeReturn.OK
    elements["tcamconv"].get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, _count_debayer_input)

    elements["bayer_caps"].set_property("caps", Gst.Caps.from_string(bayer_caps_str))

    pipeline.add(elements["source"])
    pipeline.add(elements["bayer_caps"])
    pipeline.add(elements["tcamconv"])
    pipeline.add(elements["queue_src"])
    if not elements["source"].link(elements["bayer_caps"]): sys.exit("ERROR: Could not link appsrc to bayer_caps.")
    if not elements["bayer_caps"].link(elements["tcamconv"]): sys.exit("ERROR: Could not link bayer_caps to tcamconv.")
    if not elements["tcamconv"].link(elements["queue_src"]): sys.exit("ERROR: Could not link tcamconv to queue_src.")
    last_source_element = elements["queue_src"]

    # --- 3. Configure the rest of the elements (identical to production) ---
    elements["raw_caps"].set_property("caps", Gst.Caps.from_string("video/x-raw"))

    crop_left_str = f"{config.crop_pos_x_left}:{config.crop_pos_y_left}:{config.crop_width_left}:{config.crop_height_left}"
    crop_right_str = f"{config.crop_pos_x_right}:{config.crop_pos_y_right}:{config.crop_width_right}:{config.crop_height_right}"
    elements["vidconv_left"].set_property("src-crop", crop_left_str)
    elements["vidconv_right"].set_property("src-crop", crop_right_str)
    elements["vidconv_left"].set_property("compute-hw", 1)
    elements["vidconv_right"].set_property("compute-hw", 1)

    nvmm_caps_str = f"video/x-raw(memory:NVMM),format=RGBA,width={config.muxer_output_width},height={config.muxer_output_height}"
    nvmm_caps = Gst.Caps.from_string(nvmm_caps_str)
    elements["nvmm_caps_left"].set_property("caps", nvmm_caps)
    elements["nvmm_caps_right"].set_property("caps", nvmm_caps)

    elements["muxer"].set_property("batch-size", config.muxer_batch_size)
    elements["muxer"].set_property("width", config.muxer_output_width)
    elements["muxer"].set_property("height", config.muxer_output_height)
    elements["muxer"].set_property("live-source", True)
    elements["muxer"].set_property("batched-push-timeout", config.muxer_batch_timeout_usec)
    elements["muxer"].set_property("compute-hw", 1)
    elements["muxer"].set_property("enable-padding", config.enable_padding)

    elements["pgie"].set_property("config-file-path", config.nvinfer_config_file)

    elements["msgconv"].set_property("config", config.msgconv_config_file)
    elements["msgconv"].set_property("payload-type", 1) # PAYLOAD CUSTOM --> 257 MINIMAL --> 1
    elements["msgconv"].set_property("msg2p-lib", "/opt/nvidia/deepstream/deepstream/sources/libs/nvmsgconv/libnvds_msgconv.so")
    elements["msgconv"].set_property("msg2p-newapi", 1)
    elements["msgconv"].set_property("frame-interval", 1)

    elements["broker"].set_property("proto-lib", config.nvds_redis_proto_lib)
    elements["broker"].set_property("conn-str", f"{config.redis_host};{config.redis_port}")
    elements["broker"].set_property("topic",  config.redis_topic)
    elements["broker"].set_property("sync", False)

    # --- 4. Add common elements and link the full pipeline ---
    for element in elements.values():
        if not element.get_parent():
            pipeline.add(element)

    logger.info("Linking pipeline elements...")

    if not last_source_element.link(elements["raw_caps"]): sys.exit("ERROR: Could not link source branch to raw_caps.")
    if not elements["raw_caps"].link(elements["tee"]): sys.exit("ERROR: Could not link raw_caps to tee.")

    # Branch 1 (Left)
    tee_src_pad1 = elements["tee"].get_request_pad("src_%u")
    queue1_sink_pad = elements["queue1"].get_static_pad("sink")
    if tee_src_pad1.link(queue1_sink_pad) != Gst.PadLinkReturn.OK: sys.exit("ERROR: Could not link tee to queue1.")

    if not elements["queue1"].link(elements["vidconv_left"]): sys.exit("ERROR: Could not link queue1 to vidconv_left.")
    if not elements["vidconv_left"].link(elements["nvmm_caps_left"]): sys.exit("ERROR: Could not link vidconv_left to nvmm_caps_left.")

    # Branch 2 (Right)
    tee_src_pad2 = elements["tee"].get_request_pad("src_%u")
    queue2_sink_pad = elements["queue2"].get_static_pad("sink")
    if tee_src_pad2.link(queue2_sink_pad) != Gst.PadLinkReturn.OK: sys.exit("ERROR: Could not link tee to queue2.")

    if not elements["queue2"].link(elements["vidconv_right"]): sys.exit("ERROR: Could not link queue2 to vidconv_right.")
    if not elements["vidconv_right"].link(elements["nvmm_caps_right"]): sys.exit("ERROR: Could not link vidconv_right to nvmm_caps_right.")

    # Merge branches into muxer
    muxer_sink_pad_0 = elements["muxer"].get_request_pad("sink_0")
    nvmm_left_src_pad = elements["nvmm_caps_left"].get_static_pad("src")
    if nvmm_left_src_pad.link(muxer_sink_pad_0) != Gst.PadLinkReturn.OK: sys.exit("ERROR: Could not link nvmm_caps_left to muxer.")

    muxer_sink_pad_1 = elements["muxer"].get_request_pad("sink_1")
    nvmm_right_src_pad = elements["nvmm_caps_right"].get_static_pad("src")
    if nvmm_right_src_pad.link(muxer_sink_pad_1) != Gst.PadLinkReturn.OK: sys.exit("ERROR: Could not link nvmm_caps_right to muxer.")

    # Link the rest of the pipeline
    if not elements["muxer"].link(elements["pgie"]): sys.exit("ERROR: Could not link muxer to pgie.")
    if not elements["pgie"].link(elements["msgconv"]): sys.exit("ERROR: Could not link pgie to msgconv.")
    if not elements["msgconv"].link(elements["broker"]): sys.exit("ERROR: Could not link msgconv to broker.")

    logger.info("Debug pipeline build complete.")
    return pipeline, elements
