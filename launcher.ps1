Set-Location $PSScriptRoot
if (Test-Path .\venv\Scripts\epoc-studio.exe) {
    & .\venv\Scripts\epoc-studio.exe @args
} else {
    & .\venv\Scripts\pythonw.exe -m epoc_studio @args
}
