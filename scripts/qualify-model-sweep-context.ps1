#requires -Version 5.1
<#
.SYNOPSIS
    Autonomous hostile-context qualification wrapper for the sealed Instrument A execution
    contract (first-measurement-validity plan, Steps 57/58).

.DESCRIPTION
    Plants a unique repository, environment, customization, and simulated prior-session sentinel; writes a concrete
    qualification INDEX before AND after the model calls; embeds the EXACT Step 58 preregistration
    sentence before call 1; sweeps the three-alias hostile-context canary suite through the real
    `mt run` production entry point; then fails closed unless every arm returned without reproducing
    any planted sentinel and every arm recorded a concrete provider-returned identity. The session
    marker is a simulated prior-session line in CLAUDE.md plus a process variable; this wrapper does
    not seed a real Claude conversation.

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
    $script:FailureReason = $Message
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

function Invoke-EvidenceCheck([string]$IndexPath, [string]$OutPath, [bool]$Stored) {
    # The Python verifier uses the production run-store, suite, and canonical receipt readers.
    # A second PowerShell JSON/hash implementation could drift from those contracts.
    $projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    $args = @("run", "--project", $projectRoot, "python", "-m", "measure_twice.context_qualification", "--index", $IndexPath, "--out", $OutPath)
    if ($Stored) { $args += "--verify-only" }
    $output = & uv @args
    if ($LASTEXITCODE -ne 0) {
        $detail = "stored run evidence did not verify"
        try {
            $failure = $output | ConvertFrom-Json
            if ($failure.error) { $detail = "stored run evidence: $($failure.error)" }
        } catch {}
        Fail-Closed $detail
    }
    if (@($output).Count -ne 1) { Fail-Closed "evidence verifier returned unexpected output" }
    return ($output | ConvertFrom-Json)
}

$modelList = @($Models.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
if ($Models -ne "haiku,sonnet,opus" -or $Samples -ne 1) {
    Fail-Closed "qualification requires -Models haiku,sonnet,opus and -Samples 1"
}
$outFull = [System.IO.Path]::GetFullPath($Out)
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
    $eval = Invoke-EvidenceCheck $indexPath $outFull $true
    Write-Line "verify=PASS passed=$($eval.passed) total=$($eval.total) receipt=$($eval.receipt.receipt_sha256)"
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
$Profile = [System.IO.Path]::GetFullPath($Profile)
$Suite = [System.IO.Path]::GetFullPath($Suite)

# One qualification attempt owns one output directory. A second run must use a fresh -Out path;
# otherwise a failed 3/3 result could be replaced with a favorable rerun.
if (Test-Path -LiteralPath $outFull) {
    if (@(Get-ChildItem -LiteralPath $outFull -Force).Count -gt 0) {
        Fail-Closed "output directory already has qualification evidence; choose a fresh -Out"
    }
} else {
    New-Item -ItemType Directory -Path $outFull | Out-Null
}

$repoRoot = (Get-Location).Path
$stamp = [guid]::NewGuid().ToString("N")
$canaryCwd = Join-Path $repoRoot ".mt-context-canary-$stamp"
$sentinels = [ordered]@{
    repository    = "MT-CANARY-REPOSITORY-$stamp"
    environment   = "MT-CANARY-ENVIRONMENT-$stamp"
    customization = "MT-CANARY-CUSTOMIZATION-$stamp"
    session       = "MT-CANARY-SESSION-$stamp"
}
$repoFile = Join-Path $canaryCwd ".mt-context-canary-repository-$stamp.txt"
$custFile = Join-Path $canaryCwd "CLAUDE.md"
$plantingDir = Join-Path $outFull "planting"
$snapshotRepo = Join-Path $plantingDir "repository.txt"
$snapshotCust = Join-Path $plantingDir "CLAUDE.md"
$snapshotEnv = Join-Path $plantingDir "environment.json"
$script:FailureReason = "qualification ended before PASS"

$plantedEnvSaved = [Environment]::GetEnvironmentVariable($EnvSentinelName, "Process")
$plantedSessionSaved = [Environment]::GetEnvironmentVariable($SessionSentinelName, "Process")

try {
    # Plant a disposable cwd under the caller's repo. CLAUDE.md is an actual project customization
    # filename; its fake prior-session line also baits resumed context if customizations are loaded.
    # Both environment markers are inherited by the wrapper, then blocked by the Step 56 allowlist.
    New-Item -ItemType Directory -Path $canaryCwd | Out-Null
    [System.IO.File]::WriteAllText($repoFile, $sentinels.repository)
    [System.IO.File]::WriteAllText($custFile, "Project instruction: $($sentinels.customization)`nPrior session memory: $($sentinels.session)`n")
    Set-Item -Path "Env:$EnvSentinelName" -Value $sentinels.environment
    Set-Item -Path "Env:$SessionSentinelName" -Value $sentinels.session
    New-Item -ItemType Directory -Path $plantingDir | Out-Null
    Copy-Item -LiteralPath $repoFile -Destination $snapshotRepo
    Copy-Item -LiteralPath $custFile -Destination $snapshotCust
    Write-JsonFile $snapshotEnv ([ordered]@{
        environment = $sentinels.environment
        session = $sentinels.session
    })

    # INDEX BEFORE the calls: the exact preregistration, the sentinels, and IN_PROGRESS status.
    $beforeIndex = [ordered]@{
        schema_version   = $IndexSchemaVersion
        status           = "IN_PROGRESS"
        preregistration  = $Preregister
        suite            = $Suite
        profile          = $Profile
        models           = $modelList
        samples          = $Samples
        source_hashes    = [ordered]@{
            source_suite_sha256   = (Get-FileHash -LiteralPath $Suite -Algorithm SHA256).Hash.ToLowerInvariant()
            source_profile_sha256 = (Get-FileHash -LiteralPath $Profile -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        planting_hashes  = [ordered]@{
            repository_sha256 = (Get-FileHash -LiteralPath $snapshotRepo -Algorithm SHA256).Hash.ToLowerInvariant()
            customization_sha256 = (Get-FileHash -LiteralPath $snapshotCust -Algorithm SHA256).Hash.ToLowerInvariant()
            environment_sha256 = (Get-FileHash -LiteralPath $snapshotEnv -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        sentinels        = $sentinels
        started_utc      = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    Write-JsonFile $indexPath $beforeIndex
    Write-Line "planted 4 sentinels; wrote pre-call index to $indexPath"

    # Sweep from the hostile cwd so any regression that re-enables project customization or repo
    # access encounters the planted markers. The absolute suite/profile/out paths remain fixed.
    Set-Location -LiteralPath $canaryCwd
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
    $runId = $null
    foreach ($line in $runOutput) {
        $m = [regex]::Match([string]$line, "(run_\S+?):")
        if ($m.Success) { $runId = $m.Groups[1].Value; break }
    }
    if ($null -ne $runId) {
        $beforeIndex.run_id = $runId
        Write-JsonFile $indexPath $beforeIndex
    }
    if ($runExit -ne 0) { Fail-Closed "mt run exited with code $runExit" }
    if ($null -eq $runId) { Fail-Closed "could not parse a run id from mt run output" }
    if ((Get-FileHash -LiteralPath $repoFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $beforeIndex.planting_hashes.repository_sha256 -or
        (Get-FileHash -LiteralPath $custFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $beforeIndex.planting_hashes.customization_sha256) {
        Fail-Closed "planted cwd evidence changed during the model sweep"
    }
    $eval = Invoke-EvidenceCheck $indexPath $outFull $false
    $status = "PASS"

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
        samples          = $Samples
        sentinels        = $sentinels
        run_id           = $runId
        passed           = $eval.passed
        total            = $eval.total
        arms             = $eval.arms
        receipt          = $eval.receipt
        evidence_hashes  = $eval.evidence_hashes
        source_hashes    = $eval.source_hashes
        planting_hashes  = $eval.planting_hashes
        reason           = ""
        started_utc      = $beforeIndex.started_utc
        finished_utc     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    Write-JsonFile $indexPath $afterIndex
    Write-Line "wrote post-call index to $indexPath"
    Write-Line "qualification=$status passed=$($eval.passed) total=$($eval.total)"
    exit 0
}
catch {
    $script:FailureReason = $_.Exception.Message
    [Console]::Error.WriteLine("qualify-model-sweep-context: $script:FailureReason")
    exit 1
}
finally {
    Set-Location -LiteralPath $repoRoot
    if (Test-Path -LiteralPath $indexPath) {
        $lastIndex = Read-JsonFile $indexPath
        if ($lastIndex.status -eq "IN_PROGRESS") {
            $lastIndex.status = "FAIL"
            $lastIndex | Add-Member -NotePropertyName qualification -NotePropertyValue "FAIL" -Force
            $lastIndex | Add-Member -NotePropertyName reason -NotePropertyValue $script:FailureReason -Force
            $lastIndex | Add-Member -NotePropertyName finished_utc -NotePropertyValue ((Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")) -Force
            Write-JsonFile $indexPath $lastIndex
        }
    }
    if (Test-Path -LiteralPath $repoFile) { Remove-Item -LiteralPath $repoFile -Force }
    if (Test-Path -LiteralPath $custFile) { Remove-Item -LiteralPath $custFile -Force }
    if (Test-Path -LiteralPath $canaryCwd) { Remove-Item -LiteralPath $canaryCwd -Force }
    if ($null -eq $plantedEnvSaved) { Remove-Item -Path "Env:$EnvSentinelName" -ErrorAction SilentlyContinue }
    else { Set-Item -Path "Env:$EnvSentinelName" -Value $plantedEnvSaved }
    if ($null -eq $plantedSessionSaved) { Remove-Item -Path "Env:$SessionSentinelName" -ErrorAction SilentlyContinue }
    else { Set-Item -Path "Env:$SessionSentinelName" -Value $plantedSessionSaved }
}
