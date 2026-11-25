Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  prestEEG Live Monitor (View Only - No Recording)" -ForegroundColor Green
Write-Host "  Searching for EPOC-X headset (keys from KEYS.txt)..." -ForegroundColor Yellow
Write-Host "  Close the plot window or press Ctrl+C to stop." -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

& .\venv\Scripts\presteeg-record.exe | & .\venv\Scripts\presteeg-live-viewer.exe --group 5-18 --group 19-25 --width 5s
