@echo off
cd /d "%~dp0"
if exist .\venv\Scripts\presteeg.exe (
    start "" .\venv\Scripts\presteeg.exe %*
) else (
    start "" .\venv\Scripts\pythonw.exe -m presteeg %*
)
