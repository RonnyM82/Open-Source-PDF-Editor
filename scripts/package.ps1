# Build the standalone one-folder bundle with PyInstaller and zip it.
# Usage:  .\scripts\package.ps1
# Note: pdf_editor.spec is created at milestone M2; this script fails clearly
# until then.
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
$py = Join-Path $root ".venv\Scripts\python.exe"
$spec = Join-Path $root "pdf_editor.spec"

if (-not (Test-Path $spec)) {
    Write-Error "pdf_editor.spec not found - it is created at milestone M2."
    exit 1
}

# Stage the pinned Tesseract runtime (version-checked) before every build so
# the bundle can never carry a stale or drifted copy.
& (Join-Path $PSScriptRoot "stage_tesseract.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $py -m PyInstaller $spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$bundle = Join-Path $root "dist\pdf-editor"
$zip = Join-Path $root "dist\pdf-editor.zip"
if (Test-Path $bundle) {
    Compress-Archive -Path (Join-Path $bundle "*") -DestinationPath $zip -Force
    Write-Host "Packaged: $zip"
} else {
    Write-Error "Expected bundle not found at $bundle"
    exit 1
}
