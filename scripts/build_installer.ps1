# Build the Windows installer (Inno Setup) from the already-built onedir bundle.
# Assumes scripts\package.ps1 has produced dist\pdf-editor\ first.
# Version is read from the single source of truth: pyproject.toml [project].version.
# Usage:  .\scripts\build_installer.ps1 [-InnoSetupPath <path to ISCC.exe>]
param(
    [string]$InnoSetupPath
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
$iss = Join-Path $root "installer\pdf-editor.iss"
$bundleExe = Join-Path $root "dist\pdf-editor\pdf-editor.exe"

if (-not (Test-Path $iss)) {
    Write-Error "Installer script not found: $iss"
    exit 1
}
if (-not (Test-Path $bundleExe)) {
    Write-Error "Onedir bundle not found ($bundleExe). Run scripts\package.ps1 first."
    exit 1
}

# --- Version: single source of truth = pyproject.toml [project].version ---
. "$PSScriptRoot\_version.ps1"
$version = Get-AppVersion
Write-Host "Version (from pyproject.toml): $version"

# --- Locate ISCC.exe (the Inno Setup compiler) ---
$iscc = $null
if ($InnoSetupPath) {
    if (Test-Path $InnoSetupPath) {
        $iscc = $InnoSetupPath
    } else {
        Write-Error "ISCC.exe not found at -InnoSetupPath: $InnoSetupPath"
        exit 1
    }
} else {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        # winget increasingly installs Inno Setup per-user, not to Program Files.
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { $iscc = $c; break }
    }
    if (-not $iscc) {
        try { $iscc = (Get-Command ISCC.exe -ErrorAction Stop).Source } catch { }
    }
}
if (-not $iscc) {
    Write-Error "Inno Setup compiler (ISCC.exe) not found. Install it with:  winget install -e --id JRSoftware.InnoSetup   (or pass -InnoSetupPath)."
    exit 1
}
Write-Host "Using ISCC: $iscc"

# --- Compile the installer (version flows in via the preprocessor define) ---
& $iscc "/DAppVersion=$version" $iss
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$setup = Join-Path $root "dist\pdf-editor-setup-$version.exe"
if (-not (Test-Path $setup)) {
    Write-Error "ISCC reported success but the setup exe was not found at $setup"
    exit 1
}
Write-Host "Installer built: $setup"
exit 0
