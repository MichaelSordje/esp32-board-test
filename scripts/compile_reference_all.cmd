@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0compile_reference_all.ps1" %*
exit /b %ERRORLEVEL%
