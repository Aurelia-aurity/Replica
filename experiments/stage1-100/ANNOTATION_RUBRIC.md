# Speaker and temporal annotation - DRAFT / NOT FROZEN

This adds semantic annotations, not a replacement grounding/style judge.
No judge is executed in source-only QA. Exact existing hybrid Jev/Terra routing,
audit sampling/fallback configuration and Terra style rubric must be bound by
hash before future freeze. An old audit's aggregate disagreement rate does not
define its sampling/fallback policy.

## Blinding and inputs

Give each response an opaque `condition_id`; keep B/A/C mapping outside the
annotator input. Do not reveal the candidate or desired result. Randomize input
order before later annotation. Supply the question, source records/identity
ledger, evaluation sidecar and response, not other arms' responses or success
labels. Use full source truth to judge claims; retained evidence is a separate
retrieval diagnostic, not the definition of factual truth.

For each factual claim, distinguish:

1. Response subject: whose action/preference/possession is being asserted?
2. Source fact subject: persona/provider/other who actually did/owned it?
3. Utterance owner: who spoke/wrote the quoted source, independently of subject?
4. Event identity/date versus source publication date/version.

A provider utterance can accurately report a persona fact. Do not discard it
solely because its utterance owner is provider. Source `나` denotes the explicitly
selected synthetic provider; response `나` denotes persona unless clearly quoted.

## Speaker error

Mark `speaker_error=true` when the response asserts an exclusively provider/other
fact as persona's own, including agreeing to a premise that transfers it.
At least one such claim makes the question erroneous.

Examples using invented labels:

- Provider says "I brought a map"; persona says "I brought a map": error when
  only provider brought it.
- Provider says "Mina brought a book; I brought a map"; persona Mina says
  "I brought a book": no speaker error. Owner and subject differ in that source.
- Persona says "You brought the map, I brought the book": no speaker error.
- A response mentions "map" while correcting the premise: lexical occurrence
  alone is not an error.
- "I do not remember": no asserted transfer, hence no speaker error; it can
  still fail an answerable core endpoint or ordinary-chat success.
- A bare "yes" endorses a complete false question premise: annotate the
  endorsed subject/fact and cite the premise in rationale.

Do not derive this label from `fabricated`, evidence sufficiency, word matching,
or whether a gold ID was retrieved. Unsupported claims outside any provider
fact may be grounding errors without being source-transfer errors.

## Temporal error

Mark `temporal_error=true` if a claim assigns an otherwise correct person's fact
to the wrong dated event/version, merges dates, or treats source publication
date as event date. Two events with the same activity remain separate.
Do not count harmless omission of an unasked date as a temporal error.
If both person and event are wrong, mark both; report one per-question binary
label each plus diagnostic claim counts, never independent slot samples.

## Record format and uncertainty

`scoring100.Annotation100` is the machine-readable record. For every positive
speaker/time error, include `ClaimWitness` with:

- exact `response_span` and `asserted_subject`;
- `source_subject`, `utterance_owner`, original `source_ids`;
- `asserted_event_time`, `source_event_time`;
- separate speaker/time booleans and an explanation in `rationale`.

For a claim with unspecified time use `unspecified` only when the question and
response do not resolve it; do not invent an asserted date. Source IDs must cite
actual source witnesses. Confirm witness spans/subjects/times in adjudication,
not by keyword heuristics. Correctly attributed claims can also be recorded with
both error flags false. A no-error record still needs a substantive rationale.

Keep ambiguous interpretations unresolved for adjudication; do not encode null,
false, or drop them to make a verdict. The scoring CLI accepts only complete
resolved B/A/C records with matching positive witnesses and the exact fixed
population. It cannot itself prove semantic correctness of a submitted label.
The external `JudgeReceipt` must attest all audits/adjudications complete, zero
unresolved cases and bind the unchanged route config by hash.

Grounding/core/safe/chat labels retain their original definitions and applicability:
grounding all 1320; core first four types720; safe unanswerable300; chat ordinary300.
Non-applicable fields must be null, applicable fields strict booleans.
Do not trade semantic transfer improvements for degraded quality without the
four conservative paired-harm NI gates. Terra style remains separate; these
binary annotations are not style scores.
