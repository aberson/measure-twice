# 02 — LLM rubric judges: k=3 median & the per-judge parse-fail gate

*Phase A, Step 6. Learning woven in (plan Decision 9): what the rubric judge measures, what it
cannot tell you, and what surprised us building it.*

The code: [`measure_twice/scoring/judge.py`](../../measure_twice/scoring/judge.py),
[`tests/test_judge.py`](../../tests/test_judge.py),
[`tests/anchors/test_rubric_anchor_gate.py`](../../tests/anchors/test_rubric_anchor_gate.py).
Doctrine it conforms to (referenced, not restated): judge-core §5.6 (the parse+aggregate spine) and
`dev/.claude/rules/measurement-validity.md` (calibrate-with-anchors, fail-loud, k≥3/median for LLM
judges). It ports void_furnace's judge invariants project-neutrally — it never imports void_furnace.

## What this instrument is

The third scoring path, after the two deterministic scorers ([01](01-deterministic-scoring-and-anchors.md)):
an **LLM judge** that grades an open-ended response against a rubric on a 0–10 scale, then normalizes
to 0–1 so it plugs into the same `100 × mean` suite normalization as verdict/exact.

- **The two-line contract.** The judge prompt (`build_judge_prompt`) asks for exactly `SCORE: <n>`
  then `RATIONALE: <text>`. Suites carry *all* content: for a rubric suite the item's `expected`
  field **is** the rubric text, so the prompt is assembled in one place from the item's own content —
  there is no hidden per-judge template (the fallback-prompt bug class is structurally absent, the
  same discipline that keeps a bench measuring the shipping system).
- **k=3 median of parsed samples.** Each (judge, response) is sampled `JUDGE_SAMPLE_K = 3` times and
  the judge's contribution is the **median** of the successfully-parsed samples. The item score is
  the mean across judges of each judge's 0–10 median, divided by 10.
- **The per-judge parse-fail gate.** `judge_run` accumulates each judge's own parse-fail rate across
  the whole run and aborts (`JudgeParseFailError`) if any single judge exceeds
  `JUDGE_PARSE_FAIL_RATE_THRESHOLD = 0.5`.

Because the gate is *run-level* — it can only fire once every response has been judged — the rubric
path is a **scoring pass**, not a per-cell scorer. `make_rubric_run_scorer` builds a
`RunScorer` that `mt score` drives via `runner.score_run_batch`; the deterministic dispatcher
(`make_deterministic_scorer`) stays deterministic-only and still raises for `rubric`.

## What it measures

**Graded quality against a named rubric**, taming single-sample noise. A single-sample LLM judge is
notoriously jittery — the same artifact can score 10 on one call and 5 on the next. Sampling k=3 and
taking the **median** (not the mean) makes the judge's contribution robust to one outlier sample: a
lone spurious 10 or 0 among two consistent scores is discarded by the median, where a mean would drag
toward it. This is the measurement-validity remedy ("k≥3 samples or a median") applied verbatim.

The 0–10 → 0–1 normalization is deliberate: it lets a rubric suite share the flagship dataset's 0–100
normalization exactly, so a rubric score is comparable in scale to a verdict/exact score even though
its *provenance* (an LLM's graded judgement) is entirely different.

## What it cannot tell you

- **It cannot escape judge-circularity.** The judges are Claude (`claude_cli`), and the responses
  under grading are frequently Claude-tier too — **Claude judging Claude**. A model that shares the
  judged family's blind spots and stylistic preferences will systematically over-reward its own
  kind. k=3 + median tames *variance* (sampling noise); it does **nothing** for *systematic bias*
  — three samples of a biased judge are three biased samples, and their median is exactly as biased.
  This is why the workspace flagship dataset uses `verdict`/`exact` items with curated gold answers
  for **every tier-ordering claim** (plan Decision 7): no LLM sits in the loop for a claim that
  matters. The rubric judge is for **capability profiling** — "what is this model good and bad at",
  localized to tags — never for a comparative tier ranking.
- **It never solely flips a ledger row to MEASURED** (plan Decision 7). A rubric result is
  supporting texture, not a tier verdict. A MEASURED flip rests on deterministic gold; a rubric score
  can *inform* the reader but cannot *decide* the ledger. Stating this out loud is the guardrail: the
  moment a rubric number is allowed to flip a claim alone, judge-circularity has quietly re-entered
  the claims that were designed to exclude it.
- **Why** a low score happened. A 0.0 collapses "the judges graded it poorly" and "no judge produced
  a parseable score for it" into the same number — *unless you read the recording* (below).

## Parse-fail vs invoke-error: two different failures, kept distinct

The load-bearing distinction, ported from void_furnace:

- A **parse-fail** is a judge that *returned* a response with no parseable `SCORE:` line. It is a
  broken *output format* — it counts toward the gate rate (numerator **and** denominator), because a
  judge that can't emit the required shape is producing garbage the median would otherwise launder.
- An **invoke-error** is the adapter failing to get any usable response — a transport error *or* the
  no-response (empty-text) state. It is a broken *transport*, not a broken format, so it is excluded
  from **both** counters. An unreachable endpoint is not evidence the judge can't format a score.

Collapsing the two would be a measurement lie in either direction: count invoke-errors as parse-fails
and a flaky network trips the format gate; count parse-fails as invoke-errors and a genuinely broken
judge slips it. A judge whose *every* sample invoke-errors has **zero** parse-attempts and is
**skipped** by the gate entirely (no parseability signal), never a false fire.

A rubric item where **no** judge produced a single parseable sample is a **recorded** parse-fail: it
scores `0.0` with `parsed == "parse_fail"` (the same marker the deterministic verdict spine uses),
never a silent 0 that masquerades as a real low grade. Making it countable turns it into an
instrument-health gauge, exactly as in [01](01-deterministic-scoring-and-anchors.md).

## The gate is per-judge, not pooled — and that's the whole point

`JUDGE_PARSE_FAIL_RATE_THRESHOLD = 0.5` fires on **each judge's own** rate, not the pooled rate
across all judges. The failure this prevents is precise: a broken judge force-scoring parse-fails
into a run can **hide behind healthy peers** under a pooled rate. Two judges, one 100% broken and one
perfectly healthy, pool to exactly `3/6 = 0.50` — which a strict `> 0.50` pooled gate does **not**
fire, even though half of every item's judgement is coming from a judge that cannot emit a score.
Per-judge, the broken judge's own `1.00` trips instantly and the run aborts naming it. The gate fires
*before* any row is rewritten, so a broken judge aborts loud with the run store left byte-for-byte
untouched — never a partial, poisoned rewrite.

A single-judge run's per-judge rate equals its pooled rate, so the default `["sonnet"]` config is
unaffected — the per-judge grain only *adds* protection once a second judge appears.

## The anchor: proving the judge pipeline can rank good over garbage

Per measurement-validity's calibrate-with-anchors rule, the rubric path ships a frozen known-good /
known-garbage response pair ([`tests/anchors/rubric_anchor.json`](../../tests/anchors/rubric_anchor.json))
and a CI gate asserts `score(good) > score(garbage)` **forever**, driven through the *production*
`judge_run` with a **stubbed** judge. The stub is the subtle part: this gate proves the **scoring
pipeline** discriminates (k=3 median → 0–10 → normalize → ordering), *not* that a live model grades
well. A live judge can't be made flake-free, so live-judge quality is a calibration concern, never a
per-commit gate — the same division judge-core §7 draws between the mechanized snapshot gate and the
deferred live kappa sweep. What the frozen anchor *does* lock down is that the deterministic half of
the instrument — the sampling, the median, the normalization, the ordering — can rank a clearly-good
response above a clearly-garbage one. If that ever breaks, no live number built on top of it is
trustworthy.

## What surprised us

1. **The run-level gate broke the per-cell `Scorer` abstraction — correctly.** Steps 4–5 established
   a clean `Scorer = (Item, str) -> ScoreOutcome` seam, and the instinct was to make the rubric judge
   one more `Scorer`. It can't be: the gate needs *every* judgement before it can compute a rate, and
   a per-cell callable sees one response at a time (and can't populate the row's `judge_scores` list
   at all, since the runner hardcodes it). The honest fix was a *second* seam — `RunScorer` +
   `score_run_batch` — rather than contorting the judge into the per-cell shape. The lesson: when a
   new computation has genuinely different data dependencies (whole-run vs one-cell), a parallel seam
   is cleaner than overloading the existing one until it lies about what it needs.

2. **"Median of parsed samples" makes the median test collide with the gate.** The natural unit test
   for "two parse-fails leave a single value → that value is the median" uses one judge at 2/3
   parse-fail — which is `0.67 > 0.5` and (correctly) trips the gate before any result is returned.
   The median math for parse-fail-heavy cells is therefore only observable *below* the gate, at the
   cell level (`_judge_one_cell`). That the two collide is not a bug: it's the gate doing its job —
   a judge parse-failing the majority of its samples is exactly what the gate exists to abort, so the
   only place a 2/3-fail cell's median is a legitimate value is in isolation, before the run-level
   verdict. Separating "median arithmetic" (cell-level) from "is this judge trustworthy" (run-level)
   made both testable without one masking the other.

3. **The median tames the wrong kind of noise for the scariest failure.** Building this, it was
   tempting to feel that k=3 + median made the judge *reliable*. It makes it reliable against
   *sampling variance* — and that is genuinely valuable. But the failure that actually threatens the
   claims (judge-circularity) is *systematic*, and the median is completely blind to it: a biased
   judge's three samples share the bias and their median inherits it whole. Internalizing that the
   remedy and the threat are orthogonal is what makes Decision 7 (deterministic gold for every
   tier-ordering claim) feel like a load-bearing wall rather than belt-and-suspenders.
