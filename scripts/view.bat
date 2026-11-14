@echo off
cd /d "%~dp0\.."

echo ========================================================
echo   Emotiv EPOC-X Live Monitor (View Only - No Recording)
echo   Searching for EPOC-X headset (keys from KEYS.txt)...
echo   Close the plot window or press Ctrl+C to stop.
echo ========================================================
echo.

.\venv\Scripts\epoc-direct-record.exe | .\venv\Scripts\epoc-live-viewer.exe --group 5-18 --group 19-25 --width 5s
