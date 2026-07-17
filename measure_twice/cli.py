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

# A subcommand handler: consumes the parsed namespace, returns a process exit code.
Handler = Callable[[argparse.Namespace], int]


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
