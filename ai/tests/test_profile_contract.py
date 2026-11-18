"""Node and raw-output contracts, independent of model behavior."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import profile_contract as contract
from profile_types import ProfileBody


def uid(number: int) -> str:
    return f"{number:08x}-0000-4000-8000-{number:012x}"


def profile() -> ProfileBody:
    return {"schema_version": 1, "speech_style": {
        "tone": ["해요체"], "address_terms": [], "sentence_endings": ["어요"],
        "frequent_expressions": []}, "conversation_habits": [], "limitations": ["합성 자료"]}


def analyze(**changes):
    return {"request_id": uid(1), "persona_id": uid(2), "source_revision": 3,
            "source_snapshot_id": uid(4), "node_id": uid(5), "sources": [
                {"record_id": uid(6), "text_version_id": uid(7),
                 "target_utterances": ["  합성 발화예요.\n", "밥 먹었어요?"]}], **changes}


def merge(**changes):
    return {"request_id": uid(8), "persona_id": uid(2), "source_revision": 3,
            "source_snapshot_id": uid(4), "node_id": uid(9), "inputs": [
                {"node_id": uid(5), "profile_content": profile()},
                {"node_id": uid(10), "profile_content": profile()}], **changes}


class ProfileContractTests(unittest.TestCase):
    def test_request_values_are_immutable_and_integral_revisions_are_canonical(self):
        request = contract.parse_request(analyze(source_revision=3.0), "analyze")
        self.assertEqual(request.source_revision, 3)
        self.assertIs(type(request.source_revision), int)
        self.assertEqual(request.sources[0].target_utterances, ("  합성 발화예요.\n", "밥 먹었어요?"))
        with self.assertRaises(FrozenInstanceError):
            setattr(request, "source_revision", 4)

    def test_structure_errors_precede_parsed_size_rejection(self):
        bad = analyze(sources=[{"record_id": uid(6), "text_version_id": uid(7),
                                "target_utterances": ["x" * 16001, True]}])
        with self.assertRaises(contract.ProfileError) as raised:
            contract.parse_request(bad, "analyze")
        self.assertEqual(raised.exception.code, "shape")
        bad = merge()
        bad["inputs"][0]["profile_content"]["speech_style"]["tone"] = ["x" * 513]
        bad["inputs"][1]["profile_content"]["limitations"] = [False]
        with self.assertRaises(contract.ProfileError) as raised:
            contract.parse_request(bad, "merge")
        self.assertEqual(raised.exception.code, "shape")

    def test_identifier_bindings_and_merge_child_rules(self):
        a = analyze()
        duplicate = analyze(sources=a["sources"] * 2)
        m = merge()
        bad_requests = [
            (analyze(request_id=uid(255).upper()), "analyze"),
            (analyze(node_id="invalid"), "analyze"),
            (analyze(source_revision=True), "analyze"),
            (analyze(source_revision=1.5), "analyze"),
            (analyze(source_revision=-1), "analyze"),
            (analyze(source_revision=float("inf")), "analyze"),
            (analyze(sources=[]), "analyze"),
            (analyze(extra="data"), "analyze"), (duplicate, "analyze"),
            (merge(inputs=m["inputs"][:1]), "merge"),
            (merge(inputs=m["inputs"][:1] * 2), "merge"),
            (merge(node_id=uid(5)), "merge"),
            (merge(max_new_tokens=1), "merge"),
        ]
        for value, task in bad_requests:
            with self.subTest(task=task, value=value), self.assertRaises(contract.ProfileError):
                contract.parse_request(value, task)

    def test_source_count_utterance_count_and_codepoint_limits(self):
        for sources in [
            [{"record_id": uid(20 + i), "text_version_id": uid(50 + i),
              "target_utterances": ["x"]} for i in range(20)],
            [{"record_id": uid(6), "text_version_id": uid(7),
              "target_utterances": ["x"] * 100}],
            [{"record_id": uid(6), "text_version_id": uid(7),
              "target_utterances": ["😀" * 16000]}],
        ]:
            good = analyze(sources=sources)
            contract.parse_request(good, "analyze")
            bad = json.loads(json.dumps(good))
            if len(sources) == 20:
                bad["sources"].append({"record_id": uid(100), "text_version_id": uid(101),
                                       "target_utterances": ["x"]})
            elif len(sources[0]["target_utterances"]) == 100:
                bad["sources"][0]["target_utterances"].append("x")
            else:
                bad["sources"][0]["target_utterances"][0] += "😀"
            with self.assertRaises(contract.ProfileError) as raised:
                contract.parse_request(bad, "analyze")
            self.assertEqual(raised.exception.code, "size")

    def test_raw_profile_parser_rejects_ambiguous_json_and_invalid_content(self):
        values = []
        for key in ("tone", "address_terms", "sentence_endings", "frequent_expressions"):
            for items in ([" "], ["x", "x"], ["x" * 513], [str(i) for i in range(17)], [1]):
                value = profile()
                value["speech_style"][key] = items
                values.append(json.dumps(value))
        for key in ("conversation_habits", "limitations"):
            for items in (["x", "x"], [None], ["x" * 513], [str(i) for i in range(17)]):
                value = profile()
                value[key] = items
                values.append(json.dumps(value))
        raw = json.dumps(profile())
        values.extend([
            "", "{", "[]", raw + " trailing", "```json\n" + raw + "\n```",
            raw.replace('"schema_version": 1', '"schema_version": true'),
            raw.replace('"schema_version": 1', '"schema_version": 2'),
            raw.replace('"schema_version": 1', '"schema_version": NaN'),
            raw.replace('"schema_version": 1', '"schema_version": 1e999'),
            raw.replace('"tone":', '"tone":[],"tone":'),
            raw.replace('"limitations":', '"extra":1,"limitations":'),
            raw.replace('"tone": ["', '"tone": ["\\ud800'),
        ])
        for value in values:
            with self.subTest(raw=value[:80]), self.assertRaises(ValueError):
                contract.parse_output(value)

    def test_profile_boundary_values_and_empty_unknown_arrays_are_preserved(self):
        value = profile()
        value["schema_version"] = 1.0
        value["speech_style"]["tone"] = [f"{i:02d}" + "x" * 510 for i in range(16)]
        parsed = contract.parse_output(json.dumps(value))
        self.assertEqual(contract.profile_body(parsed), value)
        for raw in ('{"a":1,"a":2}', '{"x":{"a":1,"a":2}}', '{"x":Infinity}'):
            with self.assertRaises(contract.ProfileError):
                contract.decode_json(raw)


if __name__ == "__main__":
    unittest.main()
