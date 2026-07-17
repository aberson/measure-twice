# measure-twice — CLAUDE.md

## Project overview

Local benchmarking toolkit (Python package + `mt` CLI, operator-only): create, run, and interpret model benchmarks across the dev/ workspace — evidence ledger for tier-routing claims (MEASURED vs ASSERTED), a discriminative flagship dataset (every roster model scores strictly inside (0,100)), and per-model capability profiles. Out of scope v1: web UI, CI integration, latency/cost benchmarking, public publishing. Canonical plan: [`plan.md`](plan.md).

## Stack

| Layer | Tool |
|---|---|
| Runtime | Python ≥3.12 + uv, hatchling build |
| Deps | **stdlib-only** + `switchboard` via uv path dep (`../switchboard`) — keep it that way |
| Package | `measure_twice/` at repo root (NOT `src/`), tests under `tests/` |
| Local models | OpenAI-compatible endpoint `localhost:8080` (llama-swap, WSL2; operator-started, client-only) |
| Claude tiers | `claude` CLI subprocess with subscription OAuth (no API key) |
| Storage | JSON suites + append-only JSONL runs + markdown reports; no DB |

## Commands

```
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run mypy --strict measure_twice
uv run mt validate suites/smoke.json
uv run mt smoke --claude
uv run mt run --suite suites/tier-judging-v0.json --models general-35b,haiku,sonnet
uv run mt report <run_id>
uv run mt claims audit
```

## Directory layout

```
measure_twice/           # package: config, suite, runner, ledger, author, report, cli
  adapters/              # local.py (OpenAI-compat), claude_cli.py (subprocess + OAuth)
  scoring/               # deterministic.py (verdict/exact + spine), judge.py (k=3 median)
  analyze/               # calibrate.py, profile.py, agreement.py (delegates to switchboard)
suites/                  # cross-cutting suites (tier-judging-v*, smoke)
data/runs|ledger|reports # append-only runs, claims.jsonl evidence ledger, rendered reports
docs/research|methodology|investigations   # seed recon, learning notes, domain investigation
tests/anchors/           # frozen good/garbage pairs per scorer (CI ordering gate)
```

## Architecture summary

Suites carry ALL item content (no prompt templates in adapters — the fallback-prompt bug class is structurally absent). Runner sweeps suite × roster × samples through DI-seamed adapters, appending JSONL rows as produced (cell-level resume, call budgets, no-response force-scored 0 before any judging). Scoring is re-runnable offline from stored raw responses. Deterministic scorers conform to judge-core §5.6 (parse-failure → scored 0 + recorded, never crash); the rubric judge is k=3 median with a per-judge parse-fail gate (>0.5 aborts). Agreement math is `switchboard.harness.aggregate_agreement` — imported, never re-implemented. The evidence ledger (`data/ledger/claims.jsonl`) ties every tier-routing claim to sources (quote-hashed for staleness) and evidence runs; `docs/tier-benchmark-map.md` is rendered from it. Doctrine: conform to `_shared/judge-core.md` + `.claude/rules/measurement-validity.md`; reference, never restate.

## Current state

Plan written (2026-07-15), no code yet. Next: `/plan-expedite --plan measure-twice/plan.md`, then `/build-phase` Phase A (Steps 1–7).

## Environment requirements

- Windows 11, PowerShell primary; uv on PATH.
- Python ≥3.12 (matches switchboard's floor).
- `claude` CLI authenticated via subscription OAuth (bench sweeps consume subscription capacity — budgets + resume built in).
- Local sweeps only: llama-swap endpoint on `localhost:8080` in WSL2, operator-started via `..\switchboard\scripts\start-offload.ps1` (never auto-spawned; `general-35b` is a reasoning model — `max_tokens` 2000+, read `content` not `reasoning_content`).
- Port note: `localhost:8080` is consumed (client-only), never bound — intentional share with switchboard/void_furnace.
