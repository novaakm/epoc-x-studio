param(
    [string]$InputFile = ""
)

Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not $InputFile) {
    if (Test-Path "recordings\eeg_recording.csv") {
        $InputFile = "recordings\eeg_recording.csv"
    } elseif (Test-Path "eeg_recording.csv") {
        $InputFile = "eeg_recording.csv"
    }
}

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  EPOC-X 3D Holographic Brain Activity Streamer" -ForegroundColor Cyan
Write-Host "  MODE: Replaying $InputFile in real-time" -ForegroundColor Gray
Write-Host "  Opening native 3D Brain Map desktop window..." -ForegroundColor Yellow
Write-Host "  Press Ctrl+C in this terminal to stop." -ForegroundColor Gray
Write-Host "========================================================" -ForegroundColor Cyan

& .\venv\Scripts\epoc-brain-map.exe --replay "$InputFile"
