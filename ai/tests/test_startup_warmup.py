"""Startup readiness uses the actual loader, with event-controlled doubles."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import server
from startup_warmup import persona_fixture
from test_server import Engine, call, request, serving


class StartupWarmupTests(unittest.TestCase):
    def test_each_mode_stays_unready_until_all_three_calls_complete(self):
        for mode in (None, "synthetic"):
            with self.subTest(mode=mode):
                engine = Engine()
                engine.answer = '{"message":"synthetic answer","evidence_used":[]}'
                entered = [threading.Event() for _ in range(3)]
                release = [threading.Event() for _ in range(3)]
                seen = []
                generate = engine.generate

                def gated(req, prepared):
                    index = len(seen)
                    seen.append(req)
                    entered[index].set()
                    if not release[index].wait(5):
                        raise AssertionError
                    return generate(req, prepared)

                service = server.Service()
                with patch.object(engine, "generate", gated), serving(service) as port:
                    try:
                        with self.assertLogs("baseline.http", level="INFO") as logs:
                            service.start_loading(lambda: engine, persona_model_name=mode)
                            for index in range(3):
                                self.assertTrue(entered[index].wait(5))
                                self.assertFalse(service.ready.is_set())
                                self.assertIsNone(service.active_id)
                                self.assertEqual(call(port, method="GET", path="/readyz"),
                                                 (503, {"ready": False}))
                                self.assertEqual(call(port, request())[0], 503)
                                self.assertEqual(engine.calls, index)
                                release[index].set()
                            self.assertTrue(service.ready.wait(5))
                            service.loader.join(5)
                            self.assertFalse(service.loader.is_alive())
                        self.assertEqual(call(port, method="GET", path="/readyz"),
                                         (200, {"ready": True}))
                    finally:
                        for gate in release:
                            gate.set()
                        service.loader.join(5)
                self.assertEqual(engine.calls, 3)
                self.assertIsNone(service.active_id)
                records = [json.loads(line.split(":", 2)[-1]) for line in logs.output
                           if '"event":"startup_timing"' in line]
                self.assertEqual([(r["stage"], r["status"]) for r in records],
                                 [("load", "begin"), ("load", "complete"),
                                  ("warmup", "begin"), ("warmup", "complete"),
                                  ("ready", "complete")])
                if mode:
                    prompts = [call[0][-1]["content"] for call in engine.tokenizer.calls]
                    self.assertEqual([p.count("Synthetic record") for p in prompts], [0, 3, 10])
                else:
                    self.assertEqual([r.message for r in seen],
                                     ["Reply with one short greeting."] * 3)

    def test_warmup_failure_keeps_not_ready_and_never_retries(self):
        for mode in (None, "synthetic"):
            for failure in ("prepare", "generate", "validation"):
                with self.subTest(mode=mode, failure=failure):
                    engine = Engine()
                    engine.answer = "" if failure == "validation" else (
                        '{"message":"answer","evidence_used":[]}')
                    if failure == "generate":
                        engine.failure = True
                    if failure == "prepare":
                        engine.context_limit = 1
                    service = server.Service()
                    with self.assertLogs("baseline.http", level="INFO") as logs:
                        service.start_loading(lambda: engine, persona_model_name=mode)
                        service.loader.join(5)
                    self.assertFalse(service.loader.is_alive())
                    self.assertFalse(service.ready.is_set())
                    self.assertIsNone(service.active_id)
                    self.assertEqual(engine.calls, 0 if failure == "prepare" else 1)
                    output = "\n".join(logs.output)
                    self.assertIn('"stage":"warmup"', output)
                    self.assertIn('"status":"failed"', output)
                    self.assertNotIn("SECRET", output)
                    with serving(service) as port:
                        self.assertEqual(call(port, method="GET", path="/readyz")[0], 503)

    def test_warmup_does_not_use_admission_or_leave_client_slot_occupied(self):
        engine = Engine()
        service = server.Service()
        with patch.object(service, "accept", wraps=service.accept) as accept:
            service.start_loading(lambda: engine)
            service.loader.join(5)
            self.assertTrue(service.ready.is_set())
            self.assertEqual(engine.calls, 3)
            accept.assert_not_called()
            with serving(service) as port:
                self.assertEqual(call(port, request())[0], 200)
            self.assertEqual(accept.call_count, 1)
            self.assertEqual(engine.calls, 4)
            self.assertIsNone(service.active_id)

    def test_synthetic_fixtures_obey_contract_and_repeat(self):
        for count in (0, 3, 6, 10):
            data = persona_fixture(count)
            parsed = server.persona_contract.parse_request(data)
            self.assertEqual(len(parsed.evidence), count)
            self.assertEqual(json.dumps(data), json.dumps(persona_fixture(count)))


if __name__ == "__main__":
    unittest.main()
