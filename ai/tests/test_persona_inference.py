"""Real persona adapter with fake decoding and real loopback HTTP."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from inference import ContextLimitExceeded
from persona_adapter import PersonaInference, reduced_identity
from persona_contract import parse_request
from persona_guard import check_reply
import server
from test_persona import PATH, request
from test_server import Engine, call, request as baseline_request, serving


class PersonaInferenceTests(unittest.TestCase):
    def test_citation_order_maps_to_supplied_uuid_not_record_id(self):
        # Given:
        data = request()
        second_id = "22222222-2222-4222-8222-222222222222"
        data["evidence"].append(dict(data["evidence"][0], chunk_id=second_id))
        engine = Engine()
        engine.answer = '{"message":"a","evidence_used":[2,1]}'
        adapter = PersonaInference(engine, "test")
        req = parse_request(data)
        # When:
        result = adapter.generate(req, adapter.prepare(req))
        # Then:
        self.assertEqual(result.evidence_used, (second_id, req.evidence[0].chunk_id))

    def test_success_maps_citations_and_preserves_metadata(self):
        # Given: the real adapter and a single deterministic decode.
        engine = Engine()
        engine.answer = json.dumps({"message": "부산에 갔지.", "evidence_used": [1]})
        data = request()
        adapter = PersonaInference(engine, "configurable-model")
        # When: driving the actual HTTP route.
        with serving(server.Service(engine, persona_generator=adapter)) as port:
            status, body = call(port, data, path=PATH)
        # Then: server metadata and input UUID, never the citation number, are returned.
        self.assertEqual(status, 200)
        self.assertEqual(body["evidence_used"], [data["evidence"][0]["chunk_id"]])
        self.assertEqual(body["applied_profile"], {
            k: data["style_profile"][k] for k in ("id", "version", "source_revision")})
        self.assertEqual(body["model_name"], "configurable-model")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(engine.system_prompt, "system")

    def test_no_evidence_and_no_citations_are_valid(self):
        for evidence in ([], request()["evidence"]):
            # Given: citations may be empty even when evidence is available.
            engine = Engine()
            engine.answer = '{"message":"안녕.","evidence_used":[]}'
            data = dict(request(), evidence=evidence)
            # When:
            with serving(server.Service(
                    engine, persona_generator=PersonaInference(engine, "test"))) as port:
                status, body = call(port, data, path=PATH)
            # Then:
            self.assertEqual(status, 200)
            self.assertEqual(body["evidence_used"], [])

    def test_invalid_output_fails_once_without_baseline_recall(self):
        outputs = [
            '{"message":"a","evidence_used":[0]}',
            '{"message":"a","evidence_used":[2]}',
            '{"message":"a","evidence_used":[1,1]}',
            '{"message":"a","evidence_used":[true]}',
            '{"message":"a","evidence_used":[1.0]}',
            '{"message":"a","evidence_used":["1"]}',
            '{"message":"a","evidence_used":null}',
            '{"message":"a"}', '{"message":"a","evidence_used":[',
            '{"message":" ","evidence_used":[]}',
            '{"message":"a","evidence_used":[],"extra":1}',
            '{"message":"a","message":"b","evidence_used":[]}',
            '```json\n{"message":"a","evidence_used":[]}\n```',
        ]
        for output in outputs:
            # Given:
            engine = Engine()
            engine.answer = output
            with self.subTest(output=output), serving(server.Service(
                    engine, persona_generator=PersonaInference(engine, "test"))) as port:
                # When:
                result = call(port, request(), path=PATH)
            # Then:
            self.assertEqual(result, (500, server.error_body(500, request()["request_id"])))
            self.assertEqual(engine.calls, 1)

    def test_no_evidence_cannot_cite_and_failure_does_not_retry(self):
        for answer, failure in (('{"message":"a","evidence_used":[1]}', False), ("", True)):
            # Given:
            engine = Engine()
            engine.answer, engine.failure = answer, failure
            with self.subTest(failure=failure), serving(server.Service(
                    engine, persona_generator=PersonaInference(engine, "test"))) as port:
                # When:
                status, _ = call(port, dict(request(), evidence=[]), path=PATH)
            # Then:
            self.assertEqual(status, 500)
            self.assertEqual(engine.calls, 1)

    def test_reduced_guard_replaces_and_clears_evidence_preserving_reason(self):
        for tone in ([], ["해요체"], ["반말"], ["다정한 반말"], ["하게체"],
                     ["반말", "해요체"], ["다정한 어조"], ["합쇼체"]):
            # Given: valid free-text tones never cause a request rejection.
            data = request()
            data["style_profile"]["content"]["speech_style"]["tone"] = tone
            engine = Engine()
            engine.answer = json.dumps({"message": "엄마께 물어보세요.", "evidence_used": [1]})
            engine.reason = "length"
            parsed = parse_request(data)
            identity = reduced_identity(parsed)
            expected_register = tone[0] if len(tone) == 1 and tone[0] in (
                "반말", "다정한 반말", "해요체", "하게체") else "해요체"
            self.assertEqual(identity.register, expected_register)
            # When:
            with self.subTest(tone=tone), serving(server.Service(
                    engine, persona_generator=PersonaInference(engine, "test"))) as port:
                status, body = call(port, data, path=PATH)
            # Then:
            self.assertEqual(status, 200)
            self.assertEqual(body["message"], check_reply("엄마께 물어보세요.", identity).text)
            self.assertEqual(body["evidence_used"], [])
            self.assertEqual(body["finish_reason"], "length")

    def test_provider_address_is_not_a_self_alias(self):
        # Given:
        data = request()
        data["persona"] = {"display_name": "온유", "provider_address": "누나"}
        engine = Engine()
        engine.answer = json.dumps({"message": "누나에게 물어보세요.", "evidence_used": [1]})
        # When:
        with serving(server.Service(
                engine, persona_generator=PersonaInference(engine, "test"))) as port:
            status, body = call(port, data, path=PATH)
        # Then:
        self.assertEqual(status, 200)
        self.assertEqual(body["message"], "누나에게 물어보세요.")
        self.assertEqual(len(body["evidence_used"]), 1)
        identity = reduced_identity(parse_request(data))
        self.assertEqual(identity.name_aliases, ("온유",))
        self.assertEqual(identity.relationship_aliases, ("온유",))

    def test_prompt_keeps_evidence_roles_and_drops_only_complete_history(self):
        # Given:
        engine = Engine()
        adapter = PersonaInference(engine, "test")
        req = replace(parse_request(request()), history=(("old", "answer"), ("new", "reply")))
        keep = replace(req, history=req.history[1:])
        engine.context_limit = len(adapter.prepare(keep).token_ids) + req.max_new_tokens
        # When:
        prepared = adapter.prepare(req)
        # Then:
        self.assertEqual((prepared.used_history_turns, prepared.dropped_history_turns), (1, 1))
        messages, options = engine.tokenizer.calls[-1]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual([m["content"] for m in messages[1:3]], list(req.history[1]))
        block = messages[-1]["content"].split("<evidence>\n")[1].split("\n</evidence>")[0]
        evidence = json.loads(block)
        self.assertEqual(evidence[0]["chunk_id"], req.evidence[0].chunk_id)
        self.assertEqual(evidence[0]["speaker_role"], req.evidence[0].speaker_role)
        self.assertEqual(evidence[0]["content"], req.evidence[0].content)
        self.assertEqual(options, {
            "tokenize": True, "add_generation_prompt": True, "enable_thinking": False})
        engine.context_limit = req.max_new_tokens
        with self.assertRaises(ContextLimitExceeded):
            adapter.prepare(req)

    def test_budget_is_unchanged_including_length_success(self):
        # Given:
        class RecordingEngine(Engine):
            def generate(self, req, prepared):
                self.budget = req.max_new_tokens
                return super().generate(req, prepared)
        for budget in (1, 160, 1024):
            engine = RecordingEngine()
            engine.answer = '{"message":"a","evidence_used":[]}'
            engine.reason = "length"
            adapter = PersonaInference(engine, "test")
            req = replace(parse_request(request()), max_new_tokens=budget)
            # When:
            result = adapter.generate(req, adapter.prepare(req))
            # Then:
            self.assertEqual(engine.budget, budget)
            self.assertEqual(result.finish_reason, "length")
            self.assertEqual(engine.calls, 1)

    def test_explicit_load_mode_shares_one_engine_and_baseline_is_separate(self):
        for model_name in (None, "test"):
            # Given:
            engine = Engine()
            engine.answer = '{"message":"a","evidence_used":[]}'
            service = server.Service()
            # When:
            service.start_loading(lambda: engine, persona_model_name=model_name)
            service.loader.join(5)
            self.assertFalse(service.loader.is_alive())
            with serving(service) as port:
                persona_status, _ = call(port, request(), path=PATH)
                baseline_status, _ = call(port, baseline_request())
            # Then:
            self.assertEqual(persona_status, 503 if model_name is None else 200)
            self.assertEqual(baseline_status, 200)
            self.assertEqual(engine.calls, 1 if model_name is None else 2)
            if model_name is not None:
                self.assertIs(service.persona_generator.engine, engine)

    def test_cli_mode_publishes_adapter_only_when_explicitly_selected(self):
        for mode in ("baseline", "persona"):
            # Given: the actual loader thread and a fake loaded engine.
            engine = Engine()
            observed = []

            class InspectServer(server.Server):
                def __init__(self, port, service):
                    super().__init__(0, service)

                def serve_forever(self):
                    self.service.loader.join(5)
                    observed.append((self.service.ready.is_set(),
                                     self.service.persona_generator))
                    raise KeyboardInterrupt

            settings = {"model_id": "configured-model"}
            with patch.object(server.baseline, "read_settings", return_value=settings), patch.object(
                    server, "Inference", return_value=engine) as load, patch.object(
                    server, "Server", InspectServer), patch.object(server.logging, "basicConfig"):
                # When:
                code = server.main(["--port", "18078", "--mode", mode])
            # Then:
            self.assertEqual(code, 0)
            load.assert_called_once_with(settings, None)
            self.assertTrue(observed[0][0])
            adapter = observed[0][1]
            if mode == "persona":
                self.assertIs(adapter.engine, engine)
                self.assertEqual(adapter.model_name, "configured-model")
            else:
                self.assertIsNone(adapter)

    def test_contract_errors_do_not_call_engine(self):
        # Given:
        engine = Engine()
        with serving(server.Service(
                engine, persona_generator=PersonaInference(engine, "test"))) as port:
            data = request()
            data["persona"]["register"] = "해요체"  # Proposed field is not on the wire.
            # When:
            status, _ = call(port, data, path=PATH)
        # Then:
        self.assertEqual(status, 400)
        self.assertEqual(engine.calls, 0)
