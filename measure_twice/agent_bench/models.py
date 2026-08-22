"""Strict model-registry and execution-profile inputs for coding-agent benchmarks.

These inputs are measurement identity, not convenient configuration.  Loaders therefore reject
unknown or missing keys, duplicate JSON keys, unsupported schema versions, and values whose Python
types only *resemble* the wire type (notably ``bool`` as ``int``).  Hashes always use compact,
sorted-key UTF-8 JSON and never include an unselected registry entry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from measure_twice.agent_bench._wire import AgentInputError, WireCodec

PROVIDERS = frozenset({"codex-cli", "claude-cli"})
RUN_CLASSES = ("smoke", "pilot", "observation")
RETRY_CLASSES = ("rate-limit", "provider-5xx", "preterminal-transport")


class ModelSpecError(AgentInputError):
    """A model registry or execution profile violated its strict v1 contract."""


_WIRE = WireCodec(ModelSpecError)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One explicit native coding-agent product profile."""

    name: str
    provider: str
    requested_model: str
    effort: str | None
    execution_profile_id: str

    def __post_init__(self) -> None:
        _WIRE.validate_safe_id(self.name, label="model.name")
        if not isinstance(self.provider, str) or self.provider not in PROVIDERS:
            raise ModelSpecError(
                f"model.provider must be one of {sorted(PROVIDERS)}, got {self.provider!r}"
            )
        _WIRE.validate_nonempty_string(self.requested_model, label="model.requested_model")
        if self.effort is not None:
            _WIRE.validate_nonempty_string(self.effort, label="model.effort")
        _WIRE.validate_safe_id(self.execution_profile_id, label="model.execution_profile_id")

    @classmethod
    def from_mapping(cls, value: object, *, label: str = "model") -> ModelSpec:
        expected = frozenset(
            {"name", "provider", "requested_model", "effort", "execution_profile_id"}
        )
        clean = _WIRE.require_exact_keys(value, expected, label=label)
        return cls(
            name=cast("str", clean["name"]),
            provider=cast("str", clean["provider"]),
            requested_model=cast("str", clean["requested_model"]),
            effort=cast("str | None", clean["effort"]),
            execution_profile_id=cast("str", clean["execution_profile_id"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "effort": self.effort,
            "execution_profile_id": self.execution_profile_id,
        }


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    schema_version: int
    models: list[ModelSpec]

    def __post_init__(self) -> None:
        _WIRE.validate_schema_version(self.schema_version, label="model registry")
        if not isinstance(self.models, list):
            raise ModelSpecError(
                f"model registry.models must be a list, got {type(self.models).__name__}"
            )
        if not self.models:
            raise ModelSpecError("model registry.models must contain at least one model")
        seen: set[str] = set()
        for index, model in enumerate(self.models):
            if not isinstance(model, ModelSpec):
                raise ModelSpecError(
                    f"model registry.models[{index}] must be a ModelSpec, "
                    f"got {type(model).__name__}"
                )
            if model.name in seen:
                raise ModelSpecError(f"duplicate model profile id {model.name!r}")
            seen.add(model.name)

    @classmethod
    def from_mapping(cls, value: object) -> ModelRegistry:
        clean = _WIRE.require_exact_keys(
            value, frozenset({"schema_version", "models"}), label="model registry"
        )
        schema_version = _WIRE.validate_schema_version(
            clean["schema_version"], label="model registry"
        )
        raw_models = clean["models"]
        if not isinstance(raw_models, list):
            raise ModelSpecError(
                f"model registry.models must be a list, got {type(raw_models).__name__}"
            )
        models = [
            ModelSpec.from_mapping(model, label=f"model registry.models[{index}]")
            for index, model in enumerate(raw_models)
        ]
        return cls(schema_version=schema_version, models=models)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "models": [model.to_mapping() for model in self.models],
        }

    def select(self, profile_ids: Sequence[str]) -> list[ModelSpec]:
        """Resolve a unique caller-declared roster without reordering it."""

        if isinstance(profile_ids, (str, bytes)) or not isinstance(profile_ids, Sequence):
            raise ModelSpecError("selected profile ids must be a sequence of safe identifiers")
        if not profile_ids:
            raise ModelSpecError("selected profile ids must contain at least one profile")
        by_id = {model.name: model for model in self.models}
        selected: list[ModelSpec] = []
        seen: set[str] = set()
        for index, profile_id in enumerate(profile_ids):
            safe_id = _WIRE.validate_safe_id(profile_id, label=f"selected profile ids[{index}]")
            if safe_id in seen:
                raise ModelSpecError(f"duplicate selected profile id {safe_id!r}")
            try:
                selected.append(by_id[safe_id])
            except KeyError as exc:
                raise ModelSpecError(f"unknown selected profile id {safe_id!r}") from exc
            seen.add(safe_id)
        return selected

    def selected_hash(self, profile_ids: Sequence[str]) -> str:
        """Return the canonical hash of a caller-declared selection from this registry."""

        return selected_profile_hash(self.select(profile_ids))


@dataclass(frozen=True, slots=True)
class Limits:
    agent_timeout_s: int
    evaluator_timeout_s: int

    def __post_init__(self) -> None:
        _WIRE.require_positive_int(self.agent_timeout_s, label="limits.agent_timeout_s")
        _WIRE.require_positive_int(self.evaluator_timeout_s, label="limits.evaluator_timeout_s")

    @classmethod
    def from_mapping(cls, value: object, *, label: str) -> Limits:
        clean = _WIRE.require_exact_keys(
            value, frozenset({"agent_timeout_s", "evaluator_timeout_s"}), label=label
        )
        return cls(
            agent_timeout_s=_WIRE.require_positive_int(
                clean["agent_timeout_s"], label=f"{label}.agent_timeout_s"
            ),
            evaluator_timeout_s=_WIRE.require_positive_int(
                clean["evaluator_timeout_s"], label=f"{label}.evaluator_timeout_s"
            ),
        )

    def to_mapping(self) -> dict[str, int]:
        return {
            "agent_timeout_s": self.agent_timeout_s,
            "evaluator_timeout_s": self.evaluator_timeout_s,
        }


@dataclass(frozen=True, slots=True)
class RunPolicy:
    limits: Limits
    task_count: int
    model_count: int
    samples: int
    max_cells_per_tranche: int
    terminal_cells: int
    provider_attempts: int
    wall_s: int

    def __post_init__(self) -> None:
        if not isinstance(self.limits, Limits):
            raise ModelSpecError(
                f"run policy.limits must be Limits, got {type(self.limits).__name__}"
            )
        for name in (
            "task_count",
            "model_count",
            "samples",
            "max_cells_per_tranche",
            "terminal_cells",
            "provider_attempts",
            "wall_s",
        ):
            _WIRE.require_positive_int(getattr(self, name), label=f"run policy.{name}")
        expected_cells = self.task_count * self.model_count * self.samples
        if self.terminal_cells != expected_cells:
            raise ModelSpecError(
                "run policy.terminal_cells must equal task_count * model_count * samples "
                f"({expected_cells}), got {self.terminal_cells}"
            )
        if self.max_cells_per_tranche < self.model_count:
            raise ModelSpecError(
                "run policy.max_cells_per_tranche must fit at least one complete model block"
            )
        if self.max_cells_per_tranche % self.model_count != 0:
            raise ModelSpecError(
                "run policy.max_cells_per_tranche may not split a complete model block"
            )

    @classmethod
    def from_mapping(cls, value: object, *, label: str) -> RunPolicy:
        expected = frozenset(
            {
                "limits",
                "task_count",
                "model_count",
                "samples",
                "max_cells_per_tranche",
                "terminal_cells",
                "provider_attempts",
                "wall_s",
            }
        )
        clean = _WIRE.require_exact_keys(value, expected, label=label)
        return cls(
            limits=Limits.from_mapping(clean["limits"], label=f"{label}.limits"),
            task_count=_WIRE.require_positive_int(clean["task_count"], label=f"{label}.task_count"),
            model_count=_WIRE.require_positive_int(
                clean["model_count"], label=f"{label}.model_count"
            ),
            samples=_WIRE.require_positive_int(clean["samples"], label=f"{label}.samples"),
            max_cells_per_tranche=_WIRE.require_positive_int(
                clean["max_cells_per_tranche"], label=f"{label}.max_cells_per_tranche"
            ),
            terminal_cells=_WIRE.require_positive_int(
                clean["terminal_cells"], label=f"{label}.terminal_cells"
            ),
            provider_attempts=_WIRE.require_positive_int(
                clean["provider_attempts"], label=f"{label}.provider_attempts"
            ),
            wall_s=_WIRE.require_positive_int(clean["wall_s"], label=f"{label}.wall_s"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "limits": self.limits.to_mapping(),
            "task_count": self.task_count,
            "model_count": self.model_count,
            "samples": self.samples,
            "max_cells_per_tranche": self.max_cells_per_tranche,
            "terminal_cells": self.terminal_cells,
            "provider_attempts": self.provider_attempts,
            "wall_s": self.wall_s,
        }


@dataclass(frozen=True, slots=True)
class Ceilings:
    changed_paths: int
    patch_bytes: int
    stream_bytes_each: int
    cell_artifact_bytes: int
    evaluator_cpu_s: int
    evaluator_memory_bytes: int
    evaluator_processes: int
    evaluator_files: int
    evaluator_file_bytes: int

    def __post_init__(self) -> None:
        for name in self.__slots__:
            _WIRE.require_positive_int(getattr(self, name), label=f"ceilings.{name}")

    @classmethod
    def from_mapping(cls, value: object) -> Ceilings:
        expected = frozenset(cls.__slots__)
        clean = _WIRE.require_exact_keys(value, expected, label="execution profile.ceilings")
        values = {
            name: _WIRE.require_positive_int(
                clean[name], label=f"execution profile.ceilings.{name}"
            )
            for name in cls.__slots__
        }
        return cls(**values)

    def to_mapping(self) -> dict[str, int]:
        return {name: cast("int", getattr(self, name)) for name in self.__slots__}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    eligible: list[str]
    max_fresh_retries: int
    retry_after_cap_s: int
    default_delay_s: int

    def __post_init__(self) -> None:
        if not isinstance(self.eligible, list) or any(
            not isinstance(item, str) for item in self.eligible
        ):
            raise ModelSpecError("execution profile.retry.eligible must be a list of strings")
        if tuple(self.eligible) != RETRY_CLASSES:
            raise ModelSpecError(
                "execution profile.retry.eligible must equal the ordered v1 retry classes "
                f"{list(RETRY_CLASSES)!r}, got {self.eligible!r}"
            )
        if (
            not isinstance(self.max_fresh_retries, int)
            or isinstance(self.max_fresh_retries, bool)
            or self.max_fresh_retries != 1
        ):
            raise ModelSpecError("execution profile.retry.max_fresh_retries must equal 1")
        if (
            not isinstance(self.retry_after_cap_s, int)
            or isinstance(self.retry_after_cap_s, bool)
            or self.retry_after_cap_s != 60
        ):
            raise ModelSpecError("execution profile.retry.retry_after_cap_s must equal 60")
        if (
            not isinstance(self.default_delay_s, int)
            or isinstance(self.default_delay_s, bool)
            or self.default_delay_s != 5
        ):
            raise ModelSpecError("execution profile.retry.default_delay_s must equal 5")

    @classmethod
    def from_mapping(cls, value: object) -> RetryPolicy:
        expected = frozenset(
            {"eligible", "max_fresh_retries", "retry_after_cap_s", "default_delay_s"}
        )
        clean = _WIRE.require_exact_keys(value, expected, label="execution profile.retry")
        return cls(
            eligible=cast("list[str]", clean["eligible"]),
            max_fresh_retries=cast("int", clean["max_fresh_retries"]),
            retry_after_cap_s=cast("int", clean["retry_after_cap_s"]),
            default_delay_s=cast("int", clean["default_delay_s"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "eligible": list(self.eligible),
            "max_fresh_retries": self.max_fresh_retries,
            "retry_after_cap_s": self.retry_after_cap_s,
            "default_delay_s": self.default_delay_s,
        }


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    schema_version: int
    id: str
    qualification_limits: Limits
    run_policy: dict[str, RunPolicy]
    repetitions: int
    concurrency: int
    ceilings: Ceilings
    retry: RetryPolicy
    sandbox_contract_version: str
    schedule_algorithm: str
    analysis_algorithm: str

    def __post_init__(self) -> None:
        _WIRE.validate_schema_version(self.schema_version, label="execution profile")
        _WIRE.validate_safe_id(self.id, label="execution profile.id")
        if not isinstance(self.qualification_limits, Limits):
            raise ModelSpecError("execution profile.qualification_limits must be Limits")
        if not isinstance(self.run_policy, dict) or set(self.run_policy) != set(RUN_CLASSES):
            raise ModelSpecError(
                f"execution profile.run_policy must contain exactly {list(RUN_CLASSES)!r}"
            )
        for run_class, policy in self.run_policy.items():
            if not isinstance(policy, RunPolicy):
                raise ModelSpecError(
                    f"execution profile.run_policy.{run_class} must be a RunPolicy"
                )
        if (
            not isinstance(self.repetitions, int)
            or isinstance(self.repetitions, bool)
            or self.repetitions != 2
        ):
            raise ModelSpecError("execution profile.repetitions must equal 2")
        if (
            not isinstance(self.concurrency, int)
            or isinstance(self.concurrency, bool)
            or self.concurrency != 1
        ):
            raise ModelSpecError("execution profile.concurrency must equal 1")
        if not isinstance(self.ceilings, Ceilings):
            raise ModelSpecError("execution profile.ceilings must be Ceilings")
        if not isinstance(self.retry, RetryPolicy):
            raise ModelSpecError("execution profile.retry must be RetryPolicy")
        if self.sandbox_contract_version != "linux-bwrap-v1":
            raise ModelSpecError(
                "execution profile.sandbox_contract_version must equal 'linux-bwrap-v1'"
            )
        if self.schedule_algorithm != "schedule-v1":
            raise ModelSpecError("execution profile.schedule_algorithm must equal 'schedule-v1'")
        if self.analysis_algorithm != "bootstrap-v1":
            raise ModelSpecError("execution profile.analysis_algorithm must equal 'bootstrap-v1'")
        expected_attempt_multiplier = 1 + self.retry.max_fresh_retries
        for run_class, policy in self.run_policy.items():
            expected_attempts = policy.terminal_cells * expected_attempt_multiplier
            if policy.provider_attempts != expected_attempts:
                raise ModelSpecError(
                    f"execution profile.run_policy.{run_class}.provider_attempts must equal "
                    f"terminal_cells * {expected_attempt_multiplier} ({expected_attempts}), "
                    f"got {policy.provider_attempts}"
                )

    @classmethod
    def from_mapping(cls, value: object) -> ExecutionProfile:
        expected = frozenset(
            {
                "schema_version",
                "id",
                "qualification_limits",
                "run_policy",
                "repetitions",
                "concurrency",
                "ceilings",
                "retry",
                "sandbox_contract_version",
                "schedule_algorithm",
                "analysis_algorithm",
            }
        )
        clean = _WIRE.require_exact_keys(value, expected, label="execution profile")
        schema_version = _WIRE.validate_schema_version(
            clean["schema_version"], label="execution profile"
        )
        raw_policy = _WIRE.require_exact_keys(
            clean["run_policy"], frozenset(RUN_CLASSES), label="execution profile.run_policy"
        )
        policies = {
            run_class: RunPolicy.from_mapping(
                raw_policy[run_class], label=f"execution profile.run_policy.{run_class}"
            )
            for run_class in RUN_CLASSES
        }
        return cls(
            schema_version=schema_version,
            id=cast("str", clean["id"]),
            qualification_limits=Limits.from_mapping(
                clean["qualification_limits"], label="execution profile.qualification_limits"
            ),
            run_policy=policies,
            repetitions=cast("int", clean["repetitions"]),
            concurrency=cast("int", clean["concurrency"]),
            ceilings=Ceilings.from_mapping(clean["ceilings"]),
            retry=RetryPolicy.from_mapping(clean["retry"]),
            sandbox_contract_version=cast("str", clean["sandbox_contract_version"]),
            schedule_algorithm=cast("str", clean["schedule_algorithm"]),
            analysis_algorithm=cast("str", clean["analysis_algorithm"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "qualification_limits": self.qualification_limits.to_mapping(),
            "run_policy": {
                run_class: self.run_policy[run_class].to_mapping() for run_class in RUN_CLASSES
            },
            "repetitions": self.repetitions,
            "concurrency": self.concurrency,
            "ceilings": self.ceilings.to_mapping(),
            "retry": self.retry.to_mapping(),
            "sandbox_contract_version": self.sandbox_contract_version,
            "schedule_algorithm": self.schedule_algorithm,
            "analysis_algorithm": self.analysis_algorithm,
        }

    @property
    def sha256(self) -> str:
        return execution_profile_hash(self)


def load_model_registry(path: str | Path) -> ModelRegistry:
    return ModelRegistry.from_mapping(_WIRE.load_json_object(path, label="model registry"))


def load_execution_profile(path: str | Path) -> ExecutionProfile:
    return ExecutionProfile.from_mapping(_WIRE.load_json_object(path, label="execution profile"))


def selected_profile_hash(profiles: Sequence[ModelSpec]) -> str:
    """Hash canonical JSON of selected ``ModelSpec`` objects in caller-declared order.

    The preimage is exactly ``[profile.to_mapping(), ...]``.  It has no registry wrapper, so an
    unselected registry entry cannot perturb run identity; reversing a selected roster does.
    """

    if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Sequence):
        raise ModelSpecError("selected profiles must be a sequence of ModelSpec objects")
    if not profiles:
        raise ModelSpecError("selected profiles must contain at least one profile")
    seen: set[str] = set()
    payload: list[dict[str, object]] = []
    for index, profile in enumerate(profiles):
        if not isinstance(profile, ModelSpec):
            raise ModelSpecError(
                f"selected profiles[{index}] must be a ModelSpec, got {type(profile).__name__}"
            )
        if profile.name in seen:
            raise ModelSpecError(f"duplicate selected profile id {profile.name!r}")
        seen.add(profile.name)
        payload.append(profile.to_mapping())
    return _WIRE.canonical_sha256(payload)


def execution_profile_hash(profile: ExecutionProfile) -> str:
    if not isinstance(profile, ExecutionProfile):
        raise ModelSpecError(f"profile must be an ExecutionProfile, got {type(profile).__name__}")
    return _WIRE.canonical_sha256(profile.to_mapping())


def validate_execution_profile_binding(
    execution_profile: ExecutionProfile,
    *,
    suite_execution_profile_id: str,
    selected_profiles: Sequence[ModelSpec],
) -> None:
    """Fail before execution when suite, selected models, and profile policy are not identical."""

    suite_id = _WIRE.validate_safe_id(
        suite_execution_profile_id, label="suite.execution_profile_id"
    )
    if suite_id != execution_profile.id:
        raise ModelSpecError(
            f"suite execution_profile_id {suite_id!r} does not match loaded execution profile "
            f"id {execution_profile.id!r}"
        )
    for profile in selected_profiles:
        if profile.execution_profile_id != execution_profile.id:
            raise ModelSpecError(
                f"model profile {profile.name!r} execution_profile_id "
                f"{profile.execution_profile_id!r} does not match loaded execution profile id "
                f"{execution_profile.id!r}"
            )


def dispatch_by_provider[T](
    profile: ModelSpec,
    dispatchers: Mapping[str, Callable[[ModelSpec], T]],
) -> T:
    """Dispatch solely through ``ModelSpec.provider``; no alias or model-name branch exists."""

    if not isinstance(profile, ModelSpec):
        raise ModelSpecError(f"profile must be a ModelSpec, got {type(profile).__name__}")
    try:
        dispatcher = dispatchers[profile.provider]
    except KeyError as exc:
        raise ModelSpecError(f"no dispatcher registered for provider {profile.provider!r}") from exc
    if not callable(dispatcher):
        raise ModelSpecError(f"dispatcher for provider {profile.provider!r} is not callable")
    return dispatcher(profile)


__all__ = [
    "PROVIDERS",
    "RETRY_CLASSES",
    "RUN_CLASSES",
    "Ceilings",
    "ExecutionProfile",
    "Limits",
    "ModelRegistry",
    "ModelSpec",
    "ModelSpecError",
    "RunPolicy",
    "dispatch_by_provider",
    "execution_profile_hash",
    "load_execution_profile",
    "load_model_registry",
    "selected_profile_hash",
    "validate_execution_profile_binding",
]
