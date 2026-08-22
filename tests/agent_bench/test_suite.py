from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from measure_twice.agent_bench import suite as suite_module
from measure_twice.agent_bench.suite import (
    ALWAYS_PROTECTED_PATHS,
    EVALUATOR_ARGV,
    AgentSuiteError,
    glob_matches,
    instrument_preimage,
    is_change_allowed,
    load_agent_suite,
    validate_allowed_change_glob,
    validate_relative_path,
)

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "suites" / "agents" / "smoke"
INSTRUMENT_HASH = "e5eceae0a8f75a685c4005b1028d86923d8eb4ca0d4665b99295e6a701cbc090"


def _bundle(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    destination = tmp_path / "smoke"
    shutil.copytree(SMOKE, destination)
    return destination


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _task_manifest(bundle: Path) -> Path:
    return bundle / "tasks" / "smoke-add" / "task.json"


def test_smoke_bundle_contract_and_instrument_hash_golden() -> None:
    suite = load_agent_suite(SMOKE)

    assert suite.suite_id == "agent-smoke"
    assert suite.run_class == "smoke"
    assert suite.runtime.to_mapping() == {"language": "python", "version": "3.12"}
    assert suite.evaluator_version == "python-pytest-v1"
    assert suite.tasks == ["tasks/smoke-add/task.json"]
    assert [task.task_id for task in suite.loaded_tasks] == ["smoke-add"]
    assert suite.instrument_hash == INSTRUMENT_HASH
    assert set(suite.task_hashes) == {"smoke-add"}


def test_evaluator_argv_layout_is_frozen_and_not_task_authored() -> None:
    assert EVALUATOR_ARGV == (
        "/opt/measure-twice/runtime/python3.12",
        "-B",
        "-s",
        "-P",
        "-m",
        "pytest",
        "-q",
        "--disable-warnings",
        "--maxfail=1",
        "--basetemp=/tmp/pytest",
        "/workspace/tests",
        "/opt/measure-twice/oracle/tests",
    )
    task_mapping = load_agent_suite(SMOKE).task_specs[0].to_mapping()
    assert "command" not in task_mapping
    assert "argv" not in task_mapping


def test_instrument_preimage_is_documented_path_and_bytes_only() -> None:
    preimage = instrument_preimage(load_agent_suite(SMOKE))
    tasks = preimage["tasks"]
    assert isinstance(tasks, list) and isinstance(tasks[0], dict)
    assets = tasks[0]["assets"]
    assert isinstance(assets, list)
    paths = [asset["path"] for asset in assets]

    assert paths == sorted(paths, key=lambda path: path.encode("utf-8"))
    assert all(set(asset) == {"path", "sha256", "size_bytes"} for asset in assets)
    assert tasks[0]["manifest_path"] == "tasks/smoke-add/task.json"


def test_instrument_hash_ignores_json_whitespace(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    baseline = load_agent_suite(bundle).instrument_hash
    manifest = _task_manifest(bundle)
    _write(manifest, _read(manifest))

    assert load_agent_suite(bundle).instrument_hash == baseline


def test_instrument_hash_ignores_host_mode_when_chmod_changes_it(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    baseline = load_agent_suite(bundle).instrument_hash
    calculator = bundle / "tasks" / "smoke-add" / "seed" / "calculator.py"
    original_mode = stat.S_IMODE(calculator.stat().st_mode)
    changed_mode = original_mode ^ stat.S_IWUSR
    try:
        os.chmod(calculator, changed_mode)
        observed_mode = stat.S_IMODE(calculator.stat().st_mode)
        if observed_mode == original_mode:
            pytest.skip("host filesystem did not apply the requested mode change")

        assert load_agent_suite(bundle).instrument_hash == baseline
    finally:
        os.chmod(calculator, original_mode)


def test_recursive_asset_and_constraint_changes_change_instrument_hash(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    baseline = load_agent_suite(bundle).instrument_hash
    nested = bundle / "tasks" / "smoke-add" / "seed" / "tests" / "test_calculator.py"
    nested.write_text(nested.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert load_agent_suite(bundle).instrument_hash != baseline

    bundle = _bundle(tmp_path / "constraints")
    baseline = load_agent_suite(bundle).instrument_hash
    manifest = _task_manifest(bundle)
    payload = _read(manifest)
    allowed = payload["allowed_changes"]
    assert isinstance(allowed, list)
    allowed.append("lib/**/*.py")
    _write(manifest, payload)
    assert load_agent_suite(bundle).instrument_hash != baseline


def test_model_registry_content_is_outside_suite_identity(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    before = load_agent_suite(bundle).instrument_hash
    unrelated_registry = tmp_path / "profiles.json"
    unrelated_registry.write_text('{"models":["anything"]}', encoding="utf-8")

    assert load_agent_suite(bundle).instrument_hash == before


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("calculator.py", "calculator.py", True),
        ("calculator.py", "src/calculator.py", False),
        ("src/*.py", "src/main.py", True),
        ("src/*.py", "src/deep/main.py", False),
        ("src/file?.py", "src/file1.py", True),
        ("src/file?.py", "src/file10.py", False),
        ("src/**/test_*.py", "src/test_one.py", True),
        ("src/**/test_*.py", "src/a/b/test_one.py", True),
        ("**", "root.py", True),
        ("**", "a/b/root.py", True),
        ("Src/**/*.py", "src/main.py", False),
    ],
)
def test_allowed_glob_golden(pattern: str, path: str, expected: bool) -> None:
    assert glob_matches(pattern, path) is expected


def test_allowed_glob_handles_thousands_of_segments_without_recursion() -> None:
    deep_path = "/".join(["x"] * 1_500)
    many_double_stars = "/".join(["**"] * 1_500)

    assert glob_matches("**", deep_path) is True
    assert glob_matches(many_double_stars, "x") is True
    assert glob_matches(f"{many_double_stars}/target", "x/target") is True


@pytest.mark.parametrize(
    "pattern",
    ["/absolute", "../escape", "a//b", "a\\b", "C:/drive", "a:[x]", "a/[bc].py", "a/**x"],
)
def test_allowed_glob_rejects_unsafe_or_unsupported_grammar(pattern: str) -> None:
    with pytest.raises(AgentSuiteError):
        validate_allowed_change_glob(pattern, label="pattern")


@pytest.mark.parametrize(
    "path",
    ["/absolute", "//server/share", "../escape", "a/../b", "a//b", "a\\b", "C:/drive"],
)
def test_relative_paths_reject_escape_and_windows_forms(path: str) -> None:
    with pytest.raises(AgentSuiteError):
        validate_relative_path(path, label="path")


def test_protected_paths_override_allowed_globs() -> None:
    task = load_agent_suite(SMOKE).task_specs[0]
    task.allowed_changes[:] = ["**"]

    assert is_change_allowed(task, "calculator.py") is True
    for protected in ALWAYS_PROTECTED_PATHS:
        assert is_change_allowed(task, protected) is False
    assert is_change_allowed(task, ".git/config") is False
    assert is_change_allowed(task, "tests/test_calculator.py") is False
    assert is_change_allowed(task, "README.md") is False


@pytest.mark.parametrize(
    ("target", "field", "value", "message"),
    [
        ("suite", "schema_version", 2, "unsupported"),
        ("suite", "suite_id", "Agent-Smoke", "must match"),
        ("suite", "run_class", "ranking", "one of"),
        ("suite", "tasks", "task.json", "non-empty list"),
        ("task", "schema_version", True, "integer 1"),
        ("task", "task_id", "../bad", "must match"),
        ("task", "family", "shell", "one of"),
        ("task", "tags", ["BadTag"], "must match"),
        ("task", "allowed_changes", "calculator.py", "non-empty list"),
        ("task", "protected_paths", {}, "must be a list"),
    ],
)
def test_suite_and_task_reject_bad_types_ids_and_versions(
    tmp_path: Path, target: str, field: str, value: object, message: str
) -> None:
    bundle = _bundle(tmp_path)
    manifest = bundle / "suite.json" if target == "suite" else _task_manifest(bundle)
    payload = _read(manifest)
    payload[field] = value
    _write(manifest, payload)

    with pytest.raises(AgentSuiteError, match=message):
        load_agent_suite(bundle)


def test_strict_manifests_reject_unknown_missing_and_duplicate_json_keys(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "unknown")
    manifest = _task_manifest(bundle)
    payload = _read(manifest)
    payload["command"] = "python -m pytest"
    _write(manifest, payload)
    with pytest.raises(AgentSuiteError, match="unknown"):
        load_agent_suite(bundle)

    bundle = _bundle(tmp_path / "missing")
    manifest = _task_manifest(bundle)
    payload = _read(manifest)
    payload.pop("reference_patch")
    _write(manifest, payload)
    with pytest.raises(AgentSuiteError, match="missing"):
        load_agent_suite(bundle)

    bundle = _bundle(tmp_path / "duplicate-key")
    manifest = _task_manifest(bundle)
    raw = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        raw.replace('"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,'),
        encoding="utf-8",
    )
    with pytest.raises(AgentSuiteError, match="duplicate JSON key"):
        load_agent_suite(bundle)


def test_duplicate_task_manifest_paths_and_task_ids_fail(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "paths")
    suite_manifest = bundle / "suite.json"
    payload = _read(suite_manifest)
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks.append(tasks[0])
    _write(suite_manifest, payload)
    with pytest.raises(AgentSuiteError, match="duplicate manifest paths"):
        load_agent_suite(bundle)

    bundle = _bundle(tmp_path / "ids")
    task_copy = bundle / "tasks" / "smoke-add" / "task-copy.json"
    shutil.copyfile(_task_manifest(bundle), task_copy)
    suite_manifest = bundle / "suite.json"
    payload = _read(suite_manifest)
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks.append("tasks/smoke-add/task-copy.json")
    _write(suite_manifest, payload)
    with pytest.raises(AgentSuiteError, match="duplicate task id"):
        load_agent_suite(bundle)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("prompt", "../prompt.md", "may not contain"),
        ("seed", "C:/seed", "may not contain ':'"),
        ("oracle", "//server/oracle", "must be relative"),
        ("reference_patch", "reference\\patch", "backslashes"),
    ],
)
def test_task_asset_paths_cannot_escape(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    bundle = _bundle(tmp_path)
    manifest = _task_manifest(bundle)
    payload = _read(manifest)
    payload[field] = value
    _write(manifest, payload)

    with pytest.raises(AgentSuiteError, match=message):
        load_agent_suite(bundle)


def test_missing_assets_and_evaluator_test_layout_fail(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "missing")
    (bundle / "tasks" / "smoke-add" / "prompt.md").unlink()
    with pytest.raises(AgentSuiteError, match="missing"):
        load_agent_suite(bundle)

    bundle = _bundle(tmp_path / "visible")
    shutil.rmtree(bundle / "tasks" / "smoke-add" / "seed" / "tests")
    with pytest.raises(AgentSuiteError, match="visible tests"):
        load_agent_suite(bundle)

    bundle = _bundle(tmp_path / "hidden")
    shutil.rmtree(bundle / "tasks" / "smoke-add" / "oracle" / "tests")
    with pytest.raises(AgentSuiteError, match="hidden tests"):
        load_agent_suite(bundle)


def test_wrong_case_asset_component_fails_even_on_case_insensitive_filesystems(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    manifest = _task_manifest(bundle)
    payload = _read(manifest)
    payload["prompt"] = "Prompt.md"
    _write(manifest, payload)

    with pytest.raises(AgentSuiteError, match=r"wrong case|missing"):
        load_agent_suite(bundle)


def test_pinned_bundle_root_never_reads_a_replacement_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path / "original")
    original_hash = load_agent_suite(bundle).instrument_hash
    replacement = _bundle(tmp_path / "replacement")
    replacement_calculator = replacement / "tasks" / "smoke-add" / "seed" / "calculator.py"
    replacement_calculator.write_text(
        "def add(left: int, right: int) -> int:\n    return 999\n", encoding="utf-8"
    )
    replacement_hash = load_agent_suite(replacement).instrument_hash
    assert replacement_hash != original_hash

    parked = bundle.with_name("original-pinned")
    real_resolve = suite_module._resolve_contained
    swapped = False

    def swap_after_root_pin(
        root: suite_module._ContainedRoot,
        relative: str,
        *,
        label: str,
        expected: Literal["file", "directory"],
        final_identity: tuple[int, int] | None = None,
    ) -> suite_module._ContainedNode:
        nonlocal swapped
        if not swapped and relative == "suite.json":
            bundle.rename(parked)
            replacement.rename(bundle)
            swapped = True
        return real_resolve(
            root,
            relative,
            label=label,
            expected=expected,
            final_identity=final_identity,
        )

    monkeypatch.setattr(suite_module, "_resolve_contained", swap_after_root_pin)

    observed = load_agent_suite(bundle).instrument_hash

    assert swapped is True
    assert observed == original_hash
    assert observed != replacement_hash


def test_absolute_ancestor_swap_cannot_redirect_root_pinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validated_parent = tmp_path / "validated-parent"
    bundle = _bundle(validated_parent)
    original_hash = load_agent_suite(bundle).instrument_hash
    replacement_parent = tmp_path / "replacement-parent"
    replacement = _bundle(replacement_parent)
    replacement_calculator = replacement / "tasks" / "smoke-add" / "seed" / "calculator.py"
    replacement_calculator.write_text(
        "def add(left: int, right: int) -> int:\n    return 999\n", encoding="utf-8"
    )
    replacement_hash = load_agent_suite(replacement).instrument_hash
    assert replacement_hash != original_hash

    parked_parent = tmp_path / "validated-parent-pinned"
    real_open_root = suite_module._native_open_root
    real_open_relative = suite_module._native_open_relative
    swapped = False

    def swap_ancestor() -> None:
        nonlocal swapped
        validated_parent.rename(parked_parent)
        replacement_parent.rename(validated_parent)
        swapped = True

    def swap_during_root_open(path: Path) -> int:
        if not swapped and path == bundle:
            swap_ancestor()
        return real_open_root(path)

    def swap_during_component_open(parent_handle: int, component: str, *, directory: bool) -> int:
        if not swapped and component == validated_parent.name:
            swap_ancestor()
        return real_open_relative(parent_handle, component, directory=directory)

    monkeypatch.setattr(suite_module, "_native_open_root", swap_during_root_open)
    monkeypatch.setattr(suite_module, "_native_open_relative", swap_during_component_open)

    try:
        observed = load_agent_suite(bundle).instrument_hash
    except AgentSuiteError:
        observed = None

    assert swapped is True
    assert observed in {None, original_hash}
    assert observed != replacement_hash


def test_absolute_ancestor_components_are_exact_case(tmp_path: Path) -> None:
    exact_parent = tmp_path / "ExactAncestor"
    bundle = _bundle(exact_parent)
    wrong_case_bundle = tmp_path / "exactancestor" / bundle.name

    with pytest.raises(AgentSuiteError, match=r"wrong case|missing"):
        load_agent_suite(wrong_case_bundle)


def test_absolute_ancestor_link_is_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    bundle = _bundle(real_parent)
    linked_parent = tmp_path / "linked-parent"
    if os.name == "nt":
        result = subprocess.run(  # noqa: S603 - fixed Windows utility and literal operation
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(linked_parent), str(real_parent)],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"junction creation unavailable: {result.stderr}")
    else:
        try:
            linked_parent.symlink_to(real_parent, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlink creation unavailable: {exc}")

    with pytest.raises(AgentSuiteError, match=r"symlink|junction|reparse"):
        load_agent_suite(linked_parent / bundle.name)


def test_loaded_hashes_are_an_immutable_snapshot_after_bundle_replacement(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "original")
    loaded = load_agent_suite(bundle)
    expected_instrument_hash = loaded.instrument_hash
    expected_task_hashes = loaded.task_hashes

    parked = bundle.with_name("original-loaded")
    replacement = _bundle(tmp_path / "replacement")
    replacement_calculator = replacement / "tasks" / "smoke-add" / "seed" / "calculator.py"
    replacement_calculator.write_text(
        "def add(left: int, right: int) -> int:\n    return 999\n", encoding="utf-8"
    )
    bundle.rename(parked)
    replacement.rename(bundle)

    assert loaded.instrument_hash == expected_instrument_hash
    assert loaded.task_hashes == expected_task_hashes


def test_snapshot_validates_the_exact_prompt_bytes_that_it_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    prompt = bundle / "tasks" / "smoke-add" / "prompt.md"
    prompt_relative = "tasks/smoke-add/prompt.md"
    original_hash = load_agent_suite(bundle).instrument_hash
    real_capture = suite_module._capture_contained_file
    changed = False

    def empty_prompt_after_capture(
        suite_root: suite_module._ContainedRoot,
        relative: str,
        *,
        label: str,
    ) -> suite_module._CapturedAsset:
        nonlocal changed
        captured = real_capture(suite_root, relative, label=label)
        if relative == prompt_relative:
            prompt.write_bytes(b"")
            changed = True
        return captured

    monkeypatch.setattr(suite_module, "_capture_contained_file", empty_prompt_after_capture)

    loaded = load_agent_suite(bundle)
    assert changed is True
    assert loaded.instrument_hash == original_hash


def test_tree_walk_rejects_a_listed_file_replaced_by_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    seed = bundle / "tasks" / "smoke-add" / "seed"
    calculator = seed / "calculator.py"
    parked = seed / "calculator-listed"
    real_open = suite_module._open_contained_handle
    swapped = False

    def swap_file_after_listing(
        parent: suite_module._ContainedNode,
        component: str,
        *,
        directory: bool,
        label: str,
        expected_entry: suite_module._DirectoryEntry | None = None,
    ) -> suite_module._ContainedNode:
        nonlocal swapped
        if not swapped and component == "calculator.py" and parent.display_path == seed:
            calculator.rename(parked)
            calculator.mkdir()
            swapped = True
        return real_open(
            parent,
            component,
            directory=directory,
            label=label,
            expected_entry=expected_entry,
        )

    monkeypatch.setattr(suite_module, "_open_contained_handle", swap_file_after_listing)

    with pytest.raises(AgentSuiteError, match=r"changed identity|regular file|open contained"):
        load_agent_suite(bundle)
    assert swapped is True


def test_pinned_parent_handle_never_reads_a_replacement_at_leaf_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    seed = bundle / "tasks" / "smoke-add" / "seed"
    parked_seed = seed.with_name("seed-validated")
    outside_seed = tmp_path / "outside-seed"
    shutil.copytree(seed, outside_seed)
    (outside_seed / "calculator.py").write_text(
        "def add(left: int, right: int) -> int:\n    return 999\n", encoding="utf-8"
    )
    expected_bytes = (seed / "calculator.py").read_bytes()
    replacement_bytes = (outside_seed / "calculator.py").read_bytes()
    assert replacement_bytes != expected_bytes
    swap_link = tmp_path / "seed-link"
    if os.name == "nt":
        result = subprocess.run(  # noqa: S603 - fixed Windows utility and literal operation
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(swap_link), str(outside_seed)],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"junction creation unavailable: {result.stderr}")
    else:
        try:
            swap_link.symlink_to(outside_seed, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlink creation unavailable: {exc}")

    real_open = suite_module._open_contained_handle
    swapped = False
    seam_outcome: bytes | str | None = None

    def swap_seed_tree() -> None:
        seed.rename(parked_seed)
        swap_link.rename(seed)

    def restore_seed_tree() -> None:
        seed.rename(swap_link)
        parked_seed.rename(seed)

    def record_probe(
        open_current: Callable[[], suite_module._ContainedNode],
    ) -> None:
        nonlocal seam_outcome
        try:
            probe = open_current()
        except AgentSuiteError:
            seam_outcome = "failed-closed"
            raise
        try:
            seam_outcome = suite_module._native_read_file(probe.handle)
        finally:
            probe.close()

    def swap_during_open(
        parent: suite_module._ContainedNode,
        component: str,
        *,
        directory: bool,
        label: str,
        expected_entry: suite_module._DirectoryEntry | None = None,
    ) -> suite_module._ContainedNode:
        nonlocal swapped

        def delegate() -> suite_module._ContainedNode:
            return real_open(
                parent,
                component,
                directory=directory,
                label=label,
                expected_entry=expected_entry,
            )

        if not swapped and component == "calculator.py" and parent.display_path == seed:
            swap_seed_tree()
            swapped = True
            try:
                record_probe(delegate)
            finally:
                restore_seed_tree()
        return delegate()

    monkeypatch.setattr(suite_module, "_open_contained_handle", swap_during_open)

    try:
        load_agent_suite(bundle)
    except AgentSuiteError:
        assert seam_outcome == "failed-closed"

    assert swapped is True
    assert seam_outcome in {"failed-closed", expected_bytes}
    assert seam_outcome != replacement_bytes


def test_contained_node_take_handle_transfers_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(suite_module, "_native_close_handle", closed.append)
    node = suite_module._ContainedNode(
        123,
        tmp_path,
        suite_module._HandleInfo(
            identity=(1, 2),
            is_directory=True,
            is_regular=False,
            is_reparse=False,
            size=0,
            mtime_token=0,
        ),
    )

    transferred = node.take_handle()
    node.close()

    assert transferred == 123
    assert closed == []
    with pytest.raises(AgentSuiteError, match="already closed"):
        _ = node.handle

    suite_module._native_close_handle(transferred)
    assert closed == [123]


@pytest.mark.parametrize(
    ("failure_type", "expected_type"),
    [(OSError, AgentSuiteError), (SystemExit, SystemExit)],
    ids=["os-error", "system-exit"],
)
def test_contained_load_closes_root_and_child_handles_on_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    expected_type: type[BaseException],
) -> None:
    bundle = _bundle(tmp_path)
    real_open_root = suite_module._native_open_root
    real_open_relative = suite_module._native_open_relative
    real_handle_info = suite_module._native_handle_info
    real_close = suite_module._native_close_handle
    opened: list[int] = []
    closed: list[int] = []
    root_handle: int | None = None

    def record_root(path: Path) -> int:
        nonlocal root_handle
        root_handle = real_open_root(path)
        opened.append(root_handle)
        return root_handle

    def record_child(parent_handle: int, component: str, *, directory: bool) -> int:
        handle = real_open_relative(parent_handle, component, directory=directory)
        opened.append(handle)
        return handle

    def fail_first_child(handle: int) -> suite_module._HandleInfo:
        if root_handle is not None and handle != root_handle:
            raise failure_type("deterministic child inspection failure")
        return real_handle_info(handle)

    def record_close(handle: int) -> None:
        closed.append(handle)
        real_close(handle)

    monkeypatch.setattr(suite_module, "_native_open_root", record_root)
    monkeypatch.setattr(suite_module, "_native_open_relative", record_child)
    monkeypatch.setattr(suite_module, "_native_handle_info", fail_first_child)
    monkeypatch.setattr(suite_module, "_native_close_handle", record_close)

    with pytest.raises(expected_type, match="deterministic child inspection failure"):
        load_agent_suite(bundle)

    assert len(opened) == 2
    assert sorted(closed) == sorted(opened)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended identity contract")
def test_windows_extended_identity_matches_enumerated_and_opened_nodes(tmp_path: Path) -> None:
    from measure_twice.agent_bench import _win32_contained

    directory = tmp_path / "ExactDirectory"
    directory.mkdir()
    leaf = directory / "asset.bin"
    leaf_bytes = b"\x00\xffidentity-probe\r\n"
    leaf.write_bytes(leaf_bytes)

    root_handle = _win32_contained.open_root(tmp_path)
    try:
        directory_entry = next(
            entry
            for entry in _win32_contained.list_directory(root_handle)
            if entry.name == directory.name
        )
        directory_handle = _win32_contained.open_relative(
            root_handle, directory.name, directory=True
        )
        try:
            assert (
                directory_entry.identity == _win32_contained.handle_info(directory_handle).identity
            )
            leaf_entry = next(
                entry
                for entry in _win32_contained.list_directory(directory_handle)
                if entry.name == leaf.name
            )
            leaf_handle = _win32_contained.open_relative(
                directory_handle, leaf.name, directory=False
            )
            try:
                assert leaf_entry.identity == _win32_contained.handle_info(leaf_handle).identity
                assert _win32_contained.read_file(leaf_handle) == leaf_bytes
            finally:
                _win32_contained.close_handle(leaf_handle)
        finally:
            _win32_contained.close_handle(directory_handle)
    finally:
        _win32_contained.close_handle(root_handle)


def test_symlinked_asset_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    prompt = bundle / "tasks" / "smoke-add" / "prompt.md"
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    prompt.unlink()
    try:
        prompt.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(AgentSuiteError, match=r"symlink|reparse"):
        load_agent_suite(bundle)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
@pytest.mark.parametrize("relative", ["tasks", "tasks/smoke-add/seed"])
def test_junctioned_asset_directory_is_rejected(tmp_path: Path, relative: str) -> None:
    bundle = _bundle(tmp_path)
    junction = bundle.joinpath(*relative.split("/"))
    original = junction.with_name(f"{junction.name}-original")
    junction.rename(original)
    result = subprocess.run(  # noqa: S603 - fixed Windows system utility and literal operation
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(original)],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr}")

    with pytest.raises(AgentSuiteError, match=r"junction|reparse"):
        load_agent_suite(bundle)
