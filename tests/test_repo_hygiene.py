"""Repo hygiene: no operator home identity in the tracked tree (issue #26).

Shape-based, not secret-based. A guard that grepped for the literal account name
would itself contain that name and fail its own assertion. Instead this asserts that
no Windows home path and no Claude Code project-dir slug names a real account at all:
the only accepted home segments are the documented placeholders. Consequence: it also
catches a FUTURE operator's account name, not just the one scrubbed in issue #26.

Enumeration is ``git ls-files``, deliberately NOT a filesystem walk. A walk would
reach gitignored working data -- ``data/runs/``, ``data/exports/issues.json`` -- which
legitimately contains absolute local paths and was never in git.

Issue #26's own header said "9 across 3 files"; that was the LINE count. There were 10
occurrences (workspace-bench-prior-art.md line 6 carried two), all backslash-form. The
sibling repo citation-needed (#21) undercounted the same way and additionally missed a
whole encoding -- the ``c--Users-<name>-<project>`` project-dir slug, which contains no
path separator and so matches no path-shaped pattern. That form is absent here today;
it is checked anyway, because the cost of checking is zero and the cost of missing it
there was 94 unremoved occurrences.

This file is part of the tree it walks and is NOT path-exempted: an exemption would go
stale on rename. So the negative fixtures assemble their account token at RUNTIME --
no literal home segment appears in this source.

DECLARED GAPS: percent-encoded, double-escaped, drive-letter-less, POSIX /home/, and
WSL /mnt/c/ forms are not detected; none are present today. It cannot see git history
or GitHub issue bodies.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Home segments that are not a real account: `x` (the sibling repo's convention),
#: an env-var indirection, or a `~` home reference.
ALLOWED_HOME_TOKENS = frozenset({"x"})

#: Windows home paths: any drive letter, either separator, any case of "users".
#: `$env:USERPROFILE` and `%USERPROFILE%` never match -- they carry no drive letter.
PATH_RE = re.compile(r"[A-Za-z]:[\\/]+(?i:users)[\\/]+(?!x(?:[\\/]|$))([A-Za-z0-9._$-]+)")

#: Claude Code project-dir slugs: the drive-dash-dash-Users-dash form.
SLUG_RE = re.compile(r"[A-Za-z]--(?i:users)-(?!x-)([A-Za-z0-9._$]+)")


def _tracked_files() -> list[str]:
    git = shutil.which("git")  # resolved, per measure_twice/agent_bench/process.py:1685
    if git is None:  # pragma: no cover - git absent
        pytest.skip("git is unavailable; cannot enumerate the tracked tree")
    proc = subprocess.run(  # noqa: S603 - resolved executable and literal flags.
        [git, "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:  # pragma: no cover - not a work tree
        pytest.skip("git ls-files failed; cannot enumerate the tracked tree")
    return [p for p in proc.stdout.decode("utf-8").split("\0") if p]


def _violations_in(text: str, rel: str) -> list[str]:
    found: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in (*PATH_RE.finditer(line), *SLUG_RE.finditer(line)):
            if match.group(1).lower() not in ALLOWED_HOME_TOKENS:
                found.append(f"{rel}:{lineno}: {match.group(0)!r}")
    return found


def test_no_operator_home_identity_in_tracked_tree() -> None:
    """No tracked file names a real account in a home path or project slug."""
    violations: list[str] = []
    for rel in _tracked_files():
        try:
            raw = (PROJECT_ROOT / rel).read_bytes()
        except OSError:
            continue
        if b"\0" in raw:  # binary
            continue
        violations.extend(_violations_in(raw.decode("utf-8", errors="ignore"), rel))
    assert not violations, (
        "operator home identity in the tracked tree. In a runnable shell recipe use "
        "$env:USERPROFILE; in an evidence citation use a workspace-relative path:\n"
        + "\n".join(violations)
    )


def test_guard_detects_a_planted_violation() -> None:
    """Red-on-garbage anchor: the patterns must actually fire.

    Without this, a typo that made both regexes match nothing would leave
    ``test_no_operator_home_identity_in_tracked_tree`` permanently, silently green.
    The account token is assembled at runtime so this file does not violate its own
    guard, which does not exempt itself.
    """
    who = "some" + "one"
    planted = [
        rf"C:\Users\{who}\dev",
        f"c:/Users/{who}/dev",
        f"C:/USERS/{who}",
        f"c--Users-{who}-dev",
        f"C--Users-{who}-dev-Alpha4Gate",
        f"memory--c--users-{who}-dev--note",
    ]
    for line in planted:
        assert _violations_in(line, "planted"), line

    allowed = [
        r"$env:USERPROFILE\dev\measure-twice",
        r"%USERPROFILE%\dev",
        "~/worktree_switchboard-endpoint-launcher/",
        "switchboard/switchboard/",
        ".claude/references/skill-role-taxonomy.md",
        r"C:\Users\x\dev",
        "c--Users-x-dev",
    ]
    for line in allowed:
        assert not _violations_in(line, "allowed"), line
