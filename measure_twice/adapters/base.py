"""Shared adapter result type + the no-response sentinel for measure-twice model calls.

Both adapters — ``local`` (OpenAI-compatible HTTP) and ``claude_cli`` (subprocess) — return a
:class:`ModelCallResult`. The failure ``reason_class`` vocabulary is **switchboard's**: it is
IMPORTED from ``switchboard/switchboard/client.py`` (the RC_* module constants), never
re-declared — code-quality.md § one source of truth, plan §8 D6. If switchboard ever renames a
reason class, this import fails loudly rather than drifting silently. switchboard is the sole
measure-twice module that touches these names; every other module imports them from HERE.

measure-twice's adapters capture the RAW response text only — they do NOT parse a verdict (that
is Step 5's scoring job), so the ``bad_verdict`` member of the taxonomy is never *emitted* by an
adapter. It is still re-exported for completeness and for the downstream scorer, and it stays in
:data:`REASON_CLASSES` so the full switchboard vocabulary has one measure-twice-side owner.

The :data:`NO_RESPONSE` sentinel marks the "model returned no usable text" state — empty or
whitespace-only content, or a reasoning-only truncation (``general-35b`` spends ~400-620 tokens
on ``reasoning_content`` before the answer; too small a ``max_tokens`` truncates the answer to
``""``). The runner (Step 4) force-scores such a cell **0 before any judge call**
(measurement-validity.md § score the production artifact: a model that narrates but emits zero
answer must score 0 — never be silently dropped, nor judged against ``""``). A no-response is a
*measured* failure of the model, distinct from a transport/envelope ERROR: it carries no
``reason_class`` (those are switchboard's 7, none of which mean "empty answer").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# One source of truth — the 7-value defer/error taxonomy imported from switchboard, not
# re-declared (code-quality.md; plan §8 D6). switchboard ships no py.typed marker, so mypy sees
# it untyped (the values arrive as ``Any``); the scoped ignore keeps --strict green without
# editing the sibling package or the mypy config (mirrors measure_twice/config.py's import). The
# ``Final[str]`` re-exports below are the measure-twice typing boundary: they pin the VALUES that
# came from switchboard to ``str`` so every downstream module gets typed constants — the values
# are still switchboard's (a rename there breaks this import), just re-typed here.
from switchboard.client import (  # type: ignore[import-untyped]
    RC_BAD_ENVELOPE as _SB_RC_BAD_ENVELOPE,
)
from switchboard.client import (
    RC_BAD_VERDICT as _SB_RC_BAD_VERDICT,
)
from switchboard.client import (
    RC_NON_JSON_BODY as _SB_RC_NON_JSON_BODY,
)
from switchboard.client import (
    RC_OS_ERROR as _SB_RC_OS_ERROR,
)
from switchboard.client import (
    RC_TIMEOUT as _SB_RC_TIMEOUT,
)
from switchboard.client import (
    RC_TRUNCATED as _SB_RC_TRUNCATED,
)
from switchboard.client import (
    RC_UNREACHABLE as _SB_RC_UNREACHABLE,
)

RC_UNREACHABLE: Final[str] = _SB_RC_UNREACHABLE
RC_TIMEOUT: Final[str] = _SB_RC_TIMEOUT
RC_OS_ERROR: Final[str] = _SB_RC_OS_ERROR
RC_NON_JSON_BODY: Final[str] = _SB_RC_NON_JSON_BODY
RC_BAD_ENVELOPE: Final[str] = _SB_RC_BAD_ENVELOPE
RC_TRUNCATED: Final[str] = _SB_RC_TRUNCATED
RC_BAD_VERDICT: Final[str] = _SB_RC_BAD_VERDICT

__all__ = [
    "NO_RESPONSE",
    "RC_BAD_ENVELOPE",
    "RC_BAD_VERDICT",
    "RC_NON_JSON_BODY",
    "RC_OS_ERROR",
    "RC_TIMEOUT",
    "RC_TRUNCATED",
    "RC_UNREACHABLE",
    "REASON_CLASSES",
    "AdapterError",
    "ModelCallResult",
    "resolved_model_of",
]

# The complete switchboard reason_class vocabulary, re-exported as ONE measure-twice-side
# frozenset so both adapters, the runner, and tests reference a single value set (and a future
# switchboard rename breaks the import above, not this collection silently).
REASON_CLASSES: Final[frozenset[str]] = frozenset(
    {
        RC_UNREACHABLE,
        RC_TIMEOUT,
        RC_OS_ERROR,
        RC_NON_JSON_BODY,
        RC_BAD_ENVELOPE,
        RC_TRUNCATED,
        RC_BAD_VERDICT,
    }
)

# The no-response sentinel: the exact ``response_raw`` value a no-response result carries. It is a
# pair of Unicode noncharacters (U+FDD0, permanently unassigned and never valid interchange text)
# wrapping an ASCII tag, so a real model can never emit it and equality against it is unambiguous.
# :attr:`ModelCallResult.no_response` derives from it, giving the state a single owner. Written
# code points via ``chr(0xFDD0)`` (not raw glyphs) so this source file stays pure ASCII.
#
# ADAPTER-PRODUCED ONLY: both adapters set this value EXCLUSIVELY via
# ``ModelCallResult.no_response_result()`` on their own ``content.strip() == ""`` check — neither
# adapter ever compares model output *against* this string. So even a model coaxed into echoing the
# noncharacter codepoints cannot be misclassified as no-response by a string match: it would be a
# non-empty ``content`` and classify as a normal SUCCESS. The sentinel is an internal state tag, not
# a content filter.
NO_RESPONSE: Final[str] = f"{chr(0xFDD0)}MEASURE_TWICE_NO_RESPONSE{chr(0xFDD0)}"


class AdapterError(ValueError):
    """Raised on programmer misuse of a :class:`ModelCallResult` constructor.

    Fail-loud sentinel (mirrors ``config.ConfigError`` / ``suite.SuiteError``): an ``error``
    result built with a ``reason_class`` outside switchboard's taxonomy, or a ``success`` built
    from empty/sentinel text, is a bug in the *adapter*, not a model failure — surface it as this
    rather than letting a malformed result flow into the run store.
    """


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    """The outcome of exactly one model call, from either adapter.

    Exactly one of three states, each decidable without inspecting the text:

    * **SUCCESS** — ``reason_class is None and not no_response``; ``response_raw`` is real,
      non-empty model text and :attr:`ok` is ``True``.
    * **NO-RESPONSE** — :attr:`no_response` is ``True`` (``response_raw`` is :data:`NO_RESPONSE`);
      the model returned empty/whitespace text. Carries no ``reason_class`` — it is a measured
      empty answer, not a transport error. The runner force-scores it 0 before any judging.
    * **ERROR** — ``reason_class`` is a switchboard reason_class; the call failed (transport,
      non-JSON body, bad envelope, or truncation). ``response_raw`` is ``""``.

    ``resolved_model`` is the CONCRETE model id the call resolved to (drift detection: a requested
    alias like ``"sonnet"`` may resolve to ``"claude-sonnet-4-..."``, or a local alias may echo a
    swapped GGUF id). It is ``""`` when the call never reached a model (a transport error before
    any response). ``elapsed_s`` is wall-clock seconds for the call.
    """

    response_raw: str
    resolved_model: str
    elapsed_s: float
    reason_class: str | None = None

    def __post_init__(self) -> None:
        if self.reason_class is not None and self.reason_class not in REASON_CLASSES:
            raise AdapterError(
                f"reason_class {self.reason_class!r} is not a switchboard reason_class; "
                f"allowed: {sorted(REASON_CLASSES)}"
            )

    @property
    def no_response(self) -> bool:
        """True when the model returned no usable text (see :data:`NO_RESPONSE`)."""
        return self.response_raw == NO_RESPONSE

    @property
    def is_error(self) -> bool:
        """True when the call failed with a switchboard ``reason_class``."""
        return self.reason_class is not None

    @property
    def ok(self) -> bool:
        """True only for a scoreable success: real text, no error, not the no-response state."""
        return self.reason_class is None and not self.no_response

    # --- Intent-revealing constructors (one per state) -----------------------------------
    @classmethod
    def success(
        cls, *, response_raw: str, resolved_model: str, elapsed_s: float
    ) -> ModelCallResult:
        """A scoreable success (rejects empty/whitespace/sentinel text)."""
        if response_raw == NO_RESPONSE or not response_raw.strip():
            raise AdapterError("success() requires non-empty, non-sentinel response text")
        return cls(response_raw=response_raw, resolved_model=resolved_model, elapsed_s=elapsed_s)

    @classmethod
    def no_response_result(cls, *, resolved_model: str, elapsed_s: float) -> ModelCallResult:
        """The model returned but its text was empty/whitespace-only -> force-scored 0 by runner."""
        return cls(response_raw=NO_RESPONSE, resolved_model=resolved_model, elapsed_s=elapsed_s)

    @classmethod
    def error(
        cls, *, reason_class: str, elapsed_s: float, resolved_model: str = ""
    ) -> ModelCallResult:
        """A failed call with a switchboard ``reason_class`` (validated in __post_init__)."""
        return cls(
            response_raw="",
            resolved_model=resolved_model,
            elapsed_s=elapsed_s,
            reason_class=reason_class,
        )


def resolved_model_of(payload: dict[str, object], requested: str) -> str:
    """The concrete model id a call resolved to, from a top-level ``model`` field (drift detection).

    ONE source of truth for both adapters (they share the same shape here): OpenAI-compatible chat
    responses echo the served model at top-level ``model`` (llama-swap reports the actually-loaded
    GGUF there, so a swap away from the requested alias is visible), and the claude
    ``--output-format json`` envelope reports the resolved concrete model at top-level ``model``
    too. Falls back to the requested alias when the field is absent/non-string, so the row always
    carries a model id; drift is simply undetectable on that row.
    """
    model = payload.get("model")
    return model if isinstance(model, str) and model else requested
