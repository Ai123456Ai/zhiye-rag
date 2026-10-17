$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Virtual environment missing. Run: python -m venv .venv'
}
& $projectPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000
