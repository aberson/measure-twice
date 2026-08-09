# Claims and evidence ledger

The tier-routing map needs to answer both “what is pinned?” and “why should that decision be
trusted?” A config boolean or a passing policy-shape test can answer the first question. It cannot
answer the second, so the evidence ledger is the source of truth for provenance and confidence.

## Method

Each JSONL claim carries a stable id, decision surface, source file and line range, SHA-256 hash of
the cited quote, evidence run ids, a verdict, preregistration when required, and a verification
timestamp. `mt claims audit` re-hashes only the cited lines. A changed, missing, unreadable, or
escaping source makes the claim `STALE`; the audit never invents a new hash or restores a stale
claim automatically.

The status vocabulary is deliberately small:

- `MEASURED`: a pre-registered claim with at least one evidence run id.
- `PARTIAL`: direct evidence informs part of the decision but not the full decision scope.
- `ASSERTED`: a policy, classification, or existing configuration that has not been measured at
  the relevant quality scope.
- `STALE`: a once-cited source has changed and must be reviewed before the claim can inform a
  decision.

## What the first ledger establishes

The ledger records 28 current claims. Its only `MEASURED` row is the pre-registered A3 paired
deep-research run: Sonnet fan-out arms showed no decision-changing gross quality gap versus a
fully-Fable baseline. That result remains narrow (`n=1` versus `n=1`, web drift, and a gross-gap
criterion), so it does not prove the broader escalation policy.

The all-Fable fan-out incident and local endpoint timing profile are `PARTIAL`. They justify
operational safeguards—arm pinning, timeouts, and token headroom—but they do not establish that a
particular local reviewer slice is accurate enough to influence a real decision.

The remaining local-routing and Fable-escalation entries are `ASSERTED`. The next benchmark work
must target the actual decision with a pre-registered production artifact: plan-init and user-debug
seed choices, the Style-only reviewer rule, and the claimed memory-distill Fable comparison are the
highest-priority gaps.

## Limits

Quote hashes establish citation freshness, not correctness of an interpretation. A fresh assertion
is still an assertion, and a successful static config parse is not a quality result. The ledger also
does not reinterpret a changed source: it surfaces `STALE` so a reviewer can decide whether the
claim, quote range, status, and any associated benchmark remain valid.
