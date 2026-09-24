"""Real ``mt report`` entry-point checks for Step 57's additive execution evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import _iid
from test_report import (
    _run_scored,
    _strip_execution_receipt,
    _unresolved_claude_stdout,
    _verdict_suite,
)

from measure_twice.adapters.base import UNRESOLVED_MODEL_ID
from measure_twice.cli import main
from measure_twice.model_sweep_execution import canonical_sha256
from measure_twice.report import LEGACY_UNSEALED, NOT_ROUTING_ELIGIBLE


@pytest.mark.parametrize("state", ["sealed", "unresolved", "legacy"])
def test_report_execution_identity_entry_points(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    out = tmp_path / "data"
    response = lambda p: "pass" if _iid(p) == "i1" else "flag"  # noqa: E731
    if state == "unresolved":
        result = _run_scored(
            _verdict_suite(),
            out_dir=out,
            roster=["haiku"],
            claude=lambda p: _unresolved_claude_stdout(response(p)),
        )
    else:
        result = _run_scored(_verdict_suite(), out_dir=out, roster=["haiku"], claude=response)
    if state == "legacy":
        _strip_execution_receipt(out, result.run_id)

    assert main(["report", result.run_id, "--out", str(out)]) == 0
    md = capsys.readouterr().out
    assert "100.0" in md  # the official score is unchanged in all three states
    assert (out / "reports" / f"{result.run_id}.md").read_text(
        encoding="utf-8"
    ).strip() == md.strip()

    assert main(["report", result.run_id, "--jsonl", "--out", str(out)]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["suite_score"] == 100.0
    assert record["seal_status"] == (LEGACY_UNSEALED if state == "legacy" else "prompt-only-v1")
    assert record["routing_eligible"] is (None if state == "sealed" else False)
    if state == "sealed":
        assert record["provider"] == "claude-cli"
        assert record["requested_model"] == "haiku"
        assert record["resolved_identities"] == ["claude-x"]
        assert record["eligibility"] == "PRELIMINARY_SEAL_IDENTITY_OK"
        assert len(record["receipt_sha256"]) == 64
    elif state == "unresolved":
        assert record["stored_identities"] == [UNRESOLVED_MODEL_ID]
        assert record["resolved_identities"] == []
        assert record["eligibility"] == NOT_ROUTING_ELIGIBLE
        assert UNRESOLVED_MODEL_ID in md
    else:
        assert record["provider"] is None
        assert record["stored_identities"] == ["claude-x"]
        assert record["resolved_identities"] == []
        assert record["identity_provenance"] == "UNVERIFIED_LEGACY"
        assert record["receipt_sha256"] is None
        assert LEGACY_UNSEALED in md

    assert main(["report", result.run_id, "--html", "--out", str(out)]) == 0
    html = (out / "reports" / f"{result.run_id}.html").read_text(encoding="utf-8")
    assert record["seal_status"] in html
    assert record["eligibility"] in html
    if state == "sealed":
        assert record["receipt_sha256"] in html
        assert "claude-x" in html
    elif state == "unresolved":
        assert UNRESOLVED_MODEL_ID in html


def test_report_compare_enforces_execution_and_shows_per_run_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "data"
    suite = _verdict_suite()
    first = _run_scored(suite, out_dir=out, roster=["haiku"])
    second = _run_scored(suite, out_dir=out, roster=["haiku"])
    command = ["report", first.run_id, "--compare", second.run_id, "--out", str(out)]

    assert main(command) == 0
    compared = capsys.readouterr().out
    assert compared.count("## Execution receipt & identity") == 2
    assert compared.count("| haiku | claude-cli | haiku | claude-x |") == 2
    assert compared.count("- **Receipt hash:**") == 2
    assert "PRELIMINARY_SEAL_IDENTITY_OK" in compared

    manifest_path = out / "runs" / second.run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = manifest["execution_receipt"]
    receipt["claude_cli"]["version"] = "different-claude-version"
    receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert main(command) == 1
    assert "different receipt_sha256" in capsys.readouterr().err

    receipt["claude_cli"]["version"] = "test-claude 1.0"
    receipt["context_profile_sha256"] = "0" * 64
    receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert main(command) == 1
    assert "different context_profile_sha256" in capsys.readouterr().err

    _strip_execution_receipt(out, second.run_id)
    assert main(command) == 1
    assert "sealed and legacy-unsealed" in capsys.readouterr().err

    _strip_execution_receipt(out, first.run_id)
    assert main(command) == 0
    legacy_comparison = capsys.readouterr().out
    assert legacy_comparison.count("LEGACY_UNSEALED") >= 2
    assert legacy_comparison.count("UNVERIFIED_LEGACY") >= 2
    assert "NOT_ROUTING_ELIGIBLE" in legacy_comparison
