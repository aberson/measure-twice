from __future__ import annotations

import json
from pathlib import Path

import pytest

from measure_twice.agent_bench.analysis import (
    AnalysisPlan,
    AnalysisPlanError,
    analysis_plan_hash,
    load_analysis_plan,
)

ROOT = Path(__file__).resolve().parents[2]
SMOKE_PLAN = ROOT / "analysis-plans" / "agent-smoke-v1.json"
SMOKE_PLAN_HASH = "5c9074afd908184f9ea78c73751feb043acd71550334759752ab7638f1635a14"


def _payload() -> dict[str, object]:
    value = json.loads(SMOKE_PLAN.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(tmp_path: Path, payload: object, name: str = "analysis.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_smoke_none_policy_and_hash_are_frozen() -> None:
    plan = load_analysis_plan(SMOKE_PLAN)

    assert isinstance(plan, AnalysisPlan)
    assert plan.policy == "none"
    assert plan.candidate_profile_id is None
    assert plan.reference_profile_id is None
    assert plan.scopes == []
    assert plan.margin_points is None
    assert plan.confidence is None
    assert plan.multiplicity == "none"
    assert plan.bootstrap_iterations == 10_000
    assert analysis_plan_hash(plan) == SMOKE_PLAN_HASH


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("candidate_profile_id", "codex-luna", "requires null"),
        ("reference_profile_id", "claude-sonnet", "requires null"),
        (
            "scopes",
            [{"scope": "overall", "scope_id": None, "confirmatory": True}],
            "empty scopes",
        ),
        ("margin_points", 5, "requires null"),
        ("confidence", 0.95, "requires null"),
        ("multiplicity", "bonferroni", "requires multiplicity"),
        ("bootstrap_iterations", True, "must equal 10000"),
    ],
)
def test_none_policy_rejects_non_none_decision_fields(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(AnalysisPlanError, match=message):
        load_analysis_plan(_write(tmp_path, payload))


def test_strict_plan_keys_versions_and_containers(tmp_path: Path) -> None:
    payload = _payload()
    payload["surprise"] = True
    with pytest.raises(AnalysisPlanError, match="unknown"):
        load_analysis_plan(_write(tmp_path, payload, "unknown.json"))

    payload = _payload()
    payload.pop("scopes")
    with pytest.raises(AnalysisPlanError, match="missing"):
        load_analysis_plan(_write(tmp_path, payload, "missing.json"))

    payload = _payload()
    payload["schema_version"] = 7
    with pytest.raises(AnalysisPlanError, match="unsupported"):
        load_analysis_plan(_write(tmp_path, payload, "version.json"))

    payload = _payload()
    payload["scopes"] = {}
    with pytest.raises(AnalysisPlanError, match="must be a list"):
        load_analysis_plan(_write(tmp_path, payload, "container.json"))


def _decision_payload() -> dict[str, object]:
    payload = _payload()
    payload.update(
        {
            "analysis_id": "agent-decision-v1",
            "policy": "superiority",
            "candidate_profile_id": "codex-luna",
            "reference_profile_id": "claude-sonnet",
            "scopes": [{"scope": "overall", "scope_id": None, "confirmatory": True}],
            "margin_points": 5,
            "confidence": 0.95,
            "multiplicity": "none",
        }
    )
    return payload


def test_decision_policy_validates_roster_and_suite_scope_names(tmp_path: Path) -> None:
    payload = _decision_payload()
    payload["scopes"] = [
        {"scope": "family", "scope_id": "bug-repair", "confirmatory": True},
        {"scope": "tag", "scope_id": "python", "confirmatory": True},
    ]
    payload["multiplicity"] = "bonferroni"
    plan = load_analysis_plan(_write(tmp_path, payload))

    plan.validate_roster(["codex-luna", "claude-sonnet"])
    plan.validate_scope_names(families={"bug-repair"}, tags={"python"})
    with pytest.raises(AnalysisPlanError, match="not in the selected roster"):
        plan.validate_roster(["codex-luna", "claude-haiku"])
    with pytest.raises(AnalysisPlanError, match="absent from the suite"):
        plan.validate_scope_names(families={"bounded-feature"}, tags={"python"})


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update({"candidate_profile_id": None}), "requires candidate"),
        (
            lambda data: data.update({"reference_profile_id": "codex-luna"}),
            "must differ",
        ),
        (lambda data: data.update({"margin_points": 5.0}), "integer margin"),
        (lambda data: data.update({"confidence": 1}), "confidence equal"),
        (lambda data: data.update({"scopes": []}), "at least one scope"),
        (lambda data: data.update({"multiplicity": "bonferroni"}), "require multiplicity"),
    ],
)
def test_decision_policy_rejects_incoherent_fields(
    tmp_path: Path, mutate: object, message: str
) -> None:
    payload = _decision_payload()
    assert callable(mutate)
    mutate(payload)

    with pytest.raises(AnalysisPlanError, match=message):
        load_analysis_plan(_write(tmp_path, payload))


def test_scopes_reject_duplicates_ids_and_wrong_nullability(tmp_path: Path) -> None:
    payload = _decision_payload()
    scope = {"scope": "overall", "scope_id": None, "confirmatory": True}
    payload["scopes"] = [scope, dict(scope)]
    payload["multiplicity"] = "bonferroni"
    with pytest.raises(AnalysisPlanError, match="duplicate analysis scope"):
        load_analysis_plan(_write(tmp_path, payload, "duplicate.json"))

    payload = _decision_payload()
    payload["scopes"] = [{"scope": "family", "scope_id": None, "confirmatory": True}]
    with pytest.raises(AnalysisPlanError, match="safe identifier"):
        load_analysis_plan(_write(tmp_path, payload, "missing-id.json"))

    payload = _decision_payload()
    payload["scopes"] = [{"scope": "overall", "scope_id": "all", "confirmatory": True}]
    with pytest.raises(AnalysisPlanError, match="must be null"):
        load_analysis_plan(_write(tmp_path, payload, "overall-id.json"))

    payload = _decision_payload()
    payload["scopes"] = [{"scope": "tag", "scope_id": "Python", "confirmatory": True}]
    with pytest.raises(AnalysisPlanError, match="must match"):
        load_analysis_plan(_write(tmp_path, payload, "unsafe-id.json"))


def test_plan_rejects_duplicate_json_keys_and_nonfinite_confidence(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1,"analysis_id":"x","policy":"none",'
        '"candidate_profile_id":null,"reference_profile_id":null,"scopes":[],'
        '"margin_points":null,"confidence":null,"multiplicity":"none",'
        '"bootstrap_iterations":10000}',
        encoding="utf-8",
    )
    with pytest.raises(AnalysisPlanError, match="duplicate JSON key"):
        load_analysis_plan(duplicate)

    invalid = tmp_path / "nonfinite.json"
    invalid.write_text(
        '{"schema_version":1,"analysis_id":"x","policy":"superiority",'
        '"candidate_profile_id":"a","reference_profile_id":"b",'
        '"scopes":[{"scope":"overall","scope_id":null,"confirmatory":true}],'
        '"margin_points":5,"confidence":NaN,"multiplicity":"none",'
        '"bootstrap_iterations":10000}',
        encoding="utf-8",
    )
    with pytest.raises(AnalysisPlanError, match="non-finite"):
        load_analysis_plan(invalid)
