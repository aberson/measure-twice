# Tier benchmark map

This is the workspace-facing explanation of what is pinned and why. The evidence ledger below is
the authoritative data record; its quote hashes keep the source spans honest and its statuses keep
policy classifications distinct from actual measurements.

## Reading the evidence

- `MEASURED` records the one pre-registered A3 paired deep-research run. It supports keeping
  Sonnet fan-out arms pinned only against a gross-quality-gap criterion; it is `n=1` versus `n=1`
  and drift-confounded.
- `PARTIAL` records operational evidence: the all-Fable fan-out cost/reliability incident and local
  endpoint timing/token limits. Neither proves a local slice is quality-safe.
- `ASSERTED` records the current routing and escalation policy plus its scan/config decisions. These
  rows are intentionally not upgraded by policy-shape checks, config parsing, or a static inventory.

The concrete gaps that should drive later benchmark work are the plan-init and user-debug Fable
seed picks, the Style-only local-reviewer rule, and the historical memory-distill Fable comparison.
Until a pre-registered run measures each decision at the same scope, it remains an assertion.

## Generated ledger

The text between the markers is byte-for-byte `mt claims render` output. The regression test checks
that relationship so this narrative cannot silently drift away from the JSONL source of truth.

<!-- BEGIN GENERATED LEDGER -->
# Evidence ledger

## MEASURED (1)

| Claim | Decision surface | Statement | Evidence | Sources |
| --- | --- | --- | --- | --- |
| `deep-research-sonnet-arms-a3` | `deep-research-pinned-arm-policy` | The paired A3 run supports keeping Sonnet arms pinned for deep research because it showed no decision-changing gross quality gap against the fully Fable-armed baseline. | wf_f25ce0c4-c03 | `docs/seeds/seed_sprint_wrap.md:78-81<br>docs/seeds/seed_sprint_wrap.md:171-180` |

## PARTIAL (2)

| Claim | Decision surface | Statement | Evidence | Sources |
| --- | --- | --- | --- | --- |
| `deep-research-fable-arm-cost` | `deep-research-pinned-arm-policy` | The fully Fable-armed deep-research baseline exposed a cost and reliability failure that motivates pinning fan-out arms to Sonnet. | wf_9ef05e51-a3a | `.claude/workflows/deep-research-pinned.js:15-19<br>docs/seeds/seed_sprint_wrap.md:188-190` |
| `local-endpoint-timeout-profile` | `switchboard-local-endpoint-defaults` | Switchboard's cold and warm timeout defaults and token floor are backed by observed endpoint timing and reasoning-token behavior, not by local-slice quality measurements. | - | `switchboard/switchboard/config.py:26-41` |

## ASSERTED (25)

| Claim | Decision surface | Statement | Evidence | Sources |
| --- | --- | --- | --- | --- |
| `escalation-deep-research-seed` | `workspace-model-tiering` | deep-research cited synthesis is a canonical Fable seed phase only when invoked through the pinned workflow. | - | `.claude/references/model-tiering.md:69-73` |
| `escalation-fanout-arms-never-fable` | `tier-escalate-up-correction` | Parallel fan-out arms remain Sonnet-tier because their value is diversity, not stronger per-arm reasoning. | - | `.claude/skills-gpt/tier-escalate/SKILL-core.md:34` |
| `escalation-memory-distill-conditional` | `workspace-model-tiering` | A cross-memory latent-principle consolidation round in memory-distill is a conditional Fable escalation case. | - | `.claude/references/model-tiering.md:28` |
| `escalation-named-trigger-required` | `tier-escalate-up-correction` | A conditional Fable escalation is incomplete unless it names a concrete trigger rather than relying on subjective size. | - | `.claude/skills-gpt/tier-escalate/SKILL-core.md:36` |
| `escalation-pinned-arms-required` | `tier-escalate-up-correction` | A session escalation that fans out work is actionable only when the dispatched arms are explicitly pinned to Sonnet; otherwise it must carry an arms-unpinned warning. | - | `.claude/skills-gpt/tier-escalate/SKILL-core.md:37` |
| `escalation-plan-feature-conditional` | `workspace-model-tiering` | A large cross-cutting plan-feature is a conditional Fable escalation case. | - | `.claude/references/model-tiering.md:25` |
| `escalation-plan-init-seed` | `workspace-model-tiering` | plan-init greenfield architecture authoring is a canonical Fable seed-artifact escalation point. | - | `.claude/references/model-tiering.md:19` |
| `escalation-plan-merge-conditional` | `workspace-model-tiering` | A deep-conflict merge of three or more plans is a conditional Fable escalation case. | - | `.claude/references/model-tiering.md:26` |
| `escalation-review-deep-conditional` | `workspace-model-tiering` | A high-stakes substrate or schema review-deep bugs lens is a conditional Fable escalation case. | - | `.claude/references/model-tiering.md:27` |
| `escalation-single-mind-synthesis` | `tier-escalate-up-correction` | Single-mind synthesis must not be fanned out merely to avoid choosing whether a stronger model is warranted. | - | `.claude/skills-gpt/tier-escalate/SKILL-core.md:35` |
| `escalation-single-seed-rule` | `workspace-model-tiering` | Fable session escalation is reserved for the single load-bearing seed-artifact shape that a diversity committee cannot replace. | - | `.claude/references/model-tiering.md:15-21` |
| `escalation-user-debug-seed` | `workspace-model-tiering` | user-debug hard root-cause diagnosis is a canonical Fable seed-artifact escalation point. | - | `.claude/references/model-tiering.md:20` |
| `memory-distill-fable-comparison-unmeasured` | `workspace-model-tiering` | The historical assertion that memory-distill is slightly worse on Fable is not supported by a quality measurement and must remain an assertion. | - | `measure-twice/docs/research/tier-skills-benchmark-map.md:39` |
| `offload-build-step-style-disabled` | `switchboard-enabled-call-sites` | build-step-style is a standing disabled call site because the surrounding Step 7 merge decision must not admit a local model. | - | `.claude/skills-gpt/tier-offload/SKILL-core.md:39<br>offload-scan-out/offload-config.json:8` |
| `offload-context-slim-classifier` | `switchboard-enabled-call-sites` | The context-slim Phase 2 classifier fan-out is configured as local-eligible while synthesis, gating, and apply remain outside the local claim. | - | `offload-scan-out/inventory.md:29<br>offload-scan-out/offload-config.json:10` |
| `offload-goblin-suggest-judge` | `switchboard-enabled-call-sites` | The goblin-suggest four-axis rubric judge fan-out is configured as local-eligible, while generation and the downstream code-ship gate are not. | - | `offload-scan-out/inventory.md:28<br>offload-scan-out/offload-config.json:9` |
| `offload-primary-gates` | `tier-offload-routing-rule` | Authorship, planning, orchestration, and final or gating judgment remain on the primary provider; only cheap fan-out judging can route local. | - | `.claude/skills-gpt/tier-offload/SKILL-core.md:24` |
| `offload-review-deep-style` | `switchboard-enabled-call-sites` | The review-deep Style lens is configured as a local-eligible slice with a low primary-provider effort recommendation, while other lenses and consolidation remain outside that local claim. | - | `offload-scan-out/inventory.md:26<br>offload-scan-out/offload-config.json:7` |
| `offload-review-gauntlet-style` | `switchboard-enabled-call-sites` | The review-gauntlet Style and Conventions lens is configured as a local-eligible slice while its correctness and aggregation work remain primary-provider work. | - | `offload-scan-out/inventory.md:25<br>offload-scan-out/offload-config.json:6` |
| `offload-skill-eval-setup-disabled` | `switchboard-enabled-call-sites` | The legacy skill-eval-setup grader remains explicitly configured false pending resolution of its final-judge precondition. | - | `offload-scan-out/inventory.md:30<br>offload-scan-out/offload-config.json:11` |
| `offload-skill-evolve-grader` | `switchboard-enabled-call-sites` | The inventory configures the shared skill-evolve grade-phase grader as a local-eligible advisory slice. | - | `offload-scan-out/inventory.md:24<br>offload-scan-out/offload-config.json:5` |
| `offload-skill-iterate-grader` | `switchboard-enabled-call-sites` | The inventory configures the skill-iterate grade-phase grader as a local-eligible advisory slice. | - | `offload-scan-out/inventory.md:23<br>offload-scan-out/offload-config.json:4` |
| `offload-strong-gate-precondition` | `tier-offload-gate-precondition` | A local judge array is allowed only when a strong-tier provider gate consolidates it; directly gating arrays remain disabled until that gate exists. | - | `.claude/skills-gpt/tier-offload/SKILL-core.md:37` |
| `offload-style-only` | `tier-offload-reviewer-lenses` | Among multi-lens reviewers, only the Style lens is treated as a cheap local slice; Correctness and Bugs remain on the primary provider. | - | `.claude/skills-gpt/tier-offload/SKILL-core.md:33` |
| `offload-tool-using-arms` | `tier-offload-tool-using-judges` | Judge arms requiring live tools such as WebFetch, browser, gh, or substrate commands must remain on the primary provider. | - | `.claude/skills-gpt/tier-offload/SKILL-core.md:35` |

## STALE (0)

No claims.
<!-- END GENERATED LEDGER -->

## Updating this map

1. Change a claim only with a current source citation and the evidence appropriate to its status.
2. Run `uv run mt claims audit`; a stale row is a failure, not a value to silently refresh.
3. Replace only the generated block with `uv run mt claims render` output, then run the map test.
4. A future benchmark may promote an assertion only after a pre-registered production run supplies
   evidence at the same decision scope.
