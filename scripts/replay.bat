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

for %%F in ("%IN%") do (
    if /i "%%~xF"==".csv" (
        set "NOTES=%%~dpnF.notes.csv"
    ) else (
        set "NOTES=%IN%.notes.csv"
    )
)

echo Replaying %IN% in real-time...
type "%IN%" | .\venv\Scripts\epoc-live-viewer.exe --group 5-18 --group 19-25 --width 5s --realtime-replay --notes "%NOTES%"
