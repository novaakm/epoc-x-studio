@echo off
cd /d "%~dp0\.."

echo ========================================================
echo   EPOC-X 3D Holographic Brain Activity Streamer
echo   MODE: Live BLE Headset Streaming (keys from KEYS.txt)
echo   Opening native 3D Brain Map desktop window...
echo   Press Ctrl+C to stop.
echo ========================================================
echo.

.\venv\Scripts\epoc-direct-record.exe | .\venv\Scripts\epoc-brain-map.exe
