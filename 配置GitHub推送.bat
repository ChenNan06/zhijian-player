@echo off
REM ASCII-only launcher. The real work is in scripts\setup-github-push.ps1
REM (a .ps1 with a UTF-8 BOM can hold Chinese; a .bat body cannot).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-github-push.ps1"
