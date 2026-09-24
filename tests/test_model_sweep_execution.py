"""Strict Instrument A execution-profile and canonical-hash tests."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from measure_twice.config import load_config
from measure_twice.model_sweep_execution import (
    DEFAULT_EXECUTION_PROFILE,
    DEFAULT_GEMINI_CONTEXT,
    PROVIDER_CLAUDE,
    PROVIDER_GEMINI,
    PROVIDER_LOCAL,
    ExecutionProfileError,
    ExecutionReceipt,
    GeminiContextProfile,
    ModelBinding,
    ModelSweepExecutionProfile,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "model-sweep-execution-v1.json"
PROFILE_SHA256 = "af438f4442ef35b7a41dfb090accc0e1188592aeabce8feab206602044a6e905"
PROVIDER_PROFILE_SHA256 = "f6b6c6c9d62c45c45cc6e5f921c6bf5a6830f3c89ec6f3f7af9ce6c987ac8012"
CONTEXT_PROFILE_SHA256 = "84a2b99593bf58f986c4f5cd818cc16e1c75d59d8f0b1fed5bc7ac2c6267bc6d"


def _profile_mapping() -> dict[str, object]:
    return copy.deepcopy(DEFAULT_EXECUTION_PROFILE.to_mapping())


def test_committed_execution_profile_matches_defaults_and_frozen_hashes() -> None:
    config = load_config(str(PROFILE_PATH))
    profile = config.execution_profile

    assert profile == DEFAULT_EXECUTION_PROFILE
    assert profile.sha256 == PROFILE_SHA256
    assert profile.provider_profile_sha256 == PROVIDER_PROFILE_SHA256
    assert profile.claude.sha256 == CONTEXT_PROFILE_SHA256
    # Additive compatibility (plan §6 D5): a Gemini-free profile serializes with NO gemini key and
    # its shared context-hash owner is EXACTLY the Claude context hash, so old hashes are unchanged.
    assert "gemini" not in profile.to_mapping()
    assert profile.gemini is None
    assert profile.context_profile_sha256 == CONTEXT_PROFILE_SHA256


def test_gemini_context_present_iff_gemini_binding() -> None:
    mapping = _profile_mapping()
    models = mapping["models"]
    assert isinstance(models, list)
    # A Gemini binding without a gemini context is rejected.
    models.append(
        {"alias": "gf", "provider": PROVIDER_GEMINI, "requested_model": "gemini-3.8-flash"}
    )
    with pytest.raises(ExecutionProfileError, match="must define a 'gemini' context"):
        ModelSweepExecutionProfile.from_mapping(mapping)
    # A gemini context with no Gemini binding is equally rejected.
    orphan = _profile_mapping()
    orphan["gemini"] = DEFAULT_GEMINI_CONTEXT.to_mapping()
    with pytest.raises(ExecutionProfileError, match="binds no 'gemini-api' model"):
        ModelSweepExecutionProfile.from_mapping(orphan)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        ({"request_contract": "other"}, "request_contract must equal"),
        ({"max_output_tokens": 0}, "max_output_tokens must be an int"),
        ({"max_output_tokens": True}, "max_output_tokens must be an integer"),
        ({"thinking_level": "extreme"}, "thinking_level must be one of"),
        ({"thinking_level": []}, "thinking_level must be one of"),
        ({"timeout_s": 0}, "timeout_s must be a finite positive"),
        ({"timeout_s": 10**400}, "timeout_s must be a finite positive"),
        ({"timeout_s": "soon"}, "timeout_s must be a number"),
    ],
)
def test_gemini_context_validation(mutate: dict[str, object], message: str) -> None:
    payload = DEFAULT_GEMINI_CONTEXT.to_mapping()
    payload.update(mutate)
    with pytest.raises(ExecutionProfileError, match=message):
        GeminiContextProfile.from_mapping(payload)


def test_gemini_context_normalizes_integral_timeout_for_receipt_roundtrip() -> None:
    profile = load_config(str(ROOT / "profiles" / "model-sweep-gemini-v1.json")).execution_profile
    context = replace(DEFAULT_GEMINI_CONTEXT, timeout_s=120)
    assert context.timeout_s == 120.0
    direct = replace(profile, gemini=context)
    assert ModelSweepExecutionProfile.from_mapping(direct.to_mapping()).sha256 == direct.sha256
    receipt = ExecutionReceipt.create(
        direct, [direct.binding_for("gemini-flash")], claude_executable=None, claude_version=None
    )
    assert ExecutionReceipt.from_mapping(receipt.to_mapping()) == receipt


def test_explicit_null_gemini_block_is_rejected_in_profile_and_receipt() -> None:
    mapping = _profile_mapping()
    mapping["gemini"] = None
    with pytest.raises(ExecutionProfileError, match="gemini must be a JSON object"):
        ModelSweepExecutionProfile.from_mapping(mapping)

    binding = DEFAULT_EXECUTION_PROFILE.binding_for("general-35b")
    receipt = ExecutionReceipt.create(
        DEFAULT_EXECUTION_PROFILE, [binding], claude_executable=None, claude_version=None
    )
    wire = receipt.to_mapping()
    wire["gemini"] = None
    with pytest.raises(ExecutionProfileError, match="gemini must be a JSON object"):
        ExecutionReceipt.from_mapping(wire)


def test_legacy_receipt_without_gemini_key_parses_and_hashes_unchanged() -> None:
    binding = DEFAULT_EXECUTION_PROFILE.binding_for("sonnet")
    receipt = ExecutionReceipt.create(
        DEFAULT_EXECUTION_PROFILE, [binding], claude_executable="claude", claude_version="1.0.0"
    )
    wire = receipt.to_mapping()
    # A Claude-only receipt omits the optional gemini key entirely (byte/hash stability, §6 D5).
    assert "gemini" not in wire
    assert receipt.gemini is None
    assert ExecutionReceipt.from_mapping(wire) == receipt


def test_receipt_gemini_biconditional_rejects_mismatch() -> None:
    profile = load_config(str(ROOT / "profiles" / "model-sweep-gemini-v1.json")).execution_profile
    gemini_binding = profile.binding_for("gemini-flash")
    local_binding = profile.binding_for("general-35b")
    # Selecting a Gemini binding requires the gemini block ...
    with pytest.raises(ExecutionProfileError, match="gemini must be present exactly when"):
        ExecutionReceipt(
            schema_version=1,
            profile_id=profile.id,
            execution_profile_sha256=profile.sha256,
            provider_profile_sha256=profile.provider_profile_sha256,
            context_profile_sha256=profile.context_profile_sha256,
            bindings=(gemini_binding,),
            sealing_mode=profile.claude.sealing_mode,
            claude_cli=None,
            gemini=None,
        )
    # ... and a gemini block with no Gemini binding selected is equally rejected (local-only here,
    # so the Claude-CLI biconditional is satisfied and the Gemini one is what fires).
    with pytest.raises(ExecutionProfileError, match="gemini must be present exactly when"):
        ExecutionReceipt(
            schema_version=1,
            profile_id=profile.id,
            execution_profile_sha256=profile.sha256,
            provider_profile_sha256=profile.provider_profile_sha256,
            context_profile_sha256=profile.context_profile_sha256,
            bindings=(local_binding,),
            sealing_mode=profile.claude.sealing_mode,
            claude_cli=None,
            gemini=DEFAULT_GEMINI_CONTEXT,
        )


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


@pytest.mark.parametrize(
    ("path", "message"),
    [
        # The top-level profile schema now permits an optional 'gemini' key, so its unknown-key
        # message names the allowed set; the leaf schemas remain strict-exact.
        (("extra",), "keys must include"),
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
