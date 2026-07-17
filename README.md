# measure-twice

A local benchmarking toolkit for deciding **which model should do which job — with measurements instead of assertions**. It provides a Python package + `mt` CLI to author benchmark suites, sweep them across a roster of models (local OpenAI-compatible endpoints and Claude tiers), score results through calibrated instruments, and keep an **evidence ledger** that ties every model-routing claim to the run that backs it (or honestly marks it `ASSERTED`). The flagship deliverable is a *discriminative* dataset: scored 0–100, designed and iterated so no roster model saturates either end.

> **Status:** plan-complete, pre-code. The full design lives in [`plan.md`](plan.md); build starts with Phase A (core engine).

## Why

An audit of this workspace's model-routing policies found exactly **one** genuine head-to-head quality measurement behind every "which tier for which task" decision — the rest is role-shape heuristics (see [`docs/research/tier-skills-benchmark-map.md`](docs/research/tier-skills-benchmark-map.md)). measure-twice makes that gap enumerable and converts it, one pre-registered run at a time.

## Stack

| Layer | Tool | Why |
|---|---|---|
| Runtime | Python ≥3.12 + uv | Workspace standard |
| Deps | stdlib-only core (+ `switchboard` path dep) | Agreement/kill math stays one-source-of-truth |
| CLI | `mt` (argparse entry point) | Scriptable from any project |
| Local models | OpenAI-compatible endpoint (`localhost:8080`, llama-swap) | Reuse, don't rewrite |
| Claude tiers | `claude` CLI subprocess (subscription OAuth) | No API key; budgets + resume built in |
| Storage | JSON suites · append-only JSONL runs · markdown reports | git-diffable, no DB |
| Quality | pytest · ruff · mypy --strict | Frozen good/garbage anchors gate every scorer in CI |

## Prerequisites

- Windows 11 (PowerShell) — the reference environment; nothing is Windows-locked by design
- Python ≥3.12 with `uv`
- `claude` CLI authenticated (for Claude-tier sweeps)
- Optional, for local-model sweeps: an OpenAI-compatible endpoint on `localhost:8080` (operator-started; never auto-spawned)

## Setup

```powershell
git clone https://github.com/aberson/measure-twice.git
cd measure-twice
uv sync --extra dev
uv run pytest -q                 # includes anchor ordering gates
uv run mt validate suites/smoke.json
uv run mt smoke --claude         # 2-item real end-to-end run
```

(Commands land with Phase A — steps 1–7 of the plan.)

## Key design decisions

- **Fail loud, never warn.** Unreachable endpoint, malformed suite, tripped judge parse-gate — runs abort. A silently degraded bench produces numbers indistinguishable from real ones.
- **Score the production artifact.** Raw responses stored verbatim; no-response force-scored 0 before any judging; scoring re-runs offline (`mt score`) without burning model calls.
- **Calibrate before comparing.** Every scorer ships a frozen good/garbage anchor pair; CI asserts `score(good) > score(garbage)` forever. The flagship dataset carries an acceptance band ([5, 95] for every roster model) and a pre-registered kill criterion.
- **No judge circularity on the claims that matter.** Tier-ordering claims rest on deterministic gold answers only; LLM rubric judging (k=3, median, per-judge parse gate) profiles capabilities but never flips a ledger claim alone.
- **Evidence ledger as first-class.** Claims carry `MEASURED / PARTIAL / ASSERTED / STALE`, quote-hashed source citations, and pre-registration sentences written before the run.

## Structure

```
measure_twice/          # package: config, suite, runner, ledger, author, report, cli
  adapters/             # local endpoint + claude CLI, DI-seamed for offline tests
  scoring/              # deterministic scorers + k=3 median rubric judge
  analyze/              # calibrate, profile, agreement (delegates to switchboard)
suites/                 # cross-cutting suites (tier-judging-v*, smoke)
data/                   # runs (gitignored) · ledger/claims.jsonl (tracked) · reports (gitignored)
docs/research/          # seed audits the plan is built on
docs/methodology/       # numbered learning notes, one+ per phase
tests/anchors/          # frozen good/garbage pairs per scorer
```

Full schemas (suite JSON, run JSONL, claim rows), phase-by-phase build steps, and the risk register: [`plan.md`](plan.md).
