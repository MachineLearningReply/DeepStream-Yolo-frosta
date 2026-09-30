import json
import os
from dataclasses import dataclass, asdict


@dataclass
class PipelineConfig:
    filling_line: str
    production_line: str   # upload image gcp naming convention
    crop_type: str
    nvinfer_config_file: str
    error_classes: dict
    error_class_tracker: dict
    manual_retraining: bool
    retraining_session_id:str
    save_image_path: str
    save_discoloration_image_path: str
    save_discoloration_prediction_path: str
    save_prediction_path: str
    log_directory:str
    log_filename:str
    # --- Camera settings ---
    camera_name: str
    source_width: int
    source_height: int
    crop_width_left: int
    crop_height_left: int
    crop_pos_x_left: int
    crop_pos_y_left: int
    crop_width_right: int
    crop_height_right: int
    crop_pos_x_right: int
    crop_pos_y_right: int
    muxer_output_width: int
    muxer_output_height: int
    muxer_batch_size: int
    muxer_batch_timeout_usec: int
    redis_topic: str
    bayer_format: str
    debug_pad_caps: bool
    use_file_source: bool
    # --- File Source Configuration (only used if USE_FILE_SOURCE is True) ---
    # IMPORTANT: The path must contain a C-style number formatter like %d or %05d.
    image_path_pattern: str
    image_directory: str
    source_framerate: int
    # --- Message Broker Configuration ---
    msgconv_config_file: str
    nvds_redis_proto_lib: str
    redis_host: str
    redis_port: int
    # --- FPS Calculation ---
    fps_interval_frames: int

    def __init__(self, **kwargs):
        # --- All must be initialized to avoid conflicts when calling the constructor ---
        self.filling_line = ""
        self.production_line = "pl1"   # upload image gcp naming convention
        self.crop_type = ""
        self.nvinfer_config_file = ""
        self.error_classes = {}
        self.error_class_tracker = {}
        self.manual_retraining = False
        self.retraining_session_id = ""
        self.save_image_path = ""
        self.save_prediction_path = ""
        self.save_discoloration_image_path = ""
        self.save_discoloration_prediction_path = ""
        self.log_directory = "/home/reply/DeepStream-Yolo-frosta/deepstream_pipeline/logs"
        self.log_filename = "defaul.log"
        self.camera_name = ""
        self.source_width = 0
        self.source_height = 0
        self.crop_width_left = 0
        self.crop_height_left = 0
        self.crop_pos_x_left = 0
        self.crop_pos_y_left = 0
        self.crop_width_right = 0
        self.crop_height_right = 0
        self.crop_pos_x_right = 0
        self.crop_pos_y_right = 0
        self.muxer_output_width = 0
        self.muxer_output_height = 0
        self.muxer_batch_size = 0
        self.muxer_batch_timeout_usec = 0
        self.redis_topic = ""
        self.debug_pad_caps = False
        self.use_file_source = False
        self.image_path_pattern = os.path.abspath("/home/reply/frosta/scripts/src/preprocessing/gut-carrots-cropped/*.jpg")
        self.image_directory = os.path.abspath("/home/reply/frosta/scripts/src/preprocessing/gut-carrots-cropped/")
        self.source_framerate = 120
        self.msgconv_config_file = "/home/reply/DeepStream-Yolo-frosta/nvmsgconv_config.txt"
        self.nvds_redis_proto_lib = "/opt/nvidia/deepstream/deepstream/lib/libnvds_redis_proto.so"
        self.redis_host = "localhost"
        self.redis_port = 6379
        self.fps_interval_frames = 30
        self.bayer_format = "rggb"

        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        
        self.create_directories()
    
    def create_directories(self):
        paths_to_verify = [
            self.save_image_path,
            self.save_prediction_path,
            self.log_directory,
            self.save_discoloration_image_path,
            self.save_discoloration_prediction_path
        ]
        for path in paths_to_verify:
            if path:
                os.makedirs(os.path.abspath(path), exist_ok=True)

    def to_json(self):
        return json.dumps(asdict(self), indent=2)

    def __str__(self):
        return self.to_json()