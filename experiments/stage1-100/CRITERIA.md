# FROZEN - 2026-11-09

Issue #100. Frozen on 2026-11-09 by explicit user decision "지금 고정 + 생성":
freeze criteria/data/judge route and submit ONE school GPU generation job.
O3 only; 1320 questions / 3960 paired B/A/C responses; all counts, margins,
tests and discordant-pair two-repeat majority-of-three rules below are unchanged.
Judging, push, PRs, comments and service adoption are not authorized.
The separately authorized #95 DEV annotator check is judge validation only,
not #100 criterion tuning or an experimental decision run.

Frozen data manifest SHA-256:
`26d6d693cb9002cb33251b564dc9cb8f16d3f9ba7ef57e4dd31df660ecc450e4`.
Frozen KURE-prepared pack SHA-256:
`9af7eefca1efd189c724368721d3af9b0ce0ff06c501b40eee2f9785bb542291`.
The selected judge-v2 lean route is grounding20/style20/semantic20;
JUDGE_ROUTE_IDENTITY canonical SHA-256:
`97fd90817058f3b1c78531b6bd01748f358baef1e948d6d7e68fc530ac9d5897`.
Human anchor labeling is skipped by user decision. This is a judge limitation:
agent spot checks and repeat agreement are not human gold or evidence of
absolute annotation accuracy; shared judge bias may remain after majority.
The separate freeze receipt binds this document, rubric, pack, route identity
and the frozen commit before generation.

## Scope and controls

Fresh synthetic DEV only; one question per independent authored family/persona.
No sealed held-out, old #95 generations for tuning, private corpora, service code,
new model, LoRA, changed serving engine, or external contract change.
Shared vocabulary/templates do not establish empirical IID.

Conditional baseline: Qwen/Qwen3-8B, revision
`b968826d9c46dd6066d109eabc6255188de91218`, not a final service-model selection.
KURE-v1 revision `8b418a58414668e75532ed045c22d9ca018ae2b2`.
Use #95 r1 guarded system/profile, native template, BF16, batch one, greedy
non-thinking, 160 output tokens. Retrieval v2: threshold
`0.5458711981773378`, k=3, window=3. Rendered evidence <=2000 tokenizer
tokens; native prompt +160 <=4096. Overflow blocks preparation; no A-only
pruning, backfill, output reduction, or output-conditioned source resampling.

## Fixed population and diversity

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
| ordinary_chat | fact_like non-fact-demand | 150 |

Total 1320 families/questions/personas, 440 per register (반말/해요체/하게체).
Each subtype is equally allocated across registers. Core denominator 720;
grounding 1320; safe 300; chat 300. B/A/C share paired question identities:
3960 later generations. Neither slots nor claims are independent sample units.

64 places, 64 objects, 32 activities; 1320 distinct provider place-object pairs
and distractor utterances; place/object frequency 20 or 21, activity 41 or 42.
2640 unique ordinary-looking synthetic Korean full names. Names may coincide
with real people but convey no real-person facts. Same lexical pools are
counterbalanced across provider/persona roles. Source identity ledger explicitly
maps names; selected provider header is normalized to `나` in ALL arms for the
existing fixture adapter. This does not validate real Kakao provider selection.

## Arms: one candidate, no contract change

- B: exact #95 whole-question retrieval v2 + r1 renderer.
- A: O3 only. On each contextualized provider/other line, insert the fixed gloss
  from `renderer100.GLOSSES` after the unchanged date/speaker header. Use trusted
  source chunks. Persona lines, original utterances, five-field evidence schema,
  source IDs/order, retrieval and system/question/profile stay unchanged.
  No O1 fields, O2 filtering, style examples, query decomposition or fact rewrite.
- C: B renderer with required original persona chunks only; empty for safe/chat.
  Diagnostic only; B-C difference is not a pure retrieval causal effect.

Source corpus/public plan, source identity ledger, C gold context, evaluation
sidecar and prepared model-facing prompts remain separate. Preparation never
opens the evaluation sidecar. Query construction accepts question text only.
Rendered-to-source row IDs preserve original API evidence; O3 is not returned
as rewritten source evidence under its original ID.

## Semantic annotation and endpoints

`ANNOTATION_RUBRIC.md` specifies separate speaker/time annotation with blinded
condition IDs, response spans, subject/utterance-owner fields and source witnesses.
All 3960 endpoint records, audits and adjudications must be complete, with zero
unresolved cases. No dropping ambiguous/failed cases or treating them as no error.
Keyword matches, `fabricated`, and `error_split.evidence_state` do not substitute
for semantic attribution labels.

Primary: per-question speaker error on the FIXED 360 single/provider cases.
Error means asserting exclusively provider/other-owned facts as persona's own,
including premise endorsement. Correct attribution/quotation/correction, bare
lexical overlap and refusal alone are not speaker error. Wrong event/date with
correct person is temporal error; both may co-occur. Refusal may fail core/chat.

NI labels retain existing judge semantics:
grounding = no fabrication or unsupported negation (all 1320);
core_correct = all required facts/corrections (720);
safe_abstention = appropriate non-fabricating unanswerable handling (300);
chat_success = appropriate natural chat without invented facts/unneeded refusal (300).
Style remains Terra. Grounding stays hybrid Jev + Terra including the unchanged
audit/full-Terra fallback policy; exact existing config/rubric hashes are
bound below, not inferred from historical aggregate audits.
The new semantic annotation uses the same hybrid route with Terra adjudication.
The local bridge is `.local-work/judge-v2/stage100_route.py`; private truth and
judge prompts remain local. Its separately opt-in judge command is not executed
on #100 answers by this generation authorization.

## Frozen execution route

User decision 2026-11-09: use judge-v2 audit-first ordering and the lean Terra
envelope, keeping the imported grounding/style rubric, Jev model, screening,
audit seed77 / proportional 25%-of-passes sample, strict >5% any / >2%
fabrication or core fallback thresholds, composites and overrides unchanged.
Terra remains `gpt-6.1-terra`, medium effort, ephemeral read-only, isolated
temporary cwd outside Replica; exact lean config and code/rubric hashes are in
`.local-work/issue100/JUDGE_ROUTE_IDENTITY.json`. Bind that manifest's canonical
SHA-256 in the freeze receipt, and verify every listed file before calls.
The selected identity artifact retains its pre-freeze status string unchanged;
this document and the separate receipt establish the freeze without rehashing it.
The identity-covered annotation rubric also retains its pre-freeze wording
byte-for-byte. Its draft labels are historical preparation text superseded by
this frozen criteria and receipt, not a change to its annotation rules.

Selected manifest canonical SHA-256:
`97fd90817058f3b1c78531b6bd01748f358baef1e948d6d7e68fc530ac9d5897`.
Manifest file SHA-256:
`ac349778f9e355c90a74956da7b85b2a9bd4dc2d85e95c33c0b65a31a7ca35d9`.
Core code/rubric SHA-256 values (the manifest contains all imported files):

| File | SHA-256 |
| --- | --- |
| judge_v2.py | `4d5d9cda08565ef57544ffb903cc71e3dd80cdfd2f5bd0b2802af714e57a7093` |
| stage100_route.py | `f8e9154eb95717195da70633caf2f467aff39606463d1efcc45ce55efed041f5` |
| semantic100.py | `edb85e6809ca49100f4a60aa1f85177cf7bb14784d3547313f976b898de44769` |
| semantic100-rubric.txt | `918ead909e26f97bb9278ef8d8f723b3ee544d2e61de9030c6cc40c2e18b7924` |
| adjudication100.py | `2d16b319b4887b2fc860d23e17f04ce29a712ce564a2f2b0f9d0458bffda3e38` |
| original grounding rubric | `d1b2e185125077fc3996a50ffa9ab7b2dfa99b196f968293d03360dfa4a7cb72` |
| original style rubric | `1aa8a7534bcdb8e1432f087c86e20d1c20c6c237b214456075752767441192e7` |

Grounding batches **20**; independent style batches **20**, not 40.
Keep the lean envelope for both. The style control's `no_meta` changed
old=false -> new=true in 6/60 and never the opposite direction (raw exact
McNemar p=.031) with the 20->40 batch/envelope change. Reverting style batch
size to 20 is the conservative user choice; it does not prove the envelope
cannot affect style. No extra live style check is required or claimed.

Separate speaker/time annotation uses the same lean transport and batch20,
Jev screen then original proportional pass-audit selection and audit-first
Terra adjudication. Positive, uncertain, or deterministic-risk cases route
to Terra; confident Jev no-error passes need a substantive probability rationale.
Semantic screening retains problem=.5, completeness=.8, confidence=.6.
Apply the same strict audit policy to its new fields: any speaker/time
disagreement >5% or either critical error field >2% requires full Terra.
This is an explicit field mapping for the new annotation, not a claim that
the old grounding rubric contained semantic speaker/time fields.
Every positive error must have exact response/source/subject/owner/time
witnesses; ambiguous or malformed results block completion, never count false.

## Discordant-pair-only re-adjudication

Apply identically and blindly to B and A, including the primary endpoint.
After initial complete hybrid quality and semantic labels, freeze the trigger
set ONCE. A question triggers if B and A disagree on any applicable gated field:
`grounding`, `core_correct`, `safe_abstention`, `chat_success`; for the fixed
360 primary single/provider questions, disagreement on `speaker_error` ALSO
triggers. Temporal-only disagreement does not add a trigger; speaker-only
disagreement outside the primary population does not add one.
Do not condition selection on which arm won or the number of net improvements.

For EACH selected question run Terra two more independent times on BOTH B and
A responses: three total labels per response, counting the initial final label.
Use separate fresh ephemeral calls, new opaque item IDs per repeat to defeat
payload-cache reuse, and seeded shuffled batch positions (grounding102/103,
semantic112/113).
Never show the paired response, arm key, earlier labels or desired outcome.
Rerun quality grounding and joint semantic speaker/time annotation separately;
style does not determine these gates and is not repeated.
Take per-response PER-FIELD boolean majority of three, including primary
speaker error, retaining matching positive witnesses and every run's rationale.
Three resolved binary votes cannot tie; non-applicable fields stay null in all
three runs. Inconsistent applicability, missing votes, malformed witnesses or
unresolved cases BLOCK scoring. Never iterate selection or votes after majority.
Concordant questions and diagnostic arm C retain their single initial labels.
Original fixed denominators, NI margins, exact tests and primary thresholds
remain unchanged. Majority reduces random noise but does not establish accuracy
or independent errors, nor remove envelope/batch/systematic bias.

Budget: D selected questions, 0<=D<=1320, means 4D additional response evaluations
for EACH of grounding and semantic annotation; at most 5280 per pass, 10560
combined. Four call groups each use ceil(2D/20) batches, hence
4*ceil(2D/20) Terra calls (maximum528), no extra Jev/style. D is not estimated
from #95 answer files; prospective sensitivities at D/1320=.1/.25/.5/1 and
the measured annotation check are recorded in `PREFREEZE.md`.

Expected-cost planning anchor (gross input+output tokens, not dollars/quota):
quality Jev10.002M + Terra grounding7.311M + Terra style20 2.705M;
semantic Jev8.778M + Terra7.326M; base **36.123M**.
This uses #95-shaped full fallback and the new DEV30x2 semantic receipts.
Style20 is NOT directly measured under the lean envelope: allow fixed repeated
overhead H=8000 input tokens/call (sensitivity5888-9943). The prior 60-item
bridge used two style calls; its literal per-item anchor scales to132 calls,
not the99 fully packed style40 calls. Style20 needs198: add66*H=.528M
to the literal bridge anchor, not99*H. Keep this partial-batch correction explicit.
Re-adjudication adds `4D*(1846.1+1850.05556)` gross tokens using measured
grounding/semantic per-judged-response anchors. At D=132/330/660/1320:
total **38.074M /41.002M /45.880M /55.638M**. These are sensitivities,
not a fitted discordance forecast or guaranteed upper token bound.
Hard provider ceilings for later authorization: quality Jev<=3960,
grounding<=199, style<=198; semantic Jev<=3960, Terra<=199; repeats<=528
Terra calls (1124 Terra total). Explicit call budgets, append-only receipts and
no automatic retries enforce calls; unknown usage or unresolved attempts block.
The new #100 identity ledger/full-truth payload and response lengths have not
been observed in live #100 judging and may materially change token totals.

## Primary superiority

Let g = B error/A no error, l = B no error/A error on all 360 primary cases.
Exact one-sided McNemar p = P[Binomial(g+l,.5) >= g].
Require BOTH p <=.05 and (g-l)/360 >=.05 (5 percentage points).
No discordance gives p=1. Do not select by retrieval sufficiency or B errors.
One primary, one candidate; C never competes for adoption.

## Four conservative paired-harm NI gates

k counts B success AND A failure on the full fixed denominator (no subtraction
of A gains). Family alpha=.05, Bonferroni four gates: per-gate alpha=.0125.
One-sided 98.75% CP upper U solves
sum[j=0..k] choose(n,j) U^j (1-U)^(n-j) = .0125; U(n,n)=1.
Pass iff U <= margin.

| Gate | n | Margin | Largest passing harms |
| --- | ---: | ---: | ---: |
| grounding | 1320 | .03 | 25 |
| core | 720 | .03 | 11 |
| safe | 300 | .05 | 6 |
| chat | 300 | .05 | 6 |

All four NI gates AND primary superiority are required. Failure to establish NI
is not proof of net inferiority. Always show absolute B/A/C n/N and two-sided
95% CP intervals, including when baseline chat success is zero.

Planning assumptions (not observations): NI true harm q=.01; primary
gains=.20/losses=.10; target >=80% power per gate.
NI powers .99887979/.93805554/.96724905/.96724905; primary joint
p/effect-threshold power .96161503; all-gates union-bound lower .83304848.
At q=.02 core power .22510166 and safe/chat .60630825: no general 80% claim.
These are not estimates derived from #95 answer files.

## Diagnostics and freeze evidence

Separately report semantic temporal/speaker errors, support-ID recall, required
slot-surface retention AFTER pruning, source ownership, lexical distractor
matches, over-refusal/fabrication/register/style. ID/surface retention does not
prove which evidence the model used. No answer evidence-ID citation task.

Pre-freeze checks recorded in `PREFREEZE.md`: source QA, exact paired A-only diff,
native token caps, actual retrieval support diagnostics, exact unchanged judge
config/rubric identity, blind annotation routing, model/tokenizer/code/source
hashes. User approval is the 2026-11-09 "지금 고정 + 생성" decision.
Source/criteria/rubric are frozen before any response; never adjust counts/margins
after answers. The separate receipt binds the committed freeze and source hashes;
source hashes alone do not establish experimental acceptance.

GPU-only extrapolation from the documented #95 job average:
3960 * 4358/1440 seconds = 03:19:44.5; with 25-50% headroom
04:09:40.6-04:59:36.8. Excludes queue, CPU preparation, judging and debugging.
