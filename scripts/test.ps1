# Run the test suite. Extra args are passed through to pytest.
# Usage:  .\scripts\test.ps1  [pytest args...]
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pytest @args
exit $LASTEXITCODE
