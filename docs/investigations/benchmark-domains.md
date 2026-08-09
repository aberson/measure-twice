# Benchmark domains for workspace model-routing decisions

**Decision owner:** measure-twice Phase C, Step 10
**Scope:** the first flagship instrument, `tier-judging-v0`, and the follow-on calibration loop.
**Non-goal:** this document does not claim that a policy classification, a passing config test, or a
single incident is a model-quality measurement. Those remain claims with their actual ledger status.

The workspace needs an instrument that can distinguish a local model, the Claude tiers, and the
Fable-tier policy at the decisions they actually inform. A broad language benchmark is useful
background, but cannot by itself answer whether a reviewer verdict, a routing recommendation, or a
planning decision is safe in this workspace. HELM's scenario/metric framing and BIG-bench's diverse
task construction support a multi-dimensional design rather than one aggregate score [3][5]. The
first flagship is deliberately narrower: deterministic, source-grounded reviewer and gate judgments.

## 1. Benchmark-domain taxonomy

Every candidate belongs to one primary domain and records any secondary tags. A single item must
not pretend to measure two unrelated capabilities; split it if two independent correct answers are
needed.

| Domain | What the item measures | Construction pattern | Gold and scorer | Main confound |
| --- | --- | --- | --- | --- |
| Judging / grading | Detecting a specified defect or deciding whether supplied evidence reaches a fixed bar | Give a compact artifact, evidence rules, and a `PASS`/`FLAG` contract; include a nearby-but-nonfatal counterexample | Curated binary label; production verdict parser and deterministic accuracy | A persuasive explanation must not earn credit when the label is wrong |
| Code authorship | Producing a change that satisfies an executable behavior | Freeze a small repository slice, request one function/patch, and run hidden plus visible tests | Unit/integration tests; retain raw patch and test output | A benchmark may reward code memorization or a patch that edits tests rather than behavior |
| Planning | Sequencing actions under explicit prerequisites, resources, and forbidden transitions | Provide an initial state, goal, allowed actions, and constraint list; generate controlled variants | Deterministic plan simulator/validator; score goal achievement and invalid action count | Natural-language plausibility can hide an invalid transition; use obfuscated or counterfactual variants [6] |
| Extraction | Recovering bounded facts, fields, or citations from supplied source material | Require an exact JSON object or span list with explicit source boundaries | Strict schema plus exact values/spans; reject extras and unsupported fields | Surface-token copying can look correct while provenance/ranges are wrong; score both content and locator |
| Instruction following | Satisfying explicit format, count, inclusion, exclusion, and ordering constraints | Combine 1-3 independently verifiable constraints in one prompt; perturb one constraint at a time | Mechanical validators, following IFEval's verifiable-instruction approach [7] | A fluent answer can conceal an ignored constraint; no free-form judge decides compliance |
| Synthesis | Producing a bounded conclusion that remains faithful to a supplied evidence set | Supply a small, dated evidence pack with required claims, counterevidence, and uncertainty rules | Claim-by-claim support ledger first; human/secondary rubric only after deterministic checks | Reference-overlap metrics can reward a smooth but unsupported summary; factual support must be scored independently [11][12] |

The categories are grounded in representative benchmark designs: executable code correctness
(HumanEval and SWE-bench [8][9]), systematic planning variants [6], verifiable instruction
constraints [7], span-oriented reading comprehension [10], and multi-dimensional summary evaluation
[11][12]. They are a taxonomy for candidate generation, not a promise that every domain belongs in
the first flagship suite.

## 2. Item-design patterns

### Shared item card

Every candidate must be authored as an item card before it enters a suite:

```text
id: stable safe slug
primary_domain: one taxonomy row
prompt: self-contained task; no hidden template fallback
expected: deterministic target label/value/plan
tags: lens, difficulty bucket, provenance class, defect family
difficulty_prior: easy | lower-mid | upper-mid | hard | adversarial
provenance: authored:<reason> | harvested:<workspace path and lines>
source_snapshot: content hash of the source material used to author it
gold_rationale: why the target is correct and why the nearest distractor is wrong
anti-shortcut: the token/heuristic a model must not be able to exploit
```

The candidate then receives a source review: a second reviewer verifies the `expected` result from
the original artifact, and an intentionally bad response is created for the frozen anchor corpus.
This follows the workspace's production-path and good-versus-garbage calibration requirements, not
an LLM's self-grading.

### Domain-specific patterns

- **Judging/grading:** create minimal pairs. One has the target defect; its paired near-miss shares
  vocabulary and shape but is acceptable. State the decisive evidence in the prompt, so a correct
  verdict is not recovered from a remembered project outcome. CheckList's behavioral-test framing
  motivates testing a named behavior rather than relying on pooled held-out accuracy [15].
- **Code authorship:** take issue-to-test shape from SWE-bench, but keep v0 tasks small enough for
  the workspace's real runner and give every task a clean sandbox. Tests must check production
  behavior, not merely syntax or a matching diff [9].
- **Planning:** vary one structural difficulty factor at a time: plan length, interacting
  preconditions, negative constraints, misleading names, or counterfactual action semantics.
  Generate and validate plans mechanically, as PlanBench does, instead of judging prose plans by
  plausibility [6].
- **Extraction/instruction following:** construct perturbation families where one field name,
  source range, count, or negated requirement changes. The expected JSON/label must change too;
  otherwise the family is not discriminative.
- **Synthesis:** record atomic evidence claims and their support spans before any prose is scored.
  Require uncertainty and counterevidence handling when the evidence pack does not settle a claim.
  Never use a similarity score as factuality evidence by itself [11][12].

## 3. Difficulty calibration methods

### Classical item statistics

For a binary item, record empirical difficulty as the fraction correct across every completed
`(model, item, sample)` cell. Record a simple discrimination signal as the difference in correctness
between the top and bottom score groups, with group definitions and sample counts stored beside the
result. This is descriptive only: with the initial five-model roster, it is not a stable psychometric
estimate. It is sufficient to find all-pass, all-fail, and non-discriminating items before making a
routing claim.

### IRT-lite, not an authority oracle

Rasch's one-parameter formulation motivates separating a model-position estimate from an item
difficulty estimate [1]. After at least two independent sweeps and enough completed binary cells,
an offline diagnostic may fit the simple form `P(correct) = sigmoid(theta_model - beta_item)`. It
may rank candidate item difficulty and flag poor fit, but it must not select a routing policy,
publish a precision claim, or replace observed score tables. The roster is small, models are not an
independent human sample, and item responses can be correlated by shared prompt patterns.

### Adaptive item replacement

After each complete roster sweep:

1. Flag items with empirical difficulty `0.0` or `1.0`, a missing terminal cell, broken parsing,
   or a provenance/source-hash failure.
2. Keep a frozen anchor set unchanged. Replace only non-anchor saturated items with a new item card
   targeted at the missing difficulty bucket or failure mode.
3. Assign a new suite version/content hash. Never compare score averages across hashes as though
   the instrument were unchanged.
4. Re-run the replacement items across the full roster; retain old rows as historical evidence.
5. Track item exposure: avoid reusing one harvested artifact or near-duplicate template as the
   apparent source of many independent wins. Adaptive-testing research treats exposure control as a
   first-class constraint, not an afterthought [2].

Dynamic renewal is a defense against benchmark saturation, not proof of generalization. Dynabench
demonstrates the value of adding examples that target model failures, while the ledger keeps each
replacement's provenance and decision scope visible [4].

## 4. Anti-saturation and coverage controls

`tier-judging-v0` must be designed to have models score inside the planned `[5, 95]` acceptance
band, but no item is discarded merely because one model finds it hard. Before its first sweep, the
author must satisfy all of the following:

- At least 100 binary verdict items, with every tag represented by at least eight items.
- Five intended `difficulty_prior` buckets: easy, lower-mid, upper-mid, hard, and adversarial.
  No bucket may contain fewer than ten candidates; the rest may be assigned by targeted gaps.
- Minimal-pair coverage for every defect family used to influence routing: a positive and a
  superficially similar negative case.
- Source/provenance diversity: no single project, skill, template, or source snapshot contributes
  more than 20% of the initial suite.
- A frozen good/garbage response pair for every active scorer path. The permanent anchor gate is
  stronger evidence than a newly generated example that happens to look difficult.

At calibration time, report per-model scores, per-tag scores, all-pass/all-fail items, parse failures,
defer/error rates, and source/provenance mix. HELM and BIG-bench both illustrate why one aggregate
number masks meaningful capability and robustness differences [3][5].

## 5. Judge-circularity and production-path guards

Tier-routing claims are deliberately restricted to deterministic `verdict` or `exact` scoring over
curated gold. The runner must use the same parser and production artifact shape that the live
consumer uses. An empty/no-response result is force-scored zero before any judge call, and malformed
responses remain a recorded failure signal rather than a silent exclusion.

LLM rubric judging is permitted only for later capability profiling. It must use multiple samples,
report parse failures per judge, and never promote an ASSERTED routing claim to MEASURED by itself.
The model being evaluated cannot grade its own output; a stronger model's rubric is secondary
evidence, not the gold label. This protects against circularity and evaluator bias, a problem IFEval
avoids for its verifiable constraints and that summary-evaluation work treats as a separate
meta-evaluation problem [7][11].

## 6. Contamination and provenance controls

Public benchmarks, committed examples, and popular code can already be in a model's training or
post-training data. Contamination is therefore a threat to the interpretation of a high score, not a
binary fact that can be wished away [13][14]. For v0:

1. Prefer workspace-authored or newly transformed examples with explicit source snapshots over
   public benchmark prompts.
2. Store provenance class, source path/line span, content hash, author date, and transformation
   recipe for every harvested item. Never present copied benchmark text as novel.
3. Do not place the gold answer or a near-verbatim solution in the prompt, repository fixture, or
   model-specific demonstration set.
4. Add counterfactual/minimal-pair variants and evaluate them as a family. A high score that
   collapses under a semantics-preserving change is a contamination or shortcut warning, not a
   routing result.
5. Keep held-out future items private until the observation run. Once an item is committed or
   repeatedly used, it remains a regression item and loses its claim to be fresh capability evidence.

## 7. Decision record: MT-DOM-01

**Decision:** build `tier-judging-v0` as a deterministic binary reviewer/gate instrument first.

| Choice | Decision | Rationale and boundary |
| --- | --- | --- |
| Decision scope | Style, correctness, and grading judgments that actually inform local-versus-primary routing | It does not measure planning, code authorship, or Fable seed quality; those need separate instruments and preregistrations. |
| Suite scoring | One global `verdict` spec with `pass` and `flag` labels | The current suite schema has one scoring type per suite. Any exact extraction check is expressed as a deterministic pass/flag verification or deferred to a companion exact suite; rows do not mix scorer semantics. |
| Item count and tags | At least 100 items; tags include `lens`, `difficulty_bucket`, `provenance_class`, and `defect_family` | Supports the planned per-tag minimum and exposes localized failure modes. |
| Source material | Workspace artifacts with line-level provenance, plus authored counterfactuals | Keeps the decision anchored to production shapes while making transformation/contamination explicit. |
| Gold construction | Two-person source review plus deterministic parser/test; frozen good/garbage anchors | A model output is never the sole authority for its own verdict. |
| Calibration loop | Complete roster sweep, empirical saturation flags, then versioned replacement of non-anchor saturated items | Preserves historical rows and refuses cross-hash score comparisons. |
| Promotion rule | Only a pre-registered real production run can change a routing claim's evidence status | Static config checks, a rubric score, endpoint timing, or a model's prose rationale cannot promote the claim. |

### Flagship authoring checklist

For each Step-12 item, the author must be able to answer all of these without another design
document:

1. Which routing decision or reviewer behavior does this item exercise?
2. What exact artifact/evidence appears in the prompt, and what deterministic `pass` or `flag`
   result follows from it?
3. What is the nearest plausible wrong answer, and how does the item prevent a lexical shortcut?
4. Which difficulty bucket and defect-family quota does it fill?
5. What source snapshot/provenance class produced it, and could that source be contaminated or
   already memorized?
6. Which production parser/test path will score it, and which frozen anchor protects that path?

An item that cannot answer any one of these stays a candidate; it does not enter the flagship suite.

## External sources

1. Georg Rasch, *Probabilistic Models for Some Intelligence and Attainment Tests* (1960):
   [bibliographic record and original preface](https://www.rasch.org/memo63.htm).
2. Wim J. van der Linden, [*Controlling item exposure and test overlap on the fly in computerized adaptive testing*](https://pubmed.ncbi.nlm.nih.gov/17650362/) (2007).
3. Percy Liang et al., [*Holistic Evaluation of Language Models*](https://arxiv.org/abs/2211.09110) (2022).
4. Douwe Kiela et al., [*Dynabench: Rethinking Benchmarking in NLP*](https://arxiv.org/abs/2104.14337) (2021).
5. Aarush Srivastava et al., [*Beyond the Imitation Game*](https://arxiv.org/abs/2206.04615) (2022).
6. Karthik Valmeekam et al., [*PlanBench*](https://arxiv.org/abs/2206.10498) (2022).
7. Jeffrey Zhou et al., [*Instruction-Following Evaluation for Large Language Models*](https://arxiv.org/abs/2311.07911) (2023).
8. Mark Chen et al., [*Evaluating Large Language Models Trained on Code*](https://arxiv.org/abs/2107.03374) (2021).
9. Carlos E. Jimenez et al., [*SWE-bench*](https://arxiv.org/abs/2310.06770) (2024).
10. Pranav Rajpurkar et al., [*SQuAD: 100,000+ Questions for Machine Comprehension of Text*](https://aclanthology.org/D16-1264/) (2016).
11. Alexander R. Fabbri et al., [*SummEval: Re-evaluating Summarization Evaluation*](https://aclanthology.org/2021.tacl-1.24/) (2021).
12. Feng Nan et al., [*Improving Factual Consistency of Abstractive Summarization via Question Answering*](https://arxiv.org/abs/2105.04623) (2021).
13. Yihong Dong et al., [*Generalization or Memorization: Data Contamination and Trustworthy Evaluation for Large Language Models*](https://arxiv.org/abs/2402.15938) (2024).
14. Yanyang Li et al., [*C²LEVA: Toward Comprehensive and Contamination-Free Language Model Evaluation*](https://arxiv.org/abs/2412.04947) (2024).
15. Marco Tulio Ribeiro et al., [*Beyond Accuracy: Behavioral Testing of NLP Models with CheckList*](https://aclanthology.org/2020.acl-main.442/) (2020).
