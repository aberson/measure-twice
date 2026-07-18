"""measure-twice scoring: deterministic scorers now, the LLM rubric judge in Step 6.

``deterministic`` holds the verdict + exact scorers and the judge-core §5.6-conformant verdict
parse spine (plan §4/§5). ``judge`` (Step 6) will add the k=3 median rubric judge at the same
runner ``Scorer`` seam. Everything a caller needs today is re-exported here.
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

__all__ = [
    "EXACT_SCORER",
    "PARSE_FAIL_MARKER",
    "VERDICT_SCORER",
    "ScoringError",
    "exact_match",
    "extract_verdict_label",
    "make_deterministic_scorer",
    "score_exact",
    "score_verdict",
    "suite_score",
]
