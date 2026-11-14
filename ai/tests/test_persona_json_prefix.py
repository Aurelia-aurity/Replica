"""Real synthetic smoke35891 failures; JSON forcing is not output repair."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import inference
from persona_adapter import PersonaInference
import server
from test_persona import PATH
from test_server import Engine, call, serving

FIXTURES = Path(__file__).parent / "fixtures/smoke35891"


class FramingEngine(Engine):
    def generate(self, req, prepared):
        self.answer = (
            "".join(chr(token) for token in req.output_prefix_ids)
            + json.dumps("synthetic framed answer") + ',"evidence_used":[]}')
        return super().generate(req, prepared)


class JsonPrefixTests(unittest.TestCase):
    def test_captured_plain_replies_show_missing_generation_constraint(self):
        for file in sorted(FIXTURES.glob("*.json")):
            # Given: verbatim real GPU output, not a fabricated repaired envelope.
            capture = json.loads(file.read_text())
            engine = Engine()
            engine.answer = capture["raw_output"]
            engine.reason = capture["finish_reason"]
            adapter = PersonaInference(engine, "Qwen/Qwen3-8B")
            with self.subTest(case=capture["case"]), serving(server.Service(
                    engine, persona_generator=adapter)) as port:
                # When: replay the actual plaintext response through the real adapter.
                with self.assertLogs(server.LOGGER, level="ERROR"):
                    status, body = call(port, capture["request"], path=PATH)
                # Then: malformed JSON is still rejected; prevent emitting that mode.
                self.assertEqual((status, body), (
                    500, server.error_body(500, capture["request_id"])))
                self.assertEqual(engine.calls, 1)

    def test_forwarded_prefix_can_be_completed_in_one_valid_persona_decode(self):
        # Given: a decoder that completes the adapter's prefix without repairing it.
        capture = json.loads((FIXTURES / "no-evidence-daily.json").read_text())
        engine = FramingEngine()
        adapter = PersonaInference(engine, "synthetic-model")
        with serving(server.Service(engine, persona_generator=adapter)) as port:
            # When: the actual adapter forwards its prefix through the HTTP route.
            status, body = call(port, capture["request"], path=PATH)
        # Then: strict parsing succeeds with one decode and the expected data.
        self.assertEqual(status, 200)
        self.assertEqual(body["message"], "synthetic framed answer")
        self.assertEqual(body["evidence_used"], [])
        self.assertEqual(engine.calls, 1)

    def test_real_decoder_forces_only_prefix_inside_unchanged_budget(self):
        # Given: the real decoder with fake tensor/model boundaries.
        engine = inference.Inference.__new__(inference.Inference)
        engine.torch = Mock()
        engine.torch.inference_mode = nullcontext
        engine.generation_config = lambda **kw: SimpleNamespace(**kw)
        engine.model = Mock()
        engine.model.generation_config.eos_token_id = 2
        output = MagicMock()
        output.__getitem__.return_value = SimpleNamespace(tolist=lambda: [3, 4, 2])
        engine.model.generate.return_value = output
        engine.tokenizer = Mock(eos_token_id=2)
        engine.tokenizer.decode.return_value = '{"message":"a","evidence_used":[]}'
        req = inference.Request("test", "question", (), 160, (11, 12, 13))
        # When:
        engine.generate(req, inference.Prepared((9, 8), 0, 0))
        # Then: the model receives the forcing processor, never extra tokens or calls.
        kwargs = engine.model.generate.call_args.kwargs
        self.assertEqual(kwargs["generation_config"].max_new_tokens, 160)
        self.assertEqual(len(kwargs["logits_processor"]), 1)
        processor = kwargs["logits_processor"][0]
        for step, token in enumerate(req.output_prefix_ids):
            scores = MagicMock()
            self.assertIs(processor(SimpleNamespace(shape=(1, 2 + step)), scores), scores)
            scores.fill_.assert_called_once_with(float("-inf"))
            scores.__setitem__.assert_called_once_with((slice(None), token), 0)
        scores = MagicMock()
        self.assertIs(processor(SimpleNamespace(shape=(1, 5)), scores), scores)
        scores.fill_.assert_not_called()
        scores.__setitem__.assert_not_called()
        engine.model.generate.assert_called_once()
        engine.torch.cuda.synchronize.assert_called_once_with(0)
