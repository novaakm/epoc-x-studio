@echo off
cd /d "%~dp0\.."

if not exist recordings mkdir recordings

if "%1"=="" (
    set OUT=recordings\eeg_recording.csv
) else (
    set OUT=%1
)

echo ========================================================
echo   prestEEG Recorder
echo   Saving to: %OUT%
echo   Searching for EPOC-X headset (keys from KEYS.txt)...
echo   Press Ctrl+C anytime to stop recording.
echo ========================================================
echo.

.\venv\Scripts\presteeg-record.exe > "%OUT%"
