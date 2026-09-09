"""Strict Instrument A execution-profile and canonical-hash tests."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from measure_twice.config import load_config
from measure_twice.model_sweep_execution import (
    CLAUDE_ARGV_TEMPLATE,
    CLAUDE_ENV_ALLOWLIST,
    DEFAULT_EXECUTION_PROFILE,
    PROVIDER_CLAUDE,
    PROVIDER_LOCAL,
    ExecutionProfileError,
    ExecutionReceipt,
    ModelBinding,
    ModelSweepExecutionProfile,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "model-sweep-execution-v1.json"
PROFILE_SHA256 = "4f99e86d192d6534ef1ee3337e19aab263d52b97196efc7c262e711aa7091526"
PROVIDER_PROFILE_SHA256 = "f6b6c6c9d62c45c45cc6e5f921c6bf5a6830f3c89ec6f3f7af9ce6c987ac8012"
CONTEXT_PROFILE_SHA256 = "aaca7846b51b89a9420ab682a44398c3ddd02bff5d29f25af11a7804d45a5df3"


def _profile_mapping() -> dict[str, object]:
    return copy.deepcopy(DEFAULT_EXECUTION_PROFILE.to_mapping())


def test_committed_execution_profile_matches_defaults_and_frozen_hashes() -> None:
    config = load_config(str(PROFILE_PATH))
    profile = config.execution_profile

    assert profile == DEFAULT_EXECUTION_PROFILE
    assert profile.sha256 == PROFILE_SHA256
    assert profile.provider_profile_sha256 == PROVIDER_PROFILE_SHA256
    assert profile.claude.sha256 == CONTEXT_PROFILE_SHA256


def test_profile_dispatches_only_by_explicit_provider_binding() -> None:
    profile = DEFAULT_EXECUTION_PROFILE
    assert profile.binding_for("general-35b").provider == PROVIDER_LOCAL
    assert profile.binding_for("general-35b").requested_model == "general-35b"
    assert profile.binding_for("sonnet").provider == PROVIDER_CLAUDE
    assert profile.binding_for("sonnet").requested_model == "sonnet"

    with pytest.raises(ExecutionProfileError, match="no explicit provider binding"):
        profile.binding_for("unknown-model")


def test_profile_rejects_unknown_provider() -> None:
    payload = _profile_mapping()
    models = payload["models"]
    assert isinstance(models, list)
    first = models[0]
    assert isinstance(first, dict)
    first["provider"] = "mystery-provider"

    with pytest.raises(ExecutionProfileError, match="provider must be one of"):
        ModelSweepExecutionProfile.from_mapping(payload)


def test_profile_rejects_non_string_provider_with_profile_error() -> None:
    payload = _profile_mapping()
    models = payload["models"]
    assert isinstance(models, list)
    first = models[0]
    assert isinstance(first, dict)
    first["provider"] = ["local-openai"]

    with pytest.raises(ExecutionProfileError, match="provider must be one of"):
        ModelSweepExecutionProfile.from_mapping(payload)


@pytest.mark.parametrize(
    "requested_model",
    [
        "sonnet model",
        "sonnet&whoami",
        "sonnet|whoami",
        "sonnet>stolen.txt",
        "sonnet%PATH%",
        "sonnet!PATH!",
        "sonnet$(whoami)",
        "sonnet;whoami",
        "sonnet`whoami`",
        "../sonnet",
        "--version",
    ],
)
def test_claude_binding_rejects_hostile_requested_model_tokens(requested_model: str) -> None:
    payload = _profile_mapping()
    models = payload["models"]
    assert isinstance(models, list)
    claude = next(
        model
        for model in models
        if isinstance(model, dict) and model["provider"] == PROVIDER_CLAUDE
    )
    claude["requested_model"] = requested_model

    with pytest.raises(ExecutionProfileError, match="safe Claude model token"):
        ModelSweepExecutionProfile.from_mapping(payload)


def test_provider_model_grammar_admits_claude_ids_and_namespaced_local_ids() -> None:
    claude = ModelBinding("future-sonnet", PROVIDER_CLAUDE, "claude-sonnet-4-5-20250929")
    local = ModelBinding("namespaced-local", PROVIDER_LOCAL, "registry.example/org/model:tag")

    assert claude.requested_model == "claude-sonnet-4-5-20250929"
    assert local.requested_model == "registry.example/org/model:tag"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (("extra",), "keys must be exactly"),
        (("models", 0, "extra"), "keys must be exactly"),
        (("claude", "extra"), "keys must be exactly"),
    ],
)
def test_profile_rejects_unknown_keys(path: tuple[object, ...], message: str) -> None:
    payload: object = _profile_mapping()
    target = payload
    for component in path[:-1]:
        if isinstance(component, int):
            assert isinstance(target, list)
            target = target[component]
        else:
            assert isinstance(target, dict)
            target = target[component]
    assert isinstance(target, dict)
    target[str(path[-1])] = "unexpected"

    with pytest.raises(ExecutionProfileError, match=message):
        ModelSweepExecutionProfile.from_mapping(payload)


def test_profile_rejects_duplicate_alias_and_context_weakening() -> None:
    duplicate = _profile_mapping()
    models = duplicate["models"]
    assert isinstance(models, list)
    models.append(copy.deepcopy(models[0]))
    with pytest.raises(ExecutionProfileError, match="duplicate alias"):
        ModelSweepExecutionProfile.from_mapping(duplicate)

    weakened = _profile_mapping()
    claude = weakened["claude"]
    assert isinstance(claude, dict)
    argv = claude["argv_template"]
    assert isinstance(argv, list)
    argv.remove("--safe-mode")
    with pytest.raises(ExecutionProfileError, match="frozen prompt-only"):
        ModelSweepExecutionProfile.from_mapping(weakened)


def test_context_contract_pins_tools_customizations_sessions_and_allowlist() -> None:
    argv = CLAUDE_ARGV_TEMPLATE
    assert "--safe-mode" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in argv
    assert "--no-chrome" in argv
    assert "--no-session-persistence" in argv
    assert "--bare" not in argv
    assert "ANTHROPIC_API_KEY" not in CLAUDE_ENV_ALLOWLIST
    assert "CLAUDE_CONFIG_DIR" not in CLAUDE_ENV_ALLOWLIST


def test_execution_receipt_round_trips_and_rejects_tampering() -> None:
    binding = DEFAULT_EXECUTION_PROFILE.binding_for("sonnet")
    receipt = ExecutionReceipt.create(
        DEFAULT_EXECUTION_PROFILE,
        [binding],
        claude_executable="C:/sealed/claude.exe",
        claude_version="9.8.7",
    )
    wire = receipt.to_mapping()

    assert ExecutionReceipt.from_mapping(wire) == receipt
    assert wire["receipt_sha256"] == receipt.receipt_sha256

    tampered = copy.deepcopy(wire)
    bindings = tampered["bindings"]
    assert isinstance(bindings, list)
    first = bindings[0]
    assert isinstance(first, dict)
    first["requested_model"] = "opus"
    with pytest.raises(ExecutionProfileError, match="does not match its canonical payload"):
        ExecutionReceipt.from_mapping(tampered)


def test_execution_receipt_rejects_binding_not_owned_by_profile() -> None:
    forged = ModelBinding("sonnet", PROVIDER_CLAUDE, "different-requested-model")
    with pytest.raises(ExecutionProfileError, match="differs from the selected execution profile"):
        ExecutionReceipt.create(
            DEFAULT_EXECUTION_PROFILE,
            [forged],
            claude_executable="C:/sealed/claude.exe",
            claude_version="9.8.7",
        )
