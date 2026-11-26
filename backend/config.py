"""配置管理：检测阈值等参数持久化到 data/config.json。"""

from __future__ import annotations

import copy
import json
import os
import threading

DEFAULT_CONFIG = {
    "video": {
        "detect_enabled": True,
        "brightness_min": 18.0,
        "brightness_max": 243.0,
        "diff_threshold": 28.0,
        "freeze_diff": 0.1,
        "freeze_seconds": 10.0,
        "color_threshold_percent": 1.5,
        "color_seconds": 2.0,
        "occlusion_contrast": 6.0,
        "occlusion_seconds": 3.0,
        "cooldown_seconds": 15.0,
    },
    "ocr": {
        "enabled": False,
        "roi": None,
        "interval": 1.0,
        "change_threshold": 0.1,
        "baseline": None,
        "min_confidence": 0.4,
    },
    "audio": {
        "enabled": True,
        "device": None,
        "threshold_db": -28.0,
        "spike_db": 16.0,
        "cooldown_seconds": 10.0,
    },
    "ui": {
        "sound_alert": True,
    },
}


class ConfigManager:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULT_CONFIG)
        self.reload()

    def live(self) -> dict:
        """供检测线程实时读取的配置字典，请通过 update 写入。"""
        return self._data

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def reload(self) -> None:
        with self._lock:
            data = copy.deepcopy(DEFAULT_CONFIG)
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        self._merge(data, json.load(fh))
                except (OSError, json.JSONDecodeError):
                    pass
            self._data.clear()
            self._data.update(data)

    def save(self) -> None:
        with self._lock:
            self._write()

    def update(self, patch: dict) -> dict:
        with self._lock:
            if isinstance(patch, dict):
                self._merge(self._data, patch)
                self._write()
            return copy.deepcopy(self._data)

    def reset(self) -> dict:
        with self._lock:
            self._data.clear()
            self._data.update(copy.deepcopy(DEFAULT_CONFIG))
            self._write()
            return copy.deepcopy(self._data)

    def _write(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def _merge(base: dict, extra: dict) -> None:
        """只接受已知字段，并做基础类型校验，避免脏配置写坏检测逻辑。"""
        for section, values in extra.items():
            if section not in base or not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key not in base[section]:
                    continue
                default = base[section][key]
                if key == "device":
                    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
                        base[section][key] = value
                    continue
                if key == "baseline":
                    if value is None:
                        base[section][key] = None
                    elif isinstance(value, (int, float)) and not isinstance(value, bool):
                        base[section][key] = float(value)
                    continue
                if key == "roi":
                    if value is None:
                        base[section][key] = None
                    elif isinstance(value, dict):
                        try:
                            roi = {axis: float(value[axis]) for axis in ("x", "y", "w", "h")}
                        except (KeyError, TypeError, ValueError):
                            continue
                        roi["x"] = min(max(roi["x"], 0.0), 1.0)
                        roi["y"] = min(max(roi["y"], 0.0), 1.0)
                        roi["w"] = min(max(roi["w"], 0.0), 1.0 - roi["x"])
                        roi["h"] = min(max(roi["h"], 0.0), 1.0 - roi["y"])
                        if roi["w"] > 0 and roi["h"] > 0:
                            base[section][key] = roi
                    continue
                if isinstance(default, bool):
                    if isinstance(value, bool):
                        base[section][key] = value
                elif isinstance(default, (int, float)):
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        base[section][key] = float(value)
                elif isinstance(default, str) and isinstance(value, str):
                    base[section][key] = value
