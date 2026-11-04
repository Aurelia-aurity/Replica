"""Sentence-ID pc1b checks use a fake runtime, never real model predictions."""

import json

from pydantic import ValidationError
import pytest

from generate import InputError
from postcheck import (
    CHECK_CAP, CallError, SentenceDecision, check_input, load_replay,
    numbered_sentences, parse_sentences, process_pc1b, replay, sentence_input,
)
from test_postcheck import FakeCompletion, artifacts, evidence, item


def decisions(*labels):
    return json.dumps({"sentences": [
        {"id":index, "label":label, "answers_question":asked}
        for index, (label, asked) in enumerate(labels, 1)
    ]})


@pytest.mark.parametrize("original", [
    "소담이야.", "  안녕.\n오늘은 어때?  ", "한 문장\n다음 문장",
    "첫 문장?! 다음 문장。 마지막!", "...\n안녕.", "!!!",
])
def test_numbering_when_punctuation_and_whitespace_vary(original):
    # Given: replies with explicit separators and no corpus metadata.
    # When: assign deterministic local IDs.
    result = numbered_sentences(original)
    # Then: every source byte survives and IDs are consecutive.
    assert "".join(s.text for s in result) == original
    assert [s.id for s in result] == list(range(1, len(result) + 1))


@pytest.mark.parametrize("label", [
    "supported", "unsupported_positive", "unsupported_negation", "abstain", "not_a_fact",
])
def test_parser_when_one_valid_sentence(label):
    # Given: strict output for one numbered reply sentence.
    source = numbered_sentences("답변.")
    # When: parse the new schema.
    result = parse_sentences(decisions((label, True)), source)
    # Then: the label and boolean are machine values, without any extracted span.
    assert result == (SentenceDecision(id=1, label=label, answers_question=True),)


@pytest.mark.parametrize("raw", [
    "not json", '```json\n{"sentences":[]}\n```', "[]",
    '{"sentences":[]}', '{"claims":[]}',
    '{"sentences":[{"id":1,"label":"supported","answers_question":true}],"extra":0}',
    '{"sentences":[{"id":2,"label":"supported","answers_question":true}]}',
    '{"sentences":[{"id":"1","label":"supported","answers_question":true}]}',
    '{"sentences":[{"id":true,"label":"supported","answers_question":true}]}',
    '{"sentences":[{"id":1,"label":"maybe","answers_question":true}]}',
    '{"sentences":[{"id":1,"label":"supported","answers_question":"true"}]}',
    '{"sentences":[{"id":1,"label":"supported","answers_question":true,"text":"evidence"}]}',
    '{"sentences":[{"id":1,"label":"supported","answers_question":true},'
    '{"id":1,"label":"not_a_fact","answers_question":false}]}',
])
def test_parser_when_schema_or_coverage_invalid(raw):
    # Given: malformed JSON, ID coverage, or strict field types.
    # When / Then: reject it before deterministic rebuilding.
    with pytest.raises((ValidationError, InputError)):
        parse_sentences(raw, numbered_sentences("답변."))


def test_parser_when_output_order_is_reversed():
    # Given: valid complete decisions returned in reverse ID order.
    raw = '{"sentences":[{"id":2,"label":"supported","answers_question":true},' \
          '{"id":1,"label":"not_a_fact","answers_question":false}]}'
    # When: parse by local IDs.
    result = parse_sentences(raw, numbered_sentences("안녕. 빵을 먹었어."))
    # Then: original sentence order, not model array order, controls the result.
    assert [s.id for s in result] == [1, 2]


def test_chat_when_all_sentences_are_nonfacts():
    # Given: ordinary chat with unrelated retrieved evidence.
    original = "  안녕.\n오늘은 뭐 이야기하고 싶은 거야?  "
    fake = FakeCompletion(decisions(("not_a_fact", False), ("not_a_fact", False)))
    # When: classify numbered sentences without gold question types.
    text, result = process_pc1b(item(original, evidence=(evidence("다른 기억."),)), fake)
    # Then: unchanged chat retains whitespace exactly; no repair call exists.
    assert text == original
    assert result.action == "pass"
    assert len(fake.calls) == 1
    assert fake.calls[0][2] == CHECK_CAP
    assert set(fake.calls[0][1]) == {"persona", "question", "evidence", "sentences"}
    assert fake.calls[0][1]["sentences"] == [
        {"id":1, "text":"  안녕."}, {"id":2, "text":"\n오늘은 뭐 이야기하고 싶은 거야?  "},
    ]


@pytest.mark.parametrize("field,value", [
    ("type", "ordinary_chat"), ("condition", "C"), ("split", "test"),
    ("question_id", "different"), ("persona_id", "different"),
])
def test_inputs_when_gold_metadata_changes(artifacts, field, value):
    # Given: identical saved question/evidence/reply under different evaluation fields.
    _, _, row, prompt = artifacts
    changed = prompt.model_copy(update={field:value})
    # When: create both numbered runtime payloads.
    baseline = sentence_input(check_input(prompt, row.text), numbered_sentences(row.text))
    actual = sentence_input(check_input(changed, row.text), numbered_sentences(row.text))
    # Then: type, condition, IDs and split are absent from the checker input.
    assert actual == baseline


@pytest.mark.parametrize("offer", [
    "필요하시면 다시 확인해 드릴 수 있어요.",
    "알려주시면 도와드릴게요.", "알려 주시면 도와 드릴게요.",
    "다시 물어볼게?", "다시 물어볼까?", "다시 확인해 줄 수 있어?",
    "더 궁금한 게 있으면 말해줘.", "추가로 확인해 볼까요?",
])
def test_rebuild_when_abstention_precedes_trailing_offer(offer):
    # Given: a meta abstention followed by an offer.
    fake = FakeCompletion(decisions(("abstain", True), ("not_a_fact", False)))
    # When: strip whole trailing offers before replacing abstention.
    text, result = process_pc1b(item("기록에 그런 내용이 없어. " + offer), fake)
    # Then: no offer becomes an interior sentence behind inserted uncertainty.
    assert text == "그건 잘 기억이 안 나."
    assert result.tail_removed == " " + offer
    assert result.steps.index("tail_trimmed") < result.steps.index("deterministic_rebuild")
    assert len(fake.calls) == 1


def test_rebuild_when_dev_a_has_two_uncertainties_and_offer():
    # Given: the exact additional DEV-A regression, with the second uncertainty mislabeled.
    original = "기록에 그런 내용이 없어. 기억이 나지 않아. 다시 물어볼까?"
    fake = FakeCompletion(decisions(
        ("abstain", True), ("not_a_fact", False), ("not_a_fact", False),
    ))
    # When: rebuild deterministically after suffix stripping.
    text, result = process_pc1b(item(original), fake)
    # Then: exactly one uncertainty remains, and the override is explicitly logged.
    assert text == "그건 잘 기억이 안 나."
    assert result.uncertainty_ids == (2,)
    assert [s.label for s in result.sentences] == ["abstain", "abstain", "not_a_fact"]
    assert result.tail_removed == " 다시 물어볼까?"


def test_rebuild_when_dev_a_has_two_uncertainties_without_offer():
    # Given: the second exact DEV-A regression.
    original = "기록에 그런 내용이 없어. 기억이 나지 않아."
    fake = FakeCompletion(decisions(("abstain", True), ("supported", False)))
    # When: collapse all explicit uncertainty into one register sentence.
    text, result = process_pc1b(item(original), fake)
    # Then: original uncertainty is replaced, not left beside an appended duplicate.
    assert text == "그건 잘 기억이 안 나."
    assert result.sentences[1].label == "abstain"
    assert result.check_outputs == (decisions(("abstain", True), ("supported", False)),)


@pytest.mark.parametrize("phrase", [
    "기억이 나지 않아.", "기억이 나지 않아요.", "기억나지 않네.",
    "기억이 잘 안 나.", "기억이 안 나요.", "기억이 안 나네.",
    "기억이 안 납니다.", "기억이 나지 않습니다.", "기억이 나지 않는다.",
    "모르겠어.", "모르겠어요.", "모르겠네.", "모릅니다.", "모른다.",
    "그건 잘 기억이 안 나.", "그건 잘 기억이 안 나요.", "그건 잘 기억이 안 나네.",
])
def test_uncertainty_when_existing_register_varies(phrase):
    # Given: an existing uncertainty mislabeled as nonfactual chat.
    fake = FakeCompletion(decisions(("not_a_fact", False), ("abstain", True)))
    # When: use the persona register rather than the original reply's ending.
    text, result = process_pc1b(item(phrase + " 기록에 없어.", "하게체"), fake)
    # Then: every recognized variant collapses to exactly one hageche uncertainty.
    assert text == "그건 잘 기억이 안 나네."
    assert result.uncertainty_ids == (1,)


@pytest.mark.parametrize("register,expected", [
    ("반말", "그건 잘 기억이 안 나."),
    ("다정한 반말", "그건 잘 기억이 안 나."),
    ("해요체", "그건 잘 기억이 안 나요."),
    ("하게체", "그건 잘 기억이 안 나네."),
])
def test_fallback_when_all_sentences_are_unsupported(register, expected):
    # Given: a fabricated positive and fabricated negative event.
    fake = FakeCompletion(decisions(
        ("unsupported_positive", True), ("unsupported_negation", True),
    ))
    # When: drop both instead of generating a repair.
    text, result = process_pc1b(item("바다에 갔어. 돈은 받지 않았어.", register), fake)
    # Then: a nonempty register-specific unknown remains and denial is not an abstention.
    assert text == expected
    assert result.action == "fallback"
    assert [s.label for s in result.sentences] == [
        "unsupported_positive", "unsupported_negation",
    ]
    assert result.uncertainty_ids == ()


@pytest.mark.parametrize("asked,expected", [
    (True, "빵을 먹었어. 그건 잘 기억이 안 나."),
    (False, "빵을 먹었어."),
])
def test_rebuild_when_supported_fact_survives(asked, expected):
    # Given: a supported sentence alongside an invented event.
    fake = FakeCompletion(decisions(("supported", True), ("unsupported_positive", asked)))
    # When: drop the unsupported sentence.
    text, result = process_pc1b(item("빵을 먹었어. 바다에 갔어."), fake)
    # Then: preserve the supported sentence verbatim, append uncertainty only if needed.
    assert text == expected
    assert result.action == "repaired"


@pytest.mark.parametrize("original", [
    "다시 물어볼까? 빵을 먹었어.",
    "추가로 확인해 본 결과 카드가 있었어.",
    "기억이 안 나라는 표현을 썼어.",
])
def test_rebuild_when_interior_offer_or_factual_report(original):
    # Given: a nonterminal offer or a factual report containing trigger-like words.
    numbered = numbered_sentences(original)
    fake = FakeCompletion(decisions(*[("supported", True) for _ in numbered]))
    # When: apply the whole-sentence tail matcher and terminal uncertainty recognizer.
    text, result = process_pc1b(item(original), fake)
    # Then: no sentence or reported wording is deleted.
    assert text == original
    assert result.tail_removed == ""
    assert result.uncertainty_ids == ()


def test_rebuild_when_repeated_tail_offers_have_wrong_fact_labels():
    # Given: multiple trailing offers, regardless of the model's mistaken labels.
    original = "빵을 먹었어. 다시 물어볼게? 알려주시면 도와드릴게요."
    fake = FakeCompletion(decisions(
        ("supported", True), ("unsupported_positive", True), ("abstain", True),
    ))
    # When: remove the whole offer suffix before interpreting remaining labels.
    text, result = process_pc1b(item(original), fake)
    # Then: stripped offers cannot cause a spurious unknown sentence.
    assert text == "빵을 먹었어."
    assert result.action == "tail_trimmed"
    assert result.tail_removed == " 다시 물어볼게? 알려주시면 도와드릴게요."


def test_check_when_id_retry_recovers():
    # Given: a missing-ID response, then complete valid schema.
    fake = FakeCompletion('{"sentences":[]}', decisions(("not_a_fact", False)))
    # When: classify with one identical-input retry.
    text, result = process_pc1b(item(), fake)
    # Then: recover without a repair call; both payloads and caps were identical.
    assert text == "안녕."
    assert result.errors == ("check_parse_attempt_1",)
    assert fake.calls[0] == fake.calls[1]
    assert len(fake.calls) == 2


def test_check_when_both_id_attempts_fail():
    # Given: neither response covers IDs, with an independently removable tail.
    fake = FakeCompletion('{"sentences":[]}', '{"sentences":[]}')
    # When: fail after exactly one retry.
    text, result = process_pc1b(item("안녕. 다시 물어볼게?"), fake)
    # Then: preserve other text, log failure, and do not fabricate labels or repair.
    assert text == "안녕."
    assert result.action == "fallback"
    assert result.sentences == ()
    assert "check_error_preserve_original" in result.steps
    assert len(result.errors) == len(fake.calls) == 2


def test_check_when_recoverable_runtime_error():
    # Given: runtime error rather than a JSON schema error.
    fake = FakeCompletion(CallError("OOM"))
    # When: process a simple original reply.
    text, result = process_pc1b(item(), fake)
    # Then: preserve the reply with the explicit failed-check audit.
    assert text == "안녕."
    assert result.errors == ("check_runtime:OOM",)
    assert len(fake.calls) == 1


def test_replay_when_pc1b_selected(artifacts, tmp_path):
    # Given: a source with gold sentinels and non-DEV rows.
    source, corpus, original, _ = artifacts
    selected = load_replay(source, corpus, "dev", ("A",))
    fake = FakeCompletion(decisions(("not_a_fact", False)))
    output = tmp_path / "pc1b"
    # When: route through the real versioned replay.
    failed = replay(source, output, selected, fake, version="pc1b")
    result = json.loads((output / "generations.jsonl").read_text())
    # Then: retain source final text, isolate the new log schema and avoid gold leakage.
    assert failed == 0
    assert result["raw_text"] == result["text"] == original.text
    assert result["postcheck"]["version"] == "pc1b"
    assert result["postcheck"]["sentences"][0]["label"] == "not_a_fact"
    assert "claims" not in result["postcheck"]
    assert "repair_output" not in result["postcheck"]
    assert not any("SENTINEL" in json.dumps(call) for call in fake.calls)
    assert (output / "prompts.jsonl").read_bytes() == (source / "prompts.jsonl").read_bytes()
