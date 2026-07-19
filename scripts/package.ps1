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
if (-not (Test-Path $bundle)) {
    Write-Error "Expected bundle not found at $bundle"
    exit 1
}

# Portable distribution: the SAME onedir bundle plus a marker file that makes
# the app run in "leaves no trace" mode (writes its log to a data\ folder next
# to the exe instead of the user profile — see pdfapp/portable.py). The marker
# is dropped in only for the zip and removed afterwards, so the installer build
# (which copies dist\pdf-editor\*) never ships it. Zipping the FOLDER (not its
# contents) makes the archive extract into a clean pdf-editor\ directory.
. "$PSScriptRoot\_version.ps1"
$version = Get-AppVersion
$zip = Join-Path $root "dist\pdf-editor-portable-$version.zip"
$marker = Join-Path $bundle "pdf-editor.portable"
try {
    New-Item -ItemType File -Path $marker -Force | Out-Null
    Compress-Archive -Path $bundle -DestinationPath $zip -Force
    Write-Host "Packaged portable build: $zip"
} finally {
    # Keep dist\pdf-editor\ pristine for scripts\build_installer.ps1.
    Remove-Item -Force $marker -ErrorAction SilentlyContinue
}
