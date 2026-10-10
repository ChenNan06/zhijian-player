@echo off
rem ============================================================================
rem  ZhiJian Player - source launcher (Windows)
rem  Usage:  run.bat [file or folder ...]
rem  Requires: Python 3.12 + PySide6 + Pillow (see requirements.txt)
rem ============================================================================
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python\python.exe"
if not exist "%PY%" set "PY=C:\DevTools\Python312\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONPATH=%ROOT%src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%ROOT%runtime\libmpv-2.dll" (
  echo [ERROR] runtime\libmpv-2.dll not found.
  echo         Run scripts\01-setup.ps1 first, or copy libmpv-2.dll into runtime\.
  pause
  exit /b 2
)

start "" "%PY%" -m zhijian %*
exit /b 0
