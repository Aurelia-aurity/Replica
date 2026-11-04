"""Register-variant mechanics; prompt prose is not an assertion target."""

from copy import deepcopy
import json
import unittest

from prepare import content_hash
from prompt import TEMPLATE_14B_PATH, TEMPLATE_PATH, render_messages, variant_bundle
from prepare import digest
from register_check import register_match


def fixture(register="하게체"):
    messages = [
        {"role": "system", "content": "system\n\n<persona>\n"
         + json.dumps({"register": register}, ensure_ascii=False) + "\n</persona>"},
        {"role": "user", "content": "<evidence>[]</evidence>\n<question>?</question>"},
    ]
    row = {"id": "q:B", "messages": messages,
           "messages_sha256": content_hash(messages), "evidence_sha256": "fixed"}
    return {"split": "dev", "rows": [row], "warmups": [deepcopy(row)],
            "stress": [deepcopy(row)], "schedule_seed": 77}


class PromptTests(unittest.TestCase):
    def test_frozen_r1_reg_template_keeps_shipped_bytes(self):
        # Given: the exact ROUND 2 artifact, which this task must preserve.
        frozen_sha256 = "f11ba3f04b4b8f07ed942a2714dc5421e66b1f9575adceb54f152a777afdedaf"
        # When
        actual = digest(TEMPLATE_PATH)
        # Then
        self.assertEqual(actual, frozen_sha256)

    def test_14b_variant_is_deterministic_and_preserves_source(self):
        # Given
        bundle = fixture()
        before = deepcopy(bundle)
        # When
        first = variant_bundle(bundle, "r1-reg-14b")
        second = variant_bundle(bundle, "r1-reg-14b")
        # Then
        self.assertEqual(first, second)
        self.assertEqual(bundle, before)
        self.assertEqual(first["register_template_sha256"], digest(TEMPLATE_14B_PATH))
        self.assertNotEqual(first["register_template_sha256"], digest(TEMPLATE_PATH))
        for phase in ("rows", "warmups", "stress"):
            self.assertEqual(first[phase][0]["messages"][1], bundle[phase][0]["messages"][1])
            self.assertEqual(first[phase][0]["evidence_sha256"], bundle[phase][0]["evidence_sha256"])
            self.assertEqual(first[phase][0]["messages_sha256"],
                             content_hash(first[phase][0]["messages"]))

    def test_14b_all_registers_and_alias_render_without_changing_defaults(self):
        # Given
        bundles = [fixture(r) for r in ("반말", "해요체", "하게체", "다정한 반말")]
        originals = [variant_bundle(b, "r1-reg") for b in bundles]
        # When
        variants = [variant_bundle(b, "r1-reg-14b") for b in bundles]
        # Then
        self.assertEqual(len({b["rows"][0]["messages_sha256"] for b in variants}), 4)
        self.assertEqual(originals, [variant_bundle(b, "r1-reg") for b in bundles])
        self.assertEqual(bundles, [variant_bundle(b) for b in bundles])
        with self.assertRaises(ValueError):
            variant_bundle(variants[0], "r1-reg-14b")

    def test_default_r1_preserves_message_bytes(self):
        # Given
        messages = fixture()["rows"][0]["messages"]
        before = json.dumps(messages, ensure_ascii=False).encode("utf-8")
        # When
        result = render_messages(messages)
        # Then
        self.assertEqual(json.dumps(result, ensure_ascii=False).encode("utf-8"), before)

    def test_default_r1_preserves_entire_bundle_bytes(self):
        # Given
        bundle = fixture()
        before = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
        # When
        result = variant_bundle(bundle)
        # Then
        self.assertEqual(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"), before)

    def test_variant_is_deterministic_without_mutating_input(self):
        # Given
        bundle = fixture()
        before = deepcopy(bundle)
        # When
        results = [variant_bundle(bundle, "r1-reg") for _ in range(2)]
        # Then
        self.assertEqual(results[0], results[1])
        self.assertEqual(bundle, before)
        self.assertNotEqual(content_hash(results[0]), content_hash(bundle))

    def test_variant_changes_only_system_content_and_message_hashes(self):
        # Given
        bundle = fixture()
        # When
        result = variant_bundle(bundle, "r1-reg")
        # Then
        for phase in ("rows", "warmups", "stress"):
            original, changed = bundle[phase][0], result[phase][0]
            self.assertEqual(changed["messages"][1], original["messages"][1])
            self.assertTrue(changed["messages"][0]["content"].startswith(
                original["messages"][0]["content"] + "\n\n"))
            self.assertEqual(changed["messages_sha256"], content_hash(changed["messages"]))
            self.assertEqual(changed["evidence_sha256"], original["evidence_sha256"])
            self.assertEqual(changed["id"], original["id"])

    def test_all_dev_registers_have_distinct_deterministic_renderings(self):
        # Given
        registers = ("반말", "해요체", "하게체", "다정한 반말")
        # When
        results = [variant_bundle(fixture(r), "r1-reg") for r in registers]
        # Then
        self.assertEqual(len({r["rows"][0]["messages_sha256"] for r in results}), 4)
        self.assertEqual(len({r["register_template_sha256"] for r in results}), 1)

    def test_unknown_register_does_not_silently_fallback(self):
        # Given
        bundle = fixture("unknown")
        # When / Then
        with self.assertRaises(KeyError):
            variant_bundle(bundle, "r1-reg")


class RegisterCheckTests(unittest.TestCase):
    def test_quotes_are_not_target_register_errors(self):
        # Given
        response = '"반갑습니다."라고 적혀 있네.'
        # When
        result = register_match(response, "하게체")
        # Then
        self.assertTrue(result)

    def test_mixed_register_is_not_hidden_by_final_ending(self):
        # Given
        response = "충분합니다. 알겠네."
        # When
        result = register_match(response, "하게체")
        # Then
        self.assertFalse(result)

    def test_haeyo_honoring_another_person_is_allowed(self):
        # Given
        response = "말씀하세요."
        # When
        result = register_match(response, "해요체")
        # Then
        self.assertTrue(result)

    def test_empty_response_is_not_correct_register(self):
        # Given
        response = ""
        # When
        result = register_match(response, "하게체")
        # Then
        self.assertFalse(result)

    def test_variant_cannot_be_applied_twice(self):
        # Given
        bundle = variant_bundle(fixture(), "r1-reg")
        # When / Then
        with self.assertRaises(ValueError):
            variant_bundle(bundle, "r1-reg")


if __name__ == "__main__":
    unittest.main()
