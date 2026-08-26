# Build a local release feed for testing the in-app updater, so the whole
# notify -> download -> install -> relaunch flow can be walked WITHOUT
# publishing a throwaway GitHub release and without touching the network.
#
# The feed is shaped exactly like GitHub's releases/latest response. The app
# reads it when PDF_EDITOR_UPDATE_FEED points at the file (pdfapp/updates.py).
#
# Usage:
#   .\scripts\make_update_feed.ps1                      # newest installer in dist\, claimed as 99.0.0
#   .\scripts\make_update_feed.ps1 -Version 0.12.0
#   .\scripts\make_update_feed.ps1 -Installer <path> -Out <path>
#
# The asset NAME must match pdf-editor-setup-<Version>.exe, because that is how
# the app finds its installer (by exact name, never by guessing which asset is
# an .exe). The download URL may point at any installer on disk: pointing a
# claimed 99.0.0 at the installer you just built is the cheapest way to exercise
# the real upgrade path from a single build. The app will genuinely download it,
# verify its length, close, install, and relaunch -- it just lands on the same
# version it started from, so the banner returns afterwards.
param(
    [string]$Version = "99.0.0",
    [string]$Installer,
    [string]$Out
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."

if (-not $Installer) {
    $candidate = Get-ChildItem (Join-Path $root "dist") -Filter "pdf-editor-setup-*.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $candidate) {
        Write-Error "No installer found in dist\. Run .\scripts\build.ps1 -Installer first, or pass -Installer."
        exit 1
    }
    $Installer = $candidate.FullName
}
if (-not (Test-Path $Installer)) { Write-Error "Installer not found: $Installer"; exit 1 }
if (-not $Out) { $Out = Join-Path $root "dist\update-feed.json" }

$file = Get-Item $Installer
# file:/// URL with backslashes flipped; urllib needs forward slashes.
$url = "file:///" + ($file.FullName -replace '\\', '/')

$feed = [ordered]@{
    tag_name   = "v$Version"
    name       = "PDF Editor v$Version"
    html_url   = "https://github.com/RonnyM82/Open-Source-PDF-Editor/releases/tag/v$Version"
    draft      = $false
    prerelease = $false
    assets     = @(
        [ordered]@{
            name                 = "pdf-editor-setup-$Version.exe"
            browser_download_url = $url
            size                 = [int]$file.Length   # the app verifies against this
            content_type         = "application/x-msdownload"
            state                = "uploaded"
        }
    )
}
$feed | ConvertTo-Json -Depth 5 | Set-Content -Path $Out -Encoding UTF8

Write-Host "Feed written: $Out"
Write-Host "  claims version : $Version"
Write-Host "  serves         : $($file.Name)  ($([math]::Round($file.Length / 1MB, 1)) MB)"
Write-Host ""
Write-Host "Point the app at it (this shell, then launch the INSTALLED app from it):"
Write-Host "  `$env:PDF_EDITOR_UPDATE_FEED = '$Out'"
Write-Host "  & `"`$env:LOCALAPPDATA\Programs\PDF Editor\pdf-editor.exe`""
Write-Host ""
Write-Host "The app checks at most once a day. To force a fresh check, delete the"
Write-Host "update_last_check key from:"
Write-Host "  `$env:LOCALAPPDATA\PDF Editor\settings.json"
exit 0
