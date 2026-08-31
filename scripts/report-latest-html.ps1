# ASCII-only by contract: PS 5.1 decodes a no-BOM .ps1 as cp1252.
#
# Render the item-level HTML report for a stored run and open it.
#
# dev-observatory launch verbs are STATIC strings, so they cannot name a run id. This wrapper
# resolves one instead: -RunId if given, else the newest run directory under <Data>/runs.
# Every number on the page comes from that stored run -- nothing is re-measured and no model is
# called, so this is safe to run at any time.
[CmdletBinding()]
param(
    [string]$RunId = "",
    [string]$Data = "data",
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $projectRoot

if ([string]::IsNullOrWhiteSpace($RunId)) {
    $runsDir = Join-Path $projectRoot (Join-Path $Data "runs")
    if (-not (Test-Path -LiteralPath $runsDir)) {
        throw "no run store at '$runsDir' - run `uv run mt run ...` first"
    }
    $newest = Get-ChildItem -LiteralPath $runsDir -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "rows.jsonl") } |
        Sort-Object -Property Name -Descending |
        Select-Object -First 1
    if ($null -eq $newest) {
        throw "no completed runs under '$runsDir' - run `uv run mt run ...` first"
    }
    # Run ids are timestamp-prefixed (run_<UTC>_<suffix>), so lexical sort IS newest-first.
    $RunId = $newest.Name
}

Write-Host "Rendering item-level report for $RunId ..."
# `mt report` writes its "wrote <path>" confirmation to STDERR by design. Under
# $ErrorActionPreference = "Stop", PowerShell wraps a native command's stderr in a
# NativeCommandError and throws -- turning a SUCCESSFUL render into a failure, which would make
# this observatory verb show red on every green run. Drop to Continue across the native call and
# branch on the real signal, the exit code, instead.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & uv run mt report $RunId --html --out $Data
    $reportExit = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousPreference
}
if ($reportExit -ne 0) {
    throw "mt report failed with exit $reportExit"
}

$page = Join-Path $projectRoot (Join-Path $Data (Join-Path "reports" "$RunId.html"))
if (-not (Test-Path -LiteralPath $page)) {
    throw "mt report reported success but '$page' is missing"
}
Write-Host "Report: $page"
if (-not $NoOpen) {
    Invoke-Item -LiteralPath $page
}
