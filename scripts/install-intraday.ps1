# Run from your existing Sthitaprajna folder to reuse its .env locally.
# Creates a separate installation; does not stop or change the options application.
$ErrorActionPreference = "Stop"
$sourceEnv = Join-Path (Get-Location) ".env"
$existingPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$knownFolder = Join-Path $env:USERPROFILE "Sthithaprajna-6cbedbf4\sthithaprajna-main"
if (-not (Test-Path $sourceEnv)) { $sourceEnv = Join-Path $knownFolder ".env" }
if (-not (Test-Path $existingPython)) { $existingPython = Join-Path $knownFolder ".venv\Scripts\python.exe" }

$destination = Join-Path $env:USERPROFILE "Sthitaprajna-Intraday500"
if (Test-Path $destination) { $destination += "-" + (Get-Date -Format "yyyyMMdd-HHmmss") }
$archive = Join-Path $env:TEMP ("intraday500-" + [guid]::NewGuid().ToString() + ".zip")
$staging = $archive + "-extracted"
Write-Host "Downloading the separate Intraday 500 version..."
try {
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/sparemai/sthithaprajna/archive/refs/heads/intraday-500.zip" -OutFile $archive
    Expand-Archive -Path $archive -DestinationPath $staging
    Move-Item -Path (Join-Path $staging "sthithaprajna-intraday-500") -Destination $destination
} finally {
    Remove-Item $archive -Force -ErrorAction SilentlyContinue
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path $sourceEnv) {
    Copy-Item $sourceEnv (Join-Path $destination ".env")
    Write-Host "Copied your existing .env locally. Credentials were not displayed or uploaded."
} else {
    Copy-Item (Join-Path $destination ".env.example") (Join-Path $destination ".env")
    Write-Host "Add DHAN_CLIENT_ID and DHAN_DATA_TOKEN in the .env file opened below. Save and close Notepad."
    Start-Process notepad.exe -ArgumentList ('"' + (Join-Path $destination ".env") + '"') -Wait
}
Set-Location $destination
if (Test-Path $existingPython) {
    & $existingPython -m venv .venv
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 -m venv .venv
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python -m venv .venv
} else {
    throw "Python 3.11 or newer is required. Install Python, then run this installer again."
}
if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment. Python 3.11 or newer is required." }
& .\.venv\Scripts\python.exe -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed; see the error above." }
Write-Host "Installed in $destination" -ForegroundColor Green
Write-Host "Open http://127.0.0.1:8792 after startup finishes. First startup loads stock history gradually."
Write-Host "Next time: Set-Location '$destination'; .\.venv\Scripts\sthitaprajna-intraday.exe --mode paper --port 8792"
& .\.venv\Scripts\sthitaprajna-intraday.exe --mode paper --port 8792
if ($LASTEXITCODE -ne 0) { throw "Intraday 500 stopped with an error. See the diagnostic above." }
