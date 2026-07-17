"""measure-twice model adapters — offline-testable behind client-factory DI seams.

``local`` calls the OpenAI-compatible local endpoint (``localhost:8080``); ``claude_cli`` shells
out to the ``claude`` CLI (subscription OAuth). Both return a :class:`ModelCallResult` and map
failures onto **switchboard's** ``reason_class`` taxonomy (imported in :mod:`base`, never
re-declared). The :data:`NO_RESPONSE` sentinel — empty/whitespace model output — is defined in
:mod:`base` and re-exported here; the runner (Step 4) force-scores it 0 before any judging.

The DI seam is a *factory callable* per adapter (default constructs the real client from stdlib
``urllib`` / ``subprocess``; tests inject a stub), so the whole engine runs offline with ZERO
live network or ``claude`` calls (readiness_bench pattern, plan §5).
"""

from __future__ import annotations

from measure_twice.adapters.base import (
    NO_RESPONSE,
    REASON_CLASSES,
    AdapterError,
    ModelCallResult,
)

__all__ = [
    "NO_RESPONSE",
    "REASON_CLASSES",
    "AdapterError",
    "ModelCallResult",
]
