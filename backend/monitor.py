"""视频采集、检测流水线与报警分发。"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import cv2

from .detectors import AudioMonitor, VisualDetector
from .ocr import OcrEngine


class MonitorService:
    def __init__(self, config, store, snapshot_dir: str):
        self.config = config
        self.store = store
        self.snapshot_dir = snapshot_dir
        os.makedirs(snapshot_dir, exist_ok=True)

        self.detector = VisualDetector(config)
        self.audio = AudioMonitor(config, self._on_audio_level, self._on_audio_alarm)
        self.ocr = OcrEngine()

        self._lock = threading.RLock()
        self._frame_lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._latest_frame = None
        self._seq = 0

        self._running = False
        self._thread: threading.Thread | None = None
        self._cap = None
        self._stop_evt = threading.Event()
        self._source: dict | None = None
        self._cooldowns: dict[str, float] = {}

        self._ocr_thread: threading.Thread | None = None
        self._ocr_stop: threading.Event | None = None
        self._ocr_pending = 0

        self.loop: asyncio.AbstractEventLoop | None = None
        self.broadcast = None

        self.stats = {
            "brightness": 0.0,
            "contrast": 0.0,
            "diff": 0.0,
            "red_ratio": 0.0,
            "green_ratio": 0.0,
            "red_ref": 0.0,
            "green_ref": 0.0,
            "audio_db": -90.0,
            "fps": 0.0,
            "frames": 0,
            "frame_width": 0,
            "frame_height": 0,
            "ocr_value": None,
            "ocr_baseline": None,
            "ocr_ok": False,
            "ocr_text": "",
        }

    # ---------------- 对外状态 ----------------

    def status(self) -> dict:
        with self._lock:
            running = self._running
            source = dict(self._source) if self._source else None
        ocr_cfg = self.config.live()["ocr"]
        return {
            "running": running,
            "source": source,
            "stats": dict(self.stats),
            "audio": {
                "available": self.audio.available,
                "error": self.audio.error,
                "device": self.audio.device_name,
            },
            "ocr": {
                "enabled": bool(ocr_cfg.get("enabled")),
                "roi": ocr_cfg.get("roi"),
                "baseline": ocr_cfg.get("baseline"),
                "interval": ocr_cfg.get("interval"),
                "threshold": ocr_cfg.get("change_threshold"),
                "available": self.ocr.available,
                "error": self.ocr.error,
            },
        }

    def get_jpeg(self):
        with self._frame_lock:
            return self._latest_jpeg, self._seq

    # ---------------- 启停 ----------------

    def start(self, source_type: str, value, loop_playback: bool = True) -> dict:
        self.stop(notify=False)
        cap = None
        if source_type == "camera":
            try:
                index = int(value)
            except (TypeError, ValueError):
                raise ValueError("摄像头序号必须是数字")
            if index < 0:
                raise ValueError("摄像头序号不能为负数")
            backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
            cap = cv2.VideoCapture(index, backend)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(index)
            label = f"摄像头 #{index}"
        elif source_type == "file":
            path = os.path.abspath(str(value))
            if not os.path.isfile(path):
                raise ValueError("视频文件不存在")
            cap = cv2.VideoCapture(path)
            label = os.path.basename(path)
        else:
            raise ValueError("不支持的视频源类型")

        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            raise RuntimeError("无法打开视频源，请检查设备是否被占用或文件是否可播放")

        with self._lock:
            self._cap = cap
            self._source = {
                "type": source_type,
                "value": value,
                "label": label,
                "loop": bool(loop_playback),
            }
            self._stop_evt = threading.Event()
            self._cooldowns.clear()
            self.detector.reset()
            self.stats.update(
                {
                    "brightness": 0.0,
                    "contrast": 0.0,
                    "diff": 0.0,
                    "red_ratio": 0.0,
                    "green_ratio": 0.0,
                    "red_ref": 0.0,
                    "green_ref": 0.0,
                    "fps": 0.0,
                    "frames": 0,
                    "ocr_value": None,
                    "ocr_baseline": self.config.live()["ocr"].get("baseline"),
                    "ocr_ok": False,
                    "ocr_text": "",
                }
            )
            self._running = True
            self._thread = threading.Thread(target=self._run, args=(cap,), daemon=True)
            self._thread.start()

        self.audio.start()
        self._start_ocr()
        return self._source

    def stop(self, notify: bool = True) -> None:
        with self._lock:
            self._stop_evt.set()
            thread = self._thread
            self._thread = None
            self._running = False
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.5)
        with self._lock:
            cap = self._cap
            self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        self._stop_ocr()
        self.audio.stop()
        self.stats["fps"] = 0.0
        if notify:
            self._push(
                {
                    "type": "stats",
                    "stats": dict(self.stats),
                    "running": False,
                    "source": self._source,
                }
            )

    def reload_audio(self) -> None:
        if self._running:
            self.audio.stop()
            self.audio.start()

    # ---------------- 采集线程 ----------------

    def _run(self, cap) -> None:
        source = self._source or {}
        is_file = source.get("type") == "file"
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        if not (1 <= fps <= 120):
            fps = 25
        interval = 1.0 / min(fps, 30.0)

        last_frame_ts = time.time()
        last_push = 0.0
        source_lost = False

        try:
            while not self._stop_evt.is_set():
                t0 = time.time()
                ok, frame = cap.read()
                if not ok:
                    if is_file and source.get("loop") and not self._stop_evt.is_set():
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    if not source_lost:
                        source_lost = True
                        if is_file:
                            self._emit_alarm(
                                "source_end",
                                "warning",
                                "视频播放结束，监控已停止",
                                0.0,
                                None,
                                cooldown=0,
                            )
                        else:
                            self._emit_alarm(
                                "source_lost",
                                "critical",
                                "视频源中断，未能读取到画面",
                                0.0,
                                None,
                                cooldown=0,
                            )
                    break

                source_lost = False
                height, width = frame.shape[:2]
                if width > 960:
                    frame = cv2.resize(
                        frame, (960, int(height * 960 / width)), interpolation=cv2.INTER_AREA
                    )
                    height, width = frame.shape[:2]
                with self._frame_lock:
                    self.stats["frame_width"] = width
                    self.stats["frame_height"] = height

                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
                if ok:
                    with self._frame_lock:
                        self._latest_jpeg = buf.tobytes()
                        self._latest_frame = frame
                        self._seq += 1

                metrics, events = self.detector.process(frame)
                cfg = self.config.live()["video"]
                for kind, level, message, value in events:
                    self._emit_alarm(
                        kind,
                        level,
                        message,
                        value,
                        frame,
                        cooldown=float(cfg.get("cooldown_seconds", 15.0)),
                    )

                now = time.time()
                dt = now - last_frame_ts
                last_frame_ts = now
                inst_fps = 1.0 / dt if dt > 0 else 0.0
                self.stats.update(metrics)
                self.stats["fps"] = round(self.stats["fps"] * 0.8 + min(inst_fps, 120.0) * 0.2, 1)
                self.stats["frames"] += 1

                if now - last_push >= 0.4:
                    last_push = now
                    self._push(
                        {
                            "type": "stats",
                            "stats": dict(self.stats),
                            "running": True,
                            "source": source,
                        }
                    )

                if is_file:
                    time.sleep(max(0.0, interval - (time.time() - t0)))
        except Exception:
            pass
        finally:
            with self._lock:
                if self._cap is cap:
                    self._cap = None
                    self._running = False
                    try:
                        cap.release()
                    except Exception:
                        pass
            self._push(
                {
                    "type": "stats",
                    "stats": dict(self.stats),
                    "running": self._running,
                    "source": self._source,
                }
            )

    # ---------------- 区域数值识别（OCR） ----------------

    def _start_ocr(self) -> None:
        self._stop_ocr()
        self._ocr_pending = 0
        self._ocr_stop = threading.Event()
        self._ocr_thread = threading.Thread(target=self._ocr_loop, args=(self._ocr_stop,), daemon=True)
        self._ocr_thread.start()

    def _stop_ocr(self) -> None:
        stop_evt = self._ocr_stop
        thread = self._ocr_thread
        self._ocr_stop = None
        self._ocr_thread = None
        if stop_evt is not None:
            stop_evt.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def calibrate(self) -> dict:
        """重新采集红绿颜色基准，并清空数值基准（下次识别值作为基准）。"""
        self.detector.reset_baselines()
        self.config.update({"ocr": {"baseline": None}})
        with self._lock:
            self._ocr_pending = 0
            self.stats["ocr_baseline"] = None
        self._push_stats()
        return {"ok": True}

    def _push_stats(self) -> None:
        self._push(
            {
                "type": "stats",
                "stats": dict(self.stats),
                "running": self._running,
                "source": self._source,
            }
        )

    def _ocr_loop(self, stop_evt: threading.Event) -> None:
        while not stop_evt.is_set():
            cfg = self.config.live()["ocr"]
            interval = max(0.3, float(cfg.get("interval") or 1.0))
            roi = cfg.get("roi")

            if not cfg.get("enabled") or not roi:
                if self.stats.get("ocr_ok") or self.stats.get("ocr_value") is not None:
                    self.stats["ocr_ok"] = False
                    self.stats["ocr_value"] = None
                    self.stats["ocr_text"] = ""
                    self._push_stats()
                stop_evt.wait(0.5)
                continue

            with self._frame_lock:
                frame = self._latest_frame.copy() if self._latest_frame is not None else None
            if frame is None:
                stop_evt.wait(0.3)
                continue

            height, width = frame.shape[:2]
            x = min(width - 1, max(0, int(roi["x"] * width)))
            y = min(height - 1, max(0, int(roi["y"] * height)))
            x2 = min(width, x + max(1, int(roi["w"] * width)))
            y2 = min(height, y + max(1, int(roi["h"] * height)))
            crop = frame[y:y2, x:x2]

            result = self.ocr.read(crop, float(cfg.get("min_confidence", 0.4)))
            if result is None:
                self.stats["ocr_ok"] = False
                self._push_stats()
                stop_evt.wait(2.0)
                continue

            value = result.get("number")
            self.stats["ocr_text"] = result.get("text", "")
            if value is None:
                self.stats["ocr_ok"] = False
                self._push_stats()
            else:
                self.stats["ocr_ok"] = True
                self.stats["ocr_value"] = round(float(value), 3)
                self._check_value(value, cfg, frame, (x, y, x2, y2))
                self._push_stats()

            stop_evt.wait(interval)

    def _check_value(self, value: float, cfg: dict, frame, box) -> None:
        threshold = abs(float(cfg.get("change_threshold", 0.1)))
        baseline = self.config.live()["ocr"].get("baseline")

        if baseline is None:
            self.config.update({"ocr": {"baseline": float(value)}})
            self.stats["ocr_baseline"] = float(value)
            self._ocr_pending = 0
            return

        self.stats["ocr_baseline"] = float(baseline)
        deviation = float(value) - float(baseline)
        if abs(deviation) < threshold:
            self._ocr_pending = 0
            return

        self._ocr_pending += 1
        if self._ocr_pending < 2:
            return

        self._ocr_pending = 0
        annotated = frame.copy()
        cv2.rectangle(annotated, (box[0], box[1]), (box[2] - 1, box[3] - 1), (0, 255, 0), 2)
        cv2.putText(
            annotated,
            f"{float(value):g}",
            (box[0], max(14, box[1] - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2,
        )
        self._emit_alarm(
            "value_change",
            "critical",
            f"区域数值由 {float(baseline):g} 变为 {float(value):g}（偏差 {deviation:+.2f}，阈值 {threshold:g}）",
            deviation,
            annotated,
            cooldown=10.0,
        )
        self.config.update({"ocr": {"baseline": float(value)}})
        self.stats["ocr_baseline"] = float(value)

    # ---------------- 报警 ----------------

    def _on_audio_level(self, db: float) -> None:
        self.stats["audio_db"] = round(db, 1)

    def _on_audio_alarm(self, kind: str, level: str, message: str, value: float) -> None:
        with self._frame_lock:
            frame = self._latest_frame.copy() if self._latest_frame is not None else None
        self._emit_alarm(kind, level, message, value, frame, cooldown=0)

    def _emit_alarm(self, kind, level, message, value, frame, cooldown: float = 0.0):
        now = time.time()
        with self._lock:
            if cooldown > 0 and now - self._cooldowns.get(kind, 0.0) < cooldown:
                return None
            self._cooldowns[kind] = now
            source = (self._source or {}).get("label", "未知来源")

        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        snapshot = None
        if frame is not None:
            name = (
                f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}_{kind}_"
                f"{int(now * 1000) % 100000}.jpg"
            )
            try:
                cv2.imwrite(
                    os.path.join(self.snapshot_dir, name),
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 82],
                )
                snapshot = name
            except Exception:
                snapshot = None

        try:
            alarm_id = self.store.add(ts, source, kind, level, message, value, snapshot)
        except Exception:
            alarm_id = -1

        alarm = {
            "id": alarm_id,
            "ts": ts,
            "source": source,
            "kind": kind,
            "level": level,
            "message": message,
            "value": round(float(value or 0.0), 2),
            "snapshot": snapshot,
        }
        self._push({"type": "alarm", "alarm": alarm})
        return alarm

    def _push(self, payload: dict) -> None:
        loop = self.loop
        broadcaster = self.broadcast
        if loop is None or broadcaster is None:
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(broadcaster(payload)))
        except RuntimeError:
            pass
