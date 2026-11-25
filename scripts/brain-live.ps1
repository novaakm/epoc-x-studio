Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  prestEEG 3D Brain Activity Streamer" -ForegroundColor Cyan
Write-Host "  MODE: Live BLE Headset Streaming (keys from KEYS.txt)" -ForegroundColor Gray
Write-Host "  Opening native 3D Brain Map desktop window..." -ForegroundColor Yellow
Write-Host "  Press Ctrl+C to stop." -ForegroundColor Gray
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

& .\venv\Scripts\presteeg-record.exe | & .\venv\Scripts\presteeg-brain-map.exe
