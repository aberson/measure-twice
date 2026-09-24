"""Offline verification of a stored three-alias context qualification run.

The PowerShell wrapper plants sentinels and makes the calls. This module reads the resulting
store through the production suite/row/receipt readers, both immediately and in VerifyOnly mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

from measure_twice.config import load_config
from measure_twice.model_sweep_execution import ExecutionReceipt, is_concrete_provider_identity
from measure_twice.runner import _read_execution_receipt, _read_manifest, _read_rows, load_run_suite
from measure_twice.suite import load_suite

PREREGISTRATION = (
    "All three live Claude canaries (haiku, sonnet, and opus) will return the requested token "
    "without reproducing any unique repository, environment, customization, or session "
    "sentinel, and every arm will record provider, requested alias, concrete resolved identity, "
    "Claude CLI path and version, and the same context-profile hash; any sentinel or unresolved "
    "identity fails the 3/3 qualification and blocks Step 13."
)
MODELS = ("haiku", "sonnet", "opus")
PRODUCER_VERSION = "step57-context-qualification-v1"
EXPECTED = {
    "canary-context": "ALEPH",
}


class QualificationError(ValueError):
    """Stored evidence cannot support a qualification PASS."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise QualificationError(message)


def _sha(path: Path) -> str:
    _require(path.is_file(), f"missing evidence file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_index(path: Path) -> dict[str, Any]:
    _require(path.is_file(), f"qualification index missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualificationError("qualification index must be an object")
    return cast("dict[str, Any]", value)


def evaluate(index_path: Path, out_dir: Path, *, verify_only: bool) -> dict[str, Any]:
    """Check every expected terminal cell and freeze/compare the evidence file digests."""
    index = _read_index(index_path)
    _require(index.get("schema_version") == 1, "unsupported qualification index schema")
    producer = {
        "version": PRODUCER_VERSION,
        "wrapper_sha256": _sha(
            Path(__file__).resolve().parent.parent / "scripts" / "qualify-model-sweep-context.ps1"
        ),
        "verifier_sha256": _sha(Path(__file__).resolve()),
    }
    _require(index.get("producer") == producer, "qualification producer version or digest changed")
    _require(index.get("preregistration") == PREREGISTRATION, "preregistration mismatch")
    _require(index.get("models") == list(MODELS), "qualification must select all three aliases")
    _require(index.get("samples") == 1, "qualification must use one sample per cell")
    if verify_only:
        _require(index.get("status") == "PASS", "stored qualification is not PASS")
        _require(index.get("qualification") == "PASS", "stored qualification verdict is not PASS")
        _require(
            index.get("passed") == 3 and index.get("total") == 3, "stored pass count is not 3/3"
        )
    else:
        _require(index.get("status") == "IN_PROGRESS", "pre-call index is not IN_PROGRESS")

    source_suite = index.get("suite")
    source_profile = index.get("profile")
    if not isinstance(source_suite, str) or not isinstance(source_profile, str):
        raise QualificationError("source suite or profile path missing")
    source_hashes = {
        "source_suite_sha256": _sha(Path(source_suite)),
        "source_profile_sha256": _sha(Path(source_profile)),
    }
    _require(index.get("source_hashes") == source_hashes, "source suite or profile changed")
    frozen_suite_path = (
        Path(__file__).resolve().parent.parent / "suites" / "model-sweep-context-canary-v1.json"
    )
    frozen_suite = load_suite(frozen_suite_path)
    supplied_suite = load_suite(source_suite)
    _require(supplied_suite.item_hash == frozen_suite.item_hash, "supplied canary prompts changed")
    config = load_config(source_profile)
    profile = config.execution_profile
    if profile is None:
        raise QualificationError("execution profile missing from supplied config")

    run_id = index.get("run_id")
    if not isinstance(run_id, str) or not run_id.startswith("run_"):
        raise QualificationError("run id is missing")
    run_dir = out_dir / "runs" / run_id
    _require(
        run_dir.is_dir() and run_dir.resolve().parent == (out_dir / "runs").resolve(),
        "run directory is missing or outside the run store",
    )
    manifest_path = run_dir / "manifest.json"
    rows_path = run_dir / "rows.jsonl"
    suite_path = run_dir / "suite.json"
    hashes = {
        "manifest_sha256": _sha(manifest_path),
        "rows_sha256": _sha(rows_path),
        "suite_snapshot_sha256": _sha(suite_path),
    }
    manifest = _read_manifest(run_dir)
    _require(manifest.get("run_id") == run_id, "manifest run id mismatch")
    _require(
        manifest.get("preregistration") == PREREGISTRATION, "manifest preregistration mismatch"
    )
    _require(manifest.get("roster") == list(MODELS), "manifest roster mismatch")
    _require(manifest.get("samples_per_cell") == 1, "manifest sample count mismatch")
    suite = load_run_suite(run_id, out_dir)
    _require(suite.suite == "model-sweep-context-canary-v1", "wrong canary suite")
    _require(suite.scoring.type == "exact", "canary suite must use exact scoring")
    _require(
        {item.id: item.expected for item in suite.items} == EXPECTED,
        "canary item ids or expected tokens differ from the frozen suite",
    )
    _require(len(suite.items) == len(EXPECTED), "duplicate or extra canary item")
    _require(manifest.get("suite_hash") == suite.item_hash, "manifest suite hash mismatch")
    _require(suite.item_hash == frozen_suite.item_hash, "run used a different canary suite")
    _require(index.get("suite_hash") in (None, suite.item_hash), "index suite hash mismatch")

    receipt = _read_execution_receipt(manifest)
    if not isinstance(receipt, ExecutionReceipt):
        raise QualificationError("execution receipt missing")
    bindings = {binding.alias: binding for binding in receipt.bindings}
    _require(set(bindings) == set(MODELS), "execution bindings differ from canary roster")
    _require(
        all(
            bindings[model].provider == "claude-cli" and bindings[model].requested_model == model
            for model in MODELS
        ),
        "canary binding provider or requested alias mismatch",
    )
    if receipt.claude_cli is None:
        raise QualificationError("Claude CLI runtime evidence missing")
    _require(
        bool(receipt.claude_cli.executable and receipt.claude_cli.version),
        "Claude CLI executable or version missing",
    )
    _require(bool(receipt.context_profile_sha256), "context profile hash missing")
    _require(receipt.profile_id == profile.id, "execution profile id mismatch")
    _require(receipt.execution_profile_sha256 == profile.sha256, "execution profile hash mismatch")
    _require(
        receipt.provider_profile_sha256 == profile.provider_profile_sha256,
        "provider profile hash mismatch",
    )
    _require(
        receipt.context_profile_sha256 == profile.claude.sha256, "context profile hash mismatch"
    )

    sentinels = index.get("sentinels")
    if not isinstance(sentinels, dict):
        raise QualificationError("sentinel index missing")
    values = [
        sentinels.get(key) for key in ("repository", "environment", "customization", "session")
    ]
    _require(
        all(isinstance(v, str) and v.startswith("MT-CANARY-") and len(v) > 30 for v in values),
        "sentinel values missing",
    )
    _require(len(set(values)) == 4, "sentinels are not unique")
    sentinel_values = cast("list[str]", values)
    planting_dir = out_dir / "planting"
    repository_snapshot = planting_dir / "repository.txt"
    customization_snapshot = planting_dir / "CLAUDE.md"
    environment_snapshot = planting_dir / "environment.json"
    planting_hashes = {
        "repository_sha256": _sha(repository_snapshot),
        "customization_sha256": _sha(customization_snapshot),
        "environment_sha256": _sha(environment_snapshot),
    }
    _require(index.get("planting_hashes") == planting_hashes, "planted sentinel evidence changed")
    _require(
        repository_snapshot.read_text(encoding="utf-8") == sentinels["repository"],
        "repository sentinel snapshot mismatch",
    )
    customization_text = customization_snapshot.read_text(encoding="utf-8")
    _require(
        sentinels["customization"] in customization_text
        and sentinels["session"] in customization_text,
        "customization or simulated session snapshot mismatch",
    )
    environment_snapshot_data = json.loads(environment_snapshot.read_text(encoding="utf-8"))
    _require(
        environment_snapshot_data
        == {
            "environment": sentinels["environment"],
            "session": sentinels["session"],
        },
        "environment sentinel snapshot mismatch",
    )

    rows, torn = _read_rows(run_dir)
    _require(not torn, "rows.jsonl has a torn tail")
    expected_keys = {(model, item, 0) for model in MODELS for item in EXPECTED}
    actual_keys = [(row.model, row.item_id, row.sample_k) for row in rows]
    _require(
        len(rows) == len(expected_keys) and set(actual_keys) == expected_keys,
        "run does not have exactly one terminal row for every canary cell",
    )
    arms = []
    for model in MODELS:
        model_rows = [row for row in rows if row.model == model]
        for row in model_rows:
            _require(row.run_id == run_id, "row run id mismatch")
            _require(row.error is None, f"{model}/{row.item_id} ended with an error")
            _require(
                not any(value in row.response_raw for value in sentinel_values),
                f"{model}/{row.item_id} reproduced a planted sentinel",
            )
            _require(
                row.response_raw.strip() == EXPECTED[row.item_id],
                f"{model}/{row.item_id} did not return the requested token",
            )
            _require(row.score == 1.0, f"{model}/{row.item_id} was not scored correct")
            _require(
                is_concrete_provider_identity(row.model_id_resolved, bindings[model].provider),
                f"{model}/{row.item_id} has unresolved provider identity (malformed or alias-only)",
            )
        arms.append(
            {
                "model": model,
                "provider": bindings[model].provider,
                "requested_model": bindings[model].requested_model,
                "resolved_identities": sorted({row.model_id_resolved for row in model_rows}),
                "identity_resolved": True,
                "sentinel_leak": False,
                "terminal_cells": len(model_rows),
            }
        )
    result: dict[str, Any] = {
        "run_id": run_id,
        "suite_hash": suite.item_hash,
        "passed": 3,
        "total": 3,
        "arms": arms,
        "receipt": {
            "sealing_mode": receipt.sealing_mode,
            "profile_id": receipt.profile_id,
            "execution_profile_sha256": receipt.execution_profile_sha256,
            "provider_profile_sha256": receipt.provider_profile_sha256,
            "context_profile_sha256": receipt.context_profile_sha256,
            "receipt_sha256": receipt.receipt_sha256,
            "claude_executable": receipt.claude_cli.executable,
            "claude_version": receipt.claude_cli.version,
        },
        "evidence_hashes": hashes,
        "source_hashes": source_hashes,
        "planting_hashes": planting_hashes,
    }
    if verify_only:
        for key in (
            "suite_hash",
            "passed",
            "total",
            "arms",
            "receipt",
            "evidence_hashes",
            "source_hashes",
            "planting_hashes",
        ):
            _require(index.get(key) == result[key], f"stored {key} changed since qualification")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.index, args.out, verify_only=args.verify_only)
    except (QualificationError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}))
        print(f"qualification evidence invalid: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
