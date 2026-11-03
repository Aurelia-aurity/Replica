"""pc1c rebuild tests reuse frozen fake labels; no model or GPU is required."""

import builtins
import json
import os
import subprocess
import sys

import pytest
import typer
from typer.testing import CliRunner

from corpus import ROOT
from generate import InputError
from postcheck import (
    CheckedGeneration, SENTENCE_CHECK_SYSTEM, load_replay, main, numbered_sentences,
    process_pc1b, rebuild_pc1c, rebuild_saved, replay,
)
from test_postcheck import FakeCompletion, artifacts, item
from test_postcheck_b import decisions


@pytest.mark.parametrize("dropped", ["abstain", "unsupported_positive", "unsupported_negation"])
@pytest.mark.parametrize("supported_answers", [True, False])
@pytest.mark.parametrize("dropped_answers", [True, False])
def test_uncertainty_when_answer_coverage_varies(dropped, supported_answers, dropped_answers):
    # Given: a removed sentence followed by a retained supported sentence.
    original = item("모르는 부분이야. 빵을 먹었어.")
    fake = FakeCompletion(decisions((dropped, dropped_answers), ("supported", supported_answers)))
    _, checked = process_pc1b(original, fake)
    # When: rebuild without changing the saved labels.
    text, result = rebuild_pc1c(original, checked)
    # Then: uncertainty is terminal, and only needed with no supported answering sentence.
    expected = "빵을 먹었어."
    if dropped_answers and not supported_answers:
        expected += " 그건 잘 기억이 안 나."
    assert text == expected
    assert result.sentences == checked.sentences
    assert result.check_outputs == checked.check_outputs
    assert len(fake.calls) == 1


def test_uncertainty_when_only_chat_precedes_missing_answer():
    # Given: duplicate abstentions and a surviving conversational sentence.
    original = item("기록이 없어. 안녕. 기억이 나지 않아.")
    fake = FakeCompletion(decisions(
        ("abstain", True), ("not_a_fact", False), ("abstain", True),
    ))
    _, checked = process_pc1b(original, fake)
    # When: rebuild v2.
    text, result = rebuild_pc1c(original, checked)
    # Then: emit exactly one uncertainty at the end, not at the first abstention.
    assert text == "안녕. 그건 잘 기억이 안 나."
    assert result.version == "pc1c"
    assert "uncertainty_appended" in result.steps


def test_uncertainty_when_meta_abstention_precedes_supported_answer():
    # Given: the reported ordering shape, with a supported answering correction.
    original = item("기록에 그런 내용은 없어. 혼자 간 건 아니야. 같이 갔어.")
    fake = FakeCompletion(decisions(
        ("abstain", False), ("supported", True), ("supported", True),
    ))
    _, checked = process_pc1b(original, fake)
    # When: rebuild without a new model call.
    text, result = rebuild_pc1c(original, checked)
    # Then: keep supported sentences verbatim and drop the contradictory meta disclaimer.
    assert text == "혼자 간 건 아니야. 같이 갔어."
    assert "uncertainty_appended" not in result.steps


@pytest.mark.parametrize("removed_label", [
    "abstain", "unsupported_positive", "unsupported_negation",
])
@pytest.mark.parametrize("question", [
    "혹시 다른 사람한테 물어봤어?",
    "혹시 그날 있었던 일에 대해 더 자세히 말해줄 수 있어?",
    "필요하면 더 말해줘?",
])
def test_question_only_when_unreliable_content_was_removed(removed_label, question):
    # Given: a removed nonanswering sentence leaves only a provider question.
    value = item("기록에 없어. " + question)
    fake = FakeCompletion(decisions((removed_label, False), ("not_a_fact", False)))
    _, checked = process_pc1b(value, fake)
    # When: apply the post-removal question guard.
    text, result = rebuild_pc1c(value, checked)
    # Then: do not leave a question-only response even when the answer flag was false.
    assert text == "그건 잘 기억이 안 나."
    assert result.action == "fallback"
    assert "provider_questions_replaced" in result.steps
    assert result.sentences == checked.sentences
    assert len(fake.calls) == 1


@pytest.mark.parametrize("register,expected", [
    ("반말", "그건 잘 기억이 안 나."),
    ("다정한 반말", "그건 잘 기억이 안 나."),
    ("해요체", "그건 잘 기억이 안 나요."),
    ("하게체", "그건 잘 기억이 안 나네."),
])
def test_question_only_when_multiple_questions_and_register_vary(register, expected):
    # Given: two provider questions survive removal of an abstention.
    value = item("기록에 없어. 어디야? 같이 갔어？", register)
    fake = FakeCompletion(decisions(
        ("abstain", True), ("not_a_fact", False), ("not_a_fact", False),
    ))
    _, checked = process_pc1b(value, fake)
    # When: recognize both ASCII and full-width terminal question marks.
    text, result = rebuild_pc1c(value, checked)
    # Then: emit one register-specific uncertainty rather than several questions.
    assert text == expected
    assert result.action == "fallback"


@pytest.mark.parametrize("removed_label", ["abstain", "unsupported_negation"])
@pytest.mark.parametrize("supported_answers", [True, False])
def test_trailing_question_when_supported_content_survives(removed_label, supported_answers):
    # Given: removed content precedes a supported sentence and repeated provider questions.
    value = item("기록에 없어. 빵을 먹었어. 혹시 다른 내용이 궁금해? 더 말해줄까?")
    fake = FakeCompletion(decisions(
        (removed_label, False), ("supported", supported_answers),
        ("not_a_fact", False), ("not_a_fact", False),
    ))
    _, checked = process_pc1b(value, fake)
    # When: trim only the terminal provider questions after removed content.
    text, result = rebuild_pc1c(value, checked)
    # Then: keep supported text verbatim, even when its answer flag is false.
    assert text == "빵을 먹었어."
    assert result.action == "repaired"
    assert "provider_questions_trimmed:3,4" in result.steps
    assert result.sentences == checked.sentences


@pytest.mark.parametrize("original", [
    "혹시 다른 사람한테 물어봤어?",
    "안녕. 오늘은 어떤 이야기를 할까?",
    "필요하면 더 말해줘?",
])
def test_ordinary_chat_when_no_semantic_sentence_removed(original):
    # Given: fact-free chat with no removed abstention or unsupported sentence.
    fake = FakeCompletion(decisions(*[
        ("not_a_fact", False) for _ in numbered_sentences(original)
    ]))
    value = item(original)
    _, checked = process_pc1b(value, fake)
    # When: apply pc1c's conditional question guard.
    text, result = rebuild_pc1c(value, checked)
    # Then: question-only ordinary chat is unchanged, not turned into memory absence.
    assert text == original
    assert result.action == "pass"
    assert not any(s.startswith("provider_questions_") for s in result.steps)


def test_questions_when_remaining_nonfact_content_is_not_only_questions():
    # Given: no supported fact, but a greeting remains alongside a question.
    value = item("기록에 없어. 안녕. 다른 내용이 궁금해?")
    fake = FakeCompletion(decisions(
        ("abstain", False), ("not_a_fact", False), ("not_a_fact", False),
    ))
    _, checked = process_pc1b(value, fake)
    # When: apply the strictly question-only replacement condition.
    text, result = rebuild_pc1c(value, checked)
    # Then: mixed nonfact content is not broadly discarded.
    assert text == "안녕. 다른 내용이 궁금해?"
    assert "provider_questions_replaced" not in result.steps


def test_trailing_question_when_it_precedes_removed_content():
    # Given: the question came before, not after, the removed sentence.
    value = item("빵을 먹었어. 궁금해? 기록에 없어.")
    fake = FakeCompletion(decisions(
        ("supported", True), ("not_a_fact", False), ("abstain", False),
    ))
    _, checked = process_pc1b(value, fake)
    # When: check source sentence order as well as the rebuilt suffix.
    text, result = rebuild_pc1c(value, checked)
    # Then: the narrower follows-removed-content rule does not delete it.
    assert text == "빵을 먹었어. 궁금해?"
    assert not any(s.startswith("provider_questions_trimmed:") for s in result.steps)


def test_supported_question_when_it_is_not_a_provider_nonfact():
    # Given: a question sentence itself has a supported label.
    value = item("기록에 없어. 같이 갔어?")
    fake = FakeCompletion(decisions(("abstain", False), ("supported", True)))
    _, checked = process_pc1b(value, fake)
    # When: apply the provider-question guard without rejudging saved labels.
    text, result = rebuild_pc1c(value, checked)
    # Then: do not delete supported content just because its punctuation is a question mark.
    assert text == "같이 갔어?"
    assert result.sentences == checked.sentences


@pytest.mark.parametrize("offer", [
    "알려줄 수 있어요?", "알려 줄 수 있어?", "또 물어볼게?",
    "필요하면 다시 확인해줄게?", "필요하면, 다시 확인해 줄게요?",
    "필요하시면 다시 확인해드릴게요.",
    "다른 자료에서 확인해 보길 권해.",
    "회사나 제품명을 알고 싶다면, 다른 자료에서 확인해 보길 권해.",
    "다른 자료에서 확인해 보세요.", "추가로 확인해 보세요.",
])
def test_tail_when_whole_terminal_offer_or_advice(offer):
    # Given: an answering fact, meta abstention, and terminal offer/advice.
    original = item("빵을 먹었어. 기록에 없어. " + offer)
    fake = FakeCompletion(decisions(
        ("supported", True), ("abstain", True), ("not_a_fact", False),
    ))
    _, checked = process_pc1b(original, fake)
    # When: match whole original terminal sentences.
    text, result = rebuild_pc1c(original, checked)
    # Then: remove the whole suffix before suppressing redundant uncertainty.
    assert text == "빵을 먹었어."
    assert result.tail_removed == " " + offer


@pytest.mark.parametrize("original", [
    "추가로 확인해 본 결과 카드가 있었어.",
    "더 궁금한 게 있으면이라는 표현을 썼어.",
    "'다른 자료에서 확인해 보세요'라고 썼어.",
    "다른 자료에서 확인해 보세요라는 문구를 봤어.",
    "다른 자료에서 확인해 보길 권해 달라고 했어.",
    '"알려줄 수 있어요?"라고 물었어.',
    "알려줄 수 있어요? 빵을 먹었어.",
    "다른 자료에서 확인해 보세요. 빵을 먹었어.",
])
def test_tail_when_quoted_retrospective_or_interior(original):
    # Given: trigger words in reported wording, factual text, or an interior offer.
    fake = FakeCompletion(decisions(*[("supported", True) for _ in numbered_sentences(original)]))
    value = item(original)
    _, checked = process_pc1b(value, fake)
    # When: apply v2 end-anchored full-sentence patterns.
    text, result = rebuild_pc1c(value, checked)
    # Then: preserve every original byte and no false tail log.
    assert text == original
    assert result.tail_removed == ""
    assert result.action == "pass"


@pytest.mark.parametrize("register,expected", [
    ("반말", "그건 잘 기억이 안 나."),
    ("다정한 반말", "그건 잘 기억이 안 나."),
    ("해요체", "그건 잘 기억이 안 나요."),
    ("하게체", "그건 잘 기억이 안 나네."),
])
def test_fallback_when_all_content_and_offers_removed(register, expected):
    # Given: a nonanswering meta abstention with nothing useful left.
    value = item("기록에 없어. 알려줄 수 있어요?", register)
    fake = FakeCompletion(decisions(("abstain", False), ("not_a_fact", False)))
    _, checked = process_pc1b(value, fake)
    # When: remove the meta sentence and terminal offer.
    text, result = rebuild_pc1c(value, checked)
    # Then: retain the nonempty fallback invariant even without an answer flag.
    assert text == expected
    assert result.action == "fallback"


def test_online_when_pc1c_reuses_exact_pc1b_check():
    # Given: one original and identical scripted checker decisions for both versions.
    value = item("기록에 없어. 빵을 먹었어.")
    raw = decisions(("abstain", True), ("supported", True))
    fake = FakeCompletion(raw)
    _, checked = process_pc1b(value, fake)
    # When: apply the pc1c rebuild.
    text, result = rebuild_pc1c(value, checked)
    # Then: inference input/schema is unchanged and no repair/check call is added.
    assert text == "빵을 먹었어."
    assert result.check_outputs == (raw,)
    assert fake.calls[0][0] == SENTENCE_CHECK_SYSTEM
    assert len(fake.calls) == 1


@pytest.fixture
def saved_b(artifacts, tmp_path):
    source, corpus, _, _ = artifacts
    directory = tmp_path / "saved-b"
    replay(
        source, directory, load_replay(source, corpus, "dev", ("A",)),
        FakeCompletion(decisions(("not_a_fact", False))), version="pc1b",
    )
    (directory / "environment.json").write_text(json.dumps({
        "postcheck_version":"pc1b", "check_system":SENTENCE_CHECK_SYSTEM,
    }), encoding="utf-8")
    return directory


def test_offline_when_cli_runs_without_runtime_import(saved_b, tmp_path, monkeypatch):
    # Given: valid saved labels and an import guard against any model runtime.
    imported = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name in {"runtime", "torch", "transformers"}:
            raise AssertionError(f"offline imported {name}")
        return imported(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    output = tmp_path / "rebuilt"
    app = typer.Typer()
    app.command()(main)
    # When: execute the real offline CLI, without source/corpus/model options.
    result = CliRunner().invoke(app, [
        "--rebuild-from", str(saved_b), "--version", "pc1c", "--output", str(output),
    ])
    # Then: raw text, saved labels and original check outputs survive unchanged.
    assert result.exit_code == 0, result.output
    before = CheckedGeneration.model_validate_json((saved_b / "generations.jsonl").read_text())
    after = CheckedGeneration.model_validate_json((output / "generations.jsonl").read_text())
    assert before.raw_text == after.raw_text
    assert before.postcheck.sentences == after.postcheck.sentences
    assert before.postcheck.check_outputs == after.postcheck.check_outputs
    assert after.postcheck.version == "pc1c"
    assert (output / "prompts.jsonl").read_bytes() == (saved_b / "prompts.jsonl").read_bytes()
    env = json.loads((output / "environment.json").read_text())
    assert env["model_calls"] == 0
    assert env["check_version"] == "pc1b"
    assert env["rebuild_version"] == 2
    assert env["action_counts"] == {"A":{"pass":1}}


def test_offline_when_cpu_only_subprocess(saved_b, tmp_path):
    # Given: a new Python process without a Slurm allocation.
    output = tmp_path / "subprocess-output"
    env = {k:v for k,v in os.environ.items() if k != "SLURM_JOB_ID"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # When: invoke the real script rather than the Python entry function.
    result = subprocess.run([
        sys.executable, str(ROOT / "postcheck.py"),
        "--rebuild-from", str(saved_b), "--version", "pc1c", "--output", str(output),
    ], env=env, capture_output=True, text=True, check=False)
    # Then: actual CLI succeeds without constructing a model runtime.
    assert result.returncode == 0, result.stderr
    assert "model_calls=0" in result.stdout
    assert json.loads((output / "environment.json").read_text())["rows"] == 1


@pytest.mark.parametrize("mutation", [
    "version", "failed", "numbering", "coverage", "duplicate",
    "missing_prompt", "prompt_scope", "environment",
])
def test_offline_when_saved_input_is_invalid(saved_b, tmp_path, mutation):
    # Given: one corrupted saved artifact pair.
    generation = saved_b / "generations.jsonl"
    payload = json.loads(generation.read_text())
    match mutation:
        case "version":
            payload["postcheck"]["version"] = "pc1c"
        case "failed":
            payload["postcheck"]["steps"].append("check_error_preserve_original")
        case "numbering":
            payload["postcheck"]["numbered_sentences"][0]["text"] = "tampered"
        case "coverage":
            payload["postcheck"]["sentences"] = []
        case "duplicate":
            generation.write_text(json.dumps(payload) + "\n" + json.dumps(payload), encoding="utf-8")
        case "missing_prompt":
            (saved_b / "prompts.jsonl").write_text("", encoding="utf-8")
        case "prompt_scope":
            path = saved_b / "prompts.jsonl"
            prompt = json.loads(path.read_text())
            prompt["persona_id"] = "foreign"
            path.write_text(json.dumps(prompt), encoding="utf-8")
        case "environment":
            (saved_b / "environment.json").write_text('{"postcheck_version":"pc1"}', encoding="utf-8")
    if mutation in {"version", "failed", "numbering", "coverage"}:
        generation.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "invalid-output"
    # When / Then: reject before creating any output or importing the runtime.
    with pytest.raises(InputError):
        rebuild_saved(saved_b, output, "dev", ())
    assert not output.exists()


def test_offline_when_output_already_exists(saved_b, tmp_path):
    # Given: a pre-existing directory with user data.
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "marker"
    marker.write_text("preserve", encoding="utf-8")
    # When / Then: never overwrite an existing run.
    with pytest.raises(FileExistsError):
        rebuild_saved(saved_b, output, "dev", ())
    assert marker.read_text() == "preserve"
