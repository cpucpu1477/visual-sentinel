"""离线 OCR 引擎封装（RapidOCR），用于读取画面指定区域的数值。"""

from __future__ import annotations

import re
import threading

import cv2
import numpy as np

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


class OcrEngine:
    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()
        self.available = None
        self.error = None

    def _ensure(self) -> bool:
        if self._engine is not None:
            return True
        if self.error:
            return False
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
            self.available = True
            self.error = None
            return True
        except Exception as exc:
            self.available = False
            self.error = f"OCR 组件不可用：{exc}"
            return False

    def read(self, image: np.ndarray, min_confidence: float = 0.4):
        """识别区域中的文字与数值，返回 {text, number, score}。"""
        if image is None or image.size == 0:
            return None
        if not self._ensure():
            return None
        prepared = self._prepare(image)
        with self._lock:
            try:
                result, _ = self._engine(prepared, use_det=True, use_cls=False, use_rec=True)
            except Exception as exc:
                self.error = f"OCR 识别失败：{exc}"
                return None

        items = []
        for item in result or []:
            try:
                box, text, score = item[0], str(item[1]).strip(), float(item[2])
            except (IndexError, TypeError, ValueError):
                continue
            if text and score >= min_confidence:
                items.append((box, text, score))
        if not items:
            return {"text": "", "number": None, "score": 0.0}

        items.sort(key=lambda item: (min(p[1] for p in item[0]), min(p[0] for p in item[0])))
        combined = " ".join(item[1] for item in items)
        return {
            "text": combined,
            "number": self._parse_number(combined),
            "score": max(item[2] for item in items),
        }

    @staticmethod
    def _prepare(image: np.ndarray) -> np.ndarray:
        # 文字贴边时检测模型容易漏检，先补一圈边缘再适度放大
        height, width = image.shape[:2]
        pad = max(8, int(min(height, width) * 0.35))
        padded = cv2.copyMakeBorder(
            image, pad, pad, pad, pad, cv2.BORDER_REPLICATE
        )
        height, width = padded.shape[:2]
        scale = min(max(1.0, 320.0 / max(width, 1)), 3.0)
        if scale > 1.0:
            padded = cv2.resize(
                padded,
                (int(width * scale), int(height * scale)),
                interpolation=cv2.INTER_CUBIC,
            )
        return padded

    @staticmethod
    def _parse_number(text: str):
        normalized = text.replace("，", ".").replace(",", ".")
        normalized = normalized.replace("O", "0").replace("o", "0")
        normalized = normalized.replace("I", "1").replace("l", "1")
        match = _NUMBER_RE.search(normalized)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None
