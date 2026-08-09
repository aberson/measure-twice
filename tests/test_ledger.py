"""Offline tests for the Step-8 quote-hashed evidence ledger and ``mt claims`` CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from measure_twice.cli import main
from measure_twice.ledger import (
    Claim,
    ClaimSource,
    ClaimStatus,
    LedgerError,
    audit_ledger,
    load_ledger,
    render_claim_list,
    render_ledger,
    source_quote_sha256,
    write_ledger,
)


def _source(workspace: Path, text: str = "first\nquoted line\nlast\n") -> ClaimSource:
    path = workspace / "policy" / "source.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    provisional = ClaimSource(
        file="policy/source.md",
        lines="2",
        quote_sha256="0" * 64,
    )
    return ClaimSource(
        file=provisional.file,
        lines=provisional.lines,
        quote_sha256=source_quote_sha256(provisional, workspace),
    )


def _claim(source: ClaimSource, *, status: ClaimStatus = ClaimStatus.ASSERTED) -> Claim:
    evidence = ("run_20260718T022629Z_a3e110",) if status is ClaimStatus.MEASURED else ()
    preregistration = (
        "Measure the production verdict artifact before selecting a tier." if evidence else None
    )
    return Claim(
        claim_id="style-lens-local-safe",
        statement="The style lens can use a local model under its registered conditions.",
        decision_surface="offload-config:build-step-style",
        sources=(source,),
        status=status,
        evidence=evidence,
        verdict=None,
        preregistration=preregistration,
        last_verified_utc="2026-08-09T00:00:00Z",
    )


def test_ledger_round_trips_in_canonical_jsonl(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    ledger_path = tmp_path / "data" / "ledger" / "claims.jsonl"

    write_ledger(ledger_path, [_claim(source)])

    assert load_ledger(ledger_path) == (_claim(source),)
    raw = ledger_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw)["claim_id"] == "style-lens-local-safe"


def test_whitespace_only_tracked_ledger_is_an_empty_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "data" / "ledger" / "claims.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("\n", encoding="utf-8")

    assert load_ledger(ledger_path) == ()


@pytest.mark.parametrize(
    ("evidence", "preregistration", "error"),
    [
        ((), "Pre-register the claim before the run.", "no evidence run id"),
        (("run_20260718T022629Z_a3e110",), None, "no preregistration sentence"),
    ],
)
def test_measured_claim_requires_evidence_and_preregistration(
    tmp_path: Path, evidence: tuple[str, ...], preregistration: str | None, error: str
) -> None:
    source = _source(tmp_path / "workspace")
    claim = Claim(
        claim_id="measured-claim",
        statement="A measured tier-routing claim.",
        decision_surface="offload-config:test",
        sources=(source,),
        status=ClaimStatus.MEASURED,
        evidence=evidence,
        verdict="supported",
        preregistration=preregistration,
        last_verified_utc="2026-08-09T00:00:00Z",
    )

    with pytest.raises(LedgerError, match=error):
        write_ledger(tmp_path / "claims.jsonl", [claim])


def test_audit_marks_a_mutated_citation_stale_and_persists_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    ledger_path = tmp_path / "data" / "ledger" / "claims.jsonl"
    write_ledger(ledger_path, [_claim(source, status=ClaimStatus.PARTIAL)])

    (workspace / "policy" / "source.md").write_text(
        "first\nchanged quote\nlast\n", encoding="utf-8"
    )
    result = audit_ledger(ledger_path, workspace)

    assert result.newly_stale == ("style-lens-local-safe",)
    assert result.stale_claim_ids == ("style-lens-local-safe",)
    assert result.issues[0].reason == "quoted source text changed (SHA-256 no longer matches)"
    assert load_ledger(ledger_path)[0].status is ClaimStatus.STALE


def test_audit_reports_fresh_citation_without_rewriting_status(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    ledger_path = tmp_path / "data" / "ledger" / "claims.jsonl"
    write_ledger(ledger_path, [_claim(source)])

    result = audit_ledger(ledger_path, workspace)

    assert result.fresh_count == 1
    assert result.stale_claim_ids == ()
    assert result.newly_stale == ()


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        json.dumps({"claim_id": "only-one-field"}),
        "",
    ],
)
def test_load_rejects_malformed_or_blank_jsonl_rows(tmp_path: Path, line: str) -> None:
    ledger_path = tmp_path / "claims.jsonl"
    ledger_path.write_text(line + "\nextra\n" if line == "" else line + "\n", encoding="utf-8")

    with pytest.raises(LedgerError):
        load_ledger(ledger_path)


@pytest.mark.parametrize(
    "unsafe_file", ["../secret.txt", "/absolute.txt", "C:/secret.txt", "a\\b.txt"]
)
def test_source_paths_must_be_safe_workspace_relative_paths(unsafe_file: str) -> None:
    raw = {
        "file": unsafe_file,
        "lines": "1-2",
        "quote_sha256": "0" * 64,
    }

    with pytest.raises(LedgerError, match=r"workspace-relative|separators"):
        ClaimSource.from_mapping(raw, label="source")


def test_source_hash_covers_only_the_cited_lines(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    path = workspace / source.file
    original = source_quote_sha256(source, workspace)

    path.write_text("changed outside\nquoted line\nlast\n", encoding="utf-8")

    assert source_quote_sha256(source, workspace) == original


def test_rendering_is_deterministic_and_grouped_by_status(tmp_path: Path) -> None:
    source = _source(tmp_path / "workspace")
    measured = _claim(source, status=ClaimStatus.MEASURED)
    asserted = _claim(source)
    asserted = Claim(
        claim_id="asserted-z",
        statement=asserted.statement,
        decision_surface=asserted.decision_surface,
        sources=asserted.sources,
        status=asserted.status,
        evidence=asserted.evidence,
        verdict=asserted.verdict,
        preregistration=asserted.preregistration,
        last_verified_utc=asserted.last_verified_utc,
    )

    rendered = render_ledger([asserted, measured])

    assert rendered.index("## MEASURED (1)") < rendered.index("## ASSERTED (1)")
    assert "`style-lens-local-safe`" in rendered
    assert "`asserted-z`" in rendered
    assert render_claim_list([asserted, measured]).splitlines()[0].startswith("ASSERTED")


def test_claims_cli_lists_renders_and_fails_stale_audits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    ledger_path = tmp_path / "claims.jsonl"
    write_ledger(ledger_path, [_claim(source)])
    base = ["claims", "--ledger", str(ledger_path), "--workspace-root", str(workspace)]

    assert main([*base, "list"]) == 0
    assert "ASSERTED\tstyle-lens-local-safe" in capsys.readouterr().out
    assert main([*base, "render"]) == 0
    assert "# Evidence ledger" in capsys.readouterr().out
    assert main([*base, "audit"]) == 0
    assert "PASS - 1 claim(s) fresh" in capsys.readouterr().out

    (workspace / source.file).write_text("first\nchanged quote\nlast\n", encoding="utf-8")
    assert main([*base, "audit"]) == 1
    captured = capsys.readouterr()
    assert "STALE - 1 stale claim(s), 1 newly marked" in captured.out
    assert "quoted source text changed" in captured.err


def test_source_hash_is_standard_sha256_for_the_exact_quote(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)

    assert source.quote_sha256 == hashlib.sha256(b"quoted line").hexdigest()
