"""LLM rubric judge — k=3 median + per-judge parse-fail gate (plan §4 rubric row, §5, §8 D3/D7).

The third scoring path (after ``deterministic``'s verdict/exact): an LLM judge that grades an
open-ended response against a rubric on a 0-10 scale, normalized to 0-1 so it plugs into the same
0-100 suite normalization. Judges are Claude via the ``claude_cli`` adapter (plan §4); tests inject
a stub judge-caller so the whole path runs OFFLINE with ZERO live calls.

This module PORTS void_furnace's judge invariants project-neutrally (never imports void_furnace) and
conforms to judge-core §5.6 (the parse+aggregate spine) + ``dev/.claude/rules/measurement-
validity.md`` (calibrate-with-anchors, fail-loud, k>=3/median for LLM judges) — referenced, not
restated. The three ported invariants:

  * **The two-line contract** — the judge prompt (:func:`build_judge_prompt`) asks for EXACTLY
    ``SCORE: <n>`` + ``RATIONALE: <text>``. Suites carry ALL content (plan §4): for a rubric suite
    the item's ``expected`` field IS the rubric text, so there is no hidden judge template — the
    prompt is assembled in ONE place from the item's own content (measurement-validity § assemble
    through the production path: the fallback-prompt bug class is structurally absent).
  * **k=3 median of PARSED samples** (:data:`JUDGE_SAMPLE_K`) — each (judge, response) is sampled
    ``k`` times and the judge's contribution is the MEDIAN of the successfully-parsed samples
    (:func:`statistics.median`, which AVERAGES the two middle values for an even parsed count).
    A single-sample judge scored the same build 10 then 5 (noise); k>=3 + median is the
    measurement-validity remedy. Parse-fails / invoke-errors are dropped from the median SET.
  * **The per-judge parse-fail gate** (:data:`JUDGE_PARSE_FAIL_RATE_THRESHOLD`) — a judge whose
    model cannot emit ``SCORE: <n>`` parse-fails, and a silent parse-fail→0 drags every mean toward
    zero (indistinguishable from a real low score). :func:`judge_run` accumulates EACH judge's own
    parse-fail rate across the whole run and ABORTS with :class:`JudgeParseFailError` if ANY single
    judge exceeds the threshold — PER-JUDGE, not pooled, so one broken judge cannot hide behind
    healthy peers (a 2-judge / 1-broken sweep pools to exactly 0.50 and would slip a pooled gate).

PARSE-FAIL vs INVOKE-ERROR are kept DISTINCT (matching void_furnace): a PARSE-FAIL is a returned
response with no parseable ``SCORE`` line (counts toward the gate rate); an INVOKE-ERROR is the
adapter failing to get any usable response (a :class:`~measure_twice.adapters.base.ModelCallResult`
error OR the no-response state) — a broken transport is not a broken output format, so it is
excluded from BOTH the gate numerator and denominator. A judge whose every sample invoke-errors
has zero parse-attempts and is SKIPPED by the gate (no parseability signal), never a false fire.

Integration (plan Step 6 "wire the rubric branch"): the gate is RUN-LEVEL, so the rubric path is a
scoring PASS, not a per-cell :data:`~measure_twice.runner.Scorer`. :func:`make_rubric_run_scorer`
builds a :data:`~measure_twice.runner.RunScorer` that ``mt score`` drives via
:func:`~measure_twice.runner.score_run_batch` — the gate accumulates across every row and aborts
once a judge crosses 0.5, BEFORE any file rewrite (fail-loud, no partial store).
``make_deterministic_scorer`` stays deterministic-only (it still raises for ``rubric``); the CLI
routes a rubric suite here instead. Core is stdlib-only (``re`` + ``statistics``); Claude is reached
only via the ``claude_cli`` adapter.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from measure_twice.adapters.base import ModelCallResult
from measure_twice.adapters.claude_cli import CallBudget, RunnerFactory, claude_call
from measure_twice.config import RunConfig
from measure_twice.runner import NO_RESPONSE_SCORER, RunRow, RunScorer
from measure_twice.scoring.deterministic import PARSE_FAIL_MARKER, ScoringError
from measure_twice.suite import Item

__all__ = [
    "JUDGE_PARSE_FAIL_RATE_THRESHOLD",
    "JUDGE_SAMPLE_K",
    "RUBRIC_SCORER",
    "JudgeCaller",
    "JudgeCell",
    "JudgeItemResult",
    "JudgeParseFailError",
    "JudgeRunResult",
    "build_judge_prompt",
    "default_judge_caller",
    "judge_run",
    "make_rubric_run_scorer",
    "parse_judge_score",
]

# Number of times each judge is sampled per (item, response) cell; the judge's contribution is the
# MEDIAN of the parsed samples. k>=3 (an odd default so the median is a real middle sample) tames
# the single-sample judge noise the measurement-validity rule warns about (the same build scored 10
# then 5). ONE source of truth for tests + the CLI (ported from void_furnace's JUDGE_SAMPLE_K).
JUDGE_SAMPLE_K: Final[int] = 3

# The per-judge parse-fail-rate abort threshold (ported from void_furnace's
# JUDGE_PARSE_FAIL_RATE_THRESHOLD). If ANY single judge's own parse-fail rate EXCEEDS this
# (strictly ``>``), :func:`judge_run` aborts: a judge that parse-fails the majority of its samples
# is force-scoring 0.0 into means and producing garbage numbers indistinguishable from real ones.
# 0.5 cleanly separates "broken judge" (majority unparseable) from "healthy judge with the odd
# drift". The rate is per-SAMPLE across all of a judge's (item x k) parse-attempts; invoke-errored
# samples are excluded from the denominator (a broken transport is not a broken output format).
JUDGE_PARSE_FAIL_RATE_THRESHOLD: Final[float] = 0.5

# The ``scorer`` tag a rubric-scored row carries (distinct from ``verdict``/``exact`` and from the
# runner's reserved ``no_response``). A rubric row's ``parsed`` records the item's 0-10 median-of-
# medians as a string on success, or :data:`~measure_twice.scoring.deterministic.PARSE_FAIL_MARKER`
# when NO judge produced a parseable sample for the item (a recorded rubric parse-fail scored 0.0).
RUBRIC_SCORER: Final[str] = "rubric"

# The rubric's 0-10 score range (plan §4). A parsed value outside it is CLAMPED to the boundary, not
# rejected: models occasionally drift ("11/10!") and clamping keeps the mean comparable across runs.
_MIN_SCORE: Final[float] = 0.0
_MAX_SCORE: Final[float] = 10.0

# SCORE-line regex (judge-core §5.6 robust-extract). Case-INSENSITIVE on the ``SCORE`` anchor and
# tolerant of surrounding text / missing newline, so ``SCORE: 7``, ``  score:7.5``, a
# ``... final SCORE: 8 ...`` prose line, and ``SCORE: 7 out of 10`` (-> 7) all parse. Requires a
# numeric token immediately after the colon, so a template echo (``SCORE: <n>``) with no digit does
# NOT match — the first REAL numeric SCORE wins. ``-?\d+(?:\.\d+)?`` admits an integer or decimal;
# a pathologically long digit run folds to ``inf`` under ``float`` and is then clamped, not raised.
# The ``(?![\w.])`` boundary makes a MALFORMED value parse-FAIL rather than partial-parse to a wrong
# number: ``SCORE: 1e9`` (sci-notation), ``SCORE: 7abc`` (trailing garbage), and ``SCORE: 7.5.2``
# (double decimal) all fail the boundary and record a parse-fail — honest, per the documented
# ``SCORE: <int|decimal>`` contract, instead of silently reading ``1``/``7``/``7.5``.
_SCORE_RE: Final[re.Pattern[str]] = re.compile(
    r"SCORE\s*:\s*(-?\d+(?:\.\d+)?)(?![\w.])", re.IGNORECASE
)


class JudgeParseFailError(ScoringError):
    """Raised when a single judge's parse-fail rate EXCEEDS :data:`JUDGE_PARSE_FAIL_RATE_THRESHOLD`.

    Fail-loud measurement guard (port of void_furnace's ``JudgeParseFailureError``): a judge whose
    model cannot emit the ``SCORE: <n>`` format parse-fails, and every parse-fail is force-scored
    0.0 — a low-but-real-looking mean indistinguishable from a judge that scored the work poorly.
    Per ``measurement-validity.md`` ("a parse-fail rate that silently drags means toward zero is a
    silent instrument failure"), the run ABORTS rather than emitting the poisoned numbers.

    Subclasses :class:`~measure_twice.scoring.deterministic.ScoringError` (and thus ``ValueError``)
    so it joins the package's fail-loud scoring family — an ``except ScoringError`` face already
    catches it, and the ``mt score`` handler surfaces it as a clean non-zero exit naming the
    offending judge(s) + rate. (void_furnace used a bare ``RuntimeError`` because it had no scoring-
    error family; measure-twice does, so the gate lives inside it.)
    """


# One judge call: given the built judge prompt + the judge model alias, return the adapter's
# ModelCallResult (SUCCESS / no-response / error). The DI seam — the default (:func:`default_judge_
# caller`) wraps ``claude_call``; tests inject a stub returning canned judge outputs, so ZERO live
# ``claude`` invocations occur in the suite. Judging the returned ModelCallResult (not a raw string)
# is what lets the caller keep PARSE-FAIL distinct from INVOKE-ERROR (``result.ok`` vs not).
JudgeCaller = Callable[[str, str], ModelCallResult]


@dataclass(frozen=True, slots=True)
class JudgeCell:
    """One (item, model response) to rubric-judge: the row's identity + the prompt's three inputs.

    ``rubric`` is the grading criteria — for a rubric suite it is the item's ``expected`` field
    (suites carry all content; there is no hidden judge template). ``prompt`` is the item's own
    prompt and ``response`` is the model output under judgement. :func:`judge_run` returns one
    :class:`JudgeItemResult` per cell, in input order.
    """

    item_id: str
    prompt: str
    rubric: str
    response: str


@dataclass(frozen=True, slots=True)
class JudgeItemResult:
    """The rubric verdict for one :class:`JudgeCell`.

    ``score`` is the 0-1 normalized item score (the mean across judges of each judge's 0-10 median,
    divided by 10). On an all-parse-fail cell (NO judge produced a single parseable sample) it is
    ``0.0`` and ``parsed`` is :data:`~measure_twice.scoring.deterministic.PARSE_FAIL_MARKER` — a
    RECORDED rubric parse-fail, never a silent 0 masquerading as a real low score. ``judge_scores``
    is the per-judge 0-10 medians (one per judge that produced >=1 parsed sample; empty on
    all-parse-fail) — the durable per-judge detail written to the row's ``judge_scores`` field.
    """

    item_id: str
    score: float
    parsed: str
    judge_scores: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class JudgeRunResult:
    """The output of :func:`judge_run`: one :class:`JudgeItemResult` per cell (input order) plus the
    accumulated per-judge parse stats ``(judge, n_parse_fail, n_parse_attempts)`` the gate ran on.

    Returned only when the gate PASSED (a failing gate raises :class:`JudgeParseFailError` rather
    than returning), so a consumer never has to re-check the rate — a returned result is clean.
    """

    results: tuple[JudgeItemResult, ...]
    per_judge_parse_stats: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class _JudgeCellStat:
    """One judge's parse bookkeeping for ONE cell: parse-fails, parse-attempts, and the median
    (0-10) of its parsed samples (``None`` if it produced no parseable sample for this cell)."""

    judge: str
    n_parse_fail: int
    n_parse_attempts: int
    median: float | None


def build_judge_prompt(item_prompt: str, response: str, rubric: str) -> str:
    """Construct the judge prompt (the two-line ``SCORE:``/``RATIONALE:`` contract).

    Assembled in ONE place from the item's own content (prompt + rubric) and the response under
    judgement — no per-judge tuning, no hidden template (plan §4: suites carry all content). The
    same prompt is sent to every judge model regardless of provider so their scores are comparable.
    """
    return (
        "You are grading a model's response against a rubric. Score the response on a 0-10 scale "
        "per the rubric below.\n\n"
        f"RUBRIC:\n{rubric}\n\n"
        f"PROMPT GIVEN TO THE MODEL:\n{item_prompt}\n\n"
        f"MODEL RESPONSE TO GRADE:\n{response}\n\n"
        "Reply with EXACTLY these two lines and nothing else:\n"
        "SCORE: <an integer or decimal from 0 to 10>\n"
        "RATIONALE: <one or two sentences explaining the score>\n"
    )


def parse_judge_score(text: str) -> float | None:
    """Extract + validate the 0-10 SCORE from a judge's raw output (the §5.6 spine's front half).

    Robustly finds the first ``SCORE: <number>`` (case-insensitive, tolerant of surrounding prose),
    validates it is a WELL-FORMED number, and CLAMPS it to ``[0, 10]``. Returns ``None`` on a
    PARSE-FAIL — no parseable ``SCORE`` line, OR a malformed value (``1e9`` sci-notation, ``7abc``
    trailing garbage, ``7.5.2`` double decimal) that fails the token boundary — which the caller
    records distinctly (never a crash, never a silent partial-parse to a wrong number). NEVER
    raises: the regex admits only a float-parseable token; an out-of-range/overflow value clamps.
    """
    match = _SCORE_RE.search(text)
    if match is None:
        return None
    value = float(match.group(1))  # regex guarantees a float literal; huge runs fold to inf (clamp)
    return min(_MAX_SCORE, max(_MIN_SCORE, value))


def _judge_one_cell(
    cell: JudgeCell, judges: Sequence[str], judge_caller: JudgeCaller, k: int
) -> tuple[JudgeItemResult, tuple[_JudgeCellStat, ...]]:
    """Judge one cell across every judge (k samples each); return its result + per-judge cell stats.

    Per judge: sample ``k`` times, parse each sample's SCORE, and take the MEDIAN of the parsed
    samples (dropping parse-fails AND invoke-errors from the median set). A sample whose call did
    not return usable text (``not result.ok`` — an adapter error OR the no-response state) is an
    INVOKE-ERROR: excluded from BOTH parse counters. A returned-but-unparseable sample is a
    PARSE-FAIL: counted toward ``n_parse_fail`` and ``n_parse_attempts``.

    The item score is the mean across judges of each judge's 0-10 median, divided by 10 (0-1). If NO
    judge produced a single parseable sample the item is a recorded parse-fail: score ``0.0``,
    ``parsed`` = :data:`PARSE_FAIL_MARKER`, empty ``judge_scores``.
    """
    judge_prompt = build_judge_prompt(cell.prompt, cell.response, cell.rubric)
    cell_stats: list[_JudgeCellStat] = []
    per_judge_medians: list[float] = []
    for judge in judges:
        parsed_samples: list[float] = []
        n_parse_fail = 0
        for _sample in range(k):
            result = judge_caller(judge_prompt, judge)
            if not result.ok:
                # INVOKE-ERROR (adapter error OR no-response): no usable text to parse. Excluded
                # from both counters — a broken transport/empty answer is not a broken format.
                continue
            value = parse_judge_score(result.response_raw)
            if value is None:
                n_parse_fail += 1  # PARSE-FAIL: returned text, no parseable SCORE — counts to rate.
                continue
            parsed_samples.append(value)
        # A parse-attempt returned a response to parse (parsed-ok + parse-failed); invoke-errors out
        n_parse_attempts = n_parse_fail + len(parsed_samples)
        median = statistics.median(parsed_samples) if parsed_samples else None
        cell_stats.append(_JudgeCellStat(judge, n_parse_fail, n_parse_attempts, median))
        if median is not None:
            per_judge_medians.append(median)

    if per_judge_medians:
        item_raw = statistics.mean(per_judge_medians)  # 0-10 mean across judges' medians
        return (
            JudgeItemResult(
                item_id=cell.item_id,
                score=item_raw / 10.0,  # normalize 0-10 -> 0-1 (plan §4)
                parsed=repr(item_raw),
                judge_scores=tuple(per_judge_medians),
            ),
            tuple(cell_stats),
        )
    # No judge produced a single parseable sample -> a RECORDED rubric parse-fail scored 0.0.
    return (
        JudgeItemResult(item_id=cell.item_id, score=0.0, parsed=PARSE_FAIL_MARKER, judge_scores=()),
        tuple(cell_stats),
    )


def _check_parse_fail_gate(per_judge_parse_stats: Sequence[tuple[str, int, int]]) -> None:
    """Fire the PER-JUDGE parse-fail gate: raise :class:`JudgeParseFailError` if a judge's own rate
    exceeds :data:`JUDGE_PARSE_FAIL_RATE_THRESHOLD`.

    Per-judge, NOT pooled: a pooled rate lets one broken judge hide behind healthy peers (2 judges /
    1 broken pools to exactly 0.50 and slips a ``> 0.50`` gate) while still force-scoring 0.0 into
    every mean. A judge with ZERO parse-attempts (every sample invoke-errored) has no parseability
    signal and is SKIPPED, never a false fire. The message names every offender worst-first.

    Doctrine: a parse-fail rate that silently drags means toward zero is a silent instrument failure
    (``dev/.claude/rules/measurement-validity.md`` § "Calibrate with anchors before comparing
    candidates") — hence the loud abort rather than an emitted-but-poisoned mean. The raised message
    stays terse (judge + rate + threshold), like every other fail-loud raise in the package.
    """
    offenders: list[tuple[str, int, int, float]] = []
    for judge, n_fail, n_attempts in per_judge_parse_stats:
        if n_attempts == 0:  # all samples invoke-errored -> no parseability signal to judge.
            continue
        rate = n_fail / n_attempts
        if rate > JUDGE_PARSE_FAIL_RATE_THRESHOLD:
            offenders.append((judge, n_fail, n_attempts, rate))
    if not offenders:
        return
    offenders.sort(key=lambda o: o[3], reverse=True)  # worst-first so the headline names the worst.
    detail = "; ".join(
        f"{judge!r} rate {rate:.2f} ({fail}/{attempts} unparseable)"
        for judge, fail, attempts, rate in offenders
    )
    raise JudgeParseFailError(
        f"judge parse-fail rate exceeds the {JUDGE_PARSE_FAIL_RATE_THRESHOLD:.2f} per-judge "
        f"threshold: {detail}"
    )


def judge_run(
    cells: Sequence[JudgeCell],
    *,
    judges: Sequence[str],
    judge_caller: JudgeCaller,
    k: int = JUDGE_SAMPLE_K,
) -> JudgeRunResult:
    """Rubric-judge every cell, accumulate the per-judge parse-fail rate, and fire the gate.

    The RUN-LEVEL rubric pass: each cell is judged across ``judges`` (k samples each, median of the
    parsed samples per judge — :func:`_judge_one_cell`), the per-judge parse stats are summed across
    ALL cells, and the PER-JUDGE gate runs last (:func:`_check_parse_fail_gate`) — so a broken judge
    aborts the WHOLE run with :class:`JudgeParseFailError`, not one cell. On a clean gate returns a
    :class:`JudgeRunResult` with one :class:`JudgeItemResult` per cell in input order.

    Fail loud on bad parameters (``judges`` empty, ``k < 1``) — an un-runnable judge config aborts
    rather than silently scoring nothing. Duplicate judges are de-duplicated (a doubled judge must
    not double-count its parse rate or its median contribution).
    """
    unique_judges = list(dict.fromkeys(judges))
    if not unique_judges:
        raise ScoringError("rubric judging requires at least one judge model")
    if k < 1:
        raise ScoringError(f"rubric judging requires k >= 1 samples per judge, got {k}")

    results: list[JudgeItemResult] = []
    acc_fail: dict[str, int] = {judge: 0 for judge in unique_judges}
    acc_attempts: dict[str, int] = {judge: 0 for judge in unique_judges}
    for cell in cells:
        item_result, cell_stats = _judge_one_cell(cell, unique_judges, judge_caller, k)
        results.append(item_result)
        for stat in cell_stats:
            acc_fail[stat.judge] += stat.n_parse_fail
            acc_attempts[stat.judge] += stat.n_parse_attempts

    per_judge_parse_stats = tuple(
        (judge, acc_fail[judge], acc_attempts[judge]) for judge in unique_judges
    )
    _check_parse_fail_gate(per_judge_parse_stats)  # raises JudgeParseFailError on a broken judge.
    return JudgeRunResult(results=tuple(results), per_judge_parse_stats=per_judge_parse_stats)


def make_rubric_run_scorer(
    *,
    judges: Sequence[str],
    judge_caller: JudgeCaller,
    k: int = JUDGE_SAMPLE_K,
) -> RunScorer:
    """Build the :data:`~measure_twice.runner.RunScorer` ``mt score`` drives for a rubric suite.

    The bridge between the runner's row store and :func:`judge_run`: given all of a run's rows + the
    suite's items-by-id, it judges every SCOREABLE row's response (skipping error rows and the
    runner's no-response force-0 rows — a model that produced nothing is never judged, plan §4),
    then writes each row's ``score`` (0-1), ``scorer`` (:data:`RUBRIC_SCORER`), ``parsed`` (the 0-10
    median or :data:`PARSE_FAIL_MARKER`), and ``judge_scores`` (per-judge 0-10 medians). For a
    rubric suite the rubric text is the item's ``expected`` field (suites carry all content).

    The per-judge gate accumulates across every judged row and fires inside :func:`judge_run` BEFORE
    this returns, so :func:`~measure_twice.runner.score_run_batch` (which rewrites only AFTER the
    scorer returns) never persists poisoned scores. An unknown ``item_id`` on a row is a corrupt run
    store and fails loud (:class:`ScoringError`).
    """

    def _run_scorer(rows: Sequence[RunRow], items_by_id: Mapping[str, Item]) -> Sequence[RunRow]:
        cell_positions: list[int] = []
        cells: list[JudgeCell] = []
        for index, row in enumerate(rows):
            if row.error is not None:
                continue  # terminal error row — no text to judge.
            if row.scorer == NO_RESPONSE_SCORER:
                continue  # force-0 no-response — never judged (pre-scoring invariant, plan §4).
            item = items_by_id.get(row.item_id)
            if item is None:
                raise ScoringError(
                    f"rubric row references unknown item_id {row.item_id!r} "
                    "(not in the suite snapshot)"
                )
            cells.append(
                JudgeCell(
                    item_id=row.item_id,
                    prompt=item.prompt,
                    rubric=item.expected,
                    response=row.response_raw,
                )
            )
            cell_positions.append(index)

        run_result = judge_run(cells, judges=judges, judge_caller=judge_caller, k=k)

        rescored = list(rows)
        for position, index in enumerate(cell_positions):
            item_result = run_result.results[position]
            rescored[index] = replace(
                rows[index],
                parsed=item_result.parsed,
                score=item_result.score,
                scorer=RUBRIC_SCORER,
                judge_scores=list(item_result.judge_scores) if item_result.judge_scores else None,
            )
        return rescored

    return _run_scorer


def default_judge_caller(
    config: RunConfig,
    budget: CallBudget,
    *,
    timeout: float | None = None,
    runner_factory: RunnerFactory | None = None,
) -> JudgeCaller:
    """The production judge-caller: one Claude call per sample via ``claude_call`` (plan §4).

    Closes over the run ``config`` + ``budget`` (each judge sample counts against the budget, to cap
    subscription spend) and returns a :class:`JudgeCaller` the rubric pass calls per sample.
    ``runner_factory`` is the same subprocess DI seam ``claude_call`` takes — production leaves it
    ``None`` (real subprocess); a live-path test could inject a stub subprocess factory, though the
    rubric tests inject a stub :class:`JudgeCaller` directly and never reach this.
    """

    def _call(judge_prompt: str, judge_alias: str) -> ModelCallResult:
        return claude_call(
            judge_prompt,
            alias=judge_alias,
            config=config,
            budget=budget,
            timeout=timeout,
            runner_factory=runner_factory,
        )

    return _call
