[CmdletBinding()]
param(
    [string]$Distribution = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-GitManifestBytes {
    param(
        [Parameter(Mandatory = $true)][string]$GitExecutable,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $GitExecutable
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $arguments = @("-C", $Root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if ($null -ne $startInfo.PSObject.Properties["ArgumentList"]) {
        foreach ($argument in $arguments) {
            [void]$startInfo.ArgumentList.Add($argument)
        }
    }
    else {
        $quotedRoot = $Root.Replace('"', '\"')
        $startInfo.Arguments = "-C `"$quotedRoot`" ls-files -z --cached --others --exclude-standard"
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $memory = [System.IO.MemoryStream]::new()
    $process.StandardOutput.BaseStream.CopyTo($memory)
    $errorText = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "git manifest failed for '$Root' (exit $($process.ExitCode)): $errorText"
    }
    return $memory.ToArray()
}

function Invoke-WslText {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $output = & wsl.exe -d $Distribution --exec @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "wsl.exe failed with exit $LASTEXITCODE while running: $($Arguments -join ' ')"
    }
    return ($output -join "`n").Trim()
}

if ($env:OS -ne "Windows_NT") {
    throw "This launcher is the Windows control-plane gate; inside WSL run: uv run pytest -q -m linux_isolation"
}

$gitCommand = Get-Command git -ErrorAction Stop
$wslCommand = Get-Command wsl.exe -ErrorAction Stop
$availableDistributions = @(& $wslCommand.Source -l -q) | ForEach-Object { $_.Replace("`0", "").Trim() }
if ([string]::IsNullOrWhiteSpace($Distribution)) {
    $Distribution = @("Ubuntu-24.04", "Ubuntu") |
        Where-Object { $availableDistributions -contains $_ } |
        Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($Distribution)) {
    throw "No Ubuntu or Ubuntu-24.04 WSL distribution is installed"
}
if ($availableDistributions -notcontains $Distribution) {
    throw "Requested WSL distribution '$Distribution' is not installed"
}
$scriptRoot = Split-Path -Parent $PSCommandPath
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $scriptRoot "..")).Path
$switchboardRoot = (Resolve-Path -LiteralPath (Join-Path $projectRoot "..\switchboard")).Path

$null = $wslCommand
$projectManifest = Join-Path ([System.IO.Path]::GetTempPath()) "measure-twice-$([guid]::NewGuid()).manifest0"
$switchboardManifest = Join-Path ([System.IO.Path]::GetTempPath()) "switchboard-$([guid]::NewGuid()).manifest0"
$runnerFile = Join-Path ([System.IO.Path]::GetTempPath()) "measure-twice-$([guid]::NewGuid()).runner.sh"

try {
    [System.IO.File]::WriteAllBytes(
        $projectManifest,
        (Get-GitManifestBytes -GitExecutable $gitCommand.Source -Root $projectRoot)
    )
    [System.IO.File]::WriteAllBytes(
        $switchboardManifest,
        (Get-GitManifestBytes -GitExecutable $gitCommand.Source -Root $switchboardRoot)
    )

    $projectSourceWsl = Invoke-WslText -Arguments @("wslpath", "-a", "-u", $projectRoot)
    $switchboardSourceWsl = Invoke-WslText -Arguments @("wslpath", "-a", "-u", $switchboardRoot)
    $projectManifestWsl = Invoke-WslText -Arguments @("wslpath", "-a", "-u", $projectManifest)
    $switchboardManifestWsl = Invoke-WslText -Arguments @("wslpath", "-a", "-u", $switchboardManifest)

    $runner = @'
set -euo pipefail
project_source=$1
switchboard_source=$2
project_manifest=$3
switchboard_manifest=$4
stage=$(mktemp -d -p /tmp measure-twice-linux-isolation.XXXXXXXX)
cleanup() {
    case "$stage" in
        /tmp/measure-twice-linux-isolation.*) rm -rf -- "$stage" ;;
        *) printf 'refusing unsafe cleanup target: %s\n' "$stage" >&2 ;;
    esac
}
trap cleanup EXIT INT TERM

mkdir -p "$stage/measure-twice" "$stage/switchboard" "$stage/tmp"
tar --create --file=- --directory="$project_source" --null --verbatim-files-from \
    --files-from="$project_manifest" | tar --extract --file=- --directory="$stage/measure-twice"
tar --create --file=- --directory="$switchboard_source" --null --verbatim-files-from \
    --files-from="$switchboard_manifest" | tar --extract --file=- --directory="$stage/switchboard"

if find "$stage" -name .git -o -name .venv | grep -q .; then
    printf 'staging contract failure: .git or .venv was copied\n' >&2
    exit 2
fi

tree_hash=$(
    cd "$stage/measure-twice"
    while IFS= read -r -d '' relative; do
        printf '%s\0' "$relative"
        sha256sum -- "$relative" | cut -d ' ' -f 1
    done < <(find . -type f -printf '%P\0' | LC_ALL=C sort -z) | sha256sum | cut -d ' ' -f 1
)
printf 'staged-tree-sha256: %s\n' "$tree_hash"
printf 'staged-root: %s (WSL ext4 temporary; removed on exit)\n' "$stage"

command -v uv >/dev/null || {
    printf 'uv is unavailable inside %s\n' "${WSL_DISTRO_NAME:-WSL}" >&2
    exit 2
}
export TMPDIR="$stage/tmp"
cd "$stage/measure-twice"
uv sync --extra dev --offline

junit_skip_count() {
    uv run python -I -c 'import sys, xml.etree.ElementTree as ET; root = ET.parse(sys.argv[1]).getroot(); print(sum(1 for case in root.iter("testcase") if case.find("skipped") is not None))' "$1"
}

# Exercise the exact XML parser before trusting it as the non-skipping gate.
skip_gate_pass_xml="$stage/skip-gate-pass.xml"
skip_gate_skip_xml="$stage/skip-gate-skip.xml"
printf '%s\n' '<testsuites><testsuite><testcase name="pass"/></testsuite></testsuites>' > "$skip_gate_pass_xml"
printf '%s\n' '<testsuites><testsuite><testcase name="skip"><skipped/></testcase></testsuite></testsuites>' > "$skip_gate_skip_xml"
if [ "$(junit_skip_count "$skip_gate_pass_xml")" != 0 ] || [ "$(junit_skip_count "$skip_gate_skip_xml")" != 1 ]; then
    printf 'Linux isolation skip-gate self-check failed\n' >&2
    exit 2
fi

junit_report="$stage/linux-isolation-junit.xml"
set +e
uv run pytest -q -m linux_isolation --junitxml="$junit_report"
pytest_status=$?
set -e
if [ "$pytest_status" -ne 0 ]; then
    exit "$pytest_status"
fi
skipped=$(junit_skip_count "$junit_report")
if [ "$skipped" -ne 0 ]; then
    printf 'Linux isolation gate selected %s skipped test(s); skips are forbidden\n' "$skipped" >&2
    exit 3
fi
'@

    [System.IO.File]::WriteAllBytes(
        $runnerFile,
        ([System.Text.UTF8Encoding]::new($false).GetBytes("$runner`n"))
    )
    $runnerFileWsl = Invoke-WslText -Arguments @("wslpath", "-a", "-u", $runnerFile)
    & wsl.exe -d $Distribution --exec bash --login $runnerFileWsl `
        $projectSourceWsl $switchboardSourceWsl $projectManifestWsl $switchboardManifestWsl
    $gateExit = $LASTEXITCODE
    if ($gateExit -ne 0) {
        [Console]::Error.WriteLine("Linux isolation gate failed with exit $gateExit")
        exit $gateExit
    }
}
finally {
    foreach ($manifest in @($projectManifest, $switchboardManifest, $runnerFile)) {
        if (Test-Path -LiteralPath $manifest) {
            Remove-Item -LiteralPath $manifest -Force
        }
    }
}
