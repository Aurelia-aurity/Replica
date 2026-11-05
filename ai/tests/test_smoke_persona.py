"""Exercise prepared smoke control flow without loading a model or using a GPU."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_server import Engine

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("smoke_persona", SCRIPTS / "smoke_persona.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class HookHandle:
    def __init__(self, model):
        self.model = model

    def remove(self):
        self.model.hook = None


class HookModel:
    """A fake first-forward event, not GPU execution evidence."""
    hook = None

    def register_forward_hook(self, hook):
        self.hook = hook
        return HookHandle(self)


class SmokeEngine(Engine):
    def __init__(self):
        super().__init__()
        self.model = HookModel()

    def generate(self, req, prepared):
        if self.model.hook is not None:
            self.model.hook(self.model, (), None)
        prompt = "".join(chr(n) for n in prepared.token_ids)
        if req.max_new_tokens == 1:
            self.answer = "{"
        elif "<persona>" in prompt:
            citations = [] if "<evidence>\n[]" in prompt else [1]
            self.answer = json.dumps({"message": "부산에 갔어요.", "evidence_used": citations})
        else:
            self.answer = "hello"
        return super().generate(req, prepared)


class SmokeTests(unittest.TestCase):
    def test_prepared_gpu_smoke_runs_all_cases_with_fake_engine(self):
        # Given:
        engine = SmokeEngine()
        output = io.StringIO()
        with patch.object(smoke.baseline, "read_settings", return_value={
                "model_id": "Qwen/Qwen3-8B"}), patch.object(
                smoke, "Inference", return_value=engine), redirect_stdout(output):
            # When:
            code = smoke.main(["--model-path", "synthetic-unused", "--timeout", "5"])
        # Then:
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(records[-1], {"failures": 0, "suite": "persona-gpu-smoke"})
        self.assertEqual(len(records), 11)
        self.assertTrue(all(record["result"] == "PASS" for record in records[:-1]))
        self.assertEqual(engine.calls, 8)
