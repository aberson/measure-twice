"""Report + smoke-gate tests — OFFLINE, stub adapter factories, ZERO live calls (plan §5/§6/§7).

Every model call is routed through the shared offline ``StubAdapters`` DI-seam scaffolding
(``tests/conftest.py`` — one source of truth, reused by test_runner); the REAL deterministic verdict
scorer scores each deterministic run and the REAL rubric run-scorer (with a stub judge-caller) the
rubric run, so a canned-but-unparseable response genuinely trips the parse-fail gate. The suite
NEVER invokes the real ``claude`` binary nor touches the network. Coverage per the Step-7 done-when:

  * per-run report renders for a STUB MULTI-MODEL run: each model's 0-100 score, item count, and the
    no-response / parse-fail / error columns
  * a RUBRIC parse-fail is counted in the report's parse-fail column (would be missed if the counter
    gated on ``scorer == "verdict"`` — it must count the canonical PARSE_FAIL_MARKER for ANY scorer)
  * cross-run comparison: equal suite_hash -> one table; different suite_hash -> ReportError (loud)
  * ``mt smoke --claude`` WIRING offline: stub factory -> exit 0 + a report; unparseable -> non-zero
    (the parse-fail gate); the claude-CLI-not-on-PATH preflight aborts loud without running; and
    ``mt smoke --local`` skips the reachability preflight when a stub is injected, and ABORTS (never
    auto-spawns) when the operator-started endpoint is down
  * DETERMINISTIC render: same run -> byte-identical markdown
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
from conftest import StubAdapters, _iid  # shared offline stub scaffolding (tests/conftest.py)

from measure_twice.adapters.base import ModelCallResult
from measure_twice.cli import CliDeps, _local_endpoint_unreachable, main
from measure_twice.config import ENV_VAR, RunConfig
from measure_twice.report import (
    ReportError,
    build_comparison,
    build_run_report,
    render_comparison,
    render_run_report,
    run_report_jsonl,
)
from measure_twice.runner import RunResult, run, score_run_batch
from measure_twice.scoring import JUDGE_SAMPLE_K, make_deterministic_scorer, make_rubric_run_scorer
from measure_twice.suite import Item, ScoringSpec, Suite

# --- Suite builders ----------------------------------------------------------------------


def _verdict_suite(name: str = "rep", *, item_ids: tuple[str, str] = ("i1", "i2")) -> Suite:
    """A 2-item verdict suite: item_ids[0] expects ``pass``, item_ids[1] expects ``flag``."""
    a, b = item_ids
    items = [
        Item(
            id=a,
            tags=["t"],
            prompt=f"PROMPT::{a}",
            expected="pass",
            difficulty_prior=0.5,
            provenance="authored",
        ),
        Item(
            id=b,
            tags=["t"],
            prompt=f"PROMPT::{b}",
            expected="flag",
            difficulty_prior=0.5,
            provenance="authored",
        ),
    ]
    scoring = ScoringSpec(type="verdict", labels=["pass", "flag"])
    return Suite(suite=name, version=1, description="d", domain="dom", scoring=scoring, items=items)


def _rubric_suite(name: str = "rub") -> Suite:
    """A 2-item rubric suite (``expected`` IS the rubric text; suites carry all content)."""
    items = [
        Item(
            id=iid,
            tags=["t"],
            prompt=f"PROMPT::{iid}",
            expected="Grade the response's clarity from 0 to 10.",
            difficulty_prior=0.5,
            provenance="authored",
        )
        for iid in ("r1", "r2")
    ]
    return Suite(
        suite=name,
        version=1,
        description="d",
        domain="dom",
        scoring=ScoringSpec(type="rubric"),
        items=items,
    )


def _run_scored(
    suite: Suite,
    *,
    out_dir: Path,
    roster: list[str],
    claude=lambda p: "pass",
    local=lambda p: "pass",
) -> RunResult:
    """Sweep ``suite`` with the REAL deterministic scorer through the shared stub adapters."""
    stub = StubAdapters(local=local, claude=claude)
    return run(
        suite=suite,
        config=RunConfig(),
        out_dir=out_dir,
        roster=roster,
        samples_per_cell=1,
        scorer=make_deterministic_scorer(suite.scoring),
        local_transport_factory=stub.local_factory(),
        claude_runner_factory=stub.claude_factory(),
    )


# --- Per-run report renders for a stub multi-model run (Done-when) ------------------------


def test_report_renders_stub_multi_model_run(tmp_path: Path) -> None:
    suite = _verdict_suite()
    # haiku (claude adapter) answers both correctly -> 100.0; general-35b (local) gets i1 right and
    # emits an UNPARSEABLE i2 -> a recorded parse-fail and a 50.0 score.
    result = _run_scored(
        suite,
        out_dir=tmp_path,
        roster=["haiku", "general-35b"],
        claude=lambda p: "pass" if _iid(p) == "i1" else "flag",
        local=lambda p: "pass" if _iid(p) == "i1" else "banana",
    )
    report = build_run_report(result.run_id, tmp_path)

    by_model = {m.model: m for m in report.models}
    assert set(by_model) == {"haiku", "general-35b"}
    assert by_model["haiku"].suite_score == 100.0
    assert by_model["haiku"].n_items == 2
    assert by_model["haiku"].n_parse_fail == 0
    assert by_model["general-35b"].suite_score == 50.0
    assert by_model["general-35b"].n_items == 2
    assert by_model["general-35b"].n_parse_fail == 1
    assert by_model["general-35b"].n_no_response == 0
    assert by_model["general-35b"].n_error == 0

    md = render_run_report(report)
    # each model's 0-100 score, the roster/identity, and the first-class signal columns are present.
    for token in (
        "haiku",
        "general-35b",
        "100.0",
        "50.0",
        result.run_id,
        suite.item_hash,
        "No-response",
        "Parse-fail",
        "Error/defer",
    ):
        assert token in md


def test_report_surfaces_no_response_and_error_columns(tmp_path: Path) -> None:
    """A no-response cell force-scores 0 (counted in the mean); an error cell is excluded."""
    suite = _verdict_suite()
    result = _run_scored(
        suite,
        out_dir=tmp_path,
        roster=["general-35b"],
        local=lambda p: "" if _iid(p) == "i1" else urllib.error.URLError("down"),
    )
    model = {m.model: m for m in build_run_report(result.run_id, tmp_path).models}["general-35b"]
    assert model.n_no_response == 1  # i1 empty -> no-response force-0
    assert model.n_error == 1  # i2 transport error
    assert model.n_scored == 1  # only the no-response force-0 has a numeric score
    assert model.suite_score == 0.0  # 100 * mean([0.0])


def test_report_counts_rubric_parse_fail(tmp_path: Path) -> None:
    """A RUBRIC parse-fail (scorer=='rubric', not 'verdict') MUST count in the parse-fail column.

    Gating the counter on ``scorer == VERDICT_SCORER`` would silently miss it — the rubric row is
    force-scored 0.0 into the mean yet reported as parse_fail=0, masking the exact signal the column
    exists to surface (measurement-validity). The counter keys off the canonical PARSE_FAIL_MARKER,
    which every scorer funnels an unparseable cell through.
    """
    suite = _rubric_suite()
    # Sweep raw responses (collect-only default), then apply the REAL rubric run-scorer offline.
    result = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        local_transport_factory=StubAdapters(local=lambda p: "a model answer").local_factory(),
    )

    def judge_caller(judge_prompt: str, judge_alias: str) -> ModelCallResult:
        # r1 -> a parseable SCORE; r2 -> unparseable (a returned reply with no SCORE line). For the
        # single judge that is 3 fails / 6 attempts = 0.50 (NOT > 0.50), so the per-judge gate does
        # NOT fire and r2 lands as an all-parse-fail rubric row.
        parseable = "PROMPT::r1" in judge_prompt
        text = "SCORE: 8\nRATIONALE: clear" if parseable else "no parseable score in this reply"
        return ModelCallResult.success(
            response_raw=text, resolved_model=judge_alias, elapsed_s=0.01
        )

    run_scorer = make_rubric_run_scorer(
        judges=["sonnet"], judge_caller=judge_caller, k=JUDGE_SAMPLE_K
    )
    score_run_batch(run_id=result.run_id, out_dir=tmp_path, run_scorer=run_scorer)

    model = {m.model: m for m in build_run_report(result.run_id, tmp_path).models}["general-35b"]
    assert model.n_parse_fail == 1  # the rubric parse-fail — missed if the counter gated on scorer
    assert model.n_no_response == 0
    assert model.n_error == 0
    assert model.n_scored == 2  # r1 (0.8) + r2 (force-0) both carry a numeric score
    assert model.suite_score == 40.0  # 100 * mean([0.8, 0.0])


# --- Cross-run comparison: equal hash compares; different hash fails loud -----------------


def test_cross_run_equal_hash_compares(tmp_path: Path) -> None:
    suite = _verdict_suite()
    r1 = _run_scored(  # haiku 100.0
        suite,
        out_dir=tmp_path,
        roster=["haiku"],
        claude=lambda p: "pass" if _iid(p) == "i1" else "flag",
    )
    r2 = _run_scored(  # haiku 50.0 (i2 answered "pass", wrong but parseable)
        suite, out_dir=tmp_path, roster=["haiku"], claude=lambda p: "pass"
    )
    comparison = build_comparison([r1.run_id, r2.run_id], tmp_path)

    assert comparison.suite_hash == suite.item_hash
    assert comparison.models == ["haiku"]
    table = render_comparison(comparison)
    assert r1.run_id in table
    assert r2.run_id in table
    assert "100.0" in table
    assert "50.0" in table


def test_cross_run_hash_mismatch_fails_loud(tmp_path: Path) -> None:
    r1 = _run_scored(_verdict_suite("a"), out_dir=tmp_path, roster=["haiku"])
    # A different ITEM SET -> a different item hash -> a different instrument (plan §3).
    other = _verdict_suite("b", item_ids=("i1", "i3"))
    r3 = _run_scored(other, out_dir=tmp_path, roster=["haiku"])
    assert other.item_hash != _verdict_suite("a").item_hash  # guard the fixture's premise

    with pytest.raises(ReportError, match="suite_hash mismatch"):
        build_comparison([r1.run_id, r3.run_id], tmp_path)


# --- Fail-loud on a missing / traversing run ---------------------------------------------


def test_report_missing_run_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="run not found"):
        build_run_report("run_20260101T000000Z_abcdef", tmp_path)


def test_report_traversing_run_id_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ReportError):
        build_run_report("../../evil", tmp_path)


# --- Deterministic render: same run -> byte-identical markdown ----------------------------


def test_render_is_deterministic(tmp_path: Path) -> None:
    suite = _verdict_suite()
    result = _run_scored(
        suite,
        out_dir=tmp_path,
        roster=["haiku", "general-35b"],
        claude=lambda p: "pass" if _iid(p) == "i1" else "flag",
        local=lambda p: "flag" if _iid(p) == "i2" else "pass",
    )
    first = render_run_report(build_run_report(result.run_id, tmp_path))
    second = render_run_report(build_run_report(result.run_id, tmp_path))
    assert first == second  # a pure function of the run store — no wall-clock in the output


# --- JSONL export -------------------------------------------------------------------------


def test_run_report_jsonl_export(tmp_path: Path) -> None:
    suite = _verdict_suite()
    result = _run_scored(
        suite,
        out_dir=tmp_path,
        roster=["haiku"],
        claude=lambda p: "pass" if _iid(p) == "i1" else "flag",
    )
    exported = run_report_jsonl(build_run_report(result.run_id, tmp_path))
    lines = [json.loads(line) for line in exported.splitlines()]
    assert len(lines) == 1
    assert lines[0]["model"] == "haiku"
    assert lines[0]["suite_score"] == 100.0
    assert lines[0]["n_parse_fail"] == 0


# --- mt smoke WIRING (offline stub factory, ZERO live calls) -----------------------------


def _smoke_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hermetic smoke env: cwd with no measure-twice.json, no MEASURE_TWICE_CONFIG."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_VAR, raising=False)


def test_smoke_claude_stub_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _smoke_env(monkeypatch, tmp_path)
    # Stub echoes the item prompt's last word ("...: pass" / "...: flag") -> both parse + score.
    stub = StubAdapters(claude=lambda p: p.strip().split()[-1])
    deps = CliDeps(claude_runner_factory=stub.claude_factory())
    rc = main(["smoke", "--claude", "--out", str(tmp_path / "data")], deps=deps)
    captured = capsys.readouterr()
    assert rc == 0
    assert "run report" in captured.out  # the scored report was rendered + printed
    assert "PASS" in captured.out


def test_smoke_parse_fail_gate_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _smoke_env(monkeypatch, tmp_path)
    # An unparseable response for every item -> recorded parse-fails -> the exit-code gate fails.
    deps = CliDeps(claude_runner_factory=StubAdapters(claude=lambda p: "banana").claude_factory())
    rc = main(["smoke", "--claude", "--out", str(tmp_path / "data")], deps=deps)
    assert rc == 1
    assert "parse failure" in capsys.readouterr().err


def test_smoke_claude_cli_missing_aborts_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no stub injected + ``claude`` absent from PATH, --claude aborts loud, never running."""
    _smoke_env(monkeypatch, tmp_path)
    monkeypatch.setattr("measure_twice.cli.shutil.which", lambda _name: None)
    rc = main(["smoke", "--claude", "--out", str(tmp_path / "data")], deps=CliDeps())
    assert rc == 1
    err = capsys.readouterr().err
    assert "claude" in err
    assert "PATH" in err
    assert not (tmp_path / "data" / "runs").exists()  # preflight aborted before any sweep


def test_smoke_local_stub_skips_preflight_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _smoke_env(monkeypatch, tmp_path)
    # An injected local transport factory both provides canned responses AND signals the test path,
    # so the reachability preflight is skipped and no live endpoint is probed.
    stub = StubAdapters(local=lambda p: p.strip().split()[-1])
    deps = CliDeps(local_transport_factory=stub.local_factory())
    rc = main(["smoke", "--local", "--out", str(tmp_path / "data")], deps=deps)
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_smoke_local_unreachable_aborts_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _smoke_env(monkeypatch, tmp_path)
    # No stub injected + a config pointing at a closed port -> the preflight aborts loud, NEVER
    # auto-spawns, and no run dir is written (Decision 12).
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"local_base_url": "http://127.0.0.1:1/v1"}), encoding="utf-8")
    rc = main(
        ["smoke", "--local", "--out", str(tmp_path / "data"), "--config", str(cfg)],
        deps=CliDeps(),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "unreachable" in err
    assert "auto-spawn" in err
    assert not (tmp_path / "data" / "runs").exists()


def test_local_endpoint_unreachable_helper() -> None:
    """The reachability probe returns a loud, never-auto-spawn message for a closed endpoint."""
    msg = _local_endpoint_unreachable("http://127.0.0.1:1/v1")
    assert msg is not None
    assert "operator-started" in msg
    assert "auto-spawn" in msg


# --- mt report CLI wiring -----------------------------------------------------------------


def test_cli_report_prints_and_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suite = _verdict_suite()
    out = tmp_path / "data"
    result = _run_scored(
        suite, out_dir=out, roster=["haiku"], claude=lambda p: "pass" if _iid(p) == "i1" else "flag"
    )
    rc = main(["report", result.run_id, "--out", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert result.run_id in printed
    assert "100.0" in printed
    assert (out / "reports" / f"{result.run_id}.md").is_file()


def test_cli_report_jsonl_writes_jsonl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suite = _verdict_suite()
    out = tmp_path / "data"
    result = _run_scored(
        suite, out_dir=out, roster=["haiku"], claude=lambda p: "pass" if _iid(p) == "i1" else "flag"
    )
    rc = main(["report", result.run_id, "--jsonl", "--out", str(out)])
    assert rc == 0
    assert '"model"' in capsys.readouterr().out
    assert (out / "reports" / f"{result.run_id}.jsonl").is_file()


def test_cli_report_compare_mismatch_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "data"
    r1 = _run_scored(_verdict_suite("a"), out_dir=out, roster=["haiku"])
    r3 = _run_scored(_verdict_suite("b", item_ids=("i1", "i3")), out_dir=out, roster=["haiku"])
    rc = main(["report", r1.run_id, "--compare", r3.run_id, "--out", str(out)])
    assert rc == 1
    assert "mismatch" in capsys.readouterr().err
