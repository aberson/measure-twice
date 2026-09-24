"""Gemini adapter: protocol/error/security anchors + real CLI-to-store-to-report integration.

The offline protocol tests drive the production response parser through an injected transport with
deterministic response bytes (never the network). The integration tests drive ``main(...)`` — real
config, adapter, runner, scoring, and every report format — with the same seam plus a test
credential provider, so no live Gemini call and no real API key are ever needed (plan §6 D6).
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    GEMINI_TEST_KEY,
    StubAdapters,
    _gemini_body,
    _iid,
    gemini_test_credential,
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
)
from measure_twice.adapters.gemini import (
    GeminiCredentialError,
    _urllib_post,
    gemini_generate,
    resolve_gemini_credential,
)
from measure_twice.cli import CliDeps, main
from measure_twice.config import ENV_VAR, RunConfig, load_config
from measure_twice.model_sweep_execution import (
    DEFAULT_GEMINI_CONTEXT,
    GEMINI_REQUEST_CONTRACT,
    ExecutionReceipt,
    GeminiContextProfile,
    ModelSweepExecutionProfile,
)
from measure_twice.report import build_run_report, render_run_report, run_report_jsonl
from measure_twice.report_html import build_transparency_report, render_transparency_report
from measure_twice.runner import RunError, run
from measure_twice.scoring import make_deterministic_scorer
from measure_twice.suite import Item, ScoringSpec, Suite

ROOT = Path(__file__).resolve().parents[1]
GEMINI_PROFILE_PATH = ROOT / "profiles" / "model-sweep-gemini-v1.json"
GEMINI_MODEL = "gemini-3.8-flash"


# --- helpers ------------------------------------------------------------------------------


def _transport(body: object, *, capture: dict[str, Any] | None = None) -> Callable[[], object]:
    """A Gemini transport factory returning ``body`` (a raw JSON str) or raising it if an exc."""

    def factory() -> object:
        def transport(url: str, data: bytes, api_key: str, timeout: float) -> str:
            if capture is not None:
                capture["url"] = url
                capture["key"] = api_key
                capture["body"] = json.loads(data.decode("utf-8"))
                capture["timeout"] = timeout
            if isinstance(body, BaseException):
                raise body
            return str(body)

        return transport

    return factory


def _call(
    body: object,
    *,
    context: GeminiContextProfile = DEFAULT_GEMINI_CONTEXT,
    requested_model: str = GEMINI_MODEL,
    capture: dict[str, Any] | None = None,
) -> ModelCallResult:
    return gemini_generate(
        "solve it",
        requested_model=requested_model,
        context=context,
        api_key=GEMINI_TEST_KEY,
        transport_factory=_transport(body, capture=capture),  # type: ignore[arg-type]
    )


def _resp(candidates: object, *, model: str = "gemini-x", **extra: object) -> str:
    payload: dict[str, object] = {"modelVersion": model}
    if candidates is not None:
        payload["candidates"] = candidates
    payload.update(extra)
    return json.dumps(payload)


def _cand(finish: str = "STOP", parts: object = None) -> dict[str, object]:
    candidate: dict[str, object] = {"finishReason": finish}
    if parts is not None:
        candidate["content"] = {"parts": parts}
    return candidate


def _verdict_suite(item_ids: tuple[str, ...] = ("g-a", "g-b")) -> Suite:
    items = [
        Item(
            id=iid,
            tags=["t"],
            prompt=f"PROMPT::{iid}",
            expected="pass" if i == 0 else "flag",
            difficulty_prior=0.5,
            provenance="authored",
        )
        for i, iid in enumerate(item_ids)
    ]
    return Suite(
        suite="gemsuite",
        version=1,
        description="d",
        domain="dom",
        scoring=ScoringSpec(type="verdict", labels=["pass", "flag"]),
        items=items,
    )


def _gemini_config() -> RunConfig:
    return load_config(str(GEMINI_PROFILE_PATH))


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# --- protocol / classification anchors (each tied to a distinct outcome) -------------------


def test_success_reads_answer_text_and_resolved_identity() -> None:
    result = _call(_resp([_cand("STOP", [{"text": "pass"}])], model="gemini-3.8-flash-001"))
    assert result.ok
    assert result.response_raw == "pass"
    assert result.resolved_model == "gemini-3.8-flash-001"


def test_thought_parts_are_excluded_from_the_answer() -> None:
    body = _resp([_cand("STOP", [{"text": "reasoning", "thought": True}, {"text": "final"}])])
    result = _call(body)
    assert result.ok
    assert result.response_raw == "final"


def test_thought_only_response_is_no_response() -> None:
    result = _call(_resp([_cand("STOP", [{"text": "just thinking", "thought": True}])]))
    assert result.no_response


def test_empty_answer_text_is_no_response() -> None:
    assert _call(_resp([_cand("STOP", [{"text": "   "}])])).no_response


def test_prompt_block_without_candidates_is_no_response() -> None:
    result = _call(_resp(None, promptFeedback={"blockReason": "SAFETY"}))
    assert result.no_response


def test_prompt_block_cannot_score_a_candidate_answer() -> None:
    body = _resp([_cand("STOP", [{"text": "pass"}])], promptFeedback={"blockReason": "SAFETY"})
    assert _call(body).no_response


def test_unknown_prompt_block_without_candidates_is_bad_envelope() -> None:
    result = _call(_resp(None, promptFeedback={"blockReason": "UNKNOWN"}))
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_malformed_prompt_feedback_with_answer_is_bad_envelope() -> None:
    body = _resp([_cand("STOP", [{"text": "pass"}])], promptFeedback={"blockReason": 23})
    result = _call(body)
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_candidate_safety_finish_is_no_response() -> None:
    assert _call(_resp([_cand("SAFETY", None)])).no_response


def test_max_tokens_with_partial_answer_is_truncated_and_keeps_identity() -> None:
    result = _call(_resp([_cand("MAX_TOKENS", [{"text": "partial answer"}])], model="gm-v9"))
    assert result.is_error and result.reason_class == RC_TRUNCATED
    assert result.resolved_model == "gm-v9"  # identity retained even on a later error (plan §6 D4)


def test_max_tokens_with_thought_only_is_no_response() -> None:
    body = _resp([_cand("MAX_TOKENS", [{"text": "thinking hard", "thought": True}])])
    assert _call(body).no_response


def test_unsupported_content_part_is_bad_envelope() -> None:
    result = _call(_resp([_cand("STOP", [{"functionCall": {"name": "x"}}])]))
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_unsupported_thought_part_is_bad_envelope() -> None:
    parts = [{"thought": True, "functionCall": {"name": "x"}}, {"text": "pass"}]
    result = _call(_resp([_cand("STOP", parts)]))
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_null_thought_flag_is_bad_envelope() -> None:
    result = _call(_resp([_cand("STOP", [{"text": "pass", "thought": None}])]))
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_unexpected_finish_reason_on_text_is_bad_envelope() -> None:
    result = _call(_resp([_cand("WEIRD_REASON", [{"text": "hello"}])]))
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_unexpected_finish_reason_on_empty_answer_is_bad_envelope() -> None:
    result = _call(_resp([_cand("WEIRD_REASON", [])]))
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_missing_candidate_structure_without_block_is_bad_envelope() -> None:
    assert _call(_resp([])).reason_class == RC_BAD_ENVELOPE
    assert _call(_resp(None)).reason_class == RC_BAD_ENVELOPE


@pytest.mark.parametrize(
    "candidate", [{"finishReason": "STOP"}, {"finishReason": "STOP", "content": {}}]
)
def test_missing_answer_structure_is_bad_envelope(candidate: dict[str, object]) -> None:
    result = _call(_resp([candidate]))
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_multiple_candidates_violate_single_candidate_contract() -> None:
    result = _call(_resp([_cand(), _cand()]))
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_non_json_body_is_non_json_body() -> None:
    assert _call("this is not json {").reason_class == RC_NON_JSON_BODY


def test_json_non_object_body_is_bad_envelope() -> None:
    assert _call("[1, 2, 3]").reason_class == RC_BAD_ENVELOPE


def test_json_body_beyond_parser_depth_is_bad_envelope() -> None:
    depth = sys.getrecursionlimit() + 100
    result = _call("[" * depth + "0" + "]" * depth)
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


def test_absent_model_version_resolves_unresolved() -> None:
    result = _call(_resp([_cand("STOP", [{"text": "ok"}])], model="   "))
    assert result.ok and result.resolved_model == UNRESOLVED_MODEL_ID


def test_malformed_model_version_is_bad_envelope() -> None:
    body = _resp([_cand("STOP", [{"text": "pass"}])], modelVersion=7)
    result = _call(body)
    assert result.is_error and result.reason_class == RC_BAD_ENVELOPE


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (TimeoutError(), RC_TIMEOUT),
        (urllib.error.URLError("refused"), RC_UNREACHABLE),
        (urllib.error.HTTPError("u", 500, "e", {}, None), RC_UNREACHABLE),  # HTTPError is URLError
        (urllib.error.URLError(TimeoutError()), RC_TIMEOUT),  # URLError wrapping a timeout
        (OSError("socket"), RC_OS_ERROR),
    ],
)
def test_transport_failures_map_to_taxonomy(exc: BaseException, reason: str) -> None:
    result = _call(exc)
    assert result.is_error and result.reason_class == reason
    assert result.resolved_model == UNRESOLVED_MODEL_ID  # no body observed


# --- request shape + credential-security anchors -------------------------------------------


def test_request_url_body_and_header_only_key() -> None:
    capture: dict[str, Any] = {}
    _call(_resp([_cand("STOP", [{"text": "ok"}])]), capture=capture)
    assert (
        capture["url"] == "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.8-flash:generateContent"
    )
    # The key travels ONLY in the header argument, never in the URL (plan §6 D3).
    assert GEMINI_TEST_KEY not in capture["url"]
    assert capture["key"] == GEMINI_TEST_KEY
    assert capture["body"] == {
        "contents": [{"role": "user", "parts": [{"text": "solve it"}]}],
        "generationConfig": {
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    assert capture["timeout"] == 120.0


def test_request_body_reflects_context_settings() -> None:
    capture: dict[str, Any] = {}
    context = GeminiContextProfile(
        request_contract=GEMINI_REQUEST_CONTRACT,
        max_output_tokens=1024,
        thinking_level="high",
        timeout_s=45.0,
    )
    _call(_resp([_cand("STOP", [{"text": "ok"}])]), context=context, capture=capture)
    gen = capture["body"]["generationConfig"]
    assert gen["maxOutputTokens"] == 1024
    assert gen["thinkingConfig"]["thinkingLevel"] == "high"
    assert capture["timeout"] == 45.0


def test_unsafe_model_token_fails_loud_before_url_construction() -> None:
    with pytest.raises(AdapterError, match="safe single model token"):
        _call(_resp([_cand()]), requested_model="../evil:generateContent")


def test_real_urllib_transport_delivers_header_and_refuses_redirects() -> None:
    ok_body = _gemini_body("pong", model="gemini-3.8-flash")
    seen: dict[str, Any] = {"redirect_target_hit": False, "ok_headers": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # silence test server logging
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            if self.path.endswith(":generateContent"):
                seen["ok_headers"] = {k.lower(): v for k, v in self.headers.items()}
                payload = ok_body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()
            elif self.path == "/target":
                seen["redirect_target_hit"] = True
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            else:
                self.send_response(404)
                self.end_headers()

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        body = _urllib_post(
            base + "/v1beta/models/gemini-3.8-flash:generateContent", b"{}", "wire-key", 5.0
        )
        assert json.loads(body)["candidates"][0]["content"]["parts"][0]["text"] == "pong"
        assert seen["ok_headers"]["x-goog-api-key"] == "wire-key"
        # A redirect must be refused, never followed — the key is never re-sent to the target.
        with pytest.raises(urllib.error.URLError):
            _urllib_post(base + "/redirect", b"{}", "wire-key", 5.0)
        assert seen["redirect_target_hit"] is False
    finally:
        server.shutdown()


def test_resolve_credential_precedence_and_blank_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(GeminiCredentialError, match="GOOGLE_API_KEY"):
        resolve_gemini_credential()
    monkeypatch.setenv("GEMINI_API_KEY", "second")
    assert resolve_gemini_credential() == "second"
    monkeypatch.setenv("GOOGLE_API_KEY", "first")
    assert resolve_gemini_credential() == "first"  # GOOGLE precedence
    monkeypatch.setenv("GOOGLE_API_KEY", "   ")
    with pytest.raises(GeminiCredentialError) as exc_info:
        resolve_gemini_credential()  # blank winner fails, does NOT fall back to GEMINI_API_KEY
    assert "second" not in str(exc_info.value)  # never echoes a value
    monkeypatch.setenv("GOOGLE_API_KEY", "bad\x01")
    with pytest.raises(GeminiCredentialError, match="invalid header characters"):
        resolve_gemini_credential()


# --- committed profile + additive receipt compatibility ------------------------------------


def test_committed_gemini_profile_loads_and_hashes_are_stable() -> None:
    profile = _gemini_config().execution_profile
    assert profile.id == "model-sweep-gemini-v1"
    assert profile.gemini == DEFAULT_GEMINI_CONTEXT
    binding = profile.binding_for("gemini-flash")
    assert binding.provider == "gemini-api"
    assert binding.requested_model == GEMINI_MODEL
    # Composite context hash differs from the Claude-only hash exactly because Gemini is present.
    assert profile.context_profile_sha256 != profile.claude.sha256
    assert profile.sha256 == "7c16176810857c43c8859f9c6a8723c57b7a8927927f8d179eafdd2ac1c04ddf"
    assert (
        profile.context_profile_sha256
        == "bbc0391820f49248c93e0642450928e2606107fe6469d88bf3fc1685159295c7"
    )


def test_gemini_receipt_records_settings_and_round_trips() -> None:
    profile = _gemini_config().execution_profile
    receipt = ExecutionReceipt.create(
        profile, [profile.binding_for("gemini-flash")], claude_executable=None, claude_version=None
    )
    assert receipt.claude_cli is None  # no Claude binding selected
    assert receipt.gemini == DEFAULT_GEMINI_CONTEXT
    assert receipt.context_profile_sha256 == profile.context_profile_sha256
    wire = receipt.to_mapping()
    assert wire["gemini"] == DEFAULT_GEMINI_CONTEXT.to_mapping()
    assert ExecutionReceipt.from_mapping(wire) == receipt


# --- real production-entry integration -----------------------------------------------------


def test_run_gemini_stores_terminal_rows_and_report_exposes_identity_and_settings(
    tmp_path: Path,
) -> None:
    suite = _verdict_suite()
    stub = StubAdapters(gemini=lambda prompt: "pass" if _iid(prompt) == "g-a" else "flag")
    result = run(
        suite=suite,
        config=_gemini_config(),
        out_dir=tmp_path,
        roster=["gemini-flash"],
        samples_per_cell=1,
        scorer=make_deterministic_scorer(suite.scoring),
        gemini_transport_factory=stub.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
    )
    assert result.cells_completed == 2 and not result.aborted
    rows = _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")
    assert {r["model_id_resolved"] for r in rows} == {"gemini-x"}
    assert [r["score"] for r in rows] == [1.0, 1.0]

    report = build_run_report(result.run_id, tmp_path)
    arm = {m.model: m for m in report.models}["gemini-flash"]
    assert arm.provider == "gemini-api"
    assert arm.requested_model == GEMINI_MODEL
    assert arm.resolved_identities == ("gemini-x",)
    assert arm.suite_score == 100.0
    assert report.execution is not None
    assert report.execution.gemini_request_contract == GEMINI_REQUEST_CONTRACT
    assert report.execution.gemini_max_output_tokens == 4096

    md = render_run_report(report)
    tokens = ("gemini-api", GEMINI_MODEL, "gemini-x", "Gemini request", GEMINI_REQUEST_CONTRACT)
    for token in tokens:
        assert token in md

    jsonl_rows = [json.loads(line) for line in run_report_jsonl(report).splitlines()]
    gem = {r["model"]: r for r in jsonl_rows}["gemini-flash"]
    assert gem["provider"] == "gemini-api"
    assert gem["gemini_request_contract"] == GEMINI_REQUEST_CONTRACT
    assert gem["gemini_max_output_tokens"] == 4096
    assert gem["gemini_thinking_level"] == "low"
    assert gem["gemini_timeout_s"] == 120.0


def test_api_key_never_appears_in_durable_output(tmp_path: Path) -> None:
    suite = _verdict_suite()

    def behavior(prompt: str) -> str | BaseException:
        if _iid(prompt) == "g-a":
            return "pass"
        return urllib.error.URLError(f"provider echoed {GEMINI_TEST_KEY}")

    stub = StubAdapters(gemini=behavior)
    result = run(
        suite=suite,
        config=_gemini_config(),
        out_dir=tmp_path,
        roster=["gemini-flash"],
        samples_per_cell=1,
        scorer=make_deterministic_scorer(suite.scoring),
        gemini_transport_factory=stub.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
    )
    # The key WAS delivered to the transport header, but must be nowhere durable.
    assert stub.gemini_keys == [GEMINI_TEST_KEY, GEMINI_TEST_KEY]
    assert all(GEMINI_TEST_KEY not in url for url in stub.gemini_urls)
    run_dir = tmp_path / "runs" / result.run_id
    for name in ("manifest.json", "rows.jsonl", "suite.json"):
        assert GEMINI_TEST_KEY not in (run_dir / name).read_text("utf-8")
    rows = _read_jsonl(run_dir / "rows.jsonl")
    assert [row["error"] for row in rows] == [None, RC_UNREACHABLE]
    report = build_run_report(result.run_id, tmp_path)
    assert GEMINI_TEST_KEY not in render_run_report(report)
    assert GEMINI_TEST_KEY not in run_report_jsonl(report)
    assert GEMINI_TEST_KEY not in render_transparency_report(
        build_transparency_report(result.run_id, tmp_path)
    )


def test_smoke_gemini_end_to_end_passes_offline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    stub = StubAdapters(gemini=lambda prompt: prompt.strip().split()[-1])  # echoes "pass"/"flag"
    deps = CliDeps(
        gemini_transport_factory=stub.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
    )
    rc = main(
        [
            "smoke",
            "--gemini",
            "--config",
            str(GEMINI_PROFILE_PATH),
            "--out",
            str(tmp_path / "data"),
        ],
        deps=deps,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "smoke [gemini]: PASS" in out
    assert len(stub.gemini_calls) == 2


def test_smoke_gemini_fails_without_concrete_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def unresolved_answer(prompt: str) -> dict[str, object]:
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": prompt.strip().split()[-1]}]},
                }
            ]
        }

    stub = StubAdapters(gemini=unresolved_answer)
    deps = CliDeps(
        gemini_transport_factory=stub.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
    )
    rc = main(
        ["smoke", "--gemini", "--config", str(GEMINI_PROFILE_PATH), "--out", str(tmp_path)],
        deps=deps,
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "Gemini provider identity unresolved" in captured.err
    assert len(stub.gemini_calls) == 2


def test_mixed_provider_dispatch_calls_both_and_seals_both(tmp_path: Path) -> None:
    suite = _verdict_suite(("g-a",))
    stub = StubAdapters(gemini=lambda prompt: "pass", claude=lambda prompt: "pass")
    result = run(
        suite=suite,
        config=_gemini_config(),
        out_dir=tmp_path,
        roster=["gemini-flash", "haiku"],
        samples_per_cell=1,
        scorer=make_deterministic_scorer(suite.scoring),
        gemini_transport_factory=stub.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
        claude_runner_factory=stub.claude_factory(),
    )
    assert result.cells_completed == 2
    assert len(stub.gemini_calls) == 1 and len(stub.claude_calls) == 1
    report = build_run_report(result.run_id, tmp_path)
    assert report.execution is not None
    # Mixed run records BOTH the doctored Claude runtime AND the Gemini request settings.
    assert report.execution.claude_version == "test-claude 1.0"
    assert report.execution.gemini_request_contract == GEMINI_REQUEST_CONTRACT
    by_model = {m.model: m for m in report.models}
    assert by_model["gemini-flash"].provider == "gemini-api"
    assert by_model["haiku"].provider == "claude-cli"


def test_gemini_rubric_run_scores_with_sealed_claude_judge(tmp_path: Path) -> None:
    suite = Suite(
        suite="gemrubric",
        version=1,
        description="d",
        domain="d",
        scoring=ScoringSpec(type="rubric"),
        items=[
            Item(
                id="r1",
                tags=["t"],
                prompt="Explain recursion",
                expected="Award 10 for a correct explanation; 0 otherwise.",
                difficulty_prior=0.5,
                provenance="authored",
            )
        ],
    )
    stub = StubAdapters(
        gemini=lambda prompt: "A function calling itself", claude=lambda prompt: "SCORE: 9"
    )
    result = run(
        suite=suite,
        config=_gemini_config(),
        out_dir=tmp_path,
        roster=["gemini-flash"],
        gemini_transport_factory=stub.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
        claude_runner_factory=stub.claude_factory(),
    )
    rc = main(
        ["score", result.run_id, "--out", str(tmp_path), "--config", str(GEMINI_PROFILE_PATH)],
        deps=CliDeps(claude_runner_factory=stub.claude_factory()),
    )
    assert rc == 0
    rows = _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")
    assert rows[0]["scorer"] == "rubric"
    assert rows[0]["score"] == pytest.approx(0.9)
    assert len(stub.gemini_calls) == 1 and len(stub.claude_calls) == 3


def test_missing_credential_fails_before_any_run_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    suite = _verdict_suite()
    stub = StubAdapters(gemini=lambda prompt: "pass")
    with pytest.raises(RunError, match="Gemini credential preflight failed"):
        run(
            suite=suite,
            config=_gemini_config(),
            out_dir=tmp_path,
            roster=["gemini-flash"],
            samples_per_cell=1,
            gemini_transport_factory=stub.gemini_factory(),
            # No credential provider -> the real env resolver runs and finds nothing.
        )
    assert not (tmp_path / "runs").exists()  # failed BEFORE run creation (plan §6 D3)
    assert stub.gemini_calls == []  # no model call happened


def test_invalid_injected_credential_fails_before_any_run_creation(tmp_path: Path) -> None:
    stub = StubAdapters(gemini=lambda prompt: "pass")
    with pytest.raises(RunError, match="Gemini credential preflight failed"):
        run(
            suite=_verdict_suite(),
            config=_gemini_config(),
            out_dir=tmp_path,
            roster=["gemini-flash"],
            samples_per_cell=1,
            gemini_transport_factory=stub.gemini_factory(),
            gemini_credential_provider=lambda: "bad\x7f",
        )
    assert not (tmp_path / "runs").exists()
    assert stub.gemini_calls == []


def test_budget_abort_then_resume_never_recalls_completed_cells(tmp_path: Path) -> None:
    suite = _verdict_suite(("g-a", "g-b"))
    stub1 = StubAdapters(gemini=lambda prompt: "pass" if _iid(prompt) == "g-a" else "flag")
    first = run(
        suite=suite,
        config=_gemini_config(),
        out_dir=tmp_path,
        roster=["gemini-flash"],
        samples_per_cell=1,
        max_calls=1,
        scorer=make_deterministic_scorer(suite.scoring),
        gemini_transport_factory=stub1.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
    )
    assert first.aborted and len(stub1.gemini_calls) == 1

    stub2 = StubAdapters(gemini=lambda prompt: "pass" if _iid(prompt) == "g-a" else "flag")
    second = run(
        suite=suite,
        config=_gemini_config(),
        out_dir=tmp_path,
        resume=first.run_id,
        max_calls=10,
        scorer=make_deterministic_scorer(suite.scoring),
        gemini_transport_factory=stub2.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
    )
    assert not second.aborted
    # Only the one pending cell was called on resume; the completed cell was never recalled.
    assert [_iid(p) for p in stub2.gemini_calls] == ["g-b"]
    assert second.cells_completed == 2


def test_changed_gemini_settings_reject_resume_without_writing(tmp_path: Path) -> None:
    suite = _verdict_suite(("g-a", "g-b"))
    stub1 = StubAdapters(gemini=lambda prompt: "pass")
    first = run(
        suite=suite,
        config=_gemini_config(),
        out_dir=tmp_path,
        roster=["gemini-flash"],
        samples_per_cell=1,
        max_calls=1,
        scorer=make_deterministic_scorer(suite.scoring),
        gemini_transport_factory=stub1.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
    )
    assert first.aborted
    rows_path = tmp_path / "runs" / first.run_id / "rows.jsonl"
    rows_before = rows_path.read_bytes()

    # Build a config whose Gemini request settings differ (max_output_tokens 4096 -> 2048).
    mapping = _gemini_config().execution_profile.to_mapping()
    gemini_block = mapping["gemini"]
    assert isinstance(gemini_block, dict)
    gemini_block["max_output_tokens"] = 2048
    changed = RunConfig(execution_profile=ModelSweepExecutionProfile.from_mapping(mapping))

    stub2 = StubAdapters(gemini=lambda prompt: "pass")
    with pytest.raises(RunError, match="differ from the stored receipt"):
        run(
            suite=suite,
            config=changed,
            out_dir=tmp_path,
            resume=first.run_id,
            max_calls=10,
            scorer=make_deterministic_scorer(suite.scoring),
            gemini_transport_factory=stub2.gemini_factory(),
            gemini_credential_provider=gemini_test_credential,
        )
    assert rows_path.read_bytes() == rows_before  # rejected BEFORE any write
    assert stub2.gemini_calls == []


def test_gemini_error_and_no_response_rows_are_terminal(tmp_path: Path) -> None:
    suite = _verdict_suite(("g-a", "g-b"))

    def behavior(prompt: str) -> object:
        if _iid(prompt) == "g-a":
            return urllib.error.URLError("refused")  # transport error -> reason_class row
        return {"modelVersion": "gemini-x", "candidates": [{"finishReason": "SAFETY"}]}  # blocked

    stub = StubAdapters(gemini=behavior)
    result = run(
        suite=suite,
        config=_gemini_config(),
        out_dir=tmp_path,
        roster=["gemini-flash"],
        samples_per_cell=1,
        scorer=make_deterministic_scorer(suite.scoring),
        gemini_transport_factory=stub.gemini_factory(),
        gemini_credential_provider=gemini_test_credential,
    )
    rows = {r["item_id"]: r for r in _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")}
    assert rows["g-a"]["error"] == RC_UNREACHABLE
    assert rows["g-a"]["score"] is None
    # A safety-blocked response is force-scored 0 (no-response), never scored as an answer.
    assert rows["g-b"]["error"] is None
    assert rows["g-b"]["score"] == 0.0
    assert rows["g-b"]["scorer"] == "no_response"
