Set-Location $PSScriptRoot
if (Test-Path .\venv\Scripts\presteeg.exe) {
    & .\venv\Scripts\presteeg.exe @args
} else {
    & .\venv\Scripts\pythonw.exe -m presteeg @args
}
