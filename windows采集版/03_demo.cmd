@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run 01_install.cmd first.
    pause >nul
    exit /b 2
)
".venv\Scripts\python.exe" -m xhs_research demo
set "RESULT=%ERRORLEVEL%"
".venv\Scripts\python.exe" scripts\launcher_message.py %RESULT%
pause >nul
exit /b %RESULT%
