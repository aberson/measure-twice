"""Deterministic scorers + the judge-core §5.6-conformant verdict parse spine (plan §4, §5).

Two zero-model-call scorers back the flagship dataset's tier-ordering claims (plan §4 Decision 7):

  * **verdict** — parse the model's verdict label from a raw response and compare it to the item's
    gold ``expected``. The parse conforms to the judge-core §5.6 spine (referenced, not restated —
    ``dev/.claude/skills/_shared/judge-core.md`` §5.6): *extract-JSON/label robustly ->
    validate-against-the-suite's-labels -> coerce-to-a-canonical-label -> compare*; a
    **parse-failure never crashes** — it scores ``0.0`` and is RECORDED distinguishably
    (``parsed == PARSE_FAIL_MARKER``) so it is countable. A silent parse-fail->0 drags every mean
    toward zero (``dev/.claude/rules/measurement-validity.md`` § the parse-fail rate is a *signal*),
    so the recording is load-bearing, not cosmetic.
  * **exact** — deterministic normalized string equality (documented normalization: trim + casefold)
    of the raw response against ``expected``, with an opt-in ``regex`` full-match mode. No parse
    step exists — every response deterministically matches or does not — so exact never emits
    ``PARSE_FAIL_MARKER``; its ``parsed`` field is the match verdict (``"match"`` / ``"no_match"``).

``rubric`` scoring is the LLM judge (``scoring/judge.py``) — a RUN-LEVEL pass, not a per-cell
scorer, because its per-judge parse-fail gate accumulates across the whole run. This module's
per-cell dispatcher (:func:`make_deterministic_scorer`) therefore raises a clear
``NotImplementedError`` for ``rubric``: the runner/CLI routes a rubric suite to the judge pass
(``scoring.judge.make_rubric_run_scorer`` -> ``runner.score_run_batch``) instead of this dispatcher.

Purity / determinism (plan Decision 10, the Done-when: ``mt score`` re-scoring is byte-identical to
inline scoring): every scorer is a pure function of ``(item, raw response)`` — no randomness, no
clock, no dict-ordering nondeterminism in the recorded value — so re-scoring stored raw responses
offline reproduces the first-pass :class:`~measure_twice.runner.ScoreOutcome` exactly.

The scorers return the runner's :class:`~measure_twice.runner.ScoreOutcome` and satisfy the
:data:`~measure_twice.runner.Scorer` seam directly (``(Item, str) -> ScoreOutcome``), so the runner
consumes a deterministic scorer with no adapter. Core is stdlib-only (``json``/``re``/
``statistics``).
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Sequence
from typing import Final

from measure_twice.runner import ScoreOutcome, Scorer
from measure_twice.suite import Item, ScoringSpec, compile_exact_pattern

__all__ = [
    "EXACT_SCORER",
    "PARSE_FAIL_MARKER",
    "VERDICT_SCORER",
    "ScoringError",
    "exact_match",
    "extract_verdict_label",
    "make_deterministic_scorer",
    "score_exact",
    "score_verdict",
    "suite_score",
]

# The scorer-name tags recorded on a row's ``scorer`` field. NEITHER may be ``"no_response"`` (that
# name is reserved by the runner's pre-scoring force-0 branch — ``runner.NO_RESPONSE_SCORER``), so a
# stored row's ``scorer`` unambiguously distinguishes a deterministic score from a force-0.
VERDICT_SCORER: Final[str] = "verdict"
EXACT_SCORER: Final[str] = "exact"

# The RECORDED marker a verdict parse-failure carries in ``ScoreOutcome.parsed`` (judge-core §5.6:
# parse-failure -> drop/score-0, RECORDED, never crash). It is deliberately human-readable and
# countable: count rows where ``scorer == VERDICT_SCORER and parsed == PARSE_FAIL_MARKER`` for the
# per-run parse-fail count. :func:`make_deterministic_scorer` rejects a suite whose labels reserve
# this token, so ``parsed == PARSE_FAIL_MARKER`` can never be confused with a real parsed label.
PARSE_FAIL_MARKER: Final[str] = "parse_fail"

# The exact-scorer ``parsed`` verdicts (the deterministic match outcome; never a parse-fail).
_EXACT_MATCH: Final[str] = "match"
_EXACT_NO_MATCH: Final[str] = "no_match"

# JSON keys, in priority order, that a model may carry a verdict under (``{"verdict": "flag"}`` is
# the canonical shape; the rest are conventional synonyms). Matched case-insensitively.
_VERDICT_KEY_PRIORITY: Final[tuple[str, ...]] = ("verdict", "label", "answer", "classification")

# A ``VERDICT: <value>`` / ``label = <value>`` labeled line. ``[^\S\n]`` is "horizontal whitespace"
# (space/tab, never a newline) so ``^``/``$`` stay line-anchored under ``re.MULTILINE``; the value
# group is the remainder of that one line, later scanned for a unique label.
_LABELED_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?im)^[^\S\n]*(?:verdict|label|answer|classification)[^\S\n]*[:=][^\S\n]*(.*?)[^\S\n]*$"
)


class ScoringError(ValueError):
    """Raised on a scorer-config fault (an un-scoreable suite spec, a bad regex ``expected``).

    Fail-loud sentinel subclassing ``ValueError`` — the package convention shared by
    ``config.ConfigError`` / ``suite.SuiteError`` / ``runner.RunError`` / ``adapters.AdapterError``,
    so one ``except (ConfigError, SuiteError, RunError, ScoringError)`` face catches them all. A
    scorer that cannot be honestly built (verdict scoring with no labels, a label reserving the
    parse-fail marker, an invalid regex pattern) aborts rather than silently mis-scoring.
    """


# --- The verdict parse spine (judge-core §5.6-conformant) ---------------------------------------


def _try_json(candidate: str) -> object | None:
    """``json.loads`` that NEVER raises — the parsed value, or ``None`` on any parse fault.

    Faults folded to ``None``: malformed JSON (``JSONDecodeError``/``ValueError``) AND a
    ``RecursionError`` from a deeply-nested-but-balanced nesting bomb (``'{"a":' * 3000 + ... +
    '}' * 3000``, a few KB a rambling/adversarial model can trivially emit — CPython's ``json``
    recurses per nesting level). This mirrors ``suite.load_suite``'s ``RecursionError`` guard and is
    the crash-safety backbone of the verdict spine: without it an uncaught ``RecursionError`` would
    propagate through ``score_verdict`` -> the runner's scorer call -> the CLI and kill the whole
    sweep (0 rows), violating the module's "parse-failure never crashes" contract.
    """
    try:
        parsed: object = json.loads(candidate)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    return parsed


def _json_object_spans(text: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` index spans of every top-level balanced ``{...}`` in ``text``.

    A model may wrap its JSON in prose or a ``` ```json ``` fence, or emit several objects; this
    finds each complete top-level object so the spine can parse each AND so the prose fallback can
    scan the text *outside* them. A brace inside a JSON string literal (``{"note": "a } brace"}``)
    does not close the object — the scan tracks string state and backslash escapes.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, i + 1))
                start = -1
    return spans


def _text_outside_valid_json(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """``text`` with every *well-formed* JSON object span blanked to a space (BLOCK 1 fix).

    The prose fallback must not scan JSON KEY NAMES or incidental string values: a response like
    ``{"flag": true, "verdict": "unclear"}`` has an INVALID verdict field (``"unclear"`` is not a
    label) and must parse-fail — the incidental key spelled ``flag`` must NOT win it a false 1.0.
    Blanking each parseable ``{...}`` span (word boundary preserved) leaves only genuine prose for
    the fallback, so a structured-but-invalid verdict scores 0.0, not a key-name accident.
    Non-JSON brace prose (``{{{ not json }}}``) does not parse, so it is left intact and still
    scannable.
    """
    parts: list[str] = []
    prev = 0
    for start, end in spans:
        if _try_json(text[start:end]) is not None:
            parts.append(text[prev:start])
            parts.append(" ")  # keep the word boundary the span used to provide
            prev = end
    parts.append(text[prev:])
    return "".join(parts)


def _labels_present(text: str, labels: Sequence[str]) -> list[str]:
    """The distinct canonical labels that occur in ``text`` as whole words (suite order, deduped).

    A label matches only when bounded by non-alphanumeric characters, so ``flag`` matches ``flag``,
    ``flag.``, and ``the verdict is flag`` but NOT ``flags`` or ``unflagged`` — a plural/embedded
    substring is not a verdict. Returns the canonical spellings from ``labels`` (case-insensitive
    match), so a downstream ``== expected`` comparison uses the suite's own casing. ``labels`` is
    guaranteed casefold-distinct (``ScoringSpec`` rejects case-variant duplicates at load, BLOCK 4),
    so a single occurrence can never double-count across two same-spelled patterns.
    """
    found: list[str] = []
    for label in labels:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(label)}(?![A-Za-z0-9])", re.IGNORECASE)
        if pattern.search(text) and label not in found:
            found.append(label)
    return found


def _unique_label(text: str, labels: Sequence[str]) -> str | None:
    """The single canonical label present in ``text``, or ``None`` if zero or >1 distinct labels.

    Ambiguity (two different labels co-occurring with no disambiguator) resolves to ``None`` rather
    than a guess — judge-core §5.5: low confidence escalates (here, to a recorded parse-fail),
    never fabricates a verdict.
    """
    present = _labels_present(text, labels)
    return present[0] if len(present) == 1 else None


def _single_or_conflict(found: Sequence[str]) -> tuple[str | None, bool]:
    """Collapse a tier's resolved labels: ``(the unique label | None, conflict?)``.

    ``conflict`` is True iff >=2 DISTINCT labels were resolved — two disagreeing recognized verdict
    signals, which must become a parse-fail (judge-core §5.5 "never guess"), ORDER-INDEPENDENTLY.
    Exactly one distinct label -> that label; zero -> ``(None, False)`` (fall through to the next
    tier).
    """
    distinct = list(dict.fromkeys(found))
    if len(distinct) == 1:
        return distinct[0], False
    return None, len(distinct) >= 2


def extract_verdict_label(response_raw: str, labels: Sequence[str]) -> str | None:
    """Extract-then-validate a verdict label from a raw response (the §5.6 spine's front half).

    Robustly recognizes the label across shapes, most-structured first, and validates it against the
    suite's ``labels`` in the same pass (an extracted token that is not one of ``labels`` is not a
    verdict). Returns the canonical label from ``labels`` on success, or ``None`` on parse failure
    (no recognizable/valid label, or an ambiguous/conflicting response). NEVER raises.

    Recognition ladder — each tier collects ALL resolvable labels and is ORDER-INDEPENDENT: it
    returns a label only when exactly one distinct label resolves at that tier; two disagreeing
    signals at a tier -> parse-fail (BLOCK 2); zero -> fall through:

    1. **JSON** — every balanced ``{...}`` object's ``verdict`` (or ``label``/``answer``/
       ``classification``, in that priority) string field, when it carries exactly one label.
       ``{"verdict":"flag"}`` with ``{"verdict":"pass"}`` in one response conflict -> parse-fail,
       regardless of order.
    2. **Labeled line** — every ``VERDICT: <value>`` / ``label = <value>`` line's value (so
       ``VERDICT: flag`` beats a ``RATIONALE: ... pass ...`` that is not itself a verdict line);
       ``VERDICT: flag`` + ``ANSWER: pass`` conflict -> parse-fail, regardless of order.
    3. **Bare / in-prose** — a scan of the text OUTSIDE any well-formed JSON object (so JSON keys
       never leak in, BLOCK 1); a lone bare label or a single label in prose is returned, two
       distinct -> ``None``.
    """
    if not labels:
        return None

    spans = _json_object_spans(response_raw)

    # 1. JSON tier — resolve each well-formed object (priority key), then collapse across objects.
    json_labels: list[str] = []
    for start, end in spans:
        obj = _try_json(response_raw[start:end])
        if not isinstance(obj, dict):
            continue
        lowered = {k.casefold(): v for k, v in obj.items() if isinstance(k, str)}
        for key in _VERDICT_KEY_PRIORITY:
            value = lowered.get(key)
            if isinstance(value, str):
                resolved = _unique_label(value, labels)
                if resolved is not None:
                    json_labels.append(resolved)
                    break  # this object resolved via its highest-priority verdict key
    label, conflict = _single_or_conflict(json_labels)
    if label is not None:
        return label
    if conflict:
        return None  # disagreeing JSON verdict fields -> ambiguous (never guess)

    # 2. Labeled-line tier — collect every verdict-ish line's resolved label, then collapse.
    line_labels: list[str] = []
    for match in _LABELED_LINE_RE.finditer(response_raw):
        resolved = _unique_label(match.group(1), labels)
        if resolved is not None:
            line_labels.append(resolved)
    label, conflict = _single_or_conflict(line_labels)
    if label is not None:
        return label
    if conflict:
        return None  # disagreeing labeled lines -> ambiguous

    # 3. Prose tier — scan ONLY text outside well-formed JSON, so incidental JSON keys never win.
    return _unique_label(_text_outside_valid_json(response_raw, spans), labels)


# --- The scorers (satisfy the runner's Scorer seam directly) ------------------------------------


def score_verdict(item: Item, response_raw: str, *, labels: Sequence[str]) -> ScoreOutcome:
    """Score one verdict item: 1.0 iff the parsed verdict equals ``item.expected``, else 0.0.

    On parse failure the score is 0.0 and ``parsed`` is :data:`PARSE_FAIL_MARKER` (RECORDED,
    countable) — the response never crashes the scorer. On a successful parse ``parsed`` is the
    canonical label (so a *wrong* answer records the actual label it parsed, distinct from a
    parse-fail — the two are different measurement events). Comparison is case-insensitive on the
    trimmed values. Assumes ``labels`` does not reserve :data:`PARSE_FAIL_MARKER`
    (:func:`make_deterministic_scorer` guarantees this).
    """
    label = extract_verdict_label(response_raw, labels)
    if label is None:
        return ScoreOutcome(parsed=PARSE_FAIL_MARKER, score=0.0, scorer=VERDICT_SCORER)
    score = 1.0 if label.strip().casefold() == item.expected.strip().casefold() else 0.0
    return ScoreOutcome(parsed=label, score=score, scorer=VERDICT_SCORER)


def exact_match(response_raw: str, expected: str, *, regex: bool = False) -> bool:
    """Deterministic match of ``response_raw`` against ``expected``.

    Two documented, deterministic modes:

    * **literal** (default) — normalized equality: both sides are ``str.strip()``-ed (leading/
      trailing whitespace ignored) then ``str.casefold()``-ed (case-insensitive). So ``" PARIS\\n"``
      matches ``"paris"``, but ``"the answer is paris"`` does NOT (full value, never substring).
    * **regex** — ``re.fullmatch`` of the ``strip()``-ed ``expected`` (case-insensitive, ``.`` spans
      newlines) against the ``strip()``-ed response; a partial match is not a match. Both sides are
      ``strip()``-ed so whitespace normalization is CONSISTENT with the literal mode (the response
      and the pattern are trimmed the same way — via ``suite.compile_exact_pattern``, the one owner
      of the compile contract). A ``regex=true`` suite's patterns are validated at LOAD
      (``Suite.__post_init__``), so on the normal path this never sees a bad pattern; the
      :class:`ScoringError` remap is defensive belt-and-suspenders for a direct call.

    Neither mode raises on the model RESPONSE (``response_raw`` is only matched against, never
    compiled), so a hostile response can never make this raise — the scorer's never-raises contract.
    """
    if regex:
        try:
            pattern = compile_exact_pattern(expected)
        except re.error as exc:
            raise ScoringError(
                f"exact regex expected {expected!r} is not a valid pattern: {exc}"
            ) from exc
        return pattern.fullmatch(response_raw.strip()) is not None
    return response_raw.strip().casefold() == expected.strip().casefold()


def score_exact(item: Item, response_raw: str, *, regex: bool = False) -> ScoreOutcome:
    """Score one exact item: 1.0 on a deterministic match of the raw response against ``expected``.

    ``parsed`` records the match verdict (``"match"`` / ``"no_match"``) — small, countable, and
    deterministic; the response text itself is already stored verbatim on the row, so ``parsed`` is
    not a redundant copy. Exact scoring has no parse step and therefore never emits
    :data:`PARSE_FAIL_MARKER`. See :func:`exact_match` for the normalization contract.
    """
    matched = exact_match(response_raw, item.expected, regex=regex)
    return ScoreOutcome(
        parsed=_EXACT_MATCH if matched else _EXACT_NO_MATCH,
        score=1.0 if matched else 0.0,
        scorer=EXACT_SCORER,
    )


def suite_score(item_scores: Sequence[float]) -> float:
    """The 0-100 suite score: ``100 x mean(item_scores)`` (plan §4).

    All-0 -> 0.0, all-1 -> 100.0, mixed -> the scaled mean. Fail loud on an empty sequence — a suite
    always has >=1 item (``Suite`` enforces it), so "no scores" means a caller passed nothing, not a
    real 0; scoring an empty mean would either raise deep in ``statistics`` or silently invent a
    number.
    """
    if not item_scores:
        raise ScoringError("suite_score requires at least one item score (got an empty sequence)")
    return 100.0 * statistics.mean(item_scores)


# --- The dispatcher (the runner/CLI seam; rubric routes to the run-level judge pass) ------------


def make_deterministic_scorer(scoring: ScoringSpec) -> Scorer:
    """Build the deterministic :data:`~measure_twice.runner.Scorer` for a suite's scoring type.

    Dispatches on ``scoring.type``:

    * ``"verdict"`` -> the verdict scorer bound to ``scoring.labels`` (fail loud if labels are
      missing/empty, or reserve :data:`PARSE_FAIL_MARKER`).
    * ``"exact"`` -> the exact scorer, in literal or ``regex`` mode per ``scoring.regex`` (the
      suite-schema field wired through here, so the regex capability is reachable from a real suite,
      not dead — plan §4's "string/regex match").
    * ``"rubric"`` -> ``NotImplementedError``: rubric scoring is the LLM judge
      (``scoring/judge.py``), a RUN-LEVEL pass (its per-judge parse-fail gate accumulates across the
      whole run), so it is not a per-cell :data:`Scorer`. The runner/CLI routes a rubric suite to
      the judge pass (``scoring.judge.make_rubric_run_scorer`` -> ``runner.score_run_batch``) rather
      than call this dispatcher — this raise is the seam marker, not a supported path.

    The returned scorer is pure and closes only over immutable label data, so it is safe to reuse
    across a whole sweep and to re-apply offline in ``mt score``.
    """
    if scoring.type == "verdict":
        labels = scoring.labels
        if not labels:
            raise ScoringError(
                "verdict scoring requires a non-empty scoring.labels list to validate against"
            )
        if any(label.casefold() == PARSE_FAIL_MARKER for label in labels):
            raise ScoringError(
                f"scoring.labels must not reserve {PARSE_FAIL_MARKER!r} "
                "(it is the recorded verdict parse-failure marker)"
            )
        frozen_labels: tuple[str, ...] = tuple(labels)

        def _verdict_scorer(item: Item, response_raw: str) -> ScoreOutcome:
            return score_verdict(item, response_raw, labels=frozen_labels)

        return _verdict_scorer

    if scoring.type == "exact":
        use_regex = scoring.regex

        def _exact_scorer(item: Item, response_raw: str) -> ScoreOutcome:
            return score_exact(item, response_raw, regex=use_regex)

        return _exact_scorer

    if scoring.type == "rubric":
        raise NotImplementedError(
            "rubric scoring is the LLM judge (scoring/judge.py), a run-level pass via "
            "make_rubric_run_scorer -> runner.score_run_batch; make_deterministic_scorer "
            "handles only the per-cell verdict/exact scorers"
        )

    # Unreachable in practice: ScoringSpec.__post_init__ already rejects any unknown type.
    raise ScoringError(f"unsupported scoring type {scoring.type!r}")
