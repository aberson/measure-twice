"""Nested ``mt agent`` CLI registration without a dependency on the root CLI module."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from measure_twice.agent_bench import AgentCliDeps
from measure_twice.agent_bench.models import (
    ModelSpecError,
    execution_profile_hash,
    load_execution_profile,
    load_model_registry,
    selected_profile_hash,
    validate_execution_profile_binding,
)
from measure_twice.agent_bench.suite import AgentSuiteError, load_agent_suite

AgentHandler = Callable[[argparse.Namespace], int]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_REGISTRY = _PROJECT_ROOT / "profiles" / "agent-models-candidates.json"
DEFAULT_EXECUTION_PROFILE = _PROJECT_ROOT / "profiles" / "agent-execution-v1.json"


def _handle_validate(args: argparse.Namespace, deps: AgentCliDeps) -> int:
    """Validate all structural inputs; Step 25 intentionally cannot execute task anchors."""

    del deps  # Reserved DI seam; structure-only performs no provider/evaluator work.
    try:
        registry = load_model_registry(args.profiles)
        execution = load_execution_profile(args.execution_profile)
        suite = load_agent_suite(args.suite_dir)
        validate_execution_profile_binding(
            execution,
            suite_execution_profile_id=suite.execution_profile_id,
            selected_profiles=registry.models,
        )
        run_policy = execution.run_policy[suite.run_class]
        loaded_task_count = len(suite.task_specs)
        if run_policy.task_count != loaded_task_count:
            raise ModelSpecError(
                f"execution profile.run_policy.{suite.run_class}.task_count "
                f"must equal loaded suite task count ({loaded_task_count}), "
                f"got {run_policy.task_count}"
            )
        selected_model_count = len(registry.models)
        if run_policy.model_count != selected_model_count:
            raise ModelSpecError(
                f"execution profile.run_policy.{suite.run_class}.model_count "
                f"must equal selected model count ({selected_model_count}), "
                f"got {run_policy.model_count}"
            )
        profile_sha256 = selected_profile_hash(registry.models)
        execution_sha256 = execution_profile_hash(execution)
        instrument_sha256 = suite.instrument_hash
    except (ModelSpecError, AgentSuiteError) as exc:
        print(f"agent validate: {exc}", file=sys.stderr)
        return 1

    if not args.structure_only:
        print(
            "agent validate: complete evaluator-anchor validation is not available in Step 25; "
            "use --structure-only (Step 27 adds the Linux sandboxed anchors)",
            file=sys.stderr,
        )
        return 1

    print(
        f"{suite.suite_id}: valid structure "
        f"({len(suite.task_specs)} task(s), run_class={suite.run_class})"
    )
    print(f"instrument_hash: {instrument_sha256}")
    print(f"selected_profile_hash: {profile_sha256}")
    print(f"execution_profile_hash: {execution_sha256}")
    return 0


def register_agent_cli(
    root_parser: argparse.ArgumentParser,
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    handlers: dict[str, AgentHandler],
    deps: AgentCliDeps,
) -> None:
    """Register ``mt agent`` through the root parser's single extension seam."""

    del root_parser  # Kept in the stable registration contract for later shared options.
    agent_parser = subparsers.add_parser(
        "agent",
        help="validate and operate provider-neutral coding-agent benchmarks",
        description="Strict coding-agent benchmark inputs and offline/runtime operations.",
    )
    actions = agent_parser.add_subparsers(dest="agent_action", required=True, metavar="<action>")
    validate_parser = actions.add_parser(
        "validate",
        help="validate an agent-suite bundle and its profile contracts",
        description="Strictly load a complete agent-suite bundle, model registry, and execution "
        "profile. --structure-only is cross-platform and never executes suite code.",
    )
    validate_parser.add_argument(
        "suite_dir", metavar="<suite-dir>", help="directory containing suite.json and task bundles"
    )
    validate_parser.add_argument(
        "--profiles",
        default=str(DEFAULT_MODEL_REGISTRY),
        metavar="<file>",
        help="strict model registry (default: profiles/agent-models-candidates.json)",
    )
    validate_parser.add_argument(
        "--execution-profile",
        default=str(DEFAULT_EXECUTION_PROFILE),
        metavar="<file>",
        help="whole execution-profile file (default: profiles/agent-execution-v1.json)",
    )
    validate_parser.add_argument(
        "--structure-only",
        action="store_true",
        help="perform cross-platform input/path/hash validation without executing task code",
    )

    def _agent(args: argparse.Namespace) -> int:
        if args.agent_action == "validate":
            return _handle_validate(args, deps)
        # Required argparse subparsers make this unreachable; preserve fail-closed dispatch.
        print(f"agent: unsupported action {args.agent_action!r}", file=sys.stderr)
        return 1

    handlers["agent"] = _agent


__all__ = [
    "DEFAULT_EXECUTION_PROFILE",
    "DEFAULT_MODEL_REGISTRY",
    "register_agent_cli",
]
