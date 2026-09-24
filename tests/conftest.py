"""Shared OFFLINE stub scaffolding for the runner / report / scoring tests.

Extracted from ``test_runner.py`` so ``test_runner.py`` and ``test_report.py`` share ONE
``StubAdapters`` + envelope-builder implementation instead of near-duplicate copies
(``code-quality.md`` § one source of truth — the duplicate-shape drift the package warns against).
Imported by name from the test modules (``from conftest import StubAdapters, _iid``). Every stub is
offline: no network, and the real ``claude`` subprocess is never invoked.
"""

from __future__ import annotations

import json

from measure_twice.adapters.claude_cli import ClaudeInvocation, RunnerFactory, SubprocessResult
from measure_twice.adapters.gemini import GeminiTransportFactory
from measure_twice.adapters.local import TransportFactory
from measure_twice.model_sweep_execution import CLAUDE_ARGV_TEMPLATE

# The test credential an injected Gemini offline transport carries. It is a fake token: it never
# reaches a real endpoint, and the production credential resolver is bypassed (plan §6 D3 —
# "Injected offline transport can use a test credential; it must not read real credentials").
GEMINI_TEST_KEY = "test-gemini-key"


def gemini_test_credential() -> str:
    """A credential-provider seam for offline Gemini tests (returns the fake key)."""
    return GEMINI_TEST_KEY


def _iid(prompt: str) -> str:
    """Recover an item id from a test prompt (``PROMPT::<id>``), or the prompt itself if plain."""
    return prompt.split("::")[1] if "::" in prompt else prompt


def _openai_body(content: str, *, model: str = "local-x", finish_reason: str = "stop") -> str:
    return json.dumps(
        {
            "id": "c",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
        }
    )


def _gemini_body(
    text: str, *, model: str = "gemini-x", finish_reason: str = "STOP", thought: bool = False
) -> str:
    """A canned generateContent response: one candidate, one text part (optionally a thought part).

    ``thought=True`` marks the single part as reasoning, which the adapter excludes from the answer
    (so the cell classifies as no-response). An empty ``text`` also yields the no-response state.
    """
    part: dict[str, object] = {"text": text}
    if thought:
        part["thought"] = True
    return json.dumps(
        {
            "modelVersion": model,
            "candidates": [{"finishReason": finish_reason, "content": {"parts": [part]}}],
        }
    )


def _claude_stdout(result_text: str, *, model: str = "claude-x") -> str:
    """SDKResultMessage success; identity is the ModelUsage map key.

    https://code.claude.com/docs/en/agent-sdk/typescript#sdkresultmessage
    https://code.claude.com/docs/en/agent-sdk/typescript#modelusage
    """
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result_text,
            "uuid": "00000000-0000-4000-8000-000000000001",
            "session_id": "00000000-0000-4000-8000-000000000002",
            "duration_ms": 4200,
            "duration_api_ms": 3800,
            "num_turns": 1,
            "stop_reason": "end_turn",
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 120,
                "output_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "modelUsage": {
                model: {
                    "inputTokens": 120,
                    "outputTokens": 40,
                    "cacheCreationInputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "webSearchRequests": 0,
                    "costUSD": 0.0123,
                    "contextWindow": 200000,
                    "maxOutputTokens": 8192,
                }
            },
            "permission_denials": [],
        }
    )


class StubAdapters:
    """Offline stub factories for both adapters, sharing a call recorder.

    ``local`` / ``claude`` are ``prompt -> behavior`` callables; a behavior returns the text to
    echo (``""`` yields the no-response state), a ``BaseException`` to raise (transport failure ->
    reason_class), or a ready-made :class:`SubprocessResult` (claude only). Default behavior echoes
    a constant, so a suite whose prompts carry no ``::`` id (e.g. suites/smoke.json) still works.
    """

    def __init__(self, *, local=None, claude=None, gemini=None) -> None:
        self.local_behavior = local if local is not None else (lambda prompt: "loc-answer")
        self.claude_behavior = claude if claude is not None else (lambda prompt: "cl-answer")
        # A Gemini behavior returns the answer text (wrapped into a STOP response), a raw response
        # dict (used verbatim — the escape hatch for a truncated/blocked/malformed envelope), or a
        # BaseException to raise (a transport failure). Default echoes a constant.
        self.gemini_behavior = gemini if gemini is not None else (lambda prompt: "gem-answer")
        self.local_calls: list[str] = []
        self.local_timeouts: list[float] = []
        self.claude_calls: list[str] = []
        self.gemini_calls: list[str] = []
        self.gemini_keys: list[str] = []
        self.gemini_urls: list[str] = []
        self.gemini_timeouts: list[float] = []

    def local_factory(self) -> TransportFactory:
        def factory() -> object:
            def transport(url: str, data: bytes, timeout: float) -> str:
                body = json.loads(data.decode("utf-8"))
                prompt = body["messages"][0]["content"]
                self.local_calls.append(prompt)
                self.local_timeouts.append(timeout)
                out = self.local_behavior(prompt)
                if isinstance(out, BaseException):
                    raise out
                return _openai_body(out)

            return transport

        return factory  # type: ignore[return-value]

    def claude_factory(self) -> RunnerFactory:
        def factory() -> object:
            def runner(
                invocation: ClaudeInvocation, input_text: str, timeout: float
            ) -> SubprocessResult:
                if invocation.argv[-1] == "--version":
                    return SubprocessResult(0, "test-claude 1.0", "")
                if invocation.argv[-1] == "--help":
                    return SubprocessResult(0, " ".join(CLAUDE_ARGV_TEMPLATE), "")
                self.claude_calls.append(input_text)
                out = self.claude_behavior(input_text)
                if isinstance(out, BaseException):
                    raise out
                if isinstance(out, SubprocessResult):
                    return out
                return SubprocessResult(0, _claude_stdout(out), "")

            return runner

        return factory  # type: ignore[return-value]

    def gemini_factory(self) -> GeminiTransportFactory:
        def factory() -> object:
            def transport(url: str, body: bytes, api_key: str, timeout: float) -> str:
                payload = json.loads(body.decode("utf-8"))
                prompt = payload["contents"][0]["parts"][0]["text"]
                self.gemini_calls.append(prompt)
                self.gemini_keys.append(api_key)
                self.gemini_urls.append(url)
                self.gemini_timeouts.append(timeout)
                out = self.gemini_behavior(prompt)
                if isinstance(out, BaseException):
                    raise out
                if isinstance(out, dict):
                    return json.dumps(out)
                return _gemini_body(out)

            return transport

        return factory  # type: ignore[return-value]
