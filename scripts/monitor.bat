@echo off
cd /d "%~dp0\.."

if "%1"=="--demo" (
    echo Starting headmap in DEMO mode...
    .\venv\Scripts\presteeg-headmap.exe %*
    exit /b
)

echo ========================================================
echo   prestEEG Live Headmap ^& Electrode Quality Monitor
echo   Searching for EPOC-X headset (keys from KEYS.txt)...
echo   Close the window or press Ctrl+C to stop.
echo ========================================================
echo.

.\venv\Scripts\presteeg-record.exe | .\venv\Scripts\presteeg-headmap.exe %*
