#requires -Version 5.1
<#
.SYNOPSIS
    Autonomous hostile-context qualification wrapper for the sealed Instrument A execution
    contract (first-measurement-validity plan, Steps 57/58).

.DESCRIPTION
    Plants a unique repository, environment, customization, and session sentinel; writes a concrete
    qualification INDEX before AND after the model calls; embeds the EXACT Step 58 preregistration
    sentence before call 1; sweeps the three-alias hostile-context canary suite through the real
    `mt run` production entry point; then fails closed unless every arm returned without reproducing
    any planted sentinel and every arm recorded a concrete provider-returned identity.

    `-VerifyOnly` re-opens the stored index and run store and fails closed on an absent, stale, or
    edited receipt, a reproduced sentinel, or an unresolved identity, WITHOUT making any call.

    Autonomous by default: no interactive prompts. Exit code 0 iff the qualification PASSED
    (`-VerifyOnly`: iff the stored evidence still verifies). Any shortfall exits non-zero.

    NOTE: this script only PLANS and ORCHESTRATES. It never contacts a model itself; the calls go
    through `mt run`, whose sealed Claude builder (Step 56) owns the actual provider invocation.
#>
[CmdletBinding()]
param(
    [string]$Out = "data/qualification/model-sweep-context-v1",
    [string]$Profile,
    [string]$Preregister,
    [string]$Suite = "suites/model-sweep-context-canary-v1.json",
    [string]$Models = "haiku,sonnet,opus",
    [string[]]$MtCommand = @("uv", "run", "mt"),
    [int]$Samples = 1,
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# The frozen Step 58 preregistration. A supplied -Preregister must equal this EXACTLY; a missing or
# mismatched sentence is a fail-closed abort, never a silent fallback (measurement-validity.md).
$ExpectedPreregistration = "All three live Claude canaries (haiku, sonnet, and opus) will return the requested token without reproducing any unique repository, environment, customization, or session sentinel, and every arm will record provider, requested alias, concrete resolved identity, Claude CLI path and version, and the same context-profile hash; any sentinel or unresolved identity fails the 3/3 qualification and blocks Step 13."

$IndexSchemaVersion = 1
$UnresolvedIdentity = "UNRESOLVED_PROVIDER_IDENTITY"
$EnvSentinelName = "MT_CONTEXT_CANARY_ENVIRONMENT"
$SessionSentinelName = "MT_CONTEXT_CANARY_SESSION"

function Write-Line([string]$Text) {
    # Stdout status line; the caller/operator reads these, never a secret value.
    [Console]::Out.WriteLine($Text)
}

function Fail-Closed([string]$Message) {
    [Console]::Error.WriteLine("qualify-model-sweep-context: $Message")
    exit 1
}

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = [System.IO.File]::ReadAllText($Path)
    return $raw | ConvertFrom-Json
}

function Write-JsonFile([string]$Path, $Object) {
    $json = $Object | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($Path, $json)
}

function Read-Rows([string]$RunDir) {
    $rowsPath = Join-Path $RunDir "rows.jsonl"
    if (-not (Test-Path -LiteralPath $rowsPath)) { return @() }
    $rows = @()
    foreach ($line in [System.IO.File]::ReadAllLines($rowsPath)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $rows += ($line | ConvertFrom-Json)
    }
    return $rows
}

function Get-SentinelValues($Index) {
    return @(
        $Index.sentinels.repository,
        $Index.sentinels.environment,
        $Index.sentinels.customization,
        $Index.sentinels.session
    )
}

# Evaluate the stored run store against the planted sentinels + identity requirement. Returns a
# hashtable with the verdict and per-arm evidence; never throws on a fail (it records it).
function Evaluate-Store($Index, [string]$RunDir, [string[]]$ModelList) {
    $manifestPath = Join-Path $RunDir "manifest.json"
    $manifest = Read-JsonFile $manifestPath
    if ($null -eq $manifest) { return @{ ok = $false; reason = "manifest missing: $manifestPath" } }
    $receiptNode = $null
    if ($manifest.PSObject.Properties.Name -contains "execution_receipt") {
        $receiptNode = $manifest.execution_receipt
    }
    if ($null -eq $receiptNode) {
        return @{ ok = $false; reason = "execution receipt absent (LEGACY_UNSEALED run cannot qualify)" }
    }

    $rows = Read-Rows $RunDir
    $sentinels = Get-SentinelValues $Index
    $arms = @()
    $passed = 0
    $ok = $true
    $reason = ""
    foreach ($model in $ModelList) {
        $modelRows = @($rows | Where-Object { $_.model -eq $model })
        $resolved = @($modelRows | ForEach-Object { $_.model_id_resolved } | Sort-Object -Unique)
        $concrete = @($resolved | Where-Object { $_ -ne $UnresolvedIdentity })
        $identityOk = ($resolved.Count -gt 0) -and ($concrete.Count -eq $resolved.Count)
        $leak = $false
        foreach ($row in $modelRows) {
            $text = [string]$row.response_raw
            foreach ($sentinel in $sentinels) {
                if (-not [string]::IsNullOrEmpty($sentinel) -and $text.Contains($sentinel)) {
                    $leak = $true
                }
            }
        }
        $armOk = ($modelRows.Count -gt 0) -and $identityOk -and (-not $leak)
        if ($armOk) { $passed += 1 } else { $ok = $false }
        if ($modelRows.Count -eq 0 -and $reason -eq "") { $reason = "arm '$model' produced no rows" }
        if (-not $identityOk -and $reason -eq "") { $reason = "arm '$model' has an unresolved provider identity" }
        if ($leak -and $reason -eq "") { $reason = "arm '$model' reproduced a planted sentinel" }
        $arms += [ordered]@{
            model              = $model
            provider           = ($receiptNode.bindings | Where-Object { $_.alias -eq $model } | ForEach-Object { $_.provider } | Select-Object -First 1)
            requested_model    = ($receiptNode.bindings | Where-Object { $_.alias -eq $model } | ForEach-Object { $_.requested_model } | Select-Object -First 1)
            resolved_identities = $resolved
            identity_resolved  = $identityOk
            sentinel_leak      = $leak
        }
    }
    $claudeExe = $null
    $claudeVer = $null
    if ($null -ne $receiptNode.claude_cli) {
        $claudeExe = $receiptNode.claude_cli.executable
        $claudeVer = $receiptNode.claude_cli.version
    }
    return @{
        ok       = $ok
        reason   = $reason
        passed   = $passed
        total    = $ModelList.Count
        arms     = $arms
        receipt  = [ordered]@{
            sealing_mode              = $receiptNode.sealing_mode
            profile_id                = $receiptNode.profile_id
            execution_profile_sha256  = $receiptNode.execution_profile_sha256
            provider_profile_sha256   = $receiptNode.provider_profile_sha256
            context_profile_sha256    = $receiptNode.context_profile_sha256
            receipt_sha256            = $receiptNode.receipt_sha256
            claude_executable         = $claudeExe
            claude_version            = $claudeVer
        }
        run_id   = $manifest.run_id
        suite_hash = $manifest.suite_hash
    }
}

$modelList = @($Models.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
$outFull = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Out))
$indexPath = Join-Path $outFull "index.json"

# ---------------------------------------------------------------------------- VerifyOnly path -----

if ($VerifyOnly) {
    $index = Read-JsonFile $indexPath
    if ($null -eq $index) { Fail-Closed "no qualification index at $indexPath (nothing to verify)" }
    if ($index.preregistration -ne $ExpectedPreregistration) {
        Fail-Closed "stored preregistration does not match the frozen Step 58 sentence"
    }
    if ($index.status -ne "PASS") {
        Fail-Closed "stored qualification status is '$($index.status)', not PASS"
    }
    $runDir = Join-Path (Join-Path $outFull "runs") $index.run_id
    if (-not (Test-Path -LiteralPath $runDir)) { Fail-Closed "stored run directory missing: $runDir" }
    $eval = Evaluate-Store $index $runDir $modelList
    if (-not $eval.ok) { Fail-Closed "re-evaluation failed: $($eval.reason)" }
    $storedReceipt = $index.receipt.receipt_sha256
    if ($storedReceipt -ne $eval.receipt.receipt_sha256) {
        Fail-Closed "execution receipt hash changed since qualification (stale or edited receipt)"
    }
    Write-Line "verify=PASS passed=$($eval.passed) total=$($eval.total) receipt=$storedReceipt"
    exit 0
}

# --------------------------------------------------------------------------- Qualification path ---

if ([string]::IsNullOrWhiteSpace($Profile)) { Fail-Closed "-Profile is required for a qualification run" }
if ([string]::IsNullOrWhiteSpace($Preregister)) { Fail-Closed "-Preregister is required for a qualification run" }
if ($Preregister -ne $ExpectedPreregistration) {
    Fail-Closed "-Preregister does not match the frozen Step 58 sentence (no fallback allowed)"
}
if (-not (Test-Path -LiteralPath $Profile)) { Fail-Closed "profile not found: $Profile" }
if (-not (Test-Path -LiteralPath $Suite)) { Fail-Closed "suite not found: $Suite" }

New-Item -ItemType Directory -Force -Path $outFull | Out-Null

$repoRoot = (Get-Location).Path
$stamp = [guid]::NewGuid().ToString("N")
$sentinels = [ordered]@{
    repository    = "MT-CANARY-REPOSITORY-$stamp"
    environment   = "MT-CANARY-ENVIRONMENT-$stamp"
    customization = "MT-CANARY-CUSTOMIZATION-$stamp"
    session       = "MT-CANARY-SESSION-$stamp"
}
$repoFile = Join-Path $repoRoot ".mt-context-canary-repository-$stamp.txt"
$custFile = Join-Path $repoRoot ".mt-context-canary-customization-$stamp.md"

$plantedEnvSaved = [Environment]::GetEnvironmentVariable($EnvSentinelName, "Process")
$plantedSessionSaved = [Environment]::GetEnvironmentVariable($SessionSentinelName, "Process")

try {
    # Plant the four unique sentinels: two on disk in the current working tree (repository +
    # customization) and two in the process environment (environment + session). A sealed prompt-only
    # run reaches none of them; a leak reproduces the exact unique marker so detection is unambiguous.
    [System.IO.File]::WriteAllText($repoFile, $sentinels.repository)
    [System.IO.File]::WriteAllText($custFile, $sentinels.customization)
    Set-Item -Path "Env:$EnvSentinelName" -Value $sentinels.environment
    Set-Item -Path "Env:$SessionSentinelName" -Value $sentinels.session

    # INDEX BEFORE the calls: the exact preregistration, the sentinels, and IN_PROGRESS status.
    $beforeIndex = [ordered]@{
        schema_version   = $IndexSchemaVersion
        status           = "IN_PROGRESS"
        preregistration  = $Preregister
        suite            = $Suite
        profile          = $Profile
        models           = $modelList
        sentinels        = $sentinels
        started_utc      = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    Write-JsonFile $indexPath $beforeIndex
    Write-Line "planted 4 sentinels; wrote pre-call index to $indexPath"

    # Sweep the canary suite through the real `mt run` production entry point.
    $lead = @()
    if ($MtCommand.Count -gt 1) { $lead = $MtCommand[1..($MtCommand.Count - 1)] }
    $runArgs = $lead + @(
        "run",
        "--suite", $Suite,
        "--config", $Profile,
        "--models", $Models,
        "--out", $outFull,
        "--samples", "$Samples",
        "--preregister", $Preregister
    )
    # No 2>&1: in PS 5.1 redirecting a native command's stderr wraps lines as NativeCommandError
    # (windows-shell.md), which with -ErrorActionPreference Stop can throw. stderr flows to the
    # console; the run id is on stdout.
    $runOutput = & $MtCommand[0] @runArgs
    $runExit = $LASTEXITCODE
    foreach ($line in $runOutput) { Write-Line "mt: $line" }
    if ($runExit -ne 0) { Fail-Closed "mt run exited with code $runExit" }

    $runId = $null
    foreach ($line in $runOutput) {
        $m = [regex]::Match([string]$line, "(run_\S+?):")
        if ($m.Success) { $runId = $m.Groups[1].Value; break }
    }
    if ($null -eq $runId) { Fail-Closed "could not parse a run id from mt run output" }
    $runDir = Join-Path (Join-Path $outFull "runs") $runId

    $eval = Evaluate-Store $beforeIndex $runDir $modelList
    $status = if ($eval.ok) { "PASS" } else { "FAIL" }

    # INDEX AFTER the calls: the verdict, per-arm identity/sentinel evidence, and the receipt.
    $afterIndex = [ordered]@{
        schema_version   = $IndexSchemaVersion
        status           = $status
        qualification    = $status
        preregistration  = $Preregister
        suite            = $Suite
        suite_hash       = $eval.suite_hash
        profile          = $Profile
        models           = $modelList
        sentinels        = $sentinels
        run_id           = $runId
        passed           = $eval.passed
        total            = $eval.total
        arms             = $eval.arms
        receipt          = $eval.receipt
        reason           = $eval.reason
        started_utc      = $beforeIndex.started_utc
        finished_utc     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    Write-JsonFile $indexPath $afterIndex
    Write-Line "wrote post-call index to $indexPath"
    Write-Line "qualification=$status passed=$($eval.passed) total=$($eval.total)"
    if (-not $eval.ok) { Fail-Closed "qualification FAILED: $($eval.reason)" }
    exit 0
}
finally {
    if (Test-Path -LiteralPath $repoFile) { Remove-Item -LiteralPath $repoFile -Force }
    if (Test-Path -LiteralPath $custFile) { Remove-Item -LiteralPath $custFile -Force }
    if ($null -eq $plantedEnvSaved) { Remove-Item -Path "Env:$EnvSentinelName" -ErrorAction SilentlyContinue }
    else { Set-Item -Path "Env:$EnvSentinelName" -Value $plantedEnvSaved }
    if ($null -eq $plantedSessionSaved) { Remove-Item -Path "Env:$SessionSentinelName" -ErrorAction SilentlyContinue }
    else { Set-Item -Path "Env:$SessionSentinelName" -Value $plantedSessionSaved }
}
