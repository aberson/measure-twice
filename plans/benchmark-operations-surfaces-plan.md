# Benchmark Operations and Observatory Surfaces

**Status:** APPROVED (2026-08-07)
**Depends on:** canonical `plan.md` Steps 8-17 for complete ledger/calibration data

## 1. What This Feature Does

Adds an honest benchmark catalog, comparable-run leaderboard, interactive refresh workflow, and
project-local `/change-benchmark` skill, then exposes their read-only data to dev-observatory.

Proposal: [Utility Projects UAT proposal](../../docs/utility-project-surfaces-proposal.html)

## 2. Existing Context

Suites are validated in `measure_twice/suite.py`; runs are append-only under `data/runs`; reporting
lives in `measure_twice/report.py`; execution and resume live in `runner.py`; CLI dispatch is
`cli.py`. The canonical plan explicitly excluded a web UI, so this project emits versioned JSON and
terminal workflows while dev-observatory owns HTML.

## 3. Scope

**In:** list/detail suite metadata; equal-hash leaderboard; refresh selector; change skill;
observatory JSON contract. **Out:** hosted service, cross-hash ranking, silent suite edits, automated
benchmark policy changes, or public publishing.

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `measure_twice/config.py` | extend | validated catalog roots and precedence | current config owner rejects unknown keys |
| `measure_twice/suite.py` | extend | catalog summaries/details and instrument fingerprint | suite schema/hash owner |
| `measure_twice/report.py` | extend | comparable leaderboard rows | current report owner |
| `measure_twice/runner.py` | extend | persist instrument/scorer fingerprint | manifest currently omits it |
| `measure_twice/cli.py` | extend | catalog/leaderboard/refresh verbs | only CLI dispatcher |
| `.claude/skills/change-benchmark/` | create | deliberate benchmark-change workflow | project-local skill convention |
| `tests/` | extend | JSON, comparison, selector, skill contract | mirrors package modules |

## 5. New Components

- `catalog.py`: discovers explicitly configured suite roots and emits schema-versioned list/detail JSON.
- `leaderboard.py`: latest comparable run per suite hash + instrument/scorer fingerprint/model, with sample size, parse failures,
  freshness, and refusal reasons.
- `mt refresh`: terminal selector that shows suite description/hash/last run, then calls the existing
  runner with explicit roster/budget confirmation.
- `/change-benchmark`: inspects impact, updates suite version/content, validates, runs anchors and a
  bounded calibration, and records why the instrument changed.

## 6. Design Decisions

Catalog roots resolve from explicit CLI flag -> config file -> project-local `suites/`; each is
validated under an approved root and bounded against traversal. Leaderboards compare only equal
suite hashes and deterministic instrument/scorer fingerprints. Legacy manifests without the new
fingerprint are `incomparable_legacy`. Refreshing never edits a suite.
Changing a benchmark is a skill because it requires judgment, provenance, and calibration, not a
dashboard POST. JSON uses `null`/status fields for unavailable evidence rather than zero.

## 7. Build Steps

### Step 18: Suite catalog list/detail JSON
- **Problem:** Add `mt catalog list|show --json` over configured suite roots with descriptions,
  domains, scoring, item/tag counts, hash, provenance, and validation status.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Files:** `measure_twice/config.py`, `measure_twice/catalog.py`, `measure_twice/suite.py`,
  `measure_twice/cli.py`, `tests/test_config.py`, `tests/test_catalog.py`
- **Produces:** catalog module, CLI, tests
- **Done when:** every listed ID can be shown and malformed suites remain visible as invalid entries
- **Depends on:** 8

### Step 19: Persist instrument/scorer fingerprints
- **Problem:** Define a deterministic fingerprint over scoring type/config and implementation
  version, persist it in new run manifests, and read legacy manifests as explicitly incomparable.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/suite.py`, `measure_twice/runner.py`, `measure_twice/report.py`,
  `tests/test_suite.py`, `tests/test_runner.py`, `tests/test_report.py`
- **Produces:** versioned manifest field and compatibility reader
- **Done when:** changing scoring metadata changes the fingerprint even when item hash is unchanged
- **Depends on:** 18

### Step 20: Comparable-run leaderboard
- **Problem:** Add `mt leaderboard --json|--format md` with strict suite-hash/fingerprint compatibility,
  sample sizes, freshness, parse failures, and explicit incomparable groups.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/leaderboard.py`, `measure_twice/report.py`, `measure_twice/cli.py`,
  `tests/test_leaderboard.py`
- **Produces:** leaderboard module and reports
- **Done when:** mixed hashes never produce one ranking and golden fixtures reproduce expected order
- **Depends on:** 19

### Step 21: Interactive refresh selector
- **Problem:** Add `mt refresh` to select a catalog entry, inspect its last comparable run, choose a
  roster/budget, and invoke the existing runner/resume path.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Files:** `measure_twice/refresh.py`, `measure_twice/runner.py`, `measure_twice/cli.py`,
  `tests/test_refresh.py`
- **Produces:** terminal workflow
- **Done when:** cancel is side-effect free and a fixture adapter completes one selected refresh
- **Depends on:** 18

### Step 22: `/change-benchmark` skill
- **Problem:** Create the project-local skill and its evals for deliberate suite changes, including
  version/hash impact, provenance, validation, anchors, bounded calibration, and report comparison.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `.claude/skills/change-benchmark/SKILL.md`,
  `.claude/skills/change-benchmark/evals/evals.json`, `README.md`, `CLAUDE.md`
- **Produces:** skill, evals, documentation note in README/CLAUDE
- **Done when:** a fixture change cannot complete without a reason and calibration evidence
- **Depends on:** 20

### Step 23: Observatory artifact contract
- **Problem:** Add an explicit `mt observatory-export --out <path>` that writes one bounded,
  versioned JSON artifact containing catalog list/detail and leaderboard data. Produce contract
  fixtures; dev-observatory alone owns registry/UI integration.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code
- **Files:** `measure_twice/observatory_export.py`, `measure_twice/cli.py`,
  `tests/test_observatory_export.py`, `docs/observatory-contract.md`
- **Produces:** versioned schema docs and contract fixtures
- **Done when:** the exporter and committed fixture validate against the documented schema without
  importing dev-observatory
- **Depends on:** 20, 21

### Step 24: Real benchmark smoke
- **Problem:** Run catalog -> leaderboard -> one bounded refresh -> leaderboard over real stored data.
- **Type:** operator
- **Issue:** #
- **Produces:** operator evidence only
- **Done when:** the refreshed run is traceable by suite hash and no incomparable run enters its rank
- **Depends on:** 23

## 8. Risks and Open Questions

| Item | Risk | Mitigation |
|---|---|---|
| Sparse Phase-A data | empty leaderboard looks broken | explicit no-comparable-runs state |
| Expensive refresh | accidental large sweep | budget/roster confirmation and resume |
| Benchmark gaming | change optimizes known models | provenance, held-out guidance, calibration |

## 9. Testing Strategy

Use valid/invalid suites, equal/mixed hashes, sparse runs, parse failures, cancel paths, and DI model
adapters. The final smoke uses real filesystem data and the production runner.

## Appendix: Decision Inventory

| ID | P/D | Choice | Status |
|---|---|---|---|
| P2 | P | Add leaderboard, benchmark explorer, refresh, and change-benchmark surfaces | accepted |
| D2 | D | Emit bounded JSON artifacts; dev-observatory owns HTML and registry wiring | accepted |
| D4 | D | Keep refresh/change as project-owned terminal/skill actions | accepted |
| D8 | D | Compare runs only when suite hash and instrument/scorer fingerprint match | accepted |
