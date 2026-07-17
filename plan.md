# measure-twice — v1 plan

## 1. What This Is

**measure-twice** is a local benchmarking toolkit (Python package + `mt` CLI, operator-only) for creating, running, and interpreting model benchmarks across the `dev/` workspace. It exists to (a) convert the workspace's tier-routing decisions from assertions into measurements — a reconnaissance audit found exactly ONE genuine head-to-head quality measurement behind all of tier-offload/tier-escalate's model pins (see `docs/research/tier-skills-benchmark-map.md`); (b) give any project reusable tools to author and run its own benchmarks; (c) build a **discriminative flagship dataset** where every roster model scores strictly inside (0, 100); (d) profile what different models are good and bad at; and (e) teach the operator benchmark-craft by doing — every phase pairs the tool with a real experiment and a methodology note. Out of scope for v1: web UI/dashboards, CI/regression integration, latency/cost benchmarking (cost is logged, not benchmarked), and any public/shareable benchmark publishing.

**Doctrine:** measure-twice *conforms to* `dev/.claude/skills/_shared/judge-core.md` (verdict-contract spine §5.6, honesty invariants §5) and operationalizes `dev/.claude/rules/measurement-validity.md` (score the production artifact; assemble through production paths; fail loud on fallback; calibrate with anchors; match measurement scope to decision scope). It references both — it never restates them.

**Proposal:** https://claude.ai/code/artifact/d641f0ba-41d3-46ea-898a-be0a9d4d1b66 — operator-facing review surface (`/plan-redline`); canonical P/D decision registry in Appendix § Decision Inventory.

## 2. Stack

| Layer | Tool | Why |
|---|---|---|
| Language / runtime | Python ≥3.12 + uv | Workspace standard; matches switchboard's floor (`switchboard/pyproject.toml` `requires-python = ">=3.12"`) |
| Package layout | `measure_twice/` at repo root (NOT `src/`) | Mirrors sibling switchboard convention |
| Build backend | hatchling | Mirrors switchboard |
| Runtime deps | **stdlib-only** + `switchboard` via uv path dependency (`../switchboard`) | No-unjustified-deps rule; switchboard's `aggregate_agreement` kill-rule math and `local_judge` stay one-source-of-truth (verified: switchboard is a hatchling package with `packages = ["switchboard"]`) |
| CLI | `mt` entry point, argparse | stdlib; scriptable from other projects |
| Local model calls | switchboard `local_judge` semantics / OpenAI-compatible endpoint `localhost:8080` (llama-swap, WSL2) | Reuse, don't rewrite; endpoint is operator-started (`switchboard/scripts/start-offload.ps1`) |
| Claude-tier calls | `claude` CLI subprocess with subscription OAuth | Workspace standard (OAuth over API key); same pattern as void_furnace `readiness_bench` Path A |
| Tests / lint / types | pytest, ruff, mypy --strict | Mirror switchboard's `pyproject.toml` settings |
| Storage | JSON (suites) + append-only JSONL (runs) + rendered markdown (reports) | git-diffable, stdlib, matches switchboard/void_furnace report shapes |

## 3. Data Store

Flat files only. No database in v1.

### Layout (hybrid data home)

```
measure-twice/
  suites/                      # cross-cutting suites owned by this project (tier-judging, calibration)
  data/
    runs/<run_id>/rows.jsonl   # append-only result rows (+ manifest.json per run)
    ledger/claims.jsonl        # evidence ledger (append-mutate: rewrite row by claim_id)
    reports/                   # rendered markdown reports
<other-project>/
  benchmarks/suites/*.json     # project-specific suites live WITH the consuming project
  benchmarks/runs/...          # and their results stay there too (engine takes paths)
```

measure-twice is the engine either way; `mt run --suite <path> --out <dir>` works on any path.

### Suite file (JSON)

```json
{
  "suite": "tier-judging-v0",
  "version": 1,
  "description": "...",
  "domain": "judging",
  "scoring": {"type": "verdict", "labels": ["pass", "flag"]},
  "items": [
    {
      "id": "001-style-verdict-easy",
      "tags": ["style", "easy", "code-review"],
      "prompt": "...",
      "expected": "flag",
      "difficulty_prior": 0.2,
      "provenance": "harvested: <file or 'authored'>"
    }
  ]
}
```

- `scoring.type ∈ {verdict, exact, rubric}` — v1 scoring types (§4).
- **Suite hash:** sha256 over the canonical JSON (sorted keys) of `items`; recorded in every run manifest. A changed hash = a different instrument; cross-run comparisons require equal hashes.
- Suite names, model names, item ids all match switchboard's `_SAFE_NAME_RE` (`^[A-Za-z0-9._\-]+$`, `switchboard/switchboard/config.py:47`) so any result can name a switchboard `enabled_call_sites` key without translation.
- Loader validates on read and **aborts** on any schema violation (no lenient skips).

### Run store (append-only JSONL)

- `run_id` = `run_<YYYYMMDDTHHMMSSZ>_<6hex>` (UTC stamp + `os.urandom` hex).
- `manifest.json`: `{run_id, suite, suite_hash, roster, samples_per_cell, judges, started_utc, config_source, budgets, preregistration}`.
- `rows.jsonl`, one row per (model × item × sample): `{run_id, model, model_id_resolved, item_id, sample_k, response_raw, parsed, score, scorer, judge_scores, elapsed_s, error}`.
- **Resume:** a cell is complete iff a row with non-null `score` (or a terminal `error`) exists; `mt run --resume <run_id>` skips complete cells. A torn trailing line (crash mid-append) is detected and truncated on resume.
- **Re-run/dedup semantics:** re-running the same suite@hash × roster mints a NEW run (append-only history); reports resolve latest-per-(suite_hash, model) by manifest timestamp unless `--run` pins one.

### Evidence ledger (`data/ledger/claims.jsonl`)

One row per tier-routing/model-choice claim:

```json
{
  "claim_id": "style-lens-local-safe",
  "statement": "The review-deep Style lens can be judged by a local model at >=80% agreement with Claude",
  "decision_surface": "offload-config: build-step-style",
  "sources": [{"file": "dev/.claude/skills/tier-offload/SKILL.md", "lines": "30-33", "quote_sha256": "<hash of cited text>"}],
  "status": "ASSERTED",
  "evidence": [],
  "verdict": null,
  "preregistration": null,
  "last_verified_utc": "2026-07-15T00:00:00Z"
}
```

- `status ∈ {MEASURED, PARTIAL, ASSERTED, STALE}`. `mt claims audit` recomputes each `quote_sha256` against the live source file and flips rows to STALE when cited text drifted. A MEASURED row must carry ≥1 `evidence` run id + a `preregistration` sentence (measurement-validity § scope).
- This ledger is the durable answer to "not just what's pinned, but why" — switchboard's config records only bare booleans with **no evidence/provenance field** (verified against `SwitchboardConfig`, `config.py:72-102`), so the *why* lives here, upstream. A switchboard schema handshake is deferred (v2, Open Questions).

### Corruption protection

Append-only JSONL + per-run directories; suites and ledger are git-tracked; torn-line truncation on resume; loader/ledger abort on malformed rows rather than skipping.

## 4. Scoring Model & the Discriminative Contract

### Scoring types (v1)

| Type | Mechanism | Score per item |
|---|---|---|
| `verdict` | Deterministic: parse the model's verdict (judge-core §5.6 spine: extract-JSON/label robustly → validate → coerce; parse-failure → item scored 0, recorded as `parse_fail`, never crash) and compare to `expected` | 0 or 1 |
| `exact` | Deterministic string/regex match against `expected` | 0 or 1 |
| `rubric` | LLM judge (Claude), k=3 samples, **median** of parsed samples, `SCORE: <n>` / `RATIONALE:` two-line contract, clamp 0–10 | 0–10 normalized to 0–1 |

- **Suite score = 100 × mean item score** → the 0–100 scale.
- **Empty/no response is force-scored 0 before any judge call** (port of void_furnace's `no_diff` invariant, `runner.py:78,904-911`) — a model that produced nothing never gets judged.
- **Per-judge parse-fail gate:** if any single judge's parse-failure rate exceeds 0.5, the run **aborts** (port of `JudgeParseFailureError`, fired per-judge so one broken judge can't hide behind healthy peers).
- **Judge-circularity guard:** the flagship dataset uses `verdict`/`exact` items with curated gold answers — no LLM judge in the loop for any tier-ordering claim. `rubric` is for capability profiling only, and rubric results never gate a ledger MEASURED flip on their own.

### The discriminative contract (flagship dataset)

Goal: a dataset scored 0–100 on which **no roster model scores 0 or 100**.

- **Per-item difficulty** (empirical): fraction of (model, sample) cells correct across the roster. Items at 0.0 (nobody) or 1.0 (everybody) are *saturated* — flagged by `mt calibrate` for replacement.
- **Acceptance band:** every default-roster model scores within **[5, 95]**; configurable per suite.
- **Instrument anchors:** every scoring path ships frozen good/garbage response pairs; CI asserts `score(good) > score(garbage)` forever (void_furnace `anchors.py` pattern).
- **Pre-registered kill criterion:** if the best local model outscores Opus on the flagship suite by >10 points overall, HALT and audit the instrument before trusting or publishing any number (per-tag inversions are *findings*; gross overall inversion is presumed instrument failure first).

### Capability profiling

`mt profile <model>`: per-tag accuracy delta vs roster mean across all stored runs of equal suite hash; a tag renders only with n ≥ 8 items (min-n guard); output ranks strongest/weakest tags per model and biggest cross-model gaps per tag. This is the "what are models good/bad at" deliverable — differences localized to tags, not vibes.

## 5. Modules

All under `measure_twice/` (package at root, switchboard convention).

- `config.py` — run config resolution: explicit `--config` → `$MEASURE_TWICE_CONFIG` → `<cwd>/measure-twice.json` → built-in defaults. The resolved source is recorded in every run manifest; a live run (`mt run`, `mt smoke`) **aborts** if the local endpoint is named in the roster but unreachable, or if the claude CLI is not invocable — startup checks, not warnings (measurement-validity § fail loud). Default roster: `general-35b`, `coder-30b` (local), `haiku`, `sonnet`, `opus` (claude CLI aliases). Budgets: `max_calls` per run (default 500), per-model timeout.
- `suite.py` — schema, loader (abort on violation), canonical-JSON item hash, `validate` entry.
- `adapters/local.py` — OpenAI-compatible chat call against `localhost:8080` reusing switchboard's error taxonomy (defer `reason_class` values: `unreachable, timeout, os_error, non_json_body, bad_envelope, truncated, bad_verdict` — `switchboard/switchboard/client.py`); reasoning-model handling (read `choices[0].message.content`, ignore `reasoning_content`; `max_tokens ≥ 2000` for verdicts — switchboard CLAUDE.md gotcha).
- `adapters/claude_cli.py` — `claude -p --model <alias> --output-format json` subprocess; the prompt is passed via **stdin, never argv** (Windows argv >32K raises WinError 206 — `feedback_subprocess_large_arg_stdin_windows`); unwraps the JSON envelope; records the resolved model id from the envelope (drift detection); counts calls against the run budget; bounded parallelism (small pool), sequential against the local endpoint.
- Both adapters take a **client-factory DI seam** so the whole engine is offline-testable (readiness_bench pattern).
- `runner.py` — sweep suite × roster × samples; append rows as produced; resume; budget abort; no-response force-0.
- `scoring/deterministic.py` — verdict/exact scorers + the §5.6-conformant parse spine.
- `scoring/judge.py` — k=3 median rubric judge + per-judge parse-fail gate.
- `analyze/calibrate.py` — item difficulty, saturation flags, acceptance band, kill-criterion check.
- `analyze/profile.py` — per-tag deltas, min-n guard, cross-model comparison.
- `analyze/agreement.py` — thin delegation to `switchboard.harness.aggregate_agreement` (kill iff disagreement > 0.20 strictly; DEFER excluded from denominator) — never re-implemented.
- `ledger.py` — claims load/save, quote-hash staleness audit, render to markdown.
- `author.py` — item harvesters (workspace artifacts → candidate items with provenance) + authoring stubs (Phase C).
- `report.py` — per-run markdown, cross-run comparison tables, JSONL export.
- `cli.py` — `mt` dispatch.
- `tests/` mirrors modules; `tests/anchors/` holds the frozen good/garbage pairs.

## 6. CLI Contract

(No backend API; the CLI is the contract.)

| Command | Does |
|---|---|
| `mt validate <suite.json>` | Schema check + item-hash print; exit non-zero on violation |
| `mt run --suite <path> --models <csv> [--samples N] [--judges <csv>] [--budget N] [--resume <run_id>] [--out <dir>] [--config <path>]` | Execute a sweep; append-only; resumable |
| `mt score <run_id>` | (Re)score stored raw responses — scoring is re-runnable without re-calling models |
| `mt report <run_id> [--compare <run_id...>]` | Render markdown report / cross-run table |
| `mt calibrate --suite <path> [--runs <ids>]` | Difficulty histogram, saturation list, acceptance-band + kill-criterion verdict |
| `mt profile <model>` | Strengths/weaknesses by tag across stored runs |
| `mt claims list\|audit\|render` | Ledger operations; `audit` flips drifted rows to STALE |
| `mt author harvest <source> \| stub <suite>` | Item-authoring pipeline (Phase C) |
| `mt smoke [--claude\|--local]` | 2-item end-to-end real-model smoke; exit code is the gate |

## 7. Project Structure

```
measure-twice/
  plan.md                      # this file (canonical entry plan)
  CLAUDE.md
  pyproject.toml               # hatchling; [tool.uv.sources] switchboard = { path = "../switchboard" }
  measure_twice/
    __init__.py
    config.py  suite.py  runner.py  ledger.py  author.py  report.py  cli.py
    adapters/   local.py  claude_cli.py
    scoring/    deterministic.py  judge.py
    analyze/    calibrate.py  profile.py  agreement.py
  suites/                      # cross-cutting suites (tier-judging-v*.json, smoke.json)
  data/
    runs/  ledger/claims.jsonl  reports/
  docs/
    research/                  # seed recon: tier-skills-benchmark-map.md, workspace-bench-prior-art.md
    methodology/               # numbered learning notes, one+ per phase
    investigations/            # benchmark-domains investigation (Step 10)
    tier-benchmark-map.md      # Phase B deliverable (rendered from ledger)
  tests/
    anchors/                   # frozen good/garbage pairs per scorer
```

## 8. Key Design Decisions

1. **Stdlib-only core + switchboard path dependency.** The agreement/kill math (`aggregate_agreement`), local-endpoint semantics, and defer taxonomy already exist in switchboard as the one source of truth; measure-twice imports them (verified importable: hatchling package, zero deps, `>=3.12`). Re-implementing any of it is the bench-vs-production assembly drift measurement-validity §2 warns about.
2. **Port void_furnace's invariants, not its code.** no-response force-0, fail-loud guards, k=3 median judging, frozen anchors are re-implemented here in project-neutral form (VF's modules import its harness/workflows and can't be reused directly). Switchboard already established this port-the-shape precedent (`bakeoff.py` docstring).
3. **Fail loud, never warn.** Unreachable roster endpoint, claude CLI absent, malformed suite, per-judge parse-fail >0.5, ledger row malformed — all abort. A bench that silently degrades produces numbers indistinguishable from real ones.
4. **The VF fallback-prompt bug class is structurally absent.** Suites carry ALL item content; adapters carry no prompt templates — there is nothing to silently fall back to. The residual fallback risk (endpoint/config defaults) is closed by recording `config_source` in the manifest + startup reachability aborts.
5. **Evidence ledger as first-class.** Every tier-routing claim becomes a row with status MEASURED/PARTIAL/ASSERTED/STALE, source quote-hashes, and pre-registration sentences. Recon found ~1 real measurement behind the entire current routing policy; the ledger makes the gap enumerable and the progress measurable. Switchboard handshake (a provenance field in its config) deferred to v2 to keep v1 single-project.
6. **Claude via CLI OAuth with budgets + resume.** Bench sweeps ride the subscription; `max_calls` budgets, bounded parallelism, and cell-level resume make a blown session limit an interruption, not a loss. API-key mode is a non-goal for v1.
7. **Deterministic gold before LLM judges.** Flagship tier-ordering claims rest only on verdict/exact items with curated answers; rubric judging exists for profiling but never solely flips a ledger row to MEASURED. This kills judge-circularity (Claude judging Claude) on the claims that matter.
8. **Hybrid data home.** Project suites/results live with the consuming project (the project owns its instrument); cross-cutting suites + the ledger live here. The engine is path-agnostic.
9. **Learning woven in.** Each phase ends by producing a `docs/methodology/NN-*.md` note answering: what does this instrument measure, what can't it tell you, what surprised us. These notes are step deliverables, not optional extras.
10. **Scoring is re-runnable offline.** Raw responses are stored verbatim; `mt score` re-scores without re-calling models — scorer bugs don't burn model calls, and scorer changes are diffable against frozen raw data.

## 9. Open Questions / Risks

| Item | Risk | Mitigation |
|---|---|---|
| Claude CLI session limits during sweeps | A 5-model × 100-item sweep may hit subscription caps mid-run | Budgets + resume; sweeps sized ≤500 calls; Claude portion of roster can run in tranches |
| Local endpoint availability | llama-swap in WSL2 is operator-started; agent must never auto-spawn it | Steps needing it are `Type: wait`/operator M-steps with explicit start commands; `mt run` aborts (not warns) when local models are in roster but endpoint is down |
| switchboard `suites/*.json` not found on current branch | Recon cited `switchboard/suites/{effort-vs-model,review-deep-style}.json`; not present on this checkout | Verify at build (Step 16); shadow-mode agreement runs depend only on `harness.py` APIs, which are present |
| Fable in roster | Needs ≥30-day retention; session-limit blowout risk | Excluded from default roster; one-off invocable via `--models fable` when a specific claim needs it |
| Dataset difficulty collapse | First 100 items may all cluster easy (all models ~90+) | `difficulty_prior` targets at authoring time + Step 14 iteration loop replacing saturated items |
| Judge circularity | Claude judging Claude-tier outputs inflates agreement | Decision 7: deterministic gold for all tier-ordering claims |
| Item contamination | Harvested workspace artifacts may be trivially familiar to Claude tiers | Provenance recorded per item; contamination-suspect tags; treated as a finding dimension in calibrate, not ignored |
| `mt` command-name collision | Some other `mt` on PATH | Entry point also registered as `measure-twice`; `mt` checked at Step 1 (`where.exe mt`) |
| claude CLI flag drift | `--output-format json` envelope shape may change with CLI updates | Envelope unwrap isolated in `adapters/claude_cli.py` with a contract test; resolved model id recorded per row |

## 10. How to Run

```powershell
cd c:\Users\abero\dev\measure-twice
uv sync --extra dev
uv run pytest -q                                   # includes anchor ordering gates
uv run mt validate suites/smoke.json
uv run mt smoke --claude                            # 2-item real haiku run, end-to-end
# local models: start the endpoint first (operator action):
#   powershell -ExecutionPolicy Bypass -NoProfile -File ..\switchboard\scripts\start-offload.ps1
uv run mt smoke --local
uv run mt run --suite suites/tier-judging-v0.json --models general-35b,haiku,sonnet
uv run mt report <run_id>
uv run mt claims list
```

## 11. Development Process

Built via `/build-phase` per phase, one worktree-isolated `/build-step` per step, `--reviewers code` default. Phase map:

| Phase | Steps | Theme | Closing manual step | Umbrella |
|---|---|---|---|---|
| A | 1–7 | Core engine | M1 (local smoke) | #21 |
| B | 8–9 | Evidence ledger + tier map | — | #22 |
| C | 10–14 | Domains investigation + flagship dataset | M2 (calibration review) | #23 |
| D | 15–17 | Profiling + first measurements | M3 (walkthrough) | #24 |

Run as e.g. `/build-phase --plan measure-twice/plan.md --phase A` after `/plan-expedite`.

### Automated Steps

(These run unattended via /build-phase.)

**Phase A — Core engine**

### Step 1: Scaffold package, config module, switchboard path dep
- **Problem:** Create the uv/hatchling project (`measure_twice/` at root), `mt` + `measure-twice` CLI entry points, `config.py` with resolution order (explicit → `$MEASURE_TWICE_CONFIG` → `<cwd>/measure-twice.json` → defaults), recorded `config_source`, default roster/budgets, and the switchboard path dependency (`[tool.uv.sources] switchboard = { path = "../switchboard" }`).
- **Type:** code
- **Issue:** #1
- **Flags:** --reviewers code
- **Produces:** `pyproject.toml`, `measure_twice/{__init__,config,cli}.py`, `tests/test_config.py`
- **Done when:** `uv run mt --version` exits 0; `from switchboard.harness import aggregate_agreement` succeeds in a test; config tests prove abort-on-malformed and correct resolution order; pytest/ruff/mypy --strict green
- **Depends on:** none
- **Status:** DONE (2026-07-17)

<!-- autofix-applied: 2026-07-16 -->
### Step 2: Suite schema, loader, content hash
- **Problem:** Implement the suite JSON schema (§3), fail-loud loader, canonical-JSON item hash, `_SAFE_NAME_RE` name checks (imported from switchboard, not re-declared), `mt validate`, plus `suites/smoke.json` (2 trivial verdict items) as fixture.
- **Type:** code
- **Issue:** #2
- **Flags:** --reviewers deep
- **Produces:** `measure_twice/suite.py`, `suites/smoke.json`, `tests/test_suite.py`
- **Done when:** round-trip + stable-hash tests pass; malformed suites (bad name, missing expected, dup ids) each raise with a distinct error; `mt validate suites/smoke.json` exits 0 via the CLI entry point
- **Depends on:** 1
- **Status:** DONE (2026-07-17)

### Step 3: Model adapters (local + claude CLI) with DI seams
- **Problem:** `adapters/local.py` (OpenAI-compat chat vs `localhost:8080`; switchboard defer taxonomy; reasoning-model handling per switchboard CLAUDE.md gotchas) and `adapters/claude_cli.py` (`claude -p --model <alias> --output-format json`; envelope unwrap; resolved-model-id capture; call counting; bounded pool). Both behind client-factory DI seams; no-response sentinel defined here.
- **Type:** code
- **Issue:** #3
- **Flags:** --reviewers code
- **Produces:** `measure_twice/adapters/{local,claude_cli}.py`, `tests/test_adapters.py` (stub factories, offline)
- **Done when:** offline tests cover happy path + every error class (unreachable/timeout/non-json/truncated/empty) for both adapters; envelope contract test pins the claude CLI JSON shape; zero live calls in the test suite
- **Depends on:** 1

<!-- autofix-applied: 2026-07-16 -->
### Step 4: Runner — sweep, append-only JSONL, resume, budgets
- **Problem:** `runner.py`: sweep suite × roster × samples through the adapters; write manifest + append rows as produced; cell-complete resume with torn-line truncation; budget abort; no-response force-scored 0 before any judging; sequential local / small-pool claude scheduling.
- **Type:** code
- **Issue:** #4
- **Flags:** --reviewers deep
- **Produces:** `measure_twice/runner.py`, `mt run` + `mt score` wiring, `tests/test_runner.py`
- **Done when:** with stub clients: full sweep completes; kill-mid-run then `--resume` skips exactly the completed cells; budget-exceeded aborts resumably; **integration test drives `mt run` through the CLI entry point** (production caller) on `suites/smoke.json` with stub factories
- **Depends on:** 2, 3

<!-- autofix-applied: 2026-07-16 -->
### Step 5: Deterministic scoring + verdict spine + frozen anchors
- **Problem:** `scoring/deterministic.py` (verdict/exact scorers; judge-core §5.6-conformant parse: robust extract → validate → coerce → parse-failure→scored-0-recorded, never crash), 0–100 suite normalization, and the first frozen anchor pairs (`tests/anchors/`) with the CI ordering gate `score(good) > score(garbage)` per scorer.
- **Type:** code
- **Issue:** #5
- **Flags:** --reviewers deep
- **Produces:** `measure_twice/scoring/deterministic.py`, `tests/anchors/*`, `tests/test_scoring.py`, `docs/methodology/01-deterministic-scoring-and-anchors.md`
- **Done when:** anchor ordering gate green; parse-fail paths covered; re-scoring stored raw rows via `mt score` matches first-pass scores byte-for-byte
- **Depends on:** 4

### Step 6: LLM rubric judge — k=3 median + per-judge parse gate
- **Problem:** `scoring/judge.py`: `SCORE:`/`RATIONALE:` two-line contract, k=3 samples per judge, median of parsed samples, clamp 0–10, per-judge parse-fail-rate gate (>0.5 → abort run), parse-fail vs invoke-error kept distinct. Judges are Claude via the claude_cli adapter.
- **Type:** code
- **Issue:** #6
- **Flags:** --reviewers code
- **Produces:** `measure_twice/scoring/judge.py`, rubric anchor pair, `tests/test_judge.py`, `docs/methodology/02-llm-judges.md`
- **Done when:** offline tests prove median math (incl. even-parse-count), gate fires per-judge not pooled, rubric anchor ordering holds via stubbed judges
- **Depends on:** 5

### Step 7: Reports + claude-path smoke gate
- **Problem:** `report.py` (per-run markdown, cross-run comparison keyed on equal suite hash) and `mt smoke --claude`: run `suites/smoke.json` against haiku for real — 1 real request per item, no mocks, end-to-end suite→runner→scorer→report. This is the pipeline smoke gate that must pass before any long sweep.
- **Type:** code
- **Issue:** #7
- **Flags:** --reviewers code
- **Produces:** `measure_twice/report.py`, `mt smoke`, `tests/test_report.py`
- **Done when:** `uv run mt smoke --claude` exits 0 in under 60s producing a scored report with 0 parse failures; report renders for a stub multi-model run
- **Depends on:** 6

**Phase B — Evidence ledger + tier map**

### Step 8: Ledger module + `mt claims`
- **Problem:** `ledger.py` + `mt claims list|audit|render`: claims.jsonl schema (§3), quote-sha256 staleness audit against live source files, markdown rendering of the ledger grouped by status, MEASURED-row invariants (≥1 evidence run + preregistration sentence, enforced on write).
- **Type:** code
- **Issue:** #8
- **Flags:** --reviewers code
- **Produces:** `measure_twice/ledger.py`, `tests/test_ledger.py` (incl. a fixture where a cited source mutates → audit flips row to STALE)
- **Done when:** ledger round-trips; audit catches the mutated-source fixture; write of a MEASURED row without evidence/preregistration is rejected
- **Depends on:** 1

### Step 9: Populate the ledger + author the tier benchmark map
- **Problem:** Convert the seed recon (`docs/research/tier-skills-benchmark-map.md`) into ledger rows — every tier-offload/tier-escalate/model-preference decision becomes a claim with real `file:lines` citations and honest status (expect ~1 MEASURED [A3 arms], 1-2 PARTIAL [cost incident, endpoint timings], the rest ASSERTED) — and author `docs/tier-benchmark-map.md`: the narrative map ("what's pinned and why"), with tables rendered from the ledger by `mt claims render` so doc and data cannot drift.
- **Type:** code
- **Issue:** #9
- **Flags:** --reviewers code
- **Produces:** populated `data/ledger/claims.jsonl` (≥15 claims), `docs/tier-benchmark-map.md`, `docs/methodology/03-claims-and-evidence.md`
- **Done when:** `mt claims audit` passes fresh; every row's citation resolves; map's tables regenerate byte-identical from `mt claims render`; ≥15 claims covering both skills' rules, the three canonical Fable seeds, and the top-3 asserted-not-measured items from the recon
- **Depends on:** 8

**Phase C — Domains investigation + flagship dataset**

### Step 10: Benchmark-domains investigation
- **Problem:** Research and write `docs/investigations/benchmark-domains.md`: a taxonomy of benchmark domains relevant to this workspace (judging/grading, code authorship, planning, extraction, instruction-following, synthesis…); item-design patterns per domain; difficulty-calibration methods (classical difficulty indices, IRT-lite, adaptive item replacement); anti-saturation techniques; judge-circularity and contamination guards; and a decision record picking the item-design patterns for the flagship suite. Use web research (via the pinned deep-research workflow where appropriate) + the seed docs in `docs/research/`.
- **Type:** code
- **Issue:** #10
- **Flags:** --reviewers code
- **Produces:** `docs/investigations/benchmark-domains.md`, `docs/methodology/04-domain-taxonomy.md`
- **Done when:** doc contains all six required sections above, ≥10 cited external sources, and an explicit decision record; a fresh model could design flagship items from it alone
- **Depends on:** none (parallel-safe with Phase B)

### Step 11: Item-authoring pipeline
- **Problem:** `author.py` + `mt author harvest|stub`: harvesters that mine real workspace artifacts into candidate items with provenance (skill-eval golden corpora at `.claude/skills/*/evals/golden/`, review-deep style/correctness verdict fixtures, git-history code snippets), plus `stub` mode emitting schema-valid item templates (id, tags, expected, difficulty_prior, provenance) for agent/operator authorship. Content-hash dedup across candidates.
- **Type:** code
- **Issue:** #11
- **Flags:** --reviewers code
- **Produces:** `measure_twice/author.py`, `tests/test_author.py` (fixture-driven)
- **Done when:** harvest on committed fixtures yields ≥20 schema-valid candidates with provenance; dedup collapses a planted duplicate; stub output passes `mt validate`
- **Depends on:** 2

### Step 12: Flagship dataset v0 — tier-judging-v0
- **Problem:** Author `suites/tier-judging-v0.json`: ≥100 `verdict`/`exact` items in the tier-routing judging domain (style calls, correctness calls, grading decisions) with curated gold answers, tags (lens, difficulty bucket, provenance class), `difficulty_prior` spread targeting the [5,95] band, per Step 10's decision record; plus flagship instrument anchors (gold/garbage response pairs through the real scorers).
- **Type:** code
- **Issue:** #12
- **Flags:** --reviewers code
- **Produces:** `suites/tier-judging-v0.json`, flagship anchor pairs, `docs/methodology/05-item-authoring.md`
- **Done when:** `mt validate` passes; ≥100 items; every tag has ≥8 items (min-n); difficulty_prior histogram spans ≥4 buckets; anchor ordering gate green
- **Depends on:** 10, 11

### Step 13: Calibration sweep (observation run)
- **Problem:** Run the full default roster (general-35b, coder-30b, haiku, sonnet, opus) over tier-judging-v0 via `mt run` — the first real end-to-end observation run. Local endpoint must be started by the operator first (`start-offload.ps1`); Claude portion budgeted ≤400 calls, tranche-resumable. Capture a findings note (timings, defer/error rates, anything surprising) — findings capture is a deliverable, not a side effect.
- **Type:** wait
- **Issue:** #13
- **Produces:** complete `data/runs/<run_id>/` for 5 models × ≥100 items, `docs/methodology/06-first-sweep-findings.md`
- **Done when:** rows.jsonl has a terminal row for every (model, item) cell; defer/error rate per model recorded; findings note written
- **Depends on:** 7, 12

### Step 14: Discriminative calibration + dataset iteration
- **Problem:** `analyze/calibrate.py` + `mt calibrate`: per-item empirical difficulty, saturation flags, per-model 0–100 scores, [5,95] acceptance-band verdict, and the pre-registered kill criterion (§4). Then iterate tier-judging-v0 → v1: replace saturated items (re-running only replaced items against the roster), until the acceptance band holds.
- **Type:** code
- **Issue:** #14
- **Flags:** --reviewers code
- **Produces:** `measure_twice/analyze/calibrate.py`, `suites/tier-judging-v1.json`, calibration report under `data/reports/`, `docs/methodology/07-discriminative-calibration.md`
- **Done when:** calibrate report renders from Step 13 data; v1 suite passes the acceptance band on real run data (all 5 models strictly inside [5,95]); kill criterion evaluated and documented; unit tests on fixture runs cover saturation + band logic
- **Depends on:** 13

**Phase D — Profiling + first measurements**

### Step 15: Capability profiling
- **Problem:** `analyze/profile.py` + `mt profile <model>`: per-tag accuracy deltas vs roster mean across stored equal-hash runs, min-n ≥ 8 guard, strongest/weakest tag ranking per model, biggest per-tag cross-model gaps. Render a cross-model comparison report from the Phase C runs.
- **Type:** code
- **Issue:** #15
- **Flags:** --reviewers code
- **Produces:** `measure_twice/analyze/profile.py`, profile reports under `data/reports/`, `tests/test_profile.py`
- **Done when:** fixture-run tests cover delta + min-n logic; real profiles render for all 5 roster models from Phase C data via the CLI
- **Depends on:** 14

### Step 16: First ledger measurements (observation run)
- **Problem:** Convert the top asserted claims to MEASURED/PARTIAL with pre-registered runs: (a) style-lens local agreement — general-35b vs Claude verdicts on the style-tagged flagship slice, aggregated via `switchboard.harness.aggregate_agreement` (kill >0.20); (b) "Correctness/Bugs must stay Claude" — same shadow comparison on correctness-tagged items; (c) haiku-vs-sonnet on style (validates the existing review-deep Haiku pin). Each measurement gets its one-sentence preregistration in the ledger BEFORE the run. Update ledger rows + re-render the tier map.
- **Type:** wait
- **Issue:** #16
- **Produces:** measurement runs under `data/runs/`, updated `data/ledger/claims.jsonl` + regenerated `docs/tier-benchmark-map.md`, `docs/methodology/08-first-measurements.md`
- **Done when:** ≥3 ledger rows flipped from ASSERTED with run-id evidence + preregistration; agreement math traced to switchboard's function (imported, not re-implemented); findings note records each verdict incl. any DO-NOT-CHANGE outcomes
- **Depends on:** 14, 9

### Step 17: Methodology rollup + README
- **Problem:** Write `docs/methodology/README.md` (index + the "what each instrument can and cannot tell you" rollup — the learning deliverable), project `README.md` (what/why/quickstart), and update this plan's step statuses.
- **Type:** code
- **Issue:** #17
- **Flags:** --reviewers code
- **Produces:** `README.md`, `docs/methodology/README.md`, plan status updates
- **Done when:** every methodology note 01–08 is indexed with a one-line takeaway; README quickstart commands verified against the real CLI (`--help` output quoted, not imagined)
- **Depends on:** 15, 16

### Manual Steps

(These run after the owning phase's /build-phase completes. Operator drives.)

### Step M1: Local-endpoint smoke
- **Source step:** Step 7 (Phase A close-out)
- **Issue:** #18
- **Commands:**
  ```powershell
  powershell -ExecutionPolicy Bypass -NoProfile -File c:\Users\abero\dev\switchboard\scripts\start-offload.ps1
  ```
  Then, in a second terminal:
  ```powershell
  cd c:\Users\abero\dev\measure-twice
  uv run mt smoke --local
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | `mt smoke --local` exit code | 0 |
  | Report shows parsed verdicts for both items | No `truncated`/`bad_verdict` defers (max_tokens ≥ 2000 honored) |
  | Warm-call latency in report | Roughly 16–60s per call (cold first call may take minutes — normal) |

### Step M2: Calibration review (learning moment)
- **Source step:** Step 14
- **Issue:** #19
- **Commands:**
  ```powershell
  cd c:\Users\abero\dev\measure-twice
  uv run mt calibrate --suite suites/tier-judging-v1.json
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Acceptance band | All 5 models strictly inside [5, 95] |
  | Saturation list | Empty (or only items already flagged for the next iteration) |
  | Difficulty histogram | Spread across buckets, not one spike |
  | Kill criterion | Not triggered (best local NOT >10 pts over Opus); if triggered, halt — instrument audit before trusting anything |
  | Your read | You can explain from `docs/methodology/07-*.md` WHY each saturated item saturated |

### Step M3: v1 walkthrough
- **Source step:** Step 17
- **Issue:** #20
- **Commands:**
  ```powershell
  cd c:\Users\abero\dev\measure-twice
  uv run mt profile opus
  uv run mt claims list
  ```
  Optionally: `/user-walkthrough measure-twice v1`
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Profiles | Strengths/weaknesses per model are tag-anchored with n≥8, not vibes |
  | Ledger | ≥3 MEASURED rows with run ids + preregistration; STALE/ASSERTED honestly labeled |
  | Tier map | `docs/tier-benchmark-map.md` matches `mt claims render` output |

After all Automated Steps of a phase complete, the orchestrator prints the phase's manual handoff explicitly — after Phase A: **Please run M1 next.**

## 12. Appendix

### Seed research (read these first when building)

- `docs/research/tier-skills-benchmark-map.md` — full recon of tier-offload/tier-escalate: taxonomy (verbatim), both governing rules (verbatim), the empirical-vs-asserted evidence audit, eval-harness inventory, switchboard config contract w/ file:line cites. **Step 9's primary input.**
- `docs/research/workspace-bench-prior-art.md` — what exists and the reuse verdict per piece: switchboard engine (reuse-as-is), `_shared` grading/calibration (reuse-as-is), void_furnace invariants (extract-pattern), judge-core + skill-evals (reference/integrate, never duplicate).

### Verbatim load-bearing values (sourced from producing files)

- switchboard name rule: `_SAFE_NAME_RE = ^[A-Za-z0-9._\-]+$` (`switchboard/switchboard/config.py:47`)
- switchboard kill rule: kill iff `disagreement_rate > kill_threshold` strictly, `DEFAULT_KILL_THRESHOLD = 0.20`, DEFER excluded from denominator (`switchboard/switchboard/harness.py`, `aggregate_agreement`)
- switchboard defer taxonomy: `unreachable, timeout, os_error, non_json_body, bad_envelope, truncated, bad_verdict` (+ `disabled, config_error, input_error`) (`switchboard/switchboard/client.py`)
- local reasoning model: read `choices[0].message.content`, ignore `reasoning_content`; `max_tokens` 2000+ for verdicts (switchboard `CLAUDE.md` gotchas)
- VF invariants ported: `NO_DIFF_MARKER` force-0 pre-judge (`void_furnace/src/void_furnace/benchmark/runner.py:78,904-911`); `JUDGE_PARSE_FAIL_RATE_THRESHOLD = 0.5` per-judge (`runner.py:169,958-1004`); `JUDGE_SAMPLE_K = 3`, median of parsed samples (`benchmark/judge.py`); frozen `AnchorPair` ordering gate (`benchmark/anchors.py` + `tests/test_benchmark/test_anchors.py`)
- judge-core spine: extract-JSON-robustly → validate-axes-present → coerce-types → deterministic-aggregate; parse-failure → drop, don't crash (`dev/.claude/skills/_shared/judge-core.md` §5.6)

### Default run config (illustrative `measure-twice.json`)

```json
{
  "roster": ["general-35b", "coder-30b", "haiku", "sonnet", "opus"],
  "local_base_url": "http://localhost:8080/v1",
  "local_max_tokens": 2000,
  "claude_pool": 2,
  "samples_per_cell": 1,
  "judges": ["sonnet"],
  "max_calls": 500
}
```

### Ledger status semantics

| Status | Meaning |
|---|---|
| MEASURED | ≥1 pre-registered run backs the claim; evidence run ids attached |
| PARTIAL | Some empirical signal, wrong scope or n=1-caveated (e.g. the A3 arms run, the 104-arms cost incident) |
| ASSERTED | Role-shape/heuristic reasoning only — the honest default for nearly all current claims |
| STALE | A cited source file's quoted text drifted since last audit |

### Decision Inventory

Canonical P/D ID registry for the proposal artifact (`/plan-redline` contract: append-only, never renumber; reversed decisions flip status to `changed <date>`).

| ID | P/D | Choice (short) | Status |
|---|---|---|---|
| P1 | P | Delivery: Python package + `mt` CLI | stands |
| P2 | P | Targets: local + Claude tiers from day 1 | stands |
| P3 | P | V1 cuts: no web UI, no CI hooks, no latency/cost benching, no publishing | stands |
| P4 | P | Learning woven into build steps | stands |
| P5 | P | Hybrid data home (project suites with project; ledger here) | stands |
| P6 | P | Storage: JSON + append-only JSONL, no DB | stands |
| P7 | P | Claude via CLI + OAuth | stands |
| P8 | P | Evidence ledger first-class in v1 | stands |
| P9 | P | Flagship domain: tier-routing judge tasks | stands |
| P10 | P | Stdlib-only core | stands |
| P11 | P | Roster: general-35b, coder-30b, haiku, sonnet, opus (Fable one-off) | stands |
| P12 | P | One plan, four phases | stands |
| D1 | D | Acceptance band [5,95] + saturation replacement + kill criterion (local >10 over Opus → instrument audit) | stands |
| D2 | D | Tier-ordering claims on deterministic gold only; rubric never flips a claim alone | stands |
| D3 | D | k=3 median judging; per-judge parse-fail abort at 0.5 | stands |
| D4 | D | Frozen good/garbage anchors in CI per scorer | stands |
| D5 | D | Ledger: MEASURED/PARTIAL/ASSERTED/STALE + quote-hash audit + required preregistration | stands |
| D6 | D | switchboard path dep (import agreement math); schema handshake deferred to v2 | stands |
| D7 | D | Raw responses stored; scoring re-runnable offline | stands |
| D8 | D | Budgets 500 calls/run, claude pool 2, sequential local | stands |
| D9 | D | Fail loud (abort, never warn) incl. startup reachability | stands |
| D10 | D | Flagship: ≥100 items, min-n 8 per tag, ≥4 difficulty buckets | stands |
| D11 | D | Domains investigation = Step 10, gates dataset design | stands |
| D12 | D | Local endpoint operator-started, never auto-spawned | stands |
