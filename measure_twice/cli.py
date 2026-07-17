"""The ``mt`` command-line entry point.

Step 1 ships the argparse skeleton + ``mt --version`` only. Subcommands (validate / run /
score / report / calibrate / profile / claims / author / smoke) are added by later steps at
the single extension point below: register a subparser and a handler in ``_build_parser``.
Each handler takes the parsed ``argparse.Namespace`` and returns an ``int`` exit code, so the
dispatch stays uniform and ``main`` never grows a per-command branch.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from measure_twice import __version__
from measure_twice.suite import SuiteError, load_suite

# A subcommand handler: consumes the parsed namespace, returns a process exit code.
Handler = Callable[[argparse.Namespace], int]


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


def _build_parser() -> tuple[
    argparse.ArgumentParser, argparse._SubParsersAction[argparse.ArgumentParser], dict[str, Handler]
]:
    """Build the top-level parser, its subparser action, and the command -> handler table.

    Extension point: later steps register each subcommand by calling ``subparsers.add_parser``
    and adding its handler to ``handlers`` (keyed by the same command name). ``subparsers`` is
    bound and returned — not discarded — so a step can register ``mt validate`` without a
    second ``add_subparsers`` call (which would raise ``ValueError``). The skeleton registers
    none yet.
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

    # Later steps: subparsers.add_parser("<command>", ...); handlers["<command>"] = <handler>.
    return parser, subparsers, handlers


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` (defaults to ``sys.argv[1:]``) and dispatch; return an exit code.

    The ``[project.scripts]`` wrapper calls this as ``sys.exit(main())``, so returning an int
    is the contract. ``mt --version`` prints the version and returns 0; a bare invocation with
    no subcommand prints help and returns 1 (nothing to do yet).
    """
    parser, _subparsers, handlers = _build_parser()
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
