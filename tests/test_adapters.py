"""Adapter tests — OFFLINE, stub-factory driven, ZERO live calls (plan §5 Step-3 done-when).

Every model call is routed through an injected DI-seam stub (a fake ``urllib`` transport for the
local adapter, a fake ``subprocess`` runner for the claude adapter), so the suite touches NO
network and NEVER invokes the real ``claude`` binary. Coverage per the done-when:

  * local:  happy path + resolved-model capture + reasoning-content-ignored + every error class
            (unreachable / timeout / os_error / non_json_body / bad_envelope / truncated / empty).
  * claude: happy path + resolved-model capture + STDIN-not-argv + call-counting/budget cap +
            every error class (os_error / unreachable / timeout / non_json_body / bad_envelope /
            empty) + a contract test pinning the ``--output-format json`` envelope shape.
  * shared: the no-response sentinel, the result-state discrimination, and proof the reason_class
            taxonomy is switchboard's (imported, not re-declared).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from measure_twice.adapters import claude_cli
from measure_twice.adapters.base import (
    NO_RESPONSE,
    RC_BAD_ENVELOPE,
    RC_NON_JSON_BODY,
    RC_OS_ERROR,
    RC_TIMEOUT,
    RC_TRUNCATED,
    RC_UNREACHABLE,
    REASON_CLASSES,
    UNRESOLVED_MODEL_ID,
    AdapterError,
    ModelCallResult,
    resolved_model_of,
)
from measure_twice.adapters.claude_cli import (
    BudgetExhaustedError,
    CallBudget,
    ClaudeInvocation,
    ClaudeRequest,
    ClaudeSetupError,
    RunnerFactory,
    SubprocessResult,
    _kill_process_tree,
    _LauncherBoundary,
    _runtime_launcher_policy,
    _subprocess_runner,
    build_claude_runtime,
    claude_call,
    claude_call_batch,
    doctor_claude_runtime,
)
from measure_twice.adapters.local import TransportFactory, local_chat
from measure_twice.config import RunConfig
from measure_twice.model_sweep_execution import CLAUDE_ARGV_TEMPLATE

# --- Builders / stub factories -----------------------------------------------------------


def _openai_response(
    content: str,
    *,
    model: str = "general-35b",
    finish_reason: str = "stop",
    reasoning_content: str | None = None,
) -> str:
    """A well-formed OpenAI-compatible chat-completion body (JSON text)."""
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return json.dumps(
        {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


def _static_transport(body: str) -> TransportFactory:
    def transport(url: str, data: bytes, timeout: float) -> str:
        return body

    return lambda: transport


def _recording_transport(body: str, record: list[dict[str, object]]) -> TransportFactory:
    def transport(url: str, data: bytes, timeout: float) -> str:
        record.append({"url": url, "data": data, "timeout": timeout})
        return body

    return lambda: transport


def _raising_transport(exc: BaseException) -> TransportFactory:
    def transport(url: str, data: bytes, timeout: float) -> str:
        raise exc

    return lambda: transport


def _claude_envelope(
    result_text: object,
    *,
    model: str = "claude-sonnet-4-5-20260101",
    subtype: str = "success",
    is_error: bool | None = None,
) -> str:
    """A realistic ``claude -p --output-format json`` envelope (guide-confirmed key set).

    ``is_error`` defaults to ``subtype != "success"`` (the real CLI coupling) but can be forced
    independently to exercise the ``is_error`` guard against a specific ``subtype``.
    """
    err = (subtype != "success") if is_error is None else is_error
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": err,
            "duration_ms": 4200,
            "duration_api_ms": 3800,
            "num_turns": 1,
            "result": result_text,
            "session_id": "sess-abc-123",
            "total_cost_usd": 0.0123,
            "model": model,
            "modelUsage": {
                "input_tokens": 120,
                "output_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    )


def _doctor_probe_result(invocation: ClaudeInvocation) -> SubprocessResult | None:
    args = invocation.argv[1:]
    if args == ("--version",):
        return SubprocessResult(0, "test-claude 1.0", "")
    if args == ("--help",):
        return SubprocessResult(0, " ".join(CLAUDE_ARGV_TEMPLATE), "")
    return None


def _static_runner(result: SubprocessResult) -> RunnerFactory:
    def runner(invocation: ClaudeInvocation, input_text: str, timeout: float) -> SubprocessResult:
        probe = _doctor_probe_result(invocation)
        if probe is not None:
            return probe
        return result

    return lambda: runner


def _raising_runner(exc: BaseException) -> RunnerFactory:
    def runner(invocation: ClaudeInvocation, input_text: str, timeout: float) -> SubprocessResult:
        probe = _doctor_probe_result(invocation)
        if probe is not None:
            return probe
        raise exc

    return lambda: runner


# --- Shared: result type, sentinel, taxonomy reuse ---------------------------------------


def test_reason_classes_are_switchboards_imported_not_redeclared() -> None:
    """The taxonomy is switchboard's, imported (plan §8 D6 / code-quality one-source-of-truth)."""
    from switchboard.client import (
        RC_BAD_ENVELOPE as SB_BAD_ENVELOPE,
    )
    from switchboard.client import (
        RC_BAD_VERDICT as SB_BAD_VERDICT,
    )
    from switchboard.client import (
        RC_NON_JSON_BODY as SB_NON_JSON,
    )
    from switchboard.client import (
        RC_OS_ERROR as SB_OS,
    )
    from switchboard.client import (
        RC_TIMEOUT as SB_TIMEOUT,
    )
    from switchboard.client import (
        RC_TRUNCATED as SB_TRUNCATED,
    )
    from switchboard.client import (
        RC_UNREACHABLE as SB_UNREACHABLE,
    )

    assert REASON_CLASSES == {
        SB_UNREACHABLE,
        SB_TIMEOUT,
        SB_OS,
        SB_NON_JSON,
        SB_BAD_ENVELOPE,
        SB_TRUNCATED,
        SB_BAD_VERDICT,
    }


def test_no_response_sentinel_state() -> None:
    r = ModelCallResult.no_response_result(resolved_model="general-35b", elapsed_s=0.1)
    assert r.no_response is True
    assert r.ok is False
    assert r.is_error is False
    assert r.reason_class is None
    assert r.response_raw == NO_RESPONSE
    assert NO_RESPONSE != "any real model answer"  # a real answer never equals the sentinel


def test_success_state() -> None:
    r = ModelCallResult.success(response_raw="42", resolved_model="m", elapsed_s=0.2)
    assert r.ok is True
    assert r.no_response is False
    assert r.is_error is False


def test_error_state_and_rejects_unknown_reason_class() -> None:
    r = ModelCallResult.error(reason_class=RC_TIMEOUT, elapsed_s=0.0)
    assert r.is_error is True
    assert r.ok is False
    assert r.resolved_model == UNRESOLVED_MODEL_ID
    with pytest.raises(AdapterError):
        ModelCallResult.error(reason_class="not_a_switchboard_class", elapsed_s=0.0)


@pytest.mark.parametrize("observed", [None, "", "   ", 123, [], {}])
def test_resolved_model_requires_concrete_non_whitespace_provider_evidence(
    observed: object,
) -> None:
    assert resolved_model_of({"model": observed}, requested="must-not-be-inferred") == (
        UNRESOLVED_MODEL_ID
    )


def test_resolved_model_preserves_concrete_provider_value_verbatim() -> None:
    assert resolved_model_of({"model": "provider-concrete"}, requested="alias") == (
        "provider-concrete"
    )


def test_success_rejects_empty_or_sentinel_text() -> None:
    with pytest.raises(AdapterError):
        ModelCallResult.success(response_raw="   ", resolved_model="m", elapsed_s=0.0)
    with pytest.raises(AdapterError):
        ModelCallResult.success(response_raw=NO_RESPONSE, resolved_model="m", elapsed_s=0.0)


# --- Local adapter: happy path + config threading + reasoning handling -------------------


def test_local_happy_path_parses_text_and_resolved_model() -> None:
    body = _openai_response("42", model="general-35b-Q4_K_M")
    res = local_chat(
        "2+2?", model="general-35b", config=RunConfig(), transport_factory=_static_transport(body)
    )
    assert res.ok
    assert res.response_raw == "42"
    assert res.resolved_model == "general-35b-Q4_K_M"  # echoed model -> drift-detectable


def test_local_missing_provider_identity_is_explicitly_unresolved() -> None:
    payload = json.loads(_openai_response("42"))
    del payload["model"]
    res = local_chat(
        "2+2?",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_static_transport(json.dumps(payload)),
    )

    assert res.ok
    assert res.resolved_model == UNRESOLVED_MODEL_ID
    assert res.resolved_model != "general-35b"


def test_local_sends_config_base_url_and_max_tokens_not_hardcoded() -> None:
    cfg = RunConfig(local_base_url="http://localhost:8080/v1", local_max_tokens=2048)
    record: list[dict[str, object]] = []
    body = _openai_response("ok", model="general-35b")
    res = local_chat(
        "prompt-text",
        model="general-35b",
        config=cfg,
        transport_factory=_recording_transport(body, record),
    )
    assert res.ok
    assert record[0]["url"] == "http://localhost:8080/v1/chat/completions"
    sent = json.loads(bytes(record[0]["data"]).decode("utf-8"))  # type: ignore[arg-type]
    assert sent["max_tokens"] == 2048  # from config, not a hardcoded literal
    assert sent["model"] == "general-35b"
    assert sent["messages"][0]["content"] == "prompt-text"


def test_local_ignores_reasoning_content() -> None:
    """Read choices[0].message.content; IGNORE reasoning_content (switchboard gotcha)."""
    body = _openai_response(
        "FINAL ANSWER", reasoning_content="long chain of thought that must not leak"
    )
    res = local_chat(
        "q", model="general-35b", config=RunConfig(), transport_factory=_static_transport(body)
    )
    assert res.ok
    assert res.response_raw == "FINAL ANSWER"
    assert "chain of thought" not in res.response_raw


# --- Local adapter: every error / no-response class --------------------------------------


def test_local_unreachable_on_urlerror() -> None:
    res = local_chat(
        "q",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_raising_transport(urllib.error.URLError("connection refused")),
    )
    assert res.reason_class == RC_UNREACHABLE


def test_local_timeout_plain() -> None:
    res = local_chat(
        "q",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_raising_transport(TimeoutError()),
    )
    assert res.reason_class == RC_TIMEOUT


def test_local_timeout_wrapped_in_urlerror() -> None:
    res = local_chat(
        "q",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_raising_transport(urllib.error.URLError(TimeoutError())),
    )
    assert res.reason_class == RC_TIMEOUT


def test_local_os_error() -> None:
    res = local_chat(
        "q",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_raising_transport(OSError("socket boom")),
    )
    assert res.reason_class == RC_OS_ERROR


def test_local_non_json_body() -> None:
    res = local_chat(
        "q",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_static_transport("this is not json at all"),
    )
    assert res.reason_class == RC_NON_JSON_BODY


def test_local_non_utf8_transport_error_is_structured_not_raised() -> None:
    """NIT (iter 3): a non-OSError transport/decode failure (e.g. UnicodeDecodeError from a
    non-UTF-8 body — a ValueError, not OSError) must return a structured error result, honoring
    local_chat's "never raises on a transport/envelope failure" contract (would propagate + abort
    the sweep without the widened catch)."""
    decode_err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    res = local_chat(
        "q",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_raising_transport(decode_err),
    )
    assert res.is_error
    assert res.reason_class == RC_NON_JSON_BODY  # non-decodable body -> unusable envelope


def test_local_bad_envelope_missing_choices() -> None:
    res = local_chat(
        "q",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_static_transport(json.dumps({"id": "x", "model": "m"})),
    )
    assert res.reason_class == RC_BAD_ENVELOPE
    assert res.resolved_model == "m"


def test_local_bad_envelope_non_dict_json() -> None:
    res = local_chat(
        "q",
        model="general-35b",
        config=RunConfig(),
        transport_factory=_static_transport(json.dumps([1, 2, 3])),
    )
    assert res.reason_class == RC_BAD_ENVELOPE


def test_local_truncated_nonempty_finish_length() -> None:
    """Non-empty content cut off at finish_reason=length -> the switchboard `truncated` class."""
    body = _openai_response("partial answ", finish_reason="length")
    res = local_chat(
        "q", model="general-35b", config=RunConfig(), transport_factory=_static_transport(body)
    )
    assert res.reason_class == RC_TRUNCATED


def test_local_reasoning_only_empty_content_is_no_response() -> None:
    """content='' (all tokens spent on reasoning, finish=length) -> no-response, NOT truncated."""
    body = _openai_response(
        "", finish_reason="length", reasoning_content="spent every token reasoning"
    )
    res = local_chat(
        "q", model="general-35b", config=RunConfig(), transport_factory=_static_transport(body)
    )
    assert res.no_response is True
    assert res.reason_class is None
    assert res.response_raw == NO_RESPONSE


def test_local_whitespace_content_is_no_response() -> None:
    body = _openai_response("   \n\t  ")
    res = local_chat(
        "q", model="general-35b", config=RunConfig(), transport_factory=_static_transport(body)
    )
    assert res.no_response is True


# --- Claude adapter: happy path + STDIN + budget -----------------------------------------


def test_claude_happy_path_captures_text_and_resolved_model() -> None:
    factory = _static_runner(
        SubprocessResult(0, _claude_envelope("VERDICT: pass", model="claude-sonnet-4-5-xyz"), "")
    )
    res = claude_call(
        "judge this",
        alias="sonnet",
        config=RunConfig(),
        budget=CallBudget(5),
        runner_factory=factory,
    )
    assert res.ok
    assert res.response_raw == "VERDICT: pass"
    assert res.resolved_model == "claude-sonnet-4-5-xyz"  # requested "sonnet" resolved concretely


def test_claude_rejects_a_non_claude_provider_binding_before_budget_or_spawn() -> None:
    budget = CallBudget(5)
    spawned = False

    def factory() -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("runner factory must not be reached")

    with pytest.raises(AdapterError, match="not 'claude-cli'"):
        claude_call(
            "q",
            alias="general-35b",
            config=RunConfig(),
            budget=budget,
            runner_factory=factory,  # type: ignore[arg-type]
        )

    assert budget.used == 0
    assert spawned is False


def test_claude_call_default_runtime_doctor_fails_before_budget_or_model_call() -> None:
    """A direct caller cannot downgrade a setup failure into a persisted model-error result."""
    budget = CallBudget(5)
    invocations: list[tuple[tuple[str, ...], str]] = []

    def runner(invocation: ClaudeInvocation, input_text: str, timeout: float) -> SubprocessResult:
        invocations.append((invocation.argv[1:], input_text))
        if invocation.argv[1:] == ("--version",):
            return SubprocessResult(0, "test-claude 1.0", "")
        if invocation.argv[1:] == ("--help",):
            return SubprocessResult(0, "-p --model --output-format", "")
        raise AssertionError("model prompt must not run after a failed doctor")

    with pytest.raises(ClaudeSetupError, match="does not advertise required prompt-only flag"):
        claude_call(
            "must-not-run",
            alias="sonnet",
            config=RunConfig(),
            budget=budget,
            runner_factory=lambda: runner,
        )

    assert invocations == [(("--version",), ""), (("--help",), "")]
    assert budget.used == 0


def test_claude_batch_default_runtime_doctor_fails_before_budget_or_model_call() -> None:
    """The batch convenience path owns the same pre-budget doctor invariant as one call."""
    budget = CallBudget(5)
    invocations: list[tuple[str, ...]] = []

    def runner(invocation: ClaudeInvocation, input_text: str, timeout: float) -> SubprocessResult:
        invocations.append(invocation.argv[1:])
        if invocation.argv[1:] == ("--version",):
            return SubprocessResult(0, "test-claude 1.0", "")
        if invocation.argv[1:] == ("--help",):
            return SubprocessResult(0, "-p --model --output-format", "")
        raise AssertionError("batch model prompt must not run after a failed doctor")

    with pytest.raises(ClaudeSetupError, match="does not advertise required prompt-only flag"):
        claude_call_batch(
            [ClaudeRequest(prompt="must-not-run", alias="sonnet")],
            config=RunConfig(),
            budget=budget,
            runner_factory=lambda: runner,
        )

    assert invocations == [("--version",), ("--help",)]
    assert budget.used == 0


def test_production_launcher_inventory_is_code_owned_not_ambient_root_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "hostile-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "hostile-profile"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "hostile-appdata"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "hostile-program-files"))

    policy = _runtime_launcher_policy()

    assert policy
    assert all(boundary.launcher_directory.is_absolute() for boundary in policy)
    assert all(not boundary.launcher_directory.is_relative_to(tmp_path) for boundary in policy)
    if sys.platform == "win32":
        paths = claude_cli._windows_runtime_paths()
        assert tuple(boundary.launcher_directory for boundary in policy) == (
            paths.profile / ".local" / "bin",
            paths.roaming_app_data / "npm",
            paths.program_files / "nodejs",
            paths.program_data / "chocolatey" / "bin",
        )
        assert paths.profile / ".local" / "share" / "claude" / "versions" in policy[0].target_roots
    else:
        account_home = claude_cli._posix_account_home()
        assert policy[0].launcher_directory == account_home / ".local" / "bin"
        assert account_home / ".local" / "share" / "claude" / "versions" in policy[0].target_roots


def test_claude_runtime_skips_hostile_path_and_uses_injected_approved_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hermetic policy seam proves hostile-first selection without blessing tmp in production."""
    checkout = tmp_path / "checkout"
    hostile_bin = checkout / ".venv" / "Scripts"
    approved_root = tmp_path / "injected-approved-install"
    approved_bin = approved_root / "bin"
    hostile_bin.mkdir(parents=True)
    approved_bin.mkdir(parents=True)
    launcher_name = "claude.exe" if os.name == "nt" else "claude"
    hostile_launcher = hostile_bin / launcher_name
    trusted_launcher = approved_bin / launcher_name
    for launcher in (hostile_launcher, trusted_launcher):
        launcher.write_text("not executed", encoding="utf-8")
        if os.name != "nt":
            launcher.chmod(0o755)

    boundary = _LauncherBoundary(
        launcher_directory=approved_bin,
        target_roots=(approved_root,),
        dependency_directories=(),
    )
    monkeypatch.setattr(claude_cli, "_runtime_launcher_policy", lambda: (boundary,))

    monkeypatch.chdir(checkout)
    monkeypatch.setenv("PATH", os.pathsep.join((str(hostile_bin), str(approved_bin))))
    allowed_marker = "offline-dummy-value"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", allowed_marker)
    monkeypatch.setenv("COMSPEC", str(hostile_bin / "cmd.exe"))
    monkeypatch.setenv("TEMP", str(checkout / "hostile-temp"))

    runtime = build_claude_runtime(RunConfig())

    assert runtime.executable == str(trusted_launcher.resolve())
    assert runtime.env["PATH"].split(os.pathsep)[0] == str(approved_bin.resolve())
    assert str(hostile_bin.resolve()) not in runtime.env["PATH"].split(os.pathsep)
    assert runtime.env["CLAUDE_CODE_OAUTH_TOKEN"] == allowed_marker
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in runtime.probe_env
    assert not Path(runtime.env["TEMP"]).is_relative_to(checkout)
    if sys.platform == "win32":
        expected_cmd = claude_cli._windows_runtime_paths().windows / "System32" / "cmd.exe"
        assert runtime.env["COMSPEC"] == str(expected_cmd.resolve())


def test_hostile_only_path_fails_before_runner_creation_or_secret_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile_bin = tmp_path / "hostile-bin"
    approved_root = tmp_path / "approved-install"
    approved_bin = approved_root / "bin"
    hostile_bin.mkdir()
    approved_bin.mkdir(parents=True)
    launcher_name = "claude.exe" if os.name == "nt" else "claude"
    hostile_launcher = hostile_bin / launcher_name
    hostile_launcher.write_text("not executed", encoding="utf-8")
    if os.name != "nt":
        hostile_launcher.chmod(0o755)
    boundary = _LauncherBoundary(approved_bin, (approved_root,), ())
    monkeypatch.setattr(claude_cli, "_runtime_launcher_policy", lambda: (boundary,))
    monkeypatch.setenv("PATH", str(hostile_bin))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-leak")
    factory_calls: list[str] = []

    def forbidden_factory() -> object:
        factory_calls.append("created")
        raise AssertionError("runner must not be created")

    monkeypatch.setattr(claude_cli, "_default_runner_factory", forbidden_factory)
    before = {path.name for path in tmp_path.iterdir()}

    with pytest.raises(ClaudeSetupError, match="approved runtime install root"):
        doctor_claude_runtime(RunConfig())

    assert factory_calls == []
    assert {path.name for path in tmp_path.iterdir()} == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink policy")
def test_posix_native_symlink_target_must_remain_in_approved_versions_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved_root = tmp_path / "dot-local"
    approved_bin = approved_root / "bin"
    approved_versions = approved_root / "share" / "claude" / "versions"
    approved_bin.mkdir(parents=True)
    approved_versions.mkdir(parents=True)
    versioned = approved_versions / "1.2.3"
    versioned.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    versioned.chmod(0o755)
    (approved_bin / "claude").symlink_to(versioned)
    boundary = _LauncherBoundary(
        approved_bin,
        (approved_bin, approved_versions),
        (Path("/usr/bin"), Path("/bin")),
    )
    monkeypatch.setattr(claude_cli, "_runtime_launcher_policy", lambda: (boundary,))
    monkeypatch.setenv("PATH", str(approved_bin))

    runtime = build_claude_runtime(RunConfig())

    assert runtime.executable == str(versioned.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink policy")
def test_posix_native_symlink_escape_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved_root = tmp_path / "dot-local"
    approved_bin = approved_root / "bin"
    approved_bin.mkdir(parents=True)
    escaped = tmp_path / "escaped-claude"
    escaped.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    escaped.chmod(0o755)
    (approved_bin / "claude").symlink_to(escaped)
    boundary = _LauncherBoundary(approved_bin, (approved_root,), ())
    monkeypatch.setattr(claude_cli, "_runtime_launcher_policy", lambda: (boundary,))
    monkeypatch.setenv("PATH", str(approved_bin))

    with pytest.raises(ClaudeSetupError, match="approved runtime install root"):
        build_claude_runtime(RunConfig())


def test_claude_prompt_passed_via_stdin_not_argv() -> None:
    """The prompt MUST arrive on STDIN, never argv (Windows argv >32K = WinError 206)."""
    big_prompt = "PROMPT-" + "x" * 50_000  # far past the 32K argv ceiling
    calls: list[dict[str, object]] = []
    all_calls: list[tuple[tuple[str, ...], str, dict[str, str]]] = []

    def runner(invocation: ClaudeInvocation, input_text: str, timeout: float) -> SubprocessResult:
        all_calls.append((invocation.argv, input_text, dict(invocation.env)))
        probe = _doctor_probe_result(invocation)
        if probe is not None:
            return probe
        calls.append(
            {
                "argv": list(invocation.argv),
                "input": input_text,
                "cwd": invocation.cwd,
                "env": dict(invocation.env),
            }
        )
        return SubprocessResult(0, _claude_envelope("done"), "")

    res = claude_call(
        big_prompt,
        alias="opus",
        config=RunConfig(),
        budget=CallBudget(5),
        runner_factory=lambda: runner,
    )
    assert res.ok
    rec = calls[0]
    assert rec["input"] == big_prompt  # prompt went on stdin
    argv = list(rec["argv"])  # type: ignore[arg-type]
    assert big_prompt not in argv
    assert not any(big_prompt in str(tok) for tok in argv)  # ...and not embedded in any arg
    assert Path(argv[0]).is_absolute()
    assert argv[1:] == [
        "-p",
        "--model",
        "opus",
        "--output-format",
        "json",
        "--safe-mode",
        "--tools",
        "",
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
    ]
    doctor_calls = [call for call in all_calls if call[1] == ""]
    assert len(doctor_calls) == 2
    assert all("CLAUDE_CODE_OAUTH_TOKEN" not in environment for _, _, environment in doctor_calls)
    assert all(input_text == "" for _, input_text, _ in doctor_calls)
    assert isinstance(rec["cwd"], Path)
    assert not Path(rec["cwd"]).exists()  # unique disposable cwd is removed after the call


def test_claude_budget_counts_each_call_and_caps() -> None:
    budget = CallBudget(max_calls=2)
    factory = _static_runner(SubprocessResult(0, _claude_envelope("ok"), ""))
    r1 = claude_call("a", alias="haiku", config=RunConfig(), budget=budget, runner_factory=factory)
    r2 = claude_call("b", alias="haiku", config=RunConfig(), budget=budget, runner_factory=factory)
    assert r1.ok and r2.ok
    assert budget.used == 2
    with pytest.raises(BudgetExhaustedError):
        claude_call("c", alias="haiku", config=RunConfig(), budget=budget, runner_factory=factory)
    assert budget.used == 2  # a refused (over-budget) call does not increment


# --- Claude adapter: every error / no-response class -------------------------------------


def test_claude_nonzero_exit_is_os_error() -> None:
    factory = _static_runner(SubprocessResult(2, "", "some CLI error"))
    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.reason_class == RC_OS_ERROR
    assert res.resolved_model == UNRESOLVED_MODEL_ID


def test_claude_nonzero_exit_json_preserves_observed_provider_identity() -> None:
    factory = _static_runner(
        SubprocessResult(
            2,
            json.dumps({"type": "result", "is_error": True, "model": "claude-concrete"}),
            "must-not-be-propagated",
        )
    )

    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )

    assert res.reason_class == RC_OS_ERROR
    assert res.resolved_model == "claude-concrete"


def test_claude_binary_not_found_is_unreachable() -> None:
    res = claude_call(
        "q",
        alias="opus",
        config=RunConfig(),
        budget=CallBudget(5),
        runner_factory=_raising_runner(FileNotFoundError("claude")),
    )
    assert res.reason_class == RC_UNREACHABLE
    assert res.resolved_model == UNRESOLVED_MODEL_ID


def test_claude_other_oserror_is_os_error() -> None:
    res = claude_call(
        "q",
        alias="opus",
        config=RunConfig(),
        budget=CallBudget(5),
        runner_factory=_raising_runner(PermissionError("denied")),
    )
    assert res.reason_class == RC_OS_ERROR


def test_claude_timeout() -> None:
    res = claude_call(
        "q",
        alias="opus",
        config=RunConfig(),
        budget=CallBudget(5),
        runner_factory=_raising_runner(subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0)),
    )
    assert res.reason_class == RC_TIMEOUT


def test_claude_non_json_stdout() -> None:
    factory = _static_runner(SubprocessResult(0, "not json output", ""))
    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.reason_class == RC_NON_JSON_BODY
    assert res.resolved_model == UNRESOLVED_MODEL_ID


def test_claude_bad_envelope_missing_result_key() -> None:
    factory = _static_runner(SubprocessResult(0, json.dumps({"type": "result", "model": "m"}), ""))
    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.reason_class == RC_BAD_ENVELOPE


def test_claude_bad_envelope_non_dict_json() -> None:
    factory = _static_runner(SubprocessResult(0, json.dumps(["a", "b"]), ""))
    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.reason_class == RC_BAD_ENVELOPE
    assert res.resolved_model == UNRESOLVED_MODEL_ID


def test_claude_post_extraction_exception_preserves_observed_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _static_runner(
        SubprocessResult(
            0,
            _claude_envelope(
                "error text",
                model="claude-concrete",
                subtype="error_during_execution",
                is_error=True,
            ),
            "",
        )
    )

    def broken_classifier(_subtype: object) -> bool:
        raise RuntimeError("post-extraction classification fault")

    monkeypatch.setattr(claude_cli, "_is_truncation_subtype", broken_classifier)

    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )

    assert res.reason_class == RC_OS_ERROR
    assert res.resolved_model == "claude-concrete"


def test_claude_null_result_non_error_preserves_resolved_model_on_bad_envelope() -> None:
    """Malformed text retains concrete provider identity already present in the envelope."""
    factory = _static_runner(
        SubprocessResult(0, _claude_envelope(None, subtype="success", is_error=False), "")
    )
    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.reason_class == RC_BAD_ENVELOPE
    assert res.resolved_model == "claude-sonnet-4-5-20260101"


def test_claude_is_error_with_message_is_os_error() -> None:
    """BLOCK 2: an is_error envelope's `result` is an ERROR MESSAGE (co-occurring with exit 0);
    it must classify as an error, NEVER be scored as a real model answer (silent ledger corruption).
    """
    factory = _static_runner(
        SubprocessResult(
            0,  # exit code 0 despite is_error: true — the exact production footgun
            _claude_envelope(
                "Error: the Bash tool requires permission that was denied",
                subtype="error_during_execution",
                is_error=True,
            ),
            "",
        )
    )
    res = claude_call(
        "q", alias="opus", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.reason_class == RC_OS_ERROR
    assert res.resolved_model == "claude-sonnet-4-5-20260101"
    assert res.ok is False
    assert res.response_raw == ""  # the error message is NOT captured as model text


def test_claude_error_max_turns_is_truncated() -> None:
    """BLOCK 3: the truncation signal is subtype=error_max_turns (an is_error partial answer)."""
    factory = _static_runner(
        SubprocessResult(
            0,
            _claude_envelope(
                "partial answer before the turn limit", subtype="error_max_turns", is_error=True
            ),
            "",
        )
    )
    res = claude_call(
        "q", alias="opus", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.reason_class == RC_TRUNCATED
    assert res.resolved_model == "claude-sonnet-4-5-20260101"


def test_claude_empty_result_is_no_response() -> None:
    factory = _static_runner(SubprocessResult(0, _claude_envelope(""), ""))
    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.no_response is True
    assert res.reason_class is None
    assert res.resolved_model == "claude-sonnet-4-5-20260101"


def test_claude_whitespace_result_is_no_response() -> None:
    factory = _static_runner(SubprocessResult(0, _claude_envelope("  \n  "), ""))
    res = claude_call(
        "q", alias="haiku", config=RunConfig(), budget=CallBudget(5), runner_factory=factory
    )
    assert res.no_response is True


# --- Claude adapter: envelope CONTRACT test (pins the JSON shape; plan §9 drift risk) -----


def test_claude_envelope_contract_pins_json_shape() -> None:
    """Pin the exact ``--output-format json`` shape the unwrap depends on: ``result`` (text) and
    ``model`` (resolved id). If a CLI update renames the text key, DRIFT GUARD 1 fails loudly."""
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 4200,
        "num_turns": 1,
        "result": "the assistant answer",
        "session_id": "sess-1",
        "total_cost_usd": 0.01,
        "model": "claude-opus-4-8-20260101",
        "modelUsage": {"input_tokens": 5, "output_tokens": 3},
    }
    res = claude_call(
        "q",
        alias="opus",
        config=RunConfig(),
        budget=CallBudget(5),
        runner_factory=_static_runner(SubprocessResult(0, json.dumps(envelope), "")),
    )
    assert res.ok
    assert res.response_raw == "the assistant answer"  # from top-level `result`
    assert res.resolved_model == "claude-opus-4-8-20260101"  # from top-level `model`

    # DRIFT GUARD 1: a renamed/removed `result` key surfaces as bad_envelope, not a silent pass.
    broken = {k: v for k, v in envelope.items() if k != "result"}
    res2 = claude_call(
        "q",
        alias="opus",
        config=RunConfig(),
        budget=CallBudget(5),
        runner_factory=_static_runner(SubprocessResult(0, json.dumps(broken), "")),
    )
    assert res2.reason_class == RC_BAD_ENVELOPE

    # DRIFT GUARD 2: a missing `model` is unresolved evidence, never the requested alias.
    no_model = {k: v for k, v in envelope.items() if k != "model"}
    res3 = claude_call(
        "q",
        alias="opus",
        config=RunConfig(),
        budget=CallBudget(5),
        runner_factory=_static_runner(SubprocessResult(0, json.dumps(no_model), "")),
    )
    assert res3.ok
    assert res3.resolved_model == UNRESOLVED_MODEL_ID


# --- Claude adapter: bounded pool ---------------------------------------------------------


def test_claude_batch_preserves_input_order_and_counts_budget() -> None:
    budget = CallBudget(max_calls=10)

    def runner(invocation: ClaudeInvocation, input_text: str, timeout: float) -> SubprocessResult:
        probe = _doctor_probe_result(invocation)
        if probe is not None:
            return probe
        return SubprocessResult(0, _claude_envelope(f"echo:{input_text}", model="claude-x"), "")

    reqs = [ClaudeRequest(prompt=f"p{i}", alias="sonnet") for i in range(5)]
    results = claude_call_batch(
        reqs, config=RunConfig(claude_pool=2), budget=budget, runner_factory=lambda: runner
    )
    assert [r.response_raw for r in results] == [f"echo:p{i}" for i in range(5)]
    assert budget.used == 5


def test_claude_batch_runs_up_to_pool_size_concurrently() -> None:
    """Bounded parallelism: config.claude_pool calls run at once. A Barrier(pool) only releases if
    at least `pool` workers are in flight simultaneously — proving the pool is genuinely parallel
    (a sequential impl would deadlock the barrier and raise BrokenBarrierError)."""
    pool = 3
    barrier = threading.Barrier(pool, timeout=10)

    def runner(invocation: ClaudeInvocation, input_text: str, timeout: float) -> SubprocessResult:
        probe = _doctor_probe_result(invocation)
        if probe is not None:
            return probe
        barrier.wait()  # rendezvous: needs `pool` concurrent workers to proceed
        return SubprocessResult(0, _claude_envelope(f"ok:{input_text}"), "")

    reqs = [ClaudeRequest(prompt=f"p{i}", alias="haiku") for i in range(pool)]
    results = claude_call_batch(
        reqs,
        config=RunConfig(claude_pool=pool),
        budget=CallBudget(10),
        runner_factory=lambda: runner,
    )
    assert len(results) == pool
    assert all(isinstance(r, ModelCallResult) and r.ok for r in results)


def test_claude_batch_over_budget_keeps_completed_and_marks_rest() -> None:
    """NIT 1: a batch larger than remaining budget returns every completed (budget-spending) result
    PLUS a BudgetExhaustedError marker per refused slot — no completed real call is discarded, and
    no exception propagates out of the batch (Step-4 resume relies on losing no spent work)."""
    budget = CallBudget(max_calls=3)  # only 3 of the 5 requests can spend budget

    def runner(invocation: ClaudeInvocation, input_text: str, timeout: float) -> SubprocessResult:
        probe = _doctor_probe_result(invocation)
        if probe is not None:
            return probe
        return SubprocessResult(0, _claude_envelope("ok"), "")

    reqs = [ClaudeRequest(prompt=f"p{i}", alias="sonnet") for i in range(5)]
    results = claude_call_batch(
        reqs, config=RunConfig(claude_pool=2), budget=budget, runner_factory=lambda: runner
    )
    completed = [r for r in results if isinstance(r, ModelCallResult)]
    refused = [r for r in results if isinstance(r, BudgetExhaustedError)]
    assert len(results) == 5
    assert len(completed) == 3  # every budget-spending call survived (not thrown away)
    assert len(refused) == 2  # the over-cap slots are marked, not lost
    assert all(r.ok for r in completed)
    assert budget.used == 3


# --- Real LOCAL-subprocess coverage (NOT live claude/network — a hermetic python echo/sleep) ----


def test_subprocess_runner_utf8_roundtrip_is_byte_faithful(tmp_path: Path) -> None:
    """BLOCK 1: the default runner pins UTF-8, so non-cp1252 text round-trips byte-faithfully on
    Windows (would crash with UnicodeEncodeError or mojibake under the cp1252 default). Exercised
    against a real LOCAL python echo subprocess — this is NOT a live claude or network call."""
    # Built from backslash-u escapes (ASCII source): em-dash, e-acute, emoji, CJK, quotes.
    tricky = "em-dash — café 😀 中文 curly “quote”"
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stdin.reconfigure(encoding='utf-8'); "
        "sys.stdout.reconfigure(encoding='utf-8'); sys.stdout.write(sys.stdin.read())",
    ]
    invocation = ClaudeInvocation(argv=tuple(argv), cwd=tmp_path, env=os.environ)
    res = _subprocess_runner(invocation, tricky, 30.0)
    assert res.returncode == 0
    assert res.stdout == tricky  # no encode crash, no mojibake corruption of the benchmark text


def test_subprocess_runner_timeout_kills_and_raises(tmp_path: Path) -> None:
    """BLOCK 4: on timeout the default runner tree-kills the child and re-raises TimeoutExpired
    (real LOCAL python sleep — proves the timeout+kill path fires and does not hang)."""
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    invocation = ClaudeInvocation(argv=tuple(argv), cwd=tmp_path, env=os.environ)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _subprocess_runner(invocation, "", 1.0)
    # Liveness: the kill path must return promptly, not wait out the child's 30s sleep. A
    # tree-kill regression would make this ~30s instead of failing red; assert it stays bounded.
    assert time.monotonic() - started < 15.0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill chain")
def test_windows_timeout_cleanup_pins_system_taskkill_cwd_and_credential_free_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def kill() -> None:
            raise AssertionError("canonical taskkill path should be available")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-reach-taskkill")
    monkeypatch.setenv("COMSPEC", r"C:\hostile\cmd.exe")
    monkeypatch.setattr(subprocess, "run", fake_run)

    _kill_process_tree(FakeProcess())  # type: ignore[arg-type]

    assert len(calls) == 1
    argv, kwargs = calls[0]
    paths = claude_cli._windows_runtime_paths()
    system32 = (paths.windows / "System32").resolve()
    assert argv == [str(system32 / "taskkill.exe"), "/T", "/F", "/PID", "4242"]
    assert kwargs["cwd"] == system32
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environment
    assert environment["COMSPEC"] == str(system32 / "cmd.exe")
    assert environment["PATH"] == str(system32)
