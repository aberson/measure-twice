"""Provider-neutral coding-agent benchmark inputs and CLI dependency seam."""

from __future__ import annotations

from dataclasses import dataclass

from measure_twice.agent_bench._wire import AgentInputError
from measure_twice.agent_bench.analysis import (
    AnalysisPlan,
    AnalysisPlanError,
    AnalysisScope,
    analysis_plan_hash,
    load_analysis_plan,
)
from measure_twice.agent_bench.models import (
    ExecutionProfile,
    ModelRegistry,
    ModelSpec,
    ModelSpecError,
    dispatch_by_provider,
    execution_profile_hash,
    load_execution_profile,
    load_model_registry,
    selected_profile_hash,
)
from measure_twice.agent_bench.suite import (
    AgentSuite,
    AgentSuiteError,
    AgentTask,
    instrument_hash,
    load_agent_suite,
)


@dataclass(frozen=True, slots=True)
class AgentCliDeps:
    """Dependencies injected into agent CLI handlers.

    Step 25's structural validator is deliberately pure and needs no runtime dependency.  The
    stable bundle exists now so later provider/evaluator steps can add seams without importing the
    root CLI or changing its registration shape.
    """


__all__ = [
    "AgentCliDeps",
    "AgentInputError",
    "AgentSuite",
    "AgentSuiteError",
    "AgentTask",
    "AnalysisPlan",
    "AnalysisPlanError",
    "AnalysisScope",
    "ExecutionProfile",
    "ModelRegistry",
    "ModelSpec",
    "ModelSpecError",
    "analysis_plan_hash",
    "dispatch_by_provider",
    "execution_profile_hash",
    "instrument_hash",
    "load_agent_suite",
    "load_analysis_plan",
    "load_execution_profile",
    "load_model_registry",
    "selected_profile_hash",
]
