# Shared helper: read the single source of truth for the app version.
# Dot-source this from a build script:  . "$PSScriptRoot\_version.ps1"
# then call Get-AppVersion.
#
# The version lives ONLY in pyproject.toml [project].version. The anchored
# `^version = "x.y.z"` pattern matches the top-level key at column 0; dependency
# specifiers live inside arrays and won't match.

function Get-AppVersion {
    $root = Resolve-Path "$PSScriptRoot\.."
    $pyproject = Join-Path $root "pyproject.toml"
    $verMatch = Select-String -Path $pyproject -Pattern '^version\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $verMatch) {
        throw "Could not read [project].version from $pyproject"
    }
    return $verMatch.Matches[0].Groups[1].Value
}
