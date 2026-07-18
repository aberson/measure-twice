"""measure-twice suite schema, fail-loud loader, and canonical item hash.

A *suite* is the benchmark instrument: a named, versioned set of items, each carrying ALL of
its own content (prompt + expected answer). There are no prompt templates in adapters, so the
switchboard fallback-prompt bug class (``measurement-validity.md`` § assemble through the
production path) is structurally absent — a suite is the single source of an item's text.

Fail-loud contract (``measurement-validity.md`` § fail loud on fallback config):
  * The loader **validates on read and aborts on ANY schema violation** — it NEVER lenient-skips
    a bad item. A silently-dropped item is a silently-shrunk instrument, and its numbers would be
    indistinguishable from a real run. Every distinct violation raises ``SuiteError`` with a
    message naming the specific problem (invalid JSON, unknown key, missing required field, bad
    name, duplicate ids, unknown scoring type, out-of-range prior, wrong value type) so a caller —
    or a test — can tell them apart.
  * The dataclasses are themselves the validation boundary (value checks in ``__post_init__``), so
    a hand-constructed ``Item``/``Suite`` is as strict as a loaded one — the loader only adds the
    JSON/structural checks (unknown keys, missing keys, wrong container types) on top.

Canonical item hash (plan §3): ``suite_hash(items)`` is the sha256 hex of the CANONICAL JSON of
the items — ``sort_keys=True``, ``separators=(",", ":")`` (no incidental whitespace), and
``ensure_ascii=True`` (every non-ASCII code point emitted as a ``\\uXXXX`` escape, so the
canonical string is pure ASCII and the digest can never trip over an unencodable lone
surrogate). It is recorded
in every run manifest; a changed hash means a changed instrument, so cross-run comparisons require
equal hashes. Only item content feeds the hash — suite-level metadata (name, version, description)
does not — so re-titling a suite never invalidates a comparison, but editing any item does.

Shape constant ``_SAFE_NAME_RE`` (``^[A-Za-z0-9._\\-]+$``) is IMPORTED from switchboard, never
re-declared (``code-quality.md`` § one source of truth), so a suite name or item id can be used as
a switchboard ``enabled_call_sites`` key without translation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import MISSING, asdict, dataclass, fields
from pathlib import Path
from typing import Any, cast

# One source of truth — imported from switchboard, not re-declared (code-quality.md):
#   _SAFE_NAME_RE : ^[A-Za-z0-9._\-]+$   (identifier-safe suite/item names; plan §3).
# switchboard ships no py.typed marker, so mypy sees it untyped; the scoped ignore keeps
# --strict green without editing the sibling package or the mypy config (mirrors config.py).
from switchboard.config import _SAFE_NAME_RE  # type: ignore[import-untyped]

# v1 scoring types (plan §4). ``verdict``/``exact`` are deterministic; ``rubric`` is the LLM judge.
ALLOWED_SCORING_TYPES: frozenset[str] = frozenset({"verdict", "exact", "rubric"})

# The exact-scoring regex-mode compile contract, owned HERE (the low layer) so the load-time
# validator (``Suite.__post_init__``) and the score-time matcher
# (``scoring.deterministic.exact_match``) compile a pattern the IDENTICAL way — same ``.strip()``
# normalization, same flags — with no cross-layer drift. ``scoring`` imports this; ``suite`` never
# imports ``scoring`` (one-way dependency, no cycle). Flags: case-insensitive + ``.`` spans
# newlines, matching the literal mode's trim+casefold spirit.
_EXACT_REGEX_FLAGS = re.IGNORECASE | re.DOTALL


def compile_exact_pattern(expected: str) -> re.Pattern[str]:
    """Compile an ``exact`` regex ``expected`` (``.strip()``-ed, with :data:`_EXACT_REGEX_FLAGS`).

    Raises ``re.error`` on an uncompilable pattern; callers map that to their own fail-loud sentinel
    (``SuiteError`` at load, ``ScoringError`` at score). One source of truth for the compile so a
    pattern that validates at load is byte-for-byte the pattern that matches at score time.
    """
    return re.compile(expected.strip(), _EXACT_REGEX_FLAGS)


class SuiteError(ValueError):
    """Raised when a suite fails schema validation or a named suite file cannot be read.

    Fail-loud sentinel (mirrors ``config.ConfigError``): a malformed suite, a bad name, a
    duplicate id, or a provided-but-absent path surfaces as this rather than a lenient skip or a
    silent default. Distinct violations carry distinct messages so callers can distinguish them.
    """


# --- Scalar validators (the dataclasses are the fail-loud boundary, not just the loader) ---


def _reject_lone_surrogate(value: str, label: str) -> None:
    """Reject a string that cannot UTF-8 round-trip (a lone UTF-16 surrogate, e.g. ``"\\ud800"``).

    A suite is serialized to canonical JSON and UTF-8 encoded for its item hash and printed to a
    terminal; a lone surrogate makes both explode with a raw ``UnicodeEncodeError``. Per fail-loud
    doctrine a suite that can't encode is *malformed* — reject it on read with a distinct message
    rather than declaring it valid and then crashing downstream.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SuiteError(
            f"{label} contains a lone surrogate and is not UTF-8 encodable, got {value!r}"
        ) from exc


def _validate_safe_name(value: object, label: str) -> None:
    """A non-empty, identifier-safe string per switchboard's ``_SAFE_NAME_RE`` (plan §3).

    Beyond the regex, reject all-punctuation names with no alphanumeric char — in particular the
    traversal tokens ``"."`` / ``".."`` — since suite names and item ids become filesystem path
    components (run dirs) and switchboard ``enabled_call_sites`` keys (defense-in-depth on top of
    the imported ``_SAFE_NAME_RE``, which admits dot-only strings).
    """
    if not isinstance(value, str) or not value:
        raise SuiteError(f"{label} must be a non-empty string, got {value!r}")
    if not _SAFE_NAME_RE.match(value):
        raise SuiteError(
            f"{label} {value!r} contains unsafe characters "
            f"(allowed: letters, digits, '.', '_', '-')"
        )
    if not any(ch.isalnum() for ch in value):
        raise SuiteError(
            f"{label} {value!r} must contain at least one alphanumeric character "
            f"(dot/dash-only names like '.' or '..' are path-traversal tokens)"
        )


def _validate_nonempty_str(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise SuiteError(f"{label} must be a non-empty string, got {value!r}")
    _reject_lone_surrogate(value, label)


def _validate_str_list(value: object, label: str) -> None:
    """A list of non-empty, UTF-8-encodable strings (the list itself may be empty)."""
    if not isinstance(value, list):
        raise SuiteError(f"{label} must be a list of strings, got {type(value).__name__}")
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise SuiteError(f"{label}[{i}] must be a non-empty string, got {item!r}")
        _reject_lone_surrogate(item, f"{label}[{i}]")


def _validate_int_at_least(value: object, label: str, minimum: int) -> None:
    # bool is an int subclass; reject it so a stray ``true`` can't masquerade as a number.
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SuiteError(f"{label} must be an int >= {minimum}, got {value!r}")


def _validate_unit_float(value: object, label: str) -> None:
    """A real number within the closed unit interval [0.0, 1.0] (rejects bool and NaN)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SuiteError(f"{label} must be a number in [0.0, 1.0], got {value!r}")
    if not (0.0 <= value <= 1.0):  # NaN fails both comparisons -> rejected here.
        raise SuiteError(f"{label} must be within [0.0, 1.0], got {value!r}")


def _field_sets(cls: type) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(allowed, required)`` field-name sets for a dataclass.

    ``allowed`` is every field (the sole source of truth for "which JSON keys are permitted");
    ``required`` is the subset with no default / default_factory (missing them is a violation).
    """
    allowed = frozenset(f.name for f in fields(cls))
    required = frozenset(
        f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING
    )
    return allowed, required


def _reject_unknown_and_missing(
    data: object, allowed: frozenset[str], required: frozenset[str], noun: str
) -> Mapping[str, object]:
    """Fail-loud structural gate shared by every ``from_mapping``: enforce object-ness, then
    reject unknown keys and missing required keys with messages naming ``noun`` and the keys."""
    if not isinstance(data, Mapping):
        raise SuiteError(f"{noun} must be a JSON object, got {type(data).__name__}")
    keys = set(data)
    unknown = keys - allowed
    if unknown:
        raise SuiteError(f"unknown {noun} key(s): {sorted(unknown)}; allowed: {sorted(allowed)}")
    missing = required - keys
    if missing:
        raise SuiteError(f"missing required {noun} field(s): {sorted(missing)}")
    return cast("Mapping[str, object]", data)


@dataclass(frozen=True, slots=True)
class ScoringSpec:
    """How a suite's items are scored: a ``type`` from :data:`ALLOWED_SCORING_TYPES`, plus, for
    label-based scoring (``verdict``), the permitted ``labels``, and, for ``exact`` scoring, an
    optional ``regex`` flag selecting full-match regex mode instead of literal equality.

    ``labels`` must be casefold-DISTINCT: the deterministic verdict matcher is case-insensitive, so
    case-variant duplicates (``["Flag", "flag"]``) would make one occurrence match both patterns and
    silently score every correct answer a parse-fail — the exact "silent parse-fail drags the mean
    to zero" failure measurement-validity warns about, mass-produced. So it is rejected at load
    (fail loud), the same stance as the parse-fail-marker collision. ``regex`` is meaningful only
    for ``exact`` and is rejected on any other type (a modifier that would silently do nothing).
    """

    type: str
    labels: list[str] | None = None
    regex: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or self.type not in ALLOWED_SCORING_TYPES:
            raise SuiteError(
                f"scoring.type must be one of {sorted(ALLOWED_SCORING_TYPES)}, got {self.type!r}"
            )
        if not isinstance(self.regex, bool):
            raise SuiteError(f"scoring.regex must be a bool, got {self.regex!r}")
        if self.regex and self.type != "exact":
            raise SuiteError(
                f"scoring.regex is only valid for exact scoring, not type {self.type!r}"
            )
        if self.labels is not None:
            _validate_str_list(self.labels, "scoring.labels")
            seen: dict[str, str] = {}
            for label in self.labels:
                key = label.casefold()
                if key in seen:
                    raise SuiteError(
                        "scoring.labels contains case-variant duplicate label(s): "
                        f"{seen[key]!r} and {label!r} collide case-insensitively"
                    )
                seen[key] = label

    @classmethod
    def from_mapping(cls, data: object) -> ScoringSpec:
        allowed, required = _field_sets(cls)
        clean = _reject_unknown_and_missing(data, allowed, required, "scoring")
        return cls(**cast("dict[str, Any]", dict(clean)))


@dataclass(frozen=True, slots=True)
class Item:
    """One benchmark item: a self-contained prompt + the ``expected`` answer it scores against.

    ``id`` must match ``_SAFE_NAME_RE`` (usable as a switchboard call-site key);
    ``difficulty_prior`` is an authoring-time estimate in [0.0, 1.0]; ``provenance`` records where
    the item came from (``"authored"`` or ``"harvested: <file>"``). ``expected`` is REQUIRED — an
    item with no gold answer cannot be scored, so its absence is a hard schema violation, not a
    defaulted blank.
    """

    id: str
    tags: list[str]
    prompt: str
    expected: str
    difficulty_prior: float
    provenance: str

    def __post_init__(self) -> None:
        _validate_safe_name(self.id, "item id")
        _validate_str_list(self.tags, "item tags")
        _validate_nonempty_str(self.prompt, "item prompt")
        _validate_nonempty_str(self.expected, "item expected")
        _validate_unit_float(self.difficulty_prior, "item difficulty_prior")
        _validate_nonempty_str(self.provenance, "item provenance")
        # Normalize int -> float so the canonical item hash (the instrument identity) is stable:
        # difficulty_prior 1 and 1.0 are the same value and MUST hash identically (plan §3). bool
        # is already rejected above, so this coercion never widens a stray ``true``. frozen+slots:
        # object.__setattr__ is the sanctioned in-__post_init__ mutation path.
        object.__setattr__(self, "difficulty_prior", float(self.difficulty_prior))

    @classmethod
    def from_mapping(cls, data: object) -> Item:
        allowed, required = _field_sets(cls)
        clean = _reject_unknown_and_missing(data, allowed, required, "item")
        return cls(**cast("dict[str, Any]", dict(clean)))


@dataclass(frozen=True, slots=True)
class Suite:
    """A named, versioned benchmark instrument: metadata + a scoring spec + a non-empty item list.

    The item list must have unique ids (a duplicate id would make results ambiguous). ``item_hash``
    is the canonical content hash of ``items`` (see :func:`suite_hash`) recorded in run manifests.
    """

    suite: str
    version: int
    description: str
    domain: str
    scoring: ScoringSpec
    items: list[Item]

    def __post_init__(self) -> None:
        _validate_safe_name(self.suite, "suite name")
        _validate_int_at_least(self.version, "suite version", 1)
        _validate_nonempty_str(self.description, "suite description")
        _validate_nonempty_str(self.domain, "suite domain")
        if not isinstance(self.scoring, ScoringSpec):
            raise SuiteError(
                f"suite scoring must be a ScoringSpec, got {type(self.scoring).__name__}"
            )
        if not isinstance(self.items, list):
            raise SuiteError(f"suite items must be a list, got {type(self.items).__name__}")
        if not self.items:
            raise SuiteError("suite items must contain at least one item")
        seen: set[str] = set()
        dupes: set[str] = set()
        for item in self.items:
            if not isinstance(item, Item):
                raise SuiteError(f"suite items must all be Item, got {type(item).__name__}")
            if item.id in seen:
                dupes.add(item.id)
            seen.add(item.id)
        if dupes:
            raise SuiteError(f"duplicate item id(s): {sorted(dupes)}")
        # Fail loud at LOAD on a bad regex `expected` (scoring.regex=true): compile every item's
        # pattern here, exactly like verdict labels are validated at load, so an uncompilable
        # pattern aborts BEFORE any run — never a score-time exception. This is the ONLY input-
        # independent raise path the deterministic scorer had; validating it here removes it. Both
        # load_suite and the per-run suite-snapshot reload go through this constructor, so a corrupt
        # snapshot with a bad pattern also fails loud when `mt score` reopens it.
        if self.scoring.regex:
            for item in self.items:
                try:
                    compile_exact_pattern(item.expected)
                except re.error as exc:
                    raise SuiteError(
                        f"item {item.id!r} expected {item.expected!r} is not a valid regex "
                        f"(scoring.regex=true): {exc}"
                    ) from exc

    @property
    def item_hash(self) -> str:
        """The canonical sha256 content hash of this suite's items (see :func:`suite_hash`)."""
        return suite_hash(self.items)

    @classmethod
    def from_mapping(cls, data: object) -> Suite:
        allowed, required = _field_sets(cls)
        clean = _reject_unknown_and_missing(data, allowed, required, "suite")
        scoring = ScoringSpec.from_mapping(clean["scoring"])
        items_raw = clean["items"]
        if not isinstance(items_raw, list):
            raise SuiteError(f"suite items must be a list, got {type(items_raw).__name__}")
        items = [Item.from_mapping(entry) for entry in items_raw]
        return cls(
            suite=cast("Any", clean["suite"]),
            version=cast("Any", clean["version"]),
            description=cast("Any", clean["description"]),
            domain=cast("Any", clean["domain"]),
            scoring=scoring,
            items=items,
        )


def suite_hash(items: Sequence[Item]) -> str:
    """sha256 hex over the CANONICAL JSON of ``items`` (plan §3).

    Canonical form: ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`` over
    each item's field dict, utf-8 encoded. ``ensure_ascii=True`` emits every non-ASCII code point
    (including any surrogate) as a ``\\uXXXX`` ASCII escape, so ``.encode("utf-8")`` can never raise
    on the serialized text — surrogate-safe as well as deterministic (loader already rejects lone
    surrogates, so this is defense-in-depth). Deterministic across reloads of identical content and
    sensitive to any item-content change; only item content feeds the hash, never suite metadata.
    ``difficulty_prior`` is float-normalized at ``Item`` construction so ``1`` and ``1.0`` hash the
    same.
    """
    payload = [asdict(item) for item in items]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_suite(path: str | Path) -> Suite:
    """Read a suite JSON file, fully validate it, and return the ``Suite`` — else ``SuiteError``.

    Fail loud on every failure mode: a named-but-missing path, an OS read error, bad bytes, invalid
    JSON, a non-object top level, or any schema violation. The suite is never partially loaded and a
    bad item is never skipped — an instrument the loader could not honestly build must not run.
    """
    p = Path(path)
    if not p.is_file():
        raise SuiteError(f"suite file not found: {str(path)!r}")
    try:
        with p.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        # UnicodeDecodeError subclasses ValueError (not OSError); catch it explicitly so bad bytes
        # surface as SuiteError, not an unrelated escape (mirrors config.load_config).
        # RecursionError is raised by json.load on a nesting bomb (pathologically-nested input) —
        # fold it in too, so no read failure escapes as a raw traceback.
        raise SuiteError(f"could not read suite {str(path)!r}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SuiteError(f"suite {str(path)!r} must be a JSON object, got {type(raw).__name__}")

    return Suite.from_mapping(raw)


def validate(path: str | Path) -> Suite:
    """Validation entry the CLI calls: load + fully validate a suite, returning it on success.

    A thin, intention-revealing alias for :func:`load_suite` (loading IS validation here — the
    loader aborts on any violation), so ``mt validate`` and the loader share one code path.
    """
    return load_suite(path)
