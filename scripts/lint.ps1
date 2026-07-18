# Lint + format-check with ruff. Non-zero exit if either fails.
# Usage:  .\scripts\lint.ps1
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
$py = Join-Path $root ".venv\Scripts\python.exe"

& $py -m ruff check src tests
$check = $LASTEXITCODE

& $py -m ruff format --check src tests
$fmt = $LASTEXITCODE

if ($check -ne 0 -or $fmt -ne 0) { exit 1 }
exit 0
