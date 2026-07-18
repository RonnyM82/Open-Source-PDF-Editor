# Stage the pinned Tesseract OCR runtime into vendor\tesseract for packaging.
# Usage:  .\scripts\stage_tesseract.ps1   (package.ps1 runs it automatically)
#
# The file list is tesseract.exe's PE-import dependency closure, computed and
# isolation-verified (scrubbed PATH) at milestone O2 — see docs/PLAN.md.
# Everything else in the UB-Mannheim install is deliberately NOT bundled:
# training tools and their ICU/GLib/Pango/Cairo stack (~44 MB), osd.traineddata
# (orientation detection — unused at PSM 3), the ScrollView .jars and docs.
#
# vendor\ is gitignored: binaries are staged from the local install, never
# committed. Changing $pinned is a packaging-checkpoint event (CLAUDE.md
# rule 11): update the closure below if the new build's DLL set differs.
$ErrorActionPreference = "Stop"
$pinned = "5.4.0.20240606"
$src = "C:\Program Files\Tesseract-OCR"
$root = Resolve-Path "$PSScriptRoot\.."
$dest = Join-Path $root "vendor\tesseract"

if (-not (Test-Path "$src\tesseract.exe")) {
    Write-Error ("Tesseract not installed at '$src'. Dev setup: " +
        "winget install -e --id UB-Mannheim.TesseractOCR")
    exit 1
}
# Collect ALL output before taking the first line: piping a native command
# straight into Select-Object -First kills it on pipeline-stop and leaves
# $LASTEXITCODE null (and `exit $null` is exit 0 — a silent false green).
$banner = ([string[]](& "$src\tesseract.exe" --version 2>&1))[0]
if ($banner -notmatch [regex]::Escape($pinned)) {
    Write-Error ("Installed tesseract is '$banner' but the pinned bundle version is " +
        "$pinned. Install the pinned version, or update the pin AND re-run the " +
        "packaging checkpoint (CLAUDE.md rule 11).")
    exit 1
}

# tesseract.exe's verified DLL closure (26 files; system DLLs excluded).
$dlls = @(
    "libarchive-13.dll", "libb2-1.dll", "libbz2-1.dll", "libcrypto-3-x64.dll",
    "libdeflate.dll", "libexpat-1.dll", "libgcc_s_seh-1.dll", "libgif-7.dll",
    "libiconv-2.dll", "libjbig-0.dll", "libjpeg-8.dll", "libleptonica-6.dll",
    "libLerc.dll", "liblz4.dll", "liblzma-5.dll", "libopenjp2-7.dll",
    "libpng16-16.dll", "libsharpyuv-0.dll", "libstdc++-6.dll",
    "libtesseract-5.dll", "libtiff-6.dll", "libwebp-7.dll", "libwebpmux-3.dll",
    "libwinpthread-1.dll", "libzstd.dll", "zlib1.dll"
)

# Clean slate so a previous pin's files can never linger in the bundle.
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
New-Item -ItemType Directory -Force "$dest\tessdata\configs" | Out-Null
New-Item -ItemType Directory -Force "$dest\tessdata\tessconfigs" | Out-Null

Copy-Item -Force "$src\tesseract.exe" $dest
foreach ($d in $dlls) { Copy-Item -Force "$src\$d" $dest }
# eng.traineddata (tessdata_fast variant) + the CLI config files: pytesseract's
# TSV path uses -c flags, but other paths (and bare-CLI use) resolve config
# names from tessdata\configs — bundle them (a few KB) so the whole class of
# "read_params_file: Can't open ..." failures cannot occur.
Copy-Item -Force "$src\tessdata\eng.traineddata" "$dest\tessdata"
Copy-Item -Force "$src\tessdata\configs\*" "$dest\tessdata\configs"
Copy-Item -Force "$src\tessdata\tessconfigs\*" "$dest\tessdata\tessconfigs"

$size = (Get-ChildItem -Recurse -File $dest | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Staged Tesseract {0} -> {1} ({2:N1} MB)" -f $pinned, $dest, $size)
# Explicit success code: callers check $LASTEXITCODE, which would otherwise be
# whatever (or null) the last native command left behind.
exit 0
