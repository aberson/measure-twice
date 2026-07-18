"""Rubric-judge anchor gate — the permanent ``score(good) > score(garbage)`` layer for the judge.

The LLM-judge counterpart to ``test_anchors_gate.py`` (which gates the deterministic scorers).
measurement-validity.md § "Calibrate with anchors before comparing candidates": before trusting a
metric, feed it a frozen known-good and a known-garbage input and assert ``score(good) >
score(garbage)``. A rubric judge that cannot rank good over garbage cannot pick winners.

The judge is STUBBED (ZERO live ``claude`` calls) — this gate proves the SCORING PIPELINE
discriminates (k=3 median -> 0-10 -> normalize -> ordering) through the PRODUCTION path
(:func:`~measure_twice.scoring.judge.judge_run`), not that a live model grades well (that is a
live-run / calibration concern, not a per-commit gate). The pair is frozen as DATA
(``rubric_anchor.json``) so it is a stable regression contract, decoupled from code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from measure_twice.adapters.base import ModelCallResult
from measure_twice.scoring.judge import JudgeCaller, JudgeCell, judge_run

_ANCHOR_PATH = Path(__file__).with_name("rubric_anchor.json")


def _load_anchor() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_ANCHOR_PATH.read_text(encoding="utf-8"))
    return data


_ANCHOR = _load_anchor()


def _content_judge(good_response: str, high_line: str, low_line: str) -> JudgeCaller:
    """A stub judge that scores by content: the judge prompt embeds the response under grading, so
    it returns the high SCORE line when the good response is present, the low line otherwise."""

    def _call(prompt: str, _alias: str) -> ModelCallResult:
        text = high_line if good_response in prompt else low_line
        return ModelCallResult.success(response_raw=text, resolved_model="stub", elapsed_s=0.0)

    return _call


def _score(response: str) -> float:
    """Score one response through the production rubric path (k=3) with the stubbed judge."""
    caller = _content_judge(_ANCHOR["good"], _ANCHOR["high_score_line"], _ANCHOR["low_score_line"])
    cell = JudgeCell(
        item_id="rubric-anchor",
        prompt=_ANCHOR["prompt"],
        rubric=_ANCHOR["rubric"],
        response=response,
    )
    result = judge_run([cell], judges=["sonnet"], judge_caller=caller, k=3)
    score = result.results[0].score
    assert score is not None
    return score


def test_rubric_anchor_good_differs_from_garbage() -> None:
    """The frozen good and garbage responses are genuinely distinct inputs."""
    assert _ANCHOR["good"] != _ANCHOR["garbage"]


def test_rubric_anchor_good_outscores_garbage() -> None:
    """THE ORDERING GATE: the known-good response strictly out-scores the known-garbage one through
    the production rubric judge (k=3 median, stubbed judge). Forever."""
    good = _score(_ANCHOR["good"])
    garbage = _score(_ANCHOR["garbage"])
    assert good > garbage, (
        f"rubric anchor did not discriminate: good={good} garbage={garbage} "
        "(non-discriminating judge pipeline)"
    )
    # The good response earns near-full credit and the garbage one near-zero — the anchors are
    # deliberately divergent in the dimension the rubric keys on, so the ordering is unambiguous.
    assert good >= 0.9
    assert garbage <= 0.2
