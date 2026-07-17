# tier-offload / tier-escalate — benchmark / eval / model-pinning map
(Recon by read-only Explore agent, 2026-07-15, persisted by orchestrator. Sources: both SKILL.md files, shared taxonomy reference, tier-escalate evals, tier-offload pytest contract tests, switchboard config.py, committed artifacts in offload-scan-out/ + tier-escalate-out/, memory files, seed_sprint_wrap.md.)

## 1. Role taxonomy (exact definitions)

Single source of truth: `C:\Users\abero\dev\.claude\references\skill-role-taxonomy.md` §1 (lines 21-29). Both skills CITE it and are forbidden from re-inlining it (guarded by `test_taxonomy_osot.py`). Verbatim rows:

- **ORCH** (l.23) — "Orchestration — drives a multi-step pipeline, dispatches sub-agents" / "Stateful coordinating spine; holds cross-step context"
- **AUTHOR** (l.24) — "Authorship — writes code, prose, docs, plans, notebook cells" / "Produces content; each pass creates rather than scores"
- **PLAN** (l.25) — "Planning — designs a plan, sequences steps, makes architecture calls" / "Open-ended design reasoning; decisions ripple downstream"
- **JUDGES** (l.26) — "A fan-out **array** of judges/graders scoring N items in parallel" / "Parallel array of cheap scorers; arms are independent and individually low-stakes"
- **GATE** (l.27) — "The final/consolidating judgment — the merge/ship/keep decision" / "Single decision point consuming upstream findings; correctness-critical"
- **MECH** (l.28) — "Mechanically checkable — exit code, valid JSON, regex/refusal match, `goal_condition`" / "no LLM reasoning required"
- **SOLO** (l.29) — "A single-pass reasoning call, not a fan-out array" / "One call, one context; not a parallel array"

Tag-boundary rules (l.31-40): a fan-out whose arms *produce* is AUTHOR not JUDGES; a prose checklist run as one pass is SOLO not JUDGES; the consolidating decision over a JUDGES array is a separate GATE (tag both). No "GRADER" tag — graders fall under JUDGES.

## 2. The two governing rules (exact wording)

**Difficulty-based routing rule (tier-offload, down)** — SKILL.md l.22: "authorship, planning, orchestration, and any final/gating judgment stay on Claude; only a fan-out array of cheap judges/graders goes local; mechanically-checkable work goes to a script (no LLM). The local model is **never on a correctness gate** — it advises, a Claude final judge decides." (framed as "Switchboard Decision 9 / the 3-tier judge split"). Four layered corrections (l.30-33): (1) authorship fan-outs stay Claude; (2) only the Style reviewer lens is cheap, Correctness/Bugs stay Claude; (3) a checklist "fan-out" that is really one pass is SOLO not LOCAL; (4) tool-using judge arms (WebFetch/gh/substrate) are never local-safe. Hard invariant (l.35): "offloading a JUDGES array is only safe if a Claude GATE consolidates its findings" → else emit `gate-precondition: insert-claude-final-judge`.

**Seed-artifact escalation rule (tier-escalate, up)** — SKILL.md l.22: "escalate a session to Fable only for a single load-bearing seed-artifact — a phase whose one output is the deliverable's quality ceiling AND where a diversity committee (parallel reviewers/iterations) cannot substitute for a stronger single reasoner." Three canonical shapes: greenfield architecture authoring (plan-init), hard root-cause diagnosis (user-debug), deep multi-source cited synthesis (deep-research). Four up-corrections (l.32-35): (1) fan-out arms are NEVER Fable (diversity beats per-arm strength); (2) do NOT fan out single-mind synthesis; (3) conditional escalations must NAME their trigger; (4) session escalation cascades into unpinned arms — check dispatch layer, flag `arms-unpinned`.

## 3. Evidence base — empirical vs asserted (THE CRUX)

Rules rest overwhelmingly on **heuristic/role-shape reasoning and a classification scan, not quality benchmarks.** Exactly ONE genuine head-to-head quality measurement, plus one cost incident and one endpoint-timing set.

### Empirically-backed:
1. **A3 paired deep-research run — the only real head-to-head.** `seed_sprint_wrap.md` §6 l.171-183 + `user_model_preference.md` l.28-33. Pre-registered (§A3 l.78-81). Treatment = Sonnet-armed `deep-research-pinned` on Fable session, run `wf_f25ce0c4-c03`, 106/106 agents, 0 errors; baseline = fully-Fable-armed run (`seed_sprint_wrap_baseline-fable-deep-research.json`). Metrics: fabricated figures 0; 2/2 live spot-checks verbatim (Wataoka 2410.21819, JudgeDeceiver 2403.17710); confirmed/killed/unverified 15/10/0 vs 20/5/0. Verdict: KEEP Sonnet arms. Caveats: **n=1 vs n=1, gross-gap-only, drift-confounded**. Watch-item: narrower synthesis (6 vs 10 findings), lower primary-source share (58% vs 86%). Measures **arm strength**, NOT "synthesis needs Fable."
2. **104/104-Fable-arms incident — cost measurement.** tier-escalate SKILL.md l.35: `/deep-research` on a Fable session ran 104/104 workflow agents on Fable (~101 fan-out arms; ~5x cost + session-limit blowout mid-run). Basis for up-correction 4. Cost note seed_sprint_wrap.md §6 l.188-190: Sonnet arms "~5x cheaper arm pricing, zero mid-run deaths (baseline had 50 dead agents + an interrupted run)."
3. **switchboard spike endpoint timings** — `switchboard/switchboard/config.py` l.26-39, citing `docs/investigations/switchboard-spike.md` D1: general-35b cold-load 130-264s observed, warm 16-22s, reasoning ~400-620 tokens → `DEFAULT_COLD_TIMEOUT_S=540`, `DEFAULT_WARM_TIMEOUT_S=60`, `DEFAULT_MAX_TOKENS=800` / `MIN_MAX_TOKENS=600`. Back operational endpoint defaults, not which slice is local-safe.
4. **review-deep Style lens already on Haiku** — existing-deployment fact (offload SKILL.md correction 2 l.31; inventory l.24). Adjacent-evidence at best.

### Asserted / heuristic (NO measurement — the bulk):
- The **entire down-routing rule** — asserted from "Switchboard Decision 9 / 3-tier split"; no benchmark that a local model judges any slice as well as Claude. Safety-conservative heuristic, not measured accuracy floor.
- The **entire up-escalation rule** + "Opus + Sonnet fan-out beats a lone Fable pass almost everywhere" (user_model_preference l.12) — derives from the **2026-07-09 42-skill scan** = a **classification** exercise (7 read-only Explore readers tagging phases), explicitly NOT a quality benchmark.
- **plan-init and user-debug FABLE-SEED picks** — purely structural assertions; zero measurement.
- **"Only the Style lens is cheap; Correctness/Bugs stay Claude"** (correction 2) — heuristic tied to code-quality.md; no local-model benchmark showing Correctness/Bugs fail locally.
- **"memory-distill slightly worse on Fable"** (user_model_preference l.12; sample map l.35-37) — presented as a "2026-07-09 scan found" finding, but that scan was classification. **Assertion dressed as evidence.**
- Every per-slice LOCAL verdict in the produced inventory — role-shape, not per-slice accuracy test.

**Count:** ~1 genuine quality measurement + 1 cost incident + 1 endpoint-timing set — vs. every "which tier for which slice/phase" decision resting on heuristics/classification. Top-3 asserted-not-measured: (a) plan-init/user-debug Fable picks; (b) "only Style is cheap"; (c) "memory-distill worse on Fable."

## 4. Eval harnesses

Asymmetric — only tier-escalate has an LLM-output eval suite; tier-offload has pytest contract tests.

**tier-escalate `evals/`** (grades the skill's markdown OUTPUT):
- `evals.json` — 22 T/F assertions, 6 categories, `passing_threshold: "21/22"`. Structure/columns (1-6), FABLE-SEED discipline on the trio (7-12), "fan-out arms never Fable" (13-15), "never fan out single-mind synthesis" (16-17), "conditional triggers named" (18-19), never-auto-apply + report/count integrity (20-22). **Rubric grader over the prose map, NOT a model-quality benchmark** — never measures whether an escalation improves output, only policy-shape conformance.
- `test_scenarios.json` — 3 embedded scenarios: canonical 12-skill (12/3/3/6), fictional zero-seed "forgeworks" (6/0/0/6, resists escalation-invention), "atlas" stress of up-corrections b+c (5/1/2/2).
- `evals/golden/` — `good.md` (22/22) + 5 `bad_*.md` engineered to fail specific assertions (manifest.json: bad_fanout_as_fable fails [8,9,13,14,15]; bad_seed_trio_demoted [8,12,15]; bad_synthesis_fanned_out [16,17]; bad_triggerless_conditional [18]; bad_autoapplied_claim [20,21]). Grader-calibration corpus.

**tier-offload pytest contract tests** (no evals.json):
- `test_sample_config_loads.py` — producer→consumer round-trip: loads `sample-offload-config.json`, constructs real `switchboard.config.SwitchboardConfig`. Asserts top-level keys ⊆ imported `ALLOWED_TOP_LEVEL_FIELDS`; 6 live slices enabled + `build-step-style` disabled (l.106-129); malformed `"bad task/class"` key raises `ConfigError` (l.132-142); `effort` block round-trips. Config-shape validity, not routing quality.
- `test_taxonomy_osot.py` — one-source-of-truth guard: reference owns the 7-row table; neither SKILL.md re-inlines a row; both cite the reference; no stale `offload-scan` name; self-test that the guard catches re-inlining.

Neither harness benchmarks local-model judging accuracy or Fable escalation quality lift. **Policy-shape conformance + config-load safety only.**

## 5. switchboard config shape (what "benchmark says local-safe" must look like)

Contract: `switchboard/switchboard/config.py`, dataclass `SwitchboardConfig` (l.72-102), consumed by `local_judge` via `load_config`.

- **Fields (l.95-102):** `less_token_mode: bool=True` (global on/off — OFF → `local_judge` short-circuits to defer, l.5-6), `enabled_call_sites: dict[str, bool|str]`, `effort: dict[str,str]`, `base_url`, `model` (`DEFAULT_MODEL="general-35b"`), `cold_timeout_s`, `warm_timeout_s`, `max_tokens`.
- **`enabled_call_sites` semantics (l.76-79):** `True` = offload at default model; `False` = defer to Claude; model-name string = offload pinned to that model. Unknown task_class = disabled-by-default (`is_call_site_enabled`, l.172-184).
- **Name-safety:** every task_class key AND model-name value must match `_SAFE_NAME_RE = ^[A-Za-z0-9._\-]+$` (l.47, l.61-69). Values bool or model-name str only, else `ConfigError` (l.117-126).
- **`effort` (l.85-92 + l.49-54):** task_class → `low|medium|high|xhigh|max` (`ALLOWED_EFFORT_TIERS`). Stored + validated + round-tripped but **acted on by nothing** — Claude-side hint; `local_judge` ignores it.
- **Resolution order (`load_config`, l.230-284):** explicit arg → `$SWITCHBOARD_CONFIG` → `~/.switchboard/config.json` → inert defaults.

A "benchmark says this slice is local-safe" claim plugs in as ONE entry: `"<skill>-<slice>": true` (or model-name string), subject to (a) name-safety regex, (b) gate-precondition invariant (directly-gating slice stays `false` until a Claude final judge is inserted — offload SKILL.md l.80, l.104), (c) `less_token_mode`. **CRITICAL GAP: the config carries NO field for benchmark evidence, confidence, accuracy threshold, or provenance** — decision entirely upstream; config records only the bare boolean. Switchboard consumes no benchmark data.

## 6. Produced artifacts (committed truth; samples in skill dirs are older shapes)

- `C:\Users\abero\dev\offload-scan-out\inventory.md` + `offload-config.json` — 2026-07-12 scan, 43 skills, 8 local-safe slices (7 live, 1 gated). Gated: `skill-eval-setup-grader` (`false`, l.28); `build-step-style` flipped `true` (config l.8) — inventory found "Gate precondition SATISFIED in current skill text," flags tier-offload's own background parenthetical as stale.
- `C:\Users\abero\dev\tier-escalate-out\escalation-map.md` — 2026-07-12 scan: 2 FABLE-SEED (plan-init, bug-fix/user-debug) · 4 CONDITIONAL (plan-feature, plan-merge, review-deep, review-memories/memory-distill) · 37 STAY. deep-research is NOT a SKILL.md — its seed phases live in `deep-research-pinned` workflow (§1 l.25-29). Headline: **arms-pinned audit found 16 skills dispatch unpinned arms, zero fully pinned** at scan time (§4). §5 records demotions (raw agents said 18 FABLE-SEED). Committed maps are the "monthly diff-baseline — do not retro-edit."
