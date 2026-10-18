@echo off
chcp 65001 >nul
cd /d "%~dp0"
python run.py
if errorlevel 1 (
  echo.
  echo 启动失败。请按 readme.md 安装 Python 依赖并构建界面，或下载 Release 中的 FileAtlas.exe。
  pause
)
