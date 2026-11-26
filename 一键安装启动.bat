@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 视觉哨兵 · 安装 / 启动

rem 1. 查找 Python
set "PYCMD="
where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
  where py >nul 2>nul && set "PYCMD=py -3"
)
if not defined PYCMD (
  echo.
  echo [错误] 未检测到 Python。
  echo 请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
  echo 安装时请勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)

%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [错误] Python 版本过低，需要 3.10 或更高版本。
  echo 当前版本：
  %PYCMD% --version
  echo.
  pause
  exit /b 1
)

rem 2. 创建虚拟环境
if not exist ".venv\Scripts\python.exe" (
  echo 首次运行，正在创建虚拟环境...
  %PYCMD% -m venv .venv
  if errorlevel 1 (
    echo.
    echo [错误] 创建虚拟环境失败。
    pause
    exit /b 1
  )
)

rem 3. 检查并安装依赖
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, cv2, numpy, rapidocr_onnxruntime" >nul 2>nul
if errorlevel 1 (
  echo 正在安装依赖（首次需要几分钟，请保持网络畅通）...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败，请检查网络后重新双击本文件。
    pause
    exit /b 1
  )
)

rem 4. 启动桌面程序
echo 正在启动视觉哨兵...
start "" ".venv\Scripts\pythonw.exe" app.py
exit /b 0
