@echo off
REM ASCII-only launcher: the real work is in the UTF-8 PowerShell script
REM next to this file, because .bat bodies mangle Chinese characters.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\配置GitHub推送.ps1"
