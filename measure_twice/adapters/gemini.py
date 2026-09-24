"""``gemini_generate`` — Gemini Developer API adapter (non-streaming ``generateContent``).

Sync, **stdlib-only** (``urllib`` — no SDK, plan §6 D2). POSTs one text-only, single-candidate
request to the code-owned HTTPS origin and returns a :class:`ModelCallResult`. The endpoint and the
request shape are fixed by the profile's ``request_contract`` selector; a repository/profile can
never choose a different URL (plan §6 D2/D3).

Credentials are RUNTIME-ONLY and HEADER-ONLY (plan §6 D3). The API key is read from the environment
(``GOOGLE_API_KEY`` then ``GEMINI_API_KEY``, Google's documented precedence) by
:func:`resolve_gemini_credential`, threaded to this adapter as a plain argument, and sent solely in
the ``x-goog-api-key`` header. It never appears in a URL, config, receipt, repr, log, or error, and
it is never stored on any object with a default repr. Redirects are refused rather than followed, so
the key is never re-sent to a redirect target.

Outcome classification (plan §6 D4), all decided from a ``2xx`` JSON body while retaining the
provider ``modelVersion`` as soon as it is observed:
  * prompt blocked (``promptFeedback.blockReason``) with no valid candidate -> no-response state
  * candidate ``finishReason`` a documented safety/recitation block          -> no-response state
  * empty / whitespace / thought-only answer text                           -> no-response state
  * ``finishReason == "MAX_TOKENS"`` with a partial answer                   -> ``truncated``
  * missing candidate structure w/o a valid block, unsupported content type,
    unexpected finish reason, or a malformed consumed field                 -> ``bad_envelope``
  * body not JSON / not a JSON object                       -> ``non_json_body`` / ``bad_envelope``
Transport/HTTP failures map onto switchboard's taxonomy exactly as the local adapter does
(``URLError``/HTTPError -> ``unreachable``, ``TimeoutError`` -> ``timeout``, other ``OSError`` ->
``os_error``), and NO exception text or response body that could echo a key is ever persisted — the
``reason_class`` is a fixed constant.

Thought parts (``thinkingConfig`` reasoning) are EXCLUDED from the scored answer (plan §6 D4). No
implicit retries: each scheduled attempt is exactly one call against the run budget (the runner
consumes it, mirroring the sequential local sweep). The **client-factory DI seam**
(``transport_factory``) makes the whole path offline-testable: the default builds the real
``urllib`` transport; tests inject a stub that returns canned response bytes through the SAME
production payload parser.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Final, cast

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
from measure_twice.model_sweep_execution import (
    _SAFE_GEMINI_MODEL_RE,
    GEMINI_REQUEST_CONTRACT,
    GeminiContextProfile,
)

__all__ = [
    "GeminiCredentialError",
    "GeminiCredentialProvider",
    "GeminiTransport",
    "GeminiTransportFactory",
    "gemini_generate",
    "resolve_gemini_credential",
]

# The fixed, code-owned origin + path template. Selected by ``request_contract`` (plan §6 D2), never
# read from config or a receipt. ``{model}`` is a single validated safe token, so the interpolation
# cannot introduce a new path segment, query, or host.
_GEMINI_ORIGIN: Final[str] = "https://generativelanguage.googleapis.com"
_GEMINI_PATH_TEMPLATE: Final[str] = "/v1beta/models/{model}:generateContent"

# Environment variables holding the API key, in Google's documented precedence order. A present but
# blank/invalid WINNING value fails rather than silently selecting the other (plan §6 D3).
_CREDENTIAL_ENV_VARS: Final[tuple[str, ...]] = ("GOOGLE_API_KEY", "GEMINI_API_KEY")

# finishReason vocabulary consumed for classification (plan §6 D4). Only these drive an outcome; any
# other non-STOP value is an unexpected finish reason -> bad_envelope.
_FINISH_STOP: Final[str] = "STOP"
_FINISH_MAX_TOKENS: Final[str] = "MAX_TOKENS"
# Documented safety/recitation blocks -> the no-response state (a measured empty answer, not text).
_BLOCK_FINISH_REASONS: Final[frozenset[str]] = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
)

# A transport: given the POST url, JSON body bytes, the API key, and a timeout (seconds), return the
# decoded ``2xx`` response-body text. It may raise ``TimeoutError`` / ``urllib.error.URLError`` /
# ``OSError`` on transport/HTTP failure and MUST refuse redirects (the adapter classifies these). A
# *factory* returns one — the DI seam. Default posts via stdlib ``urllib``; tests inject a stub.
GeminiTransport = Callable[[str, bytes, str, float], str]
GeminiTransportFactory = Callable[[], GeminiTransport]

# A credential provider returns the runtime API key (or raises :class:`GeminiCredentialError`). The
# default reads the environment; the runner's preflight and injected offline tests supply their own.
GeminiCredentialProvider = Callable[[], str]


class GeminiCredentialError(ValueError):
    """A required Gemini API credential was missing or blank.

    Fail-loud sentinel (package convention alongside ``ConfigError`` / ``AdapterError``). The
    message NEVER echoes any environment value — only the variable names that were consulted.
    """


def resolve_gemini_credential() -> str:
    """Read the API key from ``GOOGLE_API_KEY`` then ``GEMINI_API_KEY`` (Google's precedence).

    The FIRST variable that is present wins; if its value is blank or contains control characters
    the resolution FAILS rather than silently falling back to the other (plan §6 D3). The returned
    value is never logged; only the variable names appear in any error.
    """
    for name in _CREDENTIAL_ENV_VARS:
        if name not in os.environ:
            continue
        value = os.environ[name]
        if not value.strip():
            raise GeminiCredentialError(
                f"{name} is set but blank; refusing to silently fall back to another variable"
            )
        if any(ch in value for ch in "\r\n\x00"):
            raise GeminiCredentialError(
                f"{name} contains control characters and is not a usable header value"
            )
        return value
    raise GeminiCredentialError(
        "no Gemini API key found; set GOOGLE_API_KEY (preferred) or GEMINI_API_KEY in the "
        "environment before running a Gemini sweep"
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the credential header is never re-sent to a redirect target.

    Returning ``None`` from ``redirect_request`` tells urllib not to follow; the response then falls
    through to the default error handler and surfaces as an ``HTTPError`` the adapter classifies as
    a transport failure. No new request (and therefore no ``x-goog-api-key`` header) is ever issued
    to the ``Location`` URL (plan §6 D3).
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _urllib_post(url: str, body: bytes, api_key: str, timeout: float) -> str:
    """The default transport: POST ``body`` as JSON with a header-only key, refusing redirects."""
    # url is code-owned https with a single validated model token (never config/profile-selected),
    # so the S310 "audit URL scheme" checks on Request/opener are satisfied by construction.
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)


def _default_transport_factory() -> GeminiTransport:
    """Construct the real ``urllib`` transport (the DI seam's production default)."""
    return _urllib_post


def _classify_transport_error(exc: BaseException) -> str:
    """Map a transport/HTTP exception to a switchboard reason_class (mirrors ``local._classify``).

    ``HTTPError`` is a ``URLError`` subclass, so an HTTP error status classifies as ``unreachable``
    exactly as the local adapter classifies any ``URLError`` — one classification shape across
    adapters (``code-quality.md`` § one source of truth). The exception text is discarded so nothing
    that might echo a key is persisted.
    """
    if isinstance(exc, TimeoutError):
        return RC_TIMEOUT
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, TimeoutError):
            return RC_TIMEOUT
        return RC_UNREACHABLE
    return RC_OS_ERROR


def _endpoint(requested_model: str) -> str:
    """Build the fixed generateContent URL for a single, pre-validated safe model token."""
    if not _SAFE_GEMINI_MODEL_RE.fullmatch(requested_model):
        # Defense in depth: the profile loader already enforces this grammar, so reaching here is a
        # programmer/config fault, not a model failure -> fail loud rather than build a URL.
        raise AdapterError(
            f"Gemini requested model {requested_model!r} is not a safe single model token"
        )
    return _GEMINI_ORIGIN + _GEMINI_PATH_TEMPLATE.format(model=requested_model)


def _build_request_body(prompt: str, context: GeminiContextProfile) -> bytes:
    """The exact text-only single-candidate request body (plan §6 D2); prompt sent verbatim."""
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": context.max_output_tokens,
            "thinkingConfig": {"thinkingLevel": context.thinking_level},
        },
    }
    return json.dumps(body).encode("utf-8")


def _resolved_model(payload: Mapping[str, object]) -> str:
    """Read the concrete provider identity from ``modelVersion``; unresolved when absent/blank."""
    version = payload.get("modelVersion")
    return version if isinstance(version, str) and version.strip() else UNRESOLVED_MODEL_ID


def _block_reason(payload: Mapping[str, object]) -> str | None:
    """A non-blank ``promptFeedback.blockReason`` string, or None."""
    feedback = payload.get("promptFeedback")
    if not isinstance(feedback, dict):
        return None
    reason = feedback.get("blockReason")
    return reason if isinstance(reason, str) and reason.strip() else None


def _extract_answer(content: object) -> tuple[bool, str]:
    """Join non-thought text parts in order; ``(False, "")`` on a malformed/unsupported part.

    A ``thought`` part (``thought: true``) is excluded from the scored answer. A non-thought part
    under this text-only contract MUST carry string ``text``; a part that is not a dict, a non-bool
    ``thought``, or a non-thought part without ``text`` (an unsupported content type such as a
    function call or inline data) is malformed -> the caller maps it to ``bad_envelope`` (plan §6
    D4). Absent ``content`` / ``parts`` yields an empty answer (caller maps that to no-response).
    """
    if content is None:
        return (True, "")
    if not isinstance(content, dict):
        return (False, "")
    parts = content.get("parts")
    if parts is None:
        return (True, "")
    if not isinstance(parts, list):
        return (False, "")
    pieces: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            return (False, "")
        thought = part.get("thought")
        if thought is not None and not isinstance(thought, bool):
            return (False, "")
        if thought is True:
            continue  # exclude reasoning from the scored answer
        text = part.get("text")
        if not isinstance(text, str):
            return (False, "")
        pieces.append(text)
    return (True, "".join(pieces))


def _classify_body(raw_body: str, elapsed: float) -> ModelCallResult:
    """Turn a ``2xx`` response body into a terminal :class:`ModelCallResult` (plan §6 D4)."""
    try:
        payload_raw = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return ModelCallResult.error(reason_class=RC_NON_JSON_BODY, elapsed_s=elapsed)
    if not isinstance(payload_raw, dict):
        return ModelCallResult.error(reason_class=RC_BAD_ENVELOPE, elapsed_s=elapsed)
    payload = cast("Mapping[str, object]", payload_raw)
    # Provider identity is read (and retained) BEFORE outcome classification, so a truncated,
    # blocked, empty, or malformed response still records the observed modelVersion (plan §6 D4).
    resolved = _resolved_model(payload)

    candidates = payload.get("candidates")
    block = _block_reason(payload)
    if not isinstance(candidates, list) or len(candidates) != 1:
        # Missing/invalid candidate structure. A documented prompt block is a measured no-response;
        # anything else is a malformed envelope (plan §6 D4).
        if block is not None:
            return ModelCallResult.no_response_result(resolved_model=resolved, elapsed_s=elapsed)
        return ModelCallResult.error(
            reason_class=RC_BAD_ENVELOPE, resolved_model=resolved, elapsed_s=elapsed
        )

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return ModelCallResult.error(
            reason_class=RC_BAD_ENVELOPE, resolved_model=resolved, elapsed_s=elapsed
        )
    finish_raw = candidate.get("finishReason")
    finish = finish_raw if isinstance(finish_raw, str) else None

    # A candidate-level safety/recitation block is a no-response (typically carries no content).
    if finish in _BLOCK_FINISH_REASONS:
        return ModelCallResult.no_response_result(resolved_model=resolved, elapsed_s=elapsed)

    parts_ok, answer = _extract_answer(candidate.get("content"))
    if not parts_ok:
        return ModelCallResult.error(
            reason_class=RC_BAD_ENVELOPE, resolved_model=resolved, elapsed_s=elapsed
        )

    # Empty / whitespace / thought-only answer -> no-response (checked BEFORE MAX_TOKENS so a
    # reasoning-only truncation is force-scored 0, never a defer — mirrors the local adapter).
    if not answer.strip():
        return ModelCallResult.no_response_result(resolved_model=resolved, elapsed_s=elapsed)
    if finish == _FINISH_MAX_TOKENS:
        return ModelCallResult.error(
            reason_class=RC_TRUNCATED, resolved_model=resolved, elapsed_s=elapsed
        )
    if finish != _FINISH_STOP:
        # An unexpected or missing finish reason on a non-empty answer is a contract violation.
        return ModelCallResult.error(
            reason_class=RC_BAD_ENVELOPE, resolved_model=resolved, elapsed_s=elapsed
        )
    return ModelCallResult.success(response_raw=answer, resolved_model=resolved, elapsed_s=elapsed)


def gemini_generate(
    prompt: str,
    *,
    requested_model: str,
    context: GeminiContextProfile,
    api_key: str,
    timeout: float | None = None,
    transport_factory: GeminiTransportFactory | None = None,
) -> ModelCallResult:
    """Call the Gemini generateContent endpoint once and return a :class:`ModelCallResult`.

    Args:
        prompt: the full item prompt (suites carry all content — sent verbatim, no templating).
        requested_model: the provider model id (e.g. ``"gemini-3.8-flash"``); validated as a single
            safe token before URL construction.
        context: the Gemini request contract (max output tokens, thinking level, timeout, endpoint
            selector). ``context.timeout_s`` is the default per-call timeout.
        api_key: the runtime credential, sent ONLY in the ``x-goog-api-key`` header. Never stored.
        timeout: per-call timeout override in seconds; defaults to ``context.timeout_s``.
        transport_factory: the DI seam. ``None`` -> the real ``urllib`` transport; tests inject a
            stub factory returning a fake transport with canned response bytes.

    Never raises on a transport/HTTP/envelope failure — returns a structured ERROR result instead.
    A misuse (wrong request contract, unsafe model token) fails loud with :class:`AdapterError`.
    """
    if context.request_contract != GEMINI_REQUEST_CONTRACT:
        raise AdapterError(
            f"Gemini context request_contract must be {GEMINI_REQUEST_CONTRACT!r}, "
            f"got {context.request_contract!r}"
        )
    url = _endpoint(requested_model)
    body = _build_request_body(prompt, context)
    eff_timeout = timeout if timeout is not None else context.timeout_s
    factory = transport_factory if transport_factory is not None else _default_transport_factory
    transport = factory()

    start = time.monotonic()
    try:
        raw_body = transport(url, body, api_key, eff_timeout)
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        elapsed = round(time.monotonic() - start, 3)
        return ModelCallResult.error(reason_class=_classify_transport_error(exc), elapsed_s=elapsed)
    except Exception:
        # Honor "never raises on a transport failure": a non-OSError transport/decode fault (e.g. a
        # UnicodeDecodeError from a non-UTF-8 body — a ValueError, not OSError) is an unusable
        # envelope -> non_json_body. No exception text is persisted (it could echo nothing sensitive
        # here, but the constant reason_class keeps that guarantee unconditional).
        # KeyboardInterrupt / SystemExit are BaseException (not Exception) and still propagate.
        return ModelCallResult.error(
            reason_class=RC_NON_JSON_BODY, elapsed_s=round(time.monotonic() - start, 3)
        )
    return _classify_body(raw_body, round(time.monotonic() - start, 3))
