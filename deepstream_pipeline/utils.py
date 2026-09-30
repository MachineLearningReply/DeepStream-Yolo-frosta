import sys
import os
import redis

def check_required_files(config):
    """Checks if essential configuration files exist before starting."""
    files_to_check = {
        "NVINFER_CONFIG_FILE": config.nvinfer_config_file,
        "MSGCONV_CONFIG_FILE": config.msgconv_config_file,
        "NVDS_REDIS_PROTO_LIB": config.nvds_redis_proto_lib,
    }

    all_found = True
    for name, path in files_to_check.items():
        if not os.path.exists(path):
            print(f"ERROR: File for '{name}' not found at: {path}", file=sys.stderr)
            all_found = False
    
    if not all_found:
        sys.exit(1)

def check_redis_connection(config):
    """Checks if a connection to the Redis server can be established."""
    try:
        r = redis.Redis(host=config.redis_host, port=config.redis_port, db=0)
        r.ping()
        print("Successfully connected to Redis.")
    except redis.ConnectionError:
        print(f"ERROR: Failed to connect to Redis at {config.redis_host}:{config.redis_port}.", file=sys.stderr)
        print("Please ensure the Redis server is running.", file=sys.stderr)
        sys.exit(1)

def print_pad_capabilities(element, pad_name):
    """Prints the negotiated capabilities of a GStreamer pad."""
    pad = element.get_static_pad(pad_name)
    if not pad:
        print(f"Could not retrieve pad '{pad_name}' from '{element.get_name()}'")
        return
    caps = pad.get_current_caps()
    if not caps:
        print(f"No caps on {element.get_name()} {pad_name}")
    else:
        print(f"CAPS: {element.get_name()} {pad_name}: {caps.to_string()}")