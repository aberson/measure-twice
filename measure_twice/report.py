"""measure-twice reporting — per-run markdown, cross-run comparison, JSONL export (plan §5/§6).

Reads a stored run's ``manifest.json`` + ``rows.jsonl`` (the run-store the runner owns — plan §3)
and renders a DETERMINISTIC markdown report: per model, the 0-100 suite score, the item/cell count,
and the counts of no-response rows, parse-fail rows, and error/defer rows. The 0-100 score reuses
the Step-5 normalization (:func:`~measure_twice.scoring.deterministic.suite_score` — imported, never
re-derived); the parse-fail count is a FIRST-CLASS column, not a footnote
(``measurement-validity.md`` § the parse-fail rate is a *signal*): a silent parse-fail->0 drags the
mean toward zero, so it is surfaced alongside the score it depresses. A parse-fail is counted by the
SINGLE canonical marker (:data:`~measure_twice.scoring.deterministic.PARSE_FAIL_MARKER`), never by
scorer name — verdict AND rubric both funnel an unparseable cell through it, so counting per-scorer
would silently miss rubric parse-fails.

Deferred (plan §3, not Step 7): the "latest-per-(suite_hash, model) by manifest timestamp unless
--run pins one" auto-resolution is NOT built here — cross-run comparison takes EXPLICIT run ids
(``--compare``), which the Step-7 done-when needs; latest-per resolution is more natural once
Phase C has accumulated many runs.

Cross-run comparison (``mt report --compare``) compares runs BY EQUAL SUITE HASH only (plan §3: "a
changed hash = a different instrument; cross-run comparisons require equal hashes"). Comparing runs
with mismatched ``suite_hash`` is a fail-loud :class:`ReportError` naming the mismatch — never a
silent comparison across two different instruments (``measurement-validity.md`` § match measurement
scope to decision scope).

Run-store access reuses the runner's OWN readers (``_resolve_run_dir`` traversal guard,
``_read_manifest``, ``_read_rows`` torn-line tolerance) so the run-store layout has ONE owner
(``code-quality.md`` § one source of truth) and an untrusted ``mt report <run_id>`` positional is
path-traversal-guarded exactly as ``mt score`` is. Every run-store fault the runner raises
(``RunError``: invalid/traversing run_id, missing dir, corrupt manifest/rows) is re-faced as
:class:`ReportError`, so a report has a single fail-loud sentinel. Core is stdlib-only (``json`` /
``statistics`` via the imported normalizer).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from measure_twice import runner
from measure_twice.runner import NO_RESPONSE_SCORER, RunError, RunRow
from measure_twice.scoring.deterministic import PARSE_FAIL_MARKER, suite_score

__all__ = [
    "ComparisonReport",
    "ModelReport",
    "ReportError",
    "RunReport",
    "build_comparison",
    "build_run_report",
    "render_comparison",
    "render_run_report",
    "run_report_jsonl",
]


class ReportError(ValueError):
    """Raised on a report fault (missing/traversing run, corrupt store, cross-run hash mismatch).

    Fail-loud sentinel subclassing ``ValueError`` — the package convention shared by
    ``config.ConfigError`` / ``suite.SuiteError`` / ``runner.RunError`` / ``scoring.ScoringError``,
    so one ``except (..., ReportError)`` face catches them all. A report that cannot be honestly
    produced (a run that will not open, or a comparison spanning two DIFFERENT instruments) aborts
    rather than rendering numbers indistinguishable from a real, single-instrument report.
    """


@dataclass(frozen=True, slots=True)
class ModelReport:
    """Per-model roll-up for one run: the 0-100 suite score plus the countable failure signals.

    ``suite_score`` is ``100 x mean`` of every NUMERIC row score for the model (no-response force-0s
    and verdict parse-fails both score 0.0 and are INCLUDED — they legitimately depress the mean; an
    error/defer row has ``score=None`` and is excluded). It is ``None`` only when the model produced
    no numeric score at all (every cell errored/deferred, or the run was collected-but-unscored).
    ``n_no_response`` / ``n_parse_fail`` / ``n_error`` are the first-class signal counts.
    """

    model: str
    n_cells: int
    n_scored: int
    n_items: int
    suite_score: float | None
    n_no_response: int
    n_parse_fail: int
    n_error: int


@dataclass(frozen=True, slots=True)
class RunReport:
    """One stored run's report data: manifest identity + a per-model roll-up in a stable order."""

    run_id: str
    suite: str
    suite_hash: str
    started_utc: str
    roster: list[str]
    models: list[ModelReport]

    @property
    def total_parse_fail(self) -> int:
        return sum(m.n_parse_fail for m in self.models)

    @property
    def total_no_response(self) -> int:
        return sum(m.n_no_response for m in self.models)

    @property
    def total_error(self) -> int:
        return sum(m.n_error for m in self.models)

    @property
    def total_scored(self) -> int:
        return sum(m.n_scored for m in self.models)


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """A cross-run comparison over runs sharing ONE ``suite_hash`` (fail-loud on any mismatch).

    ``runs`` are the per-run reports in the caller's order (primary first); ``models`` is the sorted
    union of every model that appears in any run — so the rendered table is deterministic regardless
    of per-run roster order. ``suite_hash`` is the single shared instrument identity.
    """

    suite: str
    suite_hash: str
    runs: list[RunReport]
    models: list[str]


# --- Run-store access (reuse the runner's readers — one owner of the layout + traversal guard) ---


def _open_run_store(run_id: str, out_dir: str | Path) -> tuple[Mapping[str, object], list[RunRow]]:
    """Resolve + read a stored run's manifest + rows, re-facing a ``RunError`` as ``ReportError``.

    Delegates to the runner's own readers so report shares the run-store contract (traversal guard
    on the untrusted ``run_id``, torn-trailing-line tolerance, fail-loud on a corrupt/missing store)
    with ``mt run``/``mt score`` — no re-derived JSONL parse to drift (``code-quality.md``). A
    missing run dir is reported explicitly before the manifest read for a clearer message.
    """
    try:
        run_dir = runner._resolve_run_dir(Path(out_dir), run_id)
    except RunError as exc:  # invalid / path-traversing run_id
        raise ReportError(str(exc)) from exc
    if not run_dir.is_dir():
        raise ReportError(f"run not found: {run_dir}")
    try:
        manifest = runner._read_manifest(run_dir)
        rows, _torn = runner._read_rows(run_dir)
    except RunError as exc:  # corrupt manifest / mid-file rows corruption
        raise ReportError(str(exc)) from exc
    return manifest, rows


def _manifest_str(manifest: Mapping[str, object], key: str) -> str:
    """A required string manifest field, else fail loud (a corrupt/incomplete run store)."""
    value = manifest.get(key)
    if not isinstance(value, str):
        raise ReportError(f"manifest field {key!r} missing or not a string (corrupt run store)")
    return value


def _manifest_roster(manifest: Mapping[str, object]) -> list[str]:
    """The manifest roster as a list of model names, else fail loud."""
    value = manifest.get("roster")
    if not isinstance(value, list):
        raise ReportError("manifest field 'roster' missing or not a list (corrupt run store)")
    return [str(model) for model in value]


# --- Per-model roll-up -------------------------------------------------------------------


def _model_report(model: str, rows: Sequence[RunRow]) -> ModelReport:
    """Roll one model's rows into a :class:`ModelReport` (numeric-score mean + signal counts)."""
    scores = [row.score for row in rows if row.score is not None]
    return ModelReport(
        model=model,
        n_cells=len(rows),
        n_scored=len(scores),
        n_items=len({row.item_id for row in rows}),
        # suite_score fails loud on an empty sequence (Step-5 contract), so guard: no numeric score
        # for the model -> None (rendered "n/a"), never a fabricated 0 or a crash.
        suite_score=suite_score(scores) if scores else None,
        n_no_response=sum(1 for row in rows if row.scorer == NO_RESPONSE_SCORER),
        # A parse-fail is recorded via the SINGLE canonical marker, regardless of which scorer wrote
        # it: the verdict scorer AND the rubric run-scorer both set ``parsed=PARSE_FAIL_MARKER,
        # score=0.0`` on an unparseable cell (deterministic.py `score_verdict`; judge.py
        # `_judge_one_cell` all-parse-fail). Gating on ``scorer == "verdict"`` would silently miss a
        # RUBRIC parse-fail — force-scored into the mean (depressing suite_score) yet reported as
        # zero — masking the exact "silent parse-fail->0 drags the mean" signal this first-class
        # column exists to surface (measurement-validity). ``PARSE_FAIL_MARKER`` is unambiguous: no
        # non-fail scorer output collides with it (verdict labels reserving it are rejected at load;
        # exact emits "match"/"no_match"; rubric success emits a numeric repr).
        n_parse_fail=sum(1 for row in rows if row.parsed == PARSE_FAIL_MARKER),
        n_error=sum(1 for row in rows if row.error is not None),
    )


def build_run_report(run_id: str, out_dir: str | Path = "data") -> RunReport:
    """Open a stored run and roll it into a :class:`RunReport` (deterministic model ordering).

    Models are emitted in MANIFEST ROSTER order (the run's own declared order — stable and
    meaningful; a roster model that produced no rows still appears, honestly, with ``score=n/a``),
    followed by any model present in the rows but not the roster (sorted — defensive, should not
    happen). Fail loud (:class:`ReportError`) on a missing/traversing run or a corrupt run store.
    """
    manifest, rows = _open_run_store(run_id, out_dir)
    roster = _manifest_roster(manifest)

    rows_by_model: dict[str, list[RunRow]] = {}
    for row in rows:
        rows_by_model.setdefault(row.model, []).append(row)

    ordered_models = list(roster)
    for model in sorted(rows_by_model):  # sorted -> deterministic; roster order preserved above
        if model not in ordered_models:
            ordered_models.append(model)

    models = [_model_report(model, rows_by_model.get(model, [])) for model in ordered_models]
    return RunReport(
        run_id=_manifest_str(manifest, "run_id"),
        suite=_manifest_str(manifest, "suite"),
        suite_hash=_manifest_str(manifest, "suite_hash"),
        started_utc=_manifest_str(manifest, "started_utc"),
        roster=roster,
        models=models,
    )


def build_comparison(run_ids: Sequence[str], out_dir: str | Path = "data") -> ComparisonReport:
    """Build a cross-run comparison over ``run_ids`` — FAIL LOUD if their ``suite_hash`` differ.

    Every run must measure the SAME instrument: a differing ``suite_hash`` means a different suite
    content (plan §3), so comparing scores across them is comparing apples to oranges — it raises
    :class:`ReportError` naming the first mismatching run and both hashes rather than silently
    tabulating across instruments. The models axis is the sorted union across runs, so the table is
    deterministic regardless of per-run roster order.
    """
    if not run_ids:
        raise ReportError("build_comparison requires at least one run id")
    reports = [build_run_report(run_id, out_dir) for run_id in run_ids]
    base = reports[0]
    for report in reports[1:]:
        if report.suite_hash != base.suite_hash:
            raise ReportError(
                "cannot compare runs across different instruments (suite_hash mismatch): "
                f"{base.run_id} has {base.suite_hash} but {report.run_id} has {report.suite_hash}; "
                "a changed suite hash is a DIFFERENT instrument (plan §3)"
            )
    models = sorted({m.model for report in reports for m in report.models})
    return ComparisonReport(
        suite=base.suite, suite_hash=base.suite_hash, runs=reports, models=models
    )


# --- Rendering (deterministic: same run -> byte-identical markdown; no wall-clock in the output) --


def _fmt_score(score: float | None) -> str:
    """A score cell: one-decimal 0-100, or ``n/a`` when the model has no numeric score."""
    return "n/a" if score is None else f"{score:.1f}"


def render_run_report(report: RunReport) -> str:
    """Render a :class:`RunReport` to deterministic markdown (no timestamp-of-render, reproducible).

    The output is a pure function of the run store, so re-rendering the same run yields
    byte-for-byte identical markdown (the ``mt score`` re-runnability spirit — Decision 10).
    """
    lines = [
        f"# measure-twice run report: {report.run_id}",
        "",
        f"- **Suite:** {report.suite} (`{report.suite_hash}`)",
        f"- **Roster:** {', '.join(report.roster) if report.roster else '(none)'}",
        f"- **Started (UTC):** {report.started_utc}",
        "",
        "| Model | Score (0-100) | Items | Scored | No-response | Parse-fail | Error/defer |",
        "|---|---|---|---|---|---|---|",
    ]
    for model in report.models:
        lines.append(
            f"| {model.model} | {_fmt_score(model.suite_score)} | {model.n_items} | "
            f"{model.n_scored} | {model.n_no_response} | {model.n_parse_fail} | {model.n_error} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_comparison(comparison: ComparisonReport) -> str:
    """Render a :class:`ComparisonReport` to a deterministic per-model x per-run score table.

    Columns are the runs in the caller's order (primary first); rows are the sorted model union; a
    cell is the model's 0-100 score in that run, or ``-`` when the model was not run there.
    """
    scores_by_run: list[dict[str, float | None]] = [
        {m.model: m.suite_score for m in run.models} for run in comparison.runs
    ]
    header = "| Model | " + " | ".join(run.run_id for run in comparison.runs) + " |"
    divider = "|---" * (len(comparison.runs) + 1) + "|"
    lines = [
        "# measure-twice cross-run comparison",
        "",
        f"- **Suite:** {comparison.suite} (`{comparison.suite_hash}`)",
        f"- **Runs compared:** {len(comparison.runs)} (equal suite hash)",
        "",
        header,
        divider,
    ]
    for model in comparison.models:
        cells = [
            _fmt_score(run_scores[model]) if model in run_scores else "-"
            for run_scores in scores_by_run
        ]
        lines.append(f"| {model} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def run_report_jsonl(report: RunReport) -> str:
    """Export a :class:`RunReport` as JSONL — one canonical line per model (plan §6 JSONL export).

    A minimal, machine-readable sibling of the markdown table (same numbers, one JSON object per
    model), for piping a run's per-model roll-up into downstream tooling. Deterministic + ASCII.
    """
    return "\n".join(
        json.dumps(
            {
                "run_id": report.run_id,
                "suite": report.suite,
                "suite_hash": report.suite_hash,
                "model": model.model,
                "suite_score": model.suite_score,
                "n_items": model.n_items,
                "n_cells": model.n_cells,
                "n_scored": model.n_scored,
                "n_no_response": model.n_no_response,
                "n_parse_fail": model.n_parse_fail,
                "n_error": model.n_error,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        for model in report.models
    )
