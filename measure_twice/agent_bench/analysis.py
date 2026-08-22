"""Strict machine-readable analysis plans for coding-agent benchmarks."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from measure_twice.agent_bench._wire import AgentInputError, WireCodec

ANALYSIS_POLICIES = frozenset({"none", "superiority", "noninferiority"})
ANALYSIS_SCOPE_KINDS = frozenset({"overall", "family", "tag"})
MULTIPLICITY_POLICIES = frozenset({"none", "bonferroni"})


class AnalysisPlanError(AgentInputError):
    """An analysis plan violated its strict v1 wire or decision contract."""


_WIRE = WireCodec(AnalysisPlanError)


@dataclass(frozen=True, slots=True)
class AnalysisScope:
    scope: str
    scope_id: str | None
    confirmatory: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or self.scope not in ANALYSIS_SCOPE_KINDS:
            raise AnalysisPlanError(
                f"analysis scope.scope must be one of {sorted(ANALYSIS_SCOPE_KINDS)}, "
                f"got {self.scope!r}"
            )
        if self.confirmatory is not True:
            raise AnalysisPlanError("analysis scope.confirmatory must be true")
        if self.scope == "overall":
            if self.scope_id is not None:
                raise AnalysisPlanError("overall analysis scope.scope_id must be null")
        else:
            if self.scope_id is None:
                raise AnalysisPlanError(
                    f"{self.scope} analysis scope.scope_id must be a safe identifier"
                )
            _WIRE.validate_safe_id(self.scope_id, label="analysis scope.scope_id")

    @classmethod
    def from_mapping(cls, value: object, *, label: str = "analysis scope") -> AnalysisScope:
        clean = _WIRE.require_exact_keys(
            value, frozenset({"scope", "scope_id", "confirmatory"}), label=label
        )
        try:
            return cls(
                scope=cast("str", clean["scope"]),
                scope_id=cast("str | None", clean["scope_id"]),
                confirmatory=cast("bool", clean["confirmatory"]),
            )
        except AnalysisPlanError as exc:
            raise AnalysisPlanError(f"{label}: {exc}") from exc

    def to_mapping(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "confirmatory": self.confirmatory,
        }


@dataclass(frozen=True, slots=True)
class AnalysisPlan:
    schema_version: int
    analysis_id: str
    policy: str
    candidate_profile_id: str | None
    reference_profile_id: str | None
    scopes: list[AnalysisScope]
    margin_points: int | None
    confidence: float | None
    multiplicity: str
    bootstrap_iterations: int

    def __post_init__(self) -> None:
        _WIRE.validate_schema_version(self.schema_version, label="analysis plan")
        _WIRE.validate_safe_id(self.analysis_id, label="analysis plan.analysis_id")
        if not isinstance(self.policy, str) or self.policy not in ANALYSIS_POLICIES:
            raise AnalysisPlanError(
                f"analysis plan.policy must be one of {sorted(ANALYSIS_POLICIES)}, "
                f"got {self.policy!r}"
            )
        if not isinstance(self.scopes, list):
            raise AnalysisPlanError(
                f"analysis plan.scopes must be a list, got {type(self.scopes).__name__}"
            )
        seen_scopes: set[tuple[str, str | None]] = set()
        for index, scope in enumerate(self.scopes):
            if not isinstance(scope, AnalysisScope):
                raise AnalysisPlanError(
                    f"analysis plan.scopes[{index}] must be an AnalysisScope, "
                    f"got {type(scope).__name__}"
                )
            key = (scope.scope, scope.scope_id)
            if key in seen_scopes:
                raise AnalysisPlanError(f"duplicate analysis scope {key!r}")
            seen_scopes.add(key)
        if not isinstance(self.multiplicity, str) or self.multiplicity not in MULTIPLICITY_POLICIES:
            raise AnalysisPlanError(
                "analysis plan.multiplicity must be one of "
                f"{sorted(MULTIPLICITY_POLICIES)}, got {self.multiplicity!r}"
            )
        if (
            not isinstance(self.bootstrap_iterations, int)
            or isinstance(self.bootstrap_iterations, bool)
            or self.bootstrap_iterations != 10_000
        ):
            raise AnalysisPlanError("analysis plan.bootstrap_iterations must equal 10000")

        if self.policy == "none":
            if self.candidate_profile_id is not None or self.reference_profile_id is not None:
                raise AnalysisPlanError(
                    "policy 'none' requires null candidate_profile_id and reference_profile_id"
                )
            if self.scopes:
                raise AnalysisPlanError("policy 'none' requires an empty scopes list")
            if self.margin_points is not None or self.confidence is not None:
                raise AnalysisPlanError("policy 'none' requires null margin_points and confidence")
            if self.multiplicity != "none":
                raise AnalysisPlanError("policy 'none' requires multiplicity 'none'")
            return

        if self.candidate_profile_id is None or self.reference_profile_id is None:
            raise AnalysisPlanError(
                f"policy {self.policy!r} requires candidate_profile_id and reference_profile_id"
            )
        candidate = _WIRE.validate_safe_id(
            self.candidate_profile_id, label="analysis plan.candidate_profile_id"
        )
        reference = _WIRE.validate_safe_id(
            self.reference_profile_id, label="analysis plan.reference_profile_id"
        )
        if candidate == reference:
            raise AnalysisPlanError("candidate_profile_id and reference_profile_id must differ")
        if (
            not isinstance(self.margin_points, int)
            or isinstance(self.margin_points, bool)
            or self.margin_points != 5
        ):
            raise AnalysisPlanError(
                f"policy {self.policy!r} requires integer margin_points equal to 5"
            )
        if not isinstance(self.confidence, float) or self.confidence != 0.95:
            raise AnalysisPlanError(f"policy {self.policy!r} requires confidence equal to 0.95")
        if not self.scopes:
            raise AnalysisPlanError(f"policy {self.policy!r} requires at least one scope")
        expected_multiplicity = "none" if len(self.scopes) == 1 else "bonferroni"
        if self.multiplicity != expected_multiplicity:
            raise AnalysisPlanError(
                f"{len(self.scopes)} confirmatory scope(s) require multiplicity "
                f"{expected_multiplicity!r}, got {self.multiplicity!r}"
            )

    @classmethod
    def from_mapping(cls, value: object) -> AnalysisPlan:
        expected = frozenset(
            {
                "schema_version",
                "analysis_id",
                "policy",
                "candidate_profile_id",
                "reference_profile_id",
                "scopes",
                "margin_points",
                "confidence",
                "multiplicity",
                "bootstrap_iterations",
            }
        )
        clean = _WIRE.require_exact_keys(value, expected, label="analysis plan")
        raw_scopes = clean["scopes"]
        if not isinstance(raw_scopes, list):
            raise AnalysisPlanError(
                f"analysis plan.scopes must be a list, got {type(raw_scopes).__name__}"
            )
        schema_version = _WIRE.validate_schema_version(
            clean["schema_version"], label="analysis plan"
        )
        return cls(
            schema_version=schema_version,
            analysis_id=cast("str", clean["analysis_id"]),
            policy=cast("str", clean["policy"]),
            candidate_profile_id=cast("str | None", clean["candidate_profile_id"]),
            reference_profile_id=cast("str | None", clean["reference_profile_id"]),
            scopes=[
                AnalysisScope.from_mapping(scope, label=f"analysis plan.scopes[{index}]")
                for index, scope in enumerate(raw_scopes)
            ],
            margin_points=cast("int | None", clean["margin_points"]),
            confidence=cast("float | None", clean["confidence"]),
            multiplicity=cast("str", clean["multiplicity"]),
            bootstrap_iterations=cast("int", clean["bootstrap_iterations"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "analysis_id": self.analysis_id,
            "policy": self.policy,
            "candidate_profile_id": self.candidate_profile_id,
            "reference_profile_id": self.reference_profile_id,
            "scopes": [scope.to_mapping() for scope in self.scopes],
            "margin_points": self.margin_points,
            "confidence": self.confidence,
            "multiplicity": self.multiplicity,
            "bootstrap_iterations": self.bootstrap_iterations,
        }

    @property
    def sha256(self) -> str:
        return analysis_plan_hash(self)

    def validate_roster(self, selected_profile_ids: Collection[str]) -> None:
        """Validate non-null decision arms against the explicitly selected roster."""

        selected = set(selected_profile_ids)
        if len(selected) != len(selected_profile_ids):
            raise AnalysisPlanError("selected profile roster contains duplicate ids")
        for index, selected_profile_id in enumerate(selected):
            _WIRE.validate_safe_id(selected_profile_id, label=f"selected profile ids[{index}]")
        for role, decision_profile_id in (
            ("candidate", self.candidate_profile_id),
            ("reference", self.reference_profile_id),
        ):
            if decision_profile_id is not None and decision_profile_id not in selected:
                raise AnalysisPlanError(
                    f"analysis {role}_profile_id {decision_profile_id!r} is not in the "
                    "selected roster"
                )

    def validate_scope_names(self, *, families: Collection[str], tags: Collection[str]) -> None:
        """Reject a named confirmatory family/tag absent from the loaded suite."""

        family_names = set(families)
        tag_names = set(tags)
        for scope in self.scopes:
            if scope.scope == "family" and scope.scope_id not in family_names:
                raise AnalysisPlanError(
                    f"analysis family scope {scope.scope_id!r} is absent from the suite"
                )
            if scope.scope == "tag" and scope.scope_id not in tag_names:
                raise AnalysisPlanError(
                    f"analysis tag scope {scope.scope_id!r} is absent from the suite"
                )


def load_analysis_plan(path: str | Path) -> AnalysisPlan:
    return AnalysisPlan.from_mapping(_WIRE.load_json_object(path, label="analysis plan"))


def analysis_plan_hash(plan: AnalysisPlan) -> str:
    if not isinstance(plan, AnalysisPlan):
        raise AnalysisPlanError(f"plan must be an AnalysisPlan, got {type(plan).__name__}")
    return _WIRE.canonical_sha256(plan.to_mapping())


__all__ = [
    "ANALYSIS_POLICIES",
    "ANALYSIS_SCOPE_KINDS",
    "AnalysisPlan",
    "AnalysisPlanError",
    "AnalysisScope",
    "analysis_plan_hash",
    "load_analysis_plan",
]
