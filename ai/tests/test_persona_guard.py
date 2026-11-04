"""Deterministic synthetic checks; unittest matches the existing AI test runner."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ai/src"))
sys.path.insert(0, str(REPO / "experiments/persona_guard"))
from persona_guard import PersonaIdentity, check_reply
from replay import main


class PersonaGuardTests(unittest.TestCase):
    def test_preserves_advice_when_quoted_third_party_is_the_subject(self):
        # Given: quoted subject text still makes these third-party statements.
        persona = PersonaIdentity(("할아버지",), ("온유",), "하게체")
        for text in (
            "“엄마가” 할아버지께 물어봐.",
            "“엄마가” 온유에게 물어보게.",
            "“민수가” 온유에게 물어봐.",
            "“네 친구” 온유에게 물어봐.",
            "할아버지께서는 “엄마가” 알려 주시면 좋겠어요.",
            "할아버지께서는 “엄마가” 기억하시면 알려 주시면 감사하겠습니다.",
        ):
            with self.subTest(text=text):
                # When: inspect the response while excluding quoted advice.
                result = check_reply(text, persona)
                # Then: masking must not invent a direct request to the user.
                self.assertIsNone(result.matched)
                self.assertEqual(result.text.encode(), text.encode())

    def test_detects_advice_when_name_and_relation_or_line_wrap_are_used(self):
        # Given: the same persona expressed with a compound alias or line wrap.
        persona = PersonaIdentity(("할아버지",), ("온유",), "하게체")
        for text in ("온유 할아버지한테 여쭤보세요.", "할아버지께\n여쭤보세요."):
            with self.subTest(text=text):
                # When: inspect the response without normalizing its source.
                result = check_reply(text, persona)
                # Then: actual self-referral advice is removed in the right register.
                self.assertIsNotNone(result.matched)
                self.assertNotIn("여쭤", result.text)
                self.assertTrue(result.text.endswith("나네."))

    def test_explicit_outcomes_when_frozen_synthetic_cases(self):
        # Given: independent expected actions, frozen before implementation.
        cases = []
        for name in ("cases.json", "request_cases.json"):
            fixture = json.loads(
                (REPO / "experiments/persona_guard" / name).read_text(encoding="utf-8")
            )
            cases.extend((case, fixture["identities"][case["identity"]])
                         for case in fixture["cases"])
        for case, identity in cases:
            with self.subTest(case=case["id"]):
                persona = PersonaIdentity(
                    tuple(identity["relationship_aliases"]),
                    tuple(identity["name_aliases"]), identity["register"],
                )
                # When: check the reply, not the user's question.
                result = check_reply(case["output"], persona)
                # Then: expected action and independent output properties hold.
                self.assertEqual(result.matched is not None, case["expected"] == "replace")
                if case["expected"] == "pass":
                    self.assertEqual(result.text.encode(), case["output"].encode())
                else:
                    self.assertNotEqual(result.text, case["output"])
                    self.assertNotIn("?", result.text)
                    for alias in (*persona.name_aliases, *persona.relationship_aliases):
                        self.assertNotIn(alias, result.text)
                    self.assertTrue(result.text.endswith({
                        "반말": "나.", "다정한 반말": "나.",
                        "해요체": "나요.", "하게체": "나네.",
                    }[persona.register]))

    def test_replay_when_fixture_oracle_runs(self):
        # Given: the default frozen fixture and an in-memory stdout sink.
        output = io.StringIO()
        # When: run the same CLI entry point with no model inputs.
        with redirect_stdout(output):
            status = main([])
        # Then: explicit expectations and independent postconditions pass.
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["summary"]["synthetic_expectation_failures"], 0)
        self.assertEqual(report["summary"]["synthetic_independent_postcheck_violations"], 0)

    def test_cli_when_invoked_outside_repository(self):
        # Given: an absolute script path, with no cwd-based imports.
        script = REPO / "experiments/persona_guard/replay.py"
        # When: drive the real CLI, bounded by process completion rather than sleeps.
        result = subprocess.run(
            [sys.executable, "-B", str(script)], cwd=script.parent,
            capture_output=True, text=True, timeout=10, check=False,
        )
        # Then: JSON stdout and successful exit establish the actual CLI surface.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(json.loads(result.stdout)["summary"]["synthetic_rows"], 0)
