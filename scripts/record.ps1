param (
    [string]$OutputFile = "recordings\eeg_recording.csv"
)

Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path "recordings")) {
    New-Item -ItemType Directory -Path "recordings" | Out-Null
}

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  prestEEG Recorder" -ForegroundColor Green
Write-Host "  Saving to: $OutputFile" -ForegroundColor Yellow
Write-Host "  Searching for EPOC-X headset (keys from KEYS.txt)..." -ForegroundColor Yellow
Write-Host "  Press Ctrl+C anytime to stop recording." -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

& .\venv\Scripts\presteeg-record.exe > $OutputFile
