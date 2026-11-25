@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment in .\venv ...
    where py >nul 2>&1
    if %errorlevel% equ 0 (
        py -3 -m venv venv
    ) else (
        where python >nul 2>&1
        if %errorlevel% neq 0 (
            echo.
            echo [ERROR] Python 3.10+ was not found in PATH.
            echo Please install Python from https://www.python.org/downloads/ and try again.
            pause
            exit /b 1
        )
        python -m venv venv
    )
    if %errorlevel% neq 0 (
        echo.
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "EXIT_CODE=%errorlevel%"

echo.
if %EXIT_CODE% equ 0 (
    echo prestEEG installed successfully!
    echo You can launch it from the Desktop/Start Menu shortcut or launcher.bat.
) else (
    echo [ERROR] Installation failed with exit code %EXIT_CODE%.
)

pause
exit /b %EXIT_CODE%
