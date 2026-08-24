# Provider-Neutral Coding Agent Benchmark

**Status:** Steps 25-26 DONE - strict agent inputs (2026-08-22) and the Linux process and isolation substrate (2026-08-24) are shipped and merged. Step 27 is next.

**Roadmap allocation:** Steps 25-55. Canonical `plan.md` owns Steps 1-17 and the approved
operations plan owns Steps 18-24.

**Depends on:** shipped core engine patterns from Steps 1-12; implementation does not require the
pending Steps 13-24.

**Initial comparison:** `codex-luna` versus `claude-sonnet`

**Deferred expansion:** `claude-haiku`, reusing the same 32 task-asset bytes through a new
execution-profile-bound suite descriptor and the same adapter contract, runner, and report

## 1. What This Feature Does

Build an operator-invoked benchmark that gives Codex Luna and Claude Sonnet the same clean Python
repository task, runs each native CLI coding agent in a fresh constrained environment, captures the
repository change it actually produced, and scores that change with deterministic held-out tests in
a second clean evaluator copy. The first 12-task suite calibrates the harness; a fresh 32-task v1
suite supports the pre-registered Luna-versus-Sonnet observation. Model selection is supplied by an
explicit provider-neutral registry and reporting is N-model/pairwise, so a later Haiku-versus-Sonnet
quality-routing study requires a new profile and a fresh three-arm run, not a runner redesign.

The operator is the immediate user. The resulting evidence also benefits developers choosing which
coding agent should receive which class of workspace task. The trigger is the request for measured
Luna-versus-Sonnet guidance now and same-family Haiku-versus-Sonnet routing guidance later.

The claim this feature can support is deliberately end-to-end:

> Under the same seed repository, task prompt, filesystem scope, network policy, runtime, and
> wall-clock limit, which native CLI coding agent more often produces a patch that passes the
> held-out deterministic evaluator?

It does not isolate a base model from the CLI's system prompt, tools, or agent loop.

## 2. Existing Context

- `measure_twice/suite.py:182-342` defines prompt-response `ScoringSpec`/`Item`/`Suite`: each item contains
  `prompt` and `expected`, and the suite selects `verdict`, `exact`, or `rubric` scoring. It has no
  seed repository, working directory, patch, evaluator, or allowed-path contract.
- `measure_twice/runner.py:605-748` sweeps prompt cells and stores returned text. Its dispatch is a
  hard-coded Claude-alias check (`CLAUDE_ALIASES` at lines 89 and 707); every other model goes to the
  local OpenAI-compatible endpoint. Adding `gpt-5.6-luna` to a roster today would silently select
  the wrong backend.
- `measure_twice/adapters/claude_cli.py:86-105,192-231,242-345` is a response-only subprocess
  adapter. Its DI seam does not carry a working directory or controlled environment, and the
  current invocation at line 283 is designed to capture one JSON answer, not a mutated workspace.
- `measure_twice/runner.py:254-392,605-748` and `measure_twice/report.py:136-347` establish useful patterns:
  immutable manifests, append-as-produced JSONL, torn-tail handling, content-hash comparability,
  resumable cells, deterministic Markdown/JSONL output, and fail-loud corruption handling. The new
  pipeline follows these patterns but uses a sibling store because its row and artifact schemas are
  materially different.
- `measure_twice/cli.py:545-813` has one argparse parser and a handler table, with nested subcommand
  precedents in `claims` and `author`. `CliDeps` at lines 109-127 is the offline DI seam used by
  production CLI integration tests.
- The flagship `tier-judging-v0` is intentionally a deterministic reviewer-label instrument. The
  decision record in `docs/investigations/benchmark-domains.md:173-196` explicitly excludes code
  authorship, which requires a separate instrument and preregistration.
- The repository is Python 3.12, stdlib plus the existing `switchboard` path dependency, with
  pytest, Ruff, and strict mypy gates (`pyproject.toml`). No new Python dependency is needed.
- Canonical Steps 13-17 remain pending, and `plans/benchmark-operations-surfaces-plan.md` has already
  reserved Steps 18-24. This feature therefore starts at Step 25 and does not change either plan's
  identities or default five-model prompt roster.

## 3. Scope

### In scope

- Native noninteractive Codex CLI and Claude Code agent execution.
- `gpt-5.6-luna` and Sonnet as the only live v1 comparison arms.
- A strict model registry that dispatches by provider rather than model-name heuristics.
- A separate agent-suite schema for small, dependency-free Python 3.12 repository tasks.
- WSL2/Linux live execution with WSL-local disposable workspaces and fail-closed filesystem/network
  containment; native Windows remains supported for suite authoring, `--structure-only` validation,
  reporting, and offline tests, but never executes task, oracle, reference, or submitted code.
- A sandboxed clean evaluator that treats model-authored code as untrusted and denies host reads,
  outside writes, credential access, descendant escape, and all network access.
- Fresh workspace and fresh agent session for every `(task, model, sample)` cell.
- Authoritative post-run Git patch capture, clean-clone patch application, protected-path checks,
  hidden-oracle injection, and deterministic repeated evaluation.
- Immutable run manifests, append-only summary rows, content-addressed cell artifacts, budgets,
  seeded paired order, and hash-verified resume.
- Deterministic macro task success, paired outcome counts, cluster-aware uncertainty, and generic
  superiority/non-inferiority decision policies.
- A 12-task calibration pilot and a fresh 32-task Luna-versus-Sonnet v1 observation suite.
- A dormant third-model contract test proving a future Claude/Haiku profile uses the existing
  Claude adapter and N-model report path.

### Explicitly out of scope

- Changing `tier-judging-v0`, the prompt-response `Suite`, `RunRow`, or their existing run-store
  compatibility.
- Adding Luna or Haiku to `DEFAULT_ROSTER`, or changing pending canonical Steps 13-17.
- A live Haiku observation in this phase. The expansion protocol is designed and tested, but its
  model profile and run are a later, separately pre-registered measurement.
- LLM rubric judging, subjective style grading, or unreviewed/solely model-generated gold. Build
  agents may draft task artifacts only under the provenance and two-human adoption rule in §6.9.
- Claims about code maintainability beyond deterministic task invariants.
- Browser, MCP, plugin, external-API, package-install, or network-dependent tasks.
- Native-Windows live agent cells when both providers cannot satisfy the same fail-closed sandbox
  contract.
- Equal-token or equal-reasoning claims. Provider effort controls are recorded, but are not assumed
  to be semantically equivalent.
- Cost or latency rankings. Elapsed time and usage are retained as diagnostics only, consistent with
  the canonical v1 boundary.
- Multi-language tasks, full SWE-bench-scale repositories, parallel agent execution, public
  publishing, dashboards, and observatory integration.

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `measure_twice/cli.py` | extend | Register the `mt agent` validate, doctor, run, report, and evidence commands and carry one optional agent-process DI seam. | Read `CliDeps` at lines 109-127 and `_build_parser`/`main` at lines 545-813. Grep found production consumers at lines 172, 248, 288, 472, 546, and 792-800; test constructors are `tests/test_report.py:319,332,344,359,375`, `tests/test_runner.py:522,559,601,794,801`, `tests/test_scoring.py:532,558,607`, and `tests/test_judge.py:526`. The new field must have a default so none of those callers becomes positional or required. |
| `tests/conftest.py` | extend | Add workspace-aware fake agent processes and artifact fixtures for production-path CLI tests. | Read current `StubAdapters` at lines 52-97; grep found its Claude factory import/use at lines 14 and 84-93 and direct `StubAdapters` consumers in `tests/test_runner.py` and `tests/test_report.py`. Scoring/judge tests construct their own `CliDeps` seams. Existing fixture behavior remains unchanged. |
| `measure_twice/agent_bench/_linux_capabilities.py` | create | Own exact Linux file/directory descriptors across validation, Bubblewrap mounting, subprocess cwd selection, and resource-tree traversal so no security decision authorizes a later pathname lookup. | The blocked Step-26 implementation returns strings from `isolation.py:_preflighted_path`, later reopens them for mounts, and queues `DirEntry.path` in both tree scanners. `suite.py` already demonstrates the contrasting open-relative, no-follow, identity-check pattern for contained inputs. |
| `.gitignore` | extend | Keep default-path agent runs, workspaces, confirmations, traces, exports, and reports local while preserving tracked suite/evidence bundles. | Read the complete file: it currently ignores only `data/runs/`, `data/reports/`, caches, and local session state. Add `data/agent-runs/`, `data/agent-workspaces/`, `data/agent-confirmations/`, `data/agent-reports/`, and `data/exports/`; the operator protocol instead uses the automatically untracked Git-common state home. Do not ignore `suites/agents/`, `profiles/`, `analysis-plans/`, or `docs/agent-benchmark/evidence/`. |
| `pyproject.toml` | extend | Register the `linux_isolation` pytest marker used by the real WSL gate; package/runtime dependencies remain unchanged. | Read the complete pytest configuration: it currently declares only `testpaths` and `addopts`, so the new marker must be explicit rather than warning-only. |
| `README.md` | modify | Add the agent benchmark boundary, WSL2 live prerequisite, and verified commands after the real v1 run. | Read current setup/command/structure sections; they list only prompt-suite `validate`, `run`, `report`, and `smoke` paths and still carry stale Phase-A status text. |
| `CLAUDE.md` | modify | Record the new package layout, commands, live-environment constraint, current result boundary, and Haiku expansion protocol. | Read the complete current file; commands and architecture mention only `adapters/{local,claude_cli}.py`, prompt suites, and `data/runs`. |
| `plan.md` | modify | Register the feature's Steps 25-55 number allocation without claiming that number order is execution order or renumbering existing work. | Grep/read confirmed automated Steps 1-17 at `plan.md:259-428` and manual M1-M3 at lines 435-471; no Step 25+ currently exists. Update only after Step 55 evidence exists. |

No existing prompt-engine schema or callable is changed. Specifically, `measure_twice/config.py`,
`measure_twice/suite.py`, `measure_twice/runner.py`, `measure_twice/report.py`,
`measure_twice/adapters/claude_cli.py`, and `measure_twice/scoring/*` remain compatibility boundaries.
Their producers and consumers were grepped: extending the current `RunRow`, `Suite`, Claude
`RunnerFactory`, or `CallBudget` would affect dozens of prompt-run, report, scoring, judge, and
fixture call sites. The sibling pipeline deliberately avoids that migration.

This feature plan is serialized against the other pending plans: it may be built first because the
operator has explicitly prioritized the coding-agent comparison, but no two plans may modify the
shared CLI or project docs concurrently. After this feature merges, canonical Steps 13-17 rebase on
it; the blocked operations Steps 18-24 still wait for Step 17 and then rebase on both. This plan does
not extend the prompt-run semantics of Steps 19-20. Any future catalog integration for agent runs is
a new post-Step-55 phase. Coding-agent notes live under `docs/agent-benchmark/`, not the numbered
`docs/methodology/01-08` sequence owned by canonical Step 17.

## 5. New Components

### Agent benchmark package

`measure_twice/agent_bench/` is a sibling pipeline:

```text
measure_twice/agent_bench/
  __init__.py
  models.py                 # ModelSpec registry, selected-profile hash, provider dispatch
  analysis.py               # strict machine-readable analysis policy and scope validation
  suite.py                  # AgentSuite/AgentTask loaders, containment checks, instrument hash
  _linux_capabilities.py    # owned FD capabilities, identity checks, and FD-relative traversal
  process.py                # cwd/env-aware subprocess contract and process-tree termination
  isolation.py              # Linux agent/evaluator isolation profiles and canary contracts
  evaluator.py              # materialize, patch capture/apply, oracle injection, repeated tests
  evidence.py               # strict cross-worktree evidence validation and scrubbed import
  runner.py                 # paired schedule, budgets, artifacts, append-only rows, resume
  report.py                 # macro scores, cluster bootstrap, pairwise/routing decisions
  cli.py                    # nested argparse registration and handlers
  adapters/
    __init__.py
    base.py                 # AgentAdapter protocol and request/result/fingerprint dataclasses
    codex_cli.py            # Codex JSONL adapter
    claude_cli.py           # Claude stream-JSON adapter
```

The existing response-only adapters remain unchanged. The new process contract takes an immutable
request containing `argv`, UTF-8 stdin, an owned cwd capability on Linux, sanitized environment,
and timeout, then returns stdout/stderr/exit/elapsed data. Display paths remain available for
diagnostics and non-authorizing policy comparisons, but mounts, cwd entry, and recursive traversal
consume the same opened descriptors that passed validation. The launch object is one-shot and owns
every descriptor until the child has inherited its copies; all success and error paths close the
parent copies deterministically. Both real providers and offline fakes implement the same
`AgentAdapter.preflight()` and `AgentAdapter.invoke()` interface.

`AgentCliDeps` is defined inside `measure_twice.agent_bench` and supplied to a registration
function that accepts the root parser/handler table. Root `CliDeps` owns one optional/defaulted
`AgentCliDeps` field and imports the registration function; `agent_bench.cli` never imports
`measure_twice.cli`. This preserves the production-path DI seam without creating a circular import.

### Model registry

`profiles/agent-models-candidates.json` is strict, versioned, and independent of suite content. It
contains `models[]` only. Each entry contains a safe operator name, provider (`codex-cli` or
`claude-cli`), requested model, nullable effort setting, and execution-profile identifier. Initial
live entries are `codex-luna` and `claude-sonnet`. The selected entries—not unrelated registry
entries—are snapshotted and hashed in the run manifest.

The schema accepts multiple entries for one provider and nullable effort. A committed offline
fixture adds a third Claude entry representing Haiku and proves that dispatch, scheduling,
manifests, and all-pairs reporting need no special Haiku branch. The later live expansion must add
a currently verified, pinned Haiku identifier and its supported effort semantics before making any
calls.

`profiles/agent-execution-v1.json` is the sole source of limits, retry, sandbox, and schedule
mechanics. Its `id` must equal the `execution_profile_id` in the suite and in every selected
model entry; any mismatch fails before a run is created. `--execution-profile` selects a file, not
an override layer: CLI flags cannot replace values inside it, and resume uses the snapshotted file.

After the pilot, `profiles/agent-models-v1.json` freezes only the qualified selected Luna/Sonnet
model entries for the v1 observation; their identity evidence remains solely in the committed
`QualificationBundle`. The candidate registry is never rewritten in place to masquerade as the
frozen profile.

### Analysis plans

Machine-readable decision policies live under `analysis-plans/`. The smoke and pilot plans use
`policy: "none"`; `coding-agent-v1.json` binds Luna as candidate, Sonnet as reference, and the sole
confirmatory overall superiority scope. Human preregistration documents cite the canonical JSON
hash but never replace this execution input. A later Haiku plan uses the same schema with Haiku as
candidate, Sonnet as reference, and its preregistered simultaneous family scopes.

### Agent suite bundles

Agent suites live under `suites/agents/<suite-name>/`:

```text
suite.json
tasks/<task-id>/
  task.json
  prompt.md
  seed/                     # the only task content copied into the agent workspace
  oracle/                   # injected only into the clean evaluator
  reference.patch           # never copied into the agent workspace
```

`suite.json` fixes the schema version, runtime (`python`, `3.12`), evaluator version, execution
limits, scoring policy, and task manifest paths. Each task records safe ID, tags, `cluster_id`,
difficulty prior, prompt/seed/oracle/reference paths, allowed-change globs, protected paths, and
provenance. Paths are relative, non-symlinked, and contained beneath the task bundle. Task manifests
cannot supply arbitrary shell strings; v1 uses one fixed harness-owned evaluator argv.

The `instrument_hash` covers canonical suite/task JSON plus relative paths and bytes for every
prompt, seed file, hidden oracle, reference patch, and constraint. Any asset drift changes the
instrument identity. `mt agent validate` also runs the untouched seed, no-op/garbage patch, and
reference-patch anchors before declaring a task valid.

### Run store

```text
data/agent-runs/<run_id>/
  manifest.json
  status.json
  inputs/
    qualification-bundle.json
    preregistration.md
    analysis-plan.json
    validity-ledger.json          # observation only
  suite-snapshot/
  rows.jsonl
  attempt-reservations.jsonl
  cells/<cell-id>/
    cell.json
    row.json
    patch.diff
    workspace-files.json
    attempts/<attempt_k>/agent/
        invocation.json
        final.txt
        events.jsonl
        stdout.txt
        stderr.txt
    evaluation/
      result.json
      repetitions/<1|2>/
        stdout.txt
        stderr.txt
```

Every `Artifact.path` above is relative to the run root; the suite directory uses a `TreeArtifact`
covering its complete canonical file set. Fresh-run confirmation copies all external immutable
inputs into `inputs/` and the suite into `suite-snapshot/` before publishing `manifest.json`; resume
reads only these run-local bytes and the manifest's embedded profile/execution objects.

The immutable manifest records the instrument, harness, and environment fingerprints; selected
model profile snapshots; run-local qualification, preregistration, and optional validity descriptors;
task hashes; sample count; paired-order
seed; resource limits; preregistration text/hash; and cell/wall-clock budget limits. The separately
atomic `status.json` records mutable state and budget consumption and is excluded from
`manifest_sha256`. Summary rows record
cell/task/cluster identity, requested and resolved model identity, provider and CLI version, sample,
termination reason, artifact paths and hashes, changed paths, deterministic score/test counts,
containment flags, elapsed times, and error class.

Verified logical terminal rows and their linked attempt artifacts are authoritative for completed
blocks and terminal-cell counts; the append-only, fsync-before-invocation
`attempt-reservations.jsonl` is authoritative for provider-attempt consumption. `status.json` is an
atomic cache of those counters plus lifecycle state. Under the run lock, fresh/resume recomputes
counters from the manifest schedule, verified rows, and valid reservations before doing work and
atomically repairs a stale status file.
A report never mutates the run: it derives completeness from verified rows, reports status drift,
and suppresses a verdict until a runner reconciliation has restored a matching complete status.

Cell finalization is atomic. A complete cell-local `row.json` and its random `finalization_token`
land before that exact row is appended under the exclusive run lock. Resume skips a terminal cell
only when its row parses and every referenced artifact still matches its recorded hash. A torn final
JSONL fragment is moved byte-for-byte into run quarantine before truncation to the last newline; a
matching cell-local row is then appended once. Repeated rows with the same token and identical bytes
are an idempotent crash artifact and collapse to one logical row; different tokens or conflicting
bytes for one cell mark `corrupt-resume` and invalidate the run. A complete JSONL row whose cell or
artifact hashes do not verify also invalidates the run and is never rerun. Only incomplete cell
state with no complete JSONL row is quarantined and rerun; accepted evidence is never silently
overwritten.

### Wire contracts and identifiers

Every JSON object is strict: unknown or missing keys fail, persisted readers require
`schema_version == 1`, and a later version requires an explicit migrator. Canonical JSON is UTF-8
with sorted keys and compact separators; JSONL is one canonical object plus `\n`; artifact paths are
normalized POSIX paths relative to the run root.
`TreeArtifact.sha256` is SHA-256 of canonical JSON over byte-sorted
`[relative_path, file_sha256, size_bytes, executable_bit]` entries; its count/size are recomputed,
and symlinks, junctions, devices, sockets, and submodules are rejected. A planned-call artifact uses
its future fixed `inputs/...` run-local path with the source bytes' hash and size.

| Wire object | Required v1 shape |
|---|---|
| Model registry | `schema_version`, `models[]`; each model has `name`, `provider`, `requested_model`, nullable `effort`, and `execution_profile_id`. Provider is `codex-cli` or `claude-cli`; execution-profile content never appears here. |
| Execution profile | ID plus qualification/smoke/pilot/observation timeouts, repetitions `2`, concurrency `1`, path/patch/stream/artifact ceilings, class-specific wall/tranche/attempt limits, retry classes/delays, sandbox contract version, schedule algorithm, and analysis algorithm. |
| `suite.json` | `schema_version`, `suite_id`, `version`, `description`, Python `3.12` runtime, `evaluator_version`, `execution_profile_id`, `scoring_policy`, strict `run_class`, and task-manifest paths. |
| `task.json` | `schema_version`, `task_id`, family, tags, `cluster_id`, difficulty, prompt/seed/oracle/reference paths, `allowed_changes`, `protected_paths`, and provenance. Family is `bug-repair`, `bounded-feature`, `behavioral-refactor`, or `cli-data-boundary`; difficulty is `easy`, `medium`, or `hard`; no command field exists. |
| Immutable manifest | Run ID/time; suite snapshot, hashes, and tasks; harness/profile snapshot and hash; environment fingerprint; selected model snapshots; provider preflights; sample count; schedule/analysis seeds; complete schedule; preregistration text/hash; nullable human-validity-ledger descriptor/hash; and budgets/retry policy. |
| Atomic run status | Run ID, closed state, completed block count, terminal cell count, provider-attempt consumption, update time, and nullable reason; replaced atomically after transitions and excluded from immutable manifest identity. |
| Terminal row | Run/cell/schedule/task/cluster/profile/provider/model/sample/attempt identity; `cell_status`; termination and error class; nullable score; test/containment/change summaries; elapsed time; and hashed artifact descriptors. |
| Artifact descriptor | Relative `path`, lowercase SHA-256, and non-negative `size_bytes`. `cell.json` also carries ordered attempt records with request fingerprint, timestamps, termination, and retry class. |
| Report JSON | Run/instrument/harness/environment identity, completeness and pilot flags, analysis contract, per-model and all-pairs results, and nullable overall verdict. |
| Qualification source | One strict doctor raw record plus its derived per-profile snapshot; the snapshot's raw-evidence and qualification-environment hashes must be recomputable from that record. |
| Qualification bundle | Two or more unique per-profile snapshots in safe-ID byte order, each carrying requested/resolved identity, profile/CLI/executable/invocation/qualification-environment hashes, sandbox contract, effective tools, six canary outcomes, qualification time, and recomputable raw-evidence hash. V1 binds exactly Luna and Sonnet; a later three-arm run may bind Haiku without a schema branch. |
| Analysis plan | Strict policy (`none`, superiority, or non-inferiority), candidate/reference profile IDs, ordered confirmatory scopes, margin, confidence, multiplicity, and bootstrap count; report code never infers these from model names or Markdown. |
| Run evidence snapshot | Run/instrument/harness/environment/profile/preregistration/analysis-plan/validity hashes, completeness and pilot flags, analysis contract, model/scope/pair results, failure breakdown, nullable verdict, and raw manifest/status/row/artifact/report hashes. |
| Human-validity ledger | Instrument/preregistration hashes and exactly one task record per v1 task, including authorship assistance, two final human reviews, reconciliation log, task hash, and overall pass. |

The notation below is normative: `str!` is a non-empty UTF-8 string, `hex64` matches
`^[0-9a-f]{64}$`, `git_oid` matches 40 or 64 lowercase hexadecimal characters, `relpath` is a normalized contained POSIX relative path, `utc` is
`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`, and `list[T]` preserves declared order. `int+` is positive;
`int0` and `float0` are non-negative. Object braces list the complete key set, `?` means the value
is nullable but the key remains required, `float` is a finite signed JSON number, and `A|B` is a
closed enum.

```text
Artifact = {path: relpath, sha256: hex64, size_bytes: int0}
TreeArtifact = {path: relpath, sha256: hex64, file_count: int+, size_bytes: int0}
ModelSpec = {
  name: SafeId, provider: "codex-cli"|"claude-cli", requested_model: str!,
  effort: str!?, execution_profile_id: SafeId
}
ModelRegistry = {schema_version: 1, models: list[ModelSpec]}
Limits = {agent_timeout_s: int+, evaluator_timeout_s: int+}
RunPolicy = {
  limits: Limits, task_count: int+, model_count: int+, samples: int+,
  max_cells_per_tranche: int+, terminal_cells: int+, provider_attempts: int+,
  wall_s: int+
}
Ceilings = {
  changed_paths: int+, patch_bytes: int+, stream_bytes_each: int+,
  cell_artifact_bytes: int+, evaluator_cpu_s: int+, evaluator_memory_bytes: int+,
  evaluator_processes: int+, evaluator_files: int+, evaluator_file_bytes: int+,
  evaluator_cpu_bandwidth_percent: int+, evaluator_tmpfs_bytes: int+,
  evaluator_tmpfs_inodes: int+
}
ExecutionProfile = {
  schema_version: 2, id: SafeId,
  qualification_limits: Limits,
  run_policy: {smoke: RunPolicy, pilot: RunPolicy, observation: RunPolicy},
  repetitions: 2, concurrency: 1, ceilings: Ceilings,
  retry: {
    eligible: list["rate-limit"|"provider-5xx"|"preterminal-transport"],
    max_fresh_retries: 1, retry_after_cap_s: 60, default_delay_s: 5
  },
  sandbox_contract_version: "linux-bwrap-v2",
  schedule_algorithm: "schedule-v1", analysis_algorithm: "bootstrap-v1"
}
Suite = {
  schema_version: 1, suite_id: SafeId, version: str!, description: str!,
  runtime: {language: "python", version: "3.12"},
  evaluator_version: str!, execution_profile_id: SafeId,
  scoring_policy: "binary-heldout-v1", run_class: "smoke"|"pilot"|"observation",
  tasks: list[relpath]
}
Task = {
  schema_version: 1, task_id: SafeId,
  family: "bug-repair"|"bounded-feature"|"behavioral-refactor"|"cli-data-boundary",
  tags: list[SafeId], cluster_id: SafeId, difficulty: "easy"|"medium"|"hard",
  prompt: relpath, seed: relpath, oracle: relpath, reference_patch: relpath,
  allowed_changes: list[str!], protected_paths: list[relpath],
  provenance: {
    source: str!, license: str!?, authoring_identity: str!,
    authoring_assistance: list[str!], independent_reviewers: list[str!]
  }
}
TaskRef = {task_id: SafeId, cluster_id: SafeId, family: SafeId, task_hash: hex64}
AnalysisScope = {
  scope: "overall"|"family"|"tag", scope_id: SafeId?, confirmatory: true
}
AnalysisPlan = {
  schema_version: 1, analysis_id: SafeId,
  policy: "none"|"superiority"|"noninferiority",
  candidate_profile_id: SafeId?, reference_profile_id: SafeId?,
  scopes: list[AnalysisScope], margin_points: 5|null, confidence: 0.95|null,
  multiplicity: "none"|"bonferroni", bootstrap_iterations: 10000
}
AnalysisSummary = {
  analysis_plan_sha256: hex64, analysis_id: SafeId,
  policy: "none"|"superiority"|"noninferiority",
  candidate_profile_id: SafeId?, reference_profile_id: SafeId?,
  scopes: list[AnalysisScope], margin_points: 5|null, confidence: 0.95|null,
  multiplicity: "none"|"bonferroni", algorithm: "bootstrap-v1",
  iterations: 10000, simultaneous_scope_count: int0
}
Executable = {
  provider: "codex-cli"|"claude-cli", version: str!, executable_sha256: hex64,
  invocation_sha256: hex64, requested_model: str!, resolved_model: str!?
}
QualificationEnvironment = {
  os: "linux", distribution: str!, distribution_version: str!, kernel_version: str!,
  architecture: str!, python_version: str!, dependency_lock_sha256: hex64,
  bubblewrap_version: str!, bubblewrap_executable_sha256: hex64,
  runtime_mount_sha256: hex64
}
Environment = {
  os: str!, os_version: str!, architecture: str!, python_version: str!,
  dependency_lock_sha256: hex64, benchmark_commit: git_oid,
  sandbox_contract_version: "linux-bwrap-v2",
  qualification_environment: QualificationEnvironment, executables: list[Executable]
}
ScheduleCell = {
  ordinal: int0, block_ordinal: int0, block_position: int0,
  task_id: SafeId, sample_k: int+, model_profile_id: SafeId, cell_id: str!
}
Manifest = {
  schema_version: 1, run_id: RunId, created_at: utc,
  run_class: "smoke"|"pilot"|"observation",
  suite_snapshot: TreeArtifact, instrument_hash: hex64, harness_hash: hex64,
  environment: Environment, selected_profiles: list[ModelSpec],
  selected_profile_hash: hex64, execution_profile: ExecutionProfile,
  qualification_bundle: Artifact, tasks: list[TaskRef], samples: int+,
  schedule_seed: hex64, analysis_seed: hex64, schedule: list[ScheduleCell],
  preregistration: Artifact, analysis_plan: Artifact, validity_ledger: Artifact?,
  budget_limits: {terminal_cells: int+, provider_attempts: int+, wall_s: int+}
}
RunStatus = {
  schema_version: 1, run_id: RunId,
  state: "ready"|"running"|"paused"|"complete"|"invalid-infrastructure",
  completed_blocks: int0, terminal_cells: int0, provider_attempts: int0,
  updated_at: utc, reason: str!?
}
Attempt = {
  attempt_k: int+, reservation_token: hex64, request_sha256: hex64,
  state: "completed"|"abandoned-after-reservation", started_at: utc, ended_at: utc?,
  termination: str!, retry_class: "none"|"eligible"|"exhausted"|"recovery",
  exit_code: int0?, signal: int0?, invocation: Artifact?
}
AttemptReservation = {
  schema_version: 1, run_id: RunId, cell_id: str!, attempt_k: int+,
  reservation_token: hex64, request_sha256: hex64, reserved_at: utc
}
CellRecord = {
  schema_version: 1, run_id: RunId, cell_id: str!, ordinal: int0,
  state: "running"|"terminal", attempts: list[Attempt],
  finalization_token: hex64?, row_sha256: hex64?
}
WorkspaceFile = {path: relpath, sha256: hex64, size_bytes: int0, mode: str!}
WorkspaceFiles = {
  schema_version: 1, seed_tree_sha256: hex64, submitted_tree_sha256: hex64,
  changed_paths: list[relpath], files: list[WorkspaceFile]
}
AgentInvocation = {
  schema_version: 1, request_sha256: hex64, executable: Executable,
  argv_sha256: hex64, stdin_sha256: hex64, cwd: "workspace",
  environment_names: list[str!], started_at: utc, ended_at: utc,
  elapsed_ms: int0, exit_code: int0?, signal: int0?,
  stdout: Artifact, stderr: Artifact, trace: Artifact, final_text: Artifact
}
Containment = {
  outside_write_denied: bool, instruction_read_denied: bool,
  credential_read_denied: bool, child_network_denied: bool, web_tool_denied: bool
}
TestSummary = {passed: int0, failed: int0, skipped: int0, repetitions_agree: bool}
ResourceLimitEvidence = {
  name: "cpu"|"memory"|"processes"|"file-count"|"file-bytes",
  provenance: "hard-guard"|"sampled-threshold", limit: int+, observed: int0?
}
TreePolicyViolation = {
  reason: "invalid-name"|"special-file"|"structural-shape"|"symlink"|"unreadable",
  relative_path_utf8_prefix: str?
}
EvaluationRepetition = {
  ordinal: 1|2, applied_tree_sha256: hex64, result_tree_sha256: hex64?,
  outcome: "pass"|"tests-failed"|"tests-timeout"|"resource-limit"|"forbidden-edit"|
           "evaluator-infrastructure",
  passed: int0, failed: int0, skipped: int0, elapsed_ms: int0,
  stdout: Artifact, stderr: Artifact, resource_limit: ResourceLimitEvidence?,
  tree_policy_violation: TreePolicyViolation?
}
EvaluationResult = {
  schema_version: 1, evaluator_version: str!, oracle_sha256: hex64,
  repetitions: list[EvaluationRepetition], repetitions_agree: bool,
  outcome: "pass"|"tests-failed"|"tests-timeout"|"resource-limit"|"forbidden-edit"|
           "nondeterministic"|"evaluator-infrastructure"|"evaluator-nondeterministic"
}
TerminalRow = {
  schema_version: 1, run_id: RunId, cell_id: str!, ordinal: int0,
  finalization_token: hex64,
  task_id: SafeId, cluster_id: SafeId, family: SafeId,
  model_profile_id: SafeId, provider: "codex-cli"|"claude-cli",
  requested_model: str!, resolved_model: str!?, sample_k: int+, attempts: list[Attempt],
  cell_status: "scored"|"invalid", termination: str!, error_class: ErrorClass?,
  score: 0|1?, outcome: ScoreOutcome?, changed_paths: list[relpath],
  tests: TestSummary?, containment: Containment, elapsed_ms: int0,
  artifacts: list[Artifact]
}
OutcomeCounts = {both_pass: int0, neither_pass: int0, left_only: int0, right_only: int0}
ModelResult = {
  profile_id: SafeId, score: float0?, task_count: int0, scored_cells: int0,
  invalid_cells: int0, failure_counts: map[ErrorClass|ScoreOutcome, int0]
}
PairResult = {
  left_profile_id: SafeId, right_profile_id: SafeId,
  scope: "overall"|"family"|"tag", scope_id: SafeId?,
  confirmatory: bool, cluster_count: int0, delta: float?,
  ci_lower: float?, ci_upper: float?, counts: OutcomeCounts,
  decision: "CANDIDATE_ADVANTAGE"|"REFERENCE_ADVANTAGE"|"INCONCLUSIVE"|
            "CANDIDATE_ELIGIBLE"|"REFERENCE_REQUIRED"|"UNRESOLVED"|"EXPLORATORY"|null
}
ConfirmatoryDecision = {
  candidate_profile_id: SafeId, reference_profile_id: SafeId,
  scope: "overall"|"family"|"tag", scope_id: SafeId?,
  decision: "CANDIDATE_ADVANTAGE"|"REFERENCE_ADVANTAGE"|"INCONCLUSIVE"|
            "CANDIDATE_ELIGIBLE"|"REFERENCE_REQUIRED"|"UNRESOLVED"
}
Report = {
  schema_version: 1, run_id: RunId, instrument_hash: hex64, harness_hash: hex64,
  environment_sha256: hex64, complete: bool, pilot_not_ranking: bool,
  analysis: AnalysisSummary,
  models: list[ModelResult], pairs: list[PairResult],
  confirmatory_decisions: list[ConfirmatoryDecision],
  failure_breakdown: map[ErrorClass|ScoreOutcome, int0],
  quality_verdict: "CANDIDATE_ADVANTAGE"|"REFERENCE_ADVANTAGE"|"INCONCLUSIVE"|
                   "CANDIDATE_ELIGIBLE"|"REFERENCE_REQUIRED"|"UNRESOLVED"|null
}
```

The cross-worktree evidence and control objects are equally strict:

```text
QualificationSnapshot = {
  schema_version: 1, profile_id: SafeId, provider: "codex-cli"|"claude-cli",
  requested_model: str!, resolved_model: str!?, qualified_at: utc,
  model_spec_sha256: hex64, execution_profile_sha256: hex64,
  qualification_environment_sha256: hex64,
  cli_version: str!, executable_sha256: hex64, invocation_sha256: hex64,
  sandbox_contract_version: "linux-bwrap-v2", effective_tools: list[str!],
  canaries: {
    allowed_write: bool, outside_write_denied: bool, instruction_read_denied: bool,
    credential_read_denied: bool, child_network_denied: bool, web_tool_denied: bool
  },
  raw_evidence_sha256: hex64
}
DoctorRawRecord = {
  schema_version: 1, kind: "doctor-raw", profile_id: SafeId,
  provider: "codex-cli"|"claude-cli", started_at: utc, ended_at: utc,
  executable: Executable, model_spec_sha256: hex64, execution_profile_sha256: hex64,
  qualification_environment: QualificationEnvironment,
  sandbox_contract_version: "linux-bwrap-v2", effective_tools: list[str!],
  observations: list[{
    name: "allowed-write"|"outside-write"|"instruction-read"|"credential-read"|
          "child-network"|"first-class-web",
    expected: "allow"|"deny", observed: "allowed"|"denied"|"failed",
    exit_code: int0?, stdout_sha256: hex64, stderr_sha256: hex64
  }]
}
QualificationSource = {
  schema_version: 1, kind: "qualification-source",
  raw: DoctorRawRecord, snapshot: QualificationSnapshot
}
QualificationBundle = {
  schema_version: 1, kind: "qualification",
  snapshots: list[QualificationSnapshot], source_set_sha256: hex64
}
RunEvidenceSnapshot = {
  schema_version: 1, kind: "run", run_class: "smoke"|"pilot"|"observation", run_id: RunId,
  instrument_hash: hex64, harness_hash: hex64, environment_sha256: hex64,
  selected_profile_hash: hex64, qualification_bundle_sha256: hex64,
  preregistration_sha256: hex64, analysis_plan_sha256: hex64,
  validity_ledger_sha256: hex64?, complete: bool, pilot_not_ranking: bool,
  analysis: AnalysisSummary, models: list[ModelResult], pairs: list[PairResult],
  confirmatory_decisions: list[ConfirmatoryDecision],
  failure_breakdown: map[ErrorClass|ScoreOutcome, int0], quality_verdict: Report.quality_verdict,
  manifest_sha256: hex64, status_sha256: hex64, rows_sha256: hex64,
  artifact_set_sha256: hex64, report_sha256: hex64
}
ValidityLedger = {
  schema_version: 1, instrument_hash: hex64, preregistration_sha256: hex64,
  analysis_plan_sha256: hex64,
  tasks: list[{
    task_id: SafeId, task_hash: hex64, author_identity: str!,
    authoring_assistance: list[str!],
    reviews: list[{
      reviewer_id: str!, independent_of_author: bool, final_decision: "PASS",
      prompt_valid: bool, seed_valid: bool, oracle_valid: bool,
      reference_patch_valid: bool, paths_valid: bool, difficulty_valid: bool,
      provenance_valid: bool, cluster_valid: bool, provider_neutral: bool,
      rationale: str!
    }],
    reconciliations: list[str!]
  }],
  overall_pass: true
}
ValidityWorksheet = {
  schema_version: 1, kind: "validity-template", instrument_hash: hex64,
  preregistration_sha256: hex64, analysis_plan_sha256: hex64,
  tasks: list[{
    task_id: SafeId, task_hash: hex64, author_identity: str!,
    authoring_assistance: list[str!], reviews: list[], reconciliations: list[]
  }]
}
DoctorPlannedCall = {
  schema_version: 1, command_kind: "doctor", model_profile_id: SafeId,
  profiles_sha256: hex64, execution_profile_sha256: hex64,
  evidence_path: relpath, data_home_fingerprint: hex64, executable: Executable,
  qualification_environment_sha256: hex64,
  qualification_canary_sha256: hex64, limits_sha256: hex64
}
FreshRunPlannedCall = {
  schema_version: 1, command_kind: "fresh-run", suite_sha256: hex64,
  instrument_hash: hex64, profiles_sha256: hex64, execution_profile_sha256: hex64,
  qualification_bundle: Artifact,
  model_profile_ids: list[SafeId], samples: int+, preregistration: Artifact,
  analysis_plan: Artifact, validity_ledger: Artifact?,
  schedule_seed: hex64, analysis_seed: hex64,
  schedule: list[ScheduleCell], schedule_sha256: hex64, data_home_fingerprint: hex64,
  executables: list[Executable], environment_sha256: hex64,
  qualification_environment_sha256: hex64, limits_sha256: hex64
}
ResumePlannedCall = {
  schema_version: 1, command_kind: "resume", run_id: RunId,
  manifest_sha256: hex64, terminal_rows_sha256: hex64,
  attempt_reservations_sha256: hex64, next_block_ordinal: int0,
  next_block: list[ScheduleCell],
  data_home_fingerprint: hex64, executables: list[Executable],
  environment_sha256: hex64, limits_sha256: hex64
}
ConfirmationReceipt = {
  schema_version: 1, nonce: hex64, created_at: utc, expires_at: utc,
  planned_call: DoctorPlannedCall|FreshRunPlannedCall|ResumePlannedCall
}
RunOwnerLock = {
  schema_version: 1, run_id: RunId, host_fingerprint: hex64,
  pid: int+, token: hex64, acquired_at: utc
}
```

The evidence schemas contain everything consumed by Steps 31, 42, 51, and 53-55 while deliberately
excluding prompts, raw traces, stdout/stderr, environment values,
credentials, account identifiers, and absolute paths. The Step-31 and Step-34 golden fixtures freeze
literal canonical examples for every object above; implementation dataclasses may be split across
modules, but persisted key sets and enum spelling must match this contract.

`TreePolicyViolation.relative_path_utf8_prefix` is diagnostic-only, never pathname authority, and is
null when the offending name cannot be decoded as UTF-8; otherwise it is a component-boundary prefix
of at most 1,024 UTF-8 bytes. It is non-null exactly when a post-empty strict scan confirms the
violation. That repetition records `forbidden-edit`, retains any independently proven resource
evidence, and leaves `result_tree_sha256` null because no policy-invalid tree is authorized as a
canonical snapshot. `forbidden-edit` takes precedence over `resource-limit`, timeout, and ordinary
test status for the repetition outcome without erasing those underlying artifacts/evidence.

`CellRecord.finalization_token` and `row_sha256` are both null only while running and both non-null
when terminal; terminal `row.json` is byte-identical to the `TerminalRow` appended to `rows.jsonl`.
`WorkspaceFiles`, `AgentInvocation`, and `EvaluationResult` are the exact schemas for the other
named JSON files in each cell. Every provider attempt owns a distinct invocation and stream set
under `attempts/<attempt_k>/agent/`, and its `Attempt.invocation` descriptor links that strict record;
evaluation runs once against only the terminal attempt's captured cutoff workspace. Each evaluator repetition has its own freshly materialized applied
tree and fresh temp/home/PID namespace; neither repetition reuses the other's writable state.

Immediately before any provider call, the runner appends one canonical `AttemptReservation` under
the run lock, flushes and fsyncs it, and only then invokes. A torn final reservation fragment is
recoverable because invocation cannot start before successful fsync. On resume, a valid reservation
without a linked completed attempt is conservatively counted and represented as
`abandoned-after-reservation`. If the cell's single fresh retry and global budget remain, resume
must append exactly one `recovery` reservation and invoke once in a fresh workspace; it never skips
or offers a choice. If no retry remains, or that recovery cannot produce a terminal scored row
(including another orphan or eligible infrastructure failure), the whole run becomes
infrastructure-invalid. Reservation tuple/token
duplicates collapse only when byte-identical; conflicts invalidate. Thus a controller crash can
underspend but can never exceed the manifest's provider-attempt ceiling or erase call evidence.

Closed row status is `scored` or `invalid`; only `scored` rows carry score `0` or `1`. Invalid
error classes are `auth`, `model-mismatch`, `cli-contract`, `provider-unavailable`, `transport`,
`infra-exhausted`, `containment`, `artifact-store`, `patch-capture`, `patch-apply`,
`evaluator-infrastructure`, `evaluator-nondeterministic`, `hash-mismatch`, or
`corrupt-resume`. Invalid rows suppress decisions and never become model zeros. Scored outcomes are `pass`,
`tests-failed`, `tests-timeout`, `resource-limit`, `nondeterministic`, `forbidden-edit`, or `patch-unavailable`; a model timeout is scoreable when the
cutoff workspace can be captured and evaluated.

The causal mapping is normative and prevents model behavior from censoring a run:

| Observed cause | Row treatment |
|---|---|
| Ordinary visible/hidden assertion failure | `scored`, `score=0`, `tests-failed` |
| Submitted code reaches evaluator wall timeout, triggers a verified memory/task hard guard, leaves the retained private tmpfs terminally byte/inode exhausted, or crosses a sampled cumulative-CPU/logical-tree threshold while the applicable guard health and attribution remain proven | `scored`, `score=0`, `tests-timeout` for wall time or `resource-limit` otherwise; a resource result records `hard-guard` or `sampled-threshold` provenance, configured limit, and observed value when available |
| Agent behavior exceeds changed-path, patch, captured-stream, or total-cell-artifact ceiling | Preserve bounded evidence; `scored`, `score=0`, `resource-limit` |
| Submitted tree touches a forbidden/protected path, introduces a symlink/submodule, or the quiescent evaluator result tree has a typed policy violation | `scored`, `score=0`, `forbidden-edit`; retain simultaneous resource and bounded process evidence |
| Agent timeout/crash leaves no usable patch | `scored`, `score=0`, `patch-unavailable`; a usable cutoff patch is evaluated normally |
| Submitted patch repetitions disagree in result or post-evaluation tree | `scored`, `score=0`, `nondeterministic` |
| Baseline/reference anchor repetitions disagree/fail; cgroup guard setup, readback, attribution, monitoring, or teardown fails; tmpfs setup, readback, or teardown fails; containment escape succeeds; harness storage I/O fails; or patch capture/apply/evidence hash fails | `invalid` with the matching closed error class; halt/suppress inference as specified |

The private tmpfs is always a hard host-safety envelope, but Linux exposes no per-mount event counter
that proves a transient `ENOSPC` after a target deletes its writes. The harness may record a
`file-bytes` or `file-count` `hard-guard` result only when retained-FD terminal evidence unambiguously
shows exhausted blocks or inodes, using the physical tmpfs limit. Otherwise it must not invent
tmpfs-hit attribution: sampled logical-tree evidence or the ordinary test outcome governs scoring.
Likewise, concurrent model-controlled namespace churn during a live logical-tree poll is an
inconclusive sample, not evaluator invalidity and not evidence below the threshold. The strict
authoritative scan occurs only after the evaluator cgroup is proven empty. A stable symlink, special
object, denied entry, or structural tree-policy violation is surfaced as a typed model-tree outcome
for Step 27 rather than being mislabeled as harness failure.

- Safe suite, task, cluster, and profile IDs match `^[a-z0-9][a-z0-9._-]{0,63}$` and reject `.` and
  `..`. They are author-assigned and unique in their containing registry or suite.
- `run_id` matches the existing project convention `run_YYYYMMDDTHHMMSSZ_6hex`, minted from UTC
  plus three random bytes by the runner.
- `cell_id` is `cell_` plus the full lowercase SHA-256 of canonical JSON
  `[instrument_hash, selected_profile_hash, model_profile_id, task_id, sample_k]`. It is deliberately
  run-ID-independent so the complete schedule is byte-identical before and after confirmation; the
  run root scopes repeated IDs across runs. Sample and attempt indexes are one-based integers.

### CLI contract

Argparse misuse exits `2`, a validation/runtime contract failure exits `1`, and success exits `0`.
`--out` defaults to `data` and names the data home. `--profiles` always defaults to
`profiles/agent-models-candidates.json`; `--execution-profile` always defaults to
`profiles/agent-execution-v1.json`. A v1 fresh run must explicitly select
`--profiles profiles/agent-models-v1.json`. The suite's immutable `run_class`—never a CLI flag—selects
one exact `RunPolicy`; task/model/sample counts must equal that policy, terminal cells must equal
their product, the tranche limit may not split a model block, and provider attempts must equal
terminal cells times `(1 + max_fresh_retries)`. The committed v1 profile pins smoke to `1×2×1=2`,
pilot to `12×2×1=24`, and observation to `32×2×3=192`. Smoke/pilot forbid a validity ledger and
derive `pilot_not_ranking: true` in reports; observation requires the ledger and derives false.
Each seed is exactly 64 lowercase hexadecimal
characters, model CSV values are unique safe profile IDs with no whitespace, positive integers are
base-10, and every output option must name a nonexistent file or an existing directory as stated.
No command infers a roster or merges profile values.

| Command | Exact v1 syntax and behavior |
|---|---|
| `mt agent validate <suite-dir> [--profiles <file>] [--execution-profile <file>] [--structure-only]` | Inference-free strict load and hashes. `--structure-only` is cross-platform and executes no suite code. Without it, the command requires the accepted WSL2/Linux sandbox/ext4 preflight and runs untouched/no-op/reference anchors twice inside separate evaluator sandboxes; native Windows fails before executing task bytes. Writes nothing. |
| `mt agent doctor --model <profile> --evidence-out <new-json> [--profiles <file>] [--execution-profile <file>] [--out <dir>] (--dry-run or --confirm <hex64>)` | Exactly one selected model and one 60-second qualification call. Evidence output must be a new file contained by `<out>/exports`. Dry-run makes no provider call. Confirmed execution writes one atomic `QualificationSource` and no run directory. |
| `mt agent run --suite <dir> --models <csv> --samples <N> --preregister-file <file> --analysis-plan <file> --qualification-bundle <file> --schedule-seed <hex64> --analysis-seed <hex64> [--validity-ledger <file>] [--profiles <file>] [--execution-profile <file>] [--out <dir>] (--dry-run or --confirm <hex64>)` | Requires at least two profiles, a strict analysis plan and qualification bundle, and an exactly matching class-selected run policy. An observation suite requires `--validity-ledger`; smoke/pilot forbid it. Dry-run prints the complete immutable schedule and ceilings. |
| `mt agent run --resume <run_id> [--out <dir>] (--dry-run or --confirm <hex64>)` | `--resume` is mutually exclusive with every fresh-run input, registry/profile/qualification/analysis-plan path, seed, model, sample, preregistration, validity-ledger, or limit. It reads immutable inputs from the run snapshot. A clean profile-owned tranche/wall pause exits `0` with `state=paused`; failure exits `1`. |
| `mt agent report <run_id> [--out <dir>] [--format <mode>] [--report-dir <dir>] [--evidence-out <new-json>]` | Mode is `md`, `json`, or `both` and defaults to `both`; report directory defaults to `<out>/agent-reports`; evidence output, when present, must be a new file below `<out>/exports`. The command is offline and hash-verifying. It may report finalized rows while a run lock exists but marks the run incomplete and emits no verdict. Evidence export is canonical, atomic, and secret-scrubbed. |
| `mt agent evidence validity-template --suite <dir> --preregister-file <file> --analysis-plan <file> --dest <new-json> [--out <dir>]` | Inference-free. Destination must be a new file below `<out>/exports`. Writes a strict worksheet with suite/preregistration/analysis-plan hashes and one empty review slot per task. It is not a runnable validity ledger. |
| `mt agent evidence import --kind <kind> --source <file> [--source <file> ...] --dest <new-json> [--out <dir>]` | Kind is `qualification`, `run`, or `validity`. Offline strict validation verifies source and recomputable raw-record hashes, rejects prohibited fields/values and absolute paths, refuses overwrite, then writes one canonical snapshot below the checkout's `docs/agent-benchmark/evidence/`. Qualification requires two or more unique `QualificationSource` files and emits one sorted `QualificationBundle`; every other kind requires exactly one source. |

Provider-bearing `doctor`, fresh `run`, and `run --resume` use one confirmation protocol. Before a
fresh-run dry-run can create a receipt, its required qualification bundle must cover exactly the
selected profiles. Current preflight must exactly match each snapshot's profile ID/provider,
requested and resolved identity where the provider emits it, model-spec and execution-profile
hashes, CLI version, executable and invocation hashes, qualification-environment hash,
sandbox-contract version, and effective-tool
set. The dry-run binds the canonical bundle's future run-local artifact descriptor into
`FreshRunPlannedCall`; confirmed execution copies those exact bytes to
`inputs/qualification-bundle.json` before the immutable manifest is created. Any mismatch fails before a
receipt or provider call and requires new Steps 30-31 qualification evidence plus a new Step-35
real-pipeline smoke. Any later freeze/preregistration/validity artifact that cites the replaced
bundle must then be regenerated through its owning code and wait gates before observation. Resume
accepts no replacement bundle and reuses the snapshotted descriptor.

Every fresh run also requires a strict `AnalysisPlan`. `policy: "none"` requires both IDs, margin,
and confidence null,
an empty scope list, and `multiplicity: "none"`; smoke and pilot require this form. Superiority and
non-inferiority require distinct non-null candidate/reference IDs from the selected roster,
`margin_points: 5`, `confidence: 0.95`, at least one ordered
confirmatory scope, and `multiplicity: "none"` for one scope or `"bonferroni"` for multiple scopes.
Scope IDs are null only for `overall`, unique pairs of kind/ID are required, and every named
family/tag must exist in the suite. The preregistration Markdown states the canonical analysis-plan
SHA-256; dry-run verifies it, binds the descriptor, and confirmed execution snapshots it at
`inputs/analysis-plan.json`. Reporting consumes only this run-local plan for confirmatory decisions;
all unplanned pairs/scopes remain exploratory, so no model-name branch exists.
`confirmatory_decisions` preserves one ordered result per planned scope. The convenience
`quality_verdict` equals that result only when there is exactly one confirmatory scope; it is null
for `policy: "none"`, multiple scopes, incomplete/invalid runs, or suppressed min-n. A mixed
four-scope Haiku fixture must retain all four decisions and never invent an aggregate winner.

`DoctorRawRecord.observations` contains exactly the six unique canaries in the displayed enum's
order. `QualificationSource.snapshot` is a deterministic projection of `raw`; its
`raw_evidence_sha256` is SHA-256 of canonical `raw`, so import can recompute it without trusting an
unverifiable claimed digest. Its qualification-environment hash is SHA-256 of the canonical
`QualificationEnvironment`; this fingerprint intentionally excludes the changing benchmark commit
but includes WSL distribution/kernel, architecture, Python, lockfile, Bubblewrap, and read-only
runtime mount. A bundle contains two or more unique snapshots in safe profile-ID byte
order and hashes the canonical source set; Step 31 additionally fixes the v1 bundle to exactly the
`codex-luna` and `claude-sonnet` profiles.

Provider-bearing `doctor`, fresh `run`, and `run --resume` otherwise use one confirmation protocol.
`--dry-run` and `--confirm` are mutually exclusive. Dry-run completes all non-inference preflights,
builds a strict `planned_call` containing `schema_version: 1`, command kind, normalized argument
values, all selected file/content hashes, executable/version/capability and environment
fingerprints, canary hashes, limits, and either the complete fresh schedule or exact next resume
block. It excludes run ID and timestamps that affect measurement. It places that planned call with
a random 256-bit nonce and UTC `created_at`/`expires_at` (15 minutes) in the exact
`ConfirmationReceipt`, computes the lowercase SHA-256 of the complete canonical receipt as its
digest, prints the receipt and digest, and atomically stores mode-`0600`
`<out>/agent-confirmations/<digest>.pending.json`. This receipt is the dry-run's only write.

A confirmed invocation loads that exact receipt, verifies the supplied/filename digest against its
canonical bytes, rejects expiry, reconstructs the current `planned_call`, and byte-compares that
object with the stored one. It then atomically renames the unchanged file to `.used.json` before any
provider call; the filename is the sole pending/used state. Missing, reused, changed, or expired receipts fail before inference. A fresh run ID
and timestamp are minted only after consumption, so random identity cannot invalidate comparison.
Tests freeze planned-call canonicalization, change detection, expiry, one-shot consumption, and
crash-after-consumption behavior.

All operator and wait steps resolve one shared state home as
`<git-common-dir>/agent-bench-state`, where `<git-common-dir>` is the absolute result of
`git rev-parse --path-format=absolute --git-common-dir`. Git worktrees share it and Git never indexes its contents. Raw
runs, confirmations, doctor records, and export candidates live there; only a later code step may
import a strict scrubbed snapshot into `docs/agent-benchmark/evidence/`.

The model registry contains no execution-profile content. The loaded execution profile is the only
policy source, and its `id` must equal the suite and every selected model's
`execution_profile_id`. An explicit path selects a whole file and never overlays values. Resume
accepts no replacement value.


## 6. Design Decisions

### 6.1 Use a sibling agent pipeline

The prompt-response engine and coding-agent engine measure different production artifacts. Adding
workspace fields and patch scoring to `Item`, `RunRow`, and `report.py` would weaken existing
compatibility and touch every scorer, authoring path, resume reader, and report consumer. The new
package follows the existing fail-loud/run-store patterns while giving agent artifacts a dedicated
schema. Alternative rejected: treat a final textual answer as the coding result; that would not
measure the repository the agent actually changed.

### 6.2 Measure native agent products, not naked models

Codex and Claude retain their native coding tools and agent loops. Fairness means byte-identical
task inputs and outcome constraints, not pretending the two products expose identical internals.
The manifest records the CLI, model request/resolution, effort, tool policy, and invocation hash so
the report can name exactly what was measured. [Official OpenAI documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
establishes that Codex supports noninteractive execution, explicit model selection, ephemeral
sessions, JSONL, and sandbox controls; Steps 28-29 pin the supported contracts in versioned tests before any
observation. The requested Luna identifier is grounded in the official
[GPT-5.6 Luna model page](https://developers.openai.com/api/docs/models/gpt-5.6-luna).

### 6.3 Dispatch through explicit model profiles

Agent routing uses `ModelSpec.provider`; it never reuses `CLAUDE_ALIASES` or the prompt runner's
local fallback. The suite never names models, so changing the roster does not change the benchmark
instrument. Adding Haiku later means adding a pinned `claude-haiku` profile and selecting it in the
run roster. The default prompt roster remains untouched.

### 6.4 Require a common fail-closed Linux execution backend

All live cells run from WSL2/Linux with WSL-local temporary workspaces. Each provider must prove a
fresh noninteractive session, explicit working directory, no inherited repository instructions,
no MCP/plugins/browser, writes confined to the cell workspace, child network denied, and
process-tree termination at timeout. If either provider cannot satisfy a containment or network
canary, the qualification gate fails and no downstream runner/report/task build or observation may
proceed. Native Windows remains a supported control plane but not an accepted live observation
backend for v1. Alternative rejected: best-effort native Windows containment, because provider
asymmetry would become part of the score without being controlled.

The plan pins required semantics, not imagined argv. Steps 28-29 derive and contract-test the exact
supported Codex and Claude invocations from official contracts and versioned help/event fixtures.
Step 30 then compares the actually installed capabilities and qualifies those
invocations with live filesystem, instruction-leak, model-identity, and network canaries before the
runner is built. Any missing fail-closed capability is a blocker; it is not silently omitted or
replaced with a warning.

The v1 reference substrate is WSL2 Ubuntu 24.04. Every agent workspace, evaluator tree, temporary
home, and sandbox scratch directory is created on its native ext4 filesystem; `/mnt/*` for any of
those paths fails preflight. The read-only control checkout may be the same Windows checkout viewed
through WSL so Git worktrees and the Git-common evidence home remain one repository; its bytes are
fingerprinted and it is never mounted into either untrusted namespace. Bubblewrap is the Linux mount/PID isolation
primitive. The agent workspace is the only writable tree and the suite, oracle, reference patch,
run store, operator home, sibling cells, and project checkout are absent from the tool-visible mount
namespace; only required runtime files are read-only. The CLI control process can reach its provider,
while model-invoked commands use the provider's native no-network sandbox. The
[official OpenAI sandbox documentation](https://learn.chatgpt.com/docs/sandboxing) documents WSL2's
Bubblewrap prerequisite and the
[Codex network controls](https://learn.chatgpt.com/docs/agent-approvals-security) separate client
model/auth traffic from command traffic. Claude must enable its documented
[Linux/WSL2 Bubblewrap sandbox](https://code.claude.com/docs/en/sandboxing)
with `failIfUnavailable: true`, no excluded commands, no unsandboxed escape, explicit
read-denies, and an empty child-network allowlist. The installed versions and exact supported flags
remain preflight evidence, not assumptions frozen from this prose.

A filesystem security decision produces an owned descriptor capability, never durable authority in
a canonical path string. Caller-controlled roots are opened beneath a pinned directory FD with Linux
`openat2` and `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`; unsupported kernel
semantics fail preflight. Filesystem type, object type, and device/inode identity are validated from
the opened object with `fstat`/`fstatfs`, never a second pathname lookup. Harness runtime mounts,
network files, and the Bubblewrap executable are pinned before use too; only sandbox destination paths
remain strings. Capability overlap checks compare the held objects and FD-relative ancestry, not
canonical path strings.

Capture children are enumerated with `os.scandir(directory_fd)`, reopened one component at a time
relative to the pinned parent with no-follow flags, and rejected if their enumerated object type,
device, or inode differs from the opened child. Both resource scanners use that same FD-relative
walker and queue opened child-directory descriptors rather than `DirEntry.path`. Linux subprocess cwd
uses a passed directory descriptor through `/proc/self/fd/<n>` with `pass_fds` so a post-validation
rename cannot redirect `chdir`.

Bubblewrap must consume those same capabilities through `--bind-fd` and `--ro-bind-fd`, including
fail-closed mounted-object identity verification. Preflight records the pinned executable/version and
proves both FD-bind operations with live behavioral canaries; recognizing a flag or version string
alone is insufficient. Upstream Bubblewrap 0.11.2 is the pinned known-good implementation, while
Ubuntu 24.04's stock Bubblewrap 0.9.0 package lacks the required FD-bind contract. Before the real
Step-26 WSL gate, the operator must install a compatible Bubblewrap build in the fixed trusted
executable search path; tests never download, compile, or silently fall back to path binds. Missing
capability yields one actionable unavailable-substrate error and no untrusted command executes.

The effective model-tool surface is also equalized at the capability boundary. Codex receives only
its workspace file/edit and sandboxed shell tools, with built-in web search disabled under the
[Codex configuration contract](https://learn.chatgpt.com/docs/config-file/config-reference), no MCP
servers, and no browser/plugin bridge. Claude receives only `Bash`, `Read`, `Edit`, `Write`, `Glob`,
and `Grep`, pinned against the installed
[Claude CLI contract](https://code.claude.com/docs/en/cli-usage); `WebSearch`, `WebFetch`,
Chrome/browser integration, MCP, plugins, hooks, subagents, and
all other parent-mediated tools are absent or explicitly denied. The adapters must validate the
initialized/reported tool set rather than trust configuration text. Qualification asks each agent
to attempt both a shell network request and a first-class web/search request; success, an unexpected
tool, or a proxy request is a containment failure. If an installed CLI cannot expose enough
evidence to prove this allowlist, that version is unsupported.

Authentication belongs to the CLI control plane. The operator authenticates a benchmark-scoped WSL
account whose ephemeral `0700` home contains only pinned harness configuration and the minimum
credential material. Secrets never enter argv, prompts, child environments, manifests, traces, or
captured streams. Provider-native file/tool policy denies model tools the auth paths, parent
environment, and credential-bearing `/proc` state. Doctor supplies non-secret credential and
environment sentinels and fails unless the model's tools cannot read them. If either installed CLI
cannot preserve authenticated control-plane access while satisfying this tool-side secrecy contract,
live benchmarking is unsupported on that host.

### 6.5 Score a clean applied patch

The CLI event stream is trace evidence, not the production artifact. After each cell—including a
timeout—the harness captures all changes relative to the immutable seed, including tracked,
untracked, deleted, binary, staged, and agent-committed changes. No host-side Git command ever opens
the agent-owned `.git`. A separate fail-closed capture namespace mounts the submitted filesystem
read-only without its `.git`, plus a writable harness-owned repository reconstructed from the
immutable seed; a syscall-level copier overlays non-Git regular-file bytes/modes and explicit
deletions after rejecting symlinks and special files. Agent commits therefore count only through
their final working-tree bytes, while all agent-created Git metadata is quarantined as untrusted
diagnostic evidence.

The capture namespace has no network, credentials, operator/project/run-store mount, or inherited
Git environment. It uses harness-owned Git with `GIT_CONFIG_NOSYSTEM=1`,
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_ATTR_NOSYSTEM=1`, an empty fixed home/config, and
`core.hooksPath=/dev/null`; task seeds and submitted trees forbid `.gitattributes`, `.gitmodules`,
and changes to `.gitignore`. Capture uses fixed `git add -A -f -- .` and
`git diff --cached --binary --full-index --no-ext-diff --no-textconv` argv. The harness rejects any
config, filter, attributes, diff-driver, hook, path, or environment drift before Git starts. It then
rejects submodule changes, protected files, and paths outside `allowed_changes`; applies the captured patch to a
clean evaluator template and verifies the resulting tree hash. Each of two repetitions separately
materializes that same applied tree, injects the oracle read-only, and receives a fresh temp/home/PID
namespace. The harness compares both result summaries and post-evaluation tree hashes; no writable
residue from repetition one is visible to repetition two. A model timeout is retained as a secondary signal, but a correct patch present
at cutoff is still scored as the submitted artifact. Auth, model drift, sandbox escape, broken
anchors, or evaluator infrastructure faults invalidate/abort measurement rather than becoming
model zeros.

The clean evaluator is itself an untrusted-code sandbox, not merely another Git clone. It runs in a
separate Bubblewrap mount/PID/network namespace with no provider credentials, operator home, project
checkout, run store, or sibling cell mounted. Only the evaluator tree is writable; runtime and the
injected oracle are read-only; the environment is allowlisted; the network namespace has no egress;
and no target byte executes until a fresh cgroup has active, read-back memory, zero-swap, task, and
CPU-bandwidth guards and the evaluator tree is backed by a private byte/inode-bounded tmpfs. The
owner enforces wall timeout, while cumulative CPU seconds and logical tree file/byte counts are
sampled scoring thresholds rather than hard maxima; every resource-limit result records which layer
fired. Timeout kills the entire process group before collection. Malicious-patch fixtures attempt
host-sentinel reads, outside writes, oracle mutation, credential and parent-`/proc` reads,
TCP/UDP/DNS access, and detached children; a stateful fixture also leaves residue and proves that the
second repetition cannot see it. Any containment success records invalid error class `containment`,
suppresses run inference, and is never a model zero.

The v1 evaluator convention is fixed rather than task-authored. Every seed contains visible tests
under `tests/`; every oracle contains hidden tests under `tests/`. In each repetition the applied
tree is mounted writable at `/workspace`, the oracle is mounted read-only at
`/opt/measure-twice/oracle`, cwd is `/workspace`, and the harness invokes exactly
`/opt/measure-twice/runtime/python3.12 -B -s -P -m pytest -q --disable-warnings --maxfail=1
--basetemp=/tmp/pytest /workspace/tests /opt/measure-twice/oracle/tests`. The runtime path is a
read-only harness mount; `HOME=/tmp/home`, `TMPDIR=/tmp`, `PYTHONHASHSEED=0`, and
`PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` are fixed,
`PYTHONPATH` is absent, and no task can supply pytest configuration outside its
hashed seed. Validation rejects missing test roots or collection of zero visible/hidden tests.

`allowed_changes` uses a closed, case-sensitive POSIX full-path glob grammar: literal segments plus
`*` and `?` within a segment, and `**` only as a complete segment matching zero or more segments.
Backslashes, character classes, empty/absolute paths, and `.`/`..` segments are invalid. A changed
path is permitted when at least one pattern matches its complete normalized path; each
`protected_paths` entry denies that exact path and all descendants and overrides any allow match.
  `.git`, `.gitignore`, `.gitattributes`, `.gitmodules`, and the visible `tests` tree are always
  protected. Golden fixtures pin literal, `*`, `?`,
zero/multi-segment `**`, case mismatch, traversal, and protected-overrides-allowed cases.

### 6.6 Separate instrument, harness, and environment identity

- `instrument_hash`: suite/task manifests, prompts, seed trees, oracle trees, reference patches,
  constraints, and provenance-bearing task content.
- `harness_hash`: evaluator/scorer implementation version plus generic execution policy.
- `environment_fingerprint`: OS/runtime, dependency lock, benchmark commit, executable hashes,
  provider CLI versions, invocation hashes, and model/profile evidence.

Reports compare rows only when instrument and harness hashes match. Environment differences remain
visible and block automatic splicing. When Haiku is added later, Luna and Sonnet are rerun alongside
it under one environment rather than combining fresh Haiku rows with stale earlier rows.

### 6.7 Use deterministic binary quality with cluster-aware statistics

A task score is `1` only when the patch applies, all changed paths are permitted, and both evaluator
repetitions pass every visible and hidden test. Otherwise it is `0`. No LLM judge participates.

For model `m` and task `t`:

```text
task_rate(m, t) = mean(sample scores for m on t)
model_score(m) = 100 * mean(task_rate across tasks)
delta(a, b) = model_score(a) - model_score(b)
```

One immutable schedule block is `(task_id, sample_k)` with every selected model exactly once. Blocks
are adjacent and never split across tranches. For every byte-sorted block, compute lowercase hex
`SHA256(UTF8("schedule-block-v1\0" + schedule_seed + "\0" + family + "\0" + task_id + "\0" + sample_k))`,
where `sample_k` is one-based decimal; sort by `(digest, family, task_id, sample_k)`. Its zero-based
position among same-family blocks in that order is `family_ordinal`. For each safe model ID, compute
`SHA256(UTF8("schedule-model-v1\0" + schedule_seed + "\0" + family + "\0" + model_id))`; sort by
`(digest, model_id)` to form the family's base model permutation, then rotate left by
`family_ordinal mod model_count` inside the block. Each model's count in every ordinal position
therefore differs by at most one per family; for two models, first position alternates exactly.
Retries retain the original position and finish before advancing. Golden fixtures pin two- and
three-model schedules, and the complete block/cell schedule is stored before the first provider call.

The report includes all-pairs both-pass/neither-pass/A-only/B-only counts, model and per-tag scores,
failure modes, and a paired cluster bootstrap. For a scope with sorted independent cluster IDs
`c[0:K]`, each of 10,000 iterations draws exactly `K` clusters with replacement. For zero-based
`i = 0..9999` and `d = 0..K-1`, encode each index as unpadded ASCII decimal. Draw `d` in iteration
`i` selects:

```text
int(SHA256(UTF8("bootstrap-v1\0" + analysis_seed + "\0" + i + "\0" + d)), 16) mod K
```

Each selected cluster occurrence carries all its tasks, samples, and model arms together; arms are
never resampled separately. Expand each selected occurrence back to its task-rate rows, preserving
duplicate occurrences, and recompute the equal-task macro scores and their delta; do not replace the
declared estimand with an equal-cluster mean. Sort the 10,000 deltas; the two-sided 95% percentile
interval is one-based elements 250 and 9750
(`sorted[249]`, `sorted[9749]`). Golden fixtures pin schedule order, draw indices, task rates,
deltas, and endpoints. Luna/Sonnet v1 has one confirmatory scope—the overall score. Per-family and
tag intervals are emitted only when they contain at least eight independent `cluster_id` values and
are labeled `EXPLORATORY`; they cannot produce a routing or superiority verdict.

The Luna/Sonnet v1 `AnalysisPlan` sets candidate `codex-luna`, reference `claude-sonnet`, policy
`superiority`, exactly one confirmatory `overall` scope, no multiplicity adjustment, and a symmetric
5-point minimum-effect rule:

- `CANDIDATE_ADVANTAGE` only when the lower 95% bound for `Luna - Sonnet` is strictly above +5.
- `REFERENCE_ADVANTAGE` only when the upper 95% bound is strictly below -5.
- `INCONCLUSIVE` otherwise.

The generic report also supports a preregistered simultaneous set of candidate/reference
non-inferiority decisions. If a later Haiku study designates `S` family/tag routing scopes, each
scope uses a Bonferroni-adjusted two-sided interval with tail `q = 0.025 / S`. For 10,000 sorted
bootstrap deltas, use conservative one-based ranks `max(1, floor(10000*q))` and
`min(10000, ceil(10000*(1-q)))`; for four families these are elements 62 and 9938
(`sorted[61]`, `sorted[9937]`). In each preregistered scope, Haiku is candidate and Sonnet reference
with a five-point quality margin: `CANDIDATE_ELIGIBLE` only when the adjusted lower bound for
`Haiku - Sonnet` is above -5; `REFERENCE_REQUIRED` when the adjusted upper bound is below -5;
otherwise `UNRESOLVED`. This is a quality eligibility result, not a cost/latency claim.

### 6.8 Pin limits, artifacts, tranches, and retries

The versioned execution profile fixes every resource rule that can affect fairness or resume
identity. `evaluator_memory_bytes` is the cgroup memory hard guard and
`evaluator_processes` is the whole-cgroup task guard (Linux counts threads/TIDs). Swap is disabled
by the sandbox contract. `evaluator_cpu_bandwidth_percent`, `evaluator_tmpfs_bytes`, and
`evaluator_tmpfs_inodes` pin the independent CPU-rate and writable-storage safety envelope.
`evaluator_cpu_s`, `evaluator_files`, and `evaluator_file_bytes` are sampled aggregate scoring
thresholds; crossing one can overshoot between observations, and the observed value plus provenance
is evidence rather than a claim that the threshold was a hard maximum. The bounded tmpfs must fit
the applied-tree baseline before target release; inability to materialize that baseline is evaluator
infrastructure failure, not a model resource limit:

- The committed v2 sandbox profile fixes the hard guard at 1 GiB memory, zero swap, 64 tasks,
  100% CPU bandwidth (one CPU of quota per period), 64 MiB tmpfs capacity, and 20,001 tmpfs inodes.
  Its sampled thresholds are 60 cumulative CPU seconds, 10,000 logical files, and 10 MiB of logical
  tree bytes. The larger storage guard allows page rounding and directory metadata without turning
  the logical scoring thresholds into host-capacity claims.

- Pilot and observation cells: one process at a time; agent timeout 600 seconds; evaluator timeout 60 seconds
  for each of two repetitions; maximum 100 changed paths; 5 MiB binary patch; 10 MiB captured bytes
  per stdout/stderr/trace stream; and 25 MiB total cell artifacts. Crossing a model-controlled
  process/trace/patch/artifact limit terminates collection, preserves bounded evidence, and records
  `resource-limit` score zero. Independent harness storage I/O or guard-enforcement failure is
  `artifact-store` or `evaluator-infrastructure` invalidity and is never silently truncated.
- Qualification and smoke cells: agent timeout 60 seconds and evaluator timeout 30 seconds
  per repetition, with the same path/patch/trace ceilings.
- Scheduling: concurrency is always 1. In the committed v1 execution profile, smoke is one
  two-model block with two cells and four provider attempts. Pilot contains at most its 24 cells and has an
  eight-hour wall budget. V1 runs in tranches of at most 32 cells (16 complete two-model pairs),
  never splitting a pair, with the same eight-hour wall budget per invocation. Resume continues the
  immutable schedule.
- Retries: at most one fresh-workspace retry per cell, counted in the provider-attempt budget, only
  for an explicit rate-limit, provider 5xx, or transport failure before a valid terminal event.
  Honor a provider retry-after value up to 60 seconds; otherwise wait 5 seconds. Timeout, auth,
  malformed contract, model mismatch, sandbox/canary, evaluator, forbidden edit, and ordinary task
  failure are never retried automatically.
- Budgets: the committed v1 profile permits smoke 2 terminal cells/4 provider attempts, pilot 24/48, and observation 192/384. Each exact
  `RunPolicy` satisfies the schedule equations above. The manifest records cells, attempts, retries, limits,
  tranche boundaries, and wall time separately.

If the permitted fresh-workspace retry also ends in an eligible rate-limit, provider-5xx, or
pre-terminal transport failure, the row is `invalid` with error class `infra-exhausted`. The runner
atomically preserves both attempts, marks the run `invalid-infrastructure`, finishes no new block,
and leaves `quality_verdict` null. Reports may render diagnostics but refuse inference and exit
non-zero. The run cannot be repaired by reopening the cell, increasing its budget, dropping the
pair, imputing a score, or splicing another run; after the external fault is corrected, the operator
starts a new run ID over the complete frozen schedule. Auth/model/sandbox preflight failure stops
before a provider cell; mid-run auth expiry invalidates the run and likewise requires a new run.

Changing any limit, retry class, or analysis seed changes `harness_hash`; resume against a different
profile fails loud.

The runner acquires `<run-root>/.owner.lock` with exclusive create before reading resume state or
writing a cell. The strict `RunOwnerLock` records run, host fingerprint, PID, random token, and UTC
acquisition. A second fresh/resume process fails before mutation. Automatic stale recovery is
permitted only when the host fingerprint matches, the OS proves that PID is absent, and the lock is
older than 600 seconds; recovery atomically renames it under the run's quarantine before acquiring
a new lock. Cross-host, young, or unverifiable locks require operator diagnosis and are never
deleted automatically. Ctrl-C finalizes the current attempt then releases the lock; a report may
read finalized rows while locked but must mark the run incomplete and suppress inference.

### 6.9 Calibrate before observing

`coding-agent-pilot-v0` contains 12 dependency-free tasks: three each for localized bug repair,
bounded feature work, behavioral refactoring, and CLI/data-boundary handling, with easy/medium/hard
coverage. Its suite and manifest are hard-labeled `run_class: "pilot"`; report and evidence derive
`pilot_not_ranking: true` and can never emit a quality verdict.
After pilot findings, `coding-agent-v1` is authored from 32 fresh held-out tasks—eight independent
clusters per family (one task per cluster), with exactly three easy, three medium, and two hard
tasks in each family—so
exploratory family estimates satisfy the min-n convention. Pilot
tasks become regression fixtures and never re-enter the fresh v1 evidence set.

Task bundles are code-shaped artifacts, so build agents may scaffold or draft prompts, seeds,
oracles, tests, and reference patches. That assistance never establishes gold: each task records the
authoring product/model; an unavailable identity makes a v1 task ineligible until a human or known
non-arm producer independently rewrites it. All draft
artifacts must pass deterministic negative/reference anchors, and the Step-52 humans independently
inspect and adopt responsibility for every final v1 artifact. At least one human reviewer is
independent of the task author. Any evaluated-arm assistance is disclosed as a contamination risk;
neither arm ever receives oracle/reference content, and no v1 output may be used to revise v1.
Alternative rejected: claiming human-only authorship while `/build-step` necessarily uses a model
developer to create code-shaped bundles.

### 6.10 Keep execution one-shot and future operations separate

The feature adds no daemon, scheduler, monitor, or always-on process; the autonomous-behavior
observation trigger does not fire. It does add a schema-bound data pipeline, so Step 30 first
qualifies live provider containment and Step 35 is a real two-cell full-pipeline smoke before either
observation suite is authored. Generic agent-run catalog or leaderboard integration is future
post-Step-55 work; this plan does not alter the prompt-run scope reserved for Steps 19-20.

### 6.11 Build and operator quickstart

| Need | Command |
|---|---|
| Install | `uv sync --extra dev` |
| Develop/first offline run | `uv run mt agent validate suites/agents/smoke --structure-only` |
| Build package | `uv build` |
| Test | `uv run pytest -q` |
| Lint/format | `uv run ruff check .` and `uv run ruff format --check .` |
| Typecheck | `uv run mypy --strict measure_twice` |
| Linux isolation integration from Windows | `pwsh -File scripts/test-agent-bench-wsl.ps1` |
| Linux isolation integration inside WSL | `uv run pytest -q -m linux_isolation` |
| Bubblewrap capability prerequisite | A trusted installed `bwrap` must pass Step 26's live `--bind-fd` and `--ro-bind-fd` probes. Upstream 0.11.2 is pinned known-good; Ubuntu 24.04's stock 0.9.0 package is rejected. |
| Resource-guard prerequisite | The fixed trusted `/usr/bin/systemd-run` must create a delegated transient user scope whose effective cgroup-v2 memory, zero-swap, task, and CPU-bandwidth controls can be read back before target release; unprivileged private tmpfs mounts must enforce both byte and inode limits. |

Live work requires `measure-twice` and `switchboard` as sibling sources because the existing
`pyproject.toml` resolves `../switchboard`. A WSL-local clone is preferred; when orchestration began
from this Windows checkout, the runbook uses that same Git repository through WSL but stages the
package, dependency, temporary homes, agent workspaces, and evaluators onto ext4. The Step-29
runbook pins the exact commands, but the fresh-context path is: install Git, Python 3.12, `uv`,
a Bubblewrap build that passes the FD-bind behavior probe, `socat`, and the supported Linux
Codex/Claude CLIs in WSL2 Ubuntu 24.04; ensure the two
sibling sources are present; reject `/mnt/*` for every untrusted workspace/scratch path; run
`uv sync --extra dev`; authenticate the benchmark-scoped account
inside WSL using current provider instructions; run the full test/build gates; then use `doctor
--dry-run` and its exact confirmation digest before either live qualification call. The runbook also
records version/auth checks, expected exit codes, progress, Ctrl-C behavior, cleanup, secret-redaction
checks, invalid-run handling, and copy-paste validate/doctor/run/resume/report commands. The operator
must confirm that each account/plan authorizes the intended noninteractive workload without storing
account identifiers or secrets. The Windows integration script enumerates only tracked and
non-ignored worktree files plus the sibling `switchboard` source, streams that exact manifest into
a fresh WSL-ext4 temporary directory, installs there, runs the marked tests, reports the staged-tree
hash, and removes the temporary directory; it never copies `.git`, `.venv`, data homes, credentials,
or untracked ignored files.

The Linux marker is non-skipping for the isolation substrate: absent or incompatible Bubblewrap is
a failed prerequisite with a stable diagnostic, not a pytest skip or reduced-coverage pass.

## 7. Build Steps

Run `uv sync --extra dev` before the first code step. Every code step must pass `uv build`,
`uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, and
`uv run mypy --strict measure_twice` before it is marked complete. Tests remain inference-free
unless the step is explicitly typed `operator` or `wait`.

Step 26 and every later code step that touches `agent_bench`, an agent suite, or an agent profile
also runs the real Linux isolation marker. From Windows the gate is
`pwsh -File scripts/test-agent-bench-wsl.ps1`; from a WSL-ext4 checkout it is
`uv run pytest -q -m linux_isolation`. The ordinary pytest command remains green on native Windows
with Linux-only cases explicitly selected out; unit, authoring, structure-only validation, evidence-import, and
report paths do not require inference.

<!-- autofix-applied: 2026-08-21 -->
### Step 25: Strict agent inputs and structural validation
- **Problem:** Define the strict `ModelSpec`, `AnalysisPlan`, execution-profile, `AgentSuite`, and `AgentTask`
  contracts; safe identifiers; recursive instrument/profile hashing; path and symlink containment;
  the candidate Luna/Sonnet registry plus an offline Haiku fixture; a one-task smoke bundle; and the
  `mt agent validate --structure-only` surface. Keep model profiles outside suite identity.
- **Type:** code
- **Issue:** #27
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/{__init__,_wire,_win32_contained,models,analysis,suite,cli}.py`,
  `measure_twice/cli.py`, `profiles/agent-models-candidates.json`,
  `profiles/agent-execution-v1.json`, `analysis-plans/agent-smoke-v1.json`,
  `suites/agents/smoke/`, `docs/agent-benchmark/smoke-preregistration.md`,
  `.gitignore`, `.gitattributes`,
  `tests/agent_bench/__init__.py` (package marker preventing collection collisions with the legacy
  `tests/test_suite.py` module), `tests/agent_bench/test_models.py`, `tests/agent_bench/test_analysis.py`,
  `tests/agent_bench/test_suite.py`,
  `tests/agent_bench/test_cli.py`, `tests/agent_bench/fixtures/wire/inputs/`
- **Done when:** strict loaders reject unknown/missing keys, bad types, duplicate or unsafe IDs,
  escaping paths, symlinks/junctions, missing assets, arbitrary task commands, and unsupported
  schema versions; canonical selected-profile and instrument hash goldens are frozen; run-class,
  evaluator-layout, allowed/protected-glob, and `policy: "none"` analysis-plan goldens are frozen;
  the one-task bundle declares
  `run_class: "smoke"`, its no-ranking preregistration cites the analysis-plan hash, and it passes
  the real structure-only CLI; generated default data roots are ignored while suites,
  profiles, and tracked evidence remain visible; and a three-profile offline fixture proves two Claude
  profiles dispatch by provider without using `CLAUDE_ALIASES`.
- **Depends on:** 7 (shipped)

<!-- autofix-applied: 2026-08-21 -->
### Step 26: Linux process and isolation substrate
- **Problem:** Complete the fail-closed Linux execution substrate under one kernel-object-identity
  invariant: every host filesystem source is opened once as an owned Linux file-descriptor (FD)
  capability, so later rename, unlink, or symlink replacement cannot redirect sandbox mounts,
  subprocess cwd, capture enumeration, live resource monitoring, or terminal tree validation.
  Preserve the immutable stdin/environment contract, bounded streams, secret scrubbing, and
  whole-process-tree termination. Replace polling-as-containment with a two-layer evaluator resource
  contract: hard cgroup and private-tmpfs host guards are active and read back before target release,
  while cumulative CPU and logical-tree thresholds remain explicitly sampled scoring rules with
  recorded provenance. This step supplies mechanics and canaries only; it neither understands
  provider event formats nor makes inference calls.
- **Type:** code
- **Issue:** #28
- **Flags:** --reviewers deep --isolation worktree
- **Environment prerequisite:** The non-skipping Linux gate runs on WSL2 Ubuntu 24.04 with every
  caller-supplied workspace and staging path on WSL ext4. Its unified cgroup v2 delegation and user
  service manager must behaviorally support a fresh transient scope whose effective memory, swap,
  pids, and CPU controls can be read back by the harness, and an unprivileged private mount namespace
  must support tmpfs `size` and `nr_inodes` hard limits. Bubblewrap must pass live
  behavioral probes for `--bind-fd` and `--ro-bind-fd`; version text is evidence only. Upstream
  Bubblewrap 0.11.2 is the pinned known-good implementation, while Ubuntu 24.04's stock 0.9.0 package
  is unsupported. Missing or incompatible cgroup delegation, bounded-tmpfs behavior, or Bubblewrap
  raises a stable `IsolationUnavailableError` and fails the WSL gate rather than skipping it. There
  is no polling-only or path-bind fallback. No provider authentication or live model call is
  required.
- **Capability contract:** Pathnames are accepted only at root capability acquisition.
  Caller-controlled roots are opened with Linux `openat2` beneath a pinned directory FD using
  `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`; unsupported kernel semantics fail
  preflight. Filesystem/type/identity checks use the opened descriptor (`fstat`/`fstatfs`), never a
  later pathname reconstruction. Direct-child traversal treats `readdir` output only as an untrusted,
  validated component name: a no-follow `openat2` is that child's acquisition boundary, type and
  identity come only from `fstat` of the held FD, and FD-relative name rebindings before and after use
  must still resolve to that held object. Harness runtime files, network files, and the fixed trusted
  executables `/usr/bin/systemd-run` and `/usr/local/bin/bwrap` are likewise pinned before use; only sandbox
  destination paths remain pathname strings. Capability overlap checks compare the held objects and
  FD-relative ancestry, not canonical strings.

  | Internal type | Required fields and lifetime |
  |---|---|
  | `LinuxPathCapability` | owned `fd`; diagnostic-only `display_path`; `st_dev`, `st_ino`, `st_mode`, and filesystem evidence captured from that FD; explicit open/closed state; non-copyable, non-serializable, context-managed, and idempotently closed |
  | `LinuxResourceGuard` | immutable hard-control configuration: positive memory/task limits and at most 100% CPU bandwidth; the internal runtime guard owns the fresh exact scope path/identity, cgroup-directory capability, pre-opened non-inheritable `cgroup.kill` FD, read-back controls/counters, pre-release namespace-supervisor identity, and active/empty/closed/collected evidence |
  | `EvaluatorScratch` | one-shot private tmpfs configuration plus the pinned applied-tree source and retained root FD; read-back `size`/`nr_inodes`, an internal exclusive-copy-destination capability, applied-tree baseline evidence, mount-namespace teardown evidence, and explicit closed state; the same root FD survives namespace detach and is used for the Bubblewrap mount, live scans, terminal usage validation, and later Step-27 authoritative result-tree snapshot |
  | `SandboxLaunch` | profile, command/environment, FD-backed writable/read-only source mounts, optional evaluator scratch replacing the evaluator's direct writable source mount, resource guard, limits, and one-shot consumed state; it renders `--bind-fd`/`--ro-bind-fd` only while creating the process request and retains the evaluator tmpfs root FD through terminal usage validation |
  | `ProcessRequest` | immutable argv/stdin/environment/limits plus an owned cwd capability, inherited mount FDs, optional duplicated tree-monitor capability, owned resource guard/release barrier, and one-shot consumed state |
  | `ProcessResult` | bounded streams and terminal status plus nullable resource name, configured limit, observed value, and `hard-guard` or `sampled-threshold` provenance; live tree-sample inconclusive count; guard-health failure is an execution error, never a resource result |

  On Linux, `run_process` supplies every inherited descriptor through `Popen(pass_fds=...,
  close_fds=True)`, enters cwd through `/proc/self/fd/<cwd-fd>`, and executes pinned executable
  descriptors through `/proc/self/fd/<executable-fd>`. It invokes `systemd-run --user --scope
  --collect --slice=app.slice` with a harness-generated `measure-twice-<128-bit-lowercase-hex>` unit
  name (whose resulting scope is suffixed `.scope`) and only the
  controller environment needed to reach `/run/user/<uid>/bus`; that controller environment never
  enters the target's separately allowlisted environment. Scope creation places the outer namespace
  owner in the cgroup before it can create the target. The namespace supervisor mounts the bounded
  evaluator tmpfs directly over the existing `/var/tmp` only in its private outer mount namespace—
  leaving Bubblewrap's hard-coded `/tmp` setup path unobscured—and opens
  `cgroup.kill` relative to its exact cgroup directory, and sends exactly three FDs to
  the parent in one pre-release Unix-socket `SCM_RIGHTS` handshake: cgroup directory, mandatory
  evaluator tmpfs root, and pre-opened `cgroup.kill`. Receipt atomically requests close-on-exec and
  explicitly clears inheritance before interpretation. The parent verifies the generated scope's
  exact cgroup-v2 path and held identity, rejects missing, extra, malformed, or mismatched
  capabilities, and keeps the target behind the release
  barrier until every effective control and required event counter has been read back and exactly one
  cgroup member has been bound as the PID-namespace supervisor by `(host pid, starttime)`, an `NSpid`
  chain ending in namespace PID 1, and exact unified-cgroup membership; attachment after target
  execution begins is forbidden. The received tmpfs root becomes an exclusive copy destination only
  after its filesystem, physical bounds, and emptiness are validated while the target remains behind
  the barrier. The applied tree is copied FD-relative into the already bounded private tmpfs before
  release. Its
  baseline and terminal usage are validated through the retained tmpfs root FD, never by reopening a
  staging pathname; Step 27 later owns the authoritative result-tree snapshot and hash through that
  same capability. Launch-only parent descriptors close after successful `Popen`; monitoring, guard,
  and evaluator-scratch descriptors remain open through process collection and terminal validation.
  Every success, startup failure, timeout, interrupt, resource crossing, and caller exception kills
  the scope if it is populated, proves it empty through the held directory when controls remain, and
  boundedly proves that `--collect` removed the same fresh cgroup without authorizing a replacement
  object. Systemd may retire a fast-empty scope's path and controls even while a stale directory FD is
  retained. That collected branch is terminal only after the pre-release path/identity is proved, the
  exact namespace-supervisor `(pid, starttime)` is gone, the exact path is absent, and a complete
  trusted supervisor record supplies terminal cgroup CPU/event attribution; stale handles then close
  and exact-path absence is proved again. Before any valid handshake, the direct `systemd-run` owner
  must be reaped and the generated scope path must remain absent for a full bounded interval—a first
  `ENOENT` is not proof. The host namespace's `/var/tmp` identity never changes and no generated host
  mountpoint exists. Bubblewrap consumes the linked private `/var/tmp` root by FD as `/workspace`
  while creating its own inner `/tmp`; namespace-owner exit/reap detaches the outer mount. The parent-held
  root FD deliberately retains that detached tree through terminal validation and Step 27, and the
  final owned FD close releases it. Setup, readback, attribution, namespace teardown, or collection
  uncertainty fails closed. Only after the exact cgroup is empty and the namespace owner is reaped may
  the retained scratch capability supply the strict terminal logical-tree scan; a pre-kill forced poll
  is still a live sample and cannot authorize invalidity from model-controlled churn.
  Reusing a consumed request or launch fails.
- **Traversal contract:** Capture and evaluator walkers call `os.scandir(directory_fd)` only to
  discover validated single-component names. They immediately acquire each child relative to the
  held parent with a no-follow open, derive type/identity/metadata solely from `fstat` of that opened
  object, retain the child FD across the race seam and use, and prove the current name still binds to
  that same held identity. An earlier `DirEntry.stat()` tuple is never durable authority, so inode
  unlink/recreate reuse cannot redirect a later read. Directory namespaces and metadata are checked
  across each completed visit; regular-file copy checks pre/post metadata and verifies copied bytes
  against a second stable read, rejecting same-size in-place mutation. Symlinks, special files,
  unreadable objects, post-acquisition name replacement, or source drift fail closed before a
  replacement object's bytes are consumed. A replacement completed before the no-follow open may be
  acquired as the current object only when the enclosing namespace remains coherent; otherwise the
  operation fails closed, and it never falls back to a stale enumerated identity. This contract does
  not claim an atomic whole-tree snapshot against pre-acquisition churn. The walker preserves a typed
  distinction among per-visit namespace/metadata drift, stable model-created tree-policy violations,
  configured logical-limit crossings, and genuine capability/kernel/I/O failures. Copy, capture, and
  post-empty terminal use remain strict. A live sampled scan catches only the drift/policy outcomes,
  records no successful sample, preserves prior high-water evidence, and retries; all other failures
  retain their fail-closed classification. The already-validated private tmpfs
  exclusivity—not a
  racy `mkdir`/`stat` tuple—authorizes destination creation; ordinary destination capabilities are
  rejected. One shared FD-relative walker supplies both the sampled live evaluator threshold
  monitor and terminal `measure_tree_usage`/snapshot from the retained tmpfs root FD, preventing the
  scanners from drifting. Polling never supplies a host-safety boundary.
- **Files:** `measure_twice/agent_bench/{_linux_capabilities,models,process,isolation}.py`,
  `profiles/agent-execution-v1.json`, `scripts/test-agent-bench-wsl.ps1`, `pyproject.toml`,
  `tests/agent_bench/test_linux_capabilities.py`, `tests/agent_bench/test_models.py`,
  `tests/agent_bench/test_process.py`,
  `tests/agent_bench/test_isolation.py`,
  `tests/agent_bench/fixtures/isolation/`
- **Done when:** `uv build`, full pytest, Ruff lint/format, strict mypy, and the non-skipping
  `pwsh -File scripts/test-agent-bench-wsl.ps1` gate pass with all of this evidence:
  - Exact UTF-8 stdin, FD-pinned cwd, allowlisted environment, bounded raw streams, and secret
    detection remain frozen; success and every error/interrupt path leave no FD leak.
  - Timeout and interrupt kill and reap the complete Linux-owned descendant tree, including detach
    and double-fork fixtures identified by `(pid, starttime)`.
  - A barrier canary proves no evaluator target byte executes before cgroup membership and effective
    `memory.max`, `memory.swap.max=0`, `pids.max`, and `cpu.max` are read back. Multi-descendant
    allocation and fork-bomb canaries hit the hard memory/task guards; CPU bandwidth remains bounded.
    Missing controllers, delegation, mismatched readback, attribution loss, monitor failure, or
    non-empty/failed cgroup teardown fails closed as evaluator infrastructure, with no polling
    fallback. Protocol regressions cover exactly-three-FD close-on-exec receipt, malformed/mismatched
    cgroup, scratch, or `cgroup.kill` capabilities, release failure, every post-handshake startup
    failure, direct-owner reap failure, stable no-handshake absence, fast normal scope collection,
    and a descendant that outlives the nominal target.
  - Every evaluator repetition receives a fresh private tmpfs whose byte and inode safety envelope is
    active before the applied tree is copied. Parallel large-writer and many-small-file canaries hit
    that envelope without consuming the backing ext4 volume. Baseline validation, the writable
    Bubblewrap mount, live scans, and terminal usage validation all consume the same held tmpfs root
    identity that Step 27 will use for its result-tree snapshot. Success and pre-release-failure
    regressions prove the host `/var/tmp` identity never changes while real Bubblewrap and the terminal
    scanner consume the private retained FD before and after outer-namespace teardown; cancellation or
    namespace-teardown uncertainty remains an infrastructure failure while cgroup cleanup completes.
  - Cumulative CPU seconds aggregate through cgroup accounting, while logical file-count and
    tree-byte thresholds use the shared FD-relative walker. These are sampled scoring thresholds and
    may overshoot between observations; live namespace churn records an inconclusive poll and retries
    without erasing prior evidence, while the strict authoritative scan runs only after cgroup-empty
    proof. Tests assert correct termination and recorded configured
    limit, observed value, and `sampled-threshold` provenance rather than claiming a hard maximum.
    Hard memory/task events record `hard-guard` provenance. Terminally exhausted tmpfs blocks/inodes
    may record `file-bytes`/`file-count` `hard-guard` provenance with the physical tmpfs limit only
    when the retained FD proves exhaustion; a transient handled `ENOSPC` is never inferred after
    deletion. Per-process `RLIMIT_FSIZE` and `RLIMIT_NOFILE` remain backstops and are never reported as
    aggregate enforcement. Regressions cover live drift followed by a stable retry, persistent live
    churn with a post-empty threshold crossing, post-empty drift failing closed, a stable model-tree
    policy violation remaining distinguishable for Step 27, and an unrelated capability/I/O error
    remaining evaluator infrastructure.
  - Real Bubblewrap canaries prove workspace-only writes, absent suite/oracle/run/operator-home
    mounts, read-only oracle/runtime mounts, capture with submitted `.git` absent, empty child
    network for capture/evaluator, and no host/credential/parent-`/proc` disclosure.
  - Barrier-controlled, sleep-free rename-to-symlink regressions mutate each caller-supplied source
    after acquisition but before consumption: agent workspace; capture submitted root and direct
    child; capture repository; evaluator workspace, oracle, and runtime; process cwd; live tree
    scanner child; terminal tree scanner child. Root/cwd cases must consume the originally opened
    inode; child-entry identity changes must produce the documented fail-closed error; no case may
    read, mount, execute from, or count the replacement target. Direct-child regressions include
    unlink/recreate inode reuse after the no-follow acquisition, same-size mutation during copy, late
    add/remove/rename, rejection of non-exclusive copy destinations, and success/error FD baselines.
  - Existing hostile fixtures still cannot mutate an oracle, write outside the sandbox, reach
    TCP/UDP/DNS, inspect parent credential state, or leave a detached child. No provider credentials
    or live calls are used.
- **Depends on:** 25 (shipped)

<!-- autofix-applied: 2026-08-21 -->
### Step 27: Sandboxed evaluator and complete validation
- **Problem:** Build authoritative full Git change capture, allowed/protected-path enforcement,
  clean patch application and tree-hash verification, hidden-oracle injection inside the evaluator
  sandbox, twice-run deterministic scoring, anchor checks, and the full `mt agent validate`
  behavior. Harness-owned commands replace task-supplied shell text.
- **Type:** code
- **Issue:** #29
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/evaluator.py`,
  `tests/agent_bench/test_evaluator.py`, `tests/agent_bench/fixtures/evaluator/`,
  `suites/agents/smoke/`
- **Done when:** fixtures cover create/edit/delete/binary/mode changes and agent-created commits;
  untouched seed and no-op/garbage fail while the reference patch passes twice; patch capture
  round-trips into a fresh evaluator tree with an identical tree hash; malicious agent `.git`
  config, hooks, attributes, clean/smudge filters, external diff/textconv, environment injection,
  and metadata-only commits never execute and cannot affect the patch; submitted forbidden
  paths/symlinks/submodules, test timeout/resource exhaustion, model-caused hard guards or sampled
  thresholds, and submitted repetition disagreement map to their specified scored-zero outcomes;
  successful escape/oracle
  mutation, anchor/enforcement failure, and baseline/reference disagreement map to their specified
  invalid categories; stateful and malicious fixtures prove separately materialized repetitions;
  full `mt agent validate` runs all anchors only in the accepted Linux sandbox and never turns a
  harness fault into a model zero, while native Windows fails before task execution unless
  `--structure-only` is set. Each repetition captures and consumes its result-tree snapshot through
  Step 26's retained tmpfs-root capability and carries its hard-guard versus sampled-threshold resource
  provenance into evaluator evidence; it cannot substitute a pathname reconstruction or
  monitor-only launch.
- **Depends on:** 26 (shipped), including its non-optional cgroup, bounded-tmpfs, and retained terminal-FD
  contract

<!-- autofix-applied: 2026-08-21 -->
### Step 28: Codex adapter and doctor contract
- **Problem:** Add the provider-neutral adapter protocol and Codex JSONL implementation, including
  executable/version/auth/model-capability preflight, exact invocation fingerprints, fresh
  ephemeral sessions, explicit model/effort and working directory, sandbox policy, terminal/error
  event parsing, usage capture, and the dry-run/confirmation half of `mt agent doctor`.
- **Type:** code
- **Issue:** #30
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/adapters/{__init__,base,codex_cli}.py`,
  `measure_twice/agent_bench/cli.py`, `tests/conftest.py`, `tests/agent_bench/test_adapter_base.py`,
  `tests/agent_bench/test_codex_adapter.py`, `tests/agent_bench/fixtures/codex/`
- **Done when:** offline contract tests pin exact argv/stdin/cwd/sanitized environment and every
  accepted terminal/error stream; missing executable/auth/model/sandbox capability, malformed or
  incomplete JSONL, empty result, model-evidence mismatch, effective-tool/MCP/plugin/browser drift,
  attempted first-class web use, timeout, and stream overflow fail closed; `doctor --dry-run`
  emits the canonical one-shot receipt/digest and writes only its pending confirmation file—no
  evidence or run; automated tests make zero inference calls.
- **Depends on:** 26 (shipped)

<!-- autofix-applied: 2026-08-21 -->
### Step 29: Claude parity and live-operation runbook
- **Problem:** Implement the Claude stream-JSON adapter against the same protocol and finish the
  provider-neutral doctor command. Document the exact WSL2 setup, current supported CLI discovery,
  authentication boundary, canary interpretation, confirmation, cleanup, interruption, and failure
  recovery needed by a fresh operator. Do not assume unsupported flags or a resolved-model field.
- **Type:** code
- **Issue:** #31
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/adapters/claude_cli.py`,
  `measure_twice/agent_bench/cli.py`, `tests/agent_bench/test_claude_adapter.py`,
  `tests/agent_bench/test_doctor.py`, `tests/agent_bench/fixtures/claude/`,
  `docs/agent-benchmark/live-runbook.md`
- **Done when:** the Codex and Claude adapters pass the same offline request/result suite; exact
  supported invocation and event contracts are pinned in versioned help/event fixtures; Claude
  fails unless Bubblewrap is mandatory, unsandboxed escape/exclusions are disabled, reads are
  explicitly denied, child-network allowance is empty, the effective tool set is exactly
  `Bash,Read,Edit,Write,Glob,Grep`, and WebSearch/WebFetch/Chrome are unavailable; doctor atomically writes one
  secret-scrubbed evidence file only after a matching confirmation digest; the runbook has
  copy-paste install/build/test/validate/doctor commands, Git-common state-home resolution,
  pending-receipt expiry/retry, expected exits, progress/Ctrl-C behavior, secret checks, and
  account-authorization checks; no live call is made by tests.
- **Depends on:** 28

<!-- autofix-applied: 2026-08-21 -->
### Step 30: Qualify live provider containment and identity
- **Problem:** In the accepted WSL2 Ubuntu 24.04 environment, follow the runbook's exact supported
  CLI install/authentication checks, then run one adapter-owned doctor invocation for `codex-luna` and one for
  `claude-sonnet`. Each invocation must exercise intended-model evidence, an allowed write, a denied
  outside write, instruction and credential sentinels, denied child network, and a denied
  first-class web/search tool attempt.
- **Type:** wait
- **Issue:** #32
- **Evidence:** two raw, secret-scrubbed doctor records at append-only shared-state paths
  `<git-common-dir>/agent-bench-state/exports/qualifications/<profile-id>/<source-id>.json`, where
  `source-id` is minted before dry-run as `q_YYYYMMDDTHHMMSSZ_6hex` and the final receipt binds that
  already-known destination;
  no tracked diff or benchmark run
- **Done when:** the operator reviews each dry-run roster and enters its exact confirmation digest;
  both calls finish within 60 seconds and record secret-free invocation/provider fingerprints;
  requested/resolved identity evidence is acceptable; outside writes, sentinel reads, and child
  network and first-class web tools fail closed; ambient `AGENTS.md`/`CLAUDE.md`, MCP, plugins, and
  prior sessions are absent; the two selected records contain no secret or absolute path and hash
  the inspected raw evidence. Any failure blocks Step 31 and later work;
  native Windows fallback is not accepted.
- **Depends on:** 29

<!-- autofix-applied: 2026-08-21 -->
### Step 31: Verify and commit provider qualification evidence
- **Problem:** Add the offline evidence-import boundary, then use it to transform the two raw
  qualification records from the shared Git state home into strict, secret-scrubbed, hash-linked
  tracked snapshots that every later worktree can consume.
- **Type:** code
- **Issue:** #33
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/evidence.py`,
  `measure_twice/agent_bench/cli.py`, `tests/agent_bench/test_evidence.py`,
  `tests/agent_bench/fixtures/wire/evidence/`,
  `docs/agent-benchmark/evidence/provider-qualification-v1.json`,
  `docs/agent-benchmark/evidence/archive/`
- **Done when:** `mt agent evidence import --kind qualification` strictly validates both source
  records, rejects unknown fields/hash drift/secret-like values/absolute paths/wrong or duplicate
  profile IDs/source bytes while allowing multiple distinct profiles from one provider, verifies
  their raw and qualification-environment hashes from the shared state home, refuses an existing
  destination, and writes one canonical `QualificationBundle` atomically with Luna then Sonnet in
  safe-ID byte order; fixtures also pin the later generic `run` and `validity` import
  shapes; the real bundle matches both Step-30 records and is committed without raw traces or
  credentials. On requalification, the code step first moves the existing tracked active bundle to
  `archive/provider-qualification-v1-<old-source-set-sha256>.json` using all 64 hex characters
  (refusing collision), imports the
  new bundle at the stable active path, then routes each affected downstream hash-bearing artifact
  back through its owning later step; raw source-ID paths and Git history are never overwritten or
  deleted.
- **Depends on:** 30

<!-- autofix-applied: 2026-08-21 -->
### Step 32: One-block runner and atomic artifact store
- **Problem:** Implement immutable manifest creation and one two-profile schedule block through two
  fake-adapter cells in fresh workspaces/sessions, patch/evaluator integration, bounded artifact capture, canonical terminal row
  emission, atomic cell finalization, and content-addressed artifact verification through
  `mt agent run`.
- **Type:** code
- **Issue:** #34
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/runner.py`, `measure_twice/agent_bench/cli.py`,
  `tests/agent_bench/test_runner_cell.py`, `tests/agent_bench/test_run_store.py`,
  `tests/agent_bench/fixtures/wire/run/`
- **Done when:** injected fakes create the documented manifest/cell/row tree for exactly two cells
  byte-stably through the production CLI path; reservations are durable before fake invocation and
  provider-attempt counters derive from their ledger; the manifest and planned call bind the exact
  run-local qualification bundle, suite tree, preregistration, and nullable validity descriptors;
  qualification/profile/executable/tool/sandbox/qualification-environment drift fails before a
  receipt or run; each cell gets a clean workspace and session;
  requested/resolved identity,
  change, score, timing, containment, and artifact hashes are complete; finalization is atomic;
  traversal, torn writes, hash mismatch, evaluator invalidity, and outside-run writes fail closed;
  a fake-agent artifact-ceiling breach becomes `resource-limit` score zero while injected harness
  storage failure becomes `artifact-store` invalidity; an observation manifest requires and hashes
  the human-validity ledger while smoke/pilot manifests record it as null; the analysis-plan
  descriptor is snapshotted and drift-tested in both planned call and manifest; preflight or argument
  failure creates no run.
- **Depends on:** 27, 31

<!-- autofix-applied: 2026-08-21 -->
### Step 33: Paired schedule, retries, tranches, and resume
- **Problem:** Extend the runner to an immutable N-model paired schedule with recorded SHA-256
  ordering, cyclic within-family model rotation, bounded tranches, the closed retry taxonomy,
  provider-attempt accounting, pause/resume, and corruption quarantine. Retries retain their
  original position and finish before the next schedule block.
- **Type:** code
- **Issue:** #35
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/runner.py`, `measure_twice/agent_bench/cli.py`,
  `tests/agent_bench/test_schedule.py`, `tests/agent_bench/test_resume.py`
- **Done when:** two- and three-model schedule goldens are reproducible and position-balanced;
  blocks are never split; valid terminal cells are skipped exactly once; paused runs resume only
  from the immutable snapshot; incomplete cell state with no complete JSONL row is quarantined and
  rerun, while a complete row with conflicting bytes/tokens or bad artifact hashes invalidates the
  run; only eligible
  pre-terminal infrastructure faults receive one fresh-workspace retry; an exhausted retry records
  both attempts and their independently hashed invocation/stream artifacts in a golden fixture,
  marks `invalid-infrastructure`, stops before another block, and cannot be reopened,
  imputed, or spliced; two concurrent resumes cannot both acquire the run-owner lock; stale-lock
  recovery obeys the host/PID/600-second rule; crash goldens cover after reservation/before call,
  after call/before terminal row, and after row append/before status replace without exceeding or
  double-counting the attempt budget; the last case is
  repaired from authoritative rows without rerunning or double-counting, while reports expose drift
  and suppress a verdict until repair; per-attempt and both per-repetition artifact paths/hashes are
  frozen in the store golden; resume rejects every replacement identity/seed/limit input.
- **Depends on:** 32

<!-- autofix-applied: 2026-08-21 -->
### Step 34: Deterministic statistics and report CLI
- **Problem:** Add macro task scores, all-pairs outcome tables, the exact 10,000-draw paired cluster
  bootstrap, min-n suppression, model/failure/tag breakdowns, symmetric five-point superiority,
  directed five-point non-inferiority, strict comparability checks, and deterministic Markdown/JSON
  output through `mt agent report`.
- **Type:** code
- **Issue:** #36
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `measure_twice/agent_bench/report.py`, `measure_twice/agent_bench/cli.py`,
  `tests/agent_bench/test_report.py`, `tests/agent_bench/fixtures/reports/`
- **Done when:** golden fixtures pin task weighting, counter-derived draw indexes, cluster
  multiplicity, deltas, `sorted[249]`/`sorted[9749]` primary endpoints, and four-scope
  `sorted[61]`/`sorted[9937]` endpoints; they reproduce candidate/reference advantage,
  inconclusive, candidate eligible, reference required, unresolved, and min-n-suppressed outcomes;
  a mixed four-scope fixture preserves four ordered confirmatory decisions with a null aggregate
  verdict; a third model yields all three pairwise views; pilot, incomplete, invalid, or
  mixed-instrument/harness/environment evidence has a null verdict and a failing inference exit;
  evidence export is deterministic, refuses overwrite, carries all source hashes, and redacts raw
  content, secrets, environment values, and absolute paths.
- **Depends on:** 33

<!-- autofix-applied: 2026-08-21 -->
### Step 35: Run the real two-provider pipeline smoke
- **Problem:** From the qualified WSL2 environment, run the one-task smoke suite once through
  `codex-luna` and `claude-sonnet`, exercising the real
  suite→profile→preflight→agent→patch→sandboxed evaluator→row→report path before authoring either
  observation suite.
- **Type:** wait
- **Issue:** #37
- **Evidence:** one bounded two-cell run and its report under the shared Git state home; no tracked diff
- **Done when:** the suite/manifest say `run_class: "smoke"`; the operator accepts the dry-run digest
  before provider calls; its current profiles, executable/version/invocation, effective tools,
  sandbox, qualification-environment, and execution-profile hashes exactly match the committed qualification bundle; each cell
  remains under the 60-second smoke limits and uses the intended identity; fresh-session,
  outside-write, instruction/credential, oracle, and child-network canaries pass; both patches
  round-trip into clean evaluators and repeat consistently; every artifact hash verifies; the
  report renders with a null quality verdict and no instrument fault. Failure returns to the owning
  Step 25-34, requires a new hash where applicable, and is never waived into an observation.
- **Depends on:** 34

<!-- autofix-applied: 2026-08-21 -->
### Step 36: Author pilot localized bug-repair tasks
- **Problem:** Author three fresh dependency-free Python 3.12 localized bug-repair bundles—one easy,
  one medium, and one hard—with neutral prompts, immutable seeds, hidden oracles, independent
  reference patches, constrained paths, unique cluster IDs, and provenance.
- **Type:** code
- **Issue:** #38
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-pilot-v0/tasks/bug-repair-{01,02,03}/`,
  `tests/agent_bench/test_pilot_bug_repair.py`
- **Done when:** each bundle passes strict containment and twice-run untouched/no-op/reference
  anchors; the untouched and no-op/garbage variants fail for the intended behavior while the
  independently reviewed reference passes; no task needs network/package installation or exposes
  oracle/reference material; task content is not tuned to either provider.
- **Depends on:** 35

<!-- autofix-applied: 2026-08-21 -->
### Step 37: Author pilot bounded-feature tasks
- **Problem:** Author three fresh dependency-free Python 3.12 bounded-feature bundles with the same
  easy/medium/hard, neutrality, provenance, isolation, and independent-cluster contract.
- **Type:** code
- **Issue:** #39
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-pilot-v0/tasks/bounded-feature-{01,02,03}/`,
  `tests/agent_bench/test_pilot_bounded_feature.py`
- **Done when:** all three tasks pass strict containment and repeated negative/reference anchors;
  requirements admit a clear observable solution without dictating implementation; allowed paths
  are sufficient but narrow; no external dependency or provider-specific cue is present.
- **Depends on:** 36

<!-- autofix-applied: 2026-08-21 -->
### Step 38: Author pilot behavioral-refactor tasks
- **Problem:** Author three fresh dependency-free Python 3.12 behavioral-refactor bundles with one
  task at each difficulty and independently testable preservation/change requirements.
- **Type:** code
- **Issue:** #40
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-pilot-v0/tasks/behavioral-refactor-{01,02,03}/`,
  `tests/agent_bench/test_pilot_behavioral_refactor.py`
- **Done when:** all three tasks pass strict containment and repeated negative/reference anchors;
  hidden tests distinguish behavior-preserving structure work from no-op or overbroad rewrites;
  protected files and allowed paths are explicit; no oracle leakage, external dependency, or
  provider-specific tuning is present.
- **Depends on:** 37

<!-- autofix-applied: 2026-08-21 -->
### Step 39: Author pilot CLI/data-boundary tasks
- **Problem:** Author three fresh dependency-free Python 3.12 CLI/data-boundary bundles covering
  parsing, persistence, and failure-boundary behavior across easy, medium, and hard difficulty.
- **Type:** code
- **Issue:** #41
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-pilot-v0/tasks/cli-data-boundary-{01,02,03}/`,
  `tests/agent_bench/test_pilot_cli_data_boundary.py`
- **Done when:** all three tasks pass strict containment and repeated negative/reference anchors;
  oracles assert exit/status/data contracts without unstable timing or platform assumptions;
  malformed-input cases are represented; no network, installation, leakage, or provider-specific
  cue exists.
- **Depends on:** 38

<!-- autofix-applied: 2026-08-21 -->
### Step 40: Freeze and preregister `coding-agent-pilot-v0`
- **Problem:** Assemble the 12 reviewed tasks into a canonical suite, record its authoring evidence
  and immutable hashes, and preregister that the run calibrates harness correctness and task
  difficulty only. The report must be mechanically incapable of declaring a winner.
- **Type:** code
- **Issue:** #42
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-pilot-v0/suite.json`,
  `analysis-plans/coding-agent-pilot-v0.json`,
  `docs/agent-benchmark/pilot-authoring.md`,
  `docs/agent-benchmark/pilot-preregistration.md`,
  `tests/agent_bench/test_pilot_suite.py`
- **Done when:** the suite contains exactly three tasks per family and easy/medium/hard coverage,
  all assets are contained and hashed, the complete untouched/no-op/reference matrix passes twice,
  independent-review provenance is present, `run_class` is `pilot` in the suite/manifest and
  its analysis plan is strict `policy: "none"`; `pilot_not_ranking` is true in report/evidence
  fixtures, and the committed preregistration names and hashes that plan, the active qualification
  bundle, and the frozen profiles, seeds, limits, retry
  handling, diagnostics, and prohibited conclusions before any pilot call.
- **Depends on:** 39

<!-- autofix-applied: 2026-08-21 -->
### Step 41: Observe the pilot calibration
- **Problem:** Run 12 tasks × 2 models × 1 sample (24 cells) sequentially in the stored paired order,
  resuming only after a clean tranche pause or operator interruption. This is harness/task
  calibration, never ranking evidence.
- **Type:** wait
- **Issue:** #43
- **Evidence:** one complete raw pilot run plus generated reports and
  `<git-common-dir>/agent-bench-state/exports/pilot-v0.json`; no tracked diff
- **Done when:** the operator has approved the fresh-run digest after exact qualification-bundle
  matching; all 24 scheduled cells are terminal and artifacts verify; requested/resolved identities stay stable; no containment, oracle,
  evaluator, or store fault remains; the report is hard-labeled `PILOT_NOT_A_RANKING`; the fixed
  external snapshot is secret-free and matches every raw source hash. Findings
  separate instrument defects from model outcomes. Pre-call qualification drift returns through
  Steps 30-31, Step 35, and the Step-40 preregistration hash. An instrument fault returns to its
  owning Step 25-40, mints new hashes, requalifies when provider/capture/isolation identity changed,
  always reruns Step 35, and then starts a new complete 24-cell pilot—never splicing pre-fix rows.
- **Depends on:** 40

<!-- autofix-applied: 2026-08-21 -->
### Step 42: Record pilot findings and freeze the v1 authoring contract
- **Problem:** Convert only a defect-free pilot into a methodology note, identify every allowed
  pre-v1 correction, and freeze a held-out authoring rubric for 32 new tasks: eight independent
  clusters in each family, balanced difficulty, independent reference review, neutrality checks,
  and no reuse of pilot evidence.
- **Type:** code
- **Issue:** #44
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `docs/agent-benchmark/pilot-findings.md`,
  `docs/agent-benchmark/v1-authoring-contract.md`,
  `docs/agent-benchmark/evidence/pilot-v0.json`,
  `tests/agent_bench/test_v1_authoring_contract.py`
- **Done when:** every pilot statement cites the run ID and frozen hashes; instrument defects and
  model outcomes are separated; `mt agent evidence import --kind run` imports the fixed
  external snapshot, reverifies it against the shared raw run, and commits
  `docs/agent-benchmark/evidence/pilot-v0.json`; all permitted harness/task-contract changes are named and already
  resolved or the step fails; the machine-checked v1 rubric assigns 32 unique IDs/clusters,
  exact per-family 3/3/2 difficulty targets, provenance requirements, and disjoint pilot/v1 content before any v1
  task is authored.
- **Depends on:** 41

<!-- autofix-applied: 2026-08-21 -->
### Step 43: Author v1 bug-repair tasks 1-4
- **Problem:** Author the first four fresh held-out localized bug-repair tasks against the frozen v1
  rubric, one task per independent cluster.
- **Type:** code
- **Issue:** #45
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/tasks/bug-repair-{01,02,03,04}/`,
  `tests/agent_bench/test_v1_bug_repair_a.py`
- **Done when:** all four strict bundles pass twice-run untouched/no-op/reference anchors, meet their
  assigned difficulty/provenance cells, require no network/install, remain disjoint from pilot
  content, and pass independent reference and leakage review.
- **Depends on:** 42

<!-- autofix-applied: 2026-08-21 -->
### Step 44: Author v1 bug-repair tasks 5-8
- **Problem:** Complete the held-out localized bug-repair family with four more independent tasks.
- **Type:** code
- **Issue:** #46
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/tasks/bug-repair-{05,06,07,08}/`,
  `tests/agent_bench/test_v1_bug_repair_b.py`
- **Done when:** the new tasks meet the Step-43 anchor, independence, neutrality, provenance, and
  leakage gates; together the family has eight unique clusters and the rubric's difficulty balance.
- **Depends on:** 43

<!-- autofix-applied: 2026-08-21 -->
### Step 45: Author v1 bounded-feature tasks 1-4
- **Problem:** Author the first four fresh held-out bounded-feature tasks, one per independent
  cluster, with observable requirements that do not dictate an implementation.
- **Type:** code
- **Issue:** #47
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/tasks/bounded-feature-{01,02,03,04}/`,
  `tests/agent_bench/test_v1_bounded_feature_a.py`
- **Done when:** all four pass the frozen anchor, difficulty, provenance, disjointness, independent
  reference, and leakage gates; allowed paths are narrow but sufficient and no external dependency
  is required.
- **Depends on:** 44

<!-- autofix-applied: 2026-08-21 -->
### Step 46: Author v1 bounded-feature tasks 5-8
- **Problem:** Complete the held-out bounded-feature family with four more independent tasks.
- **Type:** code
- **Issue:** #48
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/tasks/bounded-feature-{05,06,07,08}/`,
  `tests/agent_bench/test_v1_bounded_feature_b.py`
- **Done when:** the new tasks pass every Step-45 gate; together the family has eight unique
  clusters, balanced assigned difficulty, and no prompt/oracle pattern copied from pilot tasks.
- **Depends on:** 45

<!-- autofix-applied: 2026-08-21 -->
### Step 47: Author v1 behavioral-refactor tasks 1-4
- **Problem:** Author the first four fresh held-out behavioral-refactor tasks with independently
  testable preservation and required-change boundaries.
- **Type:** code
- **Issue:** #49
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/tasks/behavioral-refactor-{01,02,03,04}/`,
  `tests/agent_bench/test_v1_behavioral_refactor_a.py`
- **Done when:** all four pass the frozen anchor, difficulty, provenance, disjointness, independent
  reference, and leakage gates; hidden tests reject no-op and overbroad rewrites while preserving
  allowed implementation freedom.
- **Depends on:** 46

<!-- autofix-applied: 2026-08-21 -->
### Step 48: Author v1 behavioral-refactor tasks 5-8
- **Problem:** Complete the held-out behavioral-refactor family with four more independent tasks.
- **Type:** code
- **Issue:** #50
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/tasks/behavioral-refactor-{05,06,07,08}/`,
  `tests/agent_bench/test_v1_behavioral_refactor_b.py`
- **Done when:** the new tasks pass every Step-47 gate; together the family has eight unique
  clusters, balanced assigned difficulty, and no pilot-derived or provider-specific cues.
- **Depends on:** 47

<!-- autofix-applied: 2026-08-21 -->
### Step 49: Author v1 CLI/data-boundary tasks 1-4
- **Problem:** Author the first four fresh held-out CLI/data-boundary tasks covering deterministic
  parsing, persistence, exit, and malformed-input contracts.
- **Type:** code
- **Issue:** #51
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/tasks/cli-data-boundary-{01,02,03,04}/`,
  `tests/agent_bench/test_v1_cli_data_boundary_a.py`
- **Done when:** all four pass the frozen anchor, difficulty, provenance, disjointness, independent
  reference, and leakage gates; tests avoid unstable timing/platform assumptions and external
  dependencies.
- **Depends on:** 48

<!-- autofix-applied: 2026-08-21 -->
### Step 50: Author v1 CLI/data-boundary tasks 5-8
- **Problem:** Complete the held-out CLI/data-boundary family with four more independent tasks.
- **Type:** code
- **Issue:** #52
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/tasks/cli-data-boundary-{05,06,07,08}/`,
  `tests/agent_bench/test_v1_cli_data_boundary_b.py`
- **Done when:** the new tasks pass every Step-49 gate; together the family has eight unique
  clusters, balanced assigned difficulty, and complete malformed-input/exit/data coverage without
  pilot-derived or provider-specific cues.
- **Depends on:** 49

<!-- autofix-applied: 2026-08-21 -->
### Step 51: Freeze and preregister `coding-agent-v1`
- **Problem:** Assemble the 32 reviewed tasks, freeze the qualified Luna/Sonnet model snapshot and
  all suite/harness/environment identities, and commit the exact three-sample schedule, analysis
  seed, five-point superiority rule, retry policy, exclusions, and reporting scope before any v1
  model call.
- **Type:** code
- **Issue:** #53
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `suites/agents/coding-agent-v1/suite.json`,
  `profiles/agent-models-v1.json`,
  `analysis-plans/coding-agent-v1.json`,
  `docs/agent-benchmark/v1-preregistration.md`,
  `docs/agent-benchmark/v1-validity-review-guide.md`,
  `tests/agent_bench/test_v1_suite.py`
- **Done when:** exactly 32 tasks and eight independent clusters per family pass strict validation
  and the full twice-run negative/reference matrix; every family has exactly three easy, three
  medium, and two hard tasks; no gold comes solely
  from an evaluated model; the suite declares `run_class: "observation"`; the frozen profiles match
  the exact two-profile committed qualification bundle produced from Step 30; the strict analysis
  plan names Luna candidate, Sonnet reference, one overall superiority scope, margin 5, confidence
  0.95, no multiplicity, and 10,000 draws, and the Markdown preregistration cites both its hash and
  the active qualification-bundle hash; the scheduler's offline golden
  expands the frozen inputs to exactly 192 paired cells; committed hashes and the
  lower-bound-above-+5 / upper-bound-below--5 decision rule predate observation. The live dry-run
  digest is intentionally deferred to Step 54 because the required human-validity ledger does not
  exist until that gate closes; the preregistration names overall score as the sole confirmatory
  scope and labels every family/tag result exploratory.
  `mt agent evidence validity-template` writes the exact 32-task external worksheet bound to the
  preregistration and analysis-plan hashes, and the guide
  explains blind independent review, reconciliation, revalidation, final-ledger conversion, and
  resume commands without requiring conversation history.
- **Depends on:** 50

<!-- autofix-applied: 2026-08-21 -->
### Step 52: Two-human validity and freeze check
- **Problem:** Generate the fixed external validity worksheet, then before the first v1 benchmark
  call have two humans independently review every prompt,
  seed, hidden oracle, allowed/protected path, reference patch, difficulty assignment, cluster
  independence, and provider neutrality, then reconcile disagreements without making model calls.
- **Type:** wait
- **Issue:** #54
- **Evidence:** raw non-secret matrix at
  `<git-common-dir>/agent-bench-state/exports/v1-validity-review.json`, identifying both reviewers,
  item-level verdicts, reconciliations, final hashes, authoring assistance, and neutrality attestations;
  no tracked diff
- **Done when:** Step 52 itself changes only review and reconciliation fields in the external
  worksheet. Both humans issue a final independent `PASS` for every unchanged artifact; at least one
  reviewer per task is independent of its author; every model-assisted draft is disclosed and
  treated as untrusted input rather than gold; no reviewer used v1 Luna/Sonnet outputs because no v1
  call has occurred; and the fixed raw matrix matches the final instrument/preregistration hashes.
  Any requested prompt, seed, oracle, reference, path, suite, profile, or preregistration change
  keeps this wait step incomplete: return to the owning Step 43-51 code issue, archive the stale
  worksheet under the Git-common state home, complete the reviewed code change and full Step-51
  gates, regenerate the worksheet against new hashes, and restart both independent reviews.
- **Depends on:** 51

<!-- autofix-applied: 2026-08-21 -->
### Step 53: Validate and commit the human-validity ledger
- **Problem:** Import the completed raw two-human review matrix through the strict evidence boundary
  so the frozen observation run can bind a durable, worktree-visible validity ledger.
- **Type:** code
- **Issue:** #55
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `docs/agent-benchmark/evidence/v1-validity-review.json`,
  `tests/agent_bench/test_v1_validity_evidence.py`
- **Done when:** `mt agent evidence import --kind validity` verifies both independent decisions,
  reviewer/author separation, all 32 task IDs and final instrument/preregistration/analysis-plan
  hashes, every prompt/seed/oracle/reference-patch/path/difficulty/provenance/cluster/neutrality
  boolean, reconciliation records,
  neutrality/provenance attestations, and absence of secret or absolute-path fields; the canonical
  tracked ledger is committed; and a production-path injected-fake test accepts exactly this ledger
  hash and prints the frozen 192-cell schedule without creating a live confirmation receipt or
  making a provider call.
- **Depends on:** 52

<!-- autofix-applied: 2026-08-21 -->
### Step 54: Observe preregistered Luna versus Sonnet v1
- **Problem:** Review a fresh, non-expired dry-run receipt, then run the frozen 32-task suite through Luna and Sonnet with three fresh samples per task
  (192 cells), sequential paired scheduling, at most 32 cells per tranche, and immutable resume.
  After the first provider call, do not edit suite, profile, harness, evaluator, schedule, seed, or
  decision policy.
- **Type:** wait
- **Issue:** #56
- **Evidence:** one complete raw v1 run, deterministic reports, and
  `<git-common-dir>/agent-bench-state/exports/v1.json`; no tracked diff
- **Done when:** before the fresh receipt, current profile, executable/version/invocation,
  effective-tool, sandbox, qualification-environment, and execution-profile hashes exactly match the committed qualification
  bundle; any mismatch returns through Steps 30-31 and a new Step-35 smoke. The operator accepted the
  exact fresh-run/resume digests; all 192 cells are terminal; identity and environment evidence stays stable; no containment/oracle/evaluator/store fault
  remains; artifacts and repetitions verify; the fixed external snapshot is secret-free and matches
  every raw source hash; the manifest contains the exact committed
  `v1-validity-review.json` descriptor/hash; each family satisfies eight-cluster min-n; and the
  report identifies candidate `codex-luna` and reference `claude-sonnet` and emits exactly one
  preregistered verdict: `CANDIDATE_ADVANTAGE`, `REFERENCE_ADVANTAGE`, or `INCONCLUSIVE`. An infrastructure-invalid run is retained for diagnostics but abandoned; after
  correction, start a new run ID over all 192 cells rather than dropping, imputing, or splicing. A
  discovered instrument/harness/evaluator defect likewise keeps this wait step incomplete and
  returns to its owning Step 25-53 code issue; complete reviewed fixes and new hashes, requalify in
  Steps 30-31 when provider/capture/isolation identity changed, always rerun Step 35, then repeat
  Step 51 freeze and Steps 52-53 validity binding before a fresh full observation. Step 54 itself
  never edits code or frozen inputs.
- **Depends on:** 53

<!-- autofix-applied: 2026-08-21 -->
### Step 55: Publish findings and the Haiku admission protocol
- **Problem:** Document the v1 result, limitations, family disagreements, failure modes, and what the
  evidence can and cannot route; refresh project commands/status; register this completed plan in
  the canonical roadmap; and specify the later Haiku comparison without changing pending Steps
  13-24.
- **Type:** code
- **Issue:** #57
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `docs/agent-benchmark/evidence/v1.json`,
  `docs/agent-benchmark/v1-findings.md`, `docs/agent-benchmark/haiku-admission-protocol.md`,
  `tests/agent_bench/test_haiku_admission_contract.py`, `README.md`, `CLAUDE.md`, `plan.md`
- **Done when:** `mt agent evidence import --kind run` imports the fixed external v1 snapshot,
  reverifies it against the shared raw run, and commits `docs/agent-benchmark/evidence/v1.json`;
  every conclusion links to the run ID
  and frozen hashes; the overall claim obeys the preregistered bound while family results remain
  explicitly exploratory, and diagnostics are not mislabeled as quality; routing hypotheses
  distinguish task families without claiming one universal model. The deferred protocol requires a
  new `agent-execution-haiku-v1` profile and matching three-entry Luna/Sonnet/Haiku registry; all
  three arms are freshly qualified into one sorted bundle because the execution-profile hash
  changes. Its strict `RunPolicy` pins smoke to `1×3×1=3` cells/6 attempts in one block and
  observation to `32×3×3=288` cells/576 attempts with at most 30 cells (10 complete blocks) per
  tranche. A matching smoke-suite version plus `policy: "none"` analysis plan exercise the existing
  N-model/all-pairs path. A new `coding-agent-haiku-v1` observation suite descriptor binds
  `agent-execution-haiku-v1` while reusing byte-identical copies of all 32 frozen v1 task assets;
  its necessarily new instrument hash gets a new validity ledger and preregistration and never
  masquerades as the original suite. The observation analysis plan sets Haiku candidate, Sonnet reference,
  directed five-point non-inferiority, the four byte-ordered family scopes, confidence 0.95,
  Bonferroni multiplicity, and 10,000 draws. After its three-cell smoke, run all three models afresh
  in one environment rather than splicing earlier Luna/Sonnet rows; retain the four ordered scope
  decisions and no aggregate quality verdict. The offline contract test constructs both future
  suite descriptors with fixture model identities and proves 3/288/576 policy validation,
  three-source qualification import, three-model scheduling, and mixed all-pairs reporting without
  live calls or a provider-specific runner branch.
- **Depends on:** 54

Qualification is intentionally early: no run store is built until Step 30 proves both live provider
sandboxes, and no observation suite is authored until Step 35 proves the complete real pipeline.
All live cells remain sequential. Steps 30, 35, 41, 52, and 54 are `wait` gates that halt
`build-phase` and require explicit resume; they cannot overlap
instrument changes.

## 8. Risks and Open Questions

There are no unresolved architecture choices in this plan. The following external/runtime facts
are gates to prove rather than assumptions to waive.

| Item | Risk | Mitigation |
|---|---|---|
| WSL2 CLI availability/auth | Windows-authenticated CLIs may not already be installed or authenticated inside WSL2. | Steps 28-29 provide fail-loud preflight code and the runbook; Step 30 is the operator qualification gate. Do not fall back to asymmetric native Windows execution. |
| Bubblewrap FD-bind availability | Ubuntu 24.04 may provide a path-bind-only Bubblewrap that cannot preserve an opened filesystem identity through namespace setup. | Step 26 live-probes FD-bound writable/read-only mounts, records executable/version evidence, and fails before executing untrusted code. Upstream 0.11.2 is pinned known-good; the operator installs a compatible trusted build, and the harness never downloads one or falls back to pathname binds. |
| Linux resource-guard availability | Polling `/proc` or a writable tree cannot prevent a hostile burst from exhausting host memory, tasks, or storage. | Step 26 requires read-back cgroup memory/zero-swap/task/CPU-bandwidth controls before target release and a fresh byte/inode-bounded tmpfs for each evaluator repetition. Missing setup, attribution, or teardown fails closed; sampled CPU/logical-tree thresholds are scoring evidence only. |
| Provider flag or stream drift | CLI updates can remove isolation flags or change JSONL events. | Pin exact argv/event contracts in offline tests, record executable/version hashes, and fail preflight on missing required capability. |
| Model alias drift | `sonnet` or a future `haiku` alias can silently move. | Record requested and stream-reported identity per cell, abort within-run drift, and pin the qualified full identity before v1 where the provider exposes it. Never invent a resolved Codex field that JSONL does not supply; retain request plus catalog/executable evidence. |
| Unequal native internals | Tool loops and system prompts differ across products. | Define the claim as end-to-end agent-product performance; equalize inputs, environment, permissions, and outcome scorer, and record the remaining provider differences. |
| Sandbox asymmetry or escape | One agent may reach outside the workspace or network. | Common WSL2/Linux backend, fail-closed provider settings, fixed outside-write/network/instruction-leak/first-class-web canaries, protected oracle separation, Step 30 qualification, and Step 35 full-pipeline halt-on-failure. |
| Hidden-oracle leakage | An agent could see tests/reference material and overfit. | Copy only `seed/` and prompt into a random WSL-local workspace; keep oracle/reference outside; never pass suite root; score in a second clone; validate trace/canaries. |
| Patch incompleteness or hostile Git metadata | Event telemetry can omit changes, while an agent-controlled repository can configure hooks, filters, or diff drivers. | Treat submitted filesystem bytes—not agent `.git`—as authoritative; reconstruct a trusted seed repository inside the no-network capture sandbox, use fixed empty Git config/attributes and no hooks/external diff/textconv, force-add all regular files, emit a full binary diff, store a file manifest, apply to a fresh clone, and compare tree hashes. |
| Flaky evaluator | Nondeterministic tests can masquerade as model quality. | Baseline/reference anchors run twice and any disagreement blocks the instrument; submitted-patch result/tree disagreement is a deterministic `nondeterministic` score zero, so model behavior cannot censor a run. |
| Correlated tasks | Transformed siblings can overstate effective sample size. | Required `cluster_id`, at least eight independent clusters per reported family, and cluster—not row—bootstrap. |
| Pilot overinterpretation | A 12-task calibration may look like a ranking. | Persist `run_class: "pilot"` in suite/manifest, derive `pilot_not_ranking` in report/evidence, and prohibit a superiority verdict until frozen v1. |
| Model-assisted task authoring | A build agent could imprint preferences into prompts or gold. | Record authoring identity/assistance, treat generated artifacts as drafts, require deterministic mutants/reference anchors plus two final independent human passes, disclose evaluated-arm assistance, and never revise v1 from v1 outputs. |
| Multiple family looks | Four ordinary family intervals could create false routing discoveries. | Luna/Sonnet has one confirmatory overall scope; family/tag views are exploratory. A later multi-scope Haiku routing study uses the preregistered Bonferroni interval in §6.7. |
| Subscription/rate interruption | A long 192-cell run can span quotas or CLI outages. | Sequential bounded tranches, atomic artifacts, hash-verified resume, and infrastructure-error retry without converting outage into a model zero. |
| Benchmark tuning after exposure | Editing v1 in response to model behavior invalidates inference. | Pilot tasks are calibration-only; v1 is fresh and committed with preregistration before calls; any post-start edit creates a new instrument version. |
| Future Haiku effort asymmetry | Haiku may not expose the same reasoning-effort control as Sonnet. | `effort` is nullable/provider-declared in the registry; record asymmetry and preregister a quality non-inferiority claim, never an equal-effort claim. |
| Operations-plan overlap | Future generic fingerprint/leaderboard work could duplicate agent reporting. | Keep this store/report agent-specific and leave Steps 19-20 prompt-only; any cross-pipeline catalog or identity integration is a separately reviewed post-Step-55 phase. |

## 9. Testing Strategy

### Offline gates (no inference calls)

- Schema/model registry: strict unknown/missing/type validation; safe names; path containment;
  symlink/junction rejection; recursive hash drift; selected-profile hashing; two profiles sharing one
  provider; nullable effort; frozen golden hashes.
- Evaluator: seed materialization; create/edit/delete/binary/mode/commit patch capture; clean apply
  and tree-hash round-trip; allowed/protected globs; oracle separation; baseline/no-op/reference
  anchors; timeout; nondeterministic repeat; evaluator versus model failure taxonomy.
- Process/adapters: exact argv, prompt bytes on UTF-8 stdin, descriptor-bound cwd, scrubbed
  environment without secret logging, timeout/process-tree kill, FD-bound Bubblewrap mount sources,
  pre-release cgroup guard readback, bounded-tmpfs creation/snapshot/teardown, sampled resource
  provenance, shared FD-relative tree traversal, one-shot ownership and leak checks, deterministic
  post-acquisition rename-to-symlink races, every Codex JSONL and Claude stream terminal/error shape,
  model mismatch, missing capability, empty output, exact effective-tool drift, first-class web denial,
  MCP/plugin/browser absence, one-shot confirmation receipts, and executable fingerprints.
- Runner/store: seeded pair order, unique workspaces, budget abort, atomic cell finalization,
  torn/corrupt rows, missing/hash-mismatched artifacts, quarantine and resume, exclusive run locks,
  stale-lock recovery, traversal defenses, no writes on preflight failure, and production-path CLI
  invocation through injected fakes.
- Report: macro-per-task weighting across samples; all-pairs counts for two and three models;
  cluster bootstrap with fixed seed; primary 95% and simultaneous Bonferroni endpoints; superiority
  and non-inferiority boundaries; exploratory family labeling; min-n suppression;
  model/instrument/harness mismatch; stable Markdown/JSON fixtures.
- Evidence: exact qualification/run/validity schemas; source/raw hash verification; deterministic
  import; overwrite refusal; and rejection of prompts, traces, secrets, environment values, account
  identifiers, and absolute paths.
- Regression: all existing prompt-suite, runner, scoring, judge, report, author, ledger, and anchor
  tests remain byte/behavior compatible because their schemas and stores are not migrated.

Each code step must finish green on:

```powershell
uv build
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict measure_twice
```

Applicable Step-26+ code steps also pass the WSL marker command defined in §6.11.

### Real pipeline gates

1. Step 30 is the early live qualification: one controlled 60-second invocation per provider must
   prove model identity, instruction isolation, filesystem containment, shell-network denial, and
   first-class web-tool denial before
   runner/report/task construction.
2. Step 35 is the required under-60-second-per-cell real data-pipeline smoke: one task, two real
   providers, two cells, full artifact/evaluator/report path, and repeated containment canaries.
3. Step 41 is the 24-cell calibration observation. It validates the harness and task set but is
   mechanically prevented from emitting a winner.
4. Step 54 is the only Luna/Sonnet quality observation: frozen 32-task instrument, three samples,
   192 cells, and one preregistered deterministic verdict.
5. The later Haiku phase must first run a three-cell smoke—one identical task through Luna, Sonnet,
   and Haiku—verifying Haiku's pinned resolution and the live three-model/all-pairs report path.
   It then runs a fresh 288-cell three-arm observation under one environment. A new
   execution-profile-bound suite descriptor reuses byte-identical v1 task assets and the same code
   paths but gets a new instrument hash, validity ledger, preregistration, and run identity.

The feature is one-shot and operator-invoked; no background/always-on soak is required. The long
wait steps exist because model sweeps consume real wall-clock time, not because the system runs
autonomously after the command returns.

---

## Shipped: Steps 25-26 - strict agent inputs and the Linux isolation substrate

**Both steps merged to `master`. Issues #27-#28 closed.** The instrument can now be *defined* and a
process can be *contained*; nothing yet makes an inference call. Steps 27-29 build the sandboxed
evaluator and the provider adapters on top of this substrate.

### What was built

**Step 25 - strict agent inputs and structural validation** (`3be9f3f`, issue #27)

- Fail-loud loaders for `ModelSpec`/`ModelRegistry`, `AnalysisPlan`, `ExecutionProfile`,
  `AgentSuite`, and `AgentTask`. Unknown keys, missing keys, wrong types, duplicate or unsafe
  identifiers, paths escaping the bundle, symlinks and NTFS junctions, missing assets, arbitrary
  task commands, and unsupported `schema_version` values are all rejected **at load**, never at use.
- Recursive instrument hashing over task-asset bytes, with the selected model profile hashed
  separately. **Model profiles stay outside suite identity**, so admitting a new provider cannot
  silently redefine what the instrument measures. Canonical instrument, selected-profile, and
  execution-profile hash goldens are frozen, as are run-class, evaluator-layout, allowed/protected
  glob, and `policy: "none"` analysis-plan goldens.
- `mt agent validate <suite-dir> [--structure-only]` - the structure-only path is cross-platform and
  never executes suite code.
- The candidate registry (`codex-luna`, `claude-sonnet`), execution profile `agent-execution-v1`,
  analysis plan `agent-smoke-v1`, and the one-task `suites/agents/smoke` bundle declaring
  `run_class: "smoke"` with a no-ranking preregistration that cites the analysis-plan hash.
- A three-profile offline fixture proving two Claude profiles dispatch by provider without
  consulting `CLAUDE_ALIASES`.

**Step 26 - Linux process and isolation substrate** (`cf8ac78` and follow-ups, issue #28)

- **One kernel-object-identity invariant.** Every caller-supplied filesystem source is opened once
  through `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)` and thereafter
  referred to only by that owned descriptor (`LinuxPathCapability`). A later rename, unlink, or
  symlink replacement therefore cannot redirect a sandbox mount, a subprocess cwd, a capture
  enumeration, a live resource scan, or terminal tree validation. Type and identity come from
  `fstat`/`fstatfs` of the held FD, never from a reconstructed pathname; `readdir` output is treated
  as an untrusted component name whose no-follow `openat2` is its acquisition boundary.
- **Contained execution.** `run_process` passes every inherited descriptor via
  `Popen(pass_fds=..., close_fds=True)`, enters cwd through `/proc/self/fd/<cwd-fd>`, and executes
  pinned executables through `/proc/self/fd/<exe-fd>`. Targets launch under
  `systemd-run --user --scope --collect --slice=app.slice` into a freshly named transient scope; the
  controller environment needed to reach `/run/user/<uid>/bus` never enters the target's separately
  allowlisted environment. Bubblewrap mounts sources by `--bind-fd`/`--ro-bind-fd`.
- **A two-layer, self-labelling resource contract**, replacing polling-as-containment. Memory, pids,
  and tmpfs byte/inode ceilings are *hard host guards*, active and read back before the target is
  released, and record `hard-guard` provenance. Cumulative CPU and logical-tree thresholds remain
  *sampled scoring rules* that may overshoot between observations, and record `sampled-threshold`
  provenance. `RLIMIT_FSIZE`/`RLIMIT_NOFILE` stay per-process backstops and are never reported as
  aggregate enforcement. Guard-health failure is an execution error, never a resource result.
- **Per-repetition private tmpfs** (`EvaluatorScratch`) with `size` and `nr_inodes` limits active
  before the applied tree is copied. The same retained root FD survives outer-namespace teardown and
  is what Step 27 will snapshot.
- **No fallback.** There is no polling-only and no path-bind degradation path. Missing or
  incompatible cgroup delegation, bounded-tmpfs behavior, or Bubblewrap raises
  `IsolationUnavailableError` and *fails* the WSL gate rather than skipping it. Bubblewrap 0.11.2
  from upstream is the pinned known-good build; Ubuntu 24.04's stock 0.9.0 is unsupported, and
  version text is evidence only - the harness runs live `--bind-fd`/`--ro-bind-fd` behavioral probes.
- **Canaries, not assertions about intent.** Real Bubblewrap probes prove workspace-only writes,
  absent suite/oracle/run/operator-home mounts, read-only oracle and runtime mounts, an empty child
  network, and no host, credential, or parent-`/proc` disclosure. Barrier-controlled, sleep-free
  rename-to-symlink regressions mutate each caller-supplied source after acquisition but before
  consumption and assert that the originally opened inode is still the one consumed.
- `scripts/test-agent-bench-wsl.ps1` stages the tree onto WSL ext4 and runs the `linux_isolation`
  marker for real. Its JUnit gate forbids a *selected* case from skipping, so the containment
  evidence cannot quietly evaporate.

### Files changed

| File | Change |
|---|---|
| `measure_twice/agent_bench/models.py`, `analysis.py`, `suite.py` | Strict loaders, safe identifiers, recursive instrument/profile hashing |
| `measure_twice/agent_bench/_wire.py` | Shared wire codec with duplicate-JSON-key detection |
| `measure_twice/agent_bench/cli.py`, `measure_twice/cli.py` | `mt agent validate` wired into the production CLI |
| `measure_twice/agent_bench/_linux_capabilities.py` | `openat2` FD-capability acquisition, FD-relative traversal, exclusive copy |
| `measure_twice/agent_bench/isolation.py` | Bubblewrap launch, preflight behavioral probes, evaluator tmpfs scratch |
| `measure_twice/agent_bench/process.py` | Contained `run_process`, transient cgroup scope, two-layer resource contract, tree-scan monitoring |
| `measure_twice/agent_bench/_win32_contained.py` | Windows-side contained handles for the cross-platform structural path |
| `profiles/`, `analysis-plans/`, `suites/agents/smoke/`, `docs/agent-benchmark/` | Candidate registry, execution profile, analysis plan, smoke bundle, preregistration |
| `scripts/test-agent-bench-wsl.ps1` | The real Linux containment gate, invoked from Windows |
| `.gitattributes` | `suites/agents/** -text` so task-asset bytes stay exact |
| `.gitignore` | Generated agent data roots ignored; suites, profiles, and tracked evidence stay visible |
| `tests/agent_bench/` | Units, offline fixtures, hostile probes, and the Linux containment canaries |

### Fresh context notes for Step 27

| Issue | Detail |
|---|---|
| A green Windows run is **not** containment evidence | `uv run pytest` on Windows reports the containment cases as explicit skips. Only `scripts/test-agent-bench-wsl.ps1` (or `-m linux_isolation` from a WSL-ext4 checkout) actually exercises them. Both roots must be green before a step is DONE. |
| Task-asset bytes are load-bearing | `suites/agents/** -text` in `.gitattributes` keeps git from normalizing line endings. A CRLF conversion there silently moves the instrument hash. |
| cgroup CPU accounting covers the whole scope | The namespace supervisor and Bubblewrap are billed to the same scope as the target, so three interpreter startups share one CPU budget. A ceiling meant for the target alone will fire on a contended host. |
| One test-flake shape produced five separate flakes here | Every one was a test asserting on a quantity it does not control. Two invariants now hold the line: a canary asserting that a *resource ceiling* stopped a run must never let its wall clock fire first (the ceiling depends on a kernel event the test cannot schedule); and `_ceilings()` defaults are deliberately unhittable by accident, so only a test that *means* to trip a ceiling passes an explicit low value. Preserve both when adding canaries. |
| Step 27 must consume the retained FD | `EvaluatorScratch` keeps the tmpfs root FD alive specifically so the authoritative result-tree snapshot uses the same kernel object. Re-deriving it from a pathname reintroduces the class this step exists to remove. |
| Four non-blocking follow-ups are filed | Issue #58 records them: the pre-release handshake sentinel `MT26R` still hand-maintained in two places (the supervisor source template writes it literally and the parent compares it literally, though the substitution mechanism that dedupes the status wire format already exists), a teardown half of the clock test that is not separately anchored, one reappearance test with a low-probability red-when-healthy race, and a plan edit made outside Step 26's `Files:` list. None blocks Step 27. |
