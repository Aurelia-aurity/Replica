"""Non-JSON numeric constants must fail at the shared HTTP parsing boundary."""
import json
import unittest

from test_persona import FakePersona, PATH, request as persona_request
from test_server import Engine, REQUEST_ID, call, request as baseline_request, serving
import server


class JSONConstantTests(unittest.TestCase):
    def test_persona_constants_are_rejected_before_preparation(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            for field in ("provider_address", "history"):
                with self.subTest(value=value, field=field):
                    data = persona_request()
                    if field == "provider_address":
                        data["persona"][field] = value
                    else:
                        data[field] = [{"user": "question", "assistant": value}]
                    generator = FakePersona()
                    with serving(server.Service(
                            Engine(), persona_generator=generator)) as port:
                        result = call(port, raw=json.dumps(data).encode(), path=PATH)
                    self.assertEqual(result, (400, server.error_body(400)))
                    self.assertEqual(generator.tokenizer.calls, [])
                    self.assertEqual(generator.calls, 0)

    def test_constants_cannot_be_hidden_by_a_later_duplicate_key_on_either_route(self):
        for path, factory in (("/v1/baseline/generate", baseline_request),
                              (PATH, persona_request)):
            for constant in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(path=path, constant=constant):
                    raw = ('{"message":' + constant + ',' + json.dumps(factory())[1:]).encode()
                    engine, generator = Engine(), FakePersona()
                    with serving(server.Service(
                            engine, persona_generator=generator)) as port:
                        result = call(port, raw=raw, path=path)
                    self.assertEqual(result, (400, server.error_body(400)))
                    self.assertEqual(engine.tokenizer.calls + generator.tokenizer.calls, [])
                    self.assertEqual(engine.calls + generator.calls, 0)

    def test_null_provider_address_remains_a_valid_persona_request(self):
        data = persona_request()
        data["persona"]["provider_address"] = None
        generator = FakePersona()
        with serving(server.Service(Engine(), persona_generator=generator)) as port:
            status, body = call(port, data, path=PATH)
        self.assertEqual(status, 200)
        self.assertEqual(body["request_id"], data["request_id"])
        self.assertIsNone(generator.seen.persona.provider_address)
        self.assertEqual(generator.calls, 1)

    def test_quoted_constant_names_remain_valid_text_on_either_route(self):
        for path, factory in (("/v1/baseline/generate", baseline_request),
                              (PATH, persona_request)):
            with self.subTest(path=path):
                data = factory()
                data["message"] = "NaN Infinity -Infinity"
                engine, generator = Engine(), FakePersona()
                with serving(server.Service(engine, persona_generator=generator)) as port:
                    status, body = call(port, data, path=path)
                self.assertEqual(status, 200)
                self.assertEqual(body["request_id"], data["request_id"])
                self.assertEqual(engine.calls + generator.calls, 1)

    def test_raw_size_then_auth_then_json_before_readiness_context_and_slot(self):
        for path, factory in (("/v1/baseline/generate", baseline_request),
                              (PATH, persona_request)):
            raw = ('{"message":NaN,' + json.dumps(factory())[1:]).encode()
            for auth_status in (401, 403, None):
                with self.subTest(path=path, auth_status=auth_status):
                    engine, generator = Engine(context_limit=1), FakePersona()
                    generator.context_limit = 1
                    service = server.Service(
                        engine, auth=lambda _: auth_status, persona_generator=generator)
                    service.ready.clear()
                    service.active_id = REQUEST_ID
                    with serving(service) as port:
                        oversized = call(port, raw=b"", path=path,
                                         headers={"Content-Length": "262145"})
                        result = call(port, raw=raw, path=path)
                    self.assertEqual(oversized, (413, server.error_body(413)))
                    status = auth_status if auth_status is not None else 400
                    self.assertEqual(result, (status, server.error_body(status)))
                    self.assertEqual(engine.tokenizer.calls + generator.tokenizer.calls, [])
                    self.assertEqual(engine.calls + generator.calls, 0)
