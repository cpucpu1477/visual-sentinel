"""FastAPI 服务：REST 接口、MJPEG 推流、WebSocket 实时推送。"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ConfigManager
from .monitor import MonitorService
from .storage import AlarmStore

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

config = ConfigManager(str(DATA_DIR / "config.json"))
store = AlarmStore(str(DATA_DIR / "alarms.db"))
monitor = MonitorService(config, store, str(SNAPSHOT_DIR))


class WSManager:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            clients = list(self.clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.clients.discard(ws)


ws_manager = WSManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor.loop = asyncio.get_running_loop()
    monitor.broadcast = ws_manager.broadcast
    yield
    monitor.stop()


app = FastAPI(title="视觉哨兵 · 摄像头异常监控", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR), check_dir=False), name="static")
app.mount("/snapshots", StaticFiles(directory=str(SNAPSHOT_DIR)), name="snapshots")


class StartRequest(BaseModel):
    type: str = "camera"
    value: object = 0
    loop: bool = True


@app.get("/")
async def index():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=500, detail="前端文件缺失")
    return FileResponse(str(index_file))


@app.get("/api/status")
def get_status():
    return monitor.status()


@app.post("/api/monitor/start")
def start_monitor(req: StartRequest):
    try:
        source = monitor.start(req.type, req.value, req.loop)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "source": source, "status": monitor.status()}


@app.post("/api/monitor/stop")
def stop_monitor():
    monitor.stop()
    return {"ok": True, "status": monitor.status()}


@app.post("/api/monitor/calibrate")
def calibrate_monitor():
    monitor.calibrate()
    return {"ok": True, "status": monitor.status(), "config": config.snapshot()}


@app.get("/api/alarms")
def list_alarms(limit: int = 100, level: str | None = None):
    limit = max(1, min(int(limit), 500))
    if level not in (None, "", "warning", "critical"):
        level = None
    return {"alarms": store.list(limit=limit, level=level or None)}


@app.delete("/api/alarms")
def clear_alarms():
    store.clear()
    for name in os.listdir(SNAPSHOT_DIR):
        if name.lower().endswith(".jpg"):
            try:
                os.remove(os.path.join(SNAPSHOT_DIR, name))
            except OSError:
                pass
    return {"ok": True}


@app.get("/api/config")
def get_config():
    return config.snapshot()


@app.put("/api/config")
def update_config(patch: dict = Body(default_factory=dict)):
    updated = config.update(patch)
    if isinstance(patch, dict) and "audio" in patch:
        monitor.reload_audio()
    return updated


@app.post("/api/config/reset")
def reset_config():
    updated = config.reset()
    monitor.reload_audio()
    return updated


@app.get("/api/cameras")
def list_cameras():
    found = []
    backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
    for index in range(5):
        cap = cv2.VideoCapture(index, backend)
        try:
            if cap.isOpened():
                ok, _ = cap.read()
                if ok:
                    found.append({"index": index, "name": f"摄像头 #{index}"})
        finally:
            cap.release()
    return {"cameras": found}


@app.get("/api/audio/devices")
def audio_devices():
    try:
        import sounddevice as sd

        devices = []
        for index, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0:
                devices.append(
                    {
                        "index": index,
                        "name": info["name"],
                        "channels": info["max_input_channels"],
                    }
                )
        try:
            default = int(sd.default.device[0])
        except Exception:
            default = None
        result = {"devices": devices, "default": default}
        if not devices:
            result["error"] = "未检测到音频输入设备"
        return result
    except Exception as exc:
        return {"devices": [], "default": None, "error": str(exc)}


@app.post("/api/pick-file")
def pick_file():
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            parent=root,
            title="选择视频文件",
            filetypes=[
                ("视频文件", "*.mp4 *.avi *.mkv *.mov *.flv *.wmv *.m4v"),
                ("所有文件", "*.*"),
            ],
        )
        root.destroy()
        return {"path": path or None}
    except Exception as exc:
        return {"path": None, "error": str(exc)}


@app.post("/api/shutdown")
def shutdown():
    monitor.stop()
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return {"ok": True}


@app.get("/video_feed")
async def video_feed():
    async def generator():
        last_seq = -1
        while True:
            jpeg, seq = monitor.get_jpeg()
            if jpeg is not None and seq != last_seq:
                last_seq = seq
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode()
                    + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
            else:
                await asyncio.sleep(0.01)

    return StreamingResponse(
        generator(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        await ws.send_json(
            {
                "type": "init",
                "status": monitor.status(),
                "alarms": store.list(limit=50),
            }
        )
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        ws_manager.disconnect(ws)
