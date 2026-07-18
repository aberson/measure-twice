"""The ``mt`` command-line entry point.

Subcommands are added at the single extension point below (``_build_parser``): register a
subparser and a handler keyed by the same command name. Each handler takes the parsed
``argparse.Namespace`` and returns an ``int`` exit code, so dispatch stays uniform and ``main``
never grows a per-command branch. Registered so far: ``validate`` (Step 2), ``run`` + ``score``
(Step 4).

Dependency-injection seam (offline tests): ``run`` and ``score`` reach the adapters + the scorer
through a :class:`CliDeps` bundle. Production leaves ``scorer=None`` so each command AUTO-SELECTS a
deterministic scorer from the suite's ``scoring`` type (Step 5: verdict/exact -> the deterministic
scorer; rubric -> collect-only until Step 6's judge lands); tests pass a ``CliDeps`` with stub
factories and, where a scored path is exercised, an explicit stub ``scorer`` (which overrides
auto-selection) so the whole ``mt run`` path is driven end to end with ZERO live calls.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from measure_twice import __version__, runner
from measure_twice.adapters.claude_cli import RunnerFactory
from measure_twice.adapters.local import TransportFactory
from measure_twice.config import ConfigError, load_config
from measure_twice.runner import RunError, Scorer, collect_only_scorer
from measure_twice.scoring import ScoringError, make_deterministic_scorer
from measure_twice.suite import ScoringSpec, SuiteError, load_suite

# A subcommand handler: consumes the parsed namespace, returns a process exit code.
Handler = Callable[[argparse.Namespace], int]

# The stderr note printed when a rubric suite is swept before Step 6's judge exists: responses are
# collected unscored and can be re-scored later (Decision 10) once the judge lands.
_RUBRIC_DEFER_NOTE = (
    "note: rubric scoring is the LLM judge (Step 6); responses collected unscored — "
    "re-score with `mt score <run_id>` once the judge lands"
)


@dataclass(frozen=True, slots=True)
class CliDeps:
    """Injected dependencies for the ``run`` / ``score`` commands (the DI seam).

    Production defaults (all ``None``) make ``mt run`` build the real adapters and AUTO-SELECT the
    deterministic scorer from the suite's scoring type; tests inject stub adapter factories and, for
    a scored path, an explicit ``scorer`` so the CLI path is fully offline. ``scorer`` — when not
    ``None`` — overrides auto-selection for BOTH ``mt run`` (score-as-swept) and ``mt score``
    (re-score stored rows), so a stub scorer drives either command deterministically.
    """

    local_transport_factory: TransportFactory | None = None
    claude_runner_factory: RunnerFactory | None = None
    scorer: Scorer | None = None


def _select_scorer(scoring: ScoringSpec) -> tuple[Scorer, str | None]:
    """Pick the deterministic scorer for a suite's scoring type; rubric defers to Step 6.

    Returns ``(scorer, note)``: verdict/exact -> the deterministic scorer (note ``None``); rubric ->
    the collect-only scorer + a deferral note (so ``mt run`` still COLLECTS raw responses for a
    later ``mt score``, and ``mt score`` leaves them pending — a clean documented behavior, never a
    crash). A verdict suite with no labels (or one reserving the parse-fail marker) raises
    :class:`~measure_twice.scoring.ScoringError`, surfaced by the handler as a clean non-zero exit.
    """
    if scoring.type == "rubric":
        return collect_only_scorer, _RUBRIC_DEFER_NOTE
    return make_deterministic_scorer(scoring), None


def _split_csv(value: str) -> list[str]:
    """Split a ``--models`` / ``--judges`` CSV into trimmed, non-empty tokens."""
    return [tok.strip() for tok in value.split(",") if tok.strip()]


def _handle_validate(args: argparse.Namespace) -> int:
    """``mt validate <suite.json>``: schema-check a suite and print its item-hash on success.

    Exit 0 on a valid suite (after printing the canonical item hash); NON-ZERO on any violation —
    the ``SuiteError`` is caught and printed to stderr, and the loader's fail-loud contract means
    a printed hash certifies a fully-validated instrument, never a partially-loaded one.

    The item hash is computed INSIDE the try/except and BEFORE any "valid" line is printed, so a
    hash failure surfaces as a caught error, never a raw traceback after a premature success line.
    """
    try:
        suite = load_suite(args.suite)
        item_hash = suite.item_hash
    except SuiteError as exc:
        print(f"validate: {exc}", file=sys.stderr)
        return 1
    print(f"{suite.suite}: valid ({len(suite.items)} items, scoring={suite.scoring.type})")
    print(f"item_hash: {item_hash}")
    return 0


def _handle_run(args: argparse.Namespace, deps: CliDeps) -> int:
    """``mt run --suite <path> --models <csv> ...``: execute a sweep and print a one-line summary.

    Loads config + suite (fail loud on either), resolves the roster from ``--models`` (else the
    config roster), runs the sweep through the injected factories + scorer, and prints
    ``<run_id>: <done>/<total> cells done, budget <used>/<max> used``. A clean budget abort still
    exits 0 but prints the resume hint to stderr (a resumable interruption, not a failure).
    """
    try:
        config = load_config(args.config)
        suite = load_suite(args.suite)
    except (ConfigError, SuiteError) as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 1
    # Auto-select the deterministic scorer from the suite (Step 5) unless a stub is injected.
    if deps.scorer is not None:
        scorer = deps.scorer
    else:
        try:
            scorer, note = _select_scorer(suite.scoring)
        except ScoringError as exc:
            print(f"run: {exc}", file=sys.stderr)
            return 1
        if note is not None:
            print(note, file=sys.stderr)
    roster = _split_csv(args.models) if args.models else None
    judges = _split_csv(args.judges) if args.judges else None
    try:
        result = runner.run(
            suite=suite,
            config=config,
            out_dir=Path(args.out),
            roster=roster,
            samples_per_cell=args.samples,
            judges=judges,
            max_calls=args.budget,
            preregister=args.preregister,
            resume=args.resume,
            scorer=scorer,
            local_transport_factory=deps.local_transport_factory,
            claude_runner_factory=deps.claude_runner_factory,
        )
    except RunError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 1
    print(
        f"{result.run_id}: {result.cells_completed}/{result.cells_total} cells done, "
        f"budget {result.budget_used}/{result.budget_max} used"
    )
    if result.aborted:
        print(
            f"budget exhausted at {result.budget_used}/{result.budget_max} calls; "
            f"resume with --resume {result.run_id}",
            file=sys.stderr,
        )
    return 0


def _handle_score(args: argparse.Namespace, deps: CliDeps) -> int:
    """``mt score <run_id>``: (re)score a stored run's raw responses WITHOUT re-calling models.

    Applies the scorer to each stored real response (force-0 no-response rows stay 0, error rows
    stay untouched), rewrites the scored fields, and prints
    ``<run_id>: <N> rows, <M> scored, <K> no-response``. The scorer is auto-selected from the run's
    OWN suite snapshot (the suite isn't re-supplied on this command) unless a stub is injected.
    """
    out_dir = Path(args.out)
    if deps.scorer is not None:
        scorer = deps.scorer
    else:
        try:
            suite = runner.load_run_suite(args.run_id, out_dir)
        except RunError as exc:
            print(f"score: {exc}", file=sys.stderr)
            return 1
        try:
            scorer, note = _select_scorer(suite.scoring)
        except ScoringError as exc:
            print(f"score: {exc}", file=sys.stderr)
            return 1
        if note is not None:
            print(note, file=sys.stderr)
    try:
        result = runner.score_run(run_id=args.run_id, out_dir=out_dir, scorer=scorer)
    except RunError as exc:
        print(f"score: {exc}", file=sys.stderr)
        return 1
    print(
        f"{args.run_id}: {result.total} rows, {result.scored} scored, "
        f"{result.no_response} no-response"
    )
    return 0


def _build_parser(
    deps: CliDeps,
) -> tuple[
    argparse.ArgumentParser, argparse._SubParsersAction[argparse.ArgumentParser], dict[str, Handler]
]:
    """Build the top-level parser, its subparser action, and the command -> handler table.

    Extension point: later steps register each subcommand by calling ``subparsers.add_parser``
    and adding its handler to ``handlers`` (keyed by the same command name). ``subparsers`` is
    bound and returned — not discarded — so a step can register a subcommand without a second
    ``add_subparsers`` call (which would raise ``ValueError``). ``deps`` is threaded into the
    ``run``/``score`` handlers (bound via small closures) so the DI seam reaches them without
    changing the uniform ``Handler`` signature.
    """
    parser = argparse.ArgumentParser(
        prog="mt",
        description="measure-twice: local model benchmarking + tier-claim evidence ledger.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the measure-twice version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    handlers: dict[str, Handler] = {}

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate a suite JSON file (schema check + print its item-hash)",
        description="Schema-check a suite JSON file and print its canonical item-hash. "
        "Exits non-zero on any schema violation.",
    )
    validate_parser.add_argument(
        "suite",
        metavar="<suite.json>",
        help="path to the suite JSON file to validate",
    )
    handlers["validate"] = _handle_validate

    run_parser = subparsers.add_parser(
        "run",
        help="execute a benchmark sweep (append-only JSONL, cell-level resume)",
        description="Sweep suite x roster x samples through the adapters, appending one row per "
        "cell. Mints a new run each time unless --resume continues an existing one.",
    )
    run_parser.add_argument("--suite", required=True, metavar="<path>", help="suite JSON to sweep")
    run_parser.add_argument(
        "--models", metavar="<csv>", help="comma-separated roster override (else the config roster)"
    )
    run_parser.add_argument(
        "--samples", type=int, metavar="N", help="samples per cell (else the config value)"
    )
    run_parser.add_argument(
        "--judges", metavar="<csv>", help="comma-separated judges override (recorded in manifest)"
    )
    run_parser.add_argument(
        "--budget", type=int, metavar="N", help="max model calls this run (else config max_calls)"
    )
    run_parser.add_argument(
        "--resume", metavar="<run_id>", help="resume a run, skipping already-completed cells"
    )
    run_parser.add_argument(
        "--out",
        default="data",
        metavar="<dir>",
        help="data home; runs are written under <dir>/runs/ (default: data)",
    )
    run_parser.add_argument(
        "--config", metavar="<path>", help="explicit config path (else the resolution order)"
    )
    run_parser.add_argument(
        "--preregister", metavar="<str>", help="preregistration sentence recorded in the manifest"
    )

    def _run(args: argparse.Namespace) -> int:
        return _handle_run(args, deps)

    handlers["run"] = _run

    score_parser = subparsers.add_parser(
        "score",
        help="(re)score a stored run's raw responses without re-calling models",
        description="Re-open a stored run and (re)score its raw responses offline. Scoring is "
        "re-runnable (Decision 10): no model is re-called.",
    )
    score_parser.add_argument("run_id", metavar="<run_id>", help="the run id to (re)score")
    score_parser.add_argument(
        "--out",
        default="data",
        metavar="<dir>",
        help="data home the run lives under (default: data)",
    )

    def _score(args: argparse.Namespace) -> int:
        return _handle_score(args, deps)

    handlers["score"] = _score

    # Later steps: subparsers.add_parser("<command>", ...); handlers["<command>"] = <handler>.
    return parser, subparsers, handlers


def main(argv: Sequence[str] | None = None, *, deps: CliDeps | None = None) -> int:
    """Parse ``argv`` (defaults to ``sys.argv[1:]``) and dispatch; return an exit code.

    The ``[project.scripts]`` wrapper calls this as ``sys.exit(main())``, so returning an int
    is the contract. ``deps`` is the offline DI seam: production passes nothing (real adapters +
    collect-only scorer); tests pass a :class:`CliDeps` with stub factories. ``mt --version``
    prints the version and returns 0; a bare invocation with no subcommand prints help, returns 1.
    """
    eff_deps = deps if deps is not None else CliDeps()
    parser, _subparsers, handlers = _build_parser(eff_deps)
    args = parser.parse_args(argv)

    if args.version:
        print(f"measure-twice {__version__}")
        return 0

    command: str | None = args.command
    if command is None:
        parser.print_help(sys.stderr)
        return 1

    return handlers[command](args)


if __name__ == "__main__":
    raise SystemExit(main())
