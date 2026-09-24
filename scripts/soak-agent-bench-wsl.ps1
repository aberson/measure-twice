[CmdletBinding()]
param(
    [string]$Distribution = "",
    [int]$Repetitions = 8,
    [string]$Out = "",
    [string]$Preregister = "",
    [switch]$VerifyOnly,
    [string]$GateScript = ""
)

# Autonomous soak wrapper for the Windows control-plane WSL-ext4 containment gate
# (scripts/test-agent-bench-wsl.ps1). It runs the gate N times, captures every per-run log,
# asserts ONE staged-tree hash across runs, rejects ANY nonzero gate exit, writes the
# preregistration to the evidence header BEFORE run 1, and prints the pass rate. -VerifyOnly is a
# fail-closed receipt check over an already-produced evidence directory. ASCII-only by contract
# (see .claude/rules/windows-shell.md): PowerShell 5.1 decodes a no-BOM .ps1 as cp1252.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$HASH_PATTERN = 'staged-tree-sha256:\s*([0-9a-f]{64})'
$SKIP_PATTERN = '(?m)^selected-skips:\s*(\d+)\s*$'
$STEP63_PREREG = "The reviewed containment repair will pass 8/8 independent WSL-ext4 gate invocations with zero selected skips and no live-identity or retained-FD escape; any lower pass rate returns the work to Step 62 and blocks Step 27."
$PRODUCER_VERSION = "step62-soak-v3"

function Get-StagedTreeHash {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    $first = [regex]::Match($Text, $HASH_PATTERN)
    if ($first.Success -and -not $first.NextMatch().Success) {
        return $first.Groups[1].Value
    }
    return ""
}

function Get-SelectedSkipCount {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    $first = [regex]::Match($Text, $SKIP_PATTERN)
    if ($first.Success -and -not $first.NextMatch().Success) {
        return $first.Groups[1].Value
    }
    return ""
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-SourceTreeSha256 {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $relativeRoots = @("measure_twice/agent_bench", "tests/agent_bench")
    $records = @()
    foreach ($relativeRoot in $relativeRoots) {
        $directory = Join-Path $ProjectRoot $relativeRoot
        if (-not (Test-Path -LiteralPath $directory)) {
            throw "containment source directory is absent: $directory"
        }
        foreach ($file in @(Get-ChildItem -LiteralPath $directory -Recurse -File)) {
            if ($file.Extension -notin @(".py", ".json")) { continue }
            $relative = $file.FullName.Substring($ProjectRoot.Length + 1).Replace('\', '/')
            $records += "$relative`:$(Get-FileSha256 -Path $file.FullName)"
        }
    }
    if ($records.Count -eq 0) { throw "containment source tree is empty" }
    $body = (@($records | Sort-Object) -join "`n") + "`n"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
        return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Read-HeaderValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $match = [regex]::Match($line, ('^' + [regex]::Escape($Key) + ':\s*(.*)$'))
        if ($match.Success) {
            return $match.Groups[1].Value
        }
    }
    return $null
}

function Get-RunLogPaths {
    param([Parameter(Mandatory = $true)][string]$Directory)

    return @(
        Get-ChildItem -LiteralPath $Directory -Filter 'run-*.log' -File |
            Sort-Object -Property Name |
            ForEach-Object { $_.FullName }
    )
}

function New-RunLogPath {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][int]$Index
    )

    $name = "run-{0:D2}.log" -f $Index
    return (Join-Path $Directory $name)
}

function Format-Rate {
    param(
        [Parameter(Mandatory = $true)][int]$PassCount,
        [Parameter(Mandatory = $true)][int]$Total
    )

    $pct = 0.0
    if ($Total -gt 0) {
        $pct = 100.0 * $PassCount / $Total
    }
    $pctText = [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, "{0:0.0}", $pct)
    return "containment_gate_rate=$PassCount/$Total ($pctText%)"
}

$scriptRoot = Split-Path -Parent $PSCommandPath
$production = [string]::IsNullOrWhiteSpace($GateScript)
if ($production) {
    $GateScript = Join-Path $scriptRoot "test-agent-bench-wsl.ps1"
    if ([string]::IsNullOrWhiteSpace($Preregister)) {
        if (-not $VerifyOnly) {
            throw "-Preregister is required for a soak run (the claim is written before run 1)"
        }
    }
    elseif ($Preregister -ne $STEP63_PREREG) {
        throw "production preregistration does not match the frozen Step 63 claim"
    }
}
$producerMode = if ($production) { "production" } else { "fixture" }
$GateScript = [System.IO.Path]::GetFullPath($GateScript)
$projectRoot = Split-Path -Parent $scriptRoot
if (-not (Test-Path -LiteralPath $GateScript)) {
    throw "gate script is absent"
}
$soakHash = Get-FileSha256 -Path $PSCommandPath
$gateHash = Get-FileSha256 -Path $GateScript
$sourceTreeHash = Get-SourceTreeSha256 -ProjectRoot $projectRoot
$expectedPrereg = if ($production) { $STEP63_PREREG } else { $Preregister }

if ([string]::IsNullOrWhiteSpace($Out)) {
    throw "-Out is required (the evidence directory for headers, per-run logs, and the verdict)"
}
if ($Repetitions -lt 1) {
    throw "-Repetitions must be a positive integer, got $Repetitions"
}
if ($production -and $Repetitions -ne 8) {
    throw "production containment soak requires exactly 8 repetitions"
}

$headerPath = Join-Path $Out "evidence-header.txt"
$verdictPath = Join-Path $Out "verdict.txt"

if ($VerifyOnly) {
    # Fail-closed receipt check: the evidence directory must already carry a header, a PASS
    # verdict, the expected number of run logs, and one staged-tree hash carried by every log.
    $failures = @()
    if (-not (Test-Path -LiteralPath $headerPath)) {
        $failures += "evidence header is absent: $headerPath"
    }
    if (-not (Test-Path -LiteralPath $verdictPath)) {
        $failures += "verdict is absent: $verdictPath"
    }
    if ($failures.Count -gt 0) {
        foreach ($failure in $failures) { [Console]::Error.WriteLine($failure) }
        exit 1
    }

    $headerPrereg = Read-HeaderValue -Path $headerPath -Key "preregistration"
    $headerReps = Read-HeaderValue -Path $headerPath -Key "repetitions"
    $verdictValue = Read-HeaderValue -Path $verdictPath -Key "verdict"
    $verdictReps = Read-HeaderValue -Path $verdictPath -Key "repetitions"
    $verdictHash = Read-HeaderValue -Path $verdictPath -Key "staged-tree-sha256"

    if ([string]::IsNullOrWhiteSpace($headerPrereg)) {
        $failures += "preregistration sentence is absent from the evidence header"
    }
    elseif ($headerPrereg -ne $expectedPrereg) {
        $failures += "preregistration sentence does not match the expected one (stale)"
    }
    $bindings = @{
        "producer-mode" = $producerMode
        "producer-version" = $PRODUCER_VERSION
        "soak-script-sha256" = $soakHash
        "gate-script-sha256" = $gateHash
        "source-tree-sha256" = $sourceTreeHash
        "gate-script" = $GateScript
    }
    foreach ($key in $bindings.Keys) {
        if ((Read-HeaderValue -Path $headerPath -Key $key) -ne $bindings[$key]) {
            $failures += "producer binding is absent or stale: $key"
        }
    }
    if ((Read-HeaderValue -Path $verdictPath -Key "header-sha256") -ne
        (Get-FileSha256 -Path $headerPath)) {
        $failures += "evidence header was edited after the run"
    }
    if ($headerReps -ne [string]$Repetitions) {
        $failures += "expected $Repetitions repetitions but the header records $headerReps"
    }
    if ((Read-HeaderValue -Path $verdictPath -Key "preregistration") -ne $headerPrereg) {
        $failures += "header and verdict preregistrations disagree"
    }
    if ($headerReps -ne $verdictReps) {
        $failures += "header repetitions ($headerReps) and verdict repetitions ($verdictReps) disagree"
    }
    if ($verdictValue -ne "PASS") {
        $failures += "terminal verdict is not PASS (got '$verdictValue')"
    }
    if ($verdictHash -notmatch '^[0-9a-f]{64}$') {
        $failures += "verdict staged-tree hash is absent or malformed"
    }

    $logs = @(Get-RunLogPaths -Directory $Out)
    $expectedCount = 0
    if ([int]::TryParse($verdictReps, [ref]$expectedCount) -and $expectedCount -gt 0) {
        if ($logs.Count -ne $expectedCount) {
            $failures += "expected $expectedCount run logs but found $($logs.Count)"
        }
        if ($expectedCount -ne $Repetitions) {
            $failures += "verdict repetitions ($expectedCount) do not match the requested $Repetitions"
            $expectedCount = 0
        }
    }
    else {
        $failures += "verdict repetitions is not a positive integer: '$verdictReps'"
    }
    $verifiedPasses = 0
    for ($index = 1; $index -le $expectedCount; $index++) {
        $log = New-RunLogPath -Directory $Out -Index $index
        if (-not (Test-Path -LiteralPath $log)) {
            $failures += "run log is absent: $log"
            continue
        }
        $logText = [System.IO.File]::ReadAllText($log)
        if ((Read-HeaderValue -Path $verdictPath -Key ("run-{0:D2}-log-sha256" -f $index)) -ne
            (Get-FileSha256 -Path $log)) {
            $failures += "run $index log was edited after the run"
        }
        $logHash = Get-StagedTreeHash -Text $logText
        if ($logHash -eq "") {
            $failures += "run log must carry exactly one staged-tree hash: $log"
        }
        elseif ($verdictHash -match '^[0-9a-f]{64}$' -and $logHash -ne $verdictHash) {
            $failures += "run log staged-tree hash drifted from the verdict (stale): $log"
        }
        $logExit = [regex]::Match($logText, '^=== run (\d+) exit (-?\d+) ===')
        $recordedExit = Read-HeaderValue -Path $verdictPath -Key ("run-{0:D2}-exit" -f $index)
        $recordedSkip = Read-HeaderValue -Path $verdictPath -Key ("run-{0:D2}-selected-skips" -f $index)
        $stdoutMatch = [regex]::Match($logText, '(?s)=== stdout ===\n(.*?)\n=== stderr ===')
        $logSkip = if ($stdoutMatch.Success) { Get-SelectedSkipCount -Text $stdoutMatch.Groups[1].Value } else { "" }
        $started = Read-HeaderValue -Path $verdictPath -Key ("run-{0:D2}-started-utc" -f $index)
        $finished = Read-HeaderValue -Path $verdictPath -Key ("run-{0:D2}-finished-utc" -f $index)
        $parsedStart = [DateTimeOffset]::MinValue
        $parsedFinish = [DateTimeOffset]::MinValue
        $timesValid = [DateTimeOffset]::TryParse($started, [ref]$parsedStart) -and
            [DateTimeOffset]::TryParse($finished, [ref]$parsedFinish) -and
            $parsedFinish -ge $parsedStart
        if (-not $timesValid -or $logText -notmatch [regex]::Escape("run-started-utc: $started") -or
            $logText -notmatch [regex]::Escape("run-finished-utc: $finished")) {
            $failures += "run $index timestamps are absent or inconsistent"
        }
        if ($logSkip -ne "0" -or $recordedSkip -ne "0") {
            $failures += "run $index selected-skip count is absent or nonzero"
        }
        if (-not $logExit.Success -or $logExit.Groups[1].Value -ne [string]$index -or
            $logExit.Groups[2].Value -ne "0" -or $recordedExit -ne "0" -or
            $logSkip -ne "0" -or $recordedSkip -ne "0") {
            $failures += "run $index has a missing, mismatched, or nonzero gate exit"
        }
        else {
            $verifiedPasses += 1
        }
    }
    if ((Read-HeaderValue -Path $verdictPath -Key "pass-count") -ne [string]$expectedCount -or
        $verifiedPasses -ne $expectedCount) {
        $failures += "pass count does not match every recorded gate exit"
    }
    if ((Read-HeaderValue -Path $verdictPath -Key "hash-consistent") -ne "True") {
        $failures += "verdict does not assert a consistent staged-tree hash"
    }
    $expectedRate = Format-Rate -PassCount $expectedCount -Total $expectedCount
    $recordedRates = @([System.IO.File]::ReadAllLines($verdictPath) |
        Where-Object { $_ -like 'containment_gate_rate=*' })
    if ($recordedRates.Count -ne 1 -or $recordedRates[0] -cne $expectedRate) {
        $failures += "recorded containment gate rate is absent or inconsistent"
    }

    if ($failures.Count -gt 0) {
        foreach ($failure in $failures) { [Console]::Error.WriteLine($failure) }
        exit 1
    }
    Write-Output "verify-only=PASS $((Format-Rate -PassCount $expectedCount -Total $expectedCount))"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Preregister)) {
    throw "-Preregister is required for a soak run (the claim is written before run 1)"
}
$powershell = Get-Command powershell.exe -ErrorAction Stop

if (-not (Test-Path -LiteralPath $Out)) {
    $null = New-Item -ItemType Directory -Path $Out -Force
}
elseif (@(Get-ChildItem -LiteralPath $Out -Force).Count -ne 0) {
    throw "-Out must be empty for a new soak; existing evidence cannot be overwritten: $Out"
}

# Write the preregistration to the evidence header BEFORE run 1: the claim precedes any data.
$headerLines = @(
    "preregistration: $Preregister",
    "repetitions: $Repetitions",
    "distribution: $Distribution",
    "producer-mode: $producerMode",
    "producer-version: $PRODUCER_VERSION",
    "gate-script: $GateScript",
    "soak-script-sha256: $soakHash",
    "gate-script-sha256: $gateHash",
    "source-tree-sha256: $sourceTreeHash",
    "started-utc: $([DateTime]::UtcNow.ToString('o'))"
)
[System.IO.File]::WriteAllText($headerPath, ($headerLines -join "`n") + "`n")

$exitCodes = @()
$hashes = @()
$skips = @()
$starts = @()
$finishes = @()
$logHashes = @()
$passCount = 0
for ($index = 1; $index -le $Repetitions; $index++) {
    $logPath = New-RunLogPath -Directory $Out -Index $index
    $stdoutPath = "$logPath.stdout"
    $stderrPath = "$logPath.stderr"
    $quotedGateScript = '"' + $GateScript.Replace('"', '\"') + '"'
    $quotedDistribution = '"' + $Distribution.Replace('"', '\"') + '"'
    $gateArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $quotedGateScript,
        "-Distribution", $quotedDistribution
    )
    # Foreground child process (never backgrounded): captures the exact exit code without the
    # gate's own `exit` terminating this wrapper, and preserves the full run log as evidence.
    $runStart = [DateTime]::UtcNow.ToString('o')
    $exitCode = 127
    try {
        $process = Start-Process -FilePath $powershell.Source -ArgumentList $gateArgs `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $exitCode = $process.ExitCode
    }
    catch {
        [System.IO.File]::WriteAllText($stderrPath, "gate invocation failed: $_`n")
    }
    $runFinish = [DateTime]::UtcNow.ToString('o')

    $stdoutText = ""
    if (Test-Path -LiteralPath $stdoutPath) {
        $stdoutText = [System.IO.File]::ReadAllText($stdoutPath)
    }
    $stderrText = ""
    if (Test-Path -LiteralPath $stderrPath) {
        $stderrText = [System.IO.File]::ReadAllText($stderrPath)
    }
    $skipCount = Get-SelectedSkipCount -Text $stdoutText
    $combined = "=== run $index exit $exitCode ===`nrun-started-utc: $runStart`nrun-finished-utc: $runFinish`nrun-selected-skips: $skipCount`n=== stdout ===`n$stdoutText`n=== stderr ===`n$stderrText`n"
    [System.IO.File]::WriteAllText($logPath, $combined)
    foreach ($temp in @($stdoutPath, $stderrPath)) {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force
        }
    }

    $exitCodes += $exitCode
    $hashes += (Get-StagedTreeHash -Text $stdoutText)
    $skips += $skipCount
    $starts += $runStart
    $finishes += $runFinish
    $logHashes += (Get-FileSha256 -Path $logPath)
    if ($exitCode -eq 0 -and $skipCount -eq "0") {
        $passCount += 1
    }
    Write-Output "run $index exit $exitCode"
}

# Reject any nonzero gate exit and assert exactly ONE staged-tree hash across the runs.
$distinctHashes = @($hashes | Where-Object { $_ -ne "" } | Select-Object -Unique)
$singleHash = ""
$hashConsistent = $true
if ($distinctHashes.Count -eq 1) {
    $singleHash = $distinctHashes[0]
}
elseif ($distinctHashes.Count -gt 1) {
    $hashConsistent = $false
}

# A passing run that carried no hash is a broken receipt, not a pass.
$everyPassHasHash = $true
for ($index = 0; $index -lt $exitCodes.Count; $index++) {
    if ($exitCodes[$index] -eq 0 -and $hashes[$index] -eq "") {
        $everyPassHasHash = $false
    }
}

$allPassed = ($passCount -eq $Repetitions)
$isPass = $allPassed -and $hashConsistent -and $everyPassHasHash -and ($singleHash -ne "")
$verdict = if ($isPass) { "PASS" } else { "FAIL" }
$rateLine = Format-Rate -PassCount $passCount -Total $Repetitions

$verdictLines = @("preregistration: $Preregister", "repetitions: $Repetitions")
$verdictLines += "header-sha256: $(Get-FileSha256 -Path $headerPath)"
for ($index = 1; $index -le $Repetitions; $index++) {
    $verdictLines += ("run-{0:D2}-exit: {1}" -f $index, $exitCodes[$index - 1])
    $verdictLines += ("run-{0:D2}-selected-skips: {1}" -f $index, $skips[$index - 1])
    $verdictLines += ("run-{0:D2}-started-utc: {1}" -f $index, $starts[$index - 1])
    $verdictLines += ("run-{0:D2}-finished-utc: {1}" -f $index, $finishes[$index - 1])
    $verdictLines += ("run-{0:D2}-log-sha256: {1}" -f $index, $logHashes[$index - 1])
}
$verdictLines += "pass-count: $passCount"
$verdictLines += "staged-tree-sha256: $singleHash"
$verdictLines += "hash-consistent: $hashConsistent"
$verdictLines += $rateLine
$verdictLines += "verdict: $verdict"
[System.IO.File]::WriteAllText($verdictPath, ($verdictLines -join "`n") + "`n")

if (-not $hashConsistent) {
    [Console]::Error.WriteLine("staged-tree hash drifted across runs: $($distinctHashes -join ', ')")
}
if (-not $everyPassHasHash) {
    [Console]::Error.WriteLine("a passing run produced no staged-tree hash")
}

Write-Output $rateLine
Write-Output "verdict=$verdict"
if ($isPass) {
    exit 0
}
exit 1
