from typing import Dict
from structures import (
    PipelineConfig,
    CropType
)
import time

ERROR_CLASSES_DICT = {
    CropType.CARROTS.value: {
        "Bruch": 150000,
        "Fleckig Frass": 200000,
        "Krautansatz": 80000,
        "Putzfehler": 15000,
        "Zu Grosse": 30
    },
    CropType.PEAS.value: {
        "Gebrochene": 1,
        "Kamille": 1,
        "Nachtschatten": 1,
        "Kornblume": 1,
        "Schoten": 1,
        "Stiele": 1,
        "Erbsenwickler": 1,
        "Anders": 1,
        "Diestel": 1
    },
    CropType.BEANS.value: {
        "Oxidierte": 10000,
        "Schorf": 10000,
        "Stiele von Rappe": 10000,
        "Stielenden": 10000,
        "Zu klein": 10000,
        "Bohnenkerne": 10000,
        "Frass und Feule": 10000,
        "Nachtschatten": 1,
        "Anders": 100000
    }
}

#TODO: This needs to be adapted
FILLING_LINE_CONFIGS = {
    "fl1" : {
        "camera_name" : "Baumer-VCXG.2-127C.I-700012638609",
        "source_width" : 3872,
        "source_height" : 1862,
    },

    "fl2" : {
        "camera_name" : "Baumer-VCXG.2-127C.I-700012638602",
        "source_width" : 3728,
        "source_height" : 2000,
    },

    "fl3" : {
        "camera_name" : "Baumer-VCXG.2-127C.I-700012638608",
        "source_width" : 3400,
        "source_height" : 1150,
    },
}
PRODUCTION_LINE = "pl1"   # upload image gcp naming convention 


def set_dynamic_config(crop_type, filling_line, manual_retraining=None):

    is_manual_retraining = manual_retraining is not None
    error_classes = ERROR_CLASSES_DICT[crop_type]
    save_image_directory = f"production/{filling_line}"
    redis_topic = f"deepstream_yolo_results_{filling_line}"

    if is_manual_retraining:
        save_image_directory = f"manual/{filling_line}/{int(time.time())}"
        error_classes = {}
        redis_topic = "deepstream_yolo_results_manual"
        
    error_class_tracker = error_classes.copy()

    config = PipelineConfig(
        crop_type = crop_type,
        filling_line = filling_line,
        production_line = PRODUCTION_LINE,   # upload image gcp naming convention
        nvinfer_config_file = f"/home/reply/DeepStream-Yolo-frosta/models/{crop_type}/config/config_infer_primary_yoloV7.txt",
        error_classes = error_classes,
        error_class_tracker = error_class_tracker,
        manual_retraining=is_manual_retraining,
        retraining_session_id= manual_retraining,
        save_image_path = f"/home/reply/DeepStream-Yolo-frosta/deepstream_pipeline/images/{save_image_directory}",
        save_prediction_path = f"/home/reply/DeepStream-Yolo-frosta/deepstream_pipeline/predictions/{save_image_directory}",
        log_filename = f"deepstream_pipeline_{filling_line}.log",
        redis_topic = redis_topic,
        save_discoloration_image_path = f"/home/reply/DeepStream-Yolo-frosta/deepstream_pipeline/discoloration/images/{filling_line}",
        save_discoloration_prediction_path = f"/home/reply/DeepStream-Yolo-frosta/deepstream_pipeline/discoloration/predictions/{filling_line}"
    )
    
    line_config = FILLING_LINE_CONFIGS.get(filling_line)
    if line_config:
        source_width = line_config["source_width"]
        source_height = line_config["source_height"]
        half_width = source_width // 2

        config.camera_name = line_config["camera_name"]
        config.source_width = source_width
        config.source_height = source_height

        config.crop_pos_x_left = 0
        config.crop_pos_y_left = 0
        config.crop_width_left = half_width
        config.crop_height_left = source_height

        config.crop_pos_x_right = half_width
        config.crop_pos_y_right = 0
        config.crop_width_right = half_width
        config.crop_height_right = source_height

        config.muxer_output_width = 1600
        # Rounding to the nearest EVEN integer.
        config.muxer_output_height = int(round(source_height * (config.muxer_output_width / half_width) / 2.0) * 2)
        config.muxer_batch_size = 1
        config.muxer_batch_timeout_usec = 40_000
        config.enable_padding = 0
    
    return config
