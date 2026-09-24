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
$headerPath = Join-Path $env:MT_FAKE_GATE_OUT "evidence-header.txt"
if (-not (Test-Path -LiteralPath $headerPath) -or
    -not ([System.IO.File]::ReadAllText($headerPath).Contains($env:MT_FAKE_GATE_PREREG))) {
    [Console]::Error.WriteLine("fake gate ran before preregistration header existed")
    exit 9
}
Write-Output "prereg-header-present: true"
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
$skipCount = if ($parts.Count -ge 3) { [int]$parts[2] } else { 0 }
if ($hashValue -ne "") {
    Write-Output ("staged-tree-sha256: " + $hashValue)
    Write-Output ("staged-root: /tmp/fake-" + $index + " (fake ext4; removed on exit)")
}
Write-Output ("fake gate index " + $index + " exit " + $exitCode)
Write-Output ("selected-skips: " + $skipCount)
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
    soak_script: Path = _SOAK_SCRIPT,
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
        str(soak_script),
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
        "MT_FAKE_GATE_OUT": str(out_dir),
        "MT_FAKE_GATE_PREREG": _PREREG,
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
    assert "prereg-header-present: true" in (out_dir / "run-01.log").read_text(encoding="utf-8")
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


def test_verify_only_rejects_ambiguous_staged_hash(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0"]
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=1)
    assert completed.returncode == 0, completed.stderr
    log_path = out_dir / "run-01.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"staged-tree-sha256: {_HASH_B}\n")

    verify, _ = _run_soak(tmp_path, plan=plan, repetitions=1, out=out_dir, verify_only=True)
    assert verify.returncode != 0
    assert "exactly one staged-tree hash" in verify.stderr.lower()


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


def test_default_verify_rejects_fixture_receipt_without_optional_arguments(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0" for _ in range(8)]
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=8)
    assert completed.returncode == 0, completed.stderr

    verify = subprocess.run(  # noqa: S603 - resolved PowerShell running a repo script
        [
            _POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_SOAK_SCRIPT),
            "-Out",
            str(out_dir),
            "-VerifyOnly",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verify.returncode != 0
    assert "stale" in verify.stderr.lower()


def test_verify_rejects_changed_gate_and_edited_header(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0"]
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=1)
    assert completed.returncode == 0, completed.stderr

    gate = tmp_path / "fake-gate.ps1"
    gate.write_text(gate.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    # _run_soak rewrites the fake gate; invoke the verifier directly to retain the drift.
    args = [
        _POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_SOAK_SCRIPT),
        "-Repetitions",
        "1",
        "-Out",
        str(out_dir),
        "-Preregister",
        _PREREG,
        "-GateScript",
        str(gate),
        "-VerifyOnly",
    ]
    stale = subprocess.run(args, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert stale.returncode != 0
    assert "gate-script-sha256" in stale.stderr

    header_path = out_dir / "evidence-header.txt"
    header_path.write_text(
        header_path.read_text(encoding="utf-8") + "edited: yes\n", encoding="utf-8"
    )
    edited = subprocess.run(args, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert edited.returncode != 0
    assert "header was edited" in edited.stderr


def test_zero_gate_exit_with_selected_skip_is_not_a_pass(tmp_path: Path) -> None:
    completed, out_dir = _run_soak(tmp_path, plan=[f"{_HASH_A}|0|1"], repetitions=1)
    assert completed.returncode != 0
    assert "containment_gate_rate=0/1" in completed.stdout
    assert "run-01-selected-skips: 1" in (out_dir / "verdict.txt").read_text(encoding="utf-8")
    assert (out_dir / "run-01.log").exists()


def test_zero_gate_exit_without_staged_hash_is_not_a_pass(tmp_path: Path) -> None:
    plan = [f"{_HASH_A}|0" for _ in range(8)]
    plan[3] = "|0"
    completed, out_dir = _run_soak(tmp_path, plan=plan, repetitions=8)
    assert completed.returncode != 0
    assert "a passing run produced no staged-tree hash" in completed.stderr
    assert "verdict: FAIL" in (out_dir / "verdict.txt").read_text(encoding="utf-8")
    assert len(list(out_dir.glob("run-*.log"))) == 8


def test_production_requires_eight_repetitions(tmp_path: Path) -> None:
    rejected = subprocess.run(  # noqa: S603 - resolved PowerShell running a repo script
        [
            _POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_SOAK_SCRIPT),
            "-Out",
            str(tmp_path / "receipt"),
            "-Repetitions",
            "1",
            "-VerifyOnly",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rejected.returncode != 0
    assert "exactly 8 repetitions" in rejected.stderr


def test_verify_rejects_stale_isolation_source_and_edited_rate(tmp_path: Path) -> None:
    fixture_root = tmp_path / "reviewed-tree"
    source_files = [
        "scripts/soak-agent-bench-wsl.ps1",
        "measure_twice/agent_bench/process.py",
        "measure_twice/agent_bench/isolation.py",
        "tests/agent_bench/test_process.py",
        "tests/agent_bench/test_isolation.py",
    ]
    for relative in source_files:
        destination = fixture_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REPO_ROOT / relative, destination)

    fixture_script = fixture_root / "scripts" / "soak-agent-bench-wsl.ps1"
    completed, out_dir = _run_soak(
        tmp_path, plan=[f"{_HASH_A}|0"], repetitions=1, soak_script=fixture_script
    )
    assert completed.returncode == 0, completed.stderr

    isolation = fixture_root / "measure_twice" / "agent_bench" / "isolation.py"
    isolation.write_text(isolation.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    stale, _ = _run_soak(
        tmp_path,
        plan=[f"{_HASH_A}|0"],
        repetitions=1,
        out=out_dir,
        verify_only=True,
        soak_script=fixture_script,
    )
    assert stale.returncode != 0
    assert "source-tree-sha256" in stale.stderr

    # Restore the producer so this second failure isolates the recorded rate.
    shutil.copyfile(_REPO_ROOT / "measure_twice/agent_bench/isolation.py", isolation)
    verdict_path = out_dir / "verdict.txt"
    verdict_path.write_text(
        verdict_path.read_text(encoding="utf-8").replace(
            "containment_gate_rate=1/1 (100.0%)", "containment_gate_rate=0/1 (0.0%)"
        ),
        encoding="utf-8",
    )
    edited, _ = _run_soak(
        tmp_path,
        plan=[f"{_HASH_A}|0"],
        repetitions=1,
        out=out_dir,
        verify_only=True,
        soak_script=fixture_script,
    )
    assert edited.returncode != 0
    assert "recorded containment gate rate" in edited.stderr


def test_git_manifest_ignores_growing_qualification_evidence(tmp_path: Path) -> None:
    """Use the launcher's real git manifest command to exclude growing evidence."""

    repo = tmp_path / "manifest-repo"
    repo.mkdir()
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - resolved git running against an isolated test repo
        [git, "init", "-q", str(repo)], check=True, capture_output=True
    )
    shutil.copyfile(_REPO_ROOT / ".gitignore", repo / ".gitignore")
    (repo / "source.txt").write_text("reviewed source\n", encoding="utf-8")
    subprocess.run(  # noqa: S603 - resolved git running against an isolated test repo
        [git, "-C", str(repo), "add", ".gitignore", "source.txt"],
        check=True,
        capture_output=True,
    )

    def manifest_paths() -> list[str]:
        raw = subprocess.check_output(  # noqa: S603 - exact launcher manifest command
            [git, "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        )
        return [part.decode("utf-8") for part in raw.split(b"\0") if part]

    before = manifest_paths()
    evidence = repo / "data" / "qualification" / "agent-bench-containment-step63"
    evidence.mkdir(parents=True)
    (evidence / "evidence-header.txt").write_text("preregistered\n", encoding="utf-8")
    (evidence / "run-01.log").write_text("first\n", encoding="utf-8")
    after_first = manifest_paths()
    (evidence / "run-02.log").write_text("second\n", encoding="utf-8")
    after_second = manifest_paths()
    assert before == after_first == after_second
    assert all("qualification" not in path for path in after_second)


def test_soak_script_is_ascii_only() -> None:
    """PowerShell 5.1 decodes a no-BOM .ps1 as cp1252; keep it ASCII (windows-shell rule)."""

    raw = _SOAK_SCRIPT.read_bytes()
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, f"non-ASCII bytes in soak wrapper: {non_ascii[:5]}"
