import json
import os


def config_path(base_dir):
    """Use a local file next to the source or installed executable."""
    return os.path.join(base_dir, "ats_monitor_local_config.json")


def load_local_config(base_dir):
    path = config_path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def config_value(config, name, default=""):
    value = config.get(name, default)
    return value if isinstance(value, str) else default
