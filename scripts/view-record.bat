@echo off
cd /d "%~dp0\.."

if not exist recordings mkdir recordings

if "%1"=="" (
    set OUT=recordings\eeg_recording.csv
) else (
    set OUT=%1
)

echo ========================================================
echo   Emotiv EPOC-X Live Stream + Real-Time Visualization
echo   Saving to: %OUT%
echo   Searching for EPOC-X headset (keys from KEYS.txt)...
echo   Close the plot window or press Ctrl+C to stop.
echo ========================================================
echo.

for %%F in ("%OUT%") do (
    if /i "%%~xF"==".csv" (
        set "NOTES=%%~dpnF.notes.csv"
    ) else (
        set "NOTES=%OUT%.notes.csv"
    )
)

.\venv\Scripts\epoc-direct-record.exe --csv "%OUT%" | .\venv\Scripts\epoc-live-viewer.exe --group 5-18 --group 19-25 --width 5s --notes "%NOTES%"
