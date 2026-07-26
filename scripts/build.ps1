# One-command build. Runs the CI gates, packages the frozen bundle, and smokes
# the packaged .exe. Optionally compiles the installer on top.
#
# Usage:
#   .\scripts\build.ps1                # gates + package + smoke the .exe
#   .\scripts\build.ps1 -Installer     # ...and compile the Inno Setup installer
#
#   -SkipTests   skip lint + pytest (package/smoke only — for a quick rebuild)
#   -SkipSmoke   skip the frozen-exe smokes
#   -InnoSetupPath <path>   passed through to build_installer.ps1
#
# The smokes exercise the PACKAGED exe (what unit tests cannot reach): the render
# + print pipeline, the bundled Tesseract runtime, and a TESSDATA_PREFIX hijack
# probe proving a user-global value cannot displace the bundled model data.
# tesseract exits 0 even when it produced nothing, so the OCR smokes assert real
# recognised words came back rather than trusting an exit code.
param(
    [switch]$Installer,
    [switch]$SkipTests,
    [switch]$SkipSmoke,
    [string]$InnoSetupPath
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "BUILD FAILED: $msg" -ForegroundColor Red; exit 1 }

# The bundle is built console=False (GUI subsystem), so `& $exe` does NOT wait
# and leaves $LASTEXITCODE meaningless. Start-Process -Wait is the only way to
# get a real exit code out of a smoke run. Env vars are inherited from here.
#
# stdout is redirected to a file rather than left to the console: a windowed exe
# has no console of its own, so its "OK" / "OCR OK: N words" verdict does not
# reliably reach the host terminal. Echoing the captured text is what makes a
# passing smoke VISIBLE - a silent pass and a skipped step look identical, and
# this project has been bitten by false greens twice.
# Each verdict is ALSO collected into $script:smokeLog and restated in the final
# summary: the live line sits above a wall of PyInstaller logging, which is a
# poor place to leave the only evidence that the bundle was actually verified.
$script:smokeLog = @()

function Invoke-Smoke($exePath, $label) {
    $outFile = Join-Path $env:TEMP "pdf-editor-smoke-stdout.txt"
    $proc = Start-Process -FilePath $exePath -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput $outFile
    $verdict = "(no output)"
    if (Test-Path $outFile) {
        $text = (Get-Content $outFile -Raw)
        if ($text -and $text.Trim()) { $verdict = $text.Trim() }
        Remove-Item $outFile -Force -ErrorAction SilentlyContinue
    }
    Write-Host "  $verdict"
    $script:smokeLog += "{0,-22} {1}" -f $label, $verdict
    return $proc.ExitCode
}

$started = Get-Date

# --- 0. The frozen build cannot overwrite a running exe (the false-green trap:
#        package fails on a locked DLL, and the smoke then passes from the STALE
#        exe). Refuse up front rather than shipping yesterday's build. ---
$running = @(Get-Process -Name "pdf-editor" -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Fail "pdf-editor.exe is running (PID $($running.Id -join ', ')). Close it first - PyInstaller cannot overwrite its locked DLLs."
}

# --- 1. CI gates: lint (check + format) then the whole pytest suite ---
if (-not $SkipTests) {
    Step "Lint (ruff check + format --check)"
    & (Join-Path $PSScriptRoot "lint.ps1")
    if ($LASTEXITCODE -ne 0) { Fail "ruff reported problems." }

    Step "Tests (pytest)"
    & (Join-Path $PSScriptRoot "test.ps1")
    if ($LASTEXITCODE -ne 0) { Fail "pytest reported failures." }
} else {
    Write-Host "Skipping lint + tests (-SkipTests)." -ForegroundColor Yellow
}

# --- 2. Package the onedir bundle + portable zip ---
Step "Package (PyInstaller onedir + portable zip)"
& (Join-Path $PSScriptRoot "package.ps1")
if ($LASTEXITCODE -ne 0) { Fail "packaging failed." }

$exe = Join-Path $root "dist\pdf-editor\pdf-editor.exe"
if (-not (Test-Path $exe)) { Fail "packaged exe not found at $exe" }

# --- 3. Smoke the PACKAGED exe (proves Qt, MuPDF, print support and the
#        bundled tesseract runtime all survived freezing) ---
if (-not $SkipSmoke) {
    $sample = Join-Path $root "samples\sample_quote.pdf"
    if (-not (Test-Path $sample)) {
        Write-Host "No samples\sample_quote.pdf - skipping smokes." -ForegroundColor Yellow
    } else {
        $tmp = Join-Path $env:TEMP "pdf-editor-build-smoke"
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null

        Step "Smoke: render + print pipeline"
        $env:PDF_EDITOR_SMOKE = $sample
        $env:PDF_EDITOR_PRINT_OUT = (Join-Path $tmp "print.pdf")
        try { $rc = Invoke-Smoke $exe "render + print" } finally {
            Remove-Item Env:\PDF_EDITOR_SMOKE, Env:\PDF_EDITOR_PRINT_OUT -ErrorAction SilentlyContinue
        }
        if ($rc -ne 0) { Fail "render/print smoke failed (exit $rc)." }
        if (-not (Test-Path (Join-Path $tmp "print.pdf"))) { Fail "print smoke produced no PDF." }

        Step "Smoke: OCR (bundled tesseract)"
        $env:PDF_EDITOR_OCR_SMOKE = $sample
        try { $rc = Invoke-Smoke $exe "OCR" } finally {
            Remove-Item Env:\PDF_EDITOR_OCR_SMOKE -ErrorAction SilentlyContinue
        }
        if ($rc -ne 0) { Fail "OCR smoke failed (exit $rc)." }

        # A user-global TESSDATA_PREFIX must NOT hijack the bundled runtime -
        # pdfcore.ocr pins it per call. Point it at garbage and re-run.
        Step "Smoke: TESSDATA_PREFIX hijack probe"
        $env:PDF_EDITOR_OCR_SMOKE = $sample
        $env:TESSDATA_PREFIX = (Join-Path $tmp "no-such-tessdata")
        try { $rc = Invoke-Smoke $exe "hijack probe" } finally {
            Remove-Item Env:\PDF_EDITOR_OCR_SMOKE, Env:\TESSDATA_PREFIX -ErrorAction SilentlyContinue
        }
        if ($rc -ne 0) { Fail "hijack probe failed - a global TESSDATA_PREFIX broke the bundled OCR." }

        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "Skipping frozen-exe smokes (-SkipSmoke)." -ForegroundColor Yellow
}

# --- 4. Installer (optional) ---
if ($Installer) {
    Step "Installer (Inno Setup)"
    if ($InnoSetupPath) {
        & (Join-Path $PSScriptRoot "build_installer.ps1") -InnoSetupPath $InnoSetupPath
    } else {
        & (Join-Path $PSScriptRoot "build_installer.ps1")
    }
    if ($LASTEXITCODE -ne 0) { Fail "installer build failed." }
}

# --- Summary ---
. "$PSScriptRoot\_version.ps1"
$version = Get-AppVersion
$elapsed = [int]((Get-Date) - $started).TotalSeconds
Write-Host "`n=== BUILD OK (v$version, ${elapsed}s) ===" -ForegroundColor Green
if ($smokeLog.Count -gt 0) {
    Write-Host "Frozen-exe smokes:"
    $smokeLog | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "Frozen-exe smokes: NOT RUN" -ForegroundColor Yellow
}
Write-Host "Artifacts:"
Get-ChildItem -Path (Join-Path $root "dist") -File |
    Where-Object { $_.Name -like "pdf-editor-*" } |
    ForEach-Object { Write-Host ("  {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) }
Write-Host ("  dist\pdf-editor\  (onedir bundle)")
exit 0
