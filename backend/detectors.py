"""画面数值/颜色异常检测与声音异常检测。"""

from __future__ import annotations

import math
import threading
import time

import cv2
import numpy as np


class VisualDetector:
    """逐帧计算亮度、帧差和红/绿像素占比，并按阈值产出报警事件。"""

    PROC_SIZE = (320, 180)
    # 高饱和的红/绿像素才参与统计，避免灰暗画面干扰
    RED_RANGES = (((0, 120, 60), (10, 255, 255)), ((170, 120, 60), (180, 255, 255)))
    GREEN_RANGE = ((35, 90, 50), (85, 255, 255))

    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.prev_gray = None
        self.ref_red = None
        self.ref_green = None
        self.still_since = None
        self.occl_since = None
        self.color_since = None
        self.last_scene_alarm = 0.0
        self.started = time.time()

    def reset_baselines(self) -> None:
        """重新采集红绿颜色基准。"""
        self.ref_red = None
        self.ref_green = None
        self.color_since = None

    def _ratios(self, hsv: np.ndarray) -> tuple[float, float]:
        pixels = float(hsv.shape[0] * hsv.shape[1])
        red_mask = None
        for low, high in self.RED_RANGES:
            mask = cv2.inRange(hsv, np.array(low, np.uint8), np.array(high, np.uint8))
            red_mask = mask if red_mask is None else cv2.bitwise_or(red_mask, mask)
        green_mask = cv2.inRange(
            hsv, np.array(self.GREEN_RANGE[0], np.uint8), np.array(self.GREEN_RANGE[1], np.uint8)
        )
        return (
            float(cv2.countNonZero(red_mask)) / pixels,
            float(cv2.countNonZero(green_mask)) / pixels,
        )

    def process(self, frame: np.ndarray):
        cfg = self.config.live()["video"]
        small = cv2.resize(frame, self.PROC_SIZE, interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        brightness = float(hsv[:, :, 2].mean())
        contrast = float(gray.std())

        diff = 0.0
        if self.prev_gray is not None:
            diff = float(cv2.absdiff(gray, self.prev_gray).mean())

        red_ratio, green_ratio = self._ratios(hsv)
        self.prev_gray = gray
        now = time.time()

        if self.ref_red is None and now - self.started >= 1.0:
            self.ref_red, self.ref_green = red_ratio, green_ratio

        metrics = {
            "brightness": round(brightness, 1),
            "contrast": round(contrast, 1),
            "diff": round(diff, 1),
            "red_ratio": round(red_ratio, 4),
            "green_ratio": round(green_ratio, 4),
            "red_ref": round(self.ref_red, 4) if self.ref_red is not None else 0.0,
            "green_ref": round(self.ref_green, 4) if self.ref_green is not None else 0.0,
        }
        events: list[tuple] = []

        if not cfg["detect_enabled"] or now - self.started < 1.0:
            return metrics, events

        if brightness < cfg["brightness_min"]:
            events.append(
                ("black_screen", "critical", f"画面亮度过低（{brightness:.1f}），疑似黑屏或镜头被遮挡", brightness)
            )
        elif brightness > cfg["brightness_max"]:
            events.append(
                ("overexposed", "warning", f"画面亮度过高（{brightness:.1f}），疑似过曝", brightness)
            )

        scene_changed = diff > cfg["diff_threshold"]
        if scene_changed:
            events.append(("scene_change", "warning", f"画面剧烈变化（帧差 {diff:.1f}）", diff))
            self.last_scene_alarm = now
            self.color_since = None
        else:
            # 仅关注红色 / 绿色区域占比的变化，如指示灯变色
            hold = float(cfg.get("color_seconds", 2.0))
            threshold = float(cfg.get("color_threshold_percent", 3.0)) / 100.0
            ref_red = self.ref_red if self.ref_red is not None else red_ratio
            ref_green = self.ref_green if self.ref_green is not None else green_ratio
            delta_red = abs(red_ratio - ref_red)
            delta_green = abs(green_ratio - ref_green)
            brightness_ok = cfg["brightness_min"] <= brightness <= cfg["brightness_max"]
            changed = brightness_ok and (delta_red >= threshold or delta_green >= threshold)

            if changed:
                if self.color_since is None:
                    self.color_since = now
                elif now - self.color_since >= hold and now - self.last_scene_alarm >= hold:
                    events.append(
                        (
                            "color_shift",
                            "warning",
                            "红绿颜色变化：红 {:.1f}% → {:.1f}%，绿 {:.1f}% → {:.1f}%".format(
                                ref_red * 100,
                                red_ratio * 100,
                                ref_green * 100,
                                green_ratio * 100,
                            ),
                            round(max(delta_red, delta_green) * 100, 2),
                        )
                    )
                    self.color_since = now
                    self.ref_red, self.ref_green = red_ratio, green_ratio
            else:
                self.color_since = None

        # 画面冻结：帧差持续极小
        if diff < cfg["freeze_diff"] and brightness >= cfg["brightness_min"]:
            if self.still_since is None:
                self.still_since = now
            elif now - self.still_since >= cfg["freeze_seconds"]:
                events.append(
                    ("frozen", "critical", f"画面已静止 {now - self.still_since:.0f} 秒，疑似信号冻结", now - self.still_since)
                )
                self.still_since = now
        else:
            self.still_since = None

        # 镜头遮挡/失焦：细节（对比度）持续丢失
        if contrast < cfg["occlusion_contrast"] and brightness >= cfg["brightness_min"]:
            if self.occl_since is None:
                self.occl_since = now
            elif now - self.occl_since >= cfg["occlusion_seconds"]:
                events.append(
                    ("occluded", "critical", f"画面细节丢失（对比度 {contrast:.1f}），疑似镜头遮挡或失焦", contrast)
                )
                self.occl_since = now
        else:
            self.occl_since = None

        return metrics, events


class AudioMonitor:
    """基于本机麦克风的音量阈值与突发异响检测。"""

    def __init__(self, config, on_level, on_alarm):
        self.config = config
        self.on_level = on_level
        self.on_alarm = on_alarm
        self.available = False
        self.error = "声音检测未启动"
        self.device_name = ""
        self._stream = None
        self._baseline = None
        self._start_ts = 0.0
        self._last_alarm = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        self.stop()
        cfg = self.config.live()["audio"]
        if not cfg.get("enabled"):
            self.error = "声音检测已关闭"
            return
        try:
            import sounddevice as sd
        except Exception as exc:  # 缺少依赖时降级为仅画面检测
            self.error = f"音频组件不可用：{exc}"
            return
        try:
            device = cfg.get("device")
            if device is None:
                try:
                    sd.query_devices(kind="input")
                except Exception:
                    self.available = False
                    self.error = "未检测到音频输入设备（当前仅启用画面检测）"
                    return
                device_name = str(sd.query_devices(sd.default.device[0])["name"])
            else:
                info = sd.query_devices(device)
                if info.get("max_input_channels", 0) < 1:
                    raise RuntimeError("所选设备没有输入通道")
                device_name = str(info["name"])
            stream = sd.InputStream(
                device=device,
                channels=1,
                samplerate=44100,
                blocksize=2048,
                callback=self._callback,
            )
            stream.start()
            self._stream = stream
            self._baseline = None
            self._start_ts = time.time()
            self._last_alarm = 0.0
            self.available = True
            self.error = None
            self.device_name = device_name
        except Exception as exc:
            self._stream = None
            self.available = False
            self.error = f"无法打开音频输入：{exc}"

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        self.available = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _callback(self, indata, frames, time_info, status) -> None:
        try:
            audio = indata[:, 0].astype(np.float32)
            rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
        except Exception:
            return
        db = 20.0 * math.log10(max(rms, 1e-7))
        self.on_level(db)
        try:
            self._evaluate(db)
        except Exception:
            pass

    def _evaluate(self, db: float) -> None:
        cfg = self.config.live()["audio"]
        if not cfg.get("enabled"):
            return
        now = time.time()
        threshold_db = float(cfg.get("threshold_db", -28.0))
        cooldown = float(cfg.get("cooldown_seconds", 10.0))
        with self._lock:
            # 启动 3 秒内快速收敛环境基线，避免开机误报
            if now - self._start_ts < 3.0:
                self._baseline = db if self._baseline is None else self._baseline * 0.8 + db * 0.2
                return
            if self._baseline is None:
                self._baseline = db
            elif db < threshold_db - 6:
                self._baseline = self._baseline * 0.95 + db * 0.05
            baseline = self._baseline

        if db >= threshold_db and now - self._last_alarm >= cooldown:
            self._last_alarm = now
            if db >= baseline + float(cfg.get("spike_db", 16.0)):
                self.on_alarm("audio_spike", "critical", f"检测到突发异常声响（{db:.1f} dBFS）", db)
            else:
                self.on_alarm("audio_loud", "warning", f"环境音量超过阈值（{db:.1f} dBFS）", db)
