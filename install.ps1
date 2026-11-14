param(
    [switch]$NoDesktopShortcut
)

Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating Python virtual environment in .\venv..." -ForegroundColor Cyan
    python -m venv (Join-Path $PSScriptRoot "venv")
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Write-Host "Installing EPOC-X Studio into current Python environment..." -ForegroundColor Cyan
& $py -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$shortcutArgs = @("-m", "epoc_studio", "--install-shortcut")
if ($NoDesktopShortcut) {
    $shortcutArgs += "--no-desktop"
}

& $py @shortcutArgs
exit $LASTEXITCODE
