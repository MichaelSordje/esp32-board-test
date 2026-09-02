@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_all.ps1" %*
exit /b %ERRORLEVEL%
