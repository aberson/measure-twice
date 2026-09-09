# Gemini Model-Sweep Adapter

**Status:** REVIEWED (2026-09-09); build after Steps 56 and 57 pass.

**Repo-sync phase:** `gemini-model-sweep` (explicit; Steps 64-65).

## 1. What This Is

Add one Gemini Flash model to the prompt-response benchmark through the Gemini Developer API.
The operator can smoke-test it, run an explicitly selected suite, and inspect raw answers and
provider identity in the existing reports. The user authorized this work after Steps 56 and 57
on 2026-09-09. The default five-model calibration roster and its preregistration stay intact.

Proposal: `documentation/gemini-model-sweep-proposal.html`

## 2. Existing Context

Python 3.12+, uv, standard library plus the existing sibling `switchboard` dependency.
Step 56 adds `ModelBinding(alias, provider, requested_model)`, a strict
`ModelSweepExecutionProfile`, and an additive `ExecutionReceipt` in
`measure_twice/model_sweep_execution.py`. `runner.run` dispatches those bindings, records
one terminal `RunRow` per item/model/sample, and resumes completed cells without recalling them.
Step 57 exposes receipts and provider identities through Markdown, JSONL, and HTML reports.

`ModelCallResult` carries `response_raw: str`, `resolved_model: str`, `elapsed_s: float`,
and `reason_class: str | None` from the existing switchboard error taxonomy. Missing answers are force-scored zero before
judging. `CliDeps` supplies transport seams; `_handle_run` and `_handle_smoke` call `runner.run`.
Rubric judging currently supports Claude only and remains unchanged by this addition.

`RunRow` stores run/model/item identifiers, sample index, raw/parsed answer, score/scorer,
optional judge scores, elapsed seconds, and error class. Its `(model, item_id, sample_k)`
key owns resume deduplication. Run IDs are `run_YYYYMMDDTHHMMSSZ_` plus six random hexadecimal
characters, minted by `runner._mint_run_id`; model/item aliases are safe names validated by
the existing loaders. Profile and suite identity use lowercase SHA-256 digests.

## 3. Scope

In scope: a single non-streaming text request; Gemini-specific provider binding and request
profile; environment credentials; deterministic scoring; two-item smoke; mixed-provider
dispatch, budgets, resume, stored reporting, offline tests, and operator documentation.

Out of scope: Gemini CLI, coding agents, tools, grounding, files, multimodal input, streaming,
conversation persistence, automatic retries, model discovery, judge-provider expansion,
calibration, leaderboard/dashboard work, and routing claims. This is a one-shot CLI feature;
the autonomous/background-product observation trigger does not apply.

## 4. Impact Analysis

Discovery used the preserved Step 56 worktree after merge from master `223df56`; Step 57
files below are dependent outputs and must be re-read after it lands, before implementation.

| File | Change type | Reason | Verified |
|---|---|---|---|
| `measure_twice/model_sweep_execution.py` | extend | Explicit `gemini-api` binding and hashed request settings | `SUPPORTED_PROVIDERS`/`ModelBinding` validate providers; `ModelSweepExecutionProfile` serializes and hashes profiles; `ExecutionReceipt.create/from_mapping` consume bindings. |
| `measure_twice/config.py` | extend if required | Load the optional Gemini execution settings without accepting credentials | `RunConfig.__post_init__` validates profile/roster; `from_mapping` owns allowed config keys. |
| `measure_twice/runner.py` | extend | Validate credentials before mutation, dispatch Gemini, preserve budget/resume | `run` owns provider branches; `_resolve_bindings`, `_doctor_execution`, `_validate_receipt_profile`, `_pending_cells`, `_build_row`, `_append_row` are the affected production chain. |
| `measure_twice/cli.py` | extend | `CliDeps` seam and `mt smoke --gemini` | `runner.run` is called by `_handle_run` and `_handle_smoke`; both thread local/Claude seams today; smoke parser owns mutually exclusive provider flags. |
| `measure_twice/adapters/base.py` | reuse | Existing result, no-response, and unresolved-identity contracts | Local and Claude adapters plus runner consume `ModelCallResult`; avoid changing its shape. |
| `measure_twice/report.py`, `measure_twice/report_html.py`, `measure_twice/report_template.html` | extend only where needed | Display Gemini identity/settings accurately | Step 57 owns execution evidence; inspect its actual report DTO/builders and both renderers before coding. |
| `tests/test_config.py`, `tests/test_model_sweep_execution.py`, `tests/test_runner.py`, `tests/test_report.py`, `tests/test_report_html.py` | extend | Dispatch, hash/legacy compatibility, resume and report regressions | Existing test roots and Step 56 production-entry fixtures verified by source search. |
| `README.md`, `CLAUDE.md`, `plan.md`, `same-page.toml` | update | Commands, provider scope, and discoverable plan pointer | README provider table currently says non-Claude names use local; same-page currently declares four plans. |

All changed signatures/constants require a fresh consumer search before landing; include its
disposition in developer evidence. Do not alter the coding-agent stack.

## 5. New Components

- `measure_twice/adapters/gemini.py`: typed, injectable standard-library HTTP transport and response adapter.
- `profiles/model-sweep-gemini-v1.json`: explicit alias `gemini-flash`, provider `gemini-api`, requested model `gemini-3.8-flash`, and hashed generation settings; usable with `--config`.
- `tests/test_gemini.py`: protocol/error/security anchors plus the real CLI-to-store-to-report integration tests, sharing existing production builders.

## 6. Design Decisions

**D1 — One stable Flash model.** Pin `gemini-3.8-flash`, listed as stable in Google's model
catalog checked 2026-09-09. The explicit config selects it; no default-roster change and no
automatic substitution when unavailable. Adding a model later requires an explicit binding.

**D2 — Stateless REST.** POST to the code-owned HTTPS endpoint
`https://generativelanguage.googleapis.com/v1beta/models/{requested_model}:generateContent`.
Validate the model ID as a single safe Gemini model name before URL construction. Send one
`contents` user message containing the suite prompt verbatim, no instructions or tools added.
Use `maxOutputTokens: 4096` and `thinkingConfig.thinkingLevel: low`. Omit `candidateCount`:
Google's current migration guide marks it unsupported for Gemini 3 and later. Validate one
returned candidate under this text-only request contract.
Pin these settings and the 120-second transport timeout in the Gemini execution profile so
resume cannot mix request contracts. Native REST exposes `modelVersion` directly and keeps
the existing standard-library dependency policy. Google labels generateContent a legacy API
but explicitly documents this model on it; this narrowly scoped adapter needs no session API.

Add an optional `gemini` context object to the execution profile with fields
`request_contract: "generate-content-text-v1"`, `max_output_tokens: positive int`,
`thinking_level: "low" | "medium" | "high"`, and `timeout_s: finite positive number`.
Require it for Gemini bindings. Its default sample values are the settings above.
Omit the object entirely when absent; derive a composite context hash only when it is present,
otherwise retain the existing Claude context hash. Update receipt creation and resume comparison
to use that shared hash owner. The endpoint and single-candidate/text-only request shape are
fixed by `request_contract`, never a repository-selected URL.

Request body shape: `{"contents":[{"role":"user","parts":[{"text":"suite prompt"}]}],
"generationConfig":{"maxOutputTokens":4096,
"thinkingConfig":{"thinkingLevel":"low"}}}`. Response fields used:
`modelVersion: string`, `candidates: array` with each candidate's `finishReason: string` and
`content.parts: array` of `{text: string, thought?: boolean}`, and
`promptFeedback.blockReason: string` for a blocked prompt. Usage metadata is outside this slice.

**D3 — Runtime credentials only.** Read `GOOGLE_API_KEY`, then `GEMINI_API_KEY`, matching
Google's documented precedence. A present but blank/invalid winning value fails rather than
silently selecting another. Send it only in `x-goog-api-key`; never in URLs, config, receipts,
reprs, logs, or errors. Reject redirects instead of forwarding credentials. No configurable
remote origin. Missing credentials for pending real Gemini calls fail before run creation,
torn-tail repair, row append, or any other provider call. Injected offline transport can use
a test credential; it must not read real credentials or bypass production payload parsing.

**D4 — Preserve evidence.** Read concrete identity from `modelVersion` as soon as a valid
top-level response object is available, and retain it on later errors. Never substitute the
requested alias. Join final text parts in order without trimming the stored text; exclude
parts marked `thought`. Empty, safety-blocked, or thought-only responses must not score as
answers. `MAX_TOKENS` maps to the existing truncation error; a documented safety/recitation
block maps to no-response. Missing candidate structure without a valid blocking reason and
unsupported content types, unexpected finish reasons, and malformed consumed fields map to
bad-envelope. Ignore unrelated additive response metadata. Classify transport/HTTP errors with
the existing taxonomy without persisting exception text or a body that may echo a key.
No implicit retries: each scheduled attempt consumes one call from the existing budget.

**D5 — Additive compatibility.** Existing profiles/receipts without Gemini retain their
serialized bytes and hashes. New Gemini settings participate in execution/profile identity;
changing settings or the requested model must reject resume before mutation. Existing
`RunRow`, suite hashes, deterministic scoring and Claude-only judge capability stay intact.
Reports distinguish recorded execution metadata from qualification; a successful Gemini smoke
does not establish routing eligibility or model quality. Legacy runs remain readable.

**D6 — Offline completion and live verification are separate.** Build and review can finish
without an API key. The live check is exactly two small suite calls, only with user permission
and locally provisioned credentials. Its receipt records actual PASS/FAIL or NOT RUN; do not
infer success from fixtures. No full benchmark is part of this smoke authorization.

## 7. Build Steps

### Step 64: Add a Gemini Flash provider to model sweeps

- **Problem:** Operators can benchmark local and Claude models but cannot select Gemini. Add the explicit API adapter, hashed request profile, credential preflight, budget/resume dispatch, `mt smoke --gemini`, identity-aware reporting, and documentation as one vertical slice.
- **Type:** code
- **Issue:** #71
- **Flags:** --reviewers deep --isolation worktree
- **Files:** all files in Sections 4 and 5; `tests/test_cli.py` if Step 57 creates it; otherwise put CLI integration in `tests/test_gemini.py`.
- **Produces:** adapter, profile, smoke command, offline protocol and production-entry integration tests, consumer disposition, and setup/run/report documentation.
- **Done when:** real `main` entry points drive Gemini fixture responses through production config, adapter, runner, scoring and all report formats; API key data appears nowhere in durable output; missing/blank keys and unknown providers fail before mutation; HTTP/timeout/malformed/blocked/empty/thought-only cases produce truthful terminal outcomes; budgets and resume never recall completed cells; changed Gemini settings reject resume without writes; old profiles retain hashes and old runs remain readable; full pytest, Ruff lint/format checks, strict mypy, and package build pass; separate independent code review passes.
- **Depends on:** 56 and 57 (must be DONE before implementation).
- **Status:** NOT STARTED

### Step 65: Live-smoke the Gemini adapter

- **Problem:** Offline fixtures cannot prove the account, requested model and live API agree. Run the shipped two-item smoke and inspect its stored evidence.
- **Type:** operator
- **Issue:** #72
- **Files:** local `data/runs/` and `data/reports/` artifacts only.
- **Produces:** local run/report artifacts and a recorded verification outcome; no code changes.
- **Done when:** `uv run mt smoke --gemini --config profiles/model-sweep-gemini-v1.json` exits zero with exactly two terminal cells, no parse/error/no-response failures, and concrete Gemini provider identity; its HTML report opens and retains both raw responses. This is pipeline verification only.
- **Depends on:** 64 (#71), API key provisioned locally, and permission for two Gemini calls.
- **Status:** NOT STARTED; no key present in process/user environments at discovery.

## 8. Risks and Open Questions

| Risk | Mitigation |
|---|---|
| API/model drift or account access failure | Explicit model; classify failure; never substitute another provider/model. |
| Credentials leaked by redirect or echoed error | Fixed origin, no redirects, header-only secret, bounded sanitized errors, regression anchors. |
| Reasoning text accidentally treated as an answer | Exclude thought parts; answer-only scoring and no-response tests. |
| New settings silently alter old profile identity | Omit absent optional Gemini fields in old serialization and test byte/hash stability. |
| Two-call smoke mistaken for a ranking | Exploratory metadata and no ledger promotion; calibration roster unchanged. |
| Step 57 output not yet available | Re-read its concrete producers and update impact mapping before Step 64 begins. |

No unresolved product choices block code. Live verification depends on external credentials;
build completion must report that limitation accurately.

## 9. Testing and Operator Workflow

Install: `uv sync --extra dev`. Development entry: `uv run mt --help` (no server).
Build: `uv build`. Test: `uv run pytest`. Lint: `uv run ruff check .`.
Format: `uv run ruff format --check .`. Types: `uv run mypy --strict measure_twice`.

Use narrow tests while implementing, then the full declared suite before review and after
merge. Native Windows skips Linux containment tests; no agent-containment code changes are
planned here. Offline integration uses a deterministic HTTP boundary double with actual
response bytes; config, request construction, response adapter, storage, scoring, and report
production remain real. A transport test verifies the genuine urllib request path and rejected
redirect behavior. The live Step 65 is the end-to-end provider smoke before any future campaign.

Build each code step in an isolated git worktree after dependency sync. A fresh developer agent
implements it, mechanical checks run before six independent reviewer lenses, and only a passing
review plus full post-merge tests permits DONE. The user authorized unattended implementation;
the user explicitly approved six native fresh-context reviewer agents on 2026-09-09, overriding
the installed Codex review adapter's dispatch halt while preserving its review gates. Stop neither for
routine implementation choices nor for absent Gemini credentials during the offline code step.

Provision the key through the operating-system environment-variable interface; do not paste it
into chat, shell history, or a tracked file. For this existing process, explicit runtime loading
of a newly provisioned user variable is an operator setup action. Normal invocations use process
environment only. Run the smoke with the committed profile, then `mt report RUN_ID --html`;
RUN_ID is the actual ID printed by the smoke command. Ctrl+C stops a run; `--resume` continues
unfinished cells under the original profile with completed cells retained.

## Appendix

### Decision Inventory

| ID | P/D | Choice | Status |
|---|---|---|---|
| P1 | P | Finish Steps 56 and 57 before Gemini implementation | accepted 2026-09-09 |
| P2 | P | Add one simple Gemini API model and reuse model-sweep reports | accepted 2026-09-09 |
| P3 | P | Keep the existing calibration roster intact | accepted with recommendation 2026-09-09 |
| D1 | D | Explicit stable Gemini 3.8 Flash model | defaulted |
| D2 | D | Stateless generateContent REST, low thinking, 4096 output tokens, 120-second timeout | defaulted |
| D3 | D | Header-only runtime environment key and fixed HTTPS origin | defaulted |
| D4 | D | Preserve provider identity and final-answer text; no retries | defaulted |
| D5 | D | Preserve old profile hashes and legacy reads | defaulted |
| D6 | D | Complete offline build independently of two-call live verification | defaulted |

### Primary API References

- [Model catalog](https://ai.google.dev/gemini-api/docs/models), checked 2026-09-09.
- [Gemini 3.8 Flash generateContent guide](https://ai.google.dev/gemini-api/docs/generate-content/latest-model), checked 2026-09-09.
- [API response contract](https://ai.google.dev/api/generate-content), including modelVersion, candidate content, finish reasons and usage metadata.
- [Thinking response parts](https://ai.google.dev/gemini-api/docs/generate-content/thinking), checked 2026-09-09.
- [API key setup and precedence](https://ai.google.dev/gemini-api/docs/api-key), checked 2026-09-09.
