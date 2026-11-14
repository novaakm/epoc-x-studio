param (
    [string]$OutputFile = "recordings\eeg_recording.csv"
)

Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path "recordings")) {
    New-Item -ItemType Directory -Path "recordings" | Out-Null
}

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  Emotiv EPOC-X Live Stream + Real-Time Visualization" -ForegroundColor Green
Write-Host "  Saving to: $OutputFile" -ForegroundColor Yellow
Write-Host "  Searching for EPOC-X headset (keys from KEYS.txt)..." -ForegroundColor Yellow
Write-Host "  Close the plot window or press Ctrl+C to stop." -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

$NotesFile = if ($OutputFile.EndsWith(".csv")) { $OutputFile.Substring(0, $OutputFile.Length - 4) + ".notes.csv" } else { $OutputFile + ".notes.csv" }
& .\venv\Scripts\epoc-direct-record.exe --csv $OutputFile | & .\venv\Scripts\epoc-live-viewer.exe --group 5-18 --group 19-25 --width 5s --notes $NotesFile
