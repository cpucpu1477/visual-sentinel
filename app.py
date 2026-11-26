"""视觉哨兵桌面入口：启动本地服务并打开应用窗口。"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_PATH = os.path.join(DATA_DIR, "app.log")


def log(message: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def find_browser() -> str | None:
    candidates = [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ]
    for raw in candidates:
        path = os.path.expandvars(raw)
        if "%" not in path and os.path.isfile(path):
            return path
    return None


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_ready(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/status", timeout=0.8) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def open_window(url: str):
    browser = find_browser()
    if browser:
        profile = os.path.join(DATA_DIR, "browser_profile")
        args = [
            browser,
            f"--app={url}",
            "--window-size=1500,940",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
        ]
        try:
            return subprocess.Popen(args)
        except Exception as exc:
            log(f"打开应用窗口失败: {exc}")
    webbrowser.open(url)
    return None


def run_server(port: int) -> None:
    import uvicorn

    uvicorn.run(
        "backend.api:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="视觉哨兵 · 摄像头异常监控")
    parser.add_argument("--port", type=int, default=8765, help="本地服务端口")
    parser.add_argument("--no-window", action="store_true", help="只启动服务，不打开窗口")
    args = parser.parse_args()

    port = args.port
    if not port_free(port):
        for candidate in range(port + 1, port + 30):
            if port_free(candidate):
                port = candidate
                break

    url = f"http://127.0.0.1:{port}/"
    log(f"启动本地服务: {url}")
    threading.Thread(target=run_server, args=(port,), daemon=True).start()

    if not wait_ready(url):
        log("服务启动超时")
        print("服务启动失败，请查看 data/app.log")
        return

    if args.no_window:
        while True:
            time.sleep(1)

    proc = open_window(url)
    try:
        if proc is not None:
            proc.wait()
            log("应用窗口已关闭，退出程序")
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("程序异常退出:\n" + traceback.format_exc())
        raise
