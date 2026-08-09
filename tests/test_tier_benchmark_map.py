"""Step-9 regression tests for the populated tier-claim ledger and rendered map."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from measure_twice.ledger import ClaimStatus, audit_ledger, load_ledger, render_ledger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
LEDGER_PATH = PROJECT_ROOT / "data" / "ledger" / "claims.jsonl"
MAP_PATH = PROJECT_ROOT / "docs" / "tier-benchmark-map.md"
BEGIN_MARKER = "<!-- BEGIN GENERATED LEDGER -->"
END_MARKER = "<!-- END GENERATED LEDGER -->"


def _generated_map_section(text: str) -> str:
    """Return the exact generated section, rejecting missing or duplicate delimiters."""
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1
    after_begin = text.split(BEGIN_MARKER + "\n", maxsplit=1)[1]
    return after_begin.split("\n" + END_MARKER, maxsplit=1)[0] + "\n"


def test_populated_ledger_has_required_coverage_and_honest_statuses() -> None:
    claims = load_ledger(LEDGER_PATH)
    ids = {claim.claim_id for claim in claims}

    assert len(claims) >= 15
    assert {
        "offload-primary-gates",
        "offload-style-only",
        "escalation-plan-init-seed",
        "escalation-user-debug-seed",
        "escalation-deep-research-seed",
        "memory-distill-fable-comparison-unmeasured",
        "deep-research-sonnet-arms-a3",
    } <= ids
    counts = Counter(claim.status for claim in claims)
    assert counts[ClaimStatus.MEASURED] == 1
    assert counts[ClaimStatus.PARTIAL] == 2
    assert counts[ClaimStatus.ASSERTED] >= 15


def test_every_citation_is_current_and_the_map_is_exactly_rendered() -> None:
    result = audit_ledger(LEDGER_PATH, WORKSPACE_ROOT)
    claims = load_ledger(LEDGER_PATH)

    assert result.newly_stale == ()
    assert result.stale_claim_ids == ()
    assert _generated_map_section(MAP_PATH.read_text(encoding="utf-8")) == render_ledger(claims)
