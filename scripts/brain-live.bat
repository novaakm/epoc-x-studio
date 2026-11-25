@echo off
cd /d "%~dp0\.."

echo ========================================================
echo   prestEEG 3D Brain Activity Streamer
echo   MODE: Live BLE Headset Streaming (keys from KEYS.txt)
echo   Opening native 3D Brain Map desktop window...
echo   Press Ctrl+C to stop.
echo ========================================================
echo.

.\venv\Scripts\presteeg-record.exe | .\venv\Scripts\presteeg-brain-map.exe
