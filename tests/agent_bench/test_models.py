from __future__ import annotations

import json
from pathlib import Path

import pytest

import measure_twice.agent_bench._linux_capabilities as capabilities_module
import measure_twice.agent_bench.isolation as isolation_module
import measure_twice.agent_bench.models as models_module
import measure_twice.agent_bench.process as process_module
from measure_twice.agent_bench.models import (
    Ceilings,
    ExecutionProfile,
    ModelSpec,
    ModelSpecError,
    dispatch_by_provider,
    execution_profile_hash,
    load_execution_profile,
    load_model_registry,
    selected_profile_hash,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "profiles" / "agent-models-candidates.json"
EXECUTION = ROOT / "profiles" / "agent-execution-v1.json"
THREE_MODELS = Path(__file__).parent / "fixtures" / "wire" / "inputs" / "agent-models-three.json"

SELECTED_PROFILE_HASH = "6b10e22d3f89e7f541ea3efb358367084de934b9b2a76f6b1fefe06012da27c2"
EXECUTION_PROFILE_HASH = "815525c081252c97689370d068ae0fb2595c0a7ba2e98333d08fb40b92697420"


def _payload(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(tmp_path: Path, payload: object, name: str = "input.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_candidate_registry_is_only_luna_and_sonnet() -> None:
    registry = load_model_registry(REGISTRY)

    assert [model.name for model in registry.models] == ["codex-luna", "claude-sonnet"]
    assert [model.provider for model in registry.models] == ["codex-cli", "claude-cli"]
    assert registry.models[0].requested_model == "gpt-5.6-luna"
    assert registry.models[1].requested_model == "sonnet"
    assert registry.models[0].effort == "high"
    assert registry.models[1].effort is None


def test_selected_profile_hash_golden_preserves_declared_order() -> None:
    models = load_model_registry(REGISTRY).models

    assert selected_profile_hash(models) == SELECTED_PROFILE_HASH
    assert selected_profile_hash(list(reversed(models))) != SELECTED_PROFILE_HASH


def test_unselected_registry_entry_does_not_change_selected_hash() -> None:
    production = load_model_registry(REGISTRY)
    three = load_model_registry(THREE_MODELS)

    selected = three.select(["codex-luna", "claude-sonnet"])
    assert selected_profile_hash(selected) == selected_profile_hash(production.models)


def test_three_profile_fixture_dispatches_both_claude_profiles_by_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cross the legacy alias table: only the Codex profile looks like Claude by alias.
    monkeypatch.setattr(
        "measure_twice.runner.CLAUDE_ALIASES",
        frozenset({"codex-luna", "gpt-5.6-luna"}),
    )
    registry = load_model_registry(THREE_MODELS)
    calls: list[tuple[str, str]] = []

    def codex(model: ModelSpec) -> str:
        name = model.name
        calls.append(("codex", name))
        return f"codex:{name}"

    def claude(model: ModelSpec) -> str:
        name = model.name
        calls.append(("claude", name))
        return f"claude:{name}"

    results = [
        dispatch_by_provider(model, {"codex-cli": codex, "claude-cli": claude})
        for model in registry.models
    ]

    assert results == ["codex:codex-luna", "claude:claude-sonnet", "claude:claude-haiku"]
    assert calls == [
        ("codex", "codex-luna"),
        ("claude", "claude-sonnet"),
        ("claude", "claude-haiku"),
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update({"extra": 1}), "unknown"),
        (lambda data: data.pop("models"), "missing"),
        (lambda data: data.update({"schema_version": 2}), "unsupported"),
        (lambda data: data.update({"schema_version": True}), "integer 1"),
        (lambda data: data.update({"models": {}}), "must be a list"),
        (lambda data: data.update({"models": []}), "at least one"),
    ],
)
def test_registry_rejects_bad_top_level_contract(
    tmp_path: Path, mutation: object, message: str
) -> None:
    payload = _payload(REGISTRY)
    assert callable(mutation)
    mutation(payload)

    with pytest.raises(ModelSpecError, match=message):
        load_model_registry(_write(tmp_path, payload))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "Claude-Sonnet", "must match"),
        ("name", "a" * 65, "1-64"),
        ("provider", "local", "one of"),
        ("requested_model", " ", "non-empty"),
        ("effort", 3, "non-empty"),
        ("execution_profile_id", "../escape", "must match"),
    ],
)
def test_registry_rejects_bad_model_fields(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    payload = _payload(REGISTRY)
    models = payload["models"]
    assert isinstance(models, list) and isinstance(models[0], dict)
    models[0][field] = value

    with pytest.raises(ModelSpecError, match=message):
        load_model_registry(_write(tmp_path, payload))


def test_registry_rejects_unknown_missing_and_duplicate_model_ids(tmp_path: Path) -> None:
    payload = _payload(REGISTRY)
    models = payload["models"]
    assert isinstance(models, list) and isinstance(models[0], dict)
    models[0]["command"] = "arbitrary"
    with pytest.raises(ModelSpecError, match="unknown"):
        load_model_registry(_write(tmp_path, payload, "unknown.json"))

    payload = _payload(REGISTRY)
    models = payload["models"]
    assert isinstance(models, list) and isinstance(models[0], dict)
    models[0].pop("effort")
    with pytest.raises(ModelSpecError, match="missing"):
        load_model_registry(_write(tmp_path, payload, "missing.json"))

    payload = _payload(REGISTRY)
    models = payload["models"]
    assert isinstance(models, list) and isinstance(models[0], dict) and isinstance(models[1], dict)
    models[1]["name"] = models[0]["name"]
    with pytest.raises(ModelSpecError, match="duplicate model profile"):
        load_model_registry(_write(tmp_path, payload, "duplicate.json"))


def test_registry_rejects_duplicate_json_keys_and_nonfinite_numbers(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate-key.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1,"models":[]}', encoding="utf-8")
    with pytest.raises(ModelSpecError, match="duplicate JSON key"):
        load_model_registry(duplicate)

    nonfinite = tmp_path / "nan.json"
    nonfinite.write_text('{"schema_version":NaN,"models":[]}', encoding="utf-8")
    with pytest.raises(ModelSpecError, match="non-finite"):
        load_model_registry(nonfinite)


def test_registry_selection_rejects_duplicates_unknown_and_unsafe_ids() -> None:
    registry = load_model_registry(REGISTRY)

    with pytest.raises(ModelSpecError, match="duplicate selected"):
        registry.select(["codex-luna", "codex-luna"])
    with pytest.raises(ModelSpecError, match="unknown selected"):
        registry.select(["claude-haiku"])
    with pytest.raises(ModelSpecError, match="must match"):
        registry.select(["../codex-luna"])


def test_execution_profile_frozen_defaults_and_hash() -> None:
    profile = load_execution_profile(EXECUTION)

    assert isinstance(profile, ExecutionProfile)
    assert execution_profile_hash(profile) == EXECUTION_PROFILE_HASH
    assert profile.qualification_limits.agent_timeout_s == 60
    assert profile.run_policy["smoke"].wall_s == 600
    assert profile.run_policy["observation"].terminal_cells == 192
    assert profile.ceilings.evaluator_cpu_s == 60
    assert profile.ceilings.evaluator_memory_bytes == 1_073_741_824
    assert profile.ceilings.evaluator_processes == 64
    assert profile.ceilings.evaluator_files == 10_000
    assert profile.ceilings.evaluator_file_bytes == 10_485_760
    assert profile.ceilings.evaluator_cpu_bandwidth_percent == 100
    assert profile.ceilings.evaluator_tmpfs_bytes == 67_108_864
    assert profile.ceilings.evaluator_tmpfs_inodes == 20_001


def test_execution_profile_hash_covers_nested_policy(tmp_path: Path) -> None:
    original = load_execution_profile(EXECUTION)
    payload = _payload(EXECUTION)
    policies = payload["run_policy"]
    assert isinstance(policies, dict) and isinstance(policies["smoke"], dict)
    policies["smoke"]["wall_s"] = 601
    changed = load_execution_profile(_write(tmp_path, payload))

    assert changed.sha256 != original.sha256


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("repetitions",), True, "must equal 2"),
        (("concurrency",), 1.0, "must equal 1"),
        (("retry", "retry_after_cap_s"), 60.0, "must equal 60"),
        (("run_policy", "smoke", "terminal_cells"), 3, "must equal task_count"),
        (("run_policy", "observation", "max_cells_per_tranche"), 31, "may not split"),
        (("run_policy", "smoke", "provider_attempts"), 3, "provider_attempts"),
        (("ceilings", "patch_bytes"), False, "positive integer"),
    ],
)
def test_execution_profile_rejects_wrong_types_and_arithmetic(
    tmp_path: Path, path: tuple[str, ...], value: object, message: str
) -> None:
    payload = _payload(EXECUTION)
    target: dict[str, object] = payload
    for component in path[:-1]:
        nested = target[component]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    with pytest.raises(ModelSpecError, match=message):
        load_execution_profile(_write(tmp_path, payload))


def test_execution_profile_rejects_unknown_nested_and_missing_keys(tmp_path: Path) -> None:
    payload = _payload(EXECUTION)
    ceilings = payload["ceilings"]
    assert isinstance(ceilings, dict)
    ceilings["mystery"] = 1
    with pytest.raises(ModelSpecError, match="unknown"):
        load_execution_profile(_write(tmp_path, payload, "unknown.json"))

    payload = _payload(EXECUTION)
    retry = payload["retry"]
    assert isinstance(retry, dict)
    retry.pop("default_delay_s")
    with pytest.raises(ModelSpecError, match="missing"):
        load_execution_profile(_write(tmp_path, payload, "missing.json"))


def test_shape_constants_resolve_to_one_shared_object() -> None:
    """Identity, not equality: a re-duplicated copy fails here even while the values agree.

    Both constants gate the same invariant from two modules -- ``SANDBOX_CONTRACT_VERSION``
    is the persisted contract marker validated in models.py and exported by isolation.py,
    and ``EVALUATOR_DIRECTORY_ALLOWANCE`` sizes the evaluator tmpfs inode budget that
    models.py validates and process.py mounts against. An ``==`` assertion would keep
    passing through the drift window this guards.
    """

    assert isolation_module.SANDBOX_CONTRACT_VERSION is models_module.SANDBOX_CONTRACT_VERSION
    assert (
        process_module.EVALUATOR_DIRECTORY_ALLOWANCE is models_module.EVALUATOR_DIRECTORY_ALLOWANCE
    )


def test_tmpfs_byte_floor_has_one_owner_across_modules() -> None:
    """Identity, not equality, for the FUNCTION that computes the tmpfs byte floor.

    The sibling of the constant test above.  Config-load validation (``Ceilings``) and launch
    validation (``EvaluatorScratch`` / ``_validate_evaluator_tmpfs``) must compute the identical
    floor; when they did not, a profile could load cleanly and then die with
    ``ProcessContractError`` inside ``build_evaluator_sandbox``.
    """

    assert (
        process_module.evaluator_tmpfs_minimum_bytes is models_module.evaluator_tmpfs_minimum_bytes
    )


def test_directory_allowance_is_derived_from_the_walker_tree_bound() -> None:
    """The inode allowance is exactly the walker's whole-tree directory bound plus the root inode.

    That derivation used to live only in prose.  Raising ``_MAX_TREE_DIRECTORIES`` without raising
    the allowance silently under-provisions the tmpfs so a structurally LEGAL tree hits the
    physical inode envelope -- the same cross-module drift the constant dedup closed, one file
    over from where it was fixed.
    """

    assert (
        models_module.EVALUATOR_DIRECTORY_ALLOWANCE == capabilities_module._MAX_TREE_DIRECTORIES + 1
    )


def test_ceilings_accepts_exactly_what_the_evaluator_scratch_requires() -> None:
    """Producer -> consumer round trip on the tmpfs byte bound.

    The floor is asserted against an independently written formula, not against the helper, so
    this fails if the shared helper's arithmetic changes -- not merely if the two callers drift.
    """

    files = 7
    file_bytes = 64 * 1024
    page_size = models_module.evaluator_page_size()
    expected_floor = file_bytes + files * page_size
    assert (
        models_module.evaluator_tmpfs_minimum_bytes(file_bytes=file_bytes, files=files)
        == expected_floor
    )

    def _ceilings(tmpfs_bytes: int) -> Ceilings:
        return Ceilings(
            changed_paths=100,
            patch_bytes=5 * 1024 * 1024,
            stream_bytes_each=10 * 1024 * 1024,
            cell_artifact_bytes=25 * 1024 * 1024,
            evaluator_cpu_s=2,
            evaluator_memory_bytes=256 * 1024 * 1024,
            evaluator_processes=16,
            evaluator_files=files,
            evaluator_file_bytes=file_bytes,
            evaluator_cpu_bandwidth_percent=100,
            evaluator_tmpfs_bytes=tmpfs_bytes,
            evaluator_tmpfs_inodes=files + models_module.EVALUATOR_DIRECTORY_ALLOWANCE,
        )

    # Exactly at the floor loads...
    assert _ceilings(expected_floor).evaluator_tmpfs_bytes == expected_floor
    # ...and one byte under is rejected AT LOAD, not later at evaluator launch.
    with pytest.raises(ModelSpecError, match="per-file pages"):
        _ceilings(expected_floor - 1)
