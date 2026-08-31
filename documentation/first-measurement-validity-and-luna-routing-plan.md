# First-Measurement Validity Gates and Luna Routing

**Status:** WRAPPED (2026-08-30) — 0/8 steps complete; redline publication v2 accepted;
ready for `repo-sync`; not build-ready until the issue and working-tree preconditions pass

**Repo-sync phase identifier:** `first-measurement-validity-and-luna-routing` (must be passed
explicitly; it cannot be inferred from this filename)

## 1. What This Is

Proposal: `documentation/first-measurement-validity-and-luna-routing-plan-proposal.html`

Turn the first real `measure-twice` observation into honest next work without promoting a
contaminated, single-sample result into routing evidence. Instrument A must fail closed on provider
selection, execute Claude in a sealed prompt-only context, expose identity and score-validity
evidence, and replicate a frozen v1 calibration before Step 16 can touch the ledger. In parallel,
the in-progress Instrument B containment repair must be owned, reviewed, and soaked before the
already-preregistered Luna-versus-Sonnet coding-agent rail resumes at existing Step 27.

## 2. Status and Existing Evidence

- Instrument A Steps 1-12 and Instrument B Steps 25-26 are shipped. Canonical Steps 13-17 and
  27-55 remain the owners of their existing deliverables; this plan adds gates around them.
- `run_20260830T071944Z_948385` is reproducible as stored, but it is **diagnostic-only and
  incomparable to sealed runs**. Its Claude subprocess inherited the repository working directory,
  ambient environment, customizations, tools, and session behavior. A conservative lexical audit
  identifies 23/200 responses that refer to live workspace state; row 5 explicitly begins
  "Looking at the diff in `measure_twice/agent_bench/process.py`" for a self-contained item.
- The official scores remain 50.0 for Haiku and 81.0 for Sonnet. The suite's larger constant-label
  control is 52.0, parse success is 69/100 and 90/100, and the run has one sample per cell. These
  facts bracket the defect; they do not authorize the post-hoc first-label diagnostic as a score.
- The user-owned zombie-aware process-identity candidate passed 8/8 exploratory WSL-ext4 gates on
  2026-08-30, with 130 selected Linux tests and zero skips per invocation. That is encouraging
  pre-plan evidence, not Step 63 acceptance evidence: the gate must be repeated after Step 62 owns
  and reviews the exact change.
- `profiles/agent-models-candidates.json` already preregisters `codex-luna` through provider
  `codex-cli`, requested model `gpt-5.6-luna`, effort `high`, and execution profile
  `agent-execution-v1`. Existing Steps 27-55 are the sole implementation and measurement path for
  that comparison.

The step numbers below are append-only identifiers, not a demand to execute both instruments in
numeric order. The dependency topology is:

```text
Instrument A: 56 -> 57 -> 58 -> existing 13 -> existing 14 -> 59 -> 60 -> 61 -> existing 16
                                                 |                               |
                                                 +-> existing 15 ----------------+-> existing 17

Instrument B: 62 -> 63 -> existing 27 -> ... -> existing 55
```

Steps 56 and 62 may start in parallel. Existing Step 16 remains blocked until Step 61 passes;
existing Step 27 remains blocked until Step 63 passes.

This file is a cross-plan coordinator, not one numerically contiguous build phase. Automation must
select the owning file and step explicitly; it must never run `--resume 59` merely because Step 58
finished. The only permitted transitions are:

1. build new Steps 56-57, observe new Step 58, then run canonical `plan.md` Steps 13 and 14;
2. preflight the Step 14 v1 suite and calibration artifacts, build new Steps 59-60, observe new Step 61,
   then allow canonical Step 16 (with Step 15 still independently available after Step 14);
3. build new Step 62, observe new Step 63, verify its machine-readable receipt, then allow
   `documentation/coding-agent-benchmark-plan.md` Step 27.

The Step 60 and Step 63 production scripts enforce their artifact/hash prerequisites. A missing
external artifact is a documented halt, not permission to skip ahead. `repo-sync` must cross-link
the new blocking issues to the already-existing Step 13/14/16/27 issues without rewriting those
canonical step bodies.

## 3. Non-goals

- Do not add Luna, Haiku, or any other model to `DEFAULT_ROSTER`, and do not add Luna to Instrument
  A in this plan.
- Do not change `tier-judging-v0`, the prompt-response `Suite`, `RunRow`, the official verdict
  parser, the order-independent conflict rule, or the rule that a parse failure records an official
  zero.
- Do not edit or renumber pending canonical Steps 13-24 or 27-55. In particular, do not duplicate
  the coding-agent evaluator, provider adapters, qualification, pilot, or decision-grade v1 run.
- Do not build or populate the blocked Steps 18-24 operations surfaces, and do not expose the first
  run there.
- Do not salvage the first run by deleting detected rows, quote 64/87 as scores, pool Instrument A
  and B scores, or write any ledger claim from a single or unsealed run.
- Do not turn this operator-only project into a service, scheduler, dashboard, or interactive
  mid-run approval workflow.

## 4. Preregistered Numeric Claims

These sentences are frozen before the corresponding observation. A script must place the exact
sentence in every new run manifest or evidence header before it makes a call or starts the repeated
gate; missing or fallback preregistration aborts.

- **External Step 13 gate:** "On a sealed tier-judging-v0 run of the explicit five-model roster at
  one sample per cell, every cell will be terminal, every arm will carry concrete provider identity,
  all five official scores will be strictly inside (0,100), and at least two official scores will
  differ; this is calibration only and creates no ledger update."
- **Step 58:** "All three live Claude canaries (haiku, sonnet, and opus) will return the requested
  token without reproducing any unique repository, environment, customization, or session sentinel,
  and every arm will record provider, requested alias, concrete resolved identity, Claude CLI path
  and version, and the same context-profile hash; any sentinel or unresolved identity fails the
  3/3 qualification and blocks Step 13."
- **Step 59:** "Through the production scorer, the frozen validity fixtures will score known-good
  at 100, known-garbage and no-verdict at 0, and the constant-pass and constant-flag controls at
  their exact suite label shares; an arm whose official score does not beat the larger constant
  control will be labeled NOT_ROUTING_ELIGIBLE without changing that official score."
- **Step 61:** "Across three independent sealed-context tier-judging-v1 runs of the frozen
  five-model roster at one sample per cell, a pre-run evidence header will pin one scorer-policy
  hash and a call budget equal to the frozen roster-by-item cell count, and every manifest will pin
  identical suite, execution-context, and provider-profile hashes; every arm's official score,
  parse-success rate, and larger-constant-control delta will be reported separately, every arm must
  be ROUTING_ELIGIBLE in all three runs, and each of the ten unordered pair signs (with an exact tie
  as its own sign) and each alias's concrete resolved-identity set must match across all three while
  every arm's official-score and parse-success max-minus-min range stays at or below 15 percentage
  points, or Step 16 stays blocked; this is calibration only and creates no ledger update."
- **Step 63:** "The reviewed containment repair will pass 8/8 independent WSL-ext4 gate
  invocations with zero selected skips and no live-identity or retained-FD escape; any lower pass
  rate returns the work to Step 62 and blocks Step 27."

Step 61 is calibration, never Step 16 evidence. Only after it passes may Step 16 freeze its three
ledger claim sentences and thresholds, then test them on fresh run IDs that share no Step 61 rows;
this is a separate confirmatory design, so calibration observations cannot become post-look ledger
evidence.

## 5. Impact Analysis

| Existing surface | Planned impact | Verified downstream consumers and compatibility obligation |
|---|---|---|
| `measure_twice/adapters/claude_cli.py` | Run the response-only Claude call from a unique clean temporary cwd with an allowlisted environment, no tools, no customizations, and no session persistence; pin executable path/version and preserve subscription OAuth. | Called by `measure_twice/runner.py` and `measure_twice/scoring/judge.py`; tests inject `RunnerFactory` from `tests/conftest.py` and exercise it in `tests/test_adapters.py`, `tests/test_runner.py`, and `tests/test_report.py`. The runner seam must expose argv, cwd, and environment to tests without bypassing the production builder. |
| `measure_twice/config.py` and `measure_twice/runner.py` | Replace the closed Claude-alias/open-local default with one validated alias-to-provider/requested-model profile. Reject unknown or unbound roster names before a run directory or network call. | `measure_twice/cli.py` resolves config; `tests/agent_bench/test_models.py` asserts Instrument B is independent of `CLAUDE_ALIASES`; all `DEFAULT_ROSTER` references and CLI override tests require grep evidence. Instrument B continues to dispatch only through `ModelSpec.provider`. |
| Instrument A `manifest.json` | Add one versioned execution receipt for new runs: profile/context hashes, provider bindings, requested identities, executable path/version, and sealing mode. Keep `RunRow` unchanged. | `_read_manifest`, resume validation, `measure_twice/report.py`, the current `measure_twice/report_html.py`, `tests/test_runner.py::MANIFEST_KEYS`, `tests/test_report.py`, and `tests/test_report_html.py` consume manifest shape. Old manifests remain readable and visibly legacy-unsealed; a legacy run with pending Claude cells cannot be resumed into a sealed execution. |
| `measure_twice/adapters/base.py::resolved_model_of` and `model_id_resolved` | Stop treating fallback-to-requested as proof in both Claude and local adapters. Preserve concrete provider-returned identities; encode missing identity as one explicit unresolved sentinel and make the arm ineligible. | `measure_twice/adapters/{claude_cli,local}.py` call the shared resolver; all runner result branches write the value; the new Markdown/JSONL/HTML identity summaries read it. Consumer grep must cover `measure_twice`, `tests`, `documentation`, and stored-fixture builders before landing. |
| Existing Step 14 `measure_twice/analyze/calibrate.py` and `mt calibrate` | After Step 14 exists, add a single owned, hashed validity-policy block with production-scorer controls, parse-health diagnostics, and a fail-closed routing-eligibility verdict. | Step 15 profile output and Step 16 ledger decisions are downstream. The current terminal and HTML report paths may display a diagnostic legacy warning, but this step creates no operations surface and no alternate scorer. A consumer grep for calibration result keys is attached before landing. |
| `measure_twice/agent_bench/process.py` | Finish the existing zombie-aware identity-settle repair without weakening the live-process escape assertion or retained tmpfs-root FD invariant. | `tests/agent_bench/test_process.py`, `measure_twice/agent_bench/isolation.py`, and existing Step 27's evaluator contract depend on process cleanup and retained scratch identity. No provider or evaluator code is added here. |
| `profiles/agent-models-candidates.json` and coding-agent Steps 27-55 | No schema or content change. Resume this existing Luna path only after Step 63. | `measure_twice/agent_bench/models.py` already dispatches by provider; Steps 28-31 own adapters and live qualification, Step 35 owns the real smoke, Step 41 owns non-ranking pilot calibration, and Step 54 owns the decision-grade Luna/Sonnet number. |

The Step 56 review evidence must attach the output of:

```powershell
rg -n "CLAUDE_ALIASES|DEFAULT_ROSTER|model_id_resolved|MANIFEST_KEYS|manifest\.get|manifest\[|_read_manifest|_write_manifest" measure_twice tests documentation
```

The Step 59 review evidence must separately attach the output of:

```powershell
rg -n "calibrat|acceptance_band|saturation|routing_eligib|parse_success|suite_score" measure_twice tests docs plan.md documentation
```

## 6. Design Decisions

### 6.1 Quarantine the whole first run

The 23 lexical hits are a conservative lower bound, not a safe deletion list. Every one of the 200
calls had the same uncontrolled context capability, so the run may be used only as a forensic
regression fixture. Its correct offline reproduction proves storage/scoring integrity, not that the
responses measured the intended prompt-only model behavior.

### 6.2 Retain the registered Instrument A construct and parser

Instrument A measures the right discrete answer in any reasonable, unambiguous surface form. It
does not measure bare-token obedience, and it does not guess intent when both labels occur. Official
parse failures therefore remain recorded zeroes. The scorecard decomposes parse health and
parsed-only accuracy as diagnostics while retaining the official end-to-end score. Constant-label
controls bracket whether that score can support routing; they do not rescore the model.

### 6.3 Make execution and identity fail closed

A single validated execution profile owns each allowed Instrument A alias, provider, and requested
model. The committed v1 profile covers the existing Instrument A roster; a non-profile roster entry
must carry an explicit provider binding or fail before filesystem mutation. New Claude executions
use the locally supported subscription-compatible sealing flags (`--safe-mode`, `--tools ""`,
`--disable-slash-commands`, `--no-chrome`, and `--no-session-persistence`) from a new empty temporary
cwd and an environment allowlist. Step 56 must doctor the installed CLI and pin the exact supported
argv rather than assuming flags forever. It must not use `--bare`, because the installed CLI says
that mode does not read OAuth or the keychain.

The receipt distinguishes requested identity from provider-returned identity. A missing concrete
identity is unresolved evidence, never silently replaced by the requested alias. Reports list the
set of resolved values per arm and state whether each came from provider evidence or is unresolved.

### 6.4 Preserve old data without mixing execution contracts

The execution receipt is additive for new manifests. Existing run directories remain readable and
offline-rescorable, but reports mark a missing receipt `LEGACY_UNSEALED` and
`NOT_ROUTING_ELIGIBLE`. Resume fails before append if a legacy run has pending Claude cells, so no
run can combine unsealed and sealed rows. Suite hashes and `RunRow` stay unchanged.

### 6.5 Target Luna with Instrument B

Luna is a coding-agent product subject here, so Instrument B matches the intended construct: native
agent CLI, tool loop, sandboxed workspace, produced patch, and held-out evaluator. Its committed
candidate profile already fixes provider, model, effort, and execution profile. Instrument A would
answer the separate question "can this model judge a self-contained scenario?" and would require a
new Codex response-only adapter and preregistration; that is not necessary for the routing decision
and is excluded from this plan. If pursued later, its results stay separate from Instrument B.

Keep Luna effort at `high` for the registered v1 comparison. Testing another effort requires a new
hashed profile and preregistered factorial; it is not an ad-hoc runtime override.

### 6.6 Treat containment rate as a post-review gate

The exploratory 8/8 result tests the current user-owned candidate but does not replace code review,
the regression anchor, or a post-Step-62 soak. Step 63 publishes all eight exits and the rate. A
single failure is evidence of nondeterminism and returns to Step 62; results are not averaged into a
pass.

## 7. Build Steps

Every code step is autonomous and passes `uv build`, `uv run pytest -q`,
`uv run ruff check .`, `uv run ruff format --check .`, and
`uv run mypy --strict measure_twice`. A code step touching `agent_bench` also runs the real
PowerShell-launched WSL-ext4 gate. Wait steps execute an already-built production path and may write
only run/evidence artifacts; they add no code and contain no confirmation prompt.

### Step 56: Seal and fail-close Instrument A execution

- **Problem:** Instrument A currently chooses Claude from a closed alias set and silently routes
  every other name to the local endpoint, while Claude inherits the live repository, ambient
  environment, customizations, tools, and session behavior. Build one validated alias-to-provider
  profile, a sealed Claude subprocess builder, explicit unresolved-identity evidence for both
  adapters, and an additive execution receipt; reject unknown providers before run creation and
  reject any legacy resume that would mix unsealed and sealed Claude rows.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/adapters/base.py`, `measure_twice/adapters/claude_cli.py`,
  `measure_twice/adapters/local.py`, `measure_twice/config.py`,
  `measure_twice/model_sweep_execution.py`, `measure_twice/runner.py`,
  `profiles/model-sweep-execution-v1.json`, `tests/conftest.py`, `tests/test_adapters.py`,
  `tests/test_config.py`, `tests/test_model_sweep_execution.py`, `tests/test_runner.py`
- **Produces:** a versioned Instrument A execution profile and hash; sealed Claude argv/cwd/env;
  fail-closed provider dispatch; additive manifest execution receipts; one unresolved-identity
  sentinel shared by local and Claude; legacy-resume rejection; production-entry integration tests
- **Done when:** a fake Claude executable launched through the real `mt run` subprocess entry point
  observes the exact executable path, supported flags, unique clean temporary cwd, environment
  allowlist, disabled tools/customizations/session persistence, provider binding, CLI version, and
  receipt hash; unknown/unbound names fail before run-dir creation; absent provider identity records
  unresolved rather than the requested alias; old manifests remain readable but pending legacy
  Claude cells cannot append; all Step 56 consumer-grep hits are dispositioned; and
  `"uv run pytest -q tests/test_model_sweep_execution.py tests/test_adapters.py tests/test_config.py tests/test_runner.py"`
  exits 0 through the production builder
- **Depends on:** 7, 12 (shipped)
- **Status:** NOT STARTED

### Step 57: Report the seal and build its live qualification path

- **Problem:** Step 56 can execute and store the seal, but current reports ignore
  `model_id_resolved` and cannot distinguish concrete provider evidence from legacy or unresolved
  identity. Expose the receipt and per-alias resolved-identity sets in every production report, mark
  old runs visibly ineligible without changing their score, and build one hostile-context canary
  suite plus an autonomous PowerShell qualification/verify-only wrapper for Step 58.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/cli.py`, `measure_twice/report.py`,
  `measure_twice/report_html.py`, `measure_twice/report_template.html`,
  `suites/model-sweep-context-canary-v1.json`, `scripts/qualify-model-sweep-context.ps1`,
  `tests/test_cli.py`, `tests/test_report.py`, `tests/test_report_html.py`
- **Produces:** Markdown, JSONL, and HTML execution/identity evidence; `LEGACY_UNSEALED` and
  unresolved warnings; a three-alias hostile-context suite; a wrapper that writes a concrete
  qualification index before/after calls and can later fail closed in `-VerifyOnly` mode
- **Done when:** sealed, unresolved, and legacy fixtures pass through the real `mt report` entry
  points; official legacy scores remain unchanged; reports name provider/requested/resolved values
  and receipt hashes; a fake-live integration proves the wrapper plants unique cwd/environment/
  customization sentinels, embeds the exact preregistration before call 1, and rejects a sentinel or
  unresolved identity; and
  `"uv run pytest -q tests/test_cli.py tests/test_report.py tests/test_report_html.py -k 'execution or identity or legacy or qualification'"`
  exits 0
- **Depends on:** 56
- **Status:** NOT STARTED

### Step 58: Live-qualify the sealed Instrument A context

- **Problem:** Offline builder tests cannot prove that the installed, authenticated Claude CLI
  honors the sealing contract. Exercise Haiku, Sonnet, and Opus with unique hostile repository,
  environment, customization, and session sentinels before the full Step 13 sweep; require concrete
  provider identity and one execution-context hash for every arm.
- **Type:** wait
- **Issue:** #
- **Flags:** --reviewers auto
- **Files:** `data/qualification/model-sweep-context-v1/index.json`,
  `data/qualification/model-sweep-context-v1/runs/`,
  `docs/methodology/model-sweep-context-qualification.md`
- **Produces:** three real Claude canary rows, their immutable suite/execution receipts, a 3/3 or
  failed qualification verdict, and a findings note with no model-quality claim
- **Done when:** the Step 58 preregistration sentence is embedded before the first call, the script
  records all three terminal rows and exact requested/provider/resolved/CLI/context evidence, no
  sentinel appears, no identity is unresolved, and
  `"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\qualify-model-sweep-context.ps1 -Profile .\profiles\model-sweep-execution-v1.json -Out .\data\qualification\model-sweep-context-v1 -Preregister 'All three live Claude canaries (haiku, sonnet, and opus) will return the requested token without reproducing any unique repository, environment, customization, or session sentinel, and every arm will record provider, requested alias, concrete resolved identity, Claude CLI path and version, and the same context-profile hash; any sentinel or unresolved identity fails the 3/3 qualification and blocks Step 13.'"`
  exits 0 with `qualification=PASS passed=3 total=3`; otherwise Step 13 stays blocked and the evidence
  returns to Step 56/57
- **Depends on:** 57
- **Status:** NOT STARTED

After Step 58 passes, execute existing Step 13 with the exact external Step 13 claim in Section 4
and an explicit config, then existing Step 14. The contaminated first run is not Step 13 input.

### Step 59: Add a suite-level validity scorecard to calibration

- **Problem:** Existing Step 14 owns discrimination, saturation, and v0-to-v1 iteration, but the
  first run shows that a headline score alone can fall below a degenerate label strategy while
  parsed answers remain informative. Extend the Step 14 production calibration result with frozen
  known-good, known-garbage, no-verdict, constant-label, parse-health, and identity/context controls.
  Keep the official scorer unchanged and fail closed on routing eligibility rather than choosing a
  post-hoc parser.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/analyze/calibrate.py`, `measure_twice/cli.py`,
  `tests/test_calibrate.py`,
  `tests/fixtures/calibration-validity/data/runs/run_20260830T071944Z_948385/`,
  `docs/methodology/07-discriminative-calibration.md`
- **Produces:** one hashed validity-policy block in calibration output; production-scorer controls;
  official score, parse-success, parsed-only diagnostic, larger-constant delta, identity/context
  status, and `ROUTING_ELIGIBLE`/`NOT_ROUTING_ELIGIBLE` per arm;
  a provenance-hashed forensic fixture containing the first run's manifest, suite snapshot, and all
  200 stored rows so an isolated worktree does not depend on ignored local data
- **Done when:** the Step 59 preregistered fixture expectations pass through the production scorer;
  the current stored run is reproducibly labeled forensic, legacy-unsealed, and not routing-eligible
  while retaining official scores 50.0/81.0 and parse failures 31/10; no first-label score or
  operations output exists; the validity-policy schema has one owner and all Step 59 consumer-grep
  hits are dispositioned; and
  `"uv run mt calibrate --run run_20260830T071944Z_948385 --out .\tests\fixtures\calibration-validity\data --validity"`
  reports `constant_pass=52.0`, `constant_flag=48.0`, and `sealed=false` without making an inference
  call
- **Depends on:** 14, 58
- **Status:** NOT STARTED

### Step 60: Build the fail-closed replication orchestrator

- **Problem:** Step 59 defines an interpretable scorecard, but three expensive runs still need one
  autonomous production path that freezes the scorer policy and budget before call 1, invokes the
  real `mt run`/`mt calibrate` entries, preserves fixed run IDs, refuses favorable reruns, and emits
  a machine-verifiable receipt for the cross-plan Step 16 gate.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `scripts/run-model-sweep-replication.ps1`,
  `tests/test_model_sweep_replication.py`, `docs/methodology/model-sweep-v1-replication-protocol.md`
- **Produces:** a three-run PowerShell orchestrator; one versioned replication-evidence schema and
  `-VerifyOnly` path; pre-run scorer-policy/profile/suite/budget receipt; immutable three-run index;
  stable eligibility/direction/range/identity verdict fields
- **Done when:** an offline fake-CLI integration proves the script refuses to start without the
  completed Step 14/59 artifacts and aborts on fallback config, missing preregistration,
  scorer/profile/suite hash drift, wrong sample count or budget, incomplete cells, unresolved
  identity, a fourth-run selection, and a mutated post-run policy; it preserves all fixed run IDs on
  every failure; `-VerifyOnly` rejects absent/stale/edited receipts; and
  `"uv run pytest -q tests/test_model_sweep_replication.py"` exits 0 without an inference call
- **Depends on:** 59
- **Status:** NOT STARTED

### Step 61: Replicate the frozen sealed v1 calibration

- **Problem:** One sample per cell and one run provide no run-to-run stability evidence. Execute
  three independent complete runs of the frozen five-model v1 roster under identical explicit
  execution and scorer policies at one sample per cell, retain every run separately, and block Step
  16 unless every arm clears its larger constant control in every run, all ten unordered pairwise
  signs (including ties) remain identical, and each arm's official-score and parse-success range is
  no more than 15 percentage points rather than averaging instability away. The threshold is frozen
  before run 1 and means that at most 15 of 100 binary cell outcomes may span the observed range; a
  failed gate cannot be repaired by selecting a fourth run.
- **Type:** wait
- **Issue:** #
- **Flags:** --reviewers auto
- **Files:** `data/runs/`, `data/reports/model-sweep-v1-replication.json`,
  `docs/methodology/model-sweep-v1-replication.md`
- **Produces:** three complete sealed v1 run stores; per-run official scores, parse-success rates,
  constant-control deltas, identity sets, and pairwise orders; cross-run ranges; one explicit
  advance/block verdict; no ledger mutation
- **Done when:** the Step 61 sentence and scorer-policy hash are written before any call; all three
  manifests have the same suite, execution-context, and provider-profile hashes; the evidence
  receipt proves that one scorer-policy hash remained unchanged; the budget equals the frozen
  five-model-by-item cell count at one sample per cell; every cell is terminal; all metrics are
  reported per run; every arm is routing-eligible in all three; all ten pair signs, with exact ties
  retained as ties, agree in all three; every per-arm official-score and parse-success range is at
  most 15 percentage points; the concrete resolved-identity set for each alias is identical across
  runs; and
  `"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-model-sweep-replication.ps1 -Suite .\suites\tier-judging-v1.json -Profile .\profiles\model-sweep-execution-v1.json -Repetitions 3 -SamplesPerCell 1 -MaxRangePoints 15 -Out .\data -Preregister 'Across three independent sealed-context tier-judging-v1 runs of the frozen five-model roster at one sample per cell, a pre-run evidence header will pin one scorer-policy hash and a call budget equal to the frozen roster-by-item cell count, and every manifest will pin identical suite, execution-context, and provider-profile hashes; every arm''s official score, parse-success rate, and larger-constant-control delta will be reported separately, every arm must be ROUTING_ELIGIBLE in all three runs, and each of the ten unordered pair signs (with an exact tie as its own sign) and each alias''s concrete resolved-identity set must match across all three while every arm''s official-score and parse-success max-minus-min range stays at or below 15 percentage points, or Step 16 stays blocked; this is calibration only and creates no ledger update.'"`
  exits 0 with `repetitions=3 eligible=PASS direction=PASS range=PASS identity=PASS`; any other
  result preserves the fixed three runs, blocks Step 16, and requires a versioned Step 14 suite or
  Step 59 policy change with a new preregistration before another attempt
- **Depends on:** 13, 14, 58, 59, 60
- **Status:** NOT STARTED

### Step 62: Finish the zombie-aware Linux identity-gate repair

- **Problem:** The Step 26 containment gate conflates a killed-but-unreaped zombie with a live
  process because both retain the same `/proc/<pid>/stat` start token. Implement and review a
  `_pid_state` helper and bounded settle loop from the behavior specified here, keep zombies
  acceptable only after the expected kill, and retain a negative anchor that proves a genuinely
  live identity still fails. The current user-owned dirty candidate is reference evidence, not an
  implicit build input. Preserve the retained tmpfs-root FD and every existing cleanup invariant
  required by Step 27.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/process.py`, `scripts/soak-agent-bench-wsl.ps1`,
  `tests/agent_bench/test_process.py`, `tests/agent_bench/test_wsl_soak_script.py`
- **Produces:** zombie-aware terminal identity settlement, live-process red anchor, focused unit and
  Linux regression evidence; an autonomous soak wrapper that captures every log, asserts one staged
  tree hash, rejects any nonzero gate exit, writes the preregistration before run 1, and prints the
  pass rate, plus a fail-closed `-VerifyOnly` receipt check; no evaluator, provider adapter, or
  inference path
- **Done when:** the focused tests prove gone and zombie identities settle, a live `/bin/sleep`
  identity remains red, timeout/exception paths close every owned FD, the existing retained-root
  tests remain green; an offline fake-gate integration test proves the soak wrapper rejects a
  changed staged-tree hash and a 7/8 result while preserving all logs; the full code-quality suite
  passes; and
  `"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-agent-bench-wsl.ps1 -Distribution Ubuntu"`
  exits 0 with zero selected skips from a staged WSL-ext4 tree
- **Depends on:** 26 (shipped)
- **Status:** NOT STARTED

### Step 63: Soak the reviewed containment gate on WSL ext4

- **Problem:** The captured failure was intermittent, so one green invocation cannot qualify the
  substrate. Run the reviewed Step 62 tree eight independent times through the Windows
  control-plane launcher, preserve every exit and staged-tree hash, and report a rate before Step 27
  consumes the retained tmpfs-root FD.
- **Type:** wait
- **Issue:** #
- **Flags:** --reviewers auto
- **Files:** `data/qualification/agent-bench-containment-step63/`,
  `docs/agent-benchmark/containment-soak-step63.md`
- **Produces:** eight timestamped WSL-ext4 gate logs, staged-tree hashes, exit codes, selected-skip
  counts, the pass-rate numerator/denominator, and an explicit Step-27 unblock/block verdict
- **Done when:** the Step 63 claim is written to the evidence header before run 1; every invocation
  tests the same reviewed staged-tree hash, exits 0, selects zero skips, and records no live-identity
  or FD escape; and
  `"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\soak-agent-bench-wsl.ps1 -Distribution Ubuntu -Repetitions 8 -Out .\data\qualification\agent-bench-containment-step63 -Preregister 'The reviewed containment repair will pass 8/8 independent WSL-ext4 gate invocations with zero selected skips and no live-identity or retained-FD escape; any lower pass rate returns the work to Step 62 and blocks Step 27.'"`
  exits 0 after printing `containment_gate_rate=8/8 (100.0%)`; any failure returns to Step 62 and
  leaves existing Step 27 blocked
- **Depends on:** 62
- **Status:** NOT STARTED

After Step 63 passes, resume existing Steps 27-31. Step 30 remains the first live Luna inference,
Step 35 the first real two-provider pipeline smoke, Step 41 the no-ranking pilot calibration, and
Step 54 the sole decision-grade v1 Luna-versus-Sonnet observation.

## 8. Risks and Halt Conditions

- **CLI contract drift:** Step 56 doctors the installed Claude executable before calls. An absent or
  unsupported sealing flag, changed JSON envelope, missing subscription authentication, or missing
  concrete identity is a documented halt; it does not fall back to an unsealed invocation.
- **Local endpoint prerequisite:** existing Steps 13 and 59 require the operator-started local
  OpenAI-compatible endpoint. The orchestrator preflights every frozen roster binding and aborts
  before any call if the explicit endpoint/profile cannot be resolved.
- **Metric instability:** a below-control arm or inconsistent direction is a valid calibration
  result, not a reason to tune after looking. A range over 15 points is likewise unstable. Any of
  these blocks Step 16 and returns to the owning calibration step with the fixed three runs
  preserved; adding a favorable fourth run is forbidden.
- **Historical compatibility:** a legacy run remains inspectable. Any attempt to append sealed
  Claude rows to it, infer provider identity from a requested alias, or compare unequal execution
  hashes fails loud.
- **Containment nondeterminism:** less than 8/8 is a Step 62 defect. Step 27 cannot reinterpret a
  zombie, reopen scratch by pathname, skip a Linux test, or waive the retained-FD contract.
- **Working-tree isolation:** `/build-step` worktrees do not inherit user-owned dirty changes.
  Before the build pipeline, the operator must checkpoint each current change by ownership and
  restore a clean `git status`; the process/test candidate and unrelated report/CLI work must never
  be swept into one commit. Step 62 implements from this plan and its regression tests rather than
  depending on an uncommitted patch. Steps 56/57/59 re-run their downstream-consumer grep after
  integration against the then-current committed report surface.

No product decision remains open in this plan. A genuine external prerequisite failure records the
exact command, exit, and evidence path, then halts at the owning step without a mid-run question.

## 9. Cross-plan Execution Commands

Never run this coordinator as an unfiltered numeric phase and never use `--resume` to jump a
cross-plan boundary. `repo-sync` creates/updates issues but does not write their numbers back into
this file. After its real run, backfill exactly the eight new `Issue:` fields from the emitted map,
add blocking cross-links on existing issues `#13`, `#14`, `#16`, and `#29`, and verify there are no
blank fields before build:

```powershell
$blank = Select-String -LiteralPath .\documentation\first-measurement-validity-and-luna-routing-plan.md -Pattern '^- \*\*Issue:\*\* #\s*$'; if ($blank) { $blank; throw 'repo-sync issue numbers have not been backfilled' }; 13,14,16,29 | ForEach-Object { gh issue view $_ --comments }
```

The issue comments must name the new blocking step and issue number; issue-body rewrites are not
required. After `plan-redline`, `plan-wrap`, sync, backfill, and cross-link verification, the first
code span is exactly:

```text
/build-phase --plan documentation/first-measurement-validity-and-luna-routing-plan.md --steps 56,57,62 --dry-run
```

After inspecting the parse, run the same command without `--dry-run`. Build-phase must then stop;
Steps 58 and 63 are explicit observation commands, not code dispatches.

After Step 58 completes, fail closed on its receipt before canonical Step 13:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\qualify-model-sweep-context.ps1 -VerifyOnly -Out .\data\qualification\model-sweep-context-v1
```

Run canonical Step 13 with the exact Section 4 sentence, explicit profile, one sample per cell, and
the 500-call five-model-by-100-item cell budget:

```powershell
uv run mt run --suite .\suites\tier-judging-v0.json --config .\profiles\model-sweep-execution-v1.json --models general-35b,coder-30b,haiku,sonnet,opus --samples 1 --budget 500 --preregister 'On a sealed tier-judging-v0 run of the explicit five-model roster at one sample per cell, every cell will be terminal, every arm will carry concrete provider identity, all five official scores will be strictly inside (0,100), and at least two official scores will differ; this is calibration only and creates no ledger update.'
```

Record the emitted run ID and findings note, then build canonical Step 14 explicitly:

```text
/build-phase --plan plan.md --steps 14
```

Only after the frozen Step 14 suite and calibration artifacts exist may the coordinator build Step
59:

```text
/build-phase --plan documentation/first-measurement-validity-and-luna-routing-plan.md --steps 59,60
```

After Step 61 completes, verify the fixed three-run receipt before any Step 16 invocation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-model-sweep-replication.ps1 -VerifyOnly -Out .\data
```

Step 16 then preregisters its separate confirmatory claims and uses fresh run IDs. It never imports
Step 61 rows as ledger evidence.

After Step 63 completes, verify the containment receipt before coding-agent Step 27:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\soak-agent-bench-wsl.ps1 -VerifyOnly -Out .\data\qualification\agent-bench-containment-step63
```

```text
/build-phase --plan documentation/coding-agent-benchmark-plan.md --steps 27
```

Each `-VerifyOnly` command checks the preregistration, expected count, terminal verdict, identity or
staged-tree hashes, and exact producer version; an absent/stale/mismatched receipt exits nonzero.

## 10. Testing Strategy

### Offline and integration gates

- Unit-test strict execution-profile schema, canonical hash, explicit provider dispatch, unknown
  provider/name rejection, identity-provenance sentinels, environment allowlisting, and legacy
  manifest handling.
- Drive the real `mt run` subprocess entry point with a fake Claude executable, not a direct call to
  a duplicate argv builder. Assert the fake observes the final executable path, argv, cwd, and env.
- Drive Markdown, JSONL, and HTML report entry points against sealed, unresolved, and legacy
  fixtures. Old run stores remain readable; incomparable runs are visibly ineligible.
- Test all validity controls through the existing production scorer. Freeze the one validity-policy
  hash and keep parsed-only accuracy labeled diagnostic.
- Test the replication orchestrator's preflight and halt classes without inference: missing
  preregistration, fallback config, hash drift, incomplete row, unresolved identity, and unstable
  eligibility/direction.
- Preserve the live-process negative anchor and retained-FD cleanup tests while accepting only the
  killed zombie state at the documented settlement point.

### Real observation gates

- Step 58 spends exactly three Claude canary calls and makes no quality/routing claim.
- Existing Step 13 and Step 14 create and freeze the full-roster v1 instrument before Step 59/60.
- Step 61 makes three independent complete frozen-roster runs and reports each run separately. It
  does not average away a failed eligibility or direction gate and does not touch the ledger.
- Step 63 runs only through the Windows PowerShell launcher into an ephemeral WSL-ext4 staging tree;
  native Windows pytest output is never accepted as containment evidence.
- Existing coding-agent Steps 30, 35, 41, and 54 retain their registered scopes and remain the only
  Luna inference/measurement observations.

## 11. Appendix

### Decision Inventory

IDs are append-only across proposal republications. `P` records an explicit operator choice from
the brief; `D` records an agent-defaulted implementation choice that the operator may redline.

| ID | P/D | choice | status |
|---|---|---|---|
| P1 | P | Keep Instruments A and B runtime- and evidence-independent; do not pool their scores. | active — operator-picked |
| P2 | P | Do not alter `tier-judging-v0`, `Suite`, `RunRow`, the verdict parser, `DEFAULT_ROSTER`, or pending canonical steps without an explicit owning step. | active — operator-picked |
| P3 | P | Do not publish the first run through Steps 18-24 or write a ledger claim from one unreplicated sample. | active — operator-picked |
| P4 | P | Measure production entry points, abort on fallback, preregister numeric claims, and calibrate new metrics against known-good and known-garbage anchors. | active — operator-picked |
| P5 | P | Keep execution autonomous and PowerShell-runnable; accept containment evidence only from WSL2 Ubuntu on ext4. | active — operator-picked |
| P6 | P | Number new work from Step 56 and follow `plan-feature` → `plan-review` → `plan-redline` → `plan-wrap` → `repo-sync` → `build-phase` → `repo-update`. | active — operator-picked |
| P7 | P | Exercise the intermittent containment gate 6-8 times and report a pass rate. | active — operator-picked |
| D1 | D | Quarantine all 200 rows of the first run as diagnostic-only rather than filtering the 23 visible contamination hits. | stands — operator approved 2026-08-30 |
| D2 | D | Preserve official end-to-end scoring while reporting parse health and constant-label controls separately. | stands — operator approved 2026-08-30 |
| D3 | D | Seal Instrument A behind a validated provider profile, clean cwd/environment, disabled tools/session behavior, and concrete identity receipt. | stands — operator approved 2026-08-30 |
| D4 | D | Keep legacy runs readable as `LEGACY_UNSEALED` while forbidding a resume that mixes pending Claude cells into the new execution contract. | stands — operator approved 2026-08-30 |
| D5 | D | Target Luna only through Instrument B at the preregistered `high` effort; leave Instrument A Luna support for a separate future plan. | stands — operator approved 2026-08-30 |
| D6 | D | Reuse canonical Steps 13-17 and 27-55, adding eight validity/containment gates around them and allowing Steps 56 and 62 to begin in parallel. | stands — operator approved 2026-08-30 |
| D7 | D | Require three frozen v1 replications, all arms eligible, identical ten-pair signs and identity sets, score/parse range at most 15 points, and no favorable fourth run. | stands — operator approved 2026-08-30 |
| D8 | D | Convert the operator's 6-8-run requirement into a strict post-review 8/8 WSL-ext4 acceptance gate before Step 27. | stands — operator approved 2026-08-30 |
| D9 | D | Keep scorer-policy identity in a separate preregistered replication receipt and enforce cross-plan transitions with `-VerifyOnly` checks. | stands — operator approved 2026-08-30 |
