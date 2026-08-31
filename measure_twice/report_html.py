"""measure-twice item-level transparency report — one self-contained HTML page per run.

``mt report <run_id>`` answers "what did each model score". This module answers the question an
operator actually has to ask before believing that number: **which items ran, what did each model
literally reply, and why did the scorer decide what it decided.** It renders every cell of a stored
run — prompt, verbatim response, parsed verdict, score, and the scorer's reason — into a single
offline HTML file with no network dependency and no third-party runtime.

ONE SOURCE OF TRUTH FOR THE TAXONOMY (``code-quality.md`` § one source of truth). The page splits
zero-scored cells into *wrong answer* / *scorer refused* / *no verdict*, which is the whole point of
the report — a headline mean fuses them. That split is derived by routing through the PUBLIC scorer
surface (:func:`~measure_twice.scoring.deterministic.score_verdict`,
:func:`~measure_twice.scoring.deterministic.extract_verdict_label`,
:data:`~measure_twice.scoring.deterministic.PARSE_FAIL_MARKER`), never by re-implementing the
recognition rules here. A second copy of "what counts as a label" would drift from the scorer and
turn this report into a quiet second contract.

FAIL LOUD ON IRREPRODUCIBILITY. Every verdict-scored cell is re-scored from its stored
``response_raw`` while the report is built, and the result must equal the stored
``(parsed, score)`` pair.
A single mismatch raises :class:`~measure_twice.report.ReportError` and NO page is written — a
transparency report that silently disagreed with the run store would be worse than none. This is the
offline re-scoring guarantee (``mt score`` re-runnability) used as an integrity gate.

THE DIAGNOSTIC IS NOT A SCORE. The page also reports what each model would have scored under a
deliberately different rule (first recognizable label wins), to size how much zero-scoring is answer
FORMATTING rather than judgment. Choosing a parser after seeing the data is precisely how a
benchmark flatters itself (``measurement-validity.md``), so it is rendered inside a quarantined
panel that says so, and :func:`_first_recognized_label` still delegates every match to the public
extractor rather than re-deriving the label-matching rule.

SCOPE. Verdict suites only — the taxonomy is verdict-specific — and a non-verdict run fails loud
rather than rendering a meaningless page. This module deliberately does NOT emit an observatory
JSON envelope (that contract is owned by the operations-surfaces plan) and does NOT compute an
empirical per-item difficulty (owned by the calibration step); it displays only the suite's own
authored tags and priors. Rendering is deterministic: no render timestamp, so re-rendering a run
yields a byte-identical page, matching ``report.render_run_report``'s contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from measure_twice.report import ReportError, _open_run_store, build_run_report
from measure_twice.runner import NO_RESPONSE_SCORER, RunError, RunRow, load_run_suite
from measure_twice.scoring.deterministic import (
    PARSE_FAIL_MARKER,
    VERDICT_SCORER,
    extract_verdict_label,
    score_verdict,
)
from measure_twice.suite import Item, Suite

__all__ = [
    "OUTCOME_AMBIGUOUS",
    "OUTCOME_CORRECT",
    "OUTCOME_ERROR",
    "OUTCOME_NO_RESPONSE",
    "OUTCOME_NO_VERDICT",
    "OUTCOME_REFUSED",
    "OUTCOME_WRONG",
    "CellReport",
    "ItemReport",
    "TransparencyReport",
    "build_transparency_report",
    "render_transparency_report",
]

# The cell outcome taxonomy. The three ZERO-scoring outcomes are distinct measurement events and are
# never collapsed: WRONG is a judging miss, REFUSED is the scorer declining to guess between two
# recognized labels, NO_VERDICT is the model never offering one.
OUTCOME_CORRECT = "CORRECT"
OUTCOME_WRONG = "WRONG"
OUTCOME_REFUSED = "REFUSED"
OUTCOME_NO_VERDICT = "NO_VERDICT"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_NO_RESPONSE = "NO_RESPONSE"
OUTCOME_ERROR = "ERROR"

_TEMPLATE_NAME = "report_template.html"


@dataclass(frozen=True, slots=True)
class CellReport:
    """One (item, model) cell: what was returned, what the scorer made of it, and why."""

    model: str
    raw: str
    parsed: str | None
    score: float | None
    elapsed_s: float | None
    error: str | None
    outcome: str
    why: str
    labels_present: tuple[str, ...]
    diagnostic_recoverable: bool


@dataclass(frozen=True, slots=True)
class ItemReport:
    """One suite item plus every model's cell for it, in manifest roster order."""

    item_id: str
    prompt: str
    expected: str
    prior: float | None
    provenance: str
    facets: dict[str, str]
    cells: dict[str, CellReport | None]


@dataclass(frozen=True, slots=True)
class TransparencyReport:
    """A whole run at item level: identity, the per-model roll-up, and every cell."""

    run_id: str
    suite: str
    suite_hash: str
    started_utc: str
    models: list[str]
    labels: tuple[str, ...]
    floor_label: str
    floor_score: float
    preregistration: str | None
    manifest: Mapping[str, object]
    items: list[ItemReport]
    reproduced_cells: int


# --- Label recognition (public scorer surface only — never a second matcher) ---------------


def _labels_recognized(text: str, labels: Sequence[str]) -> tuple[str, ...]:
    """Which labels the PRODUCTION extractor recognizes in ``text``, probed one label at a time.

    Probing ``extract_verdict_label(text, (label,))`` asks the real recognizer "is this one label
    present", with no competing label to trigger its conflict rule — so a per-label probe recovers
    the presence set without this module owning a copy of the matching rule (whole-word bounds,
    JSON/labeled-line/prose tiers). Order follows ``labels`` for a deterministic render.
    """
    return tuple(label for label in labels if extract_verdict_label(text, (label,)) is not None)


def _first_recognized_label(text: str, labels: Sequence[str]) -> str | None:
    """DIAGNOSTIC ONLY — the earliest-appearing recognized label, or ``None`` if there is none.

    NOT a scoring rule and never used to score: it exists solely to size how much zero-scoring is
    answer formatting rather than judgment (see the module docstring's quarantine note).

    Matching still delegates entirely to :func:`extract_verdict_label`. For each label we binary
    search the shortest PREFIX in which the real extractor recognizes it — the predicate is monotone
    because a label present in a prefix is present in every longer one. Candidate cut points are
    restricted to non-alphanumeric boundaries so a prefix can never split a word and manufacture a
    match (cutting ``"password"`` at 4 would otherwise read as ``"pass"``).
    """
    cuts = [i for i, ch in enumerate(text) if not ch.isalnum()]
    cuts.append(len(text))
    best: tuple[int, str] | None = None
    for label in labels:
        if extract_verdict_label(text, (label,)) is None:
            continue
        low, high = 0, len(cuts) - 1  # cuts[high] == len(text) always recognizes
        while low < high:
            mid = (low + high) // 2
            if extract_verdict_label(text[: cuts[mid]], (label,)) is not None:
                high = mid
            else:
                low = mid + 1
        if best is None or cuts[low] < best[0]:
            best = (cuts[low], label)
    return None if best is None else best[1]


# --- Cell classification ------------------------------------------------------------------


def _classify(row: RunRow, item: Item, labels: Sequence[str]) -> CellReport:
    """Classify one stored row, re-scoring it to prove the report agrees with the run store.

    A verdict-scored row is re-run through :func:`score_verdict`; any disagreement with the stored
    ``(parsed, score)`` is a fail-loud :class:`ReportError` rather than a quietly re-labelled cell.
    """
    raw = row.response_raw
    if row.error is not None:
        return CellReport(
            model=row.model,
            raw=raw,
            parsed=row.parsed,
            score=row.score,
            elapsed_s=row.elapsed_s,
            error=row.error,
            outcome=OUTCOME_ERROR,
            why=f"The model call failed ({row.error}); the cell was never scored.",
            labels_present=(),
            diagnostic_recoverable=False,
        )
    if row.scorer == NO_RESPONSE_SCORER:
        return CellReport(
            model=row.model,
            raw=raw,
            parsed=row.parsed,
            score=row.score,
            elapsed_s=row.elapsed_s,
            error=None,
            outcome=OUTCOME_NO_RESPONSE,
            why="The model returned no text at all; the runner force-scored the cell 0 before "
            "any scorer ran.",
            labels_present=(),
            diagnostic_recoverable=False,
        )

    outcome_recheck = score_verdict(item, raw, labels=labels)
    if (outcome_recheck.parsed, outcome_recheck.score) != (row.parsed, row.score):
        raise ReportError(
            f"cell {row.item_id}/{row.model} does not reproduce: stored "
            f"(parsed={row.parsed!r}, score={row.score!r}) but re-scoring the stored response "
            f"gives (parsed={outcome_recheck.parsed!r}, score={outcome_recheck.score!r}). "
            "The run store and the scorer disagree; no report was written."
        )

    present = _labels_recognized(raw, labels)
    first = _first_recognized_label(raw, labels)
    recoverable = False
    if row.parsed == PARSE_FAIL_MARKER:
        recoverable = first is not None and first.casefold() == item.expected.casefold()
        if len(present) >= 2:
            outcome = OUTCOME_REFUSED
            why = (
                f"The response contains more than one recognized label ({', '.join(present)}). "
                "Two disagreeing signals resolve as a conflict and the scorer refuses to guess, "
                "so the cell is a recorded parse-fail scored 0 — not a wrong answer."
            )
        elif not present:
            outcome = OUTCOME_NO_VERDICT
            why = (
                "The response contains no recognized label at all — the model never returned a "
                "verdict. Recorded as a parse-fail and scored 0."
            )
        else:
            outcome = OUTCOME_AMBIGUOUS
            why = (
                f"Exactly one label ({present[0]}) is recognizable in the text, yet the extractor "
                "still returned no verdict: a structured tier (JSON or a labelled line) carried a "
                "conflict that outranked the prose tier. Recorded as a parse-fail and scored 0."
            )
    elif row.score == 1.0:
        outcome = OUTCOME_CORRECT
        why = f"Parsed verdict {row.parsed!r} equals the expected {item.expected!r}. Scored 1."
    else:
        outcome = OUTCOME_WRONG
        why = (
            f"Parsed verdict {row.parsed!r} but the item expected {item.expected!r} — a genuine "
            "judging miss, not a formatting problem. Scored 0."
        )
    return CellReport(
        model=row.model,
        raw=raw,
        parsed=row.parsed,
        score=row.score,
        elapsed_s=row.elapsed_s,
        error=None,
        outcome=outcome,
        why=why,
        labels_present=present,
        diagnostic_recoverable=recoverable,
    )


# --- Facets (suite-authored tag groupings, auto-discovered) --------------------------------


def _derive_facets(items: Sequence[Item]) -> list[tuple[str, list[str]]]:
    """Discover ``prefix-value`` tag families that actually partition the suite.

    A tag family is kept only when it carries at least two distinct values across the suite, so
    constant provenance/instrument stamps are skipped while genuine axes (a difficulty ladder, a
    review lens) surface automatically — no suite-specific tag names are hard-coded here.

    Values are ordered by the mean AUTHORED ``difficulty_prior`` of the items carrying them, so a
    difficulty ladder renders easy-to-hard instead of alphabetically. That is the suite's own
    declared prior, never a difficulty re-estimated from the run (which the calibration step owns).
    """
    families: dict[str, dict[str, list[float]]] = {}
    for item in items:
        for tag in item.tags:
            prefix, sep, value = tag.partition("-")
            if not sep or not value:
                continue
            families.setdefault(prefix, {}).setdefault(value, []).append(item.difficulty_prior)
    facets: list[tuple[str, list[str]]] = []
    for prefix in sorted(families):
        values = families[prefix]
        if len(values) < 2:
            continue
        ordered = sorted(values, key=lambda v: (sum(values[v]) / len(values[v]), v))
        facets.append((prefix, ordered))
    return facets


# --- Builder --------------------------------------------------------------------------------


def _suite_labels(suite: Suite) -> tuple[str, ...]:
    """The verdict labels for a verdict suite, else fail loud (this report is verdict-only)."""
    if suite.scoring.type != "verdict" or not suite.scoring.labels:
        raise ReportError(
            f"item-level report supports verdict suites only; run's suite {suite.suite!r} uses "
            f"scoring type {suite.scoring.type!r}"
        )
    return tuple(suite.scoring.labels)


def build_transparency_report(run_id: str, out_dir: str | Path = "data") -> TransparencyReport:
    """Open a stored run and build its item-level report, failing loud on any irreproducible cell.

    Reuses the run-store readers (``report._open_run_store``) and the run's own suite snapshot
    (``runner.load_run_suite``) so the run-store layout and the traversal guard keep ONE owner.
    """
    manifest, rows = _open_run_store(run_id, out_dir)
    try:
        suite = load_run_suite(run_id, out_dir)
    except RunError as exc:
        raise ReportError(str(exc)) from exc

    labels = _suite_labels(suite)
    roll_up = build_run_report(run_id, out_dir)
    models = [model.model for model in roll_up.models]

    rows_by_cell: dict[tuple[str, str], RunRow] = {}
    for row in rows:
        rows_by_cell.setdefault((row.item_id, row.model), row)
    if not any(row.scorer == VERDICT_SCORER for row in rows):
        raise ReportError(
            f"run {run_id} has no verdict-scored cells to report on — score it first "
            f"(`mt score {run_id}`), or every cell errored"
        )

    facets = _derive_facets(suite.items)
    items: list[ItemReport] = []
    reproduced = 0
    for item in suite.items:
        cells: dict[str, CellReport | None] = {}
        for model in models:
            cell_row = rows_by_cell.get((item.id, model))
            if cell_row is None:
                cells[model] = None
                continue
            cells[model] = _classify(cell_row, item, labels)
            if cell_row.scorer == VERDICT_SCORER:
                reproduced += 1
        item_facets = {
            prefix: value
            for prefix, values in facets
            for value in values
            if f"{prefix}-{value}" in item.tags
        }
        items.append(
            ItemReport(
                item_id=item.id,
                prompt=item.prompt,
                expected=item.expected,
                prior=item.difficulty_prior,
                provenance=item.provenance,
                facets=item_facets,
                cells=cells,
            )
        )

    expected_counts: dict[str, int] = {}
    for item in suite.items:
        expected_counts[item.expected] = expected_counts.get(item.expected, 0) + 1
    floor_label, floor_n = max(expected_counts.items(), key=lambda kv: (kv[1], kv[0]))
    prereg = manifest.get("preregistration")

    return TransparencyReport(
        run_id=roll_up.run_id,
        suite=roll_up.suite,
        suite_hash=roll_up.suite_hash,
        started_utc=roll_up.started_utc,
        models=models,
        labels=labels,
        floor_label=floor_label,
        floor_score=100.0 * floor_n / len(suite.items),
        preregistration=prereg if isinstance(prereg, str) and prereg else None,
        manifest=manifest,
        items=items,
        reproduced_cells=reproduced,
    )


# --- Renderer -------------------------------------------------------------------------------


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    return None if not ordered else ordered[len(ordered) // 2]


def _model_totals(report: TransparencyReport, model: str) -> dict[str, object]:
    """Per-model roll-up computed from the SAME cells the page renders (no second source)."""
    cells = [item.cells[model] for item in report.items if item.cells.get(model) is not None]
    graded = [c for c in cells if c is not None]
    scores = [c.score for c in graded if c.score is not None]
    outcomes: dict[str, int] = {}
    for cell in graded:
        outcomes[cell.outcome] = outcomes.get(cell.outcome, 0) + 1
    answered = [c for c in graded if c.outcome in (OUTCOME_CORRECT, OUTCOME_WRONG)]
    diagnostic_hits = sum(
        1
        for c in graded
        if c.outcome == OUTCOME_CORRECT or (c.score == 0.0 and c.diagnostic_recoverable)
    )
    unreadable = sum(
        1 for c in graded if c.outcome in (OUTCOME_NO_VERDICT, OUTCOME_NO_RESPONSE, OUTCOME_ERROR)
    )
    elapsed = [c.elapsed_s for c in graded if c.elapsed_s is not None]
    n = len(graded)
    return {
        "n": n,
        "score": (100.0 * sum(scores) / len(scores)) if scores else None,
        "correct": outcomes.get(OUTCOME_CORRECT, 0),
        "answered": len(answered),
        "answered_correct": sum(1 for c in answered if c.outcome == OUTCOME_CORRECT),
        "median_s": _median(elapsed),
        "outcomes": outcomes,
        "diagnostic_score": (100.0 * diagnostic_hits / n) if n else 0.0,
        "diagnostic_unreadable": unreadable,
    }


def _facet_payload(report: TransparencyReport) -> list[dict[str, object]]:
    names: list[str] = []
    for item in report.items:
        for name in item.facets:
            if name not in names:
                names.append(name)
    payload: list[dict[str, object]] = []
    for name in names:
        values: list[str] = []
        for item in report.items:
            value = item.facets.get(name)
            if value is not None and value not in values:
                values.append(value)
        rows: list[dict[str, object]] = []
        for value in values:
            members = [i for i in report.items if i.facets.get(name) == value]
            scores: dict[str, float | None] = {}
            for model in report.models:
                cell_scores = [
                    c.score
                    for c in (i.cells.get(model) for i in members)
                    if c is not None and c.score is not None
                ]
                scores[model] = 100.0 * sum(cell_scores) / len(cell_scores) if cell_scores else None
            rows.append({"value": value, "n": len(members), "scores": scores})
        payload.append({"name": name, "values": rows})
    return payload


def _provenance_rows(report: TransparencyReport) -> list[list[str]]:
    manifest = report.manifest
    budgets = manifest.get("budgets")
    budget = ""
    if isinstance(budgets, Mapping):
        budget = str(budgets.get("max_calls", ""))
    cells = sum(1 for item in report.items for m in report.models if item.cells.get(m))
    rows = [
        ["Run id", report.run_id],
        ["Suite", report.suite],
        ["Suite hash", report.suite_hash],
        ["Roster", ", ".join(report.models)],
        ["Items x models", f"{len(report.items)} x {len(report.models)} = {cells} cells"],
        ["Samples per cell", str(manifest.get("samples_per_cell", ""))],
        ["Call budget", f"{cells} / {budget} used" if budget else str(cells)],
        ["Started (UTC)", report.started_utc],
        ["Config source", str(manifest.get("config_source", ""))],
        ["Scoring", f"verdict · labels {'|'.join(report.labels)} · deterministic"],
        ["Degenerate floor", f'{report.floor_score:.1f} (always "{report.floor_label}")'],
        ["Reproduced cells", f"{report.reproduced_cells} re-scored, all matching the store"],
    ]
    return [[key, value] for key, value in rows]


def _limits(report: TransparencyReport) -> list[str]:
    manifest = report.manifest
    samples = manifest.get("samples_per_cell")
    limits: list[str] = []
    if samples == 1:
        limits.append(
            "<strong>One sample per cell.</strong> There is no variance estimate, so a score near "
            "the degenerate floor is a single draw rather than a distribution &mdash; the gap "
            "between them is not established."
        )
    limits.append(
        "<strong>One run.</strong> Nothing here speaks to run-to-run stability; no repetition "
        "was performed."
    )
    limits.append(
        f"<strong>{len(report.models)} model(s) on one instrument.</strong> Results do not "
        "transfer to another suite, provider, or roster."
    )
    if manifest.get("config_source") == "defaults":
        limits.append(
            "<strong>Built-in defaults.</strong> No operator config file was resolved for this "
            "run; the value is recorded in the manifest rather than silently assumed."
        )
    if report.preregistration is None:
        limits.append(
            "<strong>No preregistered claim.</strong> Nothing was committed before the results "
            "were seen, so any conclusion drawn here is post hoc."
        )
    limits.append(
        "<strong>The first-label-wins figure is a diagnostic, not a score.</strong> It must never "
        "be quoted as a result."
    )
    return limits


def _payload(report: TransparencyReport) -> dict[str, object]:
    return {
        "title": f"{report.suite} — {report.run_id}",
        "run_id": report.run_id,
        "models": report.models,
        "labels": list(report.labels),
        "floor": {"label": report.floor_label, "score": report.floor_score},
        "preregistration": report.preregistration,
        "provenance": _provenance_rows(report),
        "repro": (
            f"# re-render this page from the stored run (no model is called)\n"
            f"uv run mt report {report.run_id} --html\n\n"
            f"# re-score the stored raw responses offline, then re-read the summary\n"
            f"uv run mt score {report.run_id}\n"
            f"uv run mt report {report.run_id}"
        ),
        "totals": {model: _model_totals(report, model) for model in report.models},
        "facets": _facet_payload(report),
        "integrity": {"cells": report.reproduced_cells},
        "limits": _limits(report),
        "items": [
            {
                "id": item.item_id,
                "prompt": item.prompt,
                "expected": item.expected,
                "prior": item.prior,
                "provenance": item.provenance,
                "facets": item.facets,
                "cells": {
                    model: (
                        None
                        if cell is None
                        else {
                            "raw": cell.raw,
                            "parsed": cell.parsed,
                            "score": cell.score,
                            "elapsed_s": cell.elapsed_s,
                            "outcome": cell.outcome,
                            "why": cell.why,
                            "labels_present": list(cell.labels_present),
                            "diagnostic_recoverable": cell.diagnostic_recoverable,
                        }
                    )
                    for model, cell in item.cells.items()
                },
            }
            for item in report.items
        ],
    }


def _load_template() -> str:
    """Read the page shell shipped beside this module (packaged as wheel data)."""
    path = Path(__file__).with_name(_TEMPLATE_NAME)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - a broken install, not a run-store fault
        raise ReportError(f"report template missing or unreadable: {path}") from exc


def render_transparency_report(report: TransparencyReport) -> str:
    """Render a :class:`TransparencyReport` to one self-contained, deterministic HTML page.

    Model output is UNTRUSTED text. It is carried into the page only inside a JSON island whose
    ``<`` characters are escaped, so a response containing ``</script>`` or a tag cannot terminate
    the island or inject markup; the page itself writes every value through ``textContent``. The
    output embeds no render timestamp, so re-rendering the same run is byte-identical.
    """
    blob = json.dumps(_payload(report), separators=(",", ":"), sort_keys=False)
    blob = blob.replace("<", "\\u003c")
    title = f"{report.suite} — {report.run_id}"
    page = _load_template().replace("__TITLE__", title).replace("__DATA__", blob)
    return page if page.endswith("\n") else page + "\n"
