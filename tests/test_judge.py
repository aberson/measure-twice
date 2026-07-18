"""LLM rubric-judge tests — k=3 median, per-judge parse-fail gate, parse-vs-invoke distinction.

OFFLINE + ZERO live ``claude`` calls: every test injects a stub :data:`JudgeCaller` (returning
canned :class:`ModelCallResult` s) — the DI seam that makes the whole rubric path deterministic.
Covers the Step-6 done-when and the ported void_furnace invariants:

  * SCORE parse (judge-core §5.6): clean / prose-surrounded / case-variant / decimal / out-of-range
    clamp / missing -> parse-fail (``None``), never a crash.
  * k=3 median of PARSED samples, incl. the EVEN parsed-count case (median = mean of two middle).
  * PARSE-FAIL (returned text, no SCORE) vs INVOKE-ERROR (adapter error / no-response) kept DISTINCT
    — invoke-errors are excluded from the parse counters, parse-fails count toward the gate rate.
  * the per-judge parse-fail gate fires PER-JUDGE (not pooled): one broken judge among healthy peers
    aborts, and a pooled rate that would sit at 0.50 does not dilute it; both-under passes;
    the ``> 0.5`` boundary is strict; an all-invoke-error judge is skipped (no false fire).
  * the rubric run-scorer + ``score_run_batch`` integration, gate-abort leaves the store untouched,
    end-to-end ``mt run`` (collect) -> ``mt score`` (judge) via the CLI, and the anchor ordering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from measure_twice.adapters.base import RC_OS_ERROR, ModelCallResult
from measure_twice.cli import CliDeps, main
from measure_twice.config import ENV_VAR, RunConfig
from measure_twice.runner import load_run_suite, run, score_run_batch
from measure_twice.scoring import PARSE_FAIL_MARKER
from measure_twice.scoring.deterministic import ScoringError
from measure_twice.scoring.judge import (
    RUBRIC_SCORER,
    JudgeCaller,
    JudgeCell,
    JudgeParseFailError,
    _judge_one_cell,
    build_judge_prompt,
    judge_run,
    make_rubric_run_scorer,
    parse_judge_score,
)
from measure_twice.suite import Item, ScoringSpec, Suite

# --- ModelCallResult builders (a stub judge-caller's return values) -----------------------------


def _ok(text: str) -> ModelCallResult:
    """A SUCCESS judge response carrying ``text`` (the sample the parser sees)."""
    return ModelCallResult.success(response_raw=text, resolved_model="stub-judge", elapsed_s=0.0)


def _invoke_error() -> ModelCallResult:
    """An adapter ERROR (the judge call failed to get a usable response) — an invoke-error."""
    return ModelCallResult.error(reason_class=RC_OS_ERROR, elapsed_s=0.0)


def _no_response() -> ModelCallResult:
    """A NO-RESPONSE judge result (empty text) — grouped with invoke-error (excluded from parse)."""
    return ModelCallResult.no_response_result(resolved_model="stub-judge", elapsed_s=0.0)


def _scripted_caller(scripts: dict[str, list[ModelCallResult]]) -> JudgeCaller:
    """A stub judge-caller replaying a per-judge queue of results IN ORDER (one pop per sample).

    ``judge_run`` calls the caller ``k`` times per (judge, cell), so a queue of length ``k`` scripts
    exactly one cell's samples for that judge. Keyed by judge alias so a multi-judge cell draws each
    judge's own scripted sequence.
    """
    queues = {judge: list(results) for judge, results in scripts.items()}

    def _call(_prompt: str, alias: str) -> ModelCallResult:
        return queues[alias].pop(0)

    return _call


def _content_caller(good_marker: str, *, high: str, low: str) -> JudgeCaller:
    """A stub that returns ``high`` when ``good_marker`` is in the judge prompt, else ``low``.

    Used for the anchor pair: the judge prompt embeds the response under grading, so a stub can
    'grade' by content — high SCORE for the good response, low for the garbage one.
    """

    def _call(prompt: str, _alias: str) -> ModelCallResult:
        return _ok(high if good_marker in prompt else low)

    return _call


def _cell(response: str, *, item_id: str = "i1", rubric: str = "grade 0-10") -> JudgeCell:
    return JudgeCell(item_id=item_id, prompt="explain X", rubric=rubric, response=response)


# --- SCORE parse (judge-core §5.6 robust-extract) ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SCORE: 7\nRATIONALE: solid.", 7.0),  # clean two-line
        ("SCORE:8", 8.0),  # no space
        ("blah blah\n final SCORE: 6 trailing prose\nRATIONALE: x", 6.0),  # surrounded by prose
        ("score: 3", 3.0),  # lowercase (case variant)
        ("Score: 4.5", 4.5),  # mixed case + decimal
        ("SCORE: 15", 10.0),  # out-of-range high -> clamp to 10
        ("SCORE: -3", 0.0),  # out-of-range low -> clamp to 0
        ("SCORE: 0", 0.0),  # a legitimate 0 is NOT a parse-fail
        ("SCORE: 7 out of 10", 7.0),  # a word boundary after the token is fine -> 7
    ],
)
def test_parse_judge_score_recognizes_and_clamps(text: str, expected: float) -> None:
    """The SCORE parser recognizes the value across shapes/case and clamps to [0, 10]."""
    assert parse_judge_score(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty
        "RATIONALE: I forgot to give a score.",  # no SCORE line
        "the answer scores well overall",  # 'score' word but no 'SCORE: <n>'
        "SCORE: high",  # non-numeric -> not matched
    ],
)
def test_parse_judge_score_missing_is_parse_fail_not_crash(text: str) -> None:
    """A judge output with no parseable numeric SCORE is a PARSE-FAIL (``None``), never a crash."""
    assert parse_judge_score(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "SCORE: 1e9",  # sci-notation -> would silently partial-parse to 1; must parse-fail
        "SCORE: 7abc",  # trailing alpha garbage -> would silently read 7; must parse-fail
        "SCORE: 7.5.2",  # double decimal -> would silently read 7.5; must parse-fail
        "SCORE: 3_000",  # digit-group underscore -> would silently read 3; must parse-fail
    ],
)
def test_parse_judge_score_malformed_value_is_parse_fail(text: str) -> None:
    """A MALFORMED numeric token PARSE-FAILS (recorded), rather than silently partial-parsing to a
    wrong number — the documented ``SCORE: <int|decimal>`` contract, honored via the boundary."""
    assert parse_judge_score(text) is None


def test_build_judge_prompt_carries_all_three_inputs_and_the_contract() -> None:
    """The judge prompt is assembled from the item's prompt + rubric + the response, and states the
    two-line SCORE/RATIONALE contract (no hidden template — suites carry all content)."""
    prompt = build_judge_prompt("explain recursion", "a base case and a step", "award full marks")
    assert "explain recursion" in prompt  # the item prompt
    assert "a base case and a step" in prompt  # the response under grading
    assert "award full marks" in prompt  # the rubric
    assert "SCORE:" in prompt and "RATIONALE:" in prompt  # the two-line contract


# --- k=3 median of PARSED samples (incl. the even parsed-count case) -----------------------------


def test_median_all_three_parsed() -> None:
    """k=3, all three samples parse -> the median (a real middle sample), normalized /10."""
    caller = _scripted_caller({"sonnet": [_ok("SCORE: 6"), _ok("SCORE: 8"), _ok("SCORE: 10")]})
    result = judge_run([_cell("r")], judges=["sonnet"], judge_caller=caller, k=3)
    item = result.results[0]
    assert item.judge_scores == (8.0,)  # median([6, 8, 10]) == 8
    assert item.score == pytest.approx(0.8)  # 8 / 10
    assert result.per_judge_parse_stats == (("sonnet", 0, 3),)


def test_median_even_parsed_count_is_mean_of_two_middle() -> None:
    """ONE parse-fail leaves 2 parsed samples -> EVEN-count median = mean of the two middle values.

    This is the load-bearing even-count case: median([6, 8]) is 7.0 (the average), not 6 or 8.
    """
    caller = _scripted_caller(
        {"sonnet": [_ok("SCORE: 6"), _ok("no score in here"), _ok("SCORE: 8")]}
    )
    result = judge_run([_cell("r")], judges=["sonnet"], judge_caller=caller, k=3)
    item = result.results[0]
    assert item.judge_scores == (7.0,)  # median([6, 8]) == mean(6, 8) == 7.0
    assert item.score == pytest.approx(0.7)
    assert result.per_judge_parse_stats == (("sonnet", 1, 3),)  # 1 parse-fail, 3 parse-attempts


def test_median_two_parse_fails_leaves_single_value() -> None:
    """TWO parse-fails leave a single parsed sample -> that value is the median.

    Exercised at the CELL level (``_judge_one_cell``) below the run-level gate: a single judge at
    2/3 parse-fail is 0.67 > 0.5, so ``judge_run`` would (correctly) ABORT — the median math for a
    parse-fail-heavy cell must be observed before the gate. This isolates the median logic.
    """
    caller = _scripted_caller({"sonnet": [_ok("SCORE: 6"), _ok("nope"), _ok("nada")]})
    item, cell_stats = _judge_one_cell(_cell("r"), ["sonnet"], caller, 3)
    assert item.judge_scores == (6.0,)  # the one surviving parsed sample is the median
    assert item.score == pytest.approx(0.6)
    assert cell_stats[0].judge == "sonnet"
    assert cell_stats[0].n_parse_fail == 2 and cell_stats[0].n_parse_attempts == 3
    assert cell_stats[0].median == 6.0


def test_all_parse_fails_is_recorded_rubric_parse_fail_scored_zero() -> None:
    """ALL k samples parse-fail -> the item is a RECORDED parse-fail: score 0.0, parsed marker.

    Cell-level (below the gate): a single judge at 3/3 parse-fail would abort ``judge_run``; here we
    observe the item-level parse-fail outcome the median logic produces when NO sample parsed.
    """
    caller = _scripted_caller({"sonnet": [_ok("x"), _ok("y"), _ok("z")]})
    item, cell_stats = _judge_one_cell(_cell("r"), ["sonnet"], caller, 3)
    assert item.score == 0.0
    assert item.parsed == PARSE_FAIL_MARKER  # distinct, countable -- not a silent 0
    assert item.judge_scores == ()
    assert cell_stats[0].n_parse_fail == 3 and cell_stats[0].n_parse_attempts == 3
    assert cell_stats[0].median is None


# --- PARSE-FAIL vs INVOKE-ERROR kept distinct ----------------------------------------------------


def test_invoke_error_excluded_from_parse_counters() -> None:
    """An adapter ERROR sample is an INVOKE-ERROR: excluded from BOTH parse counters."""
    caller = _scripted_caller({"sonnet": [_invoke_error(), _ok("SCORE: 8"), _ok("SCORE: 6")]})
    result = judge_run([_cell("r")], judges=["sonnet"], judge_caller=caller, k=3)
    item = result.results[0]
    assert item.judge_scores == (7.0,)  # median([8, 6]) == 7.0
    # n_attempts is 2 (the two returned responses), NOT 3 — the invoke-error is not a parse-attempt.
    assert result.per_judge_parse_stats == (("sonnet", 0, 2),)


def test_no_response_is_invoke_error_not_parse_fail() -> None:
    """A NO-RESPONSE sample is grouped with invoke-error: excluded from the parse counters."""
    caller = _scripted_caller({"sonnet": [_no_response(), _ok("SCORE: 4"), _ok("SCORE: 4")]})
    result = judge_run([_cell("r")], judges=["sonnet"], judge_caller=caller, k=3)
    assert result.per_judge_parse_stats == (("sonnet", 0, 2),)  # no_response excluded, 0 fails


def test_invoke_error_and_parse_fail_counted_separately() -> None:
    """A cell mixing an invoke-error, a parse-fail, and a parsed sample: only the parse-fail counts
    toward the rate; the invoke-error is excluded from the denominator entirely."""
    caller = _scripted_caller(
        {"sonnet": [_invoke_error(), _ok("garbage, no score"), _ok("SCORE: 5")]}
    )
    result = judge_run([_cell("r")], judges=["sonnet"], judge_caller=caller, k=3)
    assert result.results[0].judge_scores == (5.0,)
    assert result.per_judge_parse_stats == (("sonnet", 1, 2),)  # 1 fail / 2 attempts (err excluded)


# --- The per-judge parse-fail gate (PER-JUDGE, not pooled) ---------------------------------------


def test_gate_fires_per_judge_not_pooled() -> None:
    """Judge A parse-fails 100% while healthy judge B is 0% -> the run ABORTS citing A.

    The POOLED rate here is 3/6 = 0.50, which a ``> 0.50`` pooled gate would NOT fire — proving the
    gate is per-judge (A's own 1.00 fires) and that B's health cannot dilute A below threshold.
    """
    caller = _scripted_caller(
        {
            "judge-a": [_ok("no score"), _ok("still none"), _ok("nope")],  # 3/3 parse-fail
            "judge-b": [_ok("SCORE: 8"), _ok("SCORE: 8"), _ok("SCORE: 8")],  # 0/3 parse-fail
        }
    )
    with pytest.raises(JudgeParseFailError) as excinfo:
        judge_run([_cell("r")], judges=["judge-a", "judge-b"], judge_caller=caller, k=3)
    message = str(excinfo.value)
    assert "'judge-a'" in message  # the offender is named
    assert "'judge-b'" not in message  # the healthy peer is not flagged


def test_gate_does_not_fire_when_both_judges_under_threshold() -> None:
    """Both judges parse-fail 1/3 (0.33 < 0.50) -> no abort; the run result is returned clean."""
    caller = _scripted_caller(
        {
            "judge-a": [_ok("SCORE: 5"), _ok("no score"), _ok("SCORE: 5")],  # 1/3
            "judge-b": [_ok("SCORE: 7"), _ok("SCORE: 7"), _ok("no score")],  # 1/3
        }
    )
    result = judge_run([_cell("r")], judges=["judge-a", "judge-b"], judge_caller=caller, k=3)
    # Item score = mean of the two judges' medians (5 and 7) / 10 = 0.6.
    assert result.results[0].score == pytest.approx(0.6)
    assert result.per_judge_parse_stats == (("judge-a", 1, 3), ("judge-b", 1, 3))


def test_gate_boundary_is_strict_greater_than() -> None:
    """A judge at EXACTLY 0.5 (1 fail / 2 attempts) does NOT fire the gate (strict ``>``)."""
    caller = _scripted_caller({"sonnet": [_ok("SCORE: 7"), _ok("no score")]})  # 1/2 == 0.50
    result = judge_run([_cell("r")], judges=["sonnet"], judge_caller=caller, k=2)
    assert result.per_judge_parse_stats == (("sonnet", 1, 2),)  # rate exactly 0.5 -> allowed
    assert result.results[0].score == pytest.approx(0.7)


def test_gate_skips_all_invoke_error_judge_no_false_fire() -> None:
    """A judge whose every sample invoke-errors has ZERO parse-attempts -> SKIPPED by the gate
    (no parseability signal), never a false fire. The item is a recorded parse-fail scored 0.0."""
    caller = _scripted_caller({"sonnet": [_invoke_error(), _invoke_error(), _invoke_error()]})
    result = judge_run([_cell("r")], judges=["sonnet"], judge_caller=caller, k=3)  # no raise
    assert result.per_judge_parse_stats == (("sonnet", 0, 0),)
    assert result.results[0].score == 0.0
    assert result.results[0].parsed == PARSE_FAIL_MARKER


def test_gate_accumulates_across_cells_per_judge() -> None:
    """The gate rate accumulates across CELLS: a judge over-threshold across the run aborts, even if
    no single cell is 100% broken."""
    # Two cells, k=3. Judge fails 2/3 in each cell -> 4/6 == 0.667 > 0.5 across the run.
    caller = _scripted_caller(
        {
            "sonnet": [
                _ok("SCORE: 5"),
                _ok("no"),
                _ok("no"),  # cell 1: 2/3 fail
                _ok("SCORE: 5"),
                _ok("no"),
                _ok("no"),  # cell 2: 2/3 fail
            ]
        }
    )
    with pytest.raises(JudgeParseFailError):
        judge_run([_cell("a"), _cell("b")], judges=["sonnet"], judge_caller=caller, k=3)


def test_judge_run_rejects_empty_judges_and_bad_k() -> None:
    """Fail loud on an un-runnable judge config (no judges, or k < 1) — a ScoringError."""
    with pytest.raises(ScoringError):
        judge_run([_cell("r")], judges=[], judge_caller=_scripted_caller({}), k=3)
    with pytest.raises(ScoringError):
        judge_run([_cell("r")], judges=["sonnet"], judge_caller=_scripted_caller({}), k=0)


def test_duplicate_judges_are_deduped() -> None:
    """A doubled judge is de-duplicated so it neither double-counts its rate nor its median."""
    caller = _scripted_caller({"sonnet": [_ok("SCORE: 6"), _ok("SCORE: 8"), _ok("SCORE: 10")]})
    result = judge_run([_cell("r")], judges=["sonnet", "sonnet"], judge_caller=caller, k=3)
    assert result.per_judge_parse_stats == (("sonnet", 0, 3),)  # ONE entry, 3 attempts (not 6)
    assert result.results[0].judge_scores == (8.0,)


def test_multi_judge_item_score_is_mean_of_medians() -> None:
    """With two healthy judges the item score is the mean of their 0-10 medians, normalized /10."""
    caller = _scripted_caller(
        {
            "judge-a": [_ok("SCORE: 4"), _ok("SCORE: 4"), _ok("SCORE: 4")],  # median 4
            "judge-b": [_ok("SCORE: 8"), _ok("SCORE: 8"), _ok("SCORE: 8")],  # median 8
        }
    )
    result = judge_run([_cell("r")], judges=["judge-a", "judge-b"], judge_caller=caller, k=3)
    item = result.results[0]
    assert item.judge_scores == (4.0, 8.0)
    assert item.score == pytest.approx(0.6)  # mean(4, 8) / 10


def test_judge_dropped_from_item_mean_when_it_parse_fails_one_cell() -> None:
    """A judge that parse-fails ALL k samples of ONE cell (but stays healthy over the run) is
    DROPPED from THAT cell's per-judge-median mean — that cell's item score is the other's median
    alone — while the run-level gate does NOT trip.

    3 cells, k=3, judges A + B. B parse-fails all of cell 1 (0/3) but parses cells 2-3, so B's
    run rate is 3/9 = 0.33 < 0.5 (healthy overall, no abort). Cell 1 must score on A alone.
    """
    caller = _scripted_caller(
        {
            "judge-a": [_ok("SCORE: 8") for _ in range(9)],  # healthy in all 3 cells (median 8)
            "judge-b": [
                _ok("no"),
                _ok("no"),
                _ok("no"),  # cell 1: 3/3 parse-fail -> B dropped from cell 1's mean
                _ok("SCORE: 4"),
                _ok("SCORE: 4"),
                _ok("SCORE: 4"),  # cell 2: median 4
                _ok("SCORE: 4"),
                _ok("SCORE: 4"),
                _ok("SCORE: 4"),  # cell 3: median 4
            ],
        }
    )
    result = judge_run(
        [_cell("c1"), _cell("c2"), _cell("c3")],
        judges=["judge-a", "judge-b"],
        judge_caller=caller,
        k=3,
    )
    # Cell 1: B produced no parseable sample -> dropped; item = mean([8]) / 10 = 0.8.
    assert result.results[0].judge_scores == (8.0,)
    assert result.results[0].score == pytest.approx(0.8)
    # Cells 2-3: both judges present -> mean([8, 4]) / 10 = 0.6.
    assert result.results[1].judge_scores == (8.0, 4.0)
    assert result.results[1].score == pytest.approx(0.6)
    assert result.results[2].score == pytest.approx(0.6)
    # B is healthy across the run (3 fails / 9 attempts = 0.33 < 0.5) -> no abort.
    assert result.per_judge_parse_stats == (("judge-a", 0, 9), ("judge-b", 3, 9))


# --- Rubric anchor ordering (via a content-sensitive stubbed judge) ------------------------------


def test_rubric_anchor_ordering_good_outscores_garbage() -> None:
    """THE ORDERING GATE (stubbed judge): a clearly-good response out-scores a clearly-garbage one
    through the real ``judge_run`` path. A rubric judge that cannot rank good over garbage cannot
    pick winners (measurement-validity § calibrate with anchors)."""
    good = "A base case stops the recursion and each call shrinks the input toward it."
    garbage = "idk recursion is when a function does stuff maybe"
    caller = _content_caller(good, high="SCORE: 9", low="SCORE: 2")

    good_result = judge_run([_cell(good)], judges=["sonnet"], judge_caller=caller, k=3)
    garbage_result = judge_run([_cell(garbage)], judges=["sonnet"], judge_caller=caller, k=3)

    assert good_result.results[0].score > garbage_result.results[0].score
    assert good_result.results[0].score == pytest.approx(0.9)
    assert garbage_result.results[0].score == pytest.approx(0.2)


# --- Integration: the rubric run-scorer + score_run_batch + the CLI ------------------------------


def _rubric_suite() -> Suite:
    """A 1-item rubric suite; the item's ``expected`` field IS the rubric text (suites carry all
    content — there is no hidden judge template)."""
    return Suite(
        suite="rub",
        version=1,
        description="a rubric suite fixture",
        domain="d",
        scoring=ScoringSpec(type="rubric"),
        items=[
            Item(
                id="r1",
                tags=["t"],
                prompt="explain recursion",
                expected="Award 10 for a correct, complete explanation; 0 for wrong or empty.",
                difficulty_prior=0.5,
                provenance="authored",
            )
        ],
    )


def _fixed_local_factory(response: str):  # type: ignore[no-untyped-def]
    """A local transport returning a FIXED assistant ``response`` for any prompt (to seed a run)."""

    def factory() -> object:
        def transport(url: str, data: bytes, timeout: float) -> str:
            return json.dumps(
                {
                    "id": "c",
                    "object": "chat.completion",
                    "model": "local-x",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": response},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

        return transport

    return factory  # type: ignore[return-value]


def _erroring_local_factory():  # type: ignore[no-untyped-def]
    """A local transport returning a NON-JSON body for any prompt -> a terminal error row
    (``reason_class`` non_json_body), so a run seeded with it has ``error != None`` rows to skip."""

    def factory() -> object:
        def transport(url: str, data: bytes, timeout: float) -> str:
            return "upstream 502: not json at all"

        return transport

    return factory  # type: ignore[return-value]


def _seed_rubric_run(tmp_path: Path) -> str:
    """Sweep the rubric suite with a fixed local response (collect-only) and return the run id."""
    result = run(
        suite=_rubric_suite(),
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        local_transport_factory=_fixed_local_factory("a thorough recursion explanation"),
    )
    return result.run_id


def test_score_run_batch_applies_rubric_scores(tmp_path: Path) -> None:
    """The rubric run-scorer, driven by score_run_batch, writes the 0-1 score + per-judge medians
    onto each collected row (the run-level integration a rubric suite is scored through)."""
    run_id = _seed_rubric_run(tmp_path)
    caller = _scripted_caller({"sonnet": [_ok("SCORE: 8"), _ok("SCORE: 8"), _ok("SCORE: 8")]})
    run_scorer = make_rubric_run_scorer(judges=["sonnet"], judge_caller=caller, k=3)

    summary = score_run_batch(run_id=run_id, out_dir=tmp_path, run_scorer=run_scorer)
    assert summary.scored == 1

    run_dir = tmp_path / "runs" / run_id
    rows = [json.loads(line) for line in (run_dir / "rows.jsonl").read_text().splitlines()]
    assert rows[0]["score"] == pytest.approx(0.8)
    assert rows[0]["scorer"] == RUBRIC_SCORER
    assert rows[0]["judge_scores"] == [8.0]


def test_score_run_batch_gate_abort_leaves_store_untouched(tmp_path: Path) -> None:
    """A broken judge trips the gate INSIDE the run-scorer, BEFORE any rewrite — so the aborted
    (re)score leaves rows.jsonl byte-for-byte unchanged (fail-loud, no partial write)."""
    run_id = _seed_rubric_run(tmp_path)
    rows_path = tmp_path / "runs" / run_id / "rows.jsonl"
    before = rows_path.read_bytes()

    caller = _scripted_caller({"sonnet": [_ok("no score"), _ok("none"), _ok("nope")]})  # 3/3 fail
    run_scorer = make_rubric_run_scorer(judges=["sonnet"], judge_caller=caller, k=3)
    with pytest.raises(JudgeParseFailError):
        score_run_batch(run_id=run_id, out_dir=tmp_path, run_scorer=run_scorer)

    assert rows_path.read_bytes() == before  # store untouched — the gate fired before any rewrite


def test_cli_mt_score_judges_rubric_run_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end via the CLI: mt run collects the rubric responses, mt score judges them via the
    injected stub judge-caller (ZERO live claude calls), and the rows land rubric-scored."""
    monkeypatch.delenv(ENV_VAR, raising=False)  # config resolves to defaults (judges=["sonnet"])
    run_id = _seed_rubric_run(tmp_path)
    caller = _scripted_caller({"sonnet": [_ok("SCORE: 9"), _ok("SCORE: 9"), _ok("SCORE: 9")]})

    rc = main(["score", str(run_id), "--out", str(tmp_path)], deps=CliDeps(judge_caller=caller))
    assert rc == 0

    reloaded = load_run_suite(run_id, tmp_path)
    assert reloaded.scoring.type == "rubric"
    run_dir = tmp_path / "runs" / run_id
    rows = [json.loads(line) for line in (run_dir / "rows.jsonl").read_text().splitlines()]
    assert rows[0]["scorer"] == RUBRIC_SCORER
    assert rows[0]["score"] == pytest.approx(0.9)


def test_rubric_run_scorer_skips_no_response_rows(tmp_path: Path) -> None:
    """The run-scorer never judges a no-response force-0 row (a model that produced nothing) — it is
    left at its 0.0 without a judge call."""
    # Seed a run whose local model returns empty text -> a no-response force-0 row.
    result = run(
        suite=_rubric_suite(),
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        local_transport_factory=_fixed_local_factory("   "),  # whitespace -> no-response
    )
    run_id = result.run_id
    called = {"n": 0}

    def _counting_caller(_prompt: str, _alias: str) -> ModelCallResult:
        called["n"] += 1
        return _ok("SCORE: 8")

    run_scorer = make_rubric_run_scorer(judges=["sonnet"], judge_caller=_counting_caller, k=3)
    summary = score_run_batch(run_id=run_id, out_dir=tmp_path, run_scorer=run_scorer)

    assert called["n"] == 0  # the no-response row was never judged
    assert summary.no_response == 1
    run_dir = tmp_path / "runs" / run_id
    rows = [json.loads(line) for line in (run_dir / "rows.jsonl").read_text().splitlines()]
    assert rows[0]["scorer"] == "no_response" and rows[0]["score"] == 0.0


def test_rubric_run_scorer_skips_error_rows(tmp_path: Path) -> None:
    """The run-scorer never judges a transport-ERROR row — no judge call is burned on it and it
    keeps its unscored error shape (score/scorer None, error preserved). A deleted/inverted
    ``if row.error is not None: continue`` guard would burn a live call and miscount, so this holds
    the measurement-validity guard: an errored cell was never a real response to grade."""
    # Seed a run whose local endpoint returns non-JSON -> a terminal error row (error != None).
    result = run(
        suite=_rubric_suite(),
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        local_transport_factory=_erroring_local_factory(),
    )
    run_id = result.run_id
    run_dir = tmp_path / "runs" / run_id
    seeded = [json.loads(line) for line in (run_dir / "rows.jsonl").read_text().splitlines()]
    assert seeded[0]["error"] is not None and seeded[0]["score"] is None  # a real error row

    called = {"n": 0}

    def _counting_caller(_prompt: str, _alias: str) -> ModelCallResult:
        called["n"] += 1
        return _ok("SCORE: 8")

    run_scorer = make_rubric_run_scorer(judges=["sonnet"], judge_caller=_counting_caller, k=3)
    summary = score_run_batch(run_id=run_id, out_dir=tmp_path, run_scorer=run_scorer)

    assert called["n"] == 0  # the error row was never judged (no live call burned)
    assert summary.scored == 0
    rows = [json.loads(line) for line in (run_dir / "rows.jsonl").read_text().splitlines()]
    # Error row untouched: error preserved, still unscored (score/scorer None).
    assert rows[0]["error"] is not None
    assert rows[0]["score"] is None and rows[0]["scorer"] is None
