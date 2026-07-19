"""measure-twice run-config resolution.

Mirrors switchboard's ``load_config`` discipline (``switchboard/switchboard/config.py``)
and operationalizes ``dev/.claude/rules/measurement-validity.md`` § fail loud: a run's
config is resolved from the FIRST provided/existing source, and the resolved origin is
recorded in ``RunConfig.config_source`` so it lands in every run manifest (the fallback
risk measurement-validity §4 warns about is closed by recording *where* config came from).

Resolution order (first hit wins):

  1. explicit ``--config`` path         -> config_source ``"explicit:<path>"``
  2. ``$MEASURE_TWICE_CONFIG`` env var   -> config_source ``"env:MEASURE_TWICE_CONFIG"``
  3. ``<cwd>/measure-twice.json``        -> config_source ``"cwd:measure-twice.json"``
  4. built-in defaults                   -> config_source ``"defaults"``

Fail-loud contract:
  * A PRESENT-but-broken source (invalid JSON, unknown/mistyped key, wrong value type)
    ALWAYS raises ``ConfigError`` — NEVER a silent fall-back to defaults. Silent degradation
    produces numbers indistinguishable from real ones (measurement-validity § fail loud).
  * Tiers 1 and 2 are *provided*: the operator named a file (arg or env var), so a
    missing/unreadable one raises. An env var that is PRESENT-but-empty is still "provided"
    (``MEASURE_TWICE_CONFIG=""`` is an operator naming nothing) — it flows into the
    require-exists check and raises, symmetric with the explicit tier. Not a fall-through.
  * Tier 3 is *implicit*: ``<cwd>/measure-twice.json`` counts as a source only when it
    exists; simply being ABSENT there is not malformed — resolution falls through to defaults.

Shape constants (URL regex, safe-name regex, the reasoning-model token floor) are IMPORTED
from switchboard, never re-declared — code-quality.md § one source of truth. measure-twice's
default roster includes general-35b, the same reasoning model switchboard's floor exists for.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, cast

# One source of truth — imported from switchboard, not re-declared (plan §8 D6, code-quality):
#   _BASE_URL_RE   : ^https?://[A-Za-z0-9._:\-]+(?:/[A-Za-z0-9._\-/]*)?$   (safe http(s) URL)
#   _SAFE_NAME_RE  : ^[A-Za-z0-9._\-]+$   (identifier-safe model/task names; plan §3)
#   MIN_MAX_TOKENS : 600   (general-35b spends ~400-620 tokens on reasoning_content before the
#                           answer lands; a smaller budget truncates to content="" — the
#                           switchboard "#1 footgun". See switchboard/switchboard/config.py.)
# switchboard is fully typed but ships no py.typed marker, so mypy sees it as untyped; the
# scoped ignore keeps --strict green without editing the sibling package or the mypy config.
from switchboard.config import (  # type: ignore[import-untyped]
    _BASE_URL_RE,
    _SAFE_NAME_RE,
    MIN_MAX_TOKENS,
)

# --- Built-in defaults (plan.md Appendix § Default run config) ---------------------------
DEFAULT_ROSTER: list[str] = ["general-35b", "coder-30b", "haiku", "sonnet", "opus"]
DEFAULT_LOCAL_BASE_URL = "http://localhost:8080/v1"
DEFAULT_LOCAL_MAX_TOKENS = 2000
# Per-call timeout (seconds) for the local endpoint. 120s comfortably covers a WARM
# reasoning-model verdict (~16-60s); raise it via config when COLD model loads are expected
# to exceed it (a large model swapping into VRAM can take minutes — plan §M1). The local
# adapter imports this as its fallback default; the runner threads config.local_timeout_s per call.
DEFAULT_LOCAL_TIMEOUT_S: float = 120.0
DEFAULT_CLAUDE_POOL = 2
DEFAULT_SAMPLES_PER_CELL = 1
DEFAULT_JUDGES: list[str] = ["sonnet"]
DEFAULT_MAX_CALLS = 500

# The local-endpoint token floor, aliased from switchboard so measure-twice enforces the
# identical reasoning-model minimum without re-typing the number.
MIN_LOCAL_MAX_TOKENS = MIN_MAX_TOKENS

# Env var an operator sets to point at a config file (highest precedence after --config).
ENV_VAR = "MEASURE_TWICE_CONFIG"
# The implicit per-project config filename looked up in the current working directory.
CWD_CONFIG_NAME = "measure-twice.json"


class ConfigError(ValueError):
    """Raised when a measure-twice config value fails validation or a named file is missing.

    Fail-loud sentinel: a present-but-broken config, or a provided-but-absent file, surfaces
    as this rather than silently degrading to defaults (measurement-validity § fail loud).
    """


def _validate_name_list(value: object, label: str) -> None:
    """A non-empty list of identifier-safe, non-empty model-name strings (plan §3)."""
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be a list of strings, got {type(value).__name__}")
    if not value:
        raise ConfigError(f"{label} must contain at least one model name")
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ConfigError(f"{label}[{i}] must be a non-empty string, got {item!r}")
        if not _SAFE_NAME_RE.match(item):
            raise ConfigError(
                f"{label}[{i}] {item!r} contains unsafe characters "
                f"(allowed: letters, digits, '.', '_', '-')"
            )


def _validate_int_at_least(value: object, label: str, minimum: int) -> None:
    # bool is an int subclass; reject it so a stray ``true`` can't masquerade as a count.
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{label} must be an int >= {minimum}, got {value!r}")


def _validate_positive_number(value: object, label: str) -> None:
    # Seconds — accept int OR float; reject bool (an int subclass) and non-positive values.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{label} must be a positive number of seconds, got {value!r}")


def _validate_base_url(value: object) -> None:
    # Mirrors switchboard's base_url guard: http(s) only, no injection/path-traversal chars.
    if not isinstance(value, str) or not _BASE_URL_RE.match(value) or ".." in value:
        raise ConfigError(f"local_base_url {value!r} is not a safe http(s) URL")


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Immutable, validated run configuration for a benchmark sweep.

    Field defaults are the plan Appendix's built-in defaults, so ``RunConfig()`` is the
    tier-4 default config. ``config_source`` records WHERE the config was resolved from
    (``"explicit:<path>"`` / ``"env:MEASURE_TWICE_CONFIG"`` / ``"cwd:measure-twice.json"`` /
    ``"defaults"``) and is set by the resolver, never carried in a config file — a file that
    tries to set ``config_source`` is rejected as an unknown key by ``from_mapping``.
    """

    roster: list[str] = field(default_factory=lambda: list(DEFAULT_ROSTER))
    local_base_url: str = DEFAULT_LOCAL_BASE_URL
    local_max_tokens: int = DEFAULT_LOCAL_MAX_TOKENS
    local_timeout_s: float = DEFAULT_LOCAL_TIMEOUT_S
    claude_pool: int = DEFAULT_CLAUDE_POOL
    samples_per_cell: int = DEFAULT_SAMPLES_PER_CELL
    judges: list[str] = field(default_factory=lambda: list(DEFAULT_JUDGES))
    max_calls: int = DEFAULT_MAX_CALLS
    config_source: str = "defaults"

    def __post_init__(self) -> None:
        _validate_name_list(self.roster, "roster")
        _validate_base_url(self.local_base_url)
        _validate_int_at_least(self.local_max_tokens, "local_max_tokens", MIN_LOCAL_MAX_TOKENS)
        _validate_positive_number(self.local_timeout_s, "local_timeout_s")
        _validate_int_at_least(self.claude_pool, "claude_pool", 1)
        _validate_int_at_least(self.samples_per_cell, "samples_per_cell", 1)
        _validate_name_list(self.judges, "judges")
        _validate_int_at_least(self.max_calls, "max_calls", 1)
        if not isinstance(self.config_source, str) or not self.config_source:
            raise ConfigError(
                f"config_source must be a non-empty string, got {self.config_source!r}"
            )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object], config_source: str) -> RunConfig:
        """Construct from a parsed config mapping, rejecting any unknown top-level key.

        ``RunConfig(**data)`` would raise a raw ``TypeError`` on an unexpected kwarg; this
        surfaces a clean ``ConfigError`` naming the offending key(s) instead. Value validation
        is still done by ``__post_init__`` once the keys are known-good. ``config_source`` is
        excluded from the allow-list, so a config file cannot spoof its own provenance.
        """
        unknown = set(data) - ALLOWED_CONFIG_FIELDS
        if unknown:
            raise ConfigError(
                f"unknown config key(s): {sorted(unknown)}; "
                f"allowed: {sorted(ALLOWED_CONFIG_FIELDS)}"
            )
        # Values are object-typed at the boundary; __post_init__ does the real validation.
        return cls(config_source=config_source, **cast("Mapping[str, Any]", data))


# The single source of truth for "which top-level keys a config file may contain": the
# dataclass field set MINUS ``config_source`` (resolver-owned metadata, not file content).
ALLOWED_CONFIG_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(RunConfig) if f.name != "config_source"
)


def load_config(explicit_path: str | None = None) -> RunConfig:
    """Resolve, read, and validate a run config; return a fully-populated ``RunConfig``.

    Resolution order (first hit wins): explicit ``--config`` path -> ``$MEASURE_TWICE_CONFIG``
    -> ``<cwd>/measure-twice.json`` -> built-in defaults. See the module docstring for the
    full fail-loud contract. Once a source is selected, ANY read / JSON / validation failure
    raises ``ConfigError`` — the run must never proceed on a config it could not honestly load.
    """
    candidate: str | None = None
    source_label = ""
    require_exists = False

    if explicit_path is not None:
        candidate = explicit_path
        source_label = f"explicit:{explicit_path}"
        require_exists = True
    elif ENV_VAR in os.environ:
        # PRESENT (even if empty) means the operator named a file -> provided tier, must exist.
        # Truthiness would let MEASURE_TWICE_CONFIG="" silently fall through — a fail-loud gap.
        candidate = os.environ[ENV_VAR]
        source_label = f"env:{ENV_VAR}"
        require_exists = True
    else:
        cwd_path = Path.cwd() / CWD_CONFIG_NAME
        if cwd_path.is_file():
            candidate = str(cwd_path)
            source_label = f"cwd:{CWD_CONFIG_NAME}"

    if candidate is None:
        return RunConfig(config_source="defaults")

    p = Path(candidate)
    if require_exists and not p.is_file():
        raise ConfigError(f"config file not found: {candidate!r}")

    try:
        with p.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # UnicodeDecodeError subclasses ValueError, NOT OSError — catch it explicitly so a
        # bad-bytes file surfaces as ConfigError, not an unrelated escape.
        raise ConfigError(f"could not read config {candidate!r}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config {candidate!r} must be a JSON object, got {type(raw).__name__}")

    return RunConfig.from_mapping(cast("Mapping[str, object]", raw), source_label)
