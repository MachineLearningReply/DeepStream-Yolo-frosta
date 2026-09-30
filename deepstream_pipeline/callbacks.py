# deepstream_project/callbacks.py

import sys
import time
import json
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import pyds
import numpy as np
import cv2
from datetime import datetime as dt
import psutil
import os

# Global variables
g_frame_counter_fps = 0
g_start_time_fps = 0
MAX_TIME_STAMP_LEN = 32
counter_save = 0
save_image_manual=False

# upload image gcp naming convention
def build_image_basename(config, frame_meta, error_labels):   # upload image gcp naming convention
    """Name shared by a saved .jpg and its .json (they MUST match: the GCS uploader
    locates the prediction via splitext(image_name)[0] + '.json', and derives the crop
    from the first '_' segment). The variable-length error list is kept last.

        crop_PL_FL_DDMMYYYY_HHMMSS_NS_source_ERRORS...
        peas_PL2_FL3_03072026_171640_605125000_0_GEBROCHENE_NACHTSCHATTEN
    """
    ns = int(frame_meta.ntp_timestamp)
    stamp = dt.fromtimestamp(ns / 1e9)                    # device-local time
    errors = sorted({lbl.upper().replace(" ", "-") for lbl in error_labels}) or ["NOERR"]
    return "_".join([
        config.crop_type,                                 # unchanged: uploader parses crop from here
        config.production_line.upper(),                   # pl2 -> PL2
        config.filling_line.upper(),                      # fl3 -> FL3
        stamp.strftime("%d%m%Y"),                         # DDMMYYYY
        stamp.strftime("%H%M%S"),                         # HHMMSS
        f"{ns % 1_000_000_000:09d}",                      # nanoseconds -> uniqueness
        str(frame_meta.source_id),
        *errors,
    ])
# upload image gcp naming convention

def bus_call(bus, message, loop):
    """Main GStreamer bus message handler."""
    msg_type = message.type
    if msg_type == Gst.MessageType.EOS:
        print("End-of-stream reached.")
        loop.quit()
    elif msg_type == Gst.MessageType.WARNING:
        err, debug = message.parse_warning()
        print(f"Warning: {err}: {debug}", file=sys.stderr)
    elif msg_type == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"Error: {err}: {debug}", file=sys.stderr)
        loop.quit()
    return True

def fps_probe_callback(pad, info, u_data):
    """Pad probe to calculate and print pipeline framerate."""
    global g_frame_counter_fps, g_start_time_fps
    logger = u_data["logger"]
    interval_frames = u_data["interval_frames"]
    now = time.time()
    if g_start_time_fps == 0:
        g_start_time_fps = now
    
    g_frame_counter_fps += 1
    if g_frame_counter_fps % interval_frames == 0:
        elapsed_time = now - g_start_time_fps
        if elapsed_time > 0:
            fps = interval_frames / elapsed_time
            logger.info(f"{dt.now()} --- FPS (last {interval_frames} frames): {fps:.2f} ---")
        g_start_time_fps = now
        
    return Gst.PadProbeReturn.OK

# --- Custom Metadata Handling Functions ---

def custom_meta_copy_func(data, user_data):
    """Callback to copy custom NvDsEventMsgMeta metadata."""
    user_meta = pyds.NvDsUserMeta.cast(data)
    src_meta = pyds.NvDsEventMsgMeta.cast(user_meta.user_meta_data)
    
    dst_meta_ptr = pyds.memdup(pyds.get_ptr(src_meta), sys.getsizeof(pyds.NvDsEventMsgMeta))
    dst_meta = pyds.NvDsEventMsgMeta.cast(dst_meta_ptr)

    dst_meta.sensorStr = pyds.get_string(src_meta.sensorStr)
    
    if src_meta.objectId:
        dst_meta.objectId = pyds.get_string(src_meta.objectId)
    if src_meta.otherAttrs:
        dst_meta.otherAttrs = pyds.get_string(src_meta.otherAttrs)
    if src_meta.ts:
        dst_meta.ts = pyds.memdup(pyds.get_ptr(src_meta.ts), MAX_TIME_STAMP_LEN + 1)

    return dst_meta

def custom_meta_free_func(data, user_data):
    """
    Callback to free the custom NvDsEventMsgMeta metadata.
    This is the most robust version, using pyds.get_ptr() for freeing.
    """
    user_meta = pyds.NvDsUserMeta.cast(data)
    if user_meta and user_meta.user_meta_data:
        meta = pyds.NvDsEventMsgMeta.cast(user_meta.user_meta_data)
        
        # Free the timestamp buffer if it exists
        if meta.ts:
            pyds.free_buffer(meta.ts)
        if meta.objectId:
            pyds.free_buffer(meta.objectId)
        if meta.otherAttrs:
            pyds.free_buffer(meta.otherAttrs)

        
        # THE CRITICAL FIX: Get the raw pointer address from the Python object
        # before passing it to the C-level free function.
        pyds.free_buffer(pyds.get_ptr(user_meta.user_meta_data))


def generate_event_msg_meta(data, class_id, custom_object):
    custom_object_str = json.dumps(custom_object)
    meta = pyds.NvDsEventMsgMeta.cast(data)
    meta.sensorId = 0
    meta.placeId = 0
    meta.moduleId = 0
    meta.sensorStr = "sensor-0"
    meta.objectId = "custom"
    meta.otherAttrs = custom_object_str
    meta.ts = pyds.alloc_buffer(MAX_TIME_STAMP_LEN + 1)
    pyds.generate_ts_rfc3339(meta.ts, MAX_TIME_STAMP_LEN)
    meta.type =  pyds.NvDsEventType.NVDS_EVENT_CUSTOM

    return meta

def nvinfer_probe(pad, info, u_data):
    """
    Pad probe on nvinfer's src pad.
    Processes inference results and attaches custom metadata for the message broker.
    """
    SAMPLING_RATE = 30 #Peas images will be analysed every SAMPLING_RATE frames to check discoloration
    logger = u_data["logger"]
    config = u_data["config"]
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        return Gst.PadProbeReturn.OK

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    error_threshold_tracker = config.error_class_tracker
    
    while l_frame is not None:
        """
        If manual retraining is set to true, all images are saved. 
        """
        save_image = config.manual_retraining
        detected_objects = []
        
        error_labels = set()   # upload image gcp naming convention: error classes in this frame -> filename
        
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        l_obj = frame_meta.obj_meta_list
        frame_number = frame_meta.frame_num

        is_pea_crop = (config.crop_type.lower() == "peas" or config.crop_type.lower() == "pea")
        
        # If the crops are peas, the frame number is divisible by 30 and the frame number is more than one (so it doesnt start on the first frame), then we analyse the discoloration
        is_analytics_sample = (
            is_pea_crop and 
            frame_meta.frame_num > 0 and 
            frame_meta.frame_num % SAMPLING_RATE == 0
        )
        # print(frame_number)
        # print(frame_meta.source_frame_height)
        global counter_save
        if counter_save < 20 and save_image_manual:
            counter_save += 1
            n_frame = pyds.get_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)
            frame = np.array(n_frame, copy=True, order='C')
            frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
            cv2.imwrite(f"/home/reply/DeepStream-Yolo-frosta/deepstream_pipeline/images/manual/{config.filling_line}/{frame_number}_{frame_meta.source_id}.jpg", frame)
        #if l_obj is None:
        #    print("No objects detected")
        while l_obj is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            if obj_meta.obj_label in config.error_classes:
                error_threshold_tracker[obj_meta.obj_label] -= 1
                if error_threshold_tracker[obj_meta.obj_label] == 0:
                    error_labels.add(obj_meta.obj_label)   # upload image gcp naming convention
                    save_image = True
                    
                    logger.debug(f"------------ Saved image because of the error class {obj_meta.obj_label} -------------------")
                    logger.debug(f"Class tracker: {error_threshold_tracker}")
                    error_threshold_tracker[obj_meta.obj_label] = config.error_classes[obj_meta.obj_label]
                # else:
                #     print(f"Deleted {obj_meta.obj_label} there are {error_threshold_tracker[obj_meta.obj_label]} left")

            
            detected_objects.append(f"{obj_meta.obj_label}|{obj_meta.rect_params.left}|{obj_meta.rect_params.top}|{int(obj_meta.rect_params.width)}|{int(obj_meta.rect_params.height)}|{obj_meta.confidence:.4f}|{frame_meta.source_frame_width}|{frame_meta.source_frame_height}")
            detection_info = {
                'class_id': obj_meta.class_id,
                'class_label': obj_meta.obj_label,
                'confidence': f"{obj_meta.confidence:.4f}",
                'x1': int(obj_meta.rect_params.left),
                'y1': int(obj_meta.rect_params.top),
                'x2': int(obj_meta.rect_params.left + obj_meta.rect_params.width),
                'y2': int(obj_meta.rect_params.top + obj_meta.rect_params.height),
                'img_width': frame_meta.source_frame_width,
                'img_height': frame_meta.source_frame_height,
                'frame': frame_number,
            }
            #print(f"---------- Frame: {frame_number} ------------")
            #print(detection_info)

            user_event_meta = pyds.nvds_acquire_user_meta_from_pool(batch_meta)
            if user_event_meta:
                msg_meta = pyds.alloc_nvds_event_msg_meta(user_event_meta)
                msg_meta.objClassId = int(obj_meta.class_id)
                msg_meta.confidence = obj_meta.confidence
                msg_meta.bbox.top = obj_meta.rect_params.top
                msg_meta.bbox.left = obj_meta.rect_params.left
                msg_meta.bbox.width = obj_meta.rect_params.width
                msg_meta.bbox.height = obj_meta.rect_params.height
                msg_meta.frameId = frame_meta.frame_num
                msg_meta.confidence = obj_meta.confidence
                
                # msg_meta.sensorStr = "sensor-0"
                # msg_meta.objectId = f"custom"
                # msg_meta.otherAttrs = json.dumps(detection_info)
                
                # msg_meta.ts = pyds.alloc_buffer(MAX_TIME_STAMP_LEN + 1)
                # pyds.generate_ts_rfc3339(msg_meta.ts, MAX_TIME_STAMP_LEN)
                
                # msg_meta.type = pyds.NvDsEventType.NVDS_EVENT_CUSTOM
                msg_meta = generate_event_msg_meta(msg_meta, obj_meta.class_id, detection_info)
                user_event_meta.user_meta_data = msg_meta
                user_event_meta.base_meta.meta_type = pyds.NvDsMetaType.NVDS_EVENT_MSG_META

                pyds.set_user_copyfunc(user_event_meta, custom_meta_copy_func)
                pyds.set_user_releasefunc(user_event_meta, custom_meta_free_func)
                
                pyds.nvds_add_user_meta_to_frame(frame_meta, user_event_meta)

            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        # Get disk usage statistics for the root directory
        disk_usage = psutil.disk_usage('/')

        used_percent = float(disk_usage.percent)

        if used_percent > 80:
            logger.warning("Disk space is too high, no more images will be saved for now. Please reduce the disk space usage.")
        
        # if used_percent < 80: # used in manual testing 
        if (used_percent < 80 and len(detected_objects) > 0):
            
            if save_image:
                logger.debug(" ------ Saving Image ------------")
                n_frame = pyds.get_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)
                frame = np.array(n_frame, copy=True, order='C')
                frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
                #cv2.imwrite(f"{config.save_image_path}/{config.crop_type}_{frame_meta.ntp_timestamp}_{frame_meta.source_id}.jpg", frame)
                #with open(f"{config.save_prediction_path}/{config.crop_type}_{frame_meta.ntp_timestamp}_{frame_meta.source_id}.json", "w") as f:
                #    json.dump(detected_objects,f)
                # upload image gcp naming convention
                basename = build_image_basename(config, frame_meta, error_labels)   
                #cv2.imwrite(f"{config.save_image_path}/{basename}.jpg", frame)  
                # saving using bmp convention.
                cv2.imwrite(f"{config.save_image_path}/{basename}.bmp", frame)
                with open(f"{config.save_prediction_path}/{basename}.json", "w") as f:   
                    json.dump(detected_objects,f)
                # upload image gcp naming convention
                if config.manual_retraining:
                    config_json = {
                        "filling_line": config.filling_line, 
                        "crop_type": config.crop_type,
                        "recording_session_id": config.retraining_session_id
                    }
                    config_file_path = os.path.join(config.save_image_path, "config.json")
                    with open(config_file_path, "w") as config_file:
                        json.dump(config_json, config_file)
                    
                        
            
            if is_analytics_sample and not config.manual_retraining:
                logger.debug(f"📊 Frame {frame_meta.frame_num} picked for discoloration metrics.")
                n_frame = pyds.get_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)
                frame = np.array(n_frame, copy=True, order='C')
                frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
                #cv2.imwrite(f"{config.save_discoloration_image_path}/{config.crop_type}_{frame_meta.ntp_timestamp}_{frame_meta.source_id}.jpg", frame)
                # saving using bmp convention.
                cv2.imwrite(f"{config.save_discoloration_image_path}/{config.crop_type}_{frame_meta.ntp_timestamp}_{frame_meta.source_id}.bmp", frame)
                with open(f"{config.save_discoloration_prediction_path}/{config.crop_type}_{frame_meta.ntp_timestamp}_{frame_meta.source_id}.json", "w") as f:
                    json.dump(detected_objects,f)
                    
        elif len(detected_objects) == 0 and is_analytics_sample:
            logger.debug(f"Analytic sample for frame {frame_meta.frame_num} was not saved because there were no predictions")

        try:
            l_frame = l_frame.next
        except StopIteration:
            break
    # batch_meta = pyds.unmap.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
            
    return Gst.PadProbeReturn.OK