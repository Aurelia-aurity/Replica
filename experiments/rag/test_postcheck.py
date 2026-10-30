"""pc1 boundary and replay tests use only synthetic fixtures and a fake runtime."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

from pydantic import ValidationError
import pytest
import typer
from typer.testing import CliRunner

from corpus import ROOT, Evidence
from generate import Generation, InputError, Message, Prompt
from postcheck import (
    CHECK_CAP, REPAIR_CAP, CallError, CheckInput, Claim, SavedPersona,
    check_input, fallback, load_replay, main, parse_claims, process, repair_errors,
    replay, trim_tail,
)


class FakeCompletion:
    """A scripted model seam; records exactly what the real runtime receives."""

    def __init__(self, *outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def __call__(self, system, user, cap):
        self.calls.append((system, json.loads(user), cap))
        value = next(self.outputs)
        if isinstance(value, CallError):
            raise value
        return value


def item(reply="안녕.", register="반말", evidence=()):
    return CheckInput(
        persona=SavedPersona(
            name="가상", display_name="친구", provider_address=None,
            register=register, relationship="친구",
        ),
        question="잠깐 이야기하자.", evidence=evidence, reply=reply,
    )


def claim(span, label="unsupported_positive", asked=True):
    return Claim(span=span, label=label, answers_question=asked)


def answer(*claims):
    return json.dumps({"claims": [c.model_dump() for c in claims]}, ensure_ascii=False)


def evidence(content):
    return Evidence(
        chunk_id=UUID(int=1), record_id=UUID(int=2), text_version_id=UUID(int=3),
        content=content, speaker_role="persona",
    )


@pytest.mark.parametrize("label", [
    "supported", "unsupported_positive", "unsupported_negation", "not_a_fact",
])
def test_parser_when_valid_exact_span(label):
    # Given: one schema-conforming decision.
    raw = answer(claim("답변.", label))
    # When: parse strict model JSON.
    parsed = parse_claims(raw, "답변.")
    # Then: the machine fields survive without reinterpretation.
    assert parsed == (claim("답변.", label),)


@pytest.mark.parametrize("raw", [
    "not json", '```json\n{"claims":[]}\n```', "[]",
    '{"claims":[],"gold":"leak"}',
    '{"claims":[{"span":"없는 문장","label":"supported","answers_question":true}]}',
    '{"claims":[{"span":"","label":"supported","answers_question":true}]}',
    '{"claims":[{"span":"답변.","label":"maybe","answers_question":true}]}',
    '{"claims":[{"span":"답변.","label":"supported","answers_question":"true"}]}',
    '{"claims":[{"span":"답변.","label":"supported"}]}',
    answer(claim("답변."), claim("답변.")),
    answer(claim("답변."), claim("답변", "supported")),
])
def test_parser_when_invalid_output(raw):
    # Given: malformed JSON, schema, or spans.
    # When / Then: reject before any repair can target a fabricated span.
    with pytest.raises((ValidationError, InputError)):
        parse_claims(raw, "답변.")


def test_check_when_json_retry_recovers():
    # Given: the first response is malformed and the retry is valid.
    fake = FakeCompletion("bad", answer())
    # When: run the checker.
    text, result = process(item(), fake)
    # Then: retry exactly once, preserve chat, retain the failed attempt as evidence.
    assert text == "안녕."
    assert len(fake.calls) == 2
    assert result.action == "pass"
    assert result.errors == ("check_parse_attempt_1",)
    assert result.check_outputs == ("bad", answer())


def test_check_when_both_json_attempts_fail():
    # Given: neither model output parses.
    fake = FakeCompletion("bad", "bad")
    # When: process an ordinary reply.
    text, result = process(item(), fake)
    # Then: preserve original, explicitly fail the check, never invoke a repair.
    assert text == "안녕."
    assert len(fake.calls) == 2
    assert result.action == "fallback"
    assert "check_error_preserve_original" in result.steps
    assert result.errors == ("check_parse_attempt_1", "check_parse_attempt_2")


def test_chat_when_no_personal_claims():
    # Given: chat has no fact claims, even with retrieved unrelated evidence.
    original = "안녕하세요. 잠깐 이야기해요."
    fake = FakeCompletion(answer())
    # When: use question/evidence/reply, with no gold type route.
    text, result = process(item(original, "해요체", (evidence("다른 기억."),)), fake)
    # Then: every original byte survives and repair is not called.
    assert text == original
    assert result.action == "pass"
    assert len(fake.calls) == 1
    assert set(fake.calls[0][1]) == {"persona", "question", "evidence", "reply"}


@pytest.mark.parametrize("candidate,error", [
    ("", "repair_empty"),
    ("바다에 갔어.", "repair_new_content_words"),
    ("빵을 먹었어. 비행기를 탔어.", "repair_retained_unsupported_span"),
    ("먹었어.", "repair_dropped_supported_span"),
])
def test_repair_validation_when_candidate_breaks_constraint(candidate, error):
    # Given: one supported and one unsupported sentence.
    original = item("빵을 먹었어. 비행기를 탔어.")
    claims = (claim("빵을 먹었어.", "supported"), claim("비행기를 탔어."))
    # When: validate the second call.
    errors = repair_errors(candidate, original, claims)
    # Then: deterministic validation reports the precise broken constraint.
    assert error in errors


def test_repair_when_only_flagged_claim_removed():
    # Given: a supported sentence beside an invented peripheral claim.
    claims = (claim("빵을 먹었어.", "supported"), claim("비행기를 탔어.", asked=False))
    fake = FakeCompletion(answer(*claims), "빵을 먹었어.")
    # When: repair once.
    text, result = process(item("빵을 먹었어. 비행기를 탔어."), fake)
    # Then: supported words are unchanged and only two calls occurred.
    assert text == "빵을 먹었어."
    assert result.action == "repaired"
    assert result.errors == ()
    assert [c[2] for c in fake.calls] == [CHECK_CAP, REPAIR_CAP]
    assert fake.calls[1][1]["unsupported_spans"] == ["비행기를 탔어."]
    assert fake.calls[1][1]["supported_spans"] == ["빵을 먹었어."]


def test_repair_validation_when_new_word_occurs_only_in_evidence():
    # Given: the evidence contains a word not in the original reply.
    original = item("빵을 먹었어. 탔어.", evidence=(evidence("과자를 먹었어."),))
    # When: check the specified original-plus-evidence lexical boundary.
    errors = repair_errors("과자를 먹었어.", original, (claim("탔어.", asked=False),))
    # Then: source content words, but not evidence IDs, are permitted.
    assert errors == ()
    assert "repair_new_content_words" in repair_errors("000000000001", original, ())


@pytest.mark.parametrize("register,expected", [
    ("반말", "그건 잘 기억이 안 나."),
    ("다정한 반말", "그건 잘 기억이 안 나."),
    ("해요체", "그건 잘 기억이 안 나요."),
    ("하게체", "그건 잘 기억이 안 나네."),
])
def test_fallback_when_repair_adds_content(register, expected):
    # Given: no supported content remains and repair introduces a new place.
    fake = FakeCompletion(answer(claim("간 적 없어.", "unsupported_negation")), "바다야.")
    # When: validate then fall back deterministically.
    text, result = process(item("간 적 없어.", register), fake)
    # Then: each register gets its fixed uncertainty, never an event negation.
    assert text == expected
    assert result.action == "fallback"
    assert result.errors == ("repair_new_content_words", "repair_missing_uncertainty")


def test_repair_when_asked_part_removed_without_uncertainty():
    # Given: an unsupported answer beside a supported fact.
    original = item("빵을 먹었어. 비행기를 탔어.")
    claims = (claim("빵을 먹었어.", "supported"), claim("비행기를 탔어."))
    fake = FakeCompletion(answer(*claims), "빵을 먹었어.")
    # When: a lexical deletion repair fails to acknowledge the unknown asked part.
    text, result = process(original, fake)
    # Then: supported original content remains and fixed uncertainty is appended.
    assert text == "빵을 먹었어. 그건 잘 기억이 안 나."
    assert result.action == "fallback"
    assert result.errors == ("repair_missing_uncertainty",)


def test_fallback_when_bad_span_crosses_sentence_boundary():
    # Given: a flagged span overlaps two sentences and one fact is separate.
    original = item("빵을 먹었어. 비행기를 탔어. 바다에 갔어. 집에 왔어.")
    # When: remove sentences overlapping a multi-sentence unsupported span.
    text = fallback(original, (claim("탔어. 바다에"),))
    # Then: unrelated original sentences are verbatim; missing asked part is unknown.
    assert text == "빵을 먹었어. 집에 왔어. 그건 잘 기억이 안 나."


def test_fallback_when_unsupported_claim_is_peripheral():
    # Given: an invented aside, not an answer to the question.
    original = item("빵을 먹었어. 비행기를 탔어.")
    # When: drop its sentence.
    text = fallback(original, (claim("비행기를 탔어.", asked=False),))
    # Then: do not add an unnecessary memory disclaimer.
    assert text == "빵을 먹었어."


@pytest.mark.parametrize("offer", [
    "다시 물어볼까?", "더 궁금한 게 있으면 말해줘.",
    "추가로 확인해 볼까요?", "다시 확인해 줄 수 있어?",
    "필요하면 다시 확인해줄게?", "더 궁금하다면 물어봐.",
    "필요하시면 다시 확인해 드릴 수 있어요.",
    "추가로 확인해 주시겠어요?", "추가로 확인해드릴까요?",
])
def test_tail_when_offer_is_trailing(offer):
    # Given: an ordinary substantive answer followed by an offer.
    original = "빵을 먹었어. " + offer
    # When: run deterministic cleanup.
    text, removed = trim_tail(original)
    # Then: the answer remains verbatim and the exact removed suffix is logged.
    assert text == "빵을 먹었어."
    assert text + removed == original


@pytest.mark.parametrize("original", [
    "다시 물어볼까? 빵을 먹었어.", "누가 왔어?",
    "더 궁금한 게 있으면이라는 표현을 썼어.",
    "추가로 확인해 본 결과 카드가 있었어.",
])
def test_tail_when_not_a_trailing_offer(original):
    # Given: an interior offer, real question, or reported wording.
    # When: cleanup is anchored to an actual offer at the end.
    text, removed = trim_tail(original)
    # Then: no content disappears.
    assert text == original
    assert removed == ""


def test_tail_when_two_offers_follow_repaired_reply():
    # Given: a valid deletion repair with a trailing offer retained.
    original = "빵을 먹었어. 바다에 갔어. 다시 물어볼까?"
    fake = FakeCompletion(
        answer(claim("빵을 먹었어.", "supported"), claim("바다에 갔어.", asked=False)),
        "빵을 먹었어. 다시 물어볼까?",
    )
    # When: repair and cleanup run as separate steps.
    text, result = process(item(original), fake)
    # Then: action retains repair provenance and logs cleanup separately.
    assert text == "빵을 먹었어."
    assert result.action == "repaired"
    assert result.steps == ("check", "repaired", "tail_trimmed")
    assert result.tail_removed == " 다시 물어볼까?"


def test_tail_when_only_offer_remains():
    # Given: a reply containing only removable follow-up offers.
    fake = FakeCompletion(answer())
    # When: cleanup runs after a fact-free check.
    text, result = process(item("다시 물어볼까? 추가로 확인해 볼까요?"), fake)
    # Then: no empty reply is emitted.
    assert text == "그건 잘 기억이 안 나."
    assert result.action == "fallback"
    assert result.tail_removed


@pytest.mark.parametrize("where", ["check", "repair"])
def test_runtime_when_recoverable_error(where):
    # Given: an explicit runtime error at the narrow completion boundary.
    fake = (FakeCompletion(CallError("OOM")) if where == "check" else
            FakeCompletion(answer(claim("비행기를 탔어.")), CallError("OOM")))
    # When: process it without retries for non-JSON failures.
    text, result = process(item("비행기를 탔어."), fake)
    # Then: the exact error remains auditable.
    assert f"{where}_runtime:OOM" in result.errors
    assert result.action == "fallback"
    assert text == ("비행기를 탔어." if where == "check" else "그건 잘 기억이 안 나.")


@pytest.fixture
def artifacts(tmp_path):
    persona = item().persona.model_dump_json(by_alias=True)
    prompt = Prompt(
        question_id="synthetic-dev", condition="A", persona_id="p1",
        split="dev", type="unanswerable", evidence_ids=(),
        messages=(
            Message(role="system", content=f"<persona>\n{persona}\n</persona>"),
            Message(role="user", content="<evidence>[]</evidence><question>잠깐 이야기하자.</question>"),
        ),
    )
    row = Generation(
        **prompt.model_dump(exclude={"messages"}),
        status="success", text="안녕.", raw_text="원래 raw",
    )
    source = tmp_path / "source"
    source.mkdir()
    # A deliberately malformed non-DEV row is ignored before Generation parsing.
    (source / "generations.jsonl").write_text(
        row.model_dump_json() + '\n{"split":"test","condition":"A","text":"DO_NOT_USE"}\n',
        encoding="utf-8",
    )
    (source / "prompts.jsonl").write_text(prompt.model_dump_json() + "\n", encoding="utf-8")
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({
        "questions": [{"question_id": row.question_id, "persona_id": "p1", "split": "dev",
                       "expected_answer": "GOLD_SENTINEL", "type": "GOLD_TYPE_SENTINEL",
                       "support": ["GOLD_SUPPORT_SENTINEL"]}],
        "records": ["FULL_CORPUS_SENTINEL"],
    }), encoding="utf-8")
    return source, corpus, row, prompt


def test_replay_when_dev_only_and_no_gold_leakage(artifacts, tmp_path):
    # Given: a corpus containing sentinel gold and a non-DEV row.
    source, corpus, original, prompt = artifacts
    rows = load_replay(source, corpus, "dev", ("A",))
    fake = FakeCompletion(answer())
    output = tmp_path / "output"
    # When: replay the selected source against its saved inputs.
    failures = replay(source, output, rows, fake)
    result = json.loads((output / "generations.jsonl").read_text())
    # Then: only DEV is processed, original final text is preserved, prompts are copied.
    assert failures == 0
    assert len(fake.calls) == 1
    assert result["split"] == "dev"
    assert result["raw_text"] == original.text
    assert result["text"] == original.text
    assert (output / "prompts.jsonl").read_bytes() == (source / "prompts.jsonl").read_bytes()
    assert fake.calls[0][1] == check_input(prompt, original.text).model_dump(mode="json", by_alias=True)
    assert set(fake.calls[0][1]) == {"persona", "question", "evidence", "reply"}
    assert not any("SENTINEL" in json.dumps(call) for call in fake.calls)
    assert "type" not in fake.calls[0][1]
    assert "condition" not in fake.calls[0][1]


@pytest.mark.parametrize("field,value", [
    ("type", "ordinary_chat"), ("condition", "C"), ("split", "test"),
    ("question_id", "different"), ("persona_id", "different"),
])
def test_inputs_when_metadata_changes(artifacts, field, value):
    # Given: identical saved model-visible fields under different evaluation metadata.
    _, _, row, prompt = artifacts
    changed = prompt.model_copy(update={field: value})
    # When: build runtime inputs.
    baseline = check_input(prompt, row.text)
    actual = check_input(changed, row.text)
    # Then: labels, condition and gold question type never affect the check.
    assert actual == baseline


@pytest.mark.parametrize("mutation", ["duplicate", "missing_prompt", "wrong_persona", "bad_split"])
def test_loader_when_schedule_corrupt(artifacts, mutation):
    # Given: a damaged source pair.
    source, corpus, row, prompt = artifacts
    match mutation:
        case "duplicate":
            (source / "generations.jsonl").write_text(row.model_dump_json() + "\n" + row.model_dump_json())
        case "missing_prompt":
            (source / "prompts.jsonl").write_text("")
        case "wrong_persona":
            wrong = prompt.model_copy(update={"persona_id": "foreign"})
            (source / "prompts.jsonl").write_text(wrong.model_dump_json())
        case "bad_split":
            corpus.write_text('{"questions":[{"question_id":"synthetic-dev","persona_id":"p1","split":"test"}]}')
    # When / Then: refuse before any model call.
    with pytest.raises(InputError):
        load_replay(source, corpus, "dev", ("A",))


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("version", ["pc1", "pc1b"])
def test_main_when_fake_runtime_persists_environment(artifacts, tmp_path, monkeypatch, failed, version):
    # Given: the real CLI persistence path and a fake resident Runtime.
    source, corpus, _, _ = artifacts

    class FakeRuntime:
        def __init__(self, model_path, config):
            self.config = config
            self.environment = {"test_double": True}

        def generate(self, user, cap):
            value = answer() if version == "pc1" else json.dumps({
                "sentences": [{"id":1, "label":"not_a_fact", "answers_question":False}],
            })
            return {"text": "bad" if failed else value}

    class FakeEvaluationError(RuntimeError):
        pass

    monkeypatch.setitem(sys.modules, "runtime", SimpleNamespace(Runtime=FakeRuntime))
    monkeypatch.setitem(sys.modules, "common", SimpleNamespace(EvaluationError=FakeEvaluationError))
    output = tmp_path / "output"
    app = typer.Typer()
    app.command()(main)
    # When: execute the actual CLI option parser and entry function.
    result = CliRunner().invoke(app, [
        "--source-dir", str(source), "--output", str(output),
        "--corpus", str(corpus), "--split", "dev", "--conditions", "A",
        "--version", version,
    ])
    assert result.exit_code == int(failed), result.output
    # Then: final failed checks differ from recovered attempts; provenance is reproducible.
    environment = json.loads((output / "environment.json").read_text())
    assert environment["failed_checks"] == int(failed)
    assert environment["rows"] == 1
    assert environment["environment"] == {"test_double": True}
    assert environment["postcheck_version"] == version
    assert (environment["repair_cap"] is None) == (version == "pc1b")
    assert (environment["repair_system"] is None) == (version == "pc1b")
    assert environment["input_sha256"][str(corpus)] == sha256(corpus.read_bytes()).hexdigest()


@pytest.mark.parametrize("version", [None, "pc1b"])
def test_sbatch_when_required_paths_forwarded(tmp_path, version):
    # Given: a fake python script, with no Slurm, SSH, GPU or model runtime.
    base = tmp_path / "checkout"
    script = base / "experiments/rag/postcheck.py"
    script.parent.mkdir(parents=True)
    script.write_text("import json, sys\nprint('CALL ' + json.dumps(sys.argv[1:]))\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    (venv / "bin/activate").write_text(f'export PATH="{venv}/bin:$PATH"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPLICA_")}
    env.update(
        REPLICA_BASE=str(base), REPLICA_VENV=str(venv),
        REPLICA_SOURCE_DIR="/source", REPLICA_OUTPUT="/fresh",
        REPLICA_CORPUS="/matching/corpus", REPLICA_CONDITIONS="B_v2,C",
    )
    if version is not None:
        env["REPLICA_POSTCHECK_VERSION"] = version
    # When: execute the actual wrapper, not sbatch.
    result = subprocess.run(
        ["bash", str(ROOT / "run-postcheck.sbatch")], env=env,
        capture_output=True, text=True, check=False,
    )
    # Then: source/output/corpus and default DEV are passed through exactly.
    assert result.returncode == 0, result.stderr
    args = json.loads(next(line[5:] for line in result.stdout.splitlines() if line.startswith("CALL ")))
    for flag, expected in [
        ("--source-dir", "/source"), ("--output", "/fresh"),
        ("--corpus", "/matching/corpus"), ("--split", "dev"),
        ("--conditions", "B_v2,C"),
        ("--version", version or "pc1"),
    ]:
        assert args[args.index(flag) + 1] == expected
