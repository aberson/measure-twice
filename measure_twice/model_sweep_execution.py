"""Validated execution contracts and receipts for Instrument A model sweeps.

Instrument A dispatch is profile-driven: every public model alias is bound to exactly one
provider and one requested provider identity. There is deliberately no "anything else is local"
fallback. The same immutable profile also owns the Claude prompt-only context contract whose hash
is recorded in each new run manifest.

Hashes use canonical JSON (sorted keys, compact separators, ASCII escaping) so a profile or
receipt has the same digest on every supported platform. Runtime-only evidence such as the
resolved Claude executable path and CLI version belongs to the execution receipt, not the static
profile/context hashes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from measure_twice.adapters.base import UNRESOLVED_MODEL_ID

__all__ = [
    "CLAUDE_ARGV_TEMPLATE",
    "CLAUDE_ENV_ALLOWLIST",
    "CLAUDE_EXECUTABLE",
    "CLAUDE_MODEL_PLACEHOLDER",
    "CLAUDE_SEALING_MODE",
    "DEFAULT_EXECUTION_PROFILE",
    "DEFAULT_GEMINI_CONTEXT",
    "EXECUTION_RECEIPT_SCHEMA_VERSION",
    "GEMINI_REQUEST_CONTRACT",
    "GEMINI_THINKING_LEVELS",
    "PROFILE_SCHEMA_VERSION",
    "PROVIDER_CLAUDE",
    "PROVIDER_GEMINI",
    "PROVIDER_LOCAL",
    "SUPPORTED_PROVIDERS",
    "ClaudeContextProfile",
    "ClaudeRuntimeEvidence",
    "ExecutionProfileError",
    "ExecutionReceipt",
    "GeminiContextProfile",
    "ModelBinding",
    "ModelSweepExecutionProfile",
    "canonical_sha256",
    "is_concrete_provider_identity",
]

PROFILE_SCHEMA_VERSION: Final[int] = 1
EXECUTION_RECEIPT_SCHEMA_VERSION: Final[int] = 1
PROVIDER_LOCAL: Final[str] = "local-openai"
PROVIDER_CLAUDE: Final[str] = "claude-cli"
PROVIDER_GEMINI: Final[str] = "gemini-api"
SUPPORTED_PROVIDERS: Final[frozenset[str]] = frozenset(
    {PROVIDER_LOCAL, PROVIDER_CLAUDE, PROVIDER_GEMINI}
)


def is_concrete_provider_identity(value: object, provider: str) -> bool:
    """Whether a stored row value is usable as provider identity evidence.

    Claude's public aliases are requests, not resolved model IDs. Its returned ID must carry the
    ``claude-`` model namespace; local OpenAI-compatible servers and the Gemini API return their
    configured/served model names directly (Gemini's ``modelVersion``), so any non-blank,
    whitespace-free value that is not the unresolved sentinel qualifies. Malformed historical row
    values stay visible but cannot qualify an arm.
    """
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return False
    if value == UNRESOLVED_MODEL_ID:
        return False
    if provider == PROVIDER_CLAUDE:
        return re.fullmatch(r"claude-[A-Za-z0-9][A-Za-z0-9._-]*", value) is not None
    return bool(value.strip())


CLAUDE_SEALING_MODE: Final[str] = "prompt-only-v1"
CLAUDE_EXECUTABLE: Final[str] = "claude"
CLAUDE_MODEL_PLACEHOLDER: Final[str] = "{requested_model}"

# Complete v1 model-call argv after the absolute executable path. The requested model placeholder
# is substituted only after a validated binding has been selected. ``--bare`` is absent because
# it disables subscription OAuth/keychain reads.
CLAUDE_ARGV_TEMPLATE: Final[tuple[str, ...]] = (
    "-p",
    "--model",
    CLAUDE_MODEL_PLACEHOLDER,
    "--output-format",
    "json",
    "--safe-mode",
    "--tools",
    "",
    "--disable-slash-commands",
    "--no-chrome",
    "--no-session-persistence",
)

# Ambient variables are denied by default. These names are the minimal cross-platform process,
# locale, temporary-directory, user/keychain, and subscription-OAuth inputs the native CLI may
# need. Ambient proxy/CA overrides and customization/provider-selection variables are absent.
CLAUDE_ENV_ALLOWLIST: Final[tuple[str, ...]] = (
    "APPDATA",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)

# Profile ids/public aliases are data identifiers. Claude's requested model is additionally an
# argv token that may cross a Windows ``.cmd`` launcher, so it gets a provider-specific grammar:
# an ASCII letter/digit first, then only letters, digits, dot, underscore, or hyphen. This admits
# supported aliases and concrete Claude ids (for example ``claude-sonnet-4-5-20250929``) while
# excluding whitespace, path separators, a leading option marker, and shell metacharacters or
# expansion syntax. Local OpenAI-compatible ids retain their existing single-line contract because
# those JSON-body ids may legitimately contain provider namespaces such as ``org/model:tag``.
_SAFE_ID_RE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_SAFE_CLAUDE_MODEL_RE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
# A Gemini requested model crosses into a REST URL path segment
# (``.../models/{requested_model}:generateContent``), so it gets the same strict single-token
# grammar as a Claude argv token: an ASCII letter/digit first, then only letters, digits, dot,
# underscore, or hyphen. This admits ``gemini-3.8-flash`` while excluding slashes, colons,
# whitespace, and URL/shell metacharacters that could redirect the request.
_SAFE_GEMINI_MODEL_RE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# The single Gemini request contract this adapter speaks. It selects a code-owned HTTPS endpoint and
# a fixed text-only single-candidate request shape (plan §6 D2); the URL is NEVER repository- or
# profile-selected. The allowed thinking levels mirror Google's generateContent thinkingConfig.
GEMINI_REQUEST_CONTRACT: Final[str] = "generate-content-text-v1"
GEMINI_THINKING_LEVELS: Final[frozenset[str]] = frozenset({"low", "medium", "high"})
_DEFAULT_GEMINI_MAX_OUTPUT_TOKENS: Final[int] = 4096
_DEFAULT_GEMINI_THINKING_LEVEL: Final[str] = "low"
_DEFAULT_GEMINI_TIMEOUT_S: Final[float] = 120.0


class ExecutionProfileError(ValueError):
    """A model-sweep execution profile violated its strict v1 contract."""


def canonical_sha256(value: object) -> str:
    """Return SHA-256 over the canonical JSON encoding used by profile evidence."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_keys(
    value: object,
    required: frozenset[str],
    optional: frozenset[str],
    *,
    label: str,
) -> Mapping[str, object]:
    """Require every ``required`` key, permit every ``optional`` key, reject any other key.

    The one owner of the "known-keys" contract shared by the strict-exact schemas (``optional``
    empty) and the additive ones that grew an optional block — the Gemini context on a profile and
    on a receipt. An absent optional key is not an error; an unknown key still fails loud, so a
    typo can never be silently ignored (measurement-validity § fail loud).
    """
    if not isinstance(value, dict):
        raise ExecutionProfileError(f"{label} must be a JSON object")
    clean = cast("Mapping[str, object]", value)
    actual = set(clean)
    missing = sorted(required - actual)
    unknown = sorted(actual - (required | optional))
    if missing or unknown:
        # Keep the historical "must be exactly" wording when there is no optional key, so the strict
        # schemas' user-facing message is unchanged; name the optional key(s) only when they exist.
        if optional:
            allowed = sorted(required | optional)
            raise ExecutionProfileError(
                f"{label} keys must include {sorted(required)!r} and allow only {allowed!r}; "
                f"missing={missing!r} unknown={unknown!r}"
            )
        raise ExecutionProfileError(
            f"{label} keys must be exactly {sorted(required)!r}; "
            f"missing={missing!r} unknown={unknown!r}"
        )
    return clean


def _require_exact_keys(
    value: object, expected: frozenset[str], *, label: str
) -> Mapping[str, object]:
    return _require_keys(value, expected, frozenset(), label=label)


def _require_safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise ExecutionProfileError(
            f"{label} must contain only letters, digits, '.', '_', or '-', got {value!r}"
        )
    return value


def _require_nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in ("\x00", "\r", "\n")):
        raise ExecutionProfileError(f"{label} must be a non-empty single-line string")
    return value


def _require_claude_model_token(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_CLAUDE_MODEL_RE.fullmatch(value):
        raise ExecutionProfileError(
            f"{label} must be a safe Claude model token beginning with a letter or digit and "
            f"containing only letters, digits, '.', '_', or '-', got {value!r}"
        )
    return value


def _require_gemini_model_token(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_GEMINI_MODEL_RE.fullmatch(value):
        raise ExecutionProfileError(
            f"{label} must be a safe Gemini model token beginning with a letter or digit and "
            f"containing only letters, digits, '.', '_', or '-', got {value!r}"
        )
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    # bool is an int subclass; reject it so a stray ``true`` cannot masquerade as a count.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExecutionProfileError(f"{label} must be an int >= 1, got {value!r}")
    return value


def _require_finite_positive_number(value: object, *, label: str) -> float:
    # Seconds — accept int OR float; reject bool, non-finite (nan/inf), and non-positive values.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ExecutionProfileError(
            f"{label} must be a finite positive number of seconds, got {value!r}"
        )
    return float(value)


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """One public alias bound to an explicit provider and requested provider model."""

    alias: str
    provider: str
    requested_model: str

    def __post_init__(self) -> None:
        _require_safe_id(self.alias, label="model binding.alias")
        if not isinstance(self.provider, str) or self.provider not in SUPPORTED_PROVIDERS:
            raise ExecutionProfileError(
                f"model binding.provider must be one of {sorted(SUPPORTED_PROVIDERS)!r}, "
                f"got {self.provider!r}"
            )
        if self.provider == PROVIDER_CLAUDE:
            _require_claude_model_token(self.requested_model, label="model binding.requested_model")
        elif self.provider == PROVIDER_GEMINI:
            _require_gemini_model_token(self.requested_model, label="model binding.requested_model")
        else:
            _require_nonempty_string(self.requested_model, label="model binding.requested_model")

    @classmethod
    def from_mapping(cls, value: object) -> ModelBinding:
        clean = _require_exact_keys(
            value,
            frozenset({"alias", "provider", "requested_model"}),
            label="model binding",
        )
        return cls(
            alias=cast("str", clean["alias"]),
            provider=cast("str", clean["provider"]),
            requested_model=cast("str", clean["requested_model"]),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "alias": self.alias,
            "provider": self.provider,
            "requested_model": self.requested_model,
        }


@dataclass(frozen=True, slots=True)
class ClaudeContextProfile:
    """The frozen v1 Claude executable/argv/environment sealing contract."""

    executable: str
    sealing_mode: str
    argv_template: tuple[str, ...]
    environment_allowlist: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.executable != CLAUDE_EXECUTABLE:
            raise ExecutionProfileError(
                "execution profile.claude.executable must be the literal bare launcher "
                f"{CLAUDE_EXECUTABLE!r}; paths and custom executable names are forbidden"
            )
        if self.sealing_mode != CLAUDE_SEALING_MODE:
            raise ExecutionProfileError(
                "execution profile.claude.sealing_mode must equal "
                f"{CLAUDE_SEALING_MODE!r}, got {self.sealing_mode!r}"
            )
        if self.argv_template != CLAUDE_ARGV_TEMPLATE:
            raise ExecutionProfileError(
                "execution profile.claude.argv_template must equal the frozen prompt-only v1 "
                "template"
            )
        if self.environment_allowlist != CLAUDE_ENV_ALLOWLIST:
            raise ExecutionProfileError(
                "execution profile.claude.environment_allowlist must equal the frozen v1 allowlist"
            )

    @classmethod
    def from_mapping(cls, value: object) -> ClaudeContextProfile:
        clean = _require_exact_keys(
            value,
            frozenset({"executable", "sealing_mode", "argv_template", "environment_allowlist"}),
            label="execution profile.claude",
        )
        raw_argv = clean["argv_template"]
        raw_env = clean["environment_allowlist"]
        if not isinstance(raw_argv, list) or not all(isinstance(v, str) for v in raw_argv):
            raise ExecutionProfileError(
                "execution profile.claude.argv_template must be a list of strings"
            )
        if not isinstance(raw_env, list) or not all(isinstance(v, str) for v in raw_env):
            raise ExecutionProfileError(
                "execution profile.claude.environment_allowlist must be a list of strings"
            )
        return cls(
            executable=cast("str", clean["executable"]),
            sealing_mode=cast("str", clean["sealing_mode"]),
            argv_template=tuple(cast("Sequence[str]", raw_argv)),
            environment_allowlist=tuple(cast("Sequence[str]", raw_env)),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "executable": self.executable,
            "sealing_mode": self.sealing_mode,
            "argv_template": list(self.argv_template),
            "environment_allowlist": list(self.environment_allowlist),
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class GeminiContextProfile:
    """The frozen Gemini generateContent request contract for a sweep (plan §6 D2).

    Pins the request-shape selector plus the exact generation settings and transport timeout so a
    resume can never mix request contracts. Only ``request_contract`` selects the code-owned HTTPS
    endpoint and text-only single-candidate shape — never a stored URL. This object is deliberately
    OUTSIDE the default profile: adding it changes the composite context hash (and therefore the
    execution/receipt identity) only for profiles that actually bind a Gemini model.
    """

    request_contract: str
    max_output_tokens: int
    thinking_level: str
    timeout_s: float

    def __post_init__(self) -> None:
        if self.request_contract != GEMINI_REQUEST_CONTRACT:
            raise ExecutionProfileError(
                "execution profile.gemini.request_contract must equal "
                f"{GEMINI_REQUEST_CONTRACT!r}, got {self.request_contract!r}"
            )
        _require_positive_int(
            self.max_output_tokens, label="execution profile.gemini.max_output_tokens"
        )
        if self.thinking_level not in GEMINI_THINKING_LEVELS:
            raise ExecutionProfileError(
                "execution profile.gemini.thinking_level must be one of "
                f"{sorted(GEMINI_THINKING_LEVELS)!r}, got {self.thinking_level!r}"
            )
        _require_finite_positive_number(self.timeout_s, label="execution profile.gemini.timeout_s")

    @classmethod
    def from_mapping(cls, value: object) -> GeminiContextProfile:
        clean = _require_exact_keys(
            value,
            frozenset({"request_contract", "max_output_tokens", "thinking_level", "timeout_s"}),
            label="execution profile.gemini",
        )
        raw_tokens = clean["max_output_tokens"]
        raw_timeout = clean["timeout_s"]
        # Reject bool early: it is an int subclass and would slip past the dataclass field typing,
        # so a ``true`` in JSON must not masquerade as a token count or a timeout.
        if isinstance(raw_tokens, bool) or not isinstance(raw_tokens, int):
            raise ExecutionProfileError(
                "execution profile.gemini.max_output_tokens must be an integer"
            )
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise ExecutionProfileError("execution profile.gemini.timeout_s must be a number")
        return cls(
            request_contract=cast("str", clean["request_contract"]),
            max_output_tokens=raw_tokens,
            thinking_level=cast("str", clean["thinking_level"]),
            timeout_s=float(raw_timeout),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "request_contract": self.request_contract,
            "max_output_tokens": self.max_output_tokens,
            "thinking_level": self.thinking_level,
            "timeout_s": self.timeout_s,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_mapping())


# The default sample Gemini request settings (plan §6 D2: 4096 output tokens, low thinking, 120s).
# Not part of any default profile — a profile opts in explicitly by binding a Gemini model.
DEFAULT_GEMINI_CONTEXT: Final[GeminiContextProfile] = GeminiContextProfile(
    request_contract=GEMINI_REQUEST_CONTRACT,
    max_output_tokens=_DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
    thinking_level=_DEFAULT_GEMINI_THINKING_LEVEL,
    timeout_s=_DEFAULT_GEMINI_TIMEOUT_S,
)


@dataclass(frozen=True, slots=True)
class ModelSweepExecutionProfile:
    """Strict, versioned Instrument A provider and Claude-context profile.

    An optional ``gemini`` request contract is present exactly when a Gemini binding exists. When
    absent the profile serializes and hashes byte-for-byte as before (additive compatibility, plan
    §6 D5); when present it joins the composite :attr:`context_profile_sha256`.
    """

    schema_version: int
    id: str
    models: tuple[ModelBinding, ...]
    claude: ClaudeContextProfile
    gemini: GeminiContextProfile | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != PROFILE_SCHEMA_VERSION
        ):
            raise ExecutionProfileError(
                f"unsupported execution profile.schema_version {self.schema_version!r}; "
                f"supported version is {PROFILE_SCHEMA_VERSION}"
            )
        _require_safe_id(self.id, label="execution profile.id")
        if not isinstance(self.models, tuple) or not all(
            isinstance(model, ModelBinding) for model in self.models
        ):
            raise ExecutionProfileError(
                "execution profile.models must be a tuple of validated model bindings"
            )
        if not isinstance(self.claude, ClaudeContextProfile):
            raise ExecutionProfileError(
                "execution profile.claude must be a validated Claude context profile"
            )
        if self.gemini is not None and not isinstance(self.gemini, GeminiContextProfile):
            raise ExecutionProfileError(
                "execution profile.gemini must be a validated Gemini context profile or null"
            )
        if not self.models:
            raise ExecutionProfileError(
                "execution profile.models must contain at least one binding"
            )
        aliases = [model.alias for model in self.models]
        duplicates = sorted({alias for alias in aliases if aliases.count(alias) > 1})
        if duplicates:
            raise ExecutionProfileError(
                f"execution profile.models contains duplicate alias(es): {duplicates!r}"
            )
        # The Gemini context is required exactly when a Gemini binding exists: a binding without a
        # context cannot be called, and a context without a binding would silently perturb the
        # composite context hash of a Gemini-free profile (plan §6 D2/D5).
        has_gemini_binding = any(model.provider == PROVIDER_GEMINI for model in self.models)
        if has_gemini_binding and self.gemini is None:
            raise ExecutionProfileError(
                "execution profile with a 'gemini-api' binding must define a 'gemini' context"
            )
        if self.gemini is not None and not has_gemini_binding:
            raise ExecutionProfileError(
                "execution profile defines a 'gemini' context but binds no 'gemini-api' model"
            )

    @classmethod
    def from_mapping(cls, value: object) -> ModelSweepExecutionProfile:
        clean = _require_keys(
            value,
            frozenset({"schema_version", "id", "models", "claude"}),
            frozenset({"gemini"}),
            label="execution profile",
        )
        raw_models = clean["models"]
        if not isinstance(raw_models, list):
            raise ExecutionProfileError("execution profile.models must be a list")
        raw_gemini = clean.get("gemini")
        return cls(
            schema_version=cast("int", clean["schema_version"]),
            id=cast("str", clean["id"]),
            models=tuple(ModelBinding.from_mapping(model) for model in raw_models),
            claude=ClaudeContextProfile.from_mapping(clean["claude"]),
            gemini=None if raw_gemini is None else GeminiContextProfile.from_mapping(raw_gemini),
        )

    def to_mapping(self) -> dict[str, object]:
        mapping: dict[str, object] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "models": [model.to_mapping() for model in self.models],
            "claude": self.claude.to_mapping(),
        }
        # Omit the optional Gemini block entirely when absent so a Gemini-free profile keeps its
        # exact historical serialized bytes and every derived hash (plan §6 D5).
        if self.gemini is not None:
            mapping["gemini"] = self.gemini.to_mapping()
        return mapping

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(model.alias for model in self.models)

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def context_profile_sha256(self) -> str:
        """The shared context-hash owner recorded in a receipt and compared on resume.

        With no Gemini context this is EXACTLY the Claude context hash, so Gemini-free receipts keep
        their historical ``context_profile_sha256`` (and therefore their ``receipt_sha256``). When a
        Gemini context is present the hash is a composite over both request contracts, so changing
        any Gemini setting rejects resume before any run-store mutation (plan §6 D2/D5).
        """
        if self.gemini is None:
            return self.claude.sha256
        return canonical_sha256(
            {"claude": self.claude.to_mapping(), "gemini": self.gemini.to_mapping()}
        )

    @property
    def provider_profile_sha256(self) -> str:
        return canonical_sha256([model.to_mapping() for model in self.models])

    def binding_for(self, alias: str) -> ModelBinding:
        for model in self.models:
            if model.alias == alias:
                return model
        raise ExecutionProfileError(
            f"model alias {alias!r} has no explicit provider binding in execution profile "
            f"{self.id!r}"
        )

    def bindings_for(self, aliases: Sequence[str]) -> tuple[ModelBinding, ...]:
        return tuple(self.binding_for(alias) for alias in aliases)


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeEvidence:
    """Runtime identity of the doctor-validated Claude executable used by a run."""

    executable: str
    version: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.executable, label="execution receipt.claude_cli.executable")
        _require_nonempty_string(self.version, label="execution receipt.claude_cli.version")

    @classmethod
    def from_mapping(cls, value: object) -> ClaudeRuntimeEvidence:
        clean = _require_exact_keys(
            value,
            frozenset({"executable", "version"}),
            label="execution receipt.claude_cli",
        )
        return cls(
            executable=cast("str", clean["executable"]),
            version=cast("str", clean["version"]),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"executable": self.executable, "version": self.version}


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Bind selected providers to a preflighted runtime, without claiming they all executed.

    Rubric runs select both collection models and judges; verdict/exact runs select only their
    collection roster. Actual provider-returned identities remain response evidence.
    """

    schema_version: int
    profile_id: str
    execution_profile_sha256: str
    provider_profile_sha256: str
    context_profile_sha256: str
    bindings: tuple[ModelBinding, ...]
    sealing_mode: str
    claude_cli: ClaudeRuntimeEvidence | None
    gemini: GeminiContextProfile | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != EXECUTION_RECEIPT_SCHEMA_VERSION
        ):
            raise ExecutionProfileError(
                f"unsupported execution receipt.schema_version {self.schema_version!r}; "
                f"supported version is {EXECUTION_RECEIPT_SCHEMA_VERSION}"
            )
        _require_safe_id(self.profile_id, label="execution receipt.profile_id")
        for label, digest in (
            ("execution_profile_sha256", self.execution_profile_sha256),
            ("provider_profile_sha256", self.provider_profile_sha256),
            ("context_profile_sha256", self.context_profile_sha256),
        ):
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ExecutionProfileError(
                    f"execution receipt.{label} must be a lowercase SHA-256 digest"
                )
        if not isinstance(self.bindings, tuple) or not all(
            isinstance(binding, ModelBinding) for binding in self.bindings
        ):
            raise ExecutionProfileError(
                "execution receipt.bindings must be a tuple of validated model bindings"
            )
        if not self.bindings:
            raise ExecutionProfileError("execution receipt.bindings must not be empty")
        aliases = [binding.alias for binding in self.bindings]
        if len(set(aliases)) != len(aliases):
            raise ExecutionProfileError("execution receipt.bindings contains duplicate aliases")
        if self.sealing_mode != CLAUDE_SEALING_MODE:
            raise ExecutionProfileError(
                f"execution receipt.sealing_mode must equal {CLAUDE_SEALING_MODE!r}"
            )
        needs_claude = any(binding.provider == PROVIDER_CLAUDE for binding in self.bindings)
        if self.claude_cli is not None and not isinstance(self.claude_cli, ClaudeRuntimeEvidence):
            raise ExecutionProfileError(
                "execution receipt.claude_cli must be validated runtime evidence or null"
            )
        if needs_claude != (self.claude_cli is not None):
            raise ExecutionProfileError(
                "execution receipt.claude_cli must be present exactly when selected bindings "
                "use provider 'claude-cli'"
            )
        needs_gemini = any(binding.provider == PROVIDER_GEMINI for binding in self.bindings)
        if self.gemini is not None and not isinstance(self.gemini, GeminiContextProfile):
            raise ExecutionProfileError(
                "execution receipt.gemini must be a validated Gemini context profile or null"
            )
        if needs_gemini != (self.gemini is not None):
            raise ExecutionProfileError(
                "execution receipt.gemini must be present exactly when selected bindings "
                "use provider 'gemini-api'"
            )

    @classmethod
    def create(
        cls,
        profile: ModelSweepExecutionProfile,
        bindings: Sequence[ModelBinding],
        *,
        claude_executable: str | None,
        claude_version: str | None,
    ) -> ExecutionReceipt:
        selected = tuple(bindings)
        for binding in selected:
            try:
                expected = profile.binding_for(binding.alias)
            except ExecutionProfileError as exc:
                raise ExecutionProfileError(
                    "execution receipt bindings must come from the selected execution profile"
                ) from exc
            if binding != expected:
                raise ExecutionProfileError(
                    "execution receipt binding differs from the selected execution profile for "
                    f"alias {binding.alias!r}"
                )
        uses_claude = any(binding.provider == PROVIDER_CLAUDE for binding in selected)
        runtime: ClaudeRuntimeEvidence | None = None
        if uses_claude:
            if claude_executable is None or claude_version is None:
                raise ExecutionProfileError(
                    "Claude bindings require doctor-validated executable and version evidence"
                )
            runtime = ClaudeRuntimeEvidence(claude_executable, claude_version)
        uses_gemini = any(binding.provider == PROVIDER_GEMINI for binding in selected)
        gemini_context: GeminiContextProfile | None = None
        if uses_gemini:
            if profile.gemini is None:  # unreachable: profile.__post_init__ enforces this pairing.
                raise ExecutionProfileError(
                    "Gemini bindings require the profile's 'gemini' request contract"
                )
            gemini_context = profile.gemini
        return cls(
            schema_version=EXECUTION_RECEIPT_SCHEMA_VERSION,
            profile_id=profile.id,
            execution_profile_sha256=profile.sha256,
            provider_profile_sha256=profile.provider_profile_sha256,
            context_profile_sha256=profile.context_profile_sha256,
            bindings=selected,
            sealing_mode=profile.claude.sealing_mode,
            claude_cli=runtime,
            gemini=gemini_context,
        )

    @classmethod
    def from_mapping(cls, value: object) -> ExecutionReceipt:
        clean = _require_keys(
            value,
            frozenset(
                {
                    "schema_version",
                    "profile_id",
                    "execution_profile_sha256",
                    "provider_profile_sha256",
                    "context_profile_sha256",
                    "bindings",
                    "sealing_mode",
                    "claude_cli",
                    "receipt_sha256",
                }
            ),
            frozenset({"gemini"}),
            label="execution receipt",
        )
        raw_bindings = clean["bindings"]
        if not isinstance(raw_bindings, list):
            raise ExecutionProfileError("execution receipt.bindings must be a list")
        raw_cli = clean["claude_cli"]
        raw_gemini = clean.get("gemini")
        receipt = cls(
            schema_version=cast("int", clean["schema_version"]),
            profile_id=cast("str", clean["profile_id"]),
            execution_profile_sha256=cast("str", clean["execution_profile_sha256"]),
            provider_profile_sha256=cast("str", clean["provider_profile_sha256"]),
            context_profile_sha256=cast("str", clean["context_profile_sha256"]),
            bindings=tuple(ModelBinding.from_mapping(binding) for binding in raw_bindings),
            sealing_mode=cast("str", clean["sealing_mode"]),
            claude_cli=(None if raw_cli is None else ClaudeRuntimeEvidence.from_mapping(raw_cli)),
            gemini=(None if raw_gemini is None else GeminiContextProfile.from_mapping(raw_gemini)),
        )
        supplied_hash = clean["receipt_sha256"]
        if not isinstance(supplied_hash, str) or supplied_hash != receipt.receipt_sha256:
            raise ExecutionProfileError(
                "execution receipt.receipt_sha256 does not match its canonical payload"
            )
        return receipt

    def payload_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "execution_profile_sha256": self.execution_profile_sha256,
            "provider_profile_sha256": self.provider_profile_sha256,
            "context_profile_sha256": self.context_profile_sha256,
            "bindings": [binding.to_mapping() for binding in self.bindings],
            "sealing_mode": self.sealing_mode,
            "claude_cli": None if self.claude_cli is None else self.claude_cli.to_mapping(),
        }
        # Omit the optional Gemini block when absent so Gemini-free receipts keep their exact
        # historical canonical payload and therefore their ``receipt_sha256`` (plan §6 D5).
        if self.gemini is not None:
            payload["gemini"] = self.gemini.to_mapping()
        return payload

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.payload_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {**self.payload_mapping(), "receipt_sha256": self.receipt_sha256}


DEFAULT_EXECUTION_PROFILE: Final[ModelSweepExecutionProfile] = ModelSweepExecutionProfile(
    schema_version=PROFILE_SCHEMA_VERSION,
    id="model-sweep-execution-v1",
    models=(
        ModelBinding("general-35b", PROVIDER_LOCAL, "general-35b"),
        ModelBinding("coder-30b", PROVIDER_LOCAL, "coder-30b"),
        ModelBinding("haiku", PROVIDER_CLAUDE, "haiku"),
        ModelBinding("sonnet", PROVIDER_CLAUDE, "sonnet"),
        ModelBinding("opus", PROVIDER_CLAUDE, "opus"),
        ModelBinding("fable", PROVIDER_CLAUDE, "fable"),
    ),
    claude=ClaudeContextProfile(
        executable=CLAUDE_EXECUTABLE,
        sealing_mode=CLAUDE_SEALING_MODE,
        argv_template=CLAUDE_ARGV_TEMPLATE,
        environment_allowlist=CLAUDE_ENV_ALLOWLIST,
    ),
)
