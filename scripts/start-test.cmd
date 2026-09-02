@echo off
setlocal
cd /d "%~dp0.."

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-test.ps1" %*
set "RC=%ERRORLEVEL%"

if "%RC%"=="2" (
    echo.
    echo Board test completed: FAIL
    pause
    exit /b 2
)

if not "%RC%"=="0" (
    echo.
    echo The test could not be completed due to a technical error.
    pause
    exit /b %RC%
)

exit /b 0
