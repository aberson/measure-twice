"""Evidence-claim ledger for tier-routing decisions (plan.md section 3, Step 8).

The ledger is deliberately a small, strict JSONL store.  A claim records the decision it informs,
the exact source lines supporting it, and (when applicable) the benchmark run that measured it.
``audit_ledger`` re-reads only those cited lines and marks a claim ``STALE`` when a quote hash no
longer matches.  It never restores a stale claim automatically: a human must review the changed
source, update the citation/hash, and explicitly choose the new evidence status.

All malformed input is rejected rather than skipped.  This is measurement evidence; a partial or
silently repaired ledger would look like a genuine one while hiding its provenance failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, cast

# The safe identifier pattern belongs to switchboard.  It ships no ``py.typed`` marker, hence the
# scoped import ignore; keeping this import preserves one identifier contract across the workspace.
from switchboard.config import _SAFE_NAME_RE  # type: ignore[import-untyped]

__all__ = [
    "AuditIssue",
    "AuditResult",
    "Claim",
    "ClaimSource",
    "ClaimStatus",
    "LedgerError",
    "audit_ledger",
    "load_ledger",
    "render_claim_list",
    "render_ledger",
    "source_quote_sha256",
    "write_ledger",
]


class LedgerError(ValueError):
    """Raised when a ledger row, source citation, or ledger file is not trustworthy.

    The ledger is the provenance record behind routing decisions.  A malformed row, an unreadable
    source, or an unsafe source path must therefore abort the operation instead of being omitted.
    """


class ClaimStatus(StrEnum):
    """The four honest evidence states defined by the evidence-ledger contract."""

    MEASURED = "MEASURED"
    PARTIAL = "PARTIAL"
    ASSERTED = "ASSERTED"
    STALE = "STALE"


_STATUS_ORDER: Final[tuple[ClaimStatus, ...]] = (
    ClaimStatus.MEASURED,
    ClaimStatus.PARTIAL,
    ClaimStatus.ASSERTED,
    ClaimStatus.STALE,
)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
_LINE_RANGE_RE: Final[re.Pattern[str]] = re.compile(r"\A([1-9][0-9]*)(?:-([1-9][0-9]*))?\Z")


def _require_exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    """Reject both omitted and unrecognized JSON fields with a single useful error."""
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise LedgerError(f"{label} has " + "; ".join(details))


def _require_nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{label} must be a non-empty string")
    return value


def _parse_line_range(value: str) -> tuple[int, int]:
    """Parse a one-based inclusive ``N`` or ``N-M`` source span."""
    match = _LINE_RANGE_RE.fullmatch(value)
    if match is None:
        raise LedgerError(f"source lines {value!r} must be a one-based line or range like '30-33'")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if end < start:
        raise LedgerError(f"source lines {value!r} end before start")
    return start, end


def _validate_source_file(value: str) -> None:
    """Require a portable workspace-relative source path before auditing it.

    Ledger citations use POSIX separators to stay stable across Windows/Unix checkouts.  The
    resolver later verifies the real path as well, including symlink containment, so an audit can
    never read a file outside the named workspace.
    """
    if "\\" in value:
        raise LedgerError("source file must use '/' separators, not '\\'")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in value
    ):
        raise LedgerError(f"source file {value!r} must be a safe workspace-relative path")


def _validate_timestamp(value: str) -> None:
    """Require an explicit UTC timestamp; a naive date has ambiguous verification provenance."""
    if not value.endswith("Z"):
        raise LedgerError("last_verified_utc must be an ISO-8601 UTC timestamp ending in 'Z'")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LedgerError(f"last_verified_utc {value!r} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise LedgerError("last_verified_utc must include a UTC offset")


@dataclass(frozen=True, slots=True)
class ClaimSource:
    """A quote-hashed, workspace-relative file citation for one claim."""

    file: str
    lines: str
    quote_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], *, label: str) -> ClaimSource:
        _require_exact_keys(value, frozenset({"file", "lines", "quote_sha256"}), label)
        file = _require_nonempty_string(value["file"], f"{label}.file")
        lines = _require_nonempty_string(value["lines"], f"{label}.lines")
        quote_sha256 = _require_nonempty_string(value["quote_sha256"], f"{label}.quote_sha256")
        _validate_source_file(file)
        _parse_line_range(lines)
        if not _SHA256_RE.fullmatch(quote_sha256):
            raise LedgerError(f"{label}.quote_sha256 must be a lowercase SHA-256 hex digest")
        return cls(file=file, lines=lines, quote_sha256=quote_sha256)

    def to_mapping(self) -> dict[str, str]:
        return {
            "file": self.file,
            "lines": self.lines,
            "quote_sha256": self.quote_sha256,
        }


@dataclass(frozen=True, slots=True)
class Claim:
    """One tier-routing or model-choice assertion, measurement, or stale citation."""

    claim_id: str
    statement: str
    decision_surface: str
    sources: tuple[ClaimSource, ...]
    status: ClaimStatus
    evidence: tuple[str, ...]
    verdict: str | None
    preregistration: str | None
    last_verified_utc: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], *, label: str) -> Claim:
        expected = frozenset(
            {
                "claim_id",
                "statement",
                "decision_surface",
                "sources",
                "status",
                "evidence",
                "verdict",
                "preregistration",
                "last_verified_utc",
            }
        )
        _require_exact_keys(value, expected, label)
        claim_id = _require_nonempty_string(value["claim_id"], f"{label}.claim_id")
        if not _SAFE_NAME_RE.fullmatch(claim_id):
            raise LedgerError(
                f"{label}.claim_id {claim_id!r} contains unsafe characters "
                "(allowed: letters, digits, '.', '_', '-')"
            )
        statement = _require_nonempty_string(value["statement"], f"{label}.statement")
        decision_surface = _require_nonempty_string(
            value["decision_surface"], f"{label}.decision_surface"
        )

        raw_sources = value["sources"]
        if not isinstance(raw_sources, list) or not raw_sources:
            raise LedgerError(f"{label}.sources must be a non-empty list")
        sources: list[ClaimSource] = []
        for index, raw_source in enumerate(raw_sources, start=1):
            if not isinstance(raw_source, dict):
                raise LedgerError(f"{label}.sources[{index}] must be a JSON object")
            sources.append(
                ClaimSource.from_mapping(
                    cast("Mapping[str, object]", raw_source), label=f"{label}.sources[{index}]"
                )
            )

        raw_status = _require_nonempty_string(value["status"], f"{label}.status")
        try:
            status = ClaimStatus(raw_status)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in _STATUS_ORDER)
            raise LedgerError(
                f"{label}.status {raw_status!r} is invalid; allowed: {allowed}"
            ) from exc

        raw_evidence = value["evidence"]
        if not isinstance(raw_evidence, list):
            raise LedgerError(f"{label}.evidence must be a list of run ids")
        evidence: list[str] = []
        for index, run_id in enumerate(raw_evidence, start=1):
            parsed_run_id = _require_nonempty_string(run_id, f"{label}.evidence[{index}]")
            if parsed_run_id in evidence:
                raise LedgerError(f"{label}.evidence contains duplicate run id {parsed_run_id!r}")
            evidence.append(parsed_run_id)

        verdict_value = value["verdict"]
        if verdict_value is not None and not isinstance(verdict_value, str):
            raise LedgerError(f"{label}.verdict must be a string or null")
        preregistration_value = value["preregistration"]
        if preregistration_value is not None and not isinstance(preregistration_value, str):
            raise LedgerError(f"{label}.preregistration must be a string or null")
        preregistration = preregistration_value
        if preregistration is not None and not preregistration.strip():
            raise LedgerError(f"{label}.preregistration must not be blank when present")

        last_verified_utc = _require_nonempty_string(
            value["last_verified_utc"], f"{label}.last_verified_utc"
        )
        _validate_timestamp(last_verified_utc)

        if status is ClaimStatus.MEASURED:
            if not evidence:
                raise LedgerError(f"{label} is MEASURED but has no evidence run id")
            if preregistration is None:
                raise LedgerError(f"{label} is MEASURED but has no preregistration sentence")

        return cls(
            claim_id=claim_id,
            statement=statement,
            decision_surface=decision_surface,
            sources=tuple(sources),
            status=status,
            evidence=tuple(evidence),
            verdict=verdict_value,
            preregistration=preregistration,
            last_verified_utc=last_verified_utc,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the canonical JSONL object shape in stable field order."""
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "decision_surface": self.decision_surface,
            "sources": [source.to_mapping() for source in self.sources],
            "status": self.status.value,
            "evidence": list(self.evidence),
            "verdict": self.verdict,
            "preregistration": self.preregistration,
            "last_verified_utc": self.last_verified_utc,
        }


@dataclass(frozen=True, slots=True)
class AuditIssue:
    """One unreadable or changed source observed while auditing a claim."""

    claim_id: str
    source: ClaimSource
    reason: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    """The persisted audit result, including visible reasons for every stale citation."""

    claims: tuple[Claim, ...]
    newly_stale: tuple[str, ...]
    issues: tuple[AuditIssue, ...]

    @property
    def stale_claim_ids(self) -> tuple[str, ...]:
        return tuple(claim.claim_id for claim in self.claims if claim.status is ClaimStatus.STALE)

    @property
    def fresh_count(self) -> int:
        return len(self.claims) - len(self.stale_claim_ids)


def _default_workspace_root() -> Path:
    """Return the shared ``dev`` workspace root from this installed source tree."""
    return Path(__file__).resolve().parents[2]


def _resolve_workspace_root(workspace_root: Path | None) -> Path:
    candidate = _default_workspace_root() if workspace_root is None else Path(workspace_root)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LedgerError(f"workspace root cannot be resolved: {candidate}") from exc
    if not resolved.is_dir():
        raise LedgerError(f"workspace root is not a directory: {resolved}")
    return resolved


def _resolve_source_path(source: ClaimSource, workspace_root: Path) -> Path:
    """Resolve one syntactically-valid citation under the workspace, rejecting symlink escapes."""
    _validate_source_file(source.file)
    candidate = workspace_root.joinpath(*PurePosixPath(source.file).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LedgerError(f"source {source.file!r} cannot be resolved: {exc}") from exc
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise LedgerError(f"source {source.file!r} resolves outside workspace root") from exc
    if not resolved.is_file():
        raise LedgerError(f"source {source.file!r} is not a file")
    return resolved


def source_quote_sha256(source: ClaimSource, workspace_root: Path | None = None) -> str:
    """Hash the exact cited source lines using UTF-8 and normalized line joining.

    The hash intentionally covers only the cited span.  An unrelated edit outside that span does
    not invalidate a claim; a changed, deleted, unreadable, or escaping cited source is surfaced by
    :func:`audit_ledger` as stale evidence.
    """
    root = _resolve_workspace_root(workspace_root)
    path = _resolve_source_path(source, root)
    start, end = _parse_line_range(source.lines)
    try:
        all_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"source {source.file!r} cannot be read as UTF-8: {exc}") from exc
    if end > len(all_lines):
        raise LedgerError(
            f"source {source.file!r} has {len(all_lines)} line(s), cannot cite {source.lines!r}"
        )
    quote = "\n".join(all_lines[start - 1 : end])
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


def _claim_from_mapping(value: object, *, label: str) -> Claim:
    if not isinstance(value, dict):
        raise LedgerError(f"{label} must be a JSON object")
    return Claim.from_mapping(cast("Mapping[str, object]", value), label=label)


def load_ledger(path: Path) -> tuple[Claim, ...]:
    """Load a strict JSONL ledger; reject blank, malformed, and duplicate claim rows."""
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"could not read ledger {path}: {exc}") from exc

    # A tracked, not-yet-populated JSONL file may contain its conventional trailing newline.  Treat
    # a wholly-whitespace file as the empty ledger, while still rejecting blank rows mixed into real
    # claim data below.
    if not contents.strip():
        return ()

    claims: list[Claim] = []
    claim_ids: set[str] = set()
    for number, line in enumerate(contents.splitlines(), start=1):
        if not line.strip():
            raise LedgerError(f"ledger {path} line {number} is blank")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"ledger {path} line {number} is not valid JSON: {exc}") from exc
        claim = _claim_from_mapping(raw, label=f"ledger {path} line {number}")
        if claim.claim_id in claim_ids:
            raise LedgerError(f"ledger {path} contains duplicate claim_id {claim.claim_id!r}")
        claim_ids.add(claim.claim_id)
        claims.append(claim)
    return tuple(claims)


def _validated_claims(claims: Sequence[Claim]) -> tuple[Claim, ...]:
    """Round-trip supplied DTOs through the strict schema before persisting them."""
    validated: list[Claim] = []
    claim_ids: set[str] = set()
    for number, claim in enumerate(claims, start=1):
        try:
            raw = claim.to_mapping()
        except (AttributeError, TypeError) as exc:
            raise LedgerError(f"claim {number} is not a valid Claim DTO") from exc
        checked = Claim.from_mapping(raw, label=f"claim {number}")
        if checked.claim_id in claim_ids:
            raise LedgerError(f"ledger contains duplicate claim_id {checked.claim_id!r}")
        claim_ids.add(checked.claim_id)
        validated.append(checked)
    return tuple(validated)


def write_ledger(path: Path, claims: Sequence[Claim]) -> None:
    """Atomically persist a fully-valid ledger in deterministic JSONL field order."""
    checked_claims = _validated_claims(claims)
    rendered = "".join(
        json.dumps(claim.to_mapping(), ensure_ascii=True, separators=(",", ":"), sort_keys=False)
        + "\n"
        for claim in checked_claims
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(rendered, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        raise LedgerError(f"could not write ledger {path}: {exc}") from exc


def audit_ledger(path: Path, workspace_root: Path | None = None) -> AuditResult:
    """Audit each quote hash and persist newly-stale claims without silently repairing them."""
    claims = load_ledger(path)
    root = _resolve_workspace_root(workspace_root)
    audited: list[Claim] = []
    newly_stale: list[str] = []
    issues: list[AuditIssue] = []

    for claim in claims:
        claim_is_stale = False
        for source in claim.sources:
            try:
                actual_hash = source_quote_sha256(source, root)
            except LedgerError as exc:
                claim_is_stale = True
                issues.append(AuditIssue(claim.claim_id, source, str(exc)))
                continue
            if actual_hash != source.quote_sha256:
                claim_is_stale = True
                issues.append(
                    AuditIssue(
                        claim.claim_id,
                        source,
                        "quoted source text changed (SHA-256 no longer matches)",
                    )
                )

        if claim_is_stale and claim.status is not ClaimStatus.STALE:
            audited.append(replace(claim, status=ClaimStatus.STALE))
            newly_stale.append(claim.claim_id)
        else:
            audited.append(claim)

    result = AuditResult(tuple(audited), tuple(newly_stale), tuple(issues))
    if result.newly_stale:
        write_ledger(path, result.claims)
    return result


def _markdown_cell(value: str) -> str:
    """Keep user-authored ledger text inside one deterministic Markdown table cell."""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_claim_list(claims: Sequence[Claim]) -> str:
    """Render a compact, stable operator list without hiding current evidence state."""
    if not claims:
        return "No claims recorded."
    ordered = sorted(claims, key=lambda claim: claim.claim_id)
    return "\n".join(
        f"{claim.status.value}\t{claim.claim_id}\t{claim.statement}" for claim in ordered
    )


def render_ledger(claims: Sequence[Claim]) -> str:
    """Render deterministic Markdown grouped by the four evidence statuses."""
    by_status = {
        status: sorted(
            (claim for claim in claims if claim.status is status), key=lambda claim: claim.claim_id
        )
        for status in _STATUS_ORDER
    }
    lines = ["# Evidence ledger", ""]
    for status in _STATUS_ORDER:
        group = by_status[status]
        lines.extend(
            [
                f"## {status.value} ({len(group)})",
                "",
            ]
        )
        if not group:
            lines.extend(["No claims.", ""])
            continue
        lines.extend(
            [
                "| Claim | Decision surface | Statement | Evidence | Sources |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for claim in group:
            evidence = ", ".join(claim.evidence) if claim.evidence else "-"
            sources = "<br>".join(f"{source.file}:{source.lines}" for source in claim.sources)
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{_markdown_cell(claim.claim_id)}`",
                        f"`{_markdown_cell(claim.decision_surface)}`",
                        _markdown_cell(claim.statement),
                        _markdown_cell(evidence),
                        f"`{_markdown_cell(sources)}`",
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
