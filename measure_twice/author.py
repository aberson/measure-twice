"""Candidate-item harvesting and schema-valid suite stubs for Phase C.

This module deliberately distinguishes *candidate generation* from benchmark evidence.  A
harvester may safely turn a source artifact into a schema-valid :class:`~measure_twice.suite.Item`,
but only sources that already contain a verified outcome supply ``pass``/``flag`` gold.  Historical
Git snippets have no such gold, so they are emitted with an explicit ``needs-gold`` tag and the
sentinel expected value ``CURATE``.  Step 12 must curate those candidates before they enter the
flagship verdict suite; this module never invents a verdict from a filename, commit message, or
plausible-looking diff.

Supported, workspace-relative sources are deliberately closed:

``goldens``
    Skill-eval ``.claude/skills/*/evals/golden`` corpora.  ``good.md`` is the positive anchor;
    a bad file is admitted only when the adjacent manifest records it as ``verified_fails``.
``review-deep``
    Recorded style and correctness lens verdict fixtures under ``.review-deep``.  The source
    verdict supplies the gold, but the verdict field itself is removed from the model prompt.
``git``
    Bounded Git-history patch excerpts.  These are honest unreviewed candidates, not labeled
    examples.

The module reads artifacts only.  It never imports, executes, or shells their contents; the sole
subprocess use is a fixed-argument ``git`` read command for the named history source.  Paths are
resolved below a caller-provided workspace root and symlink escapes are rejected before a file is
read.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from measure_twice.suite import Item, ScoringSpec, Suite, SuiteError


class AuthorError(ValueError):
    """Raised when an authoring source, target, or candidate is unsafe or malformed."""


_SOURCE_NAMES: frozenset[str] = frozenset({"all", "git", "goldens", "review-deep"})
_MAX_ARTIFACT_BYTES = 512 * 1024
_MAX_GIT_COMMITS = 20
_MAX_GIT_DIFF_BYTES = 64 * 1024
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PASSING_REVIEW_VERDICTS: frozenset[str] = frozenset({"PASS", "DEFERRED-TO-UAT"})
_FAILING_REVIEW_VERDICTS: frozenset[str] = frozenset({"FAILED", "NEEDS-WORK", "NO-EVIDENCE"})


@dataclass(frozen=True, slots=True)
class HarvestResult:
    """A deterministic candidate set plus transparent collection accounting."""

    source: str
    candidates: tuple[Item, ...]
    duplicates_dropped: int


def _workspace_root(path: str | Path) -> Path:
    """Resolve and validate the workspace root used for every source lookup."""
    root = Path(path)
    if not root.is_dir():
        raise AuthorError(f"workspace root is not a directory: {str(path)!r}")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise AuthorError(f"could not resolve workspace root {str(path)!r}: {exc}") from exc


def _inside_workspace(root: Path, path: Path, *, label: str) -> Path:
    """Return a resolved path only when it is contained by ``root`` and not a symlink escape."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AuthorError(f"could not resolve {label} {str(path)!r}: {exc}") from exc
    if not resolved.is_relative_to(root):
        raise AuthorError(f"{label} escapes workspace root: {str(path)!r}")
    return resolved


def _read_workspace_file(root: Path, path: Path) -> tuple[bytes, str]:
    """Read one regular, bounded, non-escaping UTF-8 source artifact."""
    if path.is_symlink():
        raise AuthorError(f"source artifact must not be a symlink: {str(path)!r}")
    resolved = _inside_workspace(root, path, label="source artifact")
    if not resolved.is_file():
        raise AuthorError(f"source artifact is not a regular file: {str(path)!r}")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise AuthorError(f"could not stat source artifact {str(path)!r}: {exc}") from exc
    if size > _MAX_ARTIFACT_BYTES:
        raise AuthorError(
            f"source artifact exceeds {_MAX_ARTIFACT_BYTES} byte limit: "
            f"{str(path)!r} ({size} bytes)"
        )
    try:
        raw = resolved.read_bytes()
        return raw, raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuthorError(f"could not read UTF-8 source artifact {str(path)!r}: {exc}") from exc


def _relative_source(root: Path, path: Path) -> str:
    """Render a checked source path as stable, workspace-relative POSIX text."""
    return _inside_workspace(root, path, label="source artifact").relative_to(root).as_posix()


def _source_provenance(root: Path, path: Path, raw: bytes) -> str:
    """Record the artifact location and immutable snapshot hash in an item provenance field."""
    digest = hashlib.sha256(raw).hexdigest()
    return f"harvested:{_relative_source(root, path)};sha256={digest}"


def _slug(value: str) -> str:
    """Turn externally-controlled file/skill names into a non-empty safe item-id component."""
    slug = _SAFE_SLUG_RE.sub("-", value).strip(".-_")
    return slug or "artifact"


def _item_id(prefix: str, source_label: str, raw: bytes) -> str:
    """Build a deterministic safe identifier; source content changes mint a new candidate ID."""
    return f"{prefix}-{_slug(source_label)}-{hashlib.sha256(raw).hexdigest()[:12]}"


def candidate_content_hash(item: Item) -> str:
    """Hash the scored content, not incidental candidate metadata.

    IDs, tags, and provenance differ for two references to the same prompt/answer pair.  They must
    not prevent deduplication, because treating those copies as independent benchmark items would
    overstate coverage.  Canonical JSON makes the hash stable across process/platform boundaries.
    """
    payload = {"expected": item.expected, "prompt": item.prompt}
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def deduplicate_candidates(items: Iterable[Item]) -> tuple[tuple[Item, ...], int]:
    """Keep the first candidate for each semantic prompt/answer pair, preserving input order."""
    seen: set[str] = set()
    unique: list[Item] = []
    duplicates = 0
    for item in items:
        digest = candidate_content_hash(item)
        if digest in seen:
            duplicates += 1
            continue
        seen.add(digest)
        unique.append(item)
    return tuple(unique), duplicates


def _manifest_bad_files(root: Path, manifest_path: Path) -> frozenset[str]:
    """Load verified negative filenames from one golden manifest, rejecting unsafe declarations."""
    raw, text = _read_workspace_file(root, manifest_path)
    del raw
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthorError(
            f"golden manifest is not valid JSON: {str(manifest_path)!r}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("bads"), list):
        raise AuthorError(f"golden manifest lacks a list of bads: {str(manifest_path)!r}")
    approved: set[str] = set()
    for index, entry in enumerate(payload["bads"]):
        if not isinstance(entry, dict):
            raise AuthorError(f"golden manifest bads[{index}] is not an object")
        filename = entry.get("file")
        verified = entry.get("verified_fails")
        if not isinstance(filename, str) or not filename:
            raise AuthorError(f"golden manifest bads[{index}].file must be a non-empty string")
        candidate = Path(filename)
        if candidate.is_absolute() or len(candidate.parts) != 1 or candidate.name != filename:
            raise AuthorError(
                f"golden manifest bads[{index}].file is not a safe basename: {filename!r}"
            )
        if not isinstance(verified, bool):
            raise AuthorError(f"golden manifest bads[{index}].verified_fails must be a bool")
        if verified:
            approved.add(filename)
    return frozenset(approved)


def harvest_goldens(workspace_root: str | Path) -> tuple[Item, ...]:
    """Harvest verified positive/negative skill-eval artifacts into candidate verdict items."""
    root = _workspace_root(workspace_root)
    skills_dir = root / ".claude" / "skills"
    if not skills_dir.is_dir():
        raise AuthorError(f"golden source directory not found: {str(skills_dir)!r}")
    _inside_workspace(root, skills_dir, label="golden source directory")

    candidates: list[Item] = []
    for manifest_path in sorted(skills_dir.glob("*/evals/golden/manifest.json")):
        golden_dir = manifest_path.parent
        if golden_dir.is_symlink():
            raise AuthorError(f"golden directory must not be a symlink: {str(golden_dir)!r}")
        _inside_workspace(root, golden_dir, label="golden source directory")
        approved_bad_files = _manifest_bad_files(root, manifest_path)
        skill = manifest_path.parents[2].name
        source_files: list[tuple[Path, str]] = []
        good_path = golden_dir / "good.md"
        if good_path.is_file():
            source_files.append((good_path, "pass"))
        for filename in sorted(approved_bad_files):
            source_files.append((golden_dir / filename, "flag"))
        for source_path, expected in source_files:
            raw, content = _read_workspace_file(root, source_path)
            prompt = (
                "Review the following skill-evaluation artifact. Return `pass` only if it meets "
                "the stated skill contract; otherwise return `flag`.\n\n"
                f"--- artifact ---\n{content}\n--- end artifact ---"
            )
            candidates.append(
                Item(
                    id=_item_id("golden", f"{skill}-{source_path.stem}", raw),
                    tags=["golden", "skill-eval", f"skill-{_slug(skill)}", expected],
                    prompt=prompt,
                    expected=expected,
                    difficulty_prior=0.5,
                    provenance=_source_provenance(root, source_path, raw),
                )
            )
    if not candidates:
        raise AuthorError("golden harvest found no verified good.md or verified_fails artifacts")
    return tuple(candidates)


def _review_expected(value: object, *, source: Path, lens: str) -> str | None:
    """Map an explicit review-deep verdict to gold, skipping deliberately non-evaluative lenses."""
    if not isinstance(value, str):
        raise AuthorError(
            f"review-deep fixture {str(source)!r} lens {lens!r} lacks overall_verdict"
        )
    if value in _PASSING_REVIEW_VERDICTS:
        return "pass"
    if value in _FAILING_REVIEW_VERDICTS:
        return "flag"
    if value == "SKIPPED":
        return None
    raise AuthorError(
        f"review-deep fixture {str(source)!r} lens {lens!r} has unsupported verdict {value!r}"
    )


def harvest_review_deep(workspace_root: str | Path) -> tuple[Item, ...]:
    """Harvest recorded style/correctness evidence without leaking its gold labels to a prompt."""
    root = _workspace_root(workspace_root)
    fixture_dir = root / ".review-deep"
    if not fixture_dir.is_dir():
        raise AuthorError(f"review-deep fixture directory not found: {str(fixture_dir)!r}")
    _inside_workspace(root, fixture_dir, label="review-deep fixture directory")

    candidates: list[Item] = []
    for source_path in sorted(fixture_dir.glob("*.json")):
        raw, text = _read_workspace_file(root, source_path)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuthorError(
                f"review-deep fixture is not valid JSON: {str(source_path)!r}: {exc}"
            ) from exc
        lenses = payload.get("lens_verdicts") if isinstance(payload, dict) else None
        if not isinstance(lenses, list):
            raise AuthorError(
                f"review-deep fixture lacks a lens_verdicts list: {str(source_path)!r}"
            )
        for index, lens_payload in enumerate(lenses):
            if not isinstance(lens_payload, dict):
                raise AuthorError(f"review-deep fixture lens_verdicts[{index}] is not an object")
            lens = lens_payload.get("lens_id")
            if lens not in {"correctness", "style"}:
                continue
            expected = _review_expected(
                lens_payload.get("overall_verdict"), source=source_path, lens=lens
            )
            if expected is None:
                continue
            evidence = {
                key: value
                for key, value in lens_payload.items()
                if key in {"authority", "coverage_claim", "findings", "lens_id"}
            }
            prompt = (
                "Evaluate the following review evidence. Return `pass` if the evidence supports "
                "accepting the change, otherwise return `flag`.\n\n"
                f"--- {lens} evidence ---\n"
                f"{json.dumps(evidence, ensure_ascii=True, indent=2, sort_keys=True)}\n"
                "--- end evidence ---"
            )
            candidates.append(
                Item(
                    id=_item_id("review", f"{source_path.stem}-{lens}", raw + lens.encode()),
                    tags=["review-deep", "review", lens, expected],
                    prompt=prompt,
                    expected=expected,
                    difficulty_prior=0.5,
                    provenance=_source_provenance(root, source_path, raw),
                )
            )
    if not candidates:
        raise AuthorError("review-deep harvest found no scored style or correctness lens evidence")
    return tuple(candidates)


def _git(root: Path, args: Sequence[str]) -> str:
    """Run a fixed-argument, read-only Git command rooted at the checked workspace directory."""
    executable = shutil.which("git")
    if executable is None:
        raise AuthorError("could not read Git history: `git` is not available on PATH")
    try:
        completed = subprocess.run(  # noqa: S603 - the caller supplies fixed, read-only Git args.
            [executable, "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthorError(f"could not read Git history: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Git returned no diagnostic"
        raise AuthorError(f"could not read Git history: {detail}")
    return completed.stdout


def harvest_git_history(
    workspace_root: str | Path, *, limit: int = _MAX_GIT_COMMITS
) -> tuple[Item, ...]:
    """Harvest bounded, explicitly-unlabeled historical patch snippets for later human curation."""
    root = _workspace_root(workspace_root)
    if limit < 1 or limit > _MAX_GIT_COMMITS:
        raise AuthorError(f"Git history limit must be in [1, {_MAX_GIT_COMMITS}], got {limit}")
    commit_lines = _git(root, ["log", f"--max-count={limit}", "--format=%H"])
    commits = [line for line in commit_lines.splitlines() if line]
    if not commits:
        raise AuthorError("Git history contains no commits")
    candidates: list[Item] = []
    for commit in commits:
        if not _GIT_SHA_RE.fullmatch(commit):
            raise AuthorError(f"Git history returned an invalid commit ID: {commit!r}")
        patch = _git(
            root, ["show", "--format=", "--no-ext-diff", "--unified=0", "--no-renames", commit]
        )
        raw = patch.encode("utf-8")
        if not patch.strip() or len(raw) > _MAX_GIT_DIFF_BYTES or "GIT binary patch" in patch:
            continue
        prompt = (
            "This historical code change is an unreviewed benchmark candidate. A human must curate "
            "its gold verdict before it can enter a scored suite.\n\n"
            f"commit: {commit}\n--- patch ---\n{patch}\n--- end patch ---"
        )
        candidates.append(
            Item(
                id=_item_id("git", commit[:12], raw),
                tags=["git-history", "needs-gold", "candidate-only"],
                prompt=prompt,
                expected="CURATE",
                difficulty_prior=0.5,
                provenance=f"harvested:git:{commit};sha256={hashlib.sha256(raw).hexdigest()}",
            )
        )
    if not candidates:
        raise AuthorError("Git history contained no bounded text patch candidates")
    return tuple(candidates)


def harvest(source: str, workspace_root: str | Path) -> HarvestResult:
    """Harvest one named source (or all sources), then remove semantic duplicate candidates."""
    if source not in _SOURCE_NAMES:
        raise AuthorError(
            f"unknown authoring source {source!r}; choose one of {sorted(_SOURCE_NAMES)}"
        )
    selected = ("goldens", "review-deep", "git") if source == "all" else (source,)
    candidates: list[Item] = []
    for name in selected:
        if name == "goldens":
            candidates.extend(harvest_goldens(workspace_root))
        elif name == "review-deep":
            candidates.extend(harvest_review_deep(workspace_root))
        else:
            candidates.extend(harvest_git_history(workspace_root))
    unique, duplicates = deduplicate_candidates(candidates)
    return HarvestResult(source=source, candidates=unique, duplicates_dropped=duplicates)


def render_harvest(result: HarvestResult) -> str:
    """Render harvested candidates as deterministic JSON suitable for human curation or a file."""
    payload = {
        "candidates": [asdict(candidate) for candidate in result.candidates],
        "duplicates_dropped": result.duplicates_dropped,
        "source": result.source,
    }
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def build_stub_suite(suite_name: str) -> Suite:
    """Build one schema-valid, clearly non-production suite template for a human to edit."""
    return Suite(
        suite=suite_name,
        version=1,
        description=(
            "AUTHORING TEMPLATE ONLY. Replace the candidate with a source-reviewed item before "
            "using this suite for any benchmark run or evidence claim."
        ),
        domain="unassigned",
        scoring=ScoringSpec(type="verdict", labels=["pass", "flag"]),
        items=[
            Item(
                id=f"{suite_name}-template",
                tags=["template", "needs-curation"],
                prompt=(
                    "Replace this template with a self-contained, source-reviewed benchmark item. "
                    "For this placeholder only, respond with `pass`."
                ),
                expected="pass",
                difficulty_prior=0.5,
                provenance="authored:template-needs-curation",
            )
        ],
    )


def render_stub_suite(suite: Suite) -> str:
    """Render a template suite using the same keys the strict suite loader accepts."""
    payload = {
        "description": suite.description,
        "domain": suite.domain,
        "items": [asdict(item) for item in suite.items],
        "scoring": asdict(suite.scoring),
        "suite": suite.suite,
        "version": suite.version,
    }
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def write_new_file(path: str | Path, content: str) -> Path:
    """Write a new UTF-8 output file, refusing to overwrite an existing authoring artifact."""
    output = Path(path)
    if output.exists() or output.is_symlink():
        raise AuthorError(f"refusing to overwrite existing authoring output: {str(path)!r}")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise AuthorError(f"could not write authoring output {str(path)!r}: {exc}") from exc
    return output


def make_stub_file(suite_name: str, output: str | Path) -> Path:
    """Validate, render, and write a fresh authoring stub suite."""
    try:
        suite = build_stub_suite(suite_name)
    except SuiteError as exc:
        raise AuthorError(f"invalid stub suite name {suite_name!r}: {exc}") from exc
    return write_new_file(output, render_stub_suite(suite))
