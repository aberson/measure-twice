"""measure-twice sweep engine — the runner (plan §3 Run store, §4 no-response, §5 runner).

Sweeps ``suite x roster x samples`` through the DI-seamed adapters, writing a per-run
``manifest.json`` at start and appending one ``rows.jsonl`` line per (model x item x sample) as it
is produced. Cell-level resume, shared call budgets, torn-line tolerance, and the no-response
force-0 invariant all live here. The core is stdlib-only (``json``/``os``/``datetime``/``pathlib``/
``concurrent.futures`` via the claude pool); switchboard is reached only through the adapters.

The SCORER DI seam (the key to building the runner before scoring exists in Steps 5/6)
-----------------------------------------------------------------------------------------
The runner sweeps and COLLECTS raw responses; the actual verdict/exact/rubric scoring is Step 5/6.
So scoring is an INJECTED callable — a DI seam exactly like the adapters' client-factory seams:

    Scorer = Callable[[Item, str], ScoreOutcome]      # (item, raw response) -> parsed/score/scorer

Step 4 ships the DEFAULT :func:`collect_only_scorer`, which leaves ``score=None`` / ``scorer=None``
for a real response — real scoring is deferred to ``mt score`` once Step 5 lands. Steps 5/6 replace
the default with the real deterministic / judge scorers at the same seam (one-line swap; see the
``# SCORER SEAM`` markers in this module and in ``cli.py``). Tests inject a stub scorer to exercise
the scored path.

The no-response FORCE-0 (plan §4, port of void_furnace's ``no_diff``) is done by the RUNNER itself,
**before any scorer is consulted** — it is a pre-scoring invariant, not a scorer: a model that
produced nothing (the adapter's :data:`~measure_twice.adapters.base.NO_RESPONSE` state) is written
with ``score=0.0`` / ``scorer="no_response"`` and is NEVER passed to a judge
(``measurement-validity.md`` § score the production artifact).

Resume completeness rule (plan §3)
----------------------------------
The runner appends a row ONLY after a model call has produced a terminal outcome — success (raw
captured), no-response (force-0), or error (terminal ``error``). There is no row shape that means
"not yet called". Therefore **the mere existence of a row for a cell proves the model was already
called for it**, and resume skips exactly the cells that have a row. A ``null`` ``score`` does NOT
force a re-call: resume exists to avoid re-CALLING models (subscription/GPU cost), while scoring is
cheap and re-runnable offline (``mt score``, Decision 10) — so an un-scored captured response is
still terminal for resume purposes. A torn/truncated trailing line (crash mid-append) is detected
(JSON parse failure on the LAST line only) and dropped, never fatal.

Fail-loud (plan §8 D9): a missing run dir / manifest / suite snapshot, a resume against a different
instrument (suite-hash mismatch), or mid-file rows corruption (a parse failure on a non-trailing
line) all raise :class:`RunError` rather than silently degrading. Live-endpoint reachability aborts
are a live-run / ``mt smoke`` concern (Step 7); the sweep engine is instrument-neutral and fully
offline-testable through the injected factories.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Final, cast

from measure_twice.adapters.base import ModelCallResult
from measure_twice.adapters.claude_cli import (
    BudgetExhaustedError,
    CallBudget,
    ClaudeRequest,
    RunnerFactory,
    claude_call_batch,
)
from measure_twice.adapters.local import TransportFactory, local_chat
from measure_twice.config import ConfigError, RunConfig, _validate_name_list
from measure_twice.suite import Item, Suite, SuiteError

__all__ = [
    "CLAUDE_ALIASES",
    "NO_RESPONSE_SCORER",
    "RunError",
    "RunResult",
    "RunRow",
    "ScoreOutcome",
    "ScoreResult",
    "Scorer",
    "collect_only_scorer",
    "load_run_suite",
    "run",
    "score_run",
]

# Which roster names route to the claude adapter; everything else is treated as a local model
# (the "simple mapping" of plan §5). A model not in this set goes to the local endpoint. Kept in
# sync with the config default roster's claude tiers (haiku/sonnet/opus) plus the one-off ``fable``.
CLAUDE_ALIASES: Final[frozenset[str]] = frozenset({"haiku", "sonnet", "opus", "fable"})

# The reserved scorer name a no-response cell carries. It is set ONLY by the runner's force-0 branch
# (never by an injected scorer), so ``mt score`` re-identifies a force-0 row by this name and leaves
# it at 0 without re-consulting a scorer.
NO_RESPONSE_SCORER: Final[str] = "no_response"

# The exact minted-run-id shape. Any INCOMING run_id (the ``--resume`` value, the ``mt score``
# positional) is untrusted and validated against this BEFORE being joined to a path, so a ``..``
# component can never traverse out of ``<out>/runs/`` (see :func:`_resolve_run_dir`). Anchored with
# ``\A ... \Z`` (NOT ``^ ... $``): ``$`` also matches just before a trailing newline, the classic
# whole-string-allowlist pitfall — ``\Z`` admits no trailing-newline slack.
_RUN_ID_RE: Final[re.Pattern[str]] = re.compile(r"\Arun_\d{8}T\d{6}Z_[0-9a-f]{6}\Z")


class RunError(ValueError):
    """Raised on a run-store / resume fault (missing dir, corrupt rows, instrument mismatch, an
    invalid run_id / budget, or an unexpected sweep failure).

    Fail-loud sentinel subclassing ``ValueError`` — the package convention shared by
    ``config.ConfigError`` / ``suite.SuiteError`` / ``adapters.base.AdapterError`` — so a single
    ``except (ConfigError, SuiteError, RunError)`` chain catches them all. A run that cannot be
    honestly opened, resumed, or re-scored aborts rather than producing numbers indistinguishable
    from a real run.
    """


# --- The scorer DI seam ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScoreOutcome:
    """What a :data:`Scorer` returns for one real response: the parsed value, the numeric score,
    and the scorer's name. All three are ``None`` for the Step-4 collect-only default (scoring
    deferred). A real scorer (Step 5/6) fills them; it must NEVER return ``scorer="no_response"``
    (that name is reserved for the runner's pre-scoring force-0 branch)."""

    parsed: str | None
    score: float | None
    scorer: str | None


# (item, raw response text) -> ScoreOutcome. The runner calls this ONLY for a real (scoreable)
# response — never for a no-response (force-0 by the runner) or an error (no text to score).
Scorer = Callable[[Item, str], ScoreOutcome]


def collect_only_scorer(item: Item, response_raw: str) -> ScoreOutcome:
    """The Step-4 default scorer: capture the raw response, DEFER scoring to ``mt score`` (Step 5).

    Leaves ``parsed``/``score``/``scorer`` all ``None`` — the raw text is stored on the row and can
    be (re)scored offline any number of times without re-calling the model (Decision 10). Step 5
    replaces this at the ``# SCORER SEAM`` call sites with the real deterministic scorer.
    """
    return ScoreOutcome(parsed=None, score=None, scorer=None)


# --- Row + result records ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRow:
    """One ``rows.jsonl`` record — the plan §3 row shape, fields in canonical order.

    Exactly one of three terminal shapes (all produced only AFTER a model call):

    * **success (captured)** — ``response_raw`` real text; ``error=None``; ``score``/``scorer`` come
      from the injected scorer (``None`` under the Step-4 collect-only default).
    * **no-response (force-0)** — ``response_raw=""``, ``score=0.0``, ``scorer="no_response"``,
      ``error=None`` (set by the runner BEFORE any scorer).
    * **error** — ``error`` is a switchboard ``reason_class``; ``score``/``scorer``/``parsed`` are
      ``None`` and ``response_raw=""``.
    """

    run_id: str
    model: str
    model_id_resolved: str
    item_id: str
    sample_k: int
    response_raw: str
    parsed: str | None
    score: float | None
    scorer: str | None
    judge_scores: list[float] | None
    elapsed_s: float
    error: str | None

    @property
    def cell_key(self) -> tuple[str, str, int]:
        """The (model, item_id, sample_k) identity used for resume dedup."""
        return (self.model, self.item_id, self.sample_k)

    def to_json_line(self) -> str:
        """Canonical single-line JSON (ASCII-escaped, so a torn tail is always re-detectable)."""
        return json.dumps(asdict(self), ensure_ascii=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RunRow:
        """Reconstruct a row from a parsed JSON object; a missing/mistyped key raises (caught by
        the reader, which tolerates it ONLY on a torn trailing line)."""
        return cls(
            run_id=cast("str", data["run_id"]),
            model=cast("str", data["model"]),
            model_id_resolved=cast("str", data["model_id_resolved"]),
            item_id=cast("str", data["item_id"]),
            sample_k=cast("int", data["sample_k"]),
            response_raw=cast("str", data["response_raw"]),
            parsed=cast("str | None", data["parsed"]),
            score=cast("float | None", data["score"]),
            scorer=cast("str | None", data["scorer"]),
            judge_scores=cast("list[float] | None", data["judge_scores"]),
            elapsed_s=cast("float", data["elapsed_s"]),
            error=cast("str | None", data["error"]),
        )


@dataclass(frozen=True, slots=True)
class RunResult:
    """Summary of one :func:`run` invocation (the CLI renders a one-liner from it)."""

    run_id: str
    run_dir: Path
    cells_total: int
    cells_completed: int
    cells_this_run: int
    budget_used: int
    budget_max: int
    aborted: bool


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Summary of one :func:`score_run` invocation (``N rows, M scored, K no-response``)."""

    total: int
    scored: int
    no_response: int


@dataclass(frozen=True, slots=True)
class _Cell:
    """One sweep cell: a (model, item, sample_k) to call."""

    model: str
    item: Item
    sample_k: int


# --- Run-id / timestamp mints ------------------------------------------------------------


def _mint_run_id() -> str:
    """``run_<YYYYMMDDTHHMMSSZ>_<6hex>`` (UTC stamp + ``os.urandom`` hex) — plan §3."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{os.urandom(3).hex()}"


def _utc_now_iso() -> str:
    """The manifest ``started_utc`` stamp: ISO 8601 UTC with a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Manifest + suite-snapshot I/O -------------------------------------------------------


def _write_manifest(
    run_dir: Path,
    *,
    run_id: str,
    suite: Suite,
    roster: Sequence[str],
    samples: int,
    judges: Sequence[str],
    config: RunConfig,
    max_calls: int,
    preregister: str | None,
) -> None:
    """Write ``manifest.json`` with EXACTLY the plan §3 keys (written once, at run start)."""
    manifest: dict[str, object] = {
        "run_id": run_id,
        "suite": suite.suite,
        "suite_hash": suite.item_hash,
        "roster": list(roster),
        "samples_per_cell": samples,
        "judges": list(judges),
        "started_utc": _utc_now_iso(),
        "config_source": config.config_source,
        "budgets": {"max_calls": max_calls},
        "preregistration": preregister,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True), encoding="utf-8"
    )


def _read_manifest(run_dir: Path) -> Mapping[str, object]:
    """Load + minimally validate a run's ``manifest.json`` (fail loud on absence / bad JSON)."""
    path = run_dir / "manifest.json"
    if not path.is_file():
        raise RunError(f"manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunError(f"could not read manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunError(f"manifest {path} must be a JSON object, got {type(data).__name__}")
    return cast("Mapping[str, object]", data)


def _write_suite_snapshot(run_dir: Path, suite: Suite) -> None:
    """Snapshot the exact instrument this run measured (enables offline re-scoring — Decision 10).

    The run store persists raw responses (for re-scoring) AND the suite (for the gold ``expected``
    the Step-5 scorer needs), so ``mt score`` never re-reads an external suite path that may have
    drifted. The snapshot round-trips through ``Suite.from_mapping``, so its item-hash equals the
    manifest ``suite_hash`` (checked on read).
    """
    (run_dir / "suite.json").write_text(
        json.dumps(asdict(suite), indent=2, ensure_ascii=True), encoding="utf-8"
    )


def _read_suite_snapshot(run_dir: Path) -> Suite:
    """Reload the per-run suite snapshot as a validated :class:`Suite` (fail loud if bad/absent)."""
    path = run_dir / "suite.json"
    if not path.is_file():
        raise RunError(f"suite snapshot not found: {path} (run store incomplete)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunError(f"could not read suite snapshot {path}: {exc}") from exc
    try:
        return Suite.from_mapping(data)
    except SuiteError as exc:
        raise RunError(f"corrupt suite snapshot {path}: {exc}") from exc


# --- rows.jsonl I/O (append-as-produced + torn-line-tolerant read) -----------------------


def _read_rows(run_dir: Path) -> tuple[list[RunRow], bool]:
    """Read ``rows.jsonl`` defensively; return ``(rows, torn)``.

    A crash mid-append can leave a truncated final line. That is tolerated ONLY when it is the last
    line AND the file does not end in a newline (a clean append always terminates with ``\\n``): the
    torn line is dropped and ``torn=True`` is returned so the caller rewrites the file before
    appending. A parse failure on ANY earlier line is real corruption and raises :class:`RunError`.
    """
    path = run_dir / "rows.jsonl"
    if not path.is_file():
        return [], False
    text = path.read_text(encoding="utf-8")
    if text == "":
        return [], False
    raw_lines = text.split("\n")
    # A clean file ends in "\n" -> a trailing empty element that is the newline terminator, not a
    # torn line. Its presence proves the final real line was fully written.
    ends_with_newline = raw_lines[-1] == ""
    if ends_with_newline:
        raw_lines = raw_lines[:-1]

    rows: list[RunRow] = []
    torn = False
    last_index = len(raw_lines) - 1
    for i, line in enumerate(raw_lines):
        is_torn_candidate = i == last_index and not ends_with_newline
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise RunError(f"rows.jsonl line {i + 1} is not a JSON object")
            rows.append(RunRow.from_dict(cast("Mapping[str, object]", parsed)))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            if is_torn_candidate:
                torn = True  # a truncated trailing write — drop it, not fatal.
                break
            raise RunError(
                f"corrupt rows.jsonl line {i + 1} (not a torn trailing line): {exc}"
            ) from exc
    return rows, torn


def _rewrite_rows(run_dir: Path, rows: Sequence[RunRow]) -> None:
    """Atomically rewrite ``rows.jsonl`` (used to drop a torn tail on resume, and to persist
    re-scored rows in ``mt score``). Writes a temp file, fsyncs, then ``os.replace`` (atomic)."""
    path = run_dir / "rows.jsonl"
    tmp = run_dir / "rows.jsonl.tmp"
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(row.to_json_line() + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _append_row(fh: IO[str], row: RunRow) -> None:
    """Append one row and ``flush()`` immediately (plan §3 append-as-produced).

    ``flush()`` pushes the line to the OS so a PROCESS crash / interrupt (the ``--resume`` +
    torn-line scenario) keeps every prior row; it is NOT ``fsync``, so an OS crash / power loss
    could still lose the last un-synced lines. Flush-only is the deliberate throughput trade-off
    for a per-row write; the atomic ``_rewrite_rows`` path (resume rewrite, score) does fsync.
    """
    fh.write(row.to_json_line() + "\n")
    fh.flush()


# --- Row construction from an adapter result ---------------------------------------------


def _build_row(run_id: str, cell: _Cell, result: ModelCallResult, scorer: Scorer) -> RunRow:
    """Turn one adapter :class:`ModelCallResult` into a terminal row.

    Precedence (all decided WITHOUT re-inspecting the text): error first, then the no-response
    FORCE-0 (a pre-scoring invariant — the scorer is never consulted), then a real response scored
    via the injected ``scorer``.
    """
    if result.is_error:
        return RunRow(
            run_id=run_id,
            model=cell.model,
            model_id_resolved=result.resolved_model,
            item_id=cell.item.id,
            sample_k=cell.sample_k,
            response_raw="",
            parsed=None,
            score=None,
            scorer=None,
            judge_scores=None,
            elapsed_s=result.elapsed_s,
            error=result.reason_class,
        )
    if result.no_response:
        # NO-RESPONSE FORCE-0 (plan §4): the runner scores 0 BEFORE any scorer — a model that
        # produced nothing is never judged. ``response_raw`` is stored as "" (the sentinel state is
        # recorded by scorer="no_response" + score=0.0, not by echoing the noncharacter marker).
        return RunRow(
            run_id=run_id,
            model=cell.model,
            model_id_resolved=result.resolved_model,
            item_id=cell.item.id,
            sample_k=cell.sample_k,
            response_raw="",
            parsed=None,
            score=0.0,
            scorer=NO_RESPONSE_SCORER,
            judge_scores=None,
            elapsed_s=result.elapsed_s,
            error=None,
        )
    # SCORER SEAM: a real, scoreable response -> consult the injected scorer (Step-4 default is
    # collect-only: parsed/score/scorer stay None, raw is captured for offline `mt score`). The
    # scorer is NOT wrapped in a catch-all: the deterministic scorer's contract is that a hostile
    # MODEL RESPONSE never raises (extract_verdict_label folds JSONDecodeError/ValueError/
    # RecursionError -> parse_fail; exact matching cannot raise) and a bad suite regex fails loud at
    # LOAD, so the only way the scorer raises here is a genuine programming bug — which must crash
    # LOUDLY (surface in tests), never masked into a data-destroying error row. Rows are appended
    # as-produced with --resume, so even a crash loses no persisted data.
    outcome = scorer(cell.item, result.response_raw)
    return RunRow(
        run_id=run_id,
        model=cell.model,
        model_id_resolved=result.resolved_model,
        item_id=cell.item.id,
        sample_k=cell.sample_k,
        response_raw=result.response_raw,
        parsed=outcome.parsed,
        score=outcome.score,
        scorer=outcome.scorer,
        judge_scores=None,
        elapsed_s=result.elapsed_s,
        error=None,
    )


# --- The sweep ---------------------------------------------------------------------------


def _chunks(seq: Sequence[_Cell], size: int) -> Iterator[list[_Cell]]:
    """Yield successive ``size``-length chunks of ``seq`` (``size >= 1``)."""
    for i in range(0, len(seq), size):
        yield list(seq[i : i + size])


def _sweep_local(
    fh: IO[str],
    run_id: str,
    pending: Sequence[_Cell],
    *,
    config: RunConfig,
    budget: CallBudget,
    scorer: Scorer,
    transport_factory: TransportFactory | None,
) -> tuple[int, bool]:
    """Call local cells SEQUENTIALLY (single-GPU llama-swap thrashes under concurrency, plan §5).

    Returns ``(rows_written, aborted)``. The budget is consumed by the runner here (``local_chat``
    does not touch it); a spent budget aborts cleanly BEFORE the next call, so no torn row is
    written and every appended row persists for ``--resume``.
    """
    written = 0
    for cell in pending:
        try:
            budget.consume()
        except BudgetExhaustedError:
            return written, True
        result = local_chat(
            cell.item.prompt,
            model=cell.model,
            config=config,
            transport_factory=transport_factory,
        )
        _append_row(fh, _build_row(run_id, cell, result, scorer))
        written += 1
    return written, False


def _sweep_claude(
    fh: IO[str],
    run_id: str,
    pending: Sequence[_Cell],
    *,
    config: RunConfig,
    budget: CallBudget,
    scorer: Scorer,
    runner_factory: RunnerFactory | None,
) -> tuple[int, bool]:
    """Call claude cells through the bounded pool (``config.claude_pool``), one wave per chunk.

    ``claude_call_batch`` consumes the shared budget per call and returns a
    :class:`BudgetExhaustedError` marker for any slot refused when the budget runs out; those cells
    are left un-written (still pending) so ``--resume`` continues from exactly the unspent slots.
    Appending per wave (chunk size = pool) keeps the append-as-produced flush granularity while
    preserving bounded parallelism. Returns ``(rows_written, aborted)``.
    """
    written = 0
    for chunk in _chunks(pending, config.claude_pool):
        if budget.exhausted:
            return written, True
        requests = [ClaudeRequest(prompt=cell.item.prompt, alias=cell.model) for cell in chunk]
        results = claude_call_batch(
            requests, config=config, budget=budget, runner_factory=runner_factory
        )
        aborted = False
        for cell, res in zip(chunk, results, strict=True):
            if isinstance(res, BudgetExhaustedError):
                aborted = True  # refused slot — leave the cell pending for resume.
                continue
            _append_row(fh, _build_row(run_id, cell, res, scorer))
            written += 1
        if aborted:
            return written, True
    return written, False


def _resolve_run_dir(out_path: Path, run_id: str) -> Path:
    """Validate an INCOMING (untrusted) run_id and resolve its dir with a containment check.

    An externally-supplied run_id — the ``--resume`` value or the ``mt score`` positional — is
    untrusted: a ``..`` component would traverse out of ``<out>/runs/`` and let the run/score path
    create, append, or atomically OVERWRITE a ``rows.jsonl`` outside the run store (path-traversal
    write). Reject anything not matching the minted shape BEFORE joining, then assert the resolved
    path is directly contained under ``runs/`` as defense-in-depth. Never used for a freshly-minted
    id (that is trusted by construction).
    """
    if not _RUN_ID_RE.match(run_id):
        raise RunError(
            f"invalid run_id {run_id!r}; must match {_RUN_ID_RE.pattern} (path-traversal guard)"
        )
    runs_dir = (out_path / "runs").resolve()
    run_dir = (out_path / "runs" / run_id).resolve()
    if run_dir.parent != runs_dir:
        raise RunError(f"resolved run dir escapes the run store: {run_dir}")
    return run_dir


def _validate_names(names: Sequence[str], label: str) -> None:
    """Validate roster/judge names against switchboard's ``_SAFE_NAME_RE`` (via config's validator).

    ``--models`` / ``--judges`` CLI overrides bypass ``RunConfig``'s own validation, so a malformed
    name would otherwise flow straight into a run-dir path component and a switchboard call-site key
    (plan §3). Reuse ``config._validate_name_list`` (one source of truth — the same check the config
    roster gets) and convert its ``ConfigError`` into a ``RunError`` for a uniform fail-loud face.
    """
    try:
        _validate_name_list(list(names), label)
    except ConfigError as exc:
        raise RunError(str(exc)) from exc


def _validate_sweep_params(roster: Sequence[str], samples: int, max_calls: int) -> None:
    """Fail loud on invalid sweep parameters BEFORE any filesystem mutation (plan §8 D9).

    Hoisted ahead of ``mkdir`` / manifest / snapshot writes so e.g. ``--budget 0`` raises before an
    orphaned zero-row run dir is ever created.
    """
    if not roster:
        raise RunError("roster is empty; nothing to sweep")
    if samples < 1:
        raise RunError(f"samples_per_cell must be >= 1, got {samples}")
    if max_calls < 1:
        raise RunError(f"budget max_calls must be >= 1, got {max_calls}")


def _pending_cells(
    model: str, suite: Suite, samples: int, done_keys: set[tuple[str, str, int]]
) -> list[_Cell]:
    """The cells for ``model`` that have no terminal row yet (item x sample, in suite order)."""
    return [
        _Cell(model, item, k)
        for item in suite.items
        for k in range(samples)
        if (model, item.id, k) not in done_keys
    ]


def run(
    *,
    suite: Suite,
    config: RunConfig,
    out_dir: str | Path,
    roster: Sequence[str] | None = None,
    samples_per_cell: int | None = None,
    judges: Sequence[str] | None = None,
    max_calls: int | None = None,
    preregister: str | None = None,
    resume: str | None = None,
    scorer: Scorer = collect_only_scorer,
    local_transport_factory: TransportFactory | None = None,
    claude_runner_factory: RunnerFactory | None = None,
) -> RunResult:
    """Sweep ``suite x roster x samples`` and write the run store; return a :class:`RunResult`.

    A fresh run (``resume is None``) always MINTS A NEW ``run_id`` (append-only history — a prior
    run is never overwritten), writes the manifest + suite snapshot, and sweeps every cell. A
    ``resume=<run_id>`` re-opens that run dir, reads its manifest for the authoritative roster /
    samples / suite-hash, tolerates a torn trailing rows line, and sweeps only the cells with no
    terminal row (see the module docstring's completeness rule). ``max_calls`` on a resume raises
    the (fresh) budget for the remaining cells; the manifest is not rewritten.

    Local cells are called sequentially; claude cells through the bounded pool. Every model call
    counts against one shared :class:`CallBudget`; when it is exhausted the sweep STOPS cleanly and
    ``aborted=True`` — already-appended rows persist and a subsequent ``--resume`` continues.
    """
    out_path = Path(out_dir)

    if resume is not None:
        run_id = resume
        # Validate the untrusted incoming run_id + containment BEFORE any path join (BLOCK 4).
        run_dir = _resolve_run_dir(out_path, run_id)
        if not run_dir.is_dir():
            raise RunError(f"cannot resume: run dir not found: {run_dir}")
        manifest = _read_manifest(run_dir)
        stored_hash = str(manifest["suite_hash"])
        if suite.item_hash != stored_hash:
            raise RunError(
                "cannot resume: suite hash mismatch (resuming a different instrument); "
                f"manifest={stored_hash} provided={suite.item_hash}"
            )
        # Dedupe defensively (the manifest roster was deduped at write time — a duplicate model must
        # never re-process a cell, see the fresh branch); samples/budget from the stored manifest.
        stored_roster = cast("Sequence[object]", manifest["roster"])
        eff_roster = list(dict.fromkeys(str(m) for m in stored_roster))
        eff_samples = int(cast("int", manifest["samples_per_cell"]))
        stored_budgets = cast("Mapping[str, object]", manifest["budgets"])
        eff_max_calls = (
            max_calls if max_calls is not None else int(cast("int", stored_budgets["max_calls"]))
        )
        # Fail loud on an invalid (e.g. --budget 0) resume budget BEFORE the torn-line rewrite.
        _validate_sweep_params(eff_roster, eff_samples, eff_max_calls)
        existing_rows, torn = _read_rows(run_dir)
        if torn:
            _rewrite_rows(run_dir, existing_rows)
    else:
        run_id = _mint_run_id()
        run_dir = out_path / "runs" / run_id  # minted id is trusted by construction.
        # Dedupe the roster preserving order: a duplicate model (e.g. --models "m,m") must not
        # double-write the same (model,item,sample) cell (BLOCK 3).
        eff_roster = list(dict.fromkeys(roster if roster is not None else config.roster))
        eff_samples = samples_per_cell if samples_per_cell is not None else config.samples_per_cell
        eff_judges = list(judges) if judges is not None else list(config.judges)
        eff_max_calls = max_calls if max_calls is not None else config.max_calls
        # ALL validity checks BEFORE any filesystem mutation (BLOCK 1): invalid budget/roster/
        # samples or a malformed --models/--judges name must fail loud without minting a run dir.
        _validate_sweep_params(eff_roster, eff_samples, eff_max_calls)
        _validate_names(eff_roster, "roster")  # CLI overrides through the config-roster validation
        _validate_names(eff_judges, "judges")
        try:
            run_dir.mkdir(parents=True)  # NOT exist_ok: a run_id collision must fail loud.
        except FileExistsError as exc:
            raise RunError(f"run dir already exists (run_id collision): {run_dir}") from exc
        _write_manifest(
            run_dir,
            run_id=run_id,
            suite=suite,
            roster=eff_roster,
            samples=eff_samples,
            judges=eff_judges,
            config=config,
            max_calls=eff_max_calls,
            preregister=preregister,
        )
        _write_suite_snapshot(run_dir, suite)
        existing_rows = []

    budget = CallBudget(eff_max_calls)
    done_keys = {row.cell_key for row in existing_rows}
    cells_total = len(eff_roster) * len(suite.items) * eff_samples

    cells_written = 0
    aborted = False
    rows_path = run_dir / "rows.jsonl"
    with rows_path.open("a", encoding="utf-8") as fh:
        try:
            for model in eff_roster:
                pending = _pending_cells(model, suite, eff_samples, done_keys)
                if not pending:
                    continue
                if model in CLAUDE_ALIASES:
                    written, aborted = _sweep_claude(
                        fh,
                        run_id,
                        pending,
                        config=config,
                        budget=budget,
                        scorer=scorer,
                        runner_factory=claude_runner_factory,
                    )
                else:
                    written, aborted = _sweep_local(
                        fh,
                        run_id,
                        pending,
                        config=config,
                        budget=budget,
                        scorer=scorer,
                        transport_factory=local_transport_factory,
                    )
                cells_written += written
                if aborted:
                    break
        except RunError:
            raise
        except Exception as exc:
            # Defense-in-depth (BLOCK 2): with claude_call's widened catch a worker exception is
            # already a structured error row, so this should be unreachable — but if any unexpected
            # exception still escapes the sweep, wrap it as RunError so the CLI returns a clean
            # non-zero exit (never a raw traceback). Rows appended before it persist (flushed).
            raise RunError(f"sweep failed unexpectedly: {exc}") from exc

    return RunResult(
        run_id=run_id,
        run_dir=run_dir,
        cells_total=cells_total,
        cells_completed=len(done_keys) + cells_written,
        cells_this_run=cells_written,
        budget_used=budget.used,
        budget_max=budget.max_calls,
        aborted=aborted,
    )


def _open_run(run_id: str, out_dir: str | Path) -> tuple[Path, Suite]:
    """Resolve + open a stored run: traversal-guarded run dir, validated suite snapshot, hash check.

    The ONE owner of the open-a-run prologue shared by :func:`load_run_suite` and :func:`score_run`
    (resolve + manifest + snapshot + hash-check), so a future hash-check edit can't drift between
    them (NIT 2). Fail loud (``RunError``) on an invalid/traversing ``run_id``, a missing dir/
    snapshot, or a snapshot/manifest hash mismatch.
    """
    run_dir = _resolve_run_dir(Path(out_dir), run_id)
    if not run_dir.is_dir():
        raise RunError(f"cannot open run: run dir not found: {run_dir}")
    manifest = _read_manifest(run_dir)
    suite = _read_suite_snapshot(run_dir)
    if suite.item_hash != str(manifest["suite_hash"]):
        raise RunError("suite snapshot hash does not match manifest (corrupt run store)")
    return run_dir, suite


def load_run_suite(run_id: str, out_dir: str | Path) -> Suite:
    """Open a stored run's suite snapshot (the instrument it measured), traversal-guarded.

    The scorer for a suite depends on its ``scoring`` spec (a verdict scorer needs the labels), but
    ``mt score <run_id>`` is NOT re-handed the suite — it re-scores from the run store. This exposes
    the per-run snapshot so the CLI can pick the deterministic scorer from the run's own scoring
    type (Step 5), sharing :func:`_open_run`'s traversal guard + hash check with :func:`score_run`.
    Fail loud: an invalid/traversing ``run_id``, a missing run dir/snapshot, or a snapshot/manifest
    hash mismatch all raise :class:`RunError`.
    """
    return _open_run(run_id, out_dir)[1]


def score_run(
    *,
    run_id: str,
    out_dir: str | Path,
    scorer: Scorer = collect_only_scorer,
) -> ScoreResult:
    """Re-open a stored run and (re)score its raw responses WITHOUT re-calling models (Decision 10).

    Reads every row, applies the injected ``scorer`` to each REAL response (an error row is left
    untouched; a ``scorer="no_response"`` force-0 row stays 0 without re-consulting a scorer), then
    atomically rewrites ``rows.jsonl`` with the updated ``parsed``/``score``/``scorer`` fields. The
    gold ``expected`` the scorer needs comes from the per-run suite snapshot, so no external suite
    path is required (and none can drift under it). In Step 4 the default scorer is collect-only, so
    real responses stay unscored/pending; Step 5 plugs its deterministic scorer in at this seam.
    """
    # Open the run through the shared prologue (validates the untrusted run_id + containment BEFORE
    # any path join, reads the manifest + snapshot, and hash-checks — one owner, NIT 2).
    run_dir, suite = _open_run(run_id, out_dir)
    items_by_id = {item.id: item for item in suite.items}

    rows, _torn = _read_rows(run_dir)
    rescored: list[RunRow] = []
    n_scored = 0
    n_no_response = 0
    for row in rows:
        if row.error is not None:
            rescored.append(row)  # terminal error — nothing to (re)score.
            continue
        if row.scorer == NO_RESPONSE_SCORER:
            n_no_response += 1
            rescored.append(row)  # force-0 stays 0, never re-judged.
            continue
        item = items_by_id.get(row.item_id)
        if item is None:
            raise RunError(
                f"row references unknown item_id {row.item_id!r} (not in the suite snapshot)"
            )
        # SCORER SEAM (Decision 10): re-score the stored raw response — no model call. This is
        # NON-DESTRUCTIVE: only parsed/score/scorer are replaced; ``response_raw`` is preserved
        # verbatim (durable evidence — a re-score must never overwrite the stored raw with "").
        # The scorer is not wrapped in a catch-all (see _build_row): a hostile stored response never
        # raises, a bad suite regex fails loud at load, so a raise here is a genuine bug that must
        # surface, not be masked into a row that clobbers the stored raw.
        outcome = scorer(item, row.response_raw)
        rescored.append(
            replace(row, parsed=outcome.parsed, score=outcome.score, scorer=outcome.scorer)
        )
        if outcome.score is not None:
            n_scored += 1

    _rewrite_rows(run_dir, rescored)
    return ScoreResult(total=len(rows), scored=n_scored, no_response=n_no_response)
