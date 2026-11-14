param (
    [switch]$Demo,
    [switch]$FloatAbove,
    [string]$Geometry,
    [string]$Name
)

Set-Location (Split-Path $PSScriptRoot -Parent)

$extraArgs = @()
if ($FloatAbove) { $extraArgs += "--float-above" }
if ($Geometry) { $extraArgs += @("--geometry", $Geometry) }
if ($Name) { $extraArgs += @("--name", $Name) }

if ($Demo) {
    Write-Host "Starting headmap in DEMO mode..." -ForegroundColor Yellow
    & .\venv\Scripts\epoc-headmap.exe --demo @extraArgs
    exit
}

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  Emotiv EPOC-X Live Head Map & Electrode Quality Monitor" -ForegroundColor Green
Write-Host "  Searching for EPOC-X headset (keys from KEYS.txt)..." -ForegroundColor Yellow
Write-Host "  Close the window or press Ctrl+C to stop." -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

& .\venv\Scripts\epoc-direct-record.exe | & .\venv\Scripts\epoc-headmap.exe @extraArgs
