# Workspace benchmark/eval prior art survey (for measure-twice planning)
(Recon by read-only Explore agent, 2026-07-15, persisted by orchestrator.)

## Path corrections
- VF runner is `void_furnace\src\void_furnace\benchmark\runner.py` (not `benchmark\runner.py` at repo root). Siblings: `judge.py`, `anchors.py`, `cli.py`, `report.py`; tests under `void_furnace\tests\test_benchmark\`.
- switchboard package dir: `C:\Users\abero\dev\switchboard\switchboard\`; worktree copy at `C:\Users\abero\worktree_switchboard-endpoint-launcher\`.
- There is NO `harness.resolve_assignment` in switchboard. Routing/gating: `config.py` (`offload_allowed`, `model_for`). Bench engine: `harness.py`/`bakeoff.py`/`certify.py`.

## AREA 1 — void_furnace benchmark infra (`src/void_furnace/benchmark/`) — EXTRACT-PATTERN

**runner.py** — `run(role, models, judges, suite, *, model_client_factory=None, judge_client_factory=None, now=None, suite_path="<in-memory>", cwd=None, timeout_s=INVOKE_TIMEOUT_S) -> BenchmarkReport` (runner.py:720). Sweeps every (model × prompt) cell. Generalizable invariants:
- **no_diff force-scored 0 without judge call** (runner.py:78, 904-911): `NO_DIFF_MARKER = "[no_diff: coder wrote zero files]"`; code-expected coder row writing zero files force-scored mean-0 BEFORE any judge call. (measurement-validity §"Score the production artifact" exemplar.)
- **Production-path prompt assembly** (`_build_prompt`, runner.py:456-549): dispatches each role to the SAME production builder the live factory uses — `triage.build_prompt`, `coder.build_prompt`, `holdout.build_critic_prompt`, `retro.build_prompt`, `replay.build_prompt_from_template`. Suite supplies only DATA.
- **Three fail-loud guards** (abort, not warn): `EmptyPromptTemplateError` (runner.py:153); `HarnessFallbackError` (runner.py:189, raised 764) when `harness.current().version == FALLBACK_VERSION`; `JudgeParseFailureError` (runner.py:169, raised 996) with `JUDGE_PARSE_FAIL_RATE_THRESHOLD = 0.5`, fired **per-judge** so one broken judge can't hide behind healthy peers (runner.py:958-1004).
- **Deterministic verdict accuracy for verdict roles** (`VERDICT_ROLES`, runner.py:118): triage + validator scored by production `parse_verdict` via `_score_verdict -> (parsed_verdict, parse_ok, verdict_correct)` (runner.py:676-717); LLM judge demoted to secondary signal.
- **Diff-from-sandbox** (`_build_diff_from_sandbox`, runner.py:588-673): coder writes into tempfile sandbox; files ARE the diff; `DIFF_PER_FILE_CAP_BYTES=64KB`, `DIFF_TOTAL_CAP_BYTES=200KB` with truncation markers.

**judge.py** — k-sampling/median doctrine: `JUDGE_SAMPLE_K = 3` (odd → median is a real sample); each judge sampled k×, `JudgeScore` = median of successfully-parsed samples. `build_judge_prompt` emits `SCORE: <n>` / `RATIONALE:` two-line shape; `parse_judge_response -> (score, rationale) | None`, clamps 0-10. Parse-fail vs invoke-error distinct; `per_judge_parse_stats` `(spec, n_fail, n_attempts)` feeds per-judge gate.

**anchors.py** — 5 frozen `AnchorPair` (triage/coder/validator/retro/replay), `kind ∈ {verdict, diff, rubric}`, divergent good/garbage in the scored dimension. `tests/test_benchmark/test_anchors.py` ordering gate asserts `score(good) > score(garbage)` — permanent no-live-model calibration layer.

Code is VF-coupled (imports harness, holdout, replay, workflows.*, models.factory). Port invariants, not module.

## AREA 2 — readiness_bench (`scripts/readiness_bench/`) — EXTRACT-PATTERN

Files: `labeled_set.py`, `prompt.py`, `scorer.py`, `judges.py`, `verdict.py`, `__main__.py`.
- **4-archetype anchor calibration**: `deterministic_judge(case, *, strict_mypy_target=True)` (judges.py:69) = thin adapter over PRODUCTION classifier `void_furnace.readiness.deterministic_judge` (promoted out of bench → one source of truth); scores 4/4 on canonical key. Rules: Streamlit→INTRACTABLE, dependency→BLOCKED, strict-mypy-gap→ENRICHABLE, else TRACTABLE.
- **Three judge paths**, shared DI seam `model_client_factory` (offline-testable): Path C deterministic (0 model calls), Path B `local_llm_judge` (LlamaCppAdapter, coder-30b/general-instruct), Path A `claude_judge` (anthropic_cli OAuth, ceiling). Both LLM paths **fail-toward-safe**.
- **verdict.py**: `parse_readiness_verdict(text, *, safe_default_bucket=SAFE_DEFAULT_BUCKET)` — lenient JSON extraction (brace-walker), strict validation, `SAFE_DEFAULT_BUCKET = "INTRACTABLE"` (fail toward NOT waving through). Schema promoted to production `void_furnace.readiness.verdict`.
- **scorer.py**: `score(cases, verdicts) -> ScoreReport` — per-case `correct`, `accuracy` fraction, `confusion` matrix `(predicted, ground_truth) -> count`; `format_report` renders table.

## AREA 3 — replay spike (`scripts/open_model_config_spike/replay.py`) — EXTRACT-PATTERN

Standalone offline harness, 4 subcommands: `build-dataset`, `replay`, `score`, `merge-baselines`.
- Replays EXACT cached production-rendered prompts (`validator_prompt.txt` / `prompts/triage.txt`), parses via production `parse_verdict`. `_builder_framing_markers()` fingerprints the 3 longest static template lines to prove a cached prompt is genuine builder output (`--verify-builder`).
- `_unwrap_result` reproduces production `--output-format json` envelope unwrap.
- **Three metrics** (score, replay.py:464): (1) over-pass rate on gate-RED critic PRs, bar = 0; (2) parse-success ≥95%; (3) PASS-class agreement with baseline ≥70%. `build_dataset` HALTs (SystemExit) if <25 critic runs or <5 gate-RED — dataset-floor guard.

## AREA 4 — skill-evals framework — AVOID-DUPLICATING (integrate)

Layout `.claude/skills/<name>/evals/`: `evals.json`, `test_scenarios.json`, `golden/` (good.md + bad_<slug>.md + manifest.json), `iteration_log.md`, `results.tsv`. Generator: `skill-eval-setup` skill. Contract: **discrimination assertions** (name the defect that grades FALSE), ≥40% discrimination quota, 4 hardest patterns covered. Golden corpus verification gate: bad accepted only if `bad_score < good_score` strictly (regen budget 3, else INERT).

evals.json schema: `{ skill, version, description, passing_threshold: "(N-2)/N", categories: [{name, evals: [{id, statement, source: "SKILL.md, lines NN-MM", defect_type, result: null}]}] }`; `defect_type ∈ {structural, format, quantity, content, anti-pattern, required-content, internal-inconsistency, conditional-behavior, n/a-sanity, n/a-coverage}`.
test_scenarios.json: `{ skill, version, scenarios: [{id, name, description, context, expected_assertions: [ids]}] }`.
manifest v2: `{manifest_version:2, skill, generated_at, good_source, verification_summary:{accepted,inert,total}, bads:[{file, defect_type, assertion_id, verified_fails, regen_attempts}]}`.

**Grading contract**: `_shared/grader_prompt.py` → `build_grader_prompt(*, scenario_id, rendered_output, evals_json, expected_assertions)` — ONE canonical strict-grader prompt. Output: JSON-only `{"verdicts": {"<id>": {"verdict": true|false, "reason": "..."}}}`. **VACUOUS-TRUE deterministic lookup**: assertion ID not in `expected_assertions` grades true without examining output. Uses `.replace()` not `.format()`. Plus `_shared/score-skill.md`, `score_skill_absolute.py` (majority-vote + vacuous-TRUE), `score_skill_composite.py` (composite + `verify_goldens`), `score_skill.workflow.js` (no-self-grade via depth-1 Workflow).

## AREA 5 — judge-core doctrine (`.claude/skills/_shared/judge-core.md`) — AVOID-DUPLICATING (reference + conform)

Canonical doctrine for every verdict/score/recommendation skill. Skills MUST reference, not restate.
- Judge→Advisor spectrum (§1); archetypes (§2: executable-verifier / pointwise / pairwise-swap-and-tie / rubric / critique-then-score / jury / adversarial), orthogonal to dimensions (§3).
- **Honesty invariants** (§5): (1) producer never grades itself; (2) evidence on every verdict; (3) mechanical checks gate first; (4) cross-check ground truth; (5) low-confidence→escalate never auto-pass; (6) deterministic aggregation typed for shape — categorical→majority-vote/ties-escalate, graded→mean+std, ranked→rank-fusion, heterogeneous→escalate raw to one consolidating gate; no reliability-weighted EM on small correlated panels; (7) weak/local models never gate (cascade: local advisory-first, disagreement escalates).
- **Verdict-contract spine** (§5.6): extract-JSON-robustly → validate-axes-present → coerce-types → deterministic-aggregate; fixed verdict enum; parse-failure → None (drop, don't crash). Four separate-runtime conformers, document-don't-merge: goblin `src/goblin/grade.py` (median-of-N + discrimination guard), workspace `_shared` graders, toybox `src/toybox/ai/rubric.py` (clamp-[1,5]), brickomancer `tests/harness/judge.py`.
- **Calibration** (§7): golden `(input, expected_verdict)` set; agreement metrics (Cohen κ, Krippendorff α, Gwet AC2); Discrimination guard (good out-scores every bad by margin, else rubric parked); mechanized gate = `_shared/calibrate_judge.py`.

## AREA 6 — calibrate_judge.py + build_step_verdict.py — REUSE-AS-IS

`_shared/calibrate_judge.py`: pure-Python zero-live-LLM CI gate (`mode="ci"`). Replays recorded judge snapshot through FRESHNESS (`generated_at` ≤ `FRESHNESS_MAX_AGE_DAYS`), DISCRIMINATION (`verify_goldens`), AGREEMENT (recorded_verdicts vs gold `verdicts.jsonl`; agreement==1.0 on curated seed). Artifacts under `evals/golden/`: `verdicts.jsonl` (enum mirrors review-deep `PASS|NEEDS-WORK|NO-EVIDENCE|FAILED|SKIPPED|NEEDS-CLARIFICATION`), `recorded_scores.json` (`generated_at, scorer, scores, recorded_verdicts`). Fails closed. `mode="full"` (live kappa) raises NotImplementedError (Phase-3 stub). review-deep = first wired judge.

`_shared/build_step_verdict.py`: `classify_verdict(path) -> "ADVANCE"|"BLOCKED"` (default-deny, never raises); schema `{timestamp, result: PASS|NEEDS-WORK|DEFERRED-TO-UAT, halt: POST_MERGE_HALT|SHIP_GATE_HALT|null, summary}`; `ADVANCING_RESULTS = {PASS, DEFERRED-TO-UAT}`; BOM-tolerant read.

## AREA 7 — switchboard — REUSE-AS-IS engine / AVOID-DUPLICATING math

CLAUDE.md: local-LLM offload core; routes cheap judge/grader calls to local OpenAI-compatible model (`general-35b` via llama-swap on `localhost:8080`, WSL2); defers to Claude on any failure. Stdlib-only, inert until config deployed. Gotcha: general-35b is a reasoning model — max_tokens floor 600/default 800, set 2000+ or verdict truncates; read `choices[0].message.content`, ignore `reasoning_content`.

- **client.py** — `local_judge(prompt, *, config=None, cold=False, timeout=None, task_class=None, metrics=None) -> JudgeResult` where `JudgeResult = Verdict | Defer`. `Verdict` = `{verdict: "pass"|"flag", reason, _raw, _elapsed_s}`; `Defer` = `{defer: True, reason, reason_class, _elapsed_s}`. 7 defer reason_classes (`unreachable, timeout, os_error, non_json_body, bad_envelope, truncated, bad_verdict`) + `disabled, config_error, input_error`. Lenient parse (`_extract_json_object`: strip ```json fences, first balanced `{}`), strict validate (`_valid_verdict`: verdict ∈ {pass,flag} AND non-empty reason). Short-circuits to defer WITHOUT network when `not cfg.offload_allowed(task_class)`.
- **harness.py** — `run_shadow(cases, *, config=None, kill_threshold=DEFAULT_KILL_THRESHOLD, task_class=None, metrics=None) -> ShadowReport`. Keystone: `aggregate_agreement(*, verdicted, agreements, kill_threshold=0.20) -> AgreementVerdict` — ONE source of truth for (a) DEFER excluded from agreement denominator; (b) kill iff `disagreement_rate > kill_threshold` strictly. `DEFAULT_KILL_THRESHOLD = 0.20`. `ShadowCase{prompt, expected: "pass"|"flag", label, cold}`.
- **bakeoff.py** — `run_bakeoff(suite) -> BakeoffReport`; `load_suite`, `score_runner`, `run_live_local_runner` (delegates to run_shadow). **Ports the SHAPE of VF's BenchmarkReport/BenchmarkRow** (per-(runner,case) matrix + per-runner aggregate + `render` markdown + `render_jsonl` NDJSON pure functions) — "port the shape, NOT the code." Recorded mode = deterministic regression gate; suites at `switchboard/suites/{effort-vs-model,review-deep-style}.json`.
- **certify.py** — `certify_slice(report, runner) -> CertVerdict`. Certified iff `(not kill) AND agreement_rate is not None` (all-defer proves nothing). DO-NOT-ENABLE is a valid shippable outcome. On certify emits minimal `_enable_config_for(slice)` with `RECOMMENDED_MAX_TOKENS=2000`.

## AREA 8 — other eval dirs
- Skill-eval fleet across `.claude/skills/*/evals` + Alpha4Gate, pinchy_mchire, pokemon-go-tools, toybox, void_furnace, primatives/autoresearch-skills, career-ops. Shared framework — avoid duplicating.
- 4 spine conformers (goblin grade.py, toybox rubric.py, brickomancer judge.py, workspace _shared) — conform, don't merge (goblin's plan forbids importing shared scorer).
- Domain-specific (n-a): agora/benchmarks, always-best-estimates/backend/abe/eval, toybox/tests/fixtures/eval.

## Net guidance for measure-twice
1. Reference judge-core.md doctrine; conform to §5.6 verdict-contract spine.
2. Reuse as-is: `_shared` grading + calibration (`grader_prompt.py`, `score_skill_*.py`, `calibrate_judge.py`, `build_step_verdict.py`) + switchboard `aggregate_agreement`/bakeoff/certify + `local_judge`.
3. Extract-pattern (port, don't copy): VF invariants — no_diff force-0, production-path assembly, 3 fail-loud guards, k≥3 median judging, frozen good/garbage anchors.
4. Do not rebuild the skill-eval schema pair, golden corpus mechanics, or the four project scorers — integrate.
