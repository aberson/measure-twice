# 01 — Deterministic scoring & anchor calibration

*Phase A, Step 5. Learning woven in (plan Decision 9): what this instrument measures, what it
cannot tell you, and what surprised us building it.*

The code: [`measure_twice/scoring/deterministic.py`](../../measure_twice/scoring/deterministic.py),
[`tests/anchors/`](../../tests/anchors/), [`tests/test_scoring.py`](../../tests/test_scoring.py).
Doctrine it conforms to (referenced, not restated): judge-core §5.6 (the parse+aggregate spine) and
`dev/.claude/rules/measurement-validity.md` (calibrate-with-anchors, fail-loud, score-the-artifact).

## What this instrument is

Two zero-model-call scorers plus a 0–100 normalizer, wired as the runner's real default `Scorer`
for verdict/exact suites:

- **verdict** — parse the model's verdict *label* out of a raw response and compare it to the item's
  curated `expected`. Score `1` if they match, else `0`.
- **exact** — normalized string equality (trim + casefold) of the raw response against `expected`
  (optional regex full-match mode). Score `1` or `0`.
- **suite score** — `100 × mean(item scores)`, giving the 0–100 scale the flagship dataset's
  discriminative contract is stated in (plan §4).

These are the scorers the tier-ordering claims rest on precisely *because* they involve no LLM in the
loop (plan Decision 7 — kills judge-circularity: Claude judging Claude never decides a MEASURED flip).

## What it measures

**Did the model emit the right discrete answer, in any reasonable surface form?** The verdict spine
(judge-core §5.6: extract-robustly → validate-against-`labels` → coerce → compare) recognizes the
label whether the model answered as JSON (`{"verdict": "flag"}`), a bare word (`flag`), a labeled
line (`VERDICT: flag`), or a single label embedded in prose — case-insensitively. So the score
measures *the answer*, not *the formatting*: a model that is right but verbose is not punished, and a
model that is wrong but tidy is not rewarded. That separation is the whole point of the extract step
— without it we would be scoring instruction-following-of-output-format, a different (and less
interesting) quantity than correctness.

The normalization for **exact** is deliberately narrow and documented: leading/trailing whitespace is
ignored and case is folded, but the match is against the *whole* value, never a substring
(`"the answer is paris"` does **not** match `"paris"`). That keeps `exact` honest — it answers "is
the response *this string*?", not "does the response *mention* this string?".

## What it cannot tell you

- **Open-ended quality.** Neither scorer can judge whether an explanation is good, a plan is sound,
  or prose is well-written. That is the LLM rubric judge's job (Step 6, `scoring/judge.py`); this
  module's dispatcher raises `NotImplementedError` for `rubric` on purpose, leaving that seam clean.
  A deterministic scorer over a discrete gold answer is a *sharp, narrow* instrument — its precision
  is exactly why it can't stretch to graded quality.
- **Why** a model got it wrong. A `0` from `verdict` collapses "answered the wrong label" and
  "answered nothing parseable" into the same number — *unless you read the recording* (below).
- **Anything about a saturated item.** If every model answers an item correctly, the item scores
  1.0 for everyone and discriminates nothing; that is a *calibration* signal (`mt calibrate`, Step
  13), not something a single item's score reveals.

## The parse-fail rate is a signal, not noise

The load-bearing design choice: a parse failure (no recognizable/valid label, or an *ambiguous*
response naming two labels with no disambiguator) scores `0.0` **and is recorded** as
`parsed == "parse_fail"` — never a crash, never a silent drop. measurement-validity's rule is that a
silent parse-fail→0 drags every mean toward zero and is indistinguishable from a genuinely-wrong
model; making it *countable* turns it into an instrument-health gauge:

```
parse_fail_rate = (rows where scorer=="verdict" and parsed=="parse_fail") / verdict_rows
```

A rising parse-fail rate for one model usually means *the instrument*, not the model, is off — the
prompt doesn't elicit a parseable verdict, or the `labels` set is wrong — long before it means the
model is bad. A wrong-but-parsed answer records the *actual* label instead, so "wrong answer" and
"un-scoreable answer" stay separable events. (We reserve the `parse_fail` token by rejecting any
suite whose `labels` contains it, so the marker can never collide with a real label.)

Ambiguity resolves to a recorded parse-fail rather than a guess (judge-core §5.5: low confidence
escalates, never fabricates). The labeled-line shape is tried *before* the whole-response prose scan
precisely so `VERDICT: flag\nRATIONALE: this is not a pass` reads as **flag** — the explicit verdict
line disambiguates what an unstructured scan would (correctly) call ambiguous.

## The anchors: proving the instrument can fail garbage

Per measurement-validity's calibrate-with-anchors rule, every deterministic scorer ships a frozen
known-good / known-garbage pair ([`tests/anchors/scorer_anchors.json`](../../tests/anchors/scorer_anchors.json)),
and a CI gate asserts `score(good) > score(garbage)` **forever**. A scorer that can't rank a
known-good response above a known-garbage one can't pick winners, so this is the precondition for
trusting any comparative number the toolkit later produces. The pairs are frozen as *data* (a stable
regression contract) and driven through the *production* `make_deterministic_scorer` — the gate
proves the shipping scorer discriminates, not a re-implementation of it (this is the same "assemble
through the production path" discipline that keeps a bench from measuring a sibling system).

## What surprised us

1. **The exact scorer needed a `parsed` field that isn't the response.** Verdict's `parsed` is
   naturally the extracted label. Exact has no "parse" — every string deterministically matches or
   doesn't — so there is no analogous extracted value. Storing the *normalized response* would just
   duplicate `response_raw`. We settled on recording the match *verdict* (`"match"` / `"no_match"`):
   small, countable, and honestly reflecting that exact scoring's only "parse" is the yes/no itself.
   A corollary: **exact can never emit `parse_fail`** — a claim worth stating out loud, because it
   means the parse-fail health gauge above is a *verdict-suite* metric specifically.

2. **Byte-identical re-scoring falls out of purity — but only if nothing time-varying leaks into the
   recorded value.** The Done-when (`mt score` reproduces inline scores byte-for-byte) held on the
   first try *because* `ScoreOutcome` carries no clock, no randomness, and no dict-ordering: the
   runner's `elapsed_s` (which *is* time-varying) lives on the row but outside the scored fields, so
   re-scoring `replace()`s only `parsed`/`score`/`scorer` and the serialized bytes are identical.
   The lesson is that determinism is a property of *what you record*, not just of the function —
   one stray timestamp in the outcome would have quietly broken it.

3. **Robust extraction is mostly about *ordering the attempts*, not the regexes.** The hard part
   wasn't recognizing JSON vs. a bare label; it was deciding that a JSON verdict key beats a labeled
   line beats a whole-response scan, so that the most-structured signal wins and prose ambiguity is
   the last resort. The ladder is the instrument; the individual matchers are almost incidental.

## Iteration 2 — five ways a "robust" parser still leaks (review-deep findings)

An adversarial six-lens review live-reproduced four measurement-validity defects in the verdict
spine plus a dead-capability gap; the fixes sharpened the instrument in ways worth recording:

1. **A prose scan must never read JSON *structure*.** `{"flag": true, "verdict": "unclear"}` has an
   invalid verdict (`"unclear"` is no label) and must score 0.0 — but the whole-response fallback
   was scanning the raw text, so the incidental *key* spelled `flag` won a false 1.0. The fallback
   now scans only text *outside* any well-formed JSON span. A structured-but-invalid verdict is a
   parse-fail, not a key-name accident. (The general lesson: when you have a structured signal and a
   fuzzy one, the fuzzy one must not see the structured one's internals.)

2. **"First match wins" is order-dependent, and order-dependence is a scoring bug.** `VERDICT: flag`
   then `ANSWER: pass` returned *flag*; reversed, *pass* — the same response scored differently by
   line order. Each tier now *collects every* resolvable signal and returns a label only if exactly
   one distinct label appears; two disagreeing signals → parse-fail, order-independently
   (judge-core §5.5, "never guess"). Determinism isn't just "no clock" — it's also "no dependence on
   incidental input order".

3. **"Never crashes" has to survive a *valid* input too.** The hostile-input test used unbalanced
   braces, which never reach `json.loads`. A *balanced* deep-nesting bomb (a few KB) is valid JSON
   that raises `RecursionError` — a `RuntimeError`, not the `ValueError` the parser was catching — so
   it propagated through the scorer, the runner, and the CLI into a raw traceback that killed the
   whole sweep. Fixed **at the source**: `extract_verdict_label` catches `RecursionError` (mirroring
   `suite.load_suite`) → parse_fail. The right fix is *at the scorer*, not a runner catch-all — see
   iteration 3 below, where an over-broad runner wrap turned out to *destroy stored data*. The
   correct division: a hostile *response* is the scorer's job to fold into parse_fail; a bad *suite*
   fails loud at load; a genuine scorer *bug* must crash loudly, never be masked.

4. **The marker mechanism can mass-poison a suite.** Case-insensitive matching + a case-*sensitive*
   dedup meant labels `["Flag", "flag"]` made one `flag` match both patterns → "two labels" →
   parse-fail on *every correctly-answered item* — the silent-mean-drag failure, mass-produced by
   the safety mechanism itself. Fixed fail-loud: case-variant duplicate labels are a suite-authoring
   error rejected at load, the same stance as the parse-fail-marker reservation.

5. **A documented capability must be reachable.** `exact` scoring had a `regex` mode that no suite
   could select (no schema field), while the plan and this note described "string/regex match" as
   live — an overclaim. Closed by wiring a `ScoringSpec.regex` flag through `load_suite` →
   `make_deterministic_scorer` → the exact scorer (with an integration test through the production
   loader), and normalizing the pattern's whitespace consistently with the literal mode. Now the
   documented capability is the real one.

## Iteration 3 — the "defense-in-depth" that destroyed evidence

The iteration-2 fix for the nesting bomb added a *second* guard "just in case": a `try/except` around
the scorer call in the runner that, on any exception, recorded an error row and blanked
`response_raw`. A re-review live-reproduced the disaster: `mt score` re-scores from stored raw and
atomically rewrites the file, so a single exception during a re-score **overwrote a stored answer
with `""` — permanently**. The guard meant to protect the instrument was silently deleting its
evidence, and reusing the transport error code made the loss indistinguishable from a flaky network.

Two lessons worth more than the bug:

- **A catch-all around the thing you're unsure about is not "defense" — it's a place for bugs and
  data to disappear.** The wrap would also have *masked* a genuine scorer programming bug (recorded
  as a benign-looking error row) that should have crashed a test. The correct posture is the
  opposite: make each *class* of failure impossible at its own boundary — a hostile *response* folds
  to parse_fail *in the scorer*; a bad *suite pattern* fails loud *at load*; a real *bug* crashes
  *loudly*. With those three, there is nothing left for a runner catch-all to legitimately catch, so
  the right amount of catch-all is **none**.

- **Re-scoring must be provably non-destructive, and that's a test, not a comment.** `mt score`
  `replace()`s only `parsed`/`score`/`scorer`; `response_raw` is durable evidence and must survive
  verbatim. There is now a test that stores real answers, re-scores, and asserts every raw is
  byte-identical afterward — the '404 → ""' destruction is guarded forever.

The load-time regex validation (`Suite.__post_init__` compiles every `regex=true` pattern, failing
loud with `SuiteError` before any run) is what let the wrap be deleted safely: it removed the last
input-independent way the scorer could raise, so "the scorer never raises on input" became true
enough to rely on. `suite.compile_exact_pattern` is the single owner of the compile contract, so the
pattern validated at load is byte-for-byte the pattern matched at score time.
