from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

import measure_twice.adapters.claude_cli as claude_adapter
import measure_twice.adapters.local as local_adapter
import measure_twice.agent_bench.models as agent_models
from measure_twice.agent_bench import AgentCliDeps
from measure_twice.agent_bench.analysis import load_analysis_plan
from measure_twice.cli import CliDeps, main

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "suites" / "agents" / "smoke"
PROFILES = ROOT / "profiles" / "agent-models-candidates.json"
EXECUTION = ROOT / "profiles" / "agent-execution-v1.json"
ANALYSIS = ROOT / "analysis-plans" / "agent-smoke-v1.json"
PREREGISTRATION = ROOT / "docs" / "agent-benchmark" / "smoke-preregistration.md"
THREE_MODELS = (
    ROOT / "tests" / "agent_bench" / "fixtures" / "wire" / "inputs" / "agent-models-three.json"
)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _optional_snapshot(root: Path) -> dict[str, str] | None:
    return _snapshot(root) if root.exists() else None


def _git_common_root() -> Path:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - resolved trusted Git executable
        [git, "rev-parse", "--git-common-dir"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    common_root = Path(result.stdout.strip())
    return common_root.resolve() if common_root.is_absolute() else (ROOT / common_root).resolve()


def test_root_cli_owns_defaulted_agent_dependency_bundle() -> None:
    assert isinstance(CliDeps().agent, AgentCliDeps)


def test_real_structure_only_cli_passes_with_default_profiles(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["agent", "validate", str(SMOKE), "--structure-only"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "agent-smoke: valid structure (1 task(s), run_class=smoke)" in captured.out
    assert "instrument_hash:" in captured.out
    assert "selected_profile_hash:" in captured.out
    assert "execution_profile_hash:" in captured.out
    assert captured.err == ""


def test_structure_only_executes_no_commands_calls_no_providers_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "suite"
    shutil.copytree(SMOKE, bundle)
    profiles = tmp_path / "profiles.json"
    execution = tmp_path / "execution.json"
    shutil.copyfile(PROFILES, profiles)
    shutil.copyfile(EXECUTION, execution)
    state_roots = (tmp_path, ROOT, ROOT / "data", _git_common_root())
    before = {root: _optional_snapshot(root) for root in state_roots}

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("structure-only attempted provider/evaluator/network execution")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(local_adapter, "local_chat", forbidden)
    monkeypatch.setattr(claude_adapter, "claude_call", forbidden)
    monkeypatch.setattr(agent_models, "dispatch_by_provider", forbidden)

    rc = main(
        [
            "agent",
            "validate",
            str(bundle),
            "--profiles",
            str(profiles),
            "--execution-profile",
            str(execution),
            "--structure-only",
        ],
        deps=CliDeps(
            local_transport_factory=forbidden,
            claude_runner_factory=forbidden,
            scorer=forbidden,
            judge_caller=forbidden,
        ),
    )

    assert rc == 0
    assert {root: _optional_snapshot(root) for root in state_roots} == before


def test_non_structure_validate_fails_loud_without_execution(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("Step 25 must not execute evaluator anchors")

    monkeypatch.setattr(subprocess, "run", forbidden)
    rc = main(["agent", "validate", str(SMOKE)])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "not available in Step 25" in captured.err
    assert "--structure-only" in captured.err


def test_agent_validate_reports_bundle_bytecode_as_one_actionable_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "smoke"
    shutil.copytree(SMOKE, bundle)
    (bundle / "tasks" / "smoke-add" / "oracle" / "tests" / "__pycache__").mkdir()

    rc = main(["agent", "validate", str(bundle), "--structure-only"])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("agent validate: ")
    assert "generated Python bytecode" in captured.err
    assert "tasks/smoke-add/oracle/tests/__pycache__" in captured.err
    assert "re-validate" in captured.err


def test_agent_validate_surfaces_contract_failure_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.loads(EXECUTION.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["id"] = "different-profile"
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(
        [
            "agent",
            "validate",
            str(SMOKE),
            "--profiles",
            str(PROFILES),
            "--execution-profile",
            str(execution),
            "--structure-only",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "does not match loaded execution profile" in captured.err
    assert "Traceback" not in captured.err


def test_agent_validate_rejects_model_execution_profile_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.loads(PROFILES.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    models = payload["models"]
    assert isinstance(models, list)
    assert isinstance(models[0], dict)
    models[0]["execution_profile_id"] = "different-profile"
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(
        [
            "agent",
            "validate",
            str(SMOKE),
            "--profiles",
            str(profiles),
            "--execution-profile",
            str(EXECUTION),
            "--structure-only",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "model profile 'codex-luna' execution_profile_id" in captured.err
    assert "does not match loaded execution profile" in captured.err
    assert "Traceback" not in captured.err


def test_agent_validate_rejects_selected_run_policy_model_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "agent",
            "validate",
            str(SMOKE),
            "--profiles",
            str(THREE_MODELS),
            "--execution-profile",
            str(EXECUTION),
            "--structure-only",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "run_policy.smoke.model_count" in captured.err
    assert "must equal selected model count (3), got 2" in captured.err


def test_agent_validate_rejects_selected_run_policy_task_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.loads(EXECUTION.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    run_policy = payload["run_policy"]
    assert isinstance(run_policy, dict)
    smoke_policy = run_policy["smoke"]
    assert isinstance(smoke_policy, dict)
    smoke_policy.update(task_count=2, terminal_cells=4, provider_attempts=8)
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(
        [
            "agent",
            "validate",
            str(SMOKE),
            "--profiles",
            str(PROFILES),
            "--execution-profile",
            str(execution),
            "--structure-only",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "run_policy.smoke.task_count" in captured.err
    assert "must equal loaded suite task count (1), got 2" in captured.err


def test_agent_argparse_misuse_exits_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["agent", "validate"])
    assert exc_info.value.code == 2


def test_agent_validate_help_is_registered(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["agent", "validate", "--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "--structure-only" in captured.out
    assert "--execution-profile" in captured.out


def test_smoke_preregistration_cites_canonical_none_plan_hash() -> None:
    plan = load_analysis_plan(ANALYSIS)
    text = PREREGISTRATION.read_text(encoding="utf-8")

    assert plan.policy == "none"
    assert plan.sha256 in text
    assert "not a ranking study" in text


def test_default_generated_data_roots_are_ignored_but_inputs_and_evidence_are_visible() -> None:
    git = shutil.which("git")
    assert git is not None
    for relative in (
        "data/agent-runs/example/manifest.json",
        "data/agent-workspaces/example/worktree.txt",
        "data/agent-reports/example.md",
        "data/agent-confirmations/example.pending.json",
        "data/exports/example.json",
    ):
        result = subprocess.run(  # noqa: S603 - resolved trusted Git executable
            [git, "check-ignore", "--no-index", "--quiet", relative],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, relative

    for relative in (
        "profiles/agent-models-candidates.json",
        "analysis-plans/agent-smoke-v1.json",
        "suites/agents/smoke/suite.json",
        "docs/agent-benchmark/evidence/example.json",
    ):
        result = subprocess.run(  # noqa: S603 - resolved trusted Git executable
            [git, "check-ignore", "--no-index", "--quiet", relative],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 1, relative
