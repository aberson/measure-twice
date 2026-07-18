"""measure-twice scoring: deterministic scorers + the LLM rubric judge.

``deterministic`` holds the verdict + exact scorers and the judge-core §5.6-conformant verdict
parse spine (plan §4/§5). ``judge`` adds the k=3 median rubric judge with the per-judge parse-fail
gate at a RUN-LEVEL scoring pass (``make_rubric_run_scorer`` -> ``runner.score_run_batch``), since
the gate accumulates across the whole run. Everything a caller needs is re-exported here.
"""

from __future__ import annotations

from measure_twice.scoring.deterministic import (
    EXACT_SCORER,
    PARSE_FAIL_MARKER,
    VERDICT_SCORER,
    ScoringError,
    exact_match,
    extract_verdict_label,
    make_deterministic_scorer,
    score_exact,
    score_verdict,
    suite_score,
)
from measure_twice.scoring.judge import (
    JUDGE_PARSE_FAIL_RATE_THRESHOLD,
    JUDGE_SAMPLE_K,
    RUBRIC_SCORER,
    JudgeCaller,
    JudgeCell,
    JudgeItemResult,
    JudgeParseFailError,
    JudgeRunResult,
    build_judge_prompt,
    default_judge_caller,
    judge_run,
    make_rubric_run_scorer,
    parse_judge_score,
)

__all__ = [
    "EXACT_SCORER",
    "JUDGE_PARSE_FAIL_RATE_THRESHOLD",
    "JUDGE_SAMPLE_K",
    "PARSE_FAIL_MARKER",
    "RUBRIC_SCORER",
    "VERDICT_SCORER",
    "JudgeCaller",
    "JudgeCell",
    "JudgeItemResult",
    "JudgeParseFailError",
    "JudgeRunResult",
    "ScoringError",
    "build_judge_prompt",
    "default_judge_caller",
    "exact_match",
    "extract_verdict_label",
    "judge_run",
    "make_deterministic_scorer",
    "make_rubric_run_scorer",
    "parse_judge_score",
    "score_exact",
    "score_verdict",
    "suite_score",
]
