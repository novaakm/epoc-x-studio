@echo off
cd /d "%~dp0\.."
if "%1"=="" (
    if exist recordings\eeg_recording.csv (
        set IN=recordings\eeg_recording.csv
    ) else (
        set IN=eeg_recording.csv
    )
) else (
    set IN=%1
)

echo ========================================================
echo   EPOC-X 3D Holographic Brain Activity Streamer
echo   MODE: Replaying %IN% in real-time
echo   Opening native 3D Brain Map desktop window...
echo   Press Ctrl+C in this terminal to stop.
echo ========================================================

.\venv\Scripts\epoc-brain-map.exe --replay "%IN%"
