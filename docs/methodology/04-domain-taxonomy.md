# Domain taxonomy and the first flagship instrument

Step 10 separates two questions that are easy to collapse: what kinds of model work exist in the
workspace, and which of those questions the first benchmark can honestly answer. The full taxonomy
includes judging/grading, code authorship, planning, extraction, instruction following, and synthesis.
The first flagship chooses only deterministic reviewer and gate judgments because those are the
decisions currently being used to justify tier routing.

This is a validity decision. A result on a planning task does not automatically establish that a
local review lens is safe; an endpoint timeout does not establish that a Fable seed is worthwhile.
Each future domain therefore needs its own pre-registered decision statement, production artifact,
gold construction, and failure policy before it can change the evidence ledger.

`tier-judging-v0` will use one `verdict` scorer and explicit `pass`/`flag` labels. The one-scorer
choice follows the current suite schema and keeps every score on the same deterministic contract.
Exact extraction work remains a candidate family or a later companion suite; it will not be mixed
silently into a verdict instrument.

Calibration is iterative but versioned: run the complete roster, inspect saturation and per-tag
failure modes, replace only non-anchor items, and mint a new content hash. The historical rows stay
available, while cross-hash comparisons remain forbidden. This turns replacement into measurement
maintenance rather than score laundering.

The durable anti-circularity rule is simple: a model cannot promote a decision by grading its own output.
Deterministic gold and production parsers lead; LLM rubrics, where later useful for profiling, are
secondary evidence with their parse failures and disagreement preserved.
