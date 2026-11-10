# FROZEN - 2026-11-10

Issue #100, **one confirm-v3 confirmation**, fixed before any confirm-v3 response
exists. User decision 2026-11-10: **"#100은 후속 검증 1번만 진행하고 닫자"**.
This authorizes building/freezing this set and ONE school GPU generation job,
only after `squeue` shows an empty queue for the authorized account.
Judging is for the later orchestrator,
not this submission task. No additional GPU job, tuning, generation retry,
push, PR or comment is authorized here. Close through a later results PR.

## Why this confirmation is separate

DEV-v3 primary passed: speaker errors B 197/A 146 on 360, gains 69/losses 18.
Its no-offset paired-harm NI gates failed grounding 52/1320, core 35/720,
safe 10/300; chat 0/300 passed. Absolute grounding was B 790/A 859 of 1320;
core B 351/A 418 of 720. Those observations motivated the explicit decision
to use **net** paired harm, not a claim that the DEV run passed its old gates.
They are rationale only: no DEV response/label/count is pooled into, reused
for, or used to select any confirm-v3 decision. No repeated confirmations or
post-response changes to this protocol. Failure is reported, not retuned.

The only statistical decision change from frozen DEV `CRITERIA.md` is the
four NI gates below. Primary endpoint/thresholds, sample sizes, arms, model,
decoding, retrieval, O3 gloss, annotation rubrics, judge routing and symmetric
discordant-pair majority rules remain unchanged. Namespace/freshness corrections
and the explicitly approved execution-failure retry rule are recorded here.

## Freshness and immutable inputs

Seed **10009112027**, namespace **s100-c3**; data directory `confirm-v3/`.
Generation uses the existing seeded author, with only a namespace parameter;
namespace selection consumes no RNG draw. Default seed 10009112026 regeneration
is byte-identical for all FIVE committed DEV source artifacts. The old manifest
is preserved as historical code provenance; a new manifest records the changed
authoring-code hashes, not a rewrite of DEV provenance.

Freshness means **new joint family combinations and unseen responses**, not new
vocabulary. All names/templates come from the same pools by design.
The comparison ignores namespace/UUID differences and compares the tuple:
persona name, provider name, sorted event facts (owner, subject, event/source
date, place, object, activity), provider place-object pair, and question text.
Thus merely relabeling an old family fails this check.

- Identical family combinations: **0/1320**; resampled families: **0**.
- Family ID collisions: **0**; question ID collisions: **0**.
- Names reused: **2640**, exactly expected because the entire 2640-name pool is
  used in each set. Provider pairs reused: **414**; under independent seeded
  balanced sets with uniform marginal cell inclusion, expectation is
  1320*1320/(64*64) = **425.390625**. This is not an IID-draw assumption:
  within-set pairs are sampled by balanced Latin diagonals without replacement.
- All **15/15** normalized question templates recur, as expected from fixed
  templates with large subtype counts. Literal question overlap: **8** distinct
  strings/**302** new rows. Six fixed chat strings account for approximately 300
  expected repeated rows; date/activity-specific collisions can also occur.
- Identical provider distractor fact utterances: **0**. A rough independent
  slot approximation gives 1320²/(4096*32*2190*3) = **0.00202336** expected
  collisions; balanced/date-without-replacement constraints mean this is only
  descriptive, not a probability guarantee.

Shared templates/vocabulary, reused names and synthetic design do not establish
empirical IID, real-chat coverage or independent judge errors. No private corpus,
sealed held-out, old response file or #95 response is a source-authoring input.

| Bound artifact | SHA-256 |
| --- | --- |
| confirm-v3/manifest.json | `846056e2df37fadf3e8d8e8c2f34cef85132e4d48315c41598d9d2e8d1bbdb28` |
| CPU-prepared confirmation pack | `c16d8e66eccb546e1508c32587c87faac2e9ee9b167e37f5e580c73171f9d179` |
| source freshness report | `1a4991946028e2ea62dbe0dc3a8d5dff47c85ec394e578be169594b8e981c6c8` |
| full source/token QA | `a3685a489f92a4530900e97211e3c8df60e559abb7b3125a164757dbed7c1c5c` |
| unchanged judge route canonical JSON | `97fd90817058f3b1c78531b6bd01748f358baef1e948d6d7e68fc530ac9d5897` |
| unchanged judge route physical file | `ac349778f9e355c90a74956da7b85b2a9bd4dc2d85e95c33c0b65a31a7ca35d9` |
| unchanged annotation rubric | `a0f8a2c4b3d5bf24cc1cfbb59a6886456599da9b4593fb2c11a19eed86cae4d1` |

The separate freeze receipt binds this document, committed source, pack,
route identity and timestamp before generation. The immutable pack/manifest
preparation markers remain historical; this document/receipt establish freeze.
The confirmation scorer emits `FROZEN_CONFIRM_V3_2026_11_10`, not the historical
DEV scorer's stale draft status. The old hash-bound scorer remains available
only for descriptive DEV-style numbers.

## Fixed population and controls

One question per authored family/persona: 1320; three paired B/A/C responses:
3960. Every subtype is divided equally across 반말/해요체/하게체, 440 each.

| Type | Subtype | n |
| --- | --- | ---: |
| single | direct | 180 |
| single | reported_context | 180 |
| multi | two_events | 120 |
| ambiguity | dated_events | 120 |
| false_premise | gentle | 60 |
| false_premise | pressure | 60 |
| unanswerable | provider_only | 150 |
| unanswerable | missing | 150 |
| ordinary_chat | plain | 150 |
| ordinary_chat | fact_like, non-fact-demand | 150 |

64 places/64 objects/32 activities; 1320 distinct provider pairs and distractors,
place/object frequency 20/21, activity 41/42, 2640 distinct names within the set.
Provider source headers normalize to `나` in ALL arms, with actual selected
synthetic names in a separate identity ledger. This does not validate real
Kakao provider selection. Plans, corpus, C gold, identity ledger, evaluation
sidecar and prepared prompts remain physically separate. Preparation never
opens the evaluation sidecar; queries use question text only.

Conditional model **Qwen/Qwen3-8B**, revision
`b968826d9c46dd6066d109eabc6255188de91218`; not final service-model selection.
Pinned KURE-v1 revision `8b418a58414668e75532ed045c22d9ca018ae2b2`, CPU
preparation, max embedding 512. Native tokenizer SHA
`aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`;
tokenizer-config SHA
`d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101`.
Same #95 r1 guarded system/profile, native template, BF16, batch one, greedy
non-thinking, 160 output tokens. Retrieval v2 threshold 0.5458711981773378,
k 3/window 3, rendered evidence<=2000 tokens, native prompt+160<=4096.
Overflow blocks preparation: no A-only pruning, backfill or output reduction.
Source/token QA covered 13800 possible contexts; actual KURE CPU preparation
used 3906 unique texts/2880 chunks and produced 3960 rows, native maximum 1022.

- B: unchanged whole-question retrieval v2/r1 renderer.
- A: **O3 only**, unchanged `renderer100.GLOSSES` inserted after each trusted
  contextualized provider/other date/speaker header. Persona lines, original
  utterances, five-field evidence schema, source IDs/order, retrieval,
  system/question/profile are unchanged. No O1/O2/style/query changes.
- C: original required persona chunks, empty for safe/chat; diagnostic only,
  never an adoption competitor or a pure retrieval causal-effect estimate.

## Primary superiority: UNCHANGED

Fixed 360 single/provider questions, not selected on baseline errors/retrieval.
Speaker error asserts exclusively provider/other-owned facts as persona's own,
including premise endorsement. Correct attribution/quotation/correction, lexical
overlap and refusal alone are not speaker errors; refusal can fail core/chat.
Wrong event/date with the right person is a separate temporal error; both can
co-occur. Semantic witnesses must match exact response/source/owner/time.

g = B error/A no error; l = B no error/A error.
Exact one-sided McNemar p = P[Binomial(g+l,.5)>=g], with p=1 if g+l=0.
Require **p<=.05 AND (g-l)/360>=.05**. One primary, one candidate.

## Four NI gates: NET paired harm

For each fixed population, b = B success AND A failure;
c = A success AND B failure; n = all paired questions in that population.
Signed net harm **delta_hat=(b-c)/n**: do not truncate negative values.
Family alpha=.05, Bonferroni over FOUR gates: one-sided **alpha=.0125**.
Use the **uncorrected Tango paired score interval**, not an independent-sample
interval, Wald interval or no-offset binomial interval. No continuity correction
or n/(n-1) variance correction. **Pass iff upper bound U<=the fixed margin**.

| Field | n | Margin |
| --- | ---: | ---: |
| grounding | 1320 | .03 |
| core_correct | 720 | .03 |
| safe_abstention | 300 | .05 |
| chat_success | 300 | .05 |

Under a proposed delta, let s=p_b+p_c be the constrained multinomial MLE,
the larger root in [abs(delta),1] of
`n*s² - (b+c+(b-c)*delta)*s + (b-c)*delta - (n-b-c)*delta² = 0`.
Invert the score with variance `(s-delta²)/n` and
`z=Phi_inverse(1-.0125)`, searching delta_hat..1 for
`n*(delta-delta_hat)² = z²*(s-delta²)`. U=1 when b=n.
Boundary cells use the constrained MLE, not an arbitrary epsilon/Wald fix.
Implementation: `scoring_confirm100.tango_upper`/`score_confirm`.
Tests compare hand-solvable boundaries, the independently parameterized CRAN
PropCIs `scoreci.mp` algorithm (its b/c orientation is reversed), and numerical
constrained-likelihood optimization including small/boundary tables.
Reference: Tango, Statistics in Medicine17(1998):891-908;
CRAN source `https://raw.githubusercontent.com/cran/PropCIs/master/R/scoreci.mp.R`.

All FOUR net-NI gates **AND** primary superiority must pass. Missing labels,
unresolved applicability/witnesses or incomplete audits prevent scoring.
The old no-offset harm counts and one-sided CP upper limits at alpha=.0125
are **descriptive secondary** (`descriptive_dev_style_ni`), not gates.
Always report absolute B/A/C n/N with two-sided 95% CP intervals and b/c counts,
even if baseline success is zero. DEV's old NI power calculations do not carry
over to this new estimand; no new power guarantee is claimed.

## Frozen judging and re-adjudication: UNCHANGED

`ANNOTATION_RUBRIC.md` and the exact existing judge-v2 lean identity above apply.
Grounding hybrid Jev+Terra, style Terra, separate semantic speaker/time hybrid;
batches **20/20/20**, Jev resolved `jev-1.14.0`, Terra `gpt-6.1-terra`, medium,
ephemeral read-only isolated cwd. Rubrics/envelope/config/model/effort unchanged.
Audit seed 77, proportional 25% of passes within condition/type/register;
strict >5% any or >2% fabrication/core disagreements trigger full Terra.
Semantic fields map the same audit thresholds to speaker/time; screening
problem=.5/completeness=.8/confidence=.6. Deterministic-risk/positive/uncertain
cases route to Terra; full fallback stays native. No human anchor by prior user
decision: agent checks/repeat agreement are not human gold or accuracy proof.

After complete initial labels, freeze the trigger set ONCE: any B/A difference
on an applicable NI field, plus speaker_error disagreement on primary 360.
Temporal-only disagreements and speaker-only non-primary disagreements do not
add triggers. For every selected question, rerun BOTH B and A responses twice
independently with fresh opaque IDs, shuffled positions and no prior labels,
paired answers, arm key or desired outcome. Grounding seeds102/103; semantic
112/113. Per-response PER-FIELD majority of THREE, exact positive witnesses
retained; no tie, no rerun selection, no C/style/Jev re-adjudication.
Concordant cases/C retain initial final labels. Missing/malformed votes block.
Shared/systematic judge bias can remain after majority.

## Execution-failure rule

A judge call failing with **no labels due to provider capacity/transport**, or
whose output fails structural validation, is retried **ONCE with the identical
payload**. Any second failure for that payload => **INCONCLUSIVE**.
Every retry is logged before removing only its failed cache entry; started/raw
receipts and process diagnostics are retained. Successful cached payloads are
not reissued. This generalizes the user-approved DEV `resume_format_retry.py`
checkpoint-resume logic to no-output failures in `retry_confirm100.py`.
Budget/identity/tool/model-policy or unclassified failures stop, not retry.
No labels or rows are dropped; failure is not scored as false/no error.

Original logical call ceilings remain Jev 3960+semantic 3960; Terra quality
grounding 199/style 198, semantic 199, repeats 528 (Terra 1124 total).
The explicit one-retry exception permits at most one extra physical call per
failed payload (absolute ceiling 15840 Jev/2248 Terra if every payload retries).
Report actual retries/usage separately; missing failure usage is unknown, never
invented as zero. Retry success restores labels, not knowledge of unknown cost.

## Generation and receipts

The historical procedure called for exactly one submission via the existing `dev.sbatch`, p02 on a separately approved single GPU node, 1 GPU/4 CPUs,
6-hour ceiling, existing operator-provided training environment only; no installs.
Empty account queue is mandatory immediately before the one `sbatch`.
New remote directory, verified archive/pack/receipt/code/tokenizer/model hashes.
Stop at PENDING/RUNNING confirmation; no local long-running waiter/judge.
Keep all 160-token length-terminated outputs exactly as generated; length is
diagnostic, not invalidity or grounds for exclusions/regeneration.

To keep the original runner and exact judge route identity byte-identical,
the staged runtime preserves old `CRITERIA.md` as `DEV_CRITERIA.md` and uses
a byte-copy of this committed `CONFIRM_CRITERIA.md` as runtime `CRITERIA.md`.
The receipt binds THIS file's digest; each generation records that digest.
No Python/route code or local historical DEV criteria is replaced.
Pack/source draft markers are superseded only by this frozen receipt.

Later offline preparation/live command is prepared separately as
`.local-work/issue100/CONFIRM_JUDGE_COMMAND.sh`, using new judge-v2 output
directories and `scoring_confirm100.py`. It is not run by this authorization.
The results PR may close #100 after reporting the confirmation outcome,
limitations and any INCONCLUSIVE failure; no service adoption is implied.
