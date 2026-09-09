@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage_runner.ps1"
exit /b %errorlevel%
