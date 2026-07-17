"""``local_chat`` — OpenAI-compatible chat-completion adapter for the local endpoint.

Sync, **stdlib-only** (``urllib`` — no third-party HTTP lib, plan §10/§8 P10). POSTs a chat
request to ``{local_base_url}/chat/completions`` and returns a :class:`ModelCallResult`.

Reasoning-model handling (switchboard CLAUDE.md gotcha, plan §5): the verdict text is read from
``choices[0].message.content`` and ``reasoning_content`` is IGNORED; ``max_tokens`` comes from
``config.local_max_tokens`` (validated ``>= 2000`` at config time — general-35b spends ~400-620
tokens on ``reasoning_content`` before the answer, so a smaller budget truncates ``content`` to
``""``). Empty/whitespace ``content`` maps to the :data:`NO_RESPONSE` state (a reasoning-only
truncation the runner force-scores 0); non-empty content cut off at ``finish_reason == "length"``
maps to the ``truncated`` reason_class.

Failure -> switchboard reason_class (imported via :mod:`base`, plan §8 D6):
  * connection refused / DNS (``URLError``)      -> ``unreachable``
  * socket timeout (``TimeoutError``)            -> ``timeout``
  * other ``OSError``                            -> ``os_error``
  * HTTP body not JSON                           -> ``non_json_body``
  * JSON present but envelope/message missing    -> ``bad_envelope``
  * non-empty content at ``finish_reason=length``-> ``truncated``

The adapter NEVER crashes the process on a network error — it returns a structured error result
and the runner records the error row (plan §5; the fail-loud *startup* reachability check lives in
config/runner, not here). The **client-factory DI seam** (``transport_factory``) makes the whole
path offline-testable: the default builds the real ``urllib`` transport; tests inject a stub.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Final, cast

from measure_twice.adapters.base import (
    RC_BAD_ENVELOPE,
    RC_NON_JSON_BODY,
    RC_OS_ERROR,
    RC_TIMEOUT,
    RC_TRUNCATED,
    RC_UNREACHABLE,
    ModelCallResult,
    resolved_model_of,
)
from measure_twice.config import RunConfig

# A transport: given the POST url, the JSON request-body bytes, and a timeout (seconds), return
# the decoded response-body text. It may raise ``TimeoutError`` / ``urllib.error.URLError`` /
# ``OSError`` on transport failure (the adapter classifies these). A *factory* returns one — the
# DI seam. Default posts via stdlib ``urllib``; tests inject a stub factory.
Transport = Callable[[str, bytes, float], str]
TransportFactory = Callable[[], Transport]

# The local endpoint is called SEQUENTIALLY (single-GPU llama-swap), so one shared timeout is
# fine. RunConfig carries no timeout field in v1 (frozen this step); this named default is the
# fallback and any caller/runner may override it per call. A config-level ``local_timeout_s`` is a
# clean future addition — thread it into the ``timeout`` parameter without a signature change.
DEFAULT_LOCAL_TIMEOUT_S: Final[float] = 120.0


def _urllib_post(url: str, data: bytes, timeout: float) -> str:
    """The default transport: POST ``data`` as JSON and return the decoded response body."""
    # url is config-validated http(s) only (config._validate_base_url); no arbitrary-scheme risk,
    # so the S310 "audit URL scheme" checks on Request/urlopen are satisfied at config time.
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read()
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)


def _default_transport_factory() -> Transport:
    """Construct the real ``urllib`` transport (the DI seam's production default)."""
    return _urllib_post


def _classify_transport_error(exc: BaseException) -> str:
    """Map a transport exception to a switchboard reason_class (mirrors client._classify...)."""
    if isinstance(exc, TimeoutError):
        return RC_TIMEOUT
    if isinstance(exc, urllib.error.URLError):
        # A URLError wrapping a timeout still classifies as timeout (switchboard parity).
        if isinstance(exc.reason, TimeoutError):
            return RC_TIMEOUT
        return RC_UNREACHABLE
    # Other OSError family (socket errors not wrapped by urllib).
    return RC_OS_ERROR


def _message_of(payload: dict[str, object]) -> dict[str, object] | None:
    """Safely pull ``choices[0].message`` as a dict, or None if the envelope is malformed."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    msg = first.get("message")
    if not isinstance(msg, dict):
        return None
    return cast("dict[str, object]", msg)


def _finish_reason_of(payload: dict[str, object]) -> str | None:
    """``choices[0].finish_reason`` as a str, or None if absent/malformed."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    fr = first.get("finish_reason")
    return fr if isinstance(fr, str) else None


def local_chat(
    prompt: str,
    *,
    model: str,
    config: RunConfig,
    timeout: float | None = None,
    transport_factory: TransportFactory | None = None,
) -> ModelCallResult:
    """Call the local OpenAI-compatible endpoint once and return a :class:`ModelCallResult`.

    Args:
        prompt: the full item prompt (suites carry all content — no templating here, plan §8 D4).
        model: the roster model alias to request (e.g. ``"general-35b"``).
        config: run config; ``local_base_url`` and ``local_max_tokens`` are read from it — never
            hardcoded (plan §5).
        timeout: per-call timeout in seconds; defaults to :data:`DEFAULT_LOCAL_TIMEOUT_S`.
        transport_factory: the DI seam. ``None`` -> the real ``urllib`` transport; tests inject a
            stub factory returning a fake transport.

    Never raises on a transport/envelope failure — returns a structured ERROR result instead.
    """
    base_url = config.local_base_url
    max_tokens = config.local_max_tokens
    eff_timeout = timeout if timeout is not None else DEFAULT_LOCAL_TIMEOUT_S
    factory = transport_factory if transport_factory is not None else _default_transport_factory
    transport = factory()

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    data = json.dumps(body).encode("utf-8")
    url = f"{base_url}/chat/completions"

    start = time.monotonic()
    try:
        raw_body = transport(url, data, eff_timeout)
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        elapsed = round(time.monotonic() - start, 3)
        return ModelCallResult.error(reason_class=_classify_transport_error(exc), elapsed_s=elapsed)
    elapsed = round(time.monotonic() - start, 3)

    try:
        payload_raw = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return ModelCallResult.error(reason_class=RC_NON_JSON_BODY, elapsed_s=elapsed)

    if not isinstance(payload_raw, dict):
        return ModelCallResult.error(reason_class=RC_BAD_ENVELOPE, elapsed_s=elapsed)
    payload = cast("dict[str, object]", payload_raw)

    msg = _message_of(payload)
    if msg is None:
        return ModelCallResult.error(reason_class=RC_BAD_ENVELOPE, elapsed_s=elapsed)

    # Read the answer from message.content; IGNORE reasoning_content (switchboard gotcha).
    content_raw = msg.get("content")
    content = content_raw if isinstance(content_raw, str) else ""
    resolved = resolved_model_of(payload, requested=model)

    # Empty/whitespace content -> no-response (a reasoning-only truncation). This is checked
    # BEFORE finish_reason: an empty answer is force-scored 0, never recorded as a defer, even
    # when it was empty *because* the reasoning ran out of tokens (finish_reason=length).
    if not content.strip():
        return ModelCallResult.no_response_result(resolved_model=resolved, elapsed_s=elapsed)

    # Non-empty but cut off mid-answer -> the switchboard ``truncated`` reason_class.
    if _finish_reason_of(payload) == "length":
        return ModelCallResult.error(
            reason_class=RC_TRUNCATED, resolved_model=resolved, elapsed_s=elapsed
        )

    return ModelCallResult.success(response_raw=content, resolved_model=resolved, elapsed_s=elapsed)
