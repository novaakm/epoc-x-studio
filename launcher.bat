@echo off
cd /d "%~dp0"
if exist .\venv\Scripts\epoc-studio.exe (
    start "" .\venv\Scripts\epoc-studio.exe %*
) else (
    start "" .\venv\Scripts\pythonw.exe -m epoc_studio %*
)
