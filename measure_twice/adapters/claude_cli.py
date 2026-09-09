"""``claude_call`` — Claude-tier adapter shelling out to the ``claude`` CLI (subscription OAuth).

Invokes ``claude -p --model <alias> --output-format json`` and unwraps the JSON envelope. The
**prompt is fed on STDIN, never argv** (Windows argv > 32K raises WinError 206 —
``feedback_subprocess_large_arg_stdin_windows``; a benchmark prompt plus a large item easily
clears that), so ``argv`` carries only flags and the prompt goes through ``input=``.

Envelope contract (``claude -p --output-format json``; pinned by ``tests/test_adapters.py`` so CLI
flag drift is caught — plan §9 risk row): a top-level ``result`` string is the assistant text and a
top-level ``model`` string is the RESOLVED concrete model id (e.g. a requested ``"sonnet"`` alias
resolves to ``"claude-sonnet-4-..."``) — recorded per row for drift detection. Classification reads
``is_error``/``subtype`` FIRST (see below); ``result`` and ``model`` are the two text-carrying keys
the success unwrap needs. A missing/renamed ``result`` -> ``bad_envelope`` (drift surfaces loudly),
a missing ``model`` -> the row records the shared unresolved-identity sentinel (the requested
alias is never promoted into provider evidence).

The ``is_error`` guard (production ``subprocess_runner._parse_envelope``,
``void_furnace/src/void_furnace/subprocess_runner.py``): an ``is_error: true`` envelope's
``result`` field carries an ERROR MESSAGE, not model text, and can co-occur with **exit code 0** —
so ``is_error`` is checked BEFORE ``result`` is ever trusted, else a CLI error message would be
scored as a real model answer (silent ledger corruption). ``subtype`` names the failure kind: a
max-turns / length cutoff (e.g. ``"error_max_turns"``) is a truncated partial answer.

Failure -> switchboard reason_class (imported via :mod:`base`, plan §8 D6):
  * ``claude`` not on PATH (``FileNotFoundError``)      -> ``unreachable`` (the tier is unreachable)
  * other ``OSError`` spawning the process             -> ``os_error``
  * any other unclassified exception (runner bug etc)  -> ``os_error`` (never raised; contract)
  * non-zero exit                                      -> ``os_error`` (the CLI ran but failed)
  * ``subprocess.TimeoutExpired``                      -> ``timeout``
  * stdout not JSON                                    -> ``non_json_body``
  * ``is_error: true`` + max-turns/length ``subtype``  -> ``truncated`` (partial answer cut off)
  * ``is_error: true`` (any other subtype)             -> ``os_error`` (CLI error message)
  * JSON present but ``result`` missing/non-str        -> ``bad_envelope``
  * empty/whitespace ``result``                        -> the :data:`NO_RESPONSE` state (score 0)

Scheduling: claude calls run through a **bounded** ``ThreadPoolExecutor`` (``config.claude_pool``,
default 2) via :func:`claude_call_batch`; each call is counted against the run :class:`CallBudget`
(plan §8 D8: 500 calls/run). The LOCAL endpoint is the opposite — called **sequentially** (single
GPU, llama-swap thrashes under concurrency), so the runner loops :func:`local_chat` and never
batches it. The **client-factory DI seam** (``runner_factory``) makes this fully offline-testable:
the default runs the real ``subprocess``; tests inject a stub returning canned stdout/rc — ZERO
live ``claude`` invocations in the suite.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from measure_twice.adapters._claude_runtime import (
    ClaudeSetupError,
    _allowlisted_environment,
    _credential_free_environment,
    _LauncherBoundary,
    _posix_account_home,
    _resolve_executable,
    _runtime_launcher_policy,
    _trusted_temp_root,
    _windows_runtime_paths,
    _windows_system_environment,
)
from measure_twice.adapters.base import (
    RC_BAD_ENVELOPE,
    RC_NON_JSON_BODY,
    RC_OS_ERROR,
    RC_TIMEOUT,
    RC_TRUNCATED,
    RC_UNREACHABLE,
    UNRESOLVED_MODEL_ID,
    AdapterError,
    ModelCallResult,
    resolved_model_of,
)
from measure_twice.config import RunConfig
from measure_twice.model_sweep_execution import (
    CLAUDE_MODEL_PLACEHOLDER,
    PROVIDER_CLAUDE,
    ClaudeContextProfile,
    ExecutionProfileError,
)

__all__ = [
    "DEFAULT_CLAUDE_TIMEOUT_S",
    "BudgetExhaustedError",
    "CallBudget",
    "ClaudeInvocation",
    "ClaudeRequest",
    "ClaudeRuntime",
    "ClaudeSetupError",
    "RunnerFactory",
    "SubprocessResult",
    "SubprocessRunner",
    "_LauncherBoundary",
    "_posix_account_home",
    "build_claude_invocation",
    "build_claude_runtime",
    "claude_call",
    "claude_call_batch",
    "doctor_claude_runtime",
]

# claude calls can be slow (cold model, long generations); this is the per-call default. As with
# the local adapter, RunConfig carries no timeout field in v1 — a caller/runner may override.
DEFAULT_CLAUDE_TIMEOUT_S: Final[float] = 300.0

# Bounded window for the timeout tree-kill itself (the taskkill call + the post-kill pipe reap), so
# a stuck taskkill or a surviving pipe-holding grandchild can never re-hang the run the tree-kill
# exists to unblock (the Block-4 guarantee).
_TREE_KILL_TIMEOUT_S: Final[float] = 10.0

# The claude ``--output-format json`` truncation signal is the error ``subtype``: a max-turns /
# length cutoff leaves a partial answer. Matched case-insensitively by substring so the known
# ``"error_max_turns"`` plus any max-tokens / length variant all map to ``truncated``.
_TRUNCATION_SUBTYPE_MARKERS: Final[tuple[str, ...]] = ("max_turns", "max_tokens", "length")
_DOCTOR_TIMEOUT_S: Final[float] = 30.0
_REQUIRED_CLAUDE_FLAGS: Final[tuple[str, ...]] = (
    "-p",
    "--model",
    "--output-format",
    "--safe-mode",
    "--tools",
    "--disable-slash-commands",
    "--no-chrome",
    "--no-session-persistence",
)


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    """The minimal subprocess outcome the adapter needs: exit code + captured streams.

    The DI seam returns this (not a raw ``subprocess.CompletedProcess``) so a test stub can build
    one directly without constructing a ``CompletedProcess``. The default runner adapts the real
    ``subprocess.Popen`` result into it.
    """

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class ClaudeRuntime:
    """Resolved executable plus immutable prompt-only context used for one run."""

    executable: str
    context: ClaudeContextProfile
    env: Mapping[str, str]
    probe_env: Mapping[str, str]
    command_processor: str | None = None
    cli_version: str | None = None


@dataclass(frozen=True, slots=True)
class ClaudeInvocation:
    """Final Popen arguments: native argv or an already-encoded Windows batch command line."""

    argv: tuple[str, ...] | str
    cwd: Path
    env: Mapping[str, str]


# The runner receives the final immutable invocation (absolute argv, unique cwd, allowlisted env),
# stdin prompt, and timeout. Tests inspect the production builder rather than bypassing it.
SubprocessRunner = Callable[[ClaudeInvocation, str, float], SubprocessResult]
RunnerFactory = Callable[[], SubprocessRunner]


@dataclass(frozen=True, slots=True)
class ClaudeRequest:
    """One claude call for :func:`claude_call_batch`: the prompt + the model alias to request."""

    prompt: str
    alias: str


class BudgetExhaustedError(RuntimeError):
    """Raised by :meth:`CallBudget.consume` when the run's call budget is spent (plan §8 D8).

    A run-control condition, not a model failure — the runner (Step 4) catches it to stop
    scheduling, rather than recording it as a result row.
    """


class CallBudget:
    """A thread-safe run-level call budget (plan §8 D8: default 500 calls/run).

    :func:`claude_call` invokes :meth:`consume` BEFORE spawning a subprocess, so the budget both
    counts and — defense-in-depth — CAPS subscription calls even if the runner's own pre-check is
    bypassed. The claude pool runs calls concurrently, so the check-then-increment is lock-guarded
    (a bare ``+= 1`` after an unlocked ``>=`` check would let two workers overrun the cap). The
    runner owns the graceful abort via :attr:`remaining` / :attr:`exhausted`.
    """

    def __init__(self, max_calls: int, used: int = 0) -> None:
        if max_calls < 1:
            raise AdapterError(f"CallBudget max_calls must be >= 1, got {max_calls}")
        self.max_calls = max_calls
        self.used = used
        self._lock = threading.Lock()

    def consume(self) -> int:
        """Count one call and return the new ``used`` total; raise if the budget is spent."""
        with self._lock:
            if self.used >= self.max_calls:
                raise BudgetExhaustedError(
                    f"call budget exhausted: {self.used}/{self.max_calls} used"
                )
            self.used += 1
            return self.used

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.max_calls - self.used

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self.used >= self.max_calls

    def __repr__(self) -> str:
        return f"CallBudget(max_calls={self.max_calls}, used={self.used})"


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Attempt to kill ``proc`` and its descendants on timeout.

    ``subprocess``'s own kill targets only the direct child, so a grandchild leaks — verified on
    Windows (``feedback_subprocess_tree_kill_windows``). Windows walks the PID tree via
    ``taskkill /T``; POSIX signals the whole process group (the child was made a group leader via
    ``start_new_session``). If tree cleanup fails, fall back to killing the direct child;
    descendant termination is best-effort.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            paths = _windows_runtime_paths()
            system_directory = paths.windows / "System32"
            result = subprocess.run(  # noqa: S603
                [str(system_directory / "taskkill.exe"), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=_TREE_KILL_TIMEOUT_S,
                cwd=system_directory,
                env=dict(_windows_system_environment(paths)),
            )
            result.check_returncode()
        except (ClaudeSetupError, OSError, subprocess.SubprocessError):
            # Best-effort cleanup must not inherit the checkout cwd, PATH, COMSPEC, or OAuth merely
            # because the canonical system helper failed. Fall back to the direct child.
            proc.kill()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()  # fall back to a direct child kill


def _subprocess_runner(
    invocation: ClaudeInvocation, input_text: str, timeout: float
) -> SubprocessResult:
    """Run the fully-built sealed invocation with stdin and captured UTF-8 streams.

    ``encoding="utf-8"`` is PINNED (never the platform default): Windows' text mode is the locale
    ANSI code page (cp1252), which BOTH raises ``UnicodeEncodeError`` on a non-cp1252 prompt char
    (emoji/CJK) — a ``ValueError`` that ``claude_call`` does not catch, crashing the sweep — AND
    silently mojibake-corrupts non-ASCII UTF-8 stdout (em-dash, curly quotes, café) that Claude's
    prose uses constantly, poisoning the exact benchmark text we measure. ``errors="replace"``
    keeps a rare malformed byte from crashing the run (valid UTF-8 round-trips byte-faithfully).
    On timeout attempt tree cleanup, with a direct-child fallback (see :func:`_kill_process_tree`).
    """
    # POSIX: own process group so a timeout can killpg the whole tree. Windows: no-op here
    # (taskkill /T walks the PID tree directly), so the flag is False.
    proc = subprocess.Popen(  # noqa: S603  # pinned launcher and validated model/flags only.
        invocation.argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=invocation.cwd,
        env=dict(invocation.env),
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=sys.platform != "win32",
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            # Reap the killed tree's pipes so no FD/zombie leaks. Bounded: a surviving grandchild
            # holding stdout open must not hang the run — we still re-raise the original timeout.
            proc.communicate(timeout=_TREE_KILL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass
        raise
    return SubprocessResult(returncode=proc.returncode, stdout=stdout, stderr=stderr)


def _default_runner_factory() -> SubprocessRunner:
    """Construct the real ``subprocess`` runner (the DI seam's production default)."""
    return _subprocess_runner


def build_claude_runtime(
    config: RunConfig,
    *,
    runner_factory: RunnerFactory | None = None,
    cli_version: str | None = None,
) -> ClaudeRuntime:
    """Resolve executable/env without mutating global cwd or ``os.environ``."""
    context = config.execution_profile.claude
    policy = _runtime_launcher_policy()
    launcher = _resolve_executable(context, policy, allow_virtual=runner_factory is not None)
    environment = _allowlisted_environment(context, launcher)
    command_processor: str | None = None
    if sys.platform == "win32" and launcher.executable.suffix.lower() in {".bat", ".cmd"}:
        if any(character in str(launcher.executable) for character in "%!^&|<>()"):
            raise ClaudeSetupError(
                "approved Windows batch launcher path contains unsupported command metacharacters"
            )
        command_processor = str(
            (_windows_runtime_paths().windows / "System32" / "cmd.exe").resolve()
        )
    return ClaudeRuntime(
        executable=str(launcher.executable),
        context=context,
        env=environment,
        probe_env=_credential_free_environment(environment),
        command_processor=command_processor,
        cli_version=cli_version,
    )


def build_claude_invocation(
    runtime: ClaudeRuntime, *, requested_model: str, cwd: Path
) -> ClaudeInvocation:
    """Build the exact sealed model-call argv for an already-validated provider binding."""
    if not cwd.is_absolute():
        raise ClaudeSetupError(f"Claude invocation cwd must be absolute, got {cwd}")
    argv_tail = tuple(
        requested_model if argument == CLAUDE_MODEL_PLACEHOLDER else argument
        for argument in runtime.context.argv_template
    )
    return ClaudeInvocation(
        argv=_launcher_argv(runtime, argv_tail),
        cwd=cwd,
        env=runtime.env,
    )


def _launcher_argv(runtime: ClaudeRuntime, args: Sequence[str]) -> tuple[str, ...] | str:
    """Build a direct native argv or an explicit pinned Windows batch launch chain."""
    direct = (runtime.executable, *args)
    if runtime.command_processor is None:
        return direct
    # ``/d`` disables Command Processor AutoRun; ``/s`` gives the quoted /c command deterministic
    # handling. Executable provenance and the requested-model token grammar are validated before
    # this point, and every remaining argument is a frozen code-owned flag/value.
    # cmd strips ONE outer quote pair after /s /c; retain another pair around the launcher and
    # each argument, including the empty --tools value. Pass the finished command line as a string:
    # Popen's list conversion targets C-runtime parsing and would backslash-escape these quotes.
    # https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd
    command = " ".join(f'"{argument}"' for argument in direct)
    return f'"{runtime.command_processor}" /d /s /c "{command}"'


def _probe_invocation(runtime: ClaudeRuntime, args: Sequence[str], cwd: Path) -> ClaudeInvocation:
    return ClaudeInvocation(
        argv=_launcher_argv(runtime, args),
        cwd=cwd,
        env=runtime.probe_env,
    )


def _help_advertises_flag(help_text: str, flag: str) -> bool:
    """Match a complete option token, not a substring such as ``-p`` inside ``--print``."""
    pattern = rf"(?<![\w-]){re.escape(flag)}(?=$|[\s,=/])"
    return re.search(pattern, help_text) is not None


def doctor_claude_runtime(
    config: RunConfig, *, runner_factory: RunnerFactory | None = None
) -> ClaudeRuntime:
    """Fail closed unless the resolved CLI reports a version and every v1 sealing flag.

    Both probes run through the same final subprocess seam, from disposable clean directories and
    with the exact allowlisted launch environment minus model credentials. They receive empty
    stdin and do not consume the model-call budget.
    """
    runtime = build_claude_runtime(config, runner_factory=runner_factory)
    factory = runner_factory if runner_factory is not None else _default_runner_factory
    run = factory()

    def probe(args: Sequence[str]) -> SubprocessResult:
        try:
            with tempfile.TemporaryDirectory(
                prefix="measure-twice-claude-doctor-", dir=_trusted_temp_root()
            ) as temp_cwd:
                cwd = Path(temp_cwd).resolve()
                return run(_probe_invocation(runtime, args, cwd), "", _DOCTOR_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaudeSetupError(f"Claude CLI doctor probe {list(args)!r} failed: {exc}") from exc

    version_result = probe(("--version",))
    if version_result.returncode != 0:
        raise ClaudeSetupError(f"Claude CLI --version failed (exit {version_result.returncode})")
    version_lines = [line.strip() for line in version_result.stdout.splitlines() if line.strip()]
    if not version_lines:
        raise ClaudeSetupError("Claude CLI --version returned no version text")

    help_result = probe(("--help",))
    if help_result.returncode != 0:
        raise ClaudeSetupError(f"Claude CLI --help failed (exit {help_result.returncode})")
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    unsupported = [
        flag for flag in _REQUIRED_CLAUDE_FLAGS if not _help_advertises_flag(help_text, flag)
    ]
    if unsupported:
        raise ClaudeSetupError(
            f"Claude CLI does not advertise required prompt-only flag(s): {unsupported!r}"
        )

    return ClaudeRuntime(
        executable=runtime.executable,
        context=runtime.context,
        env=runtime.env,
        probe_env=runtime.probe_env,
        command_processor=runtime.command_processor,
        cli_version=version_lines[0],
    )


def _is_truncation_subtype(subtype: object) -> bool:
    """True for a max-turns / length-cutoff error subtype (an ``is_error`` partial answer)."""
    if not isinstance(subtype, str):
        return False
    low = subtype.lower()
    return any(marker in low for marker in _TRUNCATION_SUBTYPE_MARKERS)


def claude_call(
    prompt: str,
    *,
    alias: str,
    config: RunConfig,
    budget: CallBudget,
    timeout: float | None = None,
    runner_factory: RunnerFactory | None = None,
    runtime: ClaudeRuntime | None = None,
) -> ModelCallResult:
    """Run one ``claude -p`` call and return a :class:`ModelCallResult`.

    Args:
        prompt: the full item prompt — fed on STDIN, never argv (WinError 206 avoidance).
        alias: the roster model alias to request (e.g. ``"sonnet"``); passed as ``--model``.
        config: run config (reserved for future per-model settings; ``claude_pool`` is used by
            :func:`claude_call_batch`).
        budget: the run call budget; consumed BEFORE spawning. Raises :class:`BudgetExhaustedError`
            if spent (no subprocess is launched in that case).
        timeout: per-call timeout in seconds; defaults to :data:`DEFAULT_CLAUDE_TIMEOUT_S`.
        runner_factory: the DI seam. ``None`` -> the real ``subprocess`` runner; tests inject a
            stub factory returning canned output.
        runtime: an already-doctored runtime supplied by the sweep runner. ``None`` doctors the
            default runtime before budget consumption or any model invocation.

    Never raises on a model subprocess/envelope failure — returns a structured ERROR result. CLI
    setup validation (:class:`ClaudeSetupError`) and :class:`BudgetExhaustedError` propagate before
    the model call so callers fail closed without recording a bogus result.
    """
    try:
        binding = config.execution_profile.binding_for(alias)
    except ExecutionProfileError as exc:
        raise AdapterError(str(exc)) from exc
    if binding.provider != PROVIDER_CLAUDE:
        raise AdapterError(
            f"model alias {alias!r} is bound to {binding.provider!r}, not {PROVIDER_CLAUDE!r}"
        )
    # A direct production caller (notably rubric scoring) does not have the sweep runner's runtime.
    # Doctor outside the broad per-call error conversion and before budget mutation: unsupported
    # flags/version must be a fail-closed setup halt, never an ``os_error`` row that scoring can
    # persist as zero. The runner passes its receipt-bound runtime and takes this fast path.
    eff_runtime = (
        runtime
        if runtime is not None
        else doctor_claude_runtime(config, runner_factory=runner_factory)
    )
    budget.consume()  # count + cap only after setup succeeds, still before the model subprocess.
    start = time.monotonic()
    resolved = UNRESOLVED_MODEL_ID
    # The ENTIRE post-budget body is wrapped, so the documented "never raises except
    # BudgetExhaustedError — returns a structured ERROR result" contract holds for the WHOLE
    # function, not just the subprocess spawn: json.loads, the is_error/subtype dispatch,
    # resolved_model_of, and ModelCallResult.success() (which itself raises AdapterError on
    # sentinel/empty text) are all inside. A fault ANYWHERE here would otherwise propagate out of
    # claude_call_batch and discard a pool wave's already-succeeded, budget-CONSUMED sibling
    # results (silent data loss — the runner appends a wave's rows only after the batch returns).
    # BudgetExhaustedError is raised ABOVE the try so it still propagates; KeyboardInterrupt /
    # SystemExit are BaseException (not Exception), so they propagate too.
    try:
        eff_timeout = timeout if timeout is not None else DEFAULT_CLAUDE_TIMEOUT_S
        factory = runner_factory if runner_factory is not None else _default_runner_factory
        run = factory()
        # Every call gets its own empty directory; no process-global chdir/env mutation means the
        # bounded pool remains race-free. Prompt goes on stdin, never argv.
        with tempfile.TemporaryDirectory(
            prefix="measure-twice-claude-call-", dir=_trusted_temp_root()
        ) as temp_cwd:
            invocation = build_claude_invocation(
                eff_runtime,
                requested_model=binding.requested_model,
                cwd=Path(temp_cwd).resolve(),
            )
            result = run(invocation, prompt, eff_timeout)
        elapsed = round(time.monotonic() - start, 3)

        try:
            doc = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            if result.returncode != 0:
                return ModelCallResult.error(
                    reason_class=RC_OS_ERROR, resolved_model=resolved, elapsed_s=elapsed
                )
            return ModelCallResult.error(reason_class=RC_NON_JSON_BODY, elapsed_s=elapsed)
        if not isinstance(doc, dict):
            if result.returncode != 0:
                return ModelCallResult.error(
                    reason_class=RC_OS_ERROR, resolved_model=resolved, elapsed_s=elapsed
                )
            return ModelCallResult.error(reason_class=RC_BAD_ENVELOPE, elapsed_s=elapsed)
        envelope = cast("dict[str, object]", doc)
        resolved = resolved_model_of(envelope, requested=binding.requested_model)

        if result.returncode != 0:
            # Parse only enough of a failed process's JSON object to retain observed provider
            # identity. Its payload is never eligible for success/no-response classification.
            return ModelCallResult.error(
                reason_class=RC_OS_ERROR, resolved_model=resolved, elapsed_s=elapsed
            )

        # is_error guard BEFORE trusting `result` (production _parse_envelope): an is_error
        # envelope's `result` is an ERROR MESSAGE, not model text, and can co-occur with exit code
        # 0. Scoring it as a model answer silently corrupts the ledger. A max-turns/length subtype =
        # truncated (partial answer cut off); anything else = a generic CLI error.
        if envelope.get("is_error") is True:
            if _is_truncation_subtype(envelope.get("subtype")):
                return ModelCallResult.error(
                    reason_class=RC_TRUNCATED, resolved_model=resolved, elapsed_s=elapsed
                )
            return ModelCallResult.error(
                reason_class=RC_OS_ERROR, resolved_model=resolved, elapsed_s=elapsed
            )

        text_raw = envelope.get("result")
        if not isinstance(text_raw, str):
            # Missing/renamed ``result`` on a non-error envelope -> it carries no usable text.
            return ModelCallResult.error(
                reason_class=RC_BAD_ENVELOPE, resolved_model=resolved, elapsed_s=elapsed
            )

        if not text_raw.strip():
            return ModelCallResult.no_response_result(resolved_model=resolved, elapsed_s=elapsed)
        return ModelCallResult.success(
            response_raw=text_raw, resolved_model=resolved, elapsed_s=elapsed
        )
    except subprocess.TimeoutExpired:
        return ModelCallResult.error(
            reason_class=RC_TIMEOUT,
            resolved_model=resolved,
            elapsed_s=round(time.monotonic() - start, 3),
        )
    except FileNotFoundError:
        # ``claude`` is not on PATH: the Claude tier is unreachable (switchboard-consistent).
        return ModelCallResult.error(
            reason_class=RC_UNREACHABLE,
            resolved_model=resolved,
            elapsed_s=round(time.monotonic() - start, 3),
        )
    except OSError:
        return ModelCallResult.error(
            reason_class=RC_OS_ERROR,
            resolved_model=resolved,
            elapsed_s=round(time.monotonic() - start, 3),
        )
    except Exception:
        # Any OTHER unclassified failure anywhere in the post-budget body (a non-OSError transport
        # error, a runner_factory bug, a post-processing fault in resolved_model_of / success()) ->
        # a structured os_error result, never a raised exception (see the block comment above).
        return ModelCallResult.error(
            reason_class=RC_OS_ERROR,
            resolved_model=resolved,
            elapsed_s=round(time.monotonic() - start, 3),
        )


def claude_call_batch(
    requests: Sequence[ClaudeRequest],
    *,
    config: RunConfig,
    budget: CallBudget,
    timeout: float | None = None,
    runner_factory: RunnerFactory | None = None,
    runtime: ClaudeRuntime | None = None,
) -> list[ModelCallResult | BudgetExhaustedError]:
    """Run N claude calls through a bounded pool (``config.claude_pool`` workers), IN INPUT ORDER.

    Bounded parallelism is **claude-only**; the local endpoint is called sequentially (see the
    module docstring). If ``len(requests) > budget.remaining`` some workers hit the cap: their slots
    hold a :class:`BudgetExhaustedError` MARKER while every call that DID spend budget still returns
    its real :class:`ModelCallResult` — a completed (budget-consuming) call is never discarded, so
    the runner (Step 4) loses no work and can resume from exactly the unspent slots. The list is in
    input order; each entry is a ``ModelCallResult`` OR a ``BudgetExhaustedError`` marker.
    """
    eff_runtime = (
        runtime
        if runtime is not None
        else doctor_claude_runtime(config, runner_factory=runner_factory)
    )
    with ThreadPoolExecutor(max_workers=config.claude_pool) as executor:
        futures = [
            executor.submit(
                claude_call,
                r.prompt,
                alias=r.alias,
                config=config,
                budget=budget,
                timeout=timeout,
                runner_factory=runner_factory,
                runtime=eff_runtime,
            )
            for r in requests
        ]
        results: list[ModelCallResult | BudgetExhaustedError] = []
        for f in futures:
            try:
                results.append(f.result())
            except BudgetExhaustedError as exc:
                # This slot's call was refused (budget spent) — record the marker, keep the
                # already-completed real results in the other slots rather than raising them away.
                results.append(exc)
        return results
