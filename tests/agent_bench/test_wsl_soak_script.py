"""Offline integration tests for scripts/soak-agent-bench-wsl.ps1.

These drive the soak wrapper against a FAKE gate script (no real WSL, no ext4, no Bubblewrap), so
they are deterministic and run on the native Windows host. They prove the wrapper rejects a changed
staged-tree hash and a 7/8 result while preserving every per-run log, that a clean 8/8 prints the
pass rate and passes -VerifyOnly, and that -VerifyOnly fails closed on a stale or absent receipt.

The result-driving .ps1 logic is anchored red-on-garbage here (the FAIL cases), per
.claude/rules/windows-shell.md, so a silently green wrapper cannot pass this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

pytestmark = pytest.mark.skipif(
    _POWERSHELL is None,
    reason="soak wrapper is a Windows PowerShell control-plane script",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOAK_SCRIPT = _REPO_ROOT / "scripts" / "soak-agent-bench-wsl.ps1"
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_PREREG = (
    "The reviewed containment repair will pass 8/8 independent WSL-ext4 gate invocations "
    "with zero selected skips and no live-identity or retained-FD escape."
)

_FAKE_GATE = """[CmdletBinding()]
param([string]$Distribution = "")
$ErrorActionPreference = "Stop"
$stateDir = $env:MT_FAKE_GATE_STATEDIR
$planPath = $env:MT_FAKE_GATE_PLAN
$counterPath = Join-Path $stateDir "counter"
$index = 0
if (Test-Path -LiteralPath $counterPath) {
    $index = [int]([System.IO.File]::ReadAllText($counterPath))
}
$plan = @([System.IO.File]::ReadAllLines($planPath))
$line = $plan[$index]
[System.IO.File]::WriteAllText($counterPath, [string]($index + 1))
$parts = $line.Split('|')
$hashValue = $parts[0]
$exitCode = [int]$parts[1]
if ($hashValue -ne "") {
    Write-Output ("staged-tree-sha256: " + $hashValue)
    Write-Output ("staged-root: /tmp/fake-" + $index + " (fake ext4; removed on exit)")
}
Write-Output ("fake gate index " + $index + " exit " + $exitCode)
if ($exitCode -ne 0) {
    [Console]::Error.WriteLine("fake gate simulated failure at index " + $index)
}
exit $exitCode
"""


def _write_fake_gate(tmp_path: Path) -> Path:
    gate = tmp_path / "fake-gate.ps1"
    gate.write_text(_FAKE_GATE, encoding="utf-8")
    return gate


def _run_soak(
    tmp_path: Path,
    *,
    plan: list[str],
    repetitions: int,
    out: Path | None = None,
    verify_only: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    gate = _write_fake_gate(tmp_path)
    plan_path = tmp_path / "plan.txt"
    plan_path.write_text("\n".join(plan) + "\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    out_dir = out if out is not None else tmp_path / "evidence"
    args = [
        _POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_SOAK_SCRIPT),
        "-Distribution",
        "FakeUbuntu",
        "-Repetitions",
        str(repetitions),
        "-Out",
        str(out_dir),
        "-Preregister",
        _PREREG,
        "-GateScript",
        str(gate),
    ]
    if verify_only:
        args.append("-VerifyOnly")
    env = {
        **os.environ,
        "MT_FAKE_GATE_PLAN": str(plan_path),
        "MT_FAKE_GATE_STATEDIR": str(state_dir),
    }
    completed = subprocess.run(  # noqa: S603 - resolved PowerShell running a test-authored script
        args,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    return completed, out_dir


def test_clean_eight_of_eight_prints_rate_and_verify_only_passes(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0" for _ in range(8)]
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=8)

    assert completed.returncode == 0, completed.stderr
    assert "containment_gate_rate=8/8 (100.0%)" in completed.stdout
    header = (out_dir / "evidence-header.txt").read_text(encoding="utf-8")
    assert _PREREG in header
    verdict = (out_dir / "verdict.txt").read_text(encoding="utf-8")
    assert "verdict: PASS" in verdict
    assert _HASH_A in verdict
    logs = sorted(out_dir.glob("run-*.log"))
    assert len(logs) == 8

    verify, _ = _run_soak(
        tmp_path,
        plan=plan,
        repetitions=8,
        out=out_dir,
        verify_only=True,
    )
    assert verify.returncode == 0, verify.stderr


def test_rejects_changed_staged_tree_hash_and_preserves_all_logs(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0" for _ in range(8)]
    plan[4] = f"{_HASH_B}|0"  # run 5 stages a different tree while still exiting 0
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=8)

    assert completed.returncode != 0
    verdict = (out_dir / "verdict.txt").read_text(encoding="utf-8")
    assert "verdict: FAIL" in verdict
    logs = sorted(out_dir.glob("run-*.log"))
    assert len(logs) == 8  # every run log is preserved for forensics
    assert (out_dir / "evidence-header.txt").exists()


def test_rejects_seven_of_eight_and_preserves_all_logs(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0" for _ in range(8)]
    plan[6] = f"{_HASH_A}|2"  # run 7 fails the gate with a nonzero exit
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=8)

    assert completed.returncode != 0
    assert "containment_gate_rate=7/8" in completed.stdout
    verdict = (out_dir / "verdict.txt").read_text(encoding="utf-8")
    assert "verdict: FAIL" in verdict
    assert "run-07-exit: 2" in verdict
    logs = sorted(out_dir.glob("run-*.log"))
    assert len(logs) == 8


def test_verify_only_detects_a_stale_run_log(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0" for _ in range(8)]
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=8)
    assert completed.returncode == 0, completed.stderr

    stale = out_dir / "run-03.log"
    stale.write_text(
        f"=== run 3 exit 0 ===\nstaged-tree-sha256: {_HASH_B}\n",
        encoding="utf-8",
    )
    verify, _ = _run_soak(
        tmp_path,
        plan=plan,
        repetitions=8,
        out=out_dir,
        verify_only=True,
    )
    assert verify.returncode != 0
    assert "stale" in verify.stderr.lower()


def test_verify_only_rejects_forged_pass_with_a_failed_run(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0" for _ in range(8)]
    plan[6] = f"{_HASH_A}|2"
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=8)
    assert completed.returncode != 0

    verdict_path = out_dir / "verdict.txt"
    verdict_path.write_text(
        verdict_path.read_text(encoding="utf-8").replace("verdict: FAIL", "verdict: PASS"),
        encoding="utf-8",
    )
    verify, _ = _run_soak(tmp_path, plan=plan, repetitions=8, out=out_dir, verify_only=True)
    assert verify.returncode != 0
    assert "nonzero gate exit" in verify.stderr.lower()


def test_soak_preserves_existing_evidence_and_quotes_gate_path(tmp_path: Path) -> None:
    spaced = tmp_path / "gate path with spaces"
    spaced.mkdir()
    plan = [f"{_HASH_A}|0"]
    completed, out_dir = _run_soak(spaced, plan=plan, repetitions=1)
    assert completed.returncode == 0, completed.stderr
    original_header = (out_dir / "evidence-header.txt").read_bytes()

    rerun, _ = _run_soak(spaced, plan=plan, repetitions=1, out=out_dir)
    assert rerun.returncode != 0
    assert (out_dir / "evidence-header.txt").read_bytes() == original_header


def test_verify_only_fails_closed_on_absent_evidence(tmp_path: Path) -> None:
    empty = tmp_path / "empty-evidence"
    empty.mkdir()
    verify, _ = _run_soak(
        tmp_path,
        plan=[f"{_HASH_A}|0"],
        repetitions=8,
        out=empty,
        verify_only=True,
    )
    assert verify.returncode != 0
    assert "absent" in verify.stderr.lower()


def test_soak_script_is_ascii_only() -> None:
    """PowerShell 5.1 decodes a no-BOM .ps1 as cp1252; keep it ASCII (windows-shell rule)."""

    raw = _SOAK_SCRIPT.read_bytes()
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, f"non-ASCII bytes in soak wrapper: {non_ascii[:5]}"
