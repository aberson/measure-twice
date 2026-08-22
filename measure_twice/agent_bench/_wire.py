"""Neutral strict-wire primitives shared by coding-agent input domains.

Each domain binds :class:`WireCodec` to its own public error type.  This keeps JSON, schema,
identifier, integer, and canonical-hash behavior identical without making model-registry errors
the accidental base API for analysis plans or suite bundles.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

SCHEMA_VERSION = 1
SAFE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
SAFE_ID_RE = re.compile(SAFE_ID_PATTERN)

_JSONValue = object


class AgentInputError(ValueError):
    """Base error for malformed coding-agent benchmark input."""


class _DuplicateJsonKey(ValueError):
    """Internal sentinel raised by the strict JSON object hook."""


def _pairs_object(pairs: list[tuple[str, _JSONValue]]) -> dict[str, _JSONValue]:
    result: dict[str, _JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value!r} is not permitted")


class WireCodec:
    """Strict JSON and canonicalization helpers bound to one domain error class."""

    def __init__(self, error_type: type[AgentInputError]) -> None:
        if not issubclass(error_type, AgentInputError):
            raise TypeError("wire error type must derive from AgentInputError")
        self._error_type = error_type

    def load_json_object(self, path: str | Path, *, label: str) -> dict[str, object]:
        source = Path(path)
        if not source.is_file():
            raise self._error_type(f"{label} file not found: {str(path)!r}")
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise self._error_type(f"could not read {label} {str(path)!r}: {exc}") from exc
        return self.decode_json_object(raw, source=str(path), label=label)

    def decode_json_object(
        self, raw: bytes, *, source: str | Path, label: str
    ) -> dict[str, object]:
        """Decode strict UTF-8 JSON bytes already acquired through a trusted read boundary."""

        try:
            text = raw.decode("utf-8")
            value = json.loads(
                text,
                object_pairs_hook=_pairs_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise self._error_type(f"could not read {label} {str(source)!r}: {exc}") from exc
        if not isinstance(value, dict):
            raise self._error_type(f"{label} must be a JSON object, got {type(value).__name__}")
        return cast("dict[str, object]", value)

    def require_exact_keys(
        self, value: object, expected: frozenset[str], *, label: str
    ) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise self._error_type(f"{label} must be a JSON object, got {type(value).__name__}")
        actual = set(value)
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing or unknown:
            parts: list[str] = []
            if missing:
                parts.append(f"missing {missing}")
            if unknown:
                parts.append(f"unknown {unknown}")
            raise self._error_type(f"{label} has invalid keys: {', '.join(parts)}")
        return cast("Mapping[str, object]", value)

    def validate_schema_version(self, value: object, *, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise self._error_type(f"{label}.schema_version must be integer 1, got {value!r}")
        if value != SCHEMA_VERSION:
            raise self._error_type(
                f"unsupported {label}.schema_version {value!r}; "
                f"supported version is {SCHEMA_VERSION}"
            )
        return value

    def validate_safe_id(self, value: object, *, label: str) -> str:
        if not isinstance(value, str) or SAFE_ID_RE.fullmatch(value) is None:
            raise self._error_type(
                f"{label} must match {SAFE_ID_PATTERN!r} "
                f"(1-64 lowercase safe characters), got {value!r}"
            )
        if value in {".", ".."}:
            raise self._error_type(f"{label} may not be {value!r}")
        return value

    def validate_nonempty_string(self, value: object, *, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self._error_type(f"{label} must be a non-empty string, got {value!r}")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise self._error_type(f"{label} must be UTF-8 encodable") from exc
        return value

    def require_positive_int(self, value: object, *, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise self._error_type(f"{label} must be a positive integer, got {value!r}")
        return value

    def canonical_json_bytes(self, value: object) -> bytes:
        try:
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return rendered.encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise self._error_type(f"value cannot be encoded as canonical JSON: {exc}") from exc

    def canonical_sha256(self, value: object) -> str:
        return hashlib.sha256(self.canonical_json_bytes(value)).hexdigest()


__all__ = ["AgentInputError", "WireCodec"]
