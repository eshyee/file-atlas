@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 归序将请求 Windows 管理员权限。请在系统 UAC 提示中自行选择。
echo 新窗口仍需重新扫描并确认操作，文件保护不会关闭。
if exist "%~dp0FileAtlas.exe" (
  "%~dp0FileAtlas.exe" --request-admin
) else (
  python "%~dp0run.py" --request-admin
)
if errorlevel 1 (
  echo.
  echo 管理员会话未能启动。可以使用普通启动方式，或检查系统权限策略。
  pause
)
