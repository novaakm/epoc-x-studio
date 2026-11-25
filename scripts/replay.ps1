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

$NotesFile = if ($InputFile.EndsWith(".csv")) { $InputFile.Substring(0, $InputFile.Length - 4) + ".notes.csv" } else { $InputFile + ".notes.csv" }

Write-Host "Replaying $InputFile in real-time..." -ForegroundColor Cyan
cmd /c "type `"$InputFile`"" | & .\venv\Scripts\presteeg-live-viewer.exe --group 5-18 --group 19-25 --width 5s --realtime-replay --notes $NotesFile
