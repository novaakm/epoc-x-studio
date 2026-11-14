@echo off
cd /d "%~dp0\.."

if "%1"=="--demo" (
    echo Starting headmap in DEMO mode...
    .\venv\Scripts\epoc-headmap.exe %*
    exit /b
)

echo ========================================================
echo   Emotiv EPOC-X Live Head Map ^& Electrode Quality Monitor
echo   Searching for EPOC-X headset (keys from KEYS.txt)...
echo   Close the window or press Ctrl+C to stop.
echo ========================================================
echo.

.\venv\Scripts\epoc-direct-record.exe | .\venv\Scripts\epoc-headmap.exe %*
