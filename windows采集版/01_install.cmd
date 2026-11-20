@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
echo Python not found. Install 64-bit Python 3.12 from:
echo https://www.python.org/downloads/windows/
echo Enable "Add python.exe to PATH" and "Python Launcher", then retry.
pause >nul
exit /b 2
:use_py
py -3 scripts\setup_live.py --menu
set "RESULT=%ERRORLEVEL%"
py -3 scripts\launcher_message.py %RESULT%
goto finish
:use_python
python scripts\setup_live.py --menu
set "RESULT=%ERRORLEVEL%"
python scripts\launcher_message.py %RESULT%
:finish
pause >nul
exit /b %RESULT%
