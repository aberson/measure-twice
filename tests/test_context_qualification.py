"""Offline fake-live exercise of the PowerShell qualification and stored evidence verifier."""

# ruff: noqa: S603, S607 -- controlled fixture paths and the installed PowerShell executable

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from measure_twice.context_qualification import PREREGISTRATION, QualificationError, evaluate

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "qualify-model-sweep-context.ps1"
SUITE = ROOT / "suites" / "model-sweep-context-canary-v1.json"
PROFILE = ROOT / "profiles" / "model-sweep-execution-v1.json"


def _fake_command(tmp_path: Path) -> Path:
    fake_py = tmp_path / "fake_mt.py"
    fake_py.write_text(
        """import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'unused'))
sys.path.insert(0, TESTS_PATH)
from conftest import StubAdapters
from test_report import _unresolved_claude_stdout
from measure_twice.config import load_config
from measure_twice.runner import run
from measure_twice.scoring import make_deterministic_scorer
from measure_twice.suite import load_suite

parser = argparse.ArgumentParser()
parser.add_argument('command')
parser.add_argument('--suite')
parser.add_argument('--config')
parser.add_argument('--models')
parser.add_argument('--out')
parser.add_argument('--samples', type=int)
parser.add_argument('--preregister')
args = parser.parse_args()
assert args.command == 'run'
out = Path(args.out)
index = json.loads((out / 'index.json').read_text(encoding='utf-8'))
assert index['status'] == 'IN_PROGRESS'
assert index['preregistration'] == args.preregister
assert len(list(Path.cwd().glob('.mt-context-canary-repository-*.txt'))) == 1
assert (Path.cwd() / 'CLAUDE.md').is_file()
repository_file = next(Path.cwd().glob('.mt-context-canary-repository-*.txt'))
customization_file = Path.cwd() / 'CLAUDE.md'
assert index['sentinels']['repository'] in repository_file.read_text()
assert index['sentinels']['customization'] in customization_file.read_text()
assert index['sentinels']['session'] in customization_file.read_text()
assert os.environ['MT_CONTEXT_CANARY_ENVIRONMENT'] == index['sentinels']['environment']
assert os.environ['MT_CONTEXT_CANARY_SESSION'] == index['sentinels']['session']
assert args.models == 'haiku,sonnet,opus' and args.samples == 1
suite = load_suite(args.suite)
tokens = {item.prompt: item.expected for item in suite.items}
mode = os.environ.get('MT_FAKE_CANARY_MODE', 'pass')
def answer(prompt):
    token = tokens[prompt]
    if mode == 'leak':
        return token + ' ' + index['sentinels']['repository']
    if mode == 'unresolved':
        return _unresolved_claude_stdout(token)
    return token
result = run(
    suite=suite, config=load_config(args.config), out_dir=out,
    roster=args.models.split(','), samples_per_cell=args.samples,
    scorer=make_deterministic_scorer(suite.scoring),
    claude_runner_factory=StubAdapters(claude=answer).claude_factory(),
    preregister=args.preregister,
)
if mode == 'incomplete':
    rows_path = result.run_dir / 'rows.jsonl'
    rows_path.write_text('\\n'.join(rows_path.read_text().splitlines()[:-1]) + '\\n')
if mode in ('alias_only', 'blank_identity', 'nonstring_identity'):
    rows_path = result.run_dir / 'rows.jsonl'
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    for row in rows:
        row['model_id_resolved'] = (
            row['model'] if mode == 'alias_only'
            else ('   ' if mode == 'blank_identity' else 123)
        )
    rows_path.write_text('\\n'.join(json.dumps(row) for row in rows) + '\\n')
print(f'{result.run_id}: {result.cells_completed}/{result.cells_total} cells done')
""".replace("TESTS_PATH", repr(str(ROOT / "tests"))),
        encoding="utf-8",
    )
    cmd = tmp_path / "fake-mt.cmd"
    cmd.write_text(
        f'@echo off\r\nuv run --project "{ROOT}" python "{fake_py}" %*\r\n', encoding="ascii"
    )
    return cmd


def _wrapper(tmp_path: Path, fake: Path, *, mode: str = "pass") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MT_FAKE_CANARY_MODE"] = mode
    if mode == "cleanup_failure":
        env["MT_CONTEXT_CANARY_TEST_FAIL_CLEANUP"] = "1"
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-Profile",
            str(PROFILE),
            "-Suite",
            str(SUITE),
            "-Out",
            str(tmp_path / "out"),
            "-Preregister",
            PREREGISTRATION,
            "-MtCommand",
            str(fake),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5.1 wrapper runs on Windows")
def test_qualification_fake_live_and_verify_only_rechecks_hashes(tmp_path: Path) -> None:
    fake = _fake_command(tmp_path)
    result = _wrapper(tmp_path, fake)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "qualification=PASS passed=3 total=3" in result.stdout
    index_path = tmp_path / "out" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["status"] == "PASS"
    assert index["passed"] == index["total"] == 3
    assert len(index["arms"]) == 3
    assert all(arm["terminal_cells"] == 1 for arm in index["arms"])
    assert sum(arm["terminal_cells"] for arm in index["arms"]) == 3
    assert not list(tmp_path.glob(".mt-context-canary-*"))
    verify = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-VerifyOnly",
            "-Out",
            str(tmp_path / "out"),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    for field, stale_value in (
        ("version", "old-producer"),
        ("wrapper_sha256", "0" * 64),
        ("verifier_sha256", "0" * 64),
    ):
        stale_index = {**index, "producer": {**index["producer"], field: stale_value}}
        index_path.write_text(json.dumps(stale_index), encoding="utf-8")
        stale = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-VerifyOnly",
                "-Out",
                str(tmp_path / "out"),
            ],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        assert stale.returncode != 0
        assert "qualification producer version or digest changed" in stale.stderr
    index_path.write_text(json.dumps(index), encoding="utf-8")
    run_dir = tmp_path / "out" / "runs" / index["run_id"]
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-VerifyOnly",
            "-Out",
            str(tmp_path / "out"),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert changed.returncode != 0
    assert "stored evidence_hashes changed" in changed.stderr
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8").rstrip(), encoding="utf-8")
    planting_file = tmp_path / "out" / "planting" / "repository.txt"
    planting_file.write_text(planting_file.read_text(encoding="utf-8") + "x", encoding="utf-8")
    changed_planting = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-VerifyOnly",
            "-Out",
            str(tmp_path / "out"),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert changed_planting.returncode != 0
    assert "planted sentinel evidence changed" in changed_planting.stderr


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5.1 wrapper runs on Windows")
@pytest.mark.parametrize(
    "mode",
    ["leak", "unresolved", "incomplete", "alias_only", "blank_identity", "nonstring_identity"],
)
def test_qualification_rejects_bad_fake_live_evidence(tmp_path: Path, mode: str) -> None:
    result = _wrapper(tmp_path, _fake_command(tmp_path), mode=mode)
    assert result.returncode != 0, result.stdout + result.stderr
    index = json.loads((tmp_path / "out" / "index.json").read_text(encoding="utf-8"))
    assert index["status"] == "FAIL"
    assert index["qualification"] == "FAIL"
    assert index["run_id"].startswith("run_")
    assert index["reason"]
    if mode == "incomplete":
        assert "one terminal row" in index["reason"]
    elif mode in ("unresolved", "alias_only", "blank_identity", "nonstring_identity"):
        assert "unresolved provider identity" in index["reason"]
    else:
        assert "planted sentinel" in index["reason"]
    assert not list(tmp_path.glob(".mt-context-canary-*"))


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5.1 wrapper runs on Windows")
def test_qualification_refuses_to_overwrite_failed_attempt(tmp_path: Path) -> None:
    fake = _fake_command(tmp_path)
    first = _wrapper(tmp_path, fake, mode="incomplete")
    assert first.returncode != 0
    index_path = tmp_path / "out" / "index.json"
    before = index_path.read_bytes()
    second = _wrapper(tmp_path, fake)
    assert second.returncode != 0
    assert "already has qualification evidence" in second.stderr
    assert index_path.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5.1 wrapper runs on Windows")
def test_post_verification_cleanup_failure_never_leaves_pass(tmp_path: Path) -> None:
    result = _wrapper(tmp_path, _fake_command(tmp_path), mode="cleanup_failure")
    assert result.returncode != 0
    index_path = tmp_path / "out" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["status"] == index["qualification"] == "FAIL"
    assert "injected cleanup failure" in index["reason"]
    assert index["run_id"].startswith("run_")
    assert not list(tmp_path.glob(".mt-context-canary-*"))
    verify = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-VerifyOnly",
            "-Out",
            str(tmp_path / "out"),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert verify.returncode != 0
    assert "not PASS" in verify.stderr


def test_verifier_rejects_missing_index(tmp_path: Path) -> None:
    with pytest.raises(QualificationError, match="index missing"):
        evaluate(tmp_path / "index.json", tmp_path, verify_only=True)
