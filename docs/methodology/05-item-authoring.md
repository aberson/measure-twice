# 05 — Item authoring for `tier-judging-v0`

`tier-judging-v0` is the first narrow instrument for reviewer and gate decisions that could
eventually inform tier routing. It is not a model result. The current `difficulty_prior` values are
author expectations used to seed coverage; empirical difficulty, saturation, and model ordering
begin only with the pre-registered Step 13 observation run.

## Decision scope

The suite asks only whether a model can emit a deterministic `pass` or `flag` judgment for a
bounded artifact. Its three lenses are deliberately distinct:

- `lens-style`: maintainability and local convention calls.
- `lens-correctness`: stated behavioral and safety invariants.
- `lens-grading`: evidence sufficiency for a delivery/gate decision.

The prompts provide the operative rule and the observed change/evidence. They do not expose an
answer key through a rubric judge, and the production verdict parser—not a model—awards credit.
Each item therefore has curated binary gold, not a post-hoc explanation-derived score.

## Coverage and provenance

The v0 file contains 100 items across five 20-item authoring batches: easy, lower-mid, upper-mid,
hard, and adversarial. Every tag has at least eight items, every difficulty bucket has 20, and each
batch has exactly 20 items. The five provenance strings make the source-batch boundary visible and
enforce the Step 10 initial-suite cap of no more than 20% from one source snapshot/template.

All v0 examples are authored counterfactuals based on the bounded reviewer/gate patterns selected
in the domain investigation. That makes their status clear: they are curated dataset content, not
claims that a real project, endpoint, or model produced any outcome. The candidate harvester remains
available for later source-grounded extensions, but an uncurated Git candidate is explicitly tagged
`needs-gold` and cannot enter this suite unchanged.

## Frozen scorer anchors

`tests/anchors/tier_judging_v0_anchors.json` selects six real v0 items across all three lenses and
both answer labels. The gate feeds each good/garbage response through
`make_deterministic_scorer(suite.scoring)` and requires strict ordering. This checks the actual
flagship scoring contract while making no live model call and no performance claim.

Before a score can change a routing claim, Step 13 must run the complete roster against this exact
content hash with the stated pre-registration. Any replacement after calibration creates a new
version/content hash and keeps these v0 rows historical rather than silently blending instruments.
