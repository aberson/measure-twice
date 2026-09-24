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

Cross-run comparison (``mt report --compare``) requires equal suite and execution/receipt hashes.
Legacy and sealed runs cannot be compared. Each allowed comparison prints the execution and
identity evidence for every run beside the unchanged official scores.

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
from html import escape as html_escape
from pathlib import Path
from typing import Final

from measure_twice import runner
from measure_twice.adapters.base import UNRESOLVED_MODEL_ID
from measure_twice.runner import NO_RESPONSE_SCORER, RunError, RunRow
from measure_twice.scoring.deterministic import PARSE_FAIL_MARKER, suite_score

__all__ = [
    "LEGACY_UNSEALED",
    "NOT_ROUTING_ELIGIBLE",
    "PRELIMINARY_SEAL_IDENTITY_OK",
    "UNRESOLVED_IDENTITY",
    "UNVERIFIED_LEGACY",
    "ComparisonReport",
    "ExecutionEvidence",
    "ModelReport",
    "ReportError",
    "RunReport",
    "build_comparison",
    "build_execution_evidence",
    "build_run_report",
    "render_comparison",
    "render_run_report",
    "run_report_jsonl",
]

# Visible status labels for the execution seal (plan §6.4). A legacy run — a manifest with NO
# execution receipt — is marked LEGACY_UNSEALED and NOT_ROUTING_ELIGIBLE without ever touching its
# official scores. An arm whose concrete provider identity was never observed is marked
# UNRESOLVED_IDENTITY (a requested alias is never silently substituted — plan §6.3). These are
# additive report annotations; Step 59 owns the constant-control routing verdict, this step only
# gates on the seal and on identity evidence.
LEGACY_UNSEALED: Final[str] = "LEGACY_UNSEALED"
PRELIMINARY_SEAL_IDENTITY_OK: Final[str] = "PRELIMINARY_SEAL_IDENTITY_OK"
NOT_ROUTING_ELIGIBLE: Final[str] = "NOT_ROUTING_ELIGIBLE"
UNVERIFIED_LEGACY: Final[str] = "UNVERIFIED_LEGACY"
UNRESOLVED_IDENTITY: Final[str] = UNRESOLVED_MODEL_ID


class ReportError(ValueError):
    """Raised on a report fault (missing/traversing run, corrupt store, cross-run hash mismatch).

    Fail-loud sentinel subclassing ``ValueError`` — the package convention shared by
    ``config.ConfigError`` / ``suite.SuiteError`` / ``runner.RunError`` / ``scoring.ScoringError``,
    so one ``except (..., ReportError)`` face catches them all. A report that cannot be honestly
    produced (a run that will not open, or a comparison spanning two DIFFERENT instruments) aborts
    rather than rendering numbers indistinguishable from a real, single-instrument report.
    """


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """The additive execution receipt surfaced in every report (plan §6.3/§6.4).

    Built from the manifest's ``execution_receipt`` — ``None`` in :class:`RunReport` marks a legacy
    (unsealed) run. It names the profile identity, the three static hashes plus the receipt self
    hash, the sealing mode, and the doctor-validated Claude executable path/version (``None`` for a
    receipt whose selected bindings are all non-Claude). ``bindings`` is the per-alias
    provider/requested-model contract the run committed to; reports read it to name provider and
    requested identity beside the observed resolved identity.
    """

    profile_id: str
    sealing_mode: str
    execution_profile_sha256: str
    provider_profile_sha256: str
    context_profile_sha256: str
    receipt_sha256: str
    claude_executable: str | None
    claude_version: str | None
    bindings: tuple[tuple[str, str, str], ...]

    def binding_for(self, alias: str) -> tuple[str, str] | None:
        """``(provider, requested_model)`` for ``alias``, or ``None`` if the receipt omits it."""
        for b_alias, provider, requested in self.bindings:
            if b_alias == alias:
                return (provider, requested)
        return None


@dataclass(frozen=True, slots=True)
class ModelReport:
    """Per-model roll-up for one run: the 0-100 suite score plus the countable failure signals.

    ``suite_score`` is ``100 x mean`` of every NUMERIC row score for the model (no-response force-0s
    and verdict parse-fails both score 0.0 and are INCLUDED — they legitimately depress the mean; an
    error/defer row has ``score=None`` and is excluded). It is ``None`` only when the model produced
    no numeric score at all (every cell errored/deferred, or the run was collected-but-unscored).
    ``n_no_response`` / ``n_parse_fail`` / ``n_error`` are the first-class signal counts.

    Identity/seal evidence (plan §6.3/§6.4, additive — the score is never changed): ``provider`` and
    ``requested_model`` come from the receipt. ``stored_identities`` preserves row values, while
    ``resolved_identities`` contains only identities backed by a sealed, bound provider. Historical
    rows may hold requested-alias fallbacks and are marked ``UNVERIFIED_LEGACY``. A sealed arm with
    complete identity evidence receives only a preliminary status. Step 59 owns positive routing
    eligibility after calibration controls.
    """

    model: str
    n_cells: int
    n_scored: int
    n_items: int
    suite_score: float | None
    n_no_response: int
    n_parse_fail: int
    n_error: int
    provider: str | None
    requested_model: str | None
    stored_identities: tuple[str, ...]
    resolved_identities: tuple[str, ...]
    identity_provenance: str
    identity_unresolved: bool
    routing_eligible: bool | None

    @property
    def eligibility(self) -> str:
        """The preliminary status; only Step 59 can assign positive routing eligibility."""
        return (
            PRELIMINARY_SEAL_IDENTITY_OK if self.routing_eligible is None else NOT_ROUTING_ELIGIBLE
        )


@dataclass(frozen=True, slots=True)
class RunReport:
    """One stored run's report data: manifest identity + a per-model roll-up in a stable order."""

    run_id: str
    suite: str
    suite_hash: str
    started_utc: str
    roster: list[str]
    models: list[ModelReport]
    execution: ExecutionEvidence | None

    @property
    def sealed(self) -> bool:
        """True when the run carries an execution receipt (a sealed, non-legacy run)."""
        return self.execution is not None

    @property
    def seal_status(self) -> str:
        """The run-level seal label: the sealing mode, or :data:`LEGACY_UNSEALED`."""
        return LEGACY_UNSEALED if self.execution is None else self.execution.sealing_mode

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
    """A cross-run comparison over runs sharing suite and execution hashes.

    ``runs`` are the per-run reports in the caller's order (primary first); ``models`` is the sorted
    union of every model that appears in any run — so the rendered table is deterministic regardless
    of per-run roster order.
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


def build_execution_evidence(manifest: Mapping[str, object]) -> ExecutionEvidence | None:
    """Read the manifest's additive execution receipt, or ``None`` for a legacy (unsealed) run.

    Delegates to the runner's own receipt reader (one owner of the receipt shape) and re-faces its
    ``RunError`` as :class:`ReportError`, so a corrupt receipt fails loud rather than rendering a
    report that silently omits the seal. A manifest with no receipt key is a readable legacy run
    (plan §6.4), returned as ``None`` — never an error.
    """
    try:
        receipt = runner._read_execution_receipt(manifest)
    except RunError as exc:
        raise ReportError(str(exc)) from exc
    if receipt is None:
        return None
    return ExecutionEvidence(
        profile_id=receipt.profile_id,
        sealing_mode=receipt.sealing_mode,
        execution_profile_sha256=receipt.execution_profile_sha256,
        provider_profile_sha256=receipt.provider_profile_sha256,
        context_profile_sha256=receipt.context_profile_sha256,
        receipt_sha256=receipt.receipt_sha256,
        claude_executable=None if receipt.claude_cli is None else receipt.claude_cli.executable,
        claude_version=None if receipt.claude_cli is None else receipt.claude_cli.version,
        bindings=tuple((b.alias, b.provider, b.requested_model) for b in receipt.bindings),
    )


def _model_report(
    model: str, rows: Sequence[RunRow], execution: ExecutionEvidence | None
) -> ModelReport:
    """Roll one model's rows into a :class:`ModelReport` (numeric-score mean + signal counts).

    Identity/seal evidence (plan §6.3/§6.4) is folded in additively: the receipt binding names this
    alias's provider/requested identity, and the rows' ``model_id_resolved`` values give the
    observed resolved-identity set. A requested alias is NEVER substituted for an absent concrete
    identity — an unobserved identity is recorded as :data:`UNRESOLVED_IDENTITY`. The suite score is
    computed exactly as before and is never touched by any of this.
    """
    scores = [row.score for row in rows if row.score is not None]
    binding = execution.binding_for(model) if execution is not None else None
    stored = sorted({row.model_id_resolved or UNRESOLVED_IDENTITY for row in rows})
    resolved = stored if binding is not None else []
    concrete = [value for value in resolved if value != UNRESOLVED_IDENTITY]
    identity_unresolved = (UNRESOLVED_IDENTITY in resolved) or not concrete
    provider = None if binding is None else binding[0]
    requested_model = None if binding is None else binding[1]
    preliminary = execution is not None and binding is not None and not identity_unresolved
    routing_eligible: bool | None = None if preliminary else False
    identity_provenance = (
        UNVERIFIED_LEGACY
        if execution is None
        else (
            "UNBOUND_RECEIPT_IDENTITY"
            if binding is None
            else (
                "PROVIDER_CONFIRMED" if not identity_unresolved else "PROVIDER_IDENTITY_UNRESOLVED"
            )
        )
    )
    return ModelReport(
        model=model,
        n_cells=len(rows),
        n_scored=len(scores),
        n_items=len({row.item_id for row in rows}),
        # suite_score fails loud on an empty sequence (Step-5 contract), so guard: no numeric score
        # for the model -> None (rendered "n/a"), never a fabricated 0 or a crash.
        suite_score=suite_score(scores) if scores else None,
        provider=provider,
        requested_model=requested_model,
        stored_identities=tuple(stored),
        resolved_identities=tuple(resolved),
        identity_provenance=identity_provenance,
        identity_unresolved=identity_unresolved,
        routing_eligible=routing_eligible,
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
    execution = build_execution_evidence(manifest)

    rows_by_model: dict[str, list[RunRow]] = {}
    for row in rows:
        rows_by_model.setdefault(row.model, []).append(row)

    ordered_models = list(roster)
    for model in sorted(rows_by_model):  # sorted -> deterministic; roster order preserved above
        if model not in ordered_models:
            ordered_models.append(model)

    models = [
        _model_report(model, rows_by_model.get(model, []), execution) for model in ordered_models
    ]
    return RunReport(
        run_id=_manifest_str(manifest, "run_id"),
        suite=_manifest_str(manifest, "suite"),
        suite_hash=_manifest_str(manifest, "suite_hash"),
        started_utc=_manifest_str(manifest, "started_utc"),
        roster=roster,
        models=models,
        execution=execution,
    )


def build_comparison(run_ids: Sequence[str], out_dir: str | Path = "data") -> ComparisonReport:
    """Compare runs with equal suite and execution receipts; reject every mismatch."""
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
        base_receipt = base.execution
        other_receipt = report.execution
        if (base_receipt is None) != (other_receipt is None):
            raise ReportError(
                f"cannot compare sealed and legacy-unsealed runs: {base.run_id} vs {report.run_id}"
            )
        if base_receipt is not None and other_receipt is not None:
            for field in (
                "execution_profile_sha256",
                "provider_profile_sha256",
                "context_profile_sha256",
                "receipt_sha256",
            ):
                if getattr(base_receipt, field) != getattr(other_receipt, field):
                    raise ReportError(
                        f"cannot compare runs with different {field}: "
                        f"{base.run_id} vs {report.run_id}"
                    )
    models = sorted({m.model for report in reports for m in report.models})
    return ComparisonReport(
        suite=base.suite, suite_hash=base.suite_hash, runs=reports, models=models
    )


# --- Rendering (deterministic: same run -> byte-identical markdown; no wall-clock in the output) --


def _fmt_score(score: float | None) -> str:
    """A score cell: one-decimal 0-100, or ``n/a`` when the model has no numeric score."""
    return "n/a" if score is None else f"{score:.1f}"


def _fmt_resolved(model: ModelReport) -> str:
    """Show row identity values with their provenance, escaping untrusted Markdown text."""
    values = model.resolved_identities or model.stored_identities
    return ", ".join(_md_cell(value) for value in values) if values else "(none)"


def _md_cell(value: str) -> str:
    """Escape arbitrary provider text in a Markdown table cell."""
    return (
        html_escape(value, quote=True)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", "<br>")
    )


def _execution_lines(report: RunReport) -> list[str]:
    """The execution-receipt + per-alias identity section (plan §6.3/§6.4); scores untouched."""
    lines = ["## Execution receipt & identity", ""]
    evidence = report.execution
    if evidence is None:
        lines += [
            f"- **Seal:** `{LEGACY_UNSEALED}` — this run carries no execution receipt.",
            f"- **Routing:** `{NOT_ROUTING_ELIGIBLE}` for every arm; official scores below are "
            "shown unchanged for reference only and are not comparable to sealed runs.",
            "",
        ]
    else:
        cli = (
            "(no Claude bindings)"
            if evidence.claude_executable is None
            else f"`{evidence.claude_executable}` (version `{evidence.claude_version}`)"
        )
        lines += [
            f"- **Seal:** `{evidence.sealing_mode}` — profile `{evidence.profile_id}`",
            f"- **Execution-profile hash:** `{evidence.execution_profile_sha256}`",
            f"- **Provider-profile hash:** `{evidence.provider_profile_sha256}`",
            f"- **Context-profile hash:** `{evidence.context_profile_sha256}`",
            f"- **Receipt hash:** `{evidence.receipt_sha256}`",
            f"- **Claude CLI:** {cli}",
            "- **Routing:** preliminary seal and identity evidence only; "
            "Step 59 must assess validity.",
            "",
        ]
    lines += [
        "| Model | Provider | Requested | Stored identity | Provenance | Status |",
        "|---|---|---|---|---|---|",
    ]
    for model in report.models:
        lines.append(
            f"| {_md_cell(model.model)} | {_md_cell(model.provider or '(legacy)')} | "
            f"{_md_cell(model.requested_model or '(legacy)')} | {_fmt_resolved(model)} | "
            f"{model.identity_provenance} | {model.eligibility} |"
        )
    lines.append("")
    return lines


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
        f"- **Seal:** `{report.seal_status}`",
        "",
    ]
    lines += _execution_lines(report)
    lines += [
        "## Scores",
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
        f"- **Runs compared:** {len(comparison.runs)} (equal suite and execution hashes)",
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
    for run in comparison.runs:
        lines += [f"## Run {run.run_id}", "", f"- **Seal:** `{run.seal_status}`", ""]
        lines += _execution_lines(run)
    return "\n".join(lines)


def run_report_jsonl(report: RunReport) -> str:
    """Export a :class:`RunReport` as JSONL — one canonical line per model (plan §6 JSONL export).

    A minimal, machine-readable sibling of the markdown table (same numbers, one JSON object per
    model), for piping a run's per-model roll-up into downstream tooling. Deterministic + ASCII.
    """
    evidence = report.execution
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
                # Additive execution/identity evidence (plan §6.3/§6.4). Legacy runs carry null
                # provider/requested/hashes and are marked unsealed + not routing-eligible; scores
                # above are unchanged.
                "sealed": report.sealed,
                "seal_status": report.seal_status,
                "provider": model.provider,
                "requested_model": model.requested_model,
                "stored_identities": list(model.stored_identities),
                "resolved_identities": list(model.resolved_identities),
                "identity_provenance": model.identity_provenance,
                "identity_unresolved": model.identity_unresolved,
                "routing_eligible": model.routing_eligible,
                "eligibility": model.eligibility,
                "profile_id": None if evidence is None else evidence.profile_id,
                "execution_profile_sha256": (
                    None if evidence is None else evidence.execution_profile_sha256
                ),
                "provider_profile_sha256": (
                    None if evidence is None else evidence.provider_profile_sha256
                ),
                "context_profile_sha256": (
                    None if evidence is None else evidence.context_profile_sha256
                ),
                "receipt_sha256": None if evidence is None else evidence.receipt_sha256,
                "claude_executable": None if evidence is None else evidence.claude_executable,
                "claude_version": None if evidence is None else evidence.claude_version,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        for model in report.models
    )
