"""Item-level HTML report tests — OFFLINE, stub adapter factories, ZERO live calls.

Every run is swept through the shared ``StubAdapters`` DI seam (``tests/conftest.py``) with the REAL
deterministic verdict scorer, so a canned-but-unparseable response genuinely produces a stored
parse-fail rather than a hand-written row. Coverage:

  * the three ZERO-scoring outcomes are separated, not collapsed: a wrong answer, a scorer refusal
    (two recognized labels), and a no-verdict response are three distinct taxonomy buckets
  * the per-cell taxonomy RECONCILES with ``report.build_run_report``'s independent roll-up
  * the reproduction gate fails loud when the run store and the scorer disagree, and the
    RED-ON-GARBAGE ANCHOR proves that gate can actually go red (a mutated stored ``parsed`` raises)
  * untrusted model text containing ``</script>`` and markup cannot escape the JSON island
  * a non-verdict suite and an unscored run both fail loud rather than rendering a hollow page
  * DETERMINISTIC render: same run -> byte-identical HTML (no render timestamp)
  * facets are auto-derived from suite tags and ordered by the suite's AUTHORED difficulty prior
  * CLI wiring: ``mt report <run_id> --html`` writes the page and does not dump it to stdout
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import StubAdapters, _iid  # shared offline stub scaffolding (tests/conftest.py)

from measure_twice.cli import main
from measure_twice.config import RunConfig
from measure_twice.report import ReportError, build_run_report
from measure_twice.report_html import (
    OUTCOME_CORRECT,
    OUTCOME_NO_VERDICT,
    OUTCOME_REFUSED,
    OUTCOME_WRONG,
    build_transparency_report,
    render_transparency_report,
)
from measure_twice.runner import RunResult, run
from measure_twice.scoring import make_deterministic_scorer
from measure_twice.suite import Item, ScoringSpec, Suite

# Responses keyed by item id. Each exercises one taxonomy bucket.
_ANSWERS = {
    "ok": "pass",
    "wrong": "pass",  # item expects flag -> a genuine judging miss
    "refuse": "`flag` and then a rationale that also says pass, so two labels are recognized",
    "silent": "I cannot judge this without seeing the code you are referring to.",
}


def _taxonomy_suite() -> Suite:
    """A 4-item verdict suite whose items map one-to-one onto the four outcome buckets."""
    spec = [
        ("ok", "pass", ["lens-style", "difficulty-easy"], 0.1),
        ("wrong", "flag", ["lens-style", "difficulty-hard"], 0.9),
        ("refuse", "flag", ["lens-grading", "difficulty-easy"], 0.1),
        ("silent", "pass", ["lens-grading", "difficulty-hard"], 0.9),
    ]
    items = [
        Item(
            id=iid,
            tags=[*tags, "provenance-authored"],
            prompt=f"PROMPT::{iid}",
            expected=expected,
            difficulty_prior=prior,
            provenance="authored",
        )
        for iid, expected, tags, prior in spec
    ]
    return Suite(
        suite="tax",
        version=1,
        description="d",
        domain="dom",
        scoring=ScoringSpec(type="verdict", labels=["pass", "flag"]),
        items=items,
    )


def _sweep(suite: Suite, *, out_dir: Path, answer=lambda p: _ANSWERS[_iid(p)]) -> RunResult:
    """Sweep ``suite`` with the REAL deterministic scorer through the shared stub adapters."""
    stub = StubAdapters(local=answer, claude=answer)
    return run(
        suite=suite,
        config=RunConfig(),
        out_dir=out_dir,
        roster=["haiku"],
        samples_per_cell=1,
        scorer=make_deterministic_scorer(suite.scoring),
        local_transport_factory=stub.local_factory(),
        claude_runner_factory=stub.claude_factory(),
    )


def _island(html: str) -> dict[str, object]:
    """Parse the embedded JSON payload back out of a rendered page."""
    match = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S)
    assert match is not None, "rendered page has no JSON island"
    return json.loads(match.group(1).replace("\\u003c", "<"))


# --- The taxonomy ---------------------------------------------------------------------------


def test_three_kinds_of_zero_are_separated(tmp_path: Path) -> None:
    """A wrong answer, a scorer refusal, and a no-verdict response are DISTINCT outcomes."""
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    report = build_transparency_report(result.run_id, tmp_path)

    outcomes = {
        item.item_id: cell.outcome
        for item in report.items
        for cell in [item.cells["haiku"]]
        if cell is not None
    }
    assert outcomes == {
        "ok": OUTCOME_CORRECT,
        "wrong": OUTCOME_WRONG,
        "refuse": OUTCOME_REFUSED,
        "silent": OUTCOME_NO_VERDICT,
    }
    # All three zero-scoring buckets really did score 0 — they differ in CAUSE, not in score.
    cells = [i.cells["haiku"] for i in report.items]
    zeros = [c for c in cells if c is not None and c.score == 0.0]
    assert len(zeros) == 3
    assert len({c.outcome for c in zeros}) == 3


def test_taxonomy_reconciles_with_the_independent_roll_up(tmp_path: Path) -> None:
    """Per-cell classification must agree with ``build_run_report``'s own parse-fail count."""
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    report = build_transparency_report(result.run_id, tmp_path)
    roll_up = build_run_report(result.run_id, tmp_path)

    parse_fails = sum(
        1
        for item in report.items
        for cell in [item.cells["haiku"]]
        if cell is not None and cell.outcome in (OUTCOME_REFUSED, OUTCOME_NO_VERDICT)
    )
    assert parse_fails == roll_up.total_parse_fail


def test_refusal_records_both_recognized_labels(tmp_path: Path) -> None:
    """A refusal names the conflicting labels, so the reader can see WHY the scorer declined."""
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    report = build_transparency_report(result.run_id, tmp_path)
    cell = next(i.cells["haiku"] for i in report.items if i.item_id == "refuse")
    assert cell is not None
    assert set(cell.labels_present) == {"pass", "flag"}
    # Its FIRST label was the expected one -- the diagnostic, which must not change the score.
    assert cell.diagnostic_recoverable is True
    assert cell.score == 0.0


def test_a_label_embedded_in_a_longer_word_is_not_a_verdict(tmp_path: Path) -> None:
    """``password`` must not read as ``pass`` -- the diagnostic honors whole-word bounds."""
    suite = _taxonomy_suite()
    result = _sweep(
        suite,
        out_dir=tmp_path,
        answer=lambda p: "`flag` because the password check is inverted",
    )
    report = build_transparency_report(result.run_id, tmp_path)
    cell = next(i.cells["haiku"] for i in report.items if i.item_id == "wrong")
    assert cell is not None
    assert cell.labels_present == ("flag",)  # 'pass' inside 'password' is not a label
    assert cell.outcome == OUTCOME_CORRECT  # item 'wrong' expects flag; this answer is right


# --- The reproduction gate + its red-on-garbage anchor ---------------------------------------


def test_reproduction_gate_passes_on_an_untampered_run(tmp_path: Path) -> None:
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    report = build_transparency_report(result.run_id, tmp_path)
    assert report.reproduced_cells == 4


def test_reproduction_gate_goes_red_when_the_store_is_tampered_with(tmp_path: Path) -> None:
    """RED-ON-GARBAGE ANCHOR: a gate that cannot fail is not a gate.

    Mutating one stored ``parsed`` makes the run store disagree with the scorer. The build MUST
    abort rather than quietly re-label the cell from the response it re-reads.
    """
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    rows_path = tmp_path / "runs" / result.run_id / "rows.jsonl"
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    mutated = []
    for line in lines:
        row = json.loads(line)
        if row["item_id"] == "ok":
            row["parsed"] = "flag"  # store now claims a verdict the response does not carry
        mutated.append(json.dumps(row))
    rows_path.write_text("\n".join(mutated) + "\n", encoding="utf-8")

    with pytest.raises(ReportError, match="does not reproduce"):
        build_transparency_report(result.run_id, tmp_path)


# --- Fail-loud scope guards -------------------------------------------------------------------


def test_non_verdict_suite_fails_loud(tmp_path: Path) -> None:
    suite = Suite(
        suite="rub",
        version=1,
        description="d",
        domain="dom",
        scoring=ScoringSpec(type="rubric"),
        items=[
            Item(
                id="r1",
                tags=["t"],
                prompt="PROMPT::r1",
                expected="Grade clarity 0-10.",
                difficulty_prior=0.5,
                provenance="authored",
            )
        ],
    )
    stub = StubAdapters(local=lambda p: "7", claude=lambda p: "7")
    result = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["haiku"],
        samples_per_cell=1,
        local_transport_factory=stub.local_factory(),
        claude_runner_factory=stub.claude_factory(),
    )
    with pytest.raises(ReportError, match="verdict suites only"):
        build_transparency_report(result.run_id, tmp_path)


def test_unscored_run_fails_loud(tmp_path: Path) -> None:
    """A collected-but-unscored run has no verdicts to explain; say so instead of rendering."""
    suite = _taxonomy_suite()
    stub = StubAdapters(local=lambda p: "pass", claude=lambda p: "pass")
    result = run(  # no scorer -> the Step-4 collect-only default
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["haiku"],
        samples_per_cell=1,
        local_transport_factory=stub.local_factory(),
        claude_runner_factory=stub.claude_factory(),
    )
    with pytest.raises(ReportError, match="no verdict-scored cells"):
        build_transparency_report(result.run_id, tmp_path)


# --- Rendering ---------------------------------------------------------------------------------


def test_render_is_deterministic(tmp_path: Path) -> None:
    """Same run -> byte-identical page (no render timestamp), matching the markdown contract."""
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    first = render_transparency_report(build_transparency_report(result.run_id, tmp_path))
    second = render_transparency_report(build_transparency_report(result.run_id, tmp_path))
    assert first == second


def test_untrusted_model_text_cannot_escape_the_json_island(tmp_path: Path) -> None:
    """Model output is attacker-shaped text: a ``</script>`` in a response must stay inert."""
    payload = "</script><img src=x onerror=alert(1)>`flag`"
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path, answer=lambda p: payload)
    html = render_transparency_report(build_transparency_report(result.run_id, tmp_path))

    island = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S)
    assert island is not None
    assert "</script" not in island.group(1)
    assert "<" not in island.group(1)  # every '<' escaped at the boundary
    # The hostile markup appears NOWHERE as live markup in the document...
    assert "<img src=x" not in html
    # ...yet survives intact INSIDE the island as escaped data, so the report stays honest
    # about what the model actually said.
    assert payload in json.dumps(_island(html))


def test_facets_are_derived_and_ordered_by_authored_prior(tmp_path: Path) -> None:
    """Tag families with >=2 values become facets; a difficulty ladder orders easy -> hard."""
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    data = _island(render_transparency_report(build_transparency_report(result.run_id, tmp_path)))
    facets = {f["name"]: [v["value"] for v in f["values"]] for f in data["facets"]}  # type: ignore[index,union-attr]
    assert facets["difficulty"] == ["easy", "hard"]  # ordered by authored prior, not alphabetically
    assert sorted(facets["lens"]) == ["grading", "style"]
    assert "provenance" not in facets  # single-valued tag family is not an axis


def test_degenerate_floor_is_reported(tmp_path: Path) -> None:
    """The floor is the most common expected label's share -- the baseline a score must beat."""
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    report = build_transparency_report(result.run_id, tmp_path)
    assert report.floor_score == 50.0  # 2 'pass' + 2 'flag' of 4 items
    assert report.floor_label in {"pass", "flag"}


# --- CLI wiring ----------------------------------------------------------------------------------


def test_cli_html_writes_the_page_and_keeps_stdout_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _sweep(_taxonomy_suite(), out_dir=tmp_path)
    capsys.readouterr()
    rc = main(["report", result.run_id, "--html", "--out", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0
    dest = tmp_path / "reports" / f"{result.run_id}.html"
    assert dest.is_file()
    assert "<!DOCTYPE html>" in dest.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" not in captured.out  # a page is a file, not terminal output
    assert str(dest) in captured.err


def test_cli_html_surfaces_a_report_error_as_rc_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["report", "run_does_not_exist", "--html", "--out", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.startswith("report: ")
