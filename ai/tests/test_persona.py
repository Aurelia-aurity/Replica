"""Draft wire contracts over real loopback HTTP; no chosen model-output protocol."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import persona_contract as contract
import server
from test_server import Engine, OTHER_ID, call, request as baseline_request, serving

FIXTURES = Path(__file__).parent / "fixtures/contract_persona"
PATH = "/v1/persona/generate"


def request():
    return json.loads((FIXTURES / "03-persona-request.json").read_text())


def assign(data, path, value):
    current = data
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value
    return data


class FakePersona(Engine):
    """Only the generator boundary is fake; parsing, scheduling and HTTP are real."""
    def __init__(self):
        super().__init__()
        self.evidence_used = ()
        self.model_name = "synthetic-model"
        self.seen = None

    def generate(self, req, prepared):
        self.seen = req
        result = super().generate(req, prepared)
        return contract.PersonaGenerated(result.message, result.finish_reason,
                                          result.output_tokens, self.evidence_used,
                                          self.model_name)


class PersonaTests(unittest.TestCase):
    def test_pr75_examples_roundtrip_success(self):
        for prefix in ("03-persona", "04-no-evidence"):
            data = json.loads((FIXTURES / f"{prefix}-request.json").read_text())
            expected = json.loads((FIXTURES / f"{prefix}-response.json").read_text())
            fake = FakePersona()
            fake.answer, fake.reason = expected["message"], expected["finish_reason"]
            fake.evidence_used, fake.model_name = tuple(expected["evidence_used"]), expected["model_name"]
            with self.subTest(prefix=prefix), serving(
                    server.Service(Engine(), persona_generator=fake)) as port:
                self.assertEqual(call(port, data, path=PATH), (200, expected))
            self.assertEqual(fake.seen.persona.provider_address, data["persona"]["provider_address"])
            self.assertEqual(fake.seen.style_profile.source_snapshot_id,
                             data["style_profile"]["source_snapshot_id"])

    def test_default_service_blocks_persona_without_changing_baseline(self):
        engine = Engine()
        with serving(server.Service(engine)) as port:
            self.assertEqual(call(port, request(), path=PATH),
                             (503, server.error_body(503, request()["request_id"])))
            self.assertEqual(call(port, baseline_request())[0], 200)
        self.assertEqual(engine.calls, 1)

    def test_empty_or_blank_display_name_returns_400_without_generation(self):
        for display_name in ("", " ", "\t\n"):
            data = request()
            data["persona"]["display_name"] = display_name
            fake = FakePersona()
            with self.subTest(display_name=display_name), serving(
                    server.Service(Engine(), persona_generator=fake)) as port:
                self.assertEqual(call(port, data, path=PATH),
                                 (400, server.error_body(400, data["request_id"])))
            self.assertEqual(fake.calls, 0)

    def test_raw_then_auth_then_structure_then_sizes_then_readiness(self):
        cases = [
            ({"Content-Length": "262145"}, b"", 413, None),
            ({}, b"{broken", 401, None),
        ]
        for headers, raw, status, request_id in cases:
            with self.subTest(status=status), serving(
                    server.Service(auth=lambda _: 401)) as port:
                self.assertEqual(call(port, raw=raw, headers=headers, path=PATH),
                                 (status, server.error_body(status, request_id)))
        data = request()
        data["message"] = "x" * 16001
        data["extra"] = True
        with serving(server.Service()) as port:
            self.assertEqual(call(port, data, path=PATH)[0], 400)
            del data["extra"]
            self.assertEqual(call(port, data, path=PATH)[0], 413)
            self.assertEqual(call(port, request(), path=PATH)[0], 503)

    def test_nested_structural_rejections(self):
        invalid = [
            (("persona",), None), (("persona",), {"display_name": "x"}),
            (("persona", "provider_address"), ""), (("persona", "display_name"), 3),
            (("persona", "extra"), 1), (("persona_id",), "AAAAAAAA-0000-4000-8000-000000000000"),
            (("source_revision",), True), (("source_revision",), -1),
            (("style_profile", "source_revision"), 4),
            (("style_profile", "version"), 1.5), (("style_profile", "version"), True),
            (("style_profile", "source_snapshot_id"), "bad"),
            (("style_profile", "content", "schema_version"), True),
            (("style_profile", "content", "speech_style", "tone"), ["x", "x"]),
            (("style_profile", "content", "speech_style", "tone"), [" "]),
            (("style_profile", "content", "speech_style", "tone"), {}),
            (("history",), [{"user": "u", "assistant": " "}]),
            (("history",), [{"user": "u", "assistant": "a", "extra": 1}]),
            (("max_new_tokens",), True), (("max_new_tokens",), 0),
            (("max_new_tokens",), 1025), (("max_new_tokens",), 1.5),
            (("evidence", 0, "speaker_role"), "unknown"),
            (("evidence", 0, "content"), "\t"),
            (("evidence", 0, "record_id"), "bad"),
            (("evidence", 0, "extra"), 1),
        ]
        for path, value in invalid:
            with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                contract.parse_request(assign(request(), path, value))
        data = request()
        data["evidence"] *= 2
        with self.assertRaises(ValueError):
            contract.parse_request(data)
        # Every required field at every relevant object depth is checked.
        for path in ((), ("persona",), ("style_profile",),
                     ("style_profile", "content"), ("style_profile", "content", "speech_style"),
                     ("evidence", 0)):
            parent = request()
            for key in path:
                parent = parent[key]
            for key in parent:
                data = request()
                target = data
                for step in path:
                    target = target[step]
                del target[key]
                with self.subTest(missing=(*path, key)), self.assertRaises(ValueError):
                    contract.parse_request(data)

    def test_parsed_limit_boundaries(self):
        mutations = [
            (("message",), "x" * 16000, "x" * 16001),
            (("persona", "display_name"), "x" * 50, "x" * 51),
            (("persona", "provider_address"), "x" * 50, "x" * 51),
            (("history",), [{"user": "u", "assistant": "a"}] * 20,
             [{"user": "u", "assistant": "a"}] * 21),
            (("style_profile", "content", "limitations"),
             [str(i) for i in range(16)], [str(i) for i in range(17)]),
            (("style_profile", "content", "conversation_habits"), ["x" * 512], ["x" * 513]),
            (("evidence", 0, "content"), "x" * 16000, "x" * 16001),
        ]
        for path, valid, too_large in mutations:
            with self.subTest(path=path):
                contract.parse_request(assign(request(), path, valid))
                with self.assertRaises(contract.PayloadTooLarge):
                    contract.parse_request(assign(request(), path, too_large))
        data = request()
        item = data["evidence"][0]
        data["evidence"] = [dict(item, chunk_id=f"{i:08x}-0000-4000-8000-000000000000")
                            for i in range(11)]
        with self.assertRaises(contract.PayloadTooLarge):
            contract.parse_request(data)
        contract.parse_request(dict(data, evidence=data["evidence"][:10]))

    def test_integral_json_numbers_empty_profiles_and_whitespace_provider_address(self):
        data = request()
        data["max_new_tokens"] = 1.0
        data["source_revision"] = 3.0
        data["persona"] = {"display_name": "엄마", "provider_address": "\t"}
        data["style_profile"]["content"]["speech_style"] = {
            key: [] for key in reversed(data["style_profile"]["content"]["speech_style"])}
        parsed = contract.parse_request(data)
        self.assertEqual(parsed.max_new_tokens, 1)
        self.assertEqual(parsed.style_profile.content.tone, ())
        self.assertEqual(parsed.persona.provider_address, "\t")

    def test_context_and_generator_failures_use_safe_envelopes(self):
        fake = FakePersona()
        service = server.Service(Engine(), persona_generator=fake)
        with serving(service) as port:
            fake.context_limit = 1
            self.assertEqual(call(port, request(), path=PATH),
                             (422, server.error_body(422, request()["request_id"])))
            fake.context_limit = 100000
            fake.failure = True
            self.assertEqual(call(port, request(), path=PATH),
                             (500, server.error_body(500, request()["request_id"])))

    def test_success_validation_rejects_invalid_external_results(self):
        req = contract.parse_request(request())
        fake = FakePersona()
        prepared = fake.prepare(req)
        valid = contract.PersonaGenerated("answer", "length", 1, (), "synthetic-model")
        bad = [
            replace(valid, message=" "), replace(valid, message="x" * 16001),
            replace(valid, model_name=" "), replace(valid, finish_reason="invalid"),
            replace(valid, evidence_used=(OTHER_ID,)),
            replace(valid, evidence_used=(req.evidence[0].chunk_id,) * 2),
        ]
        for result in bad:
            with self.subTest(result=result), self.assertRaises(ValueError):
                contract.success_body(req, prepared, result)
        self.assertEqual(contract.success_body(req, prepared, valid)["finish_reason"], "length")

    def test_generation_metadata_failure_returns_500_over_http(self):
        fake = FakePersona()
        fake.evidence_used = (OTHER_ID,)
        with serving(server.Service(Engine(), persona_generator=fake)) as port:
            self.assertEqual(call(port, request(), path=PATH),
                             (500, server.error_body(500, request()["request_id"])))

    def test_both_routes_share_duplicate_and_busy_slot(self):
        for first_path in (PATH, "/v1/baseline/generate"):
            baseline, persona = Engine(), FakePersona()
            first_engine = persona if first_path == PATH else baseline
            first_engine.block = True
            service = server.Service(baseline, persona_generator=persona)
            first = request() if first_path == PATH else baseline_request()
            first["request_id"] = OTHER_ID
            second_path = "/v1/baseline/generate" if first_path == PATH else PATH
            second = baseline_request() if first_path == PATH else request()
            responses = []
            with self.subTest(first=first_path), serving(service) as port:
                thread = threading.Thread(target=lambda: responses.append(call(port, first, path=first_path)))
                thread.start()
                try:
                    self.assertTrue(first_engine.entered.wait(5))
                    second["request_id"] = OTHER_ID
                    self.assertEqual(call(port, second, path=second_path),
                                     (409, server.error_body(409, OTHER_ID)))
                    second["request_id"] = request()["request_id"]
                    self.assertEqual(call(port, second, path=second_path),
                                     (429, server.error_body(429, second["request_id"])))
                finally:
                    first_engine.release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(responses[0][0], 200)
