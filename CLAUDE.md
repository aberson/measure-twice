# measure-twice — CLAUDE.md

## Project overview

Local benchmarking toolkit (Python package + `mt` CLI, operator-only): create, run, and interpret model benchmarks across the dev/ workspace — evidence ledger for tier-routing claims (MEASURED vs ASSERTED), a discriminative flagship dataset (every roster model scores strictly inside (0,100)), and per-model capability profiles. It also hosts a second, provider-neutral instrument: a **coding-agent benchmark** that runs real coding agents (`codex-luna` vs `claude-sonnet`) against sealed task bundles inside a fail-closed Linux sandbox and scores the diff they produce. Out of scope v1: web UI, CI integration, latency/cost benchmarking, public publishing. Canonical plan: [`plan.md`](plan.md) (Steps 1-17). Coding-agent feature plan: [`documentation/coding-agent-benchmark-plan.md`](documentation/coding-agent-benchmark-plan.md) (Steps 25-55).

## Stack

| Layer | Tool |
|---|---|
| Runtime | Python ≥3.12 + uv, hatchling build |
| Deps | **stdlib-only** + `switchboard` via uv path dep (`../switchboard`) — keep it that way |
| Package | `measure_twice/` at repo root (NOT `src/`), tests under `tests/` |
| Local models | OpenAI-compatible endpoint `localhost:8080` (llama-swap, WSL2; operator-started, client-only) |
| Claude tiers | `claude` CLI subprocess with subscription OAuth (no API key) |
| Storage | JSON suites + append-only JSONL runs + markdown reports; no DB |
| Agent sandbox | Linux FD capabilities (`openat2`) + cgroup v2 via `systemd-run --user --scope` + private tmpfs + Bubblewrap 0.11.2, on WSL2 Ubuntu 24.04 / ext4 |

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
uv run mt report <run_id> --html   # item-level page: every item, raw response, scorer reason
.\scripts\report-latest-html.ps1   # same for the newest run, then open it (observatory verb)
uv run mt claims audit
uv run mt agent validate suites/agents/smoke --structure-only
.\scripts\test-agent-bench-wsl.ps1 -Distribution Ubuntu   # real Linux containment gate
```

`uv run pytest` from Windows reports the Linux containment cases as explicit skips; they are never silently absent. The WSL script stages the tree onto ext4 and runs them for real, and its JUnit gate forbids a selected case skipping.

## Directory layout

```
measure_twice/           # package: config, suite, runner, ledger, author, report, cli
  report_html.py         # item-level transparency page + report_template.html (wheel data)
  adapters/              # local.py (OpenAI-compat), claude_cli.py (subprocess + OAuth)
  scoring/               # deterministic.py (verdict/exact + spine), judge.py (k=3 median)
  agent_bench/           # models, analysis, suite (strict loaders + hashing), cli,
                         #   _linux_capabilities.py, isolation.py, process.py (the substrate)
suites/                  # model suites (tier-judging-v*, smoke) + agents/<name>/ bundles
profiles/                # agent model registry + execution profile (OUTSIDE suite identity)
analysis-plans/          # preregistered analysis plans, hashed into the instrument
data/runs|ledger|reports # append-only runs, claims.jsonl evidence ledger, rendered reports
docs/research|methodology|investigations   # seed recon, learning notes, domain investigation
docs/agent-benchmark/    # agent-suite preregistrations
documentation/           # coding-agent-benchmark-plan.md (the Steps 25-55 feature plan)
scripts/                 # test-agent-bench-wsl.ps1 (the real Linux containment gate)
tests/anchors/           # frozen good/garbage pairs per scorer (CI ordering gate)
tests/agent_bench/       # agent-bench units + Linux containment canaries (marker: linux_isolation)
```

## Architecture summary

Suites carry ALL item content (no prompt templates in adapters — the fallback-prompt bug class is structurally absent). Runner sweeps suite × roster × samples through DI-seamed adapters, appending JSONL rows as produced (cell-level resume, call budgets, no-response force-scored 0 before any judging). Scoring is re-runnable offline from stored raw responses. Deterministic scorers conform to judge-core §5.6 (parse-failure → scored 0 + recorded, never crash); the rubric judge is k=3 median with a per-judge parse-fail gate (>0.5 aborts). Agreement math is `switchboard.harness.aggregate_agreement` — imported, never re-implemented. The evidence ledger (`data/ledger/claims.jsonl`) ties every tier-routing claim to sources (quote-hashed for staleness) and evidence runs; `docs/tier-benchmark-map.md` is rendered from it. Doctrine: conform to `_shared/judge-core.md` + `.claude/rules/measurement-validity.md`; reference, never restate.

**`agent_bench` (the coding-agent instrument)** is a parallel stack with the same doctrine and no shared runtime with the model sweep. Suite bundles are directories (`suite.json` listing per-task `task.json` files, each naming its own prompt, `seed/`, `oracle/`, reference patch, and allowed/protected globs) loaded by strict fail-loud parsers that reject unknown/missing keys, unsafe or duplicate identifiers, escaping paths, symlinks, and arbitrary task commands. Instrument identity is a recursive hash over task-asset bytes; **model profiles are deliberately outside it**, so adding a provider never silently changes what the instrument measures. `mt agent validate --structure-only` is cross-platform and never executes suite code.

The Linux substrate rests on one **kernel-object-identity invariant**: every caller-supplied filesystem source is opened once via `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)` and thereafter referred to only by that owned descriptor (`LinuxPathCapability`), so a later rename, unlink, or symlink swap cannot redirect a mount, a cwd, a capture scan, or a resource walk. Processes launch through `systemd-run --user --scope` into a fresh transient cgroup (`LinuxResourceGuard`) with a private tmpfs per evaluator repetition (`EvaluatorScratch`), sandboxed by Bubblewrap using `--bind-fd`/`--ro-bind-fd`. Resource enforcement is explicitly **two-layer and self-labelling**: memory/pids/tmpfs ceilings are hard host guards read back before the target is released and record `hard-guard` provenance, while cumulative CPU and logical-tree thresholds are sampled scoring rules that record `sampled-threshold` provenance and may overshoot. There is no polling-only or path-bind fallback: a missing or incompatible dependency raises `IsolationUnavailableError` and fails the gate rather than degrading.

## Current state

**Phase A (core engine) COMPLETE** (2026-07-17): Steps 1–7 shipped + merged to master (issues #1–#7 closed) — `config` resolver, `suite` schema + canonical item-hash + `mt validate`, model `adapters` (local OpenAI-compat + `claude` CLI, both behind DI seams), the sweep `runner` (append-only JSONL, cell-level resume, budgets, no-response force-0), deterministic verdict/exact `scoring` + the §5.6 parse spine + frozen anchors, the k=3-median rubric `judge` (per-judge parse-fail gate), and `report` + `mt smoke`. Step M1 local-endpoint smoke (#18) is closed.

**Steps 8–12 LANDED on master** (2026-08-10, merged as `12dbb6c`; issues #8–#12 closed) — the `ledger` module + `mt claims list|audit|render`, the populated 28-claim evidence ledger + rendered `docs/tier-benchmark-map.md`, the benchmark-domains investigation, the item-authoring pipeline (`author.py`), and flagship dataset v0 `suites/tier-judging-v0.json`.

Ledger citations are quote-hashed against the *shared dev workspace*, so a file move outside this repo can orphan them: the skill-mesh Phase 7 cutover renamed `.claude/skills-gpt/<skill>/SKILL-core.md` → `.claude/skills/<skill>/core.md` and stranded 9 claims. They were re-anchored at source (identical quote text, same digests). `mt claims audit` is the real freshness gate — and note it *mutates* the ledger, writing `STALE` back for any claim it cannot verify. If `test_every_citation_is_current_and_the_map_is_exactly_rendered` fails, fix the citation, never the assertion.

**Coding-agent benchmark Steps 25–26 LANDED on master** (2026-08-24; issues #27–#28 closed) — strict `agent_bench` input contracts (`ModelSpec`, `AnalysisPlan`, `ExecutionProfile`, `AgentSuite`, `AgentTask`) with recursive instrument hashing and `mt agent validate`, plus the fail-closed Linux execution substrate: `openat2` FD capabilities, a transient `systemd-run --user --scope` cgroup, per-repetition private tmpfs, and Bubblewrap FD-bind sandboxing. The substrate can define an instrument and contain a process; **no provider adapter and no inference call exists yet** — those are Steps 27–29.

Gate evidence at wrap: native suite from the repo root **462 passed, 110 skipped, 0 failed** (the skips are the Linux-only containment cases, explicitly deselected on Windows); `ruff check` + `ruff format --check` clean over 55 files; `mypy --strict` clean over 25 source files; `uv build` clean.

> **The WSL containment gate is not reliably green: 9/10 (`0,0,0,1,0,0,0,0,0,0`) at wrap time,** run post-`6706fb6` on 2026-08-24. The single red landed on run 4, whose output was not captured; runs 7-10 were captured but all passed, so the failing case is **still unidentified**. This is the *sixth* appearance of one shape in this branch — a test asserting on a quantity it does not control (see [[test-asserts-uncontrolled-quantity]]); issue #58 item 3 already names a known red-when-healthy race in `test_linux_scope_absence_interval_restarts_when_the_path_reappears` as a candidate. **Never call this gate green off one run.** Run it 6–8× and report the pass *rate*; retrying until green hides exactly the defect you need to see. Step 27 builds more canaries on this substrate — identify and fix the remaining flake before adding to it.

**Next: canonical `plan.md` Steps 13–17** — 13 calibration sweep (observation run), 14 discriminative calibration + dataset iteration, 15 capability profiling, 16 first ledger measurements (observation run), 17 methodology rollup + README. Steps 13 and 16 are operator observation runs needing the local endpoint up. `measure_twice/analyze/` (calibrate, profile, agreement) does not exist yet — Steps 14–15 create it.

Coding-agent Steps 27–55 are also unblocked and may proceed in parallel: that plan states it does not require the pending Steps 13–24, which is why 25–26 were built ahead of them.

> **Do NOT build `plans/benchmark-operations-surfaces-plan.md` (Steps 18–24) yet.** That plan is APPROVED but *blocked*: its surfaces (suite catalog, instrument fingerprints, comparable-run leaderboard, refresh selector, `/change-benchmark`, observatory contract, real benchmark smoke) all read measurement output that Steps 13–17 have not produced. Steps 13–17 have **not started**. Build 18–24 before them and the surfaces render an empty or fabricated benchmark.

Build on the pinned Opus default.

## Environment requirements

- Windows 11, PowerShell primary; uv on PATH.
- Python ≥3.12 (matches switchboard's floor).
- `claude` CLI authenticated via subscription OAuth (bench sweeps consume subscription capacity — budgets + resume built in).
- Local sweeps only: llama-swap endpoint on `localhost:8080` in WSL2, operator-started via `..\switchboard\scripts\start-offload.ps1` (never auto-spawned; `general-35b` is a reasoning model — `max_tokens` 2000+, read `content` not `reasoning_content`).
- Port note: `localhost:8080` is consumed (client-only), never bound — intentional share with switchboard/void_furnace.
- Coding-agent benchmark only: WSL2 **Ubuntu 24.04 on ext4** (every workspace and staging path must be on ext4, not `/mnt/c`), unified cgroup v2 delegation with a working user service manager at `/run/user/<uid>/bus`, unprivileged private mount namespaces supporting tmpfs `size` + `nr_inodes`, and **Bubblewrap 0.11.2 built from upstream at `/usr/local/bin/bwrap`** — Ubuntu's stock 0.9.0 package is unsupported because it lacks the FD-bind semantics the containment invariant depends on. Version text is never trusted; the harness runs live behavioral probes for `--bind-fd`/`--ro-bind-fd`. Any shortfall raises `IsolationUnavailableError` and fails the WSL gate rather than skipping it. No provider authentication or live model call is needed to run the substrate gate.
