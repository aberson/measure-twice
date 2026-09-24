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
    assert record["routing_eligible"] is (state == "sealed")
    if state == "sealed":
        assert record["provider"] == "claude-cli"
        assert record["requested_model"] == "haiku"
        assert record["resolved_identities"] == ["claude-x"]
        assert len(record["receipt_sha256"]) == 64
    elif state == "unresolved":
        assert record["resolved_identities"] == [UNRESOLVED_MODEL_ID]
        assert record["eligibility"] == NOT_ROUTING_ELIGIBLE
        assert UNRESOLVED_MODEL_ID in md
    else:
        assert record["provider"] is None
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
