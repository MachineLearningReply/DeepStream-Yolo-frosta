# deepstream_project/pipeline_builder.py

import sys
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from utils import print_pad_capabilities

def build_pipeline(config, logger):
    logger.info("Building the GStreamer pipeline...")

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

    # --- 2. Create and configure the source branch based on config ---
    pipeline = Gst.Pipeline.new("deepstream-yolo-pipeline")

    if config.use_file_source:
        print("Using file source (appsrc).")
        source_factories = {
            "source": "appsrc",
            "jpegdec": "jpegdec",
            "vidconv_pre_tee": "videoconvert"
        }
        for name, factory in source_factories.items():
            elements[name] = Gst.ElementFactory.make(factory, name)
            if not elements[name]:
                logger.error(f"ERROR: Failed to create file source element '{name}'")
                sys.exit(f"ERROR: Failed to create file source element '{name}'")

        # Configure appsrc properties
        appsrc = elements["source"]
        appsrc.set_property('format', Gst.Format.TIME)
        appsrc.set_property('is-live', False)
        appsrc.set_property('do-timestamp', True)
        
        appsrc_caps = Gst.Caps.from_string(f"image/jpeg,framerate={config.source_framerate}/1")
        appsrc.set_property("caps", appsrc_caps)

        # Add and link the source chain
        pipeline.add(elements["source"])
        pipeline.add(elements["jpegdec"])
        pipeline.add(elements["vidconv_pre_tee"])
        if not elements["source"].link(elements["jpegdec"]): sys.exit("ERROR: Could not link appsrc to jpegdec.")
        if not elements["jpegdec"].link(elements["vidconv_pre_tee"]): sys.exit("ERROR: Could not link jpegdec to videoconvert.")
        
        last_source_element = elements["vidconv_pre_tee"]
    else: # Use live camera source
        logger.info("Using live camera source (aravissrc).")
        source_factories = {
            "source": "aravissrc",
            "bayer_caps": "capsfilter",
            "tcamconv": "tcamconvert",
            "queue_src": "queue",
        }
        for name, factory in source_factories.items():
            elements[name] = Gst.ElementFactory.make(factory, name)
            if not elements[name]:
                sys.exit(f"ERROR: Failed to create camera source element '{name}'")
        
        elements["source"].set_property("camera-name", config.camera_name)
        # Leaky queue: decouples aravissrc from downstream stalls
        elements["queue_src"].set_property("max-size-buffers", 5)
        elements["queue_src"].set_property("leaky", 2)  # 2 = drop oldest (downstream)

        bayer_caps_str = f"video/x-bayer,format={config.bayer_format},width={config.source_width},height={config.source_height}"
        elements["bayer_caps"].set_property("caps", Gst.Caps.from_string(bayer_caps_str))

        pipeline.add(elements["source"])
        pipeline.add(elements["bayer_caps"])
        pipeline.add(elements["tcamconv"])
        pipeline.add(elements["queue_src"])
        if not elements["source"].link(elements["bayer_caps"]): sys.exit("ERROR: Could not link aravissrc to bayer_caps.")
        if not elements["bayer_caps"].link(elements["tcamconv"]): sys.exit("ERROR: Could not link bayer_caps to tcamconv.")
        if not elements["tcamconv"].link(elements["queue_src"]): sys.exit("ERROR: Could not link tcamconv to queue_src.")
        last_source_element = elements["queue_src"]
    
    # --- 3. Configure the rest of the elements ---
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
    # elements["msgconv"].set_property("debug-payload-dir", "/home/reply/DeepStream-Yolo-frosta/deepstream_pipeline/logs/")
    

    elements["broker"].set_property("proto-lib", config.nvds_redis_proto_lib)
    elements["broker"].set_property("conn-str", f"{config.redis_host};{config.redis_port}")
    elements["broker"].set_property("topic",  config.redis_topic)
    elements["broker"].set_property("sync", False)

    # --- 4. Add common elements and link the full pipeline ---
    #
    # --- THE FIX IS HERE ---
    # We iterate over the *values* of our 'elements' dictionary, which are the Gst.Element objects.
    for element in elements.values():
        # Check if the element is already in the pipeline (sources are added earlier)
        if not element.get_parent():
            pipeline.add(element)
    # ---------------------
    
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
    if not elements["vidconv_right"].link(elements["nvmm_caps_right"]): sys.exit("ERROR: Could not link nvmm_caps_right to nvmm_caps_right.")

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

    logger.info("Pipeline build complete.")
    return pipeline, elements